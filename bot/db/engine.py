from __future__ import annotations

import logging
import os
from pathlib import Path
import sqlite3
from urllib.parse import unquote

from sqlalchemy import event, text
from sqlalchemy.engine import URL, make_url
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from bot.db.models import Base
from bot.db.sqlite_session import SQLiteSafeAsyncSession

log = logging.getLogger(__name__)

_SQLITE_TIMEOUT_SECONDS = 5
_SQLITE_BUSY_TIMEOUT_MS = _SQLITE_TIMEOUT_SECONDS * 1000
_SQLITE_SCHEMA_VERSION = 1
_SQLITE_VOTE_BAN_DEDUPE_SQL = (
    "UPDATE vote_ban_sessions AS candidate "
    "SET status = 'cancelled' "
    "WHERE candidate.status IN ('active', 'enforcing') "
    "AND candidate.id <> ("
    "SELECT keeper.id FROM vote_ban_sessions AS keeper "
    "WHERE keeper.group_id = candidate.group_id "
    "AND keeper.target_user_id = candidate.target_user_id "
    "AND keeper.status IN ('active', 'enforcing') "
    "ORDER BY CASE keeper.status WHEN 'enforcing' THEN 0 ELSE 1 END, "
    "keeper.id DESC LIMIT 1"
    ")"
)
_SQLITE_VOTE_BAN_INDEX_SQL = (
    "CREATE UNIQUE INDEX IF NOT EXISTS ix_vote_ban_open_target "
    "ON vote_ban_sessions (group_id, target_user_id) "
    "WHERE status IN ('active', 'enforcing')"
)


def _restrict_sqlite_file_permissions(path: Path) -> None:
    """Keep the database and WAL sidecars private on POSIX filesystems."""

    for candidate in (path, Path(f"{path}-wal"), Path(f"{path}-shm")):
        try:
            if candidate.exists():
                os.chmod(candidate, 0o600)
        except OSError:
            log.warning("Could not restrict SQLite file permissions: %s", candidate)


def _normalize_database_url(url: str) -> tuple[URL, Path | None]:
    """Validate the database URL and return its parsed SQLite path, if any.

    A trailing carriage return in ``DATABASE_URL`` previously created a
    second, perfectly valid SQLite file whose name only differed by an
    invisible character.  Reject control characters before SQLAlchemy or the
    OS can interpret the path, while harmless outer ASCII spaces are trimmed.
    """
    if not isinstance(url, str):
        raise TypeError("database URL must be a string")
    for char in url:
        if ord(char) < 32 or ord(char) == 127:
            raise ValueError(
                f"database URL contains forbidden control character U+{ord(char):04X}"
            )

    normalized = url.strip(" ")
    if not normalized:
        raise ValueError("database URL must not be empty")
    parsed = make_url(normalized)
    if not parsed.drivername.startswith("sqlite"):
        return parsed, None

    database = str(parsed.database or "")
    decoded_database = unquote(database)
    for char in decoded_database:
        if ord(char) < 32 or ord(char) == 127:
            raise ValueError(
                "SQLite database path contains a percent-encoded control character"
            )
    if decoded_database.rstrip() != decoded_database:
        raise ValueError("SQLite database path must not end with whitespace")
    if not database or database == ":memory:" or database.startswith("file:"):
        return parsed, None

    path = Path(database).expanduser()
    # Validation, directory creation, shadow detection and the engine must all
    # refer to the exact same file.  Keeping the original URL here would make
    # ``sqlite:///~/...`` create $HOME directories but open a literal cwd/~/...
    # database instead.
    parsed = parsed.set(database=str(path))
    return parsed, path


def _sqlite_shadow_evidence(path: Path) -> tuple[int, int, int] | None:
    """Return (domain rows, runtime rows, max runtime revision) for a DB."""

    if not path.is_file() or path.stat().st_size <= 0:
        return None
    connection = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
    try:
        tables = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            ).fetchall()
        }
        if not tables:
            return None
        domain_rows = 0
        for table in tables - {"runtime_config"}:
            quoted = '"' + table.replace('"', '""') + '"'
            domain_rows += max(
                0,
                int(connection.execute(f"SELECT COUNT(*) FROM {quoted}").fetchone()[0]),
            )
        runtime_rows = 0
        runtime_revision = 0
        if "runtime_config" in tables:
            runtime_rows, runtime_revision = connection.execute(
                "SELECT COUNT(*), COALESCE(MAX(revision), 0) FROM runtime_config"
            ).fetchone()
        return int(domain_rows), int(runtime_rows), int(runtime_revision)
    finally:
        connection.close()


def _warn_about_sqlite_shadow_paths(path: Path) -> None:
    """Report sibling files whose names collapse to the selected DB name.

    The configured clean path remains usable; only a URL that itself contains
    invisible/control suffixes is rejected.  Operators still get an explicit
    warning about legacy shadow files so they can archive them deliberately.
    """
    parent = path.parent
    try:
        siblings = list(parent.iterdir())
    except OSError:
        return
    target_name = path.name.rstrip(" \t\r\n\v\f\0")
    suspicious: list[Path] = []
    for sibling in siblings:
        if (
            sibling == path
            or sibling.name.rstrip(" \t\r\n\v\f\0") != target_name
        ):
            continue
        try:
            if path.exists() and sibling.samefile(path):
                continue
        except OSError:
            pass
        suspicious.append(sibling)
    if suspicious:
        log.error(
            "Suspicious SQLite shadow database path(s) detected for %s: %s",
            path,
            ", ".join(repr(str(item)) for item in suspicious),
        )
        try:
            clean_evidence = _sqlite_shadow_evidence(path)
            dangerous: list[Path] = []
            for item in suspicious:
                if not item.is_file() or item.stat().st_size <= 0:
                    continue
                try:
                    shadow_evidence = _sqlite_shadow_evidence(item)
                except (OSError, sqlite3.DatabaseError):
                    # A non-empty collapsed sibling which is not a readable DB
                    # is still ambiguous and must be handled explicitly.
                    dangerous.append(item)
                    continue
                if shadow_evidence is None:
                    continue
                shadow_domain, shadow_runtime_rows, shadow_revision = shadow_evidence
                if clean_evidence is None:
                    dangerous.append(item)
                    continue
                clean_domain, clean_runtime_rows, clean_revision = clean_evidence
                # Never use mtimes to guess which database is real.  User data
                # in the shadow is always ambiguous.  An otherwise-empty legacy
                # bootstrap DB is only harmless when the selected DB has actual
                # domain data and a strictly newer runtime configuration.
                if (
                    shadow_domain > 0
                    or clean_domain == 0
                    or (
                        shadow_runtime_rows > 0
                        and (
                            clean_runtime_rows == 0
                            or shadow_revision >= clean_revision
                        )
                    )
                ):
                    dangerous.append(item)
        except (OSError, sqlite3.DatabaseError):
            dangerous = suspicious
        if dangerous:
            raise RuntimeError(
                "Refusing to start with an ambiguous SQLite shadow database; "
                "archive or explicitly migrate: "
                + ", ".join(repr(str(item)) for item in dangerous)
            )


async def _sqlite_table_columns(conn, table: str) -> set[str]:
    result = await conn.execute(text(f"PRAGMA table_info({table})"))
    return {str(row[1]) for row in result.fetchall()}


async def _sqlite_table_exists(conn, table: str) -> bool:
    result = await conn.execute(
        text(
            "SELECT 1 FROM sqlite_master "
            "WHERE type='table' AND name=:table LIMIT 1"
        ),
        {"table": table},
    )
    return result.first() is not None


async def _sqlite_ensure_column(conn, table: str, column: str, column_def_sql: str) -> bool:
    columns = await _sqlite_table_columns(conn, table)
    if column in columns:
        return False
    await conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {column_def_sql}"))
    log.info("Migrated: added %s.%s", table, column)
    return True


async def _sqlite_get_user_version(conn) -> int:
    result = await conn.execute(text("PRAGMA user_version"))
    row = result.first()
    return int(row[0] or 0) if row else 0


async def _sqlite_set_user_version(conn, version: int) -> None:
    await conn.execute(text(f"PRAGMA user_version = {int(version)}"))


async def _sqlite_has_unique_index(
    conn,
    table: str,
    columns: tuple[str, ...],
) -> bool:
    quoted_table = '"' + table.replace('"', '""') + '"'
    for index_row in (
        await conn.execute(text(f"PRAGMA index_list({quoted_table})"))
    ).all():
        if not bool(index_row[2]):
            continue
        index_name = str(index_row[1] or "")
        quoted_index = '"' + index_name.replace('"', '""') + '"'
        actual_columns = tuple(
            str(row[2] or "")
            for row in (
                await conn.execute(text(f"PRAGMA index_info({quoted_index})"))
            ).all()
        )
        if actual_columns == columns:
            return True
    return False


async def _sqlite_named_index_matches(
    conn,
    *,
    table: str,
    name: str,
    columns: tuple[str, ...],
    unique: bool,
) -> bool:
    quoted_table = '"' + table.replace('"', '""') + '"'
    for index_row in (
        await conn.execute(text(f"PRAGMA index_list({quoted_table})"))
    ).all():
        if str(index_row[1] or "") != name:
            continue
        if bool(index_row[2]) != bool(unique):
            return False
        quoted_index = '"' + name.replace('"', '""') + '"'
        actual_columns = tuple(
            str(row[2] or "")
            for row in (
                await conn.execute(text(f"PRAGMA index_info({quoted_index})"))
            ).all()
        )
        return actual_columns == columns
    return False


async def _sqlite_migrate_violation_rule_fk(conn) -> bool:
    """Canonicalize violation FKs and durable moderation idempotency fields."""
    columns = await _sqlite_table_columns(conn, "violations")
    if not columns:
        return False

    changed = False
    changed |= await _sqlite_ensure_column(
        conn,
        "violations",
        "source_message_id",
        "source_message_id BIGINT",
    )
    changed |= await _sqlite_ensure_column(
        conn,
        "violations",
        "warning_count",
        "warning_count INTEGER",
    )
    changed |= await _sqlite_ensure_column(
        conn,
        "violations",
        "ban_enforced",
        "ban_enforced BOOLEAN",
    )
    changed |= await _sqlite_ensure_column(
        conn,
        "violations",
        "notice_sent_at",
        "notice_sent_at DATETIME",
    )
    columns = await _sqlite_table_columns(conn, "violations")

    orphan_result = await conn.execute(
        text(
            "UPDATE violations SET rule_id = NULL "
            "WHERE rule_id IS NOT NULL AND NOT EXISTS ("
            "SELECT 1 FROM moderation_rules WHERE moderation_rules.id = violations.rule_id"
            ")"
        )
    )
    repaired = max(0, int(orphan_result.rowcount or 0))
    rule_fk_is_canonical = any(
        str(row[2] or "") == "moderation_rules"
        and str(row[3] or "") == "rule_id"
        and str(row[4] or "") == "id"
        and str(row[6] or "").upper() == "SET NULL"
        for row in (await conn.execute(text("PRAGMA foreign_key_list(violations)"))).all()
    )
    if rule_fk_is_canonical:
        if repaired:
            log.warning("Migrated: cleared %d orphan violation rule reference(s)", repaired)
        # A partially deployed build could have written duplicate source ids
        # before the unique index existed. Preserve every historical row while
        # retaining the oldest row as the canonical durable event.
        dedupe = await conn.execute(
            text(
                "UPDATE violations AS duplicate SET source_message_id = NULL "
                "WHERE source_message_id IS NOT NULL AND id <> ("
                "SELECT MIN(canonical.id) FROM violations AS canonical "
                "WHERE canonical.group_id = duplicate.group_id "
                "AND canonical.source_message_id = duplicate.source_message_id)"
            )
        )
        deduped = max(0, int(dedupe.rowcount or 0))
        index_is_canonical = await _sqlite_named_index_matches(
            conn,
            table="violations",
            name="ix_violations_group_source_message",
            columns=("group_id", "source_message_id"),
            unique=True,
        )
        if not index_is_canonical:
            await conn.execute(
                text("DROP INDEX IF EXISTS ix_violations_group_source_message")
            )
            await conn.execute(
                text(
                    "CREATE UNIQUE INDEX ix_violations_group_source_message "
                    "ON violations (group_id, source_message_id)"
                )
            )
            changed = True
        if deduped:
            log.warning(
                "Migrated: detached idempotency keys from %d duplicate violation row(s)",
                deduped,
            )
        return bool(changed or repaired or deduped)

    dedupe = await conn.execute(
        text(
            "UPDATE violations AS duplicate SET source_message_id = NULL "
            "WHERE source_message_id IS NOT NULL AND id <> ("
            "SELECT MIN(canonical.id) FROM violations AS canonical "
            "WHERE canonical.group_id = duplicate.group_id "
            "AND canonical.source_message_id = duplicate.source_message_id)"
        )
    )
    deduped = max(0, int(dedupe.rowcount or 0))
    if deduped:
        log.warning(
            "Migrated: detached idempotency keys from %d duplicate violation row(s)",
            deduped,
        )

    await conn.execute(text("DROP TABLE IF EXISTS violations_fk_migration"))
    await conn.execute(
        text(
            "CREATE TABLE violations_fk_migration ("
            "id INTEGER NOT NULL PRIMARY KEY, "
            "group_id BIGINT NOT NULL, "
            "user_id BIGINT NOT NULL, "
            "rule_id INTEGER, "
            "message_text TEXT NOT NULL DEFAULT '', "
            "action_taken VARCHAR(32) NOT NULL DEFAULT 'warn', "
            "source_message_id BIGINT, "
            "warning_count INTEGER, "
            "ban_enforced BOOLEAN, "
            "notice_sent_at DATETIME, "
            "created_at DATETIME DEFAULT CURRENT_TIMESTAMP, "
            "FOREIGN KEY(group_id) REFERENCES groups(id), "
            "FOREIGN KEY(rule_id) REFERENCES moderation_rules(id) ON DELETE SET NULL"
            ")"
        )
    )
    await conn.execute(
        text(
            "INSERT INTO violations_fk_migration "
            "(id, group_id, user_id, rule_id, message_text, action_taken, "
            "source_message_id, warning_count, ban_enforced, notice_sent_at, created_at) "
            "SELECT id, group_id, user_id, rule_id, message_text, action_taken, "
            "source_message_id, warning_count, ban_enforced, notice_sent_at, created_at "
            "FROM violations"
        )
    )
    await conn.execute(text("DROP TABLE violations"))
    await conn.execute(
        text("ALTER TABLE violations_fk_migration RENAME TO violations")
    )
    await conn.execute(
        text(
            "CREATE UNIQUE INDEX IF NOT EXISTS ix_violations_group_source_message "
            "ON violations (group_id, source_message_id)"
        )
    )
    log.info(
        "Migrated: violations.rule_id now uses ON DELETE SET NULL%s",
        f"; cleared {repaired} orphan reference(s)" if repaired else "",
    )
    return True


async def _sqlite_migrate_profile_screen_scope(conn) -> bool:
    """Move the profile-screen cache from user-only to group/user scope.

    Legacy hashes cannot be assigned safely to a group because the hash embeds
    that group's rules.  They are deliberately invalidated so each member is
    screened once under the correct policy after upgrade.
    """
    result = await conn.execute(text("PRAGMA table_info(user_profile_screens)"))
    rows = result.fetchall()
    if not rows:
        return False
    columns = {str(row[1]) for row in rows}
    primary_key = {
        str(row[1]): int(row[5] or 0)
        for row in rows
        if int(row[5] or 0) > 0
    }
    not_null_columns = {str(row[1]) for row in rows if bool(row[3])}
    if (
        columns >= {"group_id", "user_id", "profile_hash", "checked_at"}
        and primary_key == {"group_id": 1, "user_id": 2}
        and {"group_id", "user_id", "profile_hash", "checked_at"}
        <= not_null_columns
    ):
        return False

    count_result = await conn.execute(text("SELECT COUNT(*) FROM user_profile_screens"))
    old_count = int(count_result.scalar_one() or 0)
    await conn.execute(text("DROP TABLE IF EXISTS user_profile_screens_group_scoped"))
    await conn.execute(
        text(
            "CREATE TABLE user_profile_screens_group_scoped ("
            "group_id BIGINT NOT NULL, "
            "user_id BIGINT NOT NULL, "
            "profile_hash VARCHAR(64) NOT NULL DEFAULT '', "
            "checked_at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL, "
            "PRIMARY KEY (group_id, user_id)"
            ")"
        )
    )
    if "group_id" in columns:
        profile_hash_expr = (
            "COALESCE(profile_hash, '')" if "profile_hash" in columns else "''"
        )
        checked_at_expr = (
            "COALESCE(checked_at, CURRENT_TIMESTAMP)"
            if "checked_at" in columns
            else "CURRENT_TIMESTAMP"
        )
        await conn.execute(
            text(
                "INSERT OR REPLACE INTO user_profile_screens_group_scoped "
                "(group_id, user_id, profile_hash, checked_at) "
                f"SELECT group_id, user_id, {profile_hash_expr}, {checked_at_expr} "
                "FROM user_profile_screens "
                "WHERE group_id IS NOT NULL AND user_id IS NOT NULL"
            )
        )
    await conn.execute(text("DROP TABLE user_profile_screens"))
    await conn.execute(
        text(
            "ALTER TABLE user_profile_screens_group_scoped "
            "RENAME TO user_profile_screens"
        )
    )
    log.info(
        "Migrated: profile-screen cache is group scoped; invalidated %d legacy row(s)",
        old_count if "group_id" not in columns else 0,
    )
    return True


async def _sqlite_repair_missing_group_parents(conn) -> int:
    """Preserve legacy child rows by creating minimal missing group parents."""
    repaired = 0
    tables = (
        await conn.execute(
            text(
                "SELECT name FROM sqlite_master "
                "WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            )
        )
    ).scalars().all()
    for raw_table in tables:
        table = str(raw_table or "")
        if not table or table == "groups":
            continue
        quoted_table = '"' + table.replace('"', '""') + '"'
        foreign_keys = (
            await conn.execute(text(f"PRAGMA foreign_key_list({quoted_table})"))
        ).all()
        for foreign_key in foreign_keys:
            parent_table = str(foreign_key[2] or "")
            child_column = str(foreign_key[3] or "")
            parent_column = str(foreign_key[4] or "id")
            if parent_table != "groups" or parent_column not in {"", "id"}:
                continue
            if not child_column:
                continue
            quoted_column = '"' + child_column.replace('"', '""') + '"'
            result = await conn.execute(
                text(
                    "INSERT OR IGNORE INTO groups (id, title, settings) "
                    f"SELECT DISTINCT child.{quoted_column}, '', '{{}}' "
                    f"FROM {quoted_table} AS child "
                    f"LEFT JOIN groups ON groups.id = child.{quoted_column} "
                    f"WHERE child.{quoted_column} IS NOT NULL AND groups.id IS NULL"
                )
            )
            repaired += max(0, int(result.rowcount or 0))
    if repaired:
        log.warning(
            "Migrated: created %d missing group parent row(s) for legacy data",
            repaired,
        )
    return repaired


async def _sqlite_validate_foreign_keys(conn) -> None:
    result = await conn.execute(text("PRAGMA foreign_key_check"))
    violations = result.fetchall()
    if not violations:
        return
    preview = ", ".join(
        f"table={row[0]} rowid={row[1]} parent={row[2]}"
        for row in violations[:10]
    )
    raise RuntimeError(
        f"SQLite foreign-key integrity check failed ({len(violations)} row(s)): {preview}"
    )


async def _sqlite_cleanup_stale_group_admins(conn) -> int:
    result = await conn.execute(
        text(
            "DELETE FROM admins WHERE NOT EXISTS ("
            "SELECT 1 FROM authorized_groups "
            "WHERE authorized_groups.group_id = admins.group_id"
            ")"
        )
    )
    removed = max(0, int(result.rowcount or 0))
    if removed:
        log.warning(
            "Migrated: removed %d stale admin grant(s) for unauthorized groups",
            removed,
        )
    return removed


async def _sqlite_cleanup_orphan_vote_ban_votes(conn) -> int:
    result = await conn.execute(
        text(
            "DELETE FROM vote_ban_votes WHERE NOT EXISTS ("
            "SELECT 1 FROM vote_ban_sessions "
            "WHERE vote_ban_sessions.id = vote_ban_votes.session_id"
            ")"
        )
    )
    removed = max(0, int(result.rowcount or 0))
    if removed:
        log.warning("Migrated: removed %d orphan vote-ban vote(s)", removed)
    return removed


async def _sqlite_migrate_admin_authorization_fk(conn) -> bool:
    """Bind admin grants to an active group authorization with CASCADE."""
    columns = await _sqlite_table_columns(conn, "admins")
    if not columns:
        return False
    admin_fk_is_canonical = any(
        str(row[2] or "") == "authorized_groups"
        and str(row[3] or "") == "group_id"
        and str(row[4] or "") == "group_id"
        and str(row[6] or "").upper() == "CASCADE"
        for row in (await conn.execute(text("PRAGMA foreign_key_list(admins)"))).all()
    )
    unique_is_canonical = await _sqlite_has_unique_index(
        conn,
        "admins",
        ("group_id", "user_id"),
    )
    if admin_fk_is_canonical and unique_is_canonical:
        return False

    await conn.execute(text("DROP TABLE IF EXISTS admins_authorization_fk"))
    await conn.execute(
        text(
            "CREATE TABLE admins_authorization_fk ("
            "id INTEGER NOT NULL PRIMARY KEY AUTOINCREMENT, "
            "group_id BIGINT NOT NULL, "
            "user_id BIGINT NOT NULL, "
            "role VARCHAR(32) NOT NULL DEFAULT 'admin', "
            "FOREIGN KEY(group_id) REFERENCES authorized_groups(group_id) "
            "ON DELETE CASCADE"
            ")"
        )
    )
    await conn.execute(
        text(
            "INSERT INTO admins_authorization_fk (id, group_id, user_id, role) "
            "SELECT admins.id, admins.group_id, admins.user_id, admins.role "
            "FROM admins JOIN authorized_groups "
            "ON authorized_groups.group_id = admins.group_id "
            "WHERE admins.id = ("
            "SELECT MAX(candidate.id) FROM admins AS candidate "
            "WHERE candidate.group_id = admins.group_id "
            "AND candidate.user_id = admins.user_id"
            ")"
        )
    )
    await conn.execute(text("DROP TABLE admins"))
    await conn.execute(
        text("ALTER TABLE admins_authorization_fk RENAME TO admins")
    )
    await conn.execute(
        text(
            "CREATE UNIQUE INDEX ix_admin_group_user "
            "ON admins (group_id, user_id)"
        )
    )
    log.info("Migrated: admins now cascade with authorized group deletion")
    return True


async def _sqlite_migrate_message_vector_timestamps(conn) -> bool:
    user_version = await _sqlite_get_user_version(conn)
    if user_version >= _SQLITE_SCHEMA_VERSION:
        return False

    columns = await _sqlite_table_columns(conn, "message_vectors")
    changed = False

    # Legacy SQLite rows were stored via CURRENT_TIMESTAMP (UTC). Shift them once to Asia/Shanghai.
    if "created_at" in columns:
        await conn.execute(
            text(
                "UPDATE message_vectors "
                "SET created_at = datetime(created_at, '+8 hours') "
                "WHERE created_at IS NOT NULL"
            )
        )
        changed = True
    if "last_accessed" in columns:
        await conn.execute(
            text(
                "UPDATE message_vectors "
                "SET last_accessed = datetime(last_accessed, '+8 hours') "
                "WHERE last_accessed IS NOT NULL"
            )
        )
        changed = True

    await _sqlite_set_user_version(conn, _SQLITE_SCHEMA_VERSION)
    if changed:
        log.info("Migrated: normalized message_vectors timestamps to Asia/Shanghai")
    return changed


async def _sqlite_migrate_join_verifications(conn) -> bool:
    """Convert captcha/link-token rows into durable permission releases.

    Members represented by these legacy rows may still be restricted in the
    Telegram group. Dropping the table would destroy the only recovery work
    item and could leave them muted forever. Preserve the common identity and
    prompt fields as immediately-expired ``releasing`` leases; the current
    sweeper will idempotently restore permissions and then remove the row.
    """
    columns = await _sqlite_table_columns(conn, "join_verifications")
    if not columns or not ({"answer", "token"} & columns):
        return False
    display_expr = "display_name" if "display_name" in columns else "''"
    prompt_expr = "prompt_message_id" if "prompt_message_id" in columns else "0"
    deadline_expr = "deadline_at" if "deadline_at" in columns else "CURRENT_TIMESTAMP"
    created_expr = "created_at" if "created_at" in columns else "CURRENT_TIMESTAMP"

    await conn.execute(text("DROP TABLE IF EXISTS join_verifications_legacy_recovery"))
    await conn.execute(
        text(
            "CREATE TABLE join_verifications_legacy_recovery ("
            "id INTEGER NOT NULL PRIMARY KEY AUTOINCREMENT, "
            "group_id BIGINT NOT NULL, "
            "user_id BIGINT NOT NULL, "
            "kind VARCHAR(32) NOT NULL DEFAULT 'join', "
            "provider VARCHAR(32) NOT NULL DEFAULT 'turnstile', "
            "reason TEXT NOT NULL DEFAULT '', "
            "status VARCHAR(16) NOT NULL DEFAULT 'pending', "
            "lease_until DATETIME, "
            "display_name VARCHAR(255) NOT NULL DEFAULT '', "
            "prompt_message_id BIGINT NOT NULL DEFAULT 0, "
            "deadline_at DATETIME NOT NULL, "
            "created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP"
            ")"
        )
    )
    await conn.execute(
        text(
            "INSERT INTO join_verifications_legacy_recovery "
            "(id, group_id, user_id, kind, provider, reason, status, lease_until, "
            "display_name, prompt_message_id, deadline_at, created_at) "
            "SELECT id, group_id, user_id, 'join', 'turnstile', "
            "'legacy verification recovery', 'releasing', "
            "datetime('now', '-1 second'), "
            f"{display_expr}, {prompt_expr}, {deadline_expr}, {created_expr} "
            "FROM join_verifications"
        )
    )
    await conn.execute(text("DROP TABLE join_verifications"))
    await conn.execute(
        text(
            "ALTER TABLE join_verifications_legacy_recovery "
            "RENAME TO join_verifications"
        )
    )
    await conn.execute(
        text(
            "CREATE UNIQUE INDEX ix_join_verification_group_user "
            "ON join_verifications (group_id, user_id)"
        )
    )
    await conn.execute(
        text(
            "CREATE INDEX ix_join_verifications_user_id "
            "ON join_verifications (user_id)"
        )
    )
    await conn.execute(
        text(
            "CREATE INDEX ix_join_verifications_status_lease "
            "ON join_verifications (status, lease_until)"
        )
    )
    log.info(
        "Migrated: legacy join verifications queued for permission recovery"
    )
    return True


async def _sqlite_migrate_join_verification_autoincrement(conn) -> bool:
    """Make verification IDs monotonic while preserving pending rows.

    Older SQLite databases used a plain INTEGER primary key, so deleting the
    last pending row allowed the next issuance to reuse its ID. That collides
    with the web server's short-lived idempotence cache.
    """
    result = await conn.execute(
        text(
            "SELECT sql FROM sqlite_master "
            "WHERE type = 'table' AND name = 'join_verifications'"
        )
    )
    row = result.first()
    table_sql = str(row[0] or "") if row else ""
    if not table_sql or "AUTOINCREMENT" in table_sql.upper():
        return False

    columns = await _sqlite_table_columns(conn, "join_verifications")
    status_expr = "status" if "status" in columns else "'pending'"
    lease_expr = "lease_until" if "lease_until" in columns else "NULL"

    await conn.execute(
        text("DROP TABLE IF EXISTS join_verifications_autoincrement")
    )
    await conn.execute(
        text(
            "CREATE TABLE join_verifications_autoincrement ("
            "id INTEGER NOT NULL PRIMARY KEY AUTOINCREMENT, "
            "group_id BIGINT NOT NULL, "
            "user_id BIGINT NOT NULL, "
            "kind VARCHAR(32) NOT NULL DEFAULT 'join', "
            "provider VARCHAR(32) NOT NULL DEFAULT 'turnstile', "
            "reason TEXT NOT NULL DEFAULT '', "
            "status VARCHAR(16) NOT NULL DEFAULT 'pending', "
            "lease_until DATETIME, "
            "display_name VARCHAR(255) NOT NULL, "
            "prompt_message_id BIGINT NOT NULL, "
            "deadline_at DATETIME NOT NULL, "
            "created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP"
            ")"
        )
    )
    await conn.execute(
        text(
            "INSERT INTO join_verifications_autoincrement "
            "(id, group_id, user_id, kind, provider, reason, status, lease_until, "
            "display_name, prompt_message_id, deadline_at, created_at) "
            "SELECT id, group_id, user_id, kind, provider, reason, "
            f"{status_expr}, {lease_expr}, display_name, prompt_message_id, "
            "deadline_at, created_at FROM join_verifications"
        )
    )
    await conn.execute(text("DROP TABLE join_verifications"))
    await conn.execute(
        text(
            "ALTER TABLE join_verifications_autoincrement "
            "RENAME TO join_verifications"
        )
    )
    await conn.execute(
        text(
            "CREATE UNIQUE INDEX ix_join_verification_group_user "
            "ON join_verifications (group_id, user_id)"
        )
    )
    await conn.execute(
        text(
            "CREATE INDEX ix_join_verifications_user_id "
            "ON join_verifications (user_id)"
        )
    )
    await conn.execute(
        text(
            "CREATE INDEX ix_join_verifications_status_lease "
            "ON join_verifications (status, lease_until)"
        )
    )
    log.info("Migrated: enabled AUTOINCREMENT for join_verifications")
    return True


async def _sqlite_ensure_vote_ban_open_index(conn) -> None:
    """Repair legacy duplicate open polls before enforcing uniqueness."""
    result = await conn.execute(
        text(
            "SELECT sql FROM sqlite_master "
            "WHERE type = 'index' AND name = 'ix_vote_ban_open_target'"
        )
    )
    row = result.first()
    if row is not None:
        # Rebuild unconditionally.  Token-presence checks cannot distinguish
        # the canonical partial unique index from a superficially similar but
        # weaker index such as UNIQUE(group_id, target_user_id, status), which
        # permits one active and one enforcing row for the same target.
        await conn.execute(text("DROP INDEX ix_vote_ban_open_target"))
        log.info("Migrated: rebuilt canonical vote-ban open-session index")

    # Earlier development builds could create more than one active/enforcing
    # row before the partial unique index existed. Prefer an enforcing row
    # (Telegram side effects may already be in progress), otherwise the newest
    # active row, and close every duplicate before CREATE UNIQUE INDEX.
    await conn.execute(
        text(_SQLITE_VOTE_BAN_DEDUPE_SQL)
    )
    await conn.execute(text(_SQLITE_VOTE_BAN_INDEX_SQL))


async def _sqlite_migrate_telegram_delete_jobs(conn) -> None:
    """Bring development/legacy cleanup tables to the durable scheduler schema."""

    columns = await _sqlite_table_columns(conn, "telegram_delete_jobs")
    if not columns:
        return

    if "id" not in columns:
        # A short-lived development build used the message pair itself as the
        # key.  Rebuild rather than dropping recoverable cleanup work merely
        # because SQLite cannot ALTER a new autoincrement primary key in place.
        await conn.execute(text("DROP TABLE IF EXISTS telegram_delete_jobs_rebuilt"))
        await conn.execute(
            text(
                "CREATE TABLE telegram_delete_jobs_rebuilt ("
                "id INTEGER NOT NULL PRIMARY KEY AUTOINCREMENT, "
                "chat_id BIGINT NOT NULL, message_id BIGINT NOT NULL, "
                "due_at DATETIME NOT NULL, attempts INTEGER NOT NULL DEFAULT 0, "
                "lease_until DATETIME, last_error TEXT NOT NULL DEFAULT '', "
                "created_at DATETIME DEFAULT CURRENT_TIMESTAMP, "
                "updated_at DATETIME DEFAULT CURRENT_TIMESTAMP)"
            )
        )
        if {"chat_id", "message_id"} <= columns:
            due_expr = (
                "MIN(COALESCE(due_at, CURRENT_TIMESTAMP))"
                if "due_at" in columns
                else "CURRENT_TIMESTAMP"
            )
            attempts_expr = (
                "MAX(COALESCE(attempts, 0))" if "attempts" in columns else "0"
            )
            lease_expr = "MAX(lease_until)" if "lease_until" in columns else "NULL"
            error_expr = (
                "MAX(COALESCE(last_error, ''))"
                if "last_error" in columns
                else "''"
            )
            created_expr = (
                "MIN(COALESCE(created_at, CURRENT_TIMESTAMP))"
                if "created_at" in columns
                else "CURRENT_TIMESTAMP"
            )
            updated_expr = (
                "MAX(COALESCE(updated_at, CURRENT_TIMESTAMP))"
                if "updated_at" in columns
                else "CURRENT_TIMESTAMP"
            )
            await conn.execute(
                text(
                    "INSERT INTO telegram_delete_jobs_rebuilt "
                    "(chat_id, message_id, due_at, attempts, lease_until, "
                    "last_error, created_at, updated_at) "
                    f"SELECT chat_id, message_id, {due_expr}, {attempts_expr}, "
                    f"{lease_expr}, {error_expr}, {created_expr}, {updated_expr} "
                    "FROM telegram_delete_jobs "
                    "WHERE chat_id <> 0 AND message_id > 0 "
                    "GROUP BY chat_id, message_id"
                )
            )
        await conn.execute(text("DROP TABLE telegram_delete_jobs"))
        await conn.execute(
            text(
                "ALTER TABLE telegram_delete_jobs_rebuilt "
                "RENAME TO telegram_delete_jobs"
            )
        )
        columns = await _sqlite_table_columns(conn, "telegram_delete_jobs")
        log.info("Migrated: rebuilt legacy Telegram cleanup job primary key")

    # ``create_all`` creates the canonical table for normal upgrades.  These
    # guards also support databases produced by intermediate development
    # builds without discarding still-pending message deletions.
    await _sqlite_ensure_column(
        conn,
        "telegram_delete_jobs",
        "chat_id",
        "chat_id BIGINT NOT NULL DEFAULT 0",
    )
    await _sqlite_ensure_column(
        conn,
        "telegram_delete_jobs",
        "message_id",
        "message_id BIGINT NOT NULL DEFAULT 0",
    )
    await _sqlite_ensure_column(
        conn,
        "telegram_delete_jobs",
        "due_at",
        "due_at DATETIME",
    )
    await _sqlite_ensure_column(
        conn,
        "telegram_delete_jobs",
        "attempts",
        "attempts INTEGER NOT NULL DEFAULT 0",
    )
    await _sqlite_ensure_column(
        conn,
        "telegram_delete_jobs",
        "lease_until",
        "lease_until DATETIME",
    )
    await _sqlite_ensure_column(
        conn,
        "telegram_delete_jobs",
        "last_error",
        "last_error TEXT NOT NULL DEFAULT ''",
    )
    await _sqlite_ensure_column(
        conn,
        "telegram_delete_jobs",
        "created_at",
        "created_at DATETIME",
    )
    await _sqlite_ensure_column(
        conn,
        "telegram_delete_jobs",
        "updated_at",
        "updated_at DATETIME",
    )
    await conn.execute(
        text(
            "UPDATE telegram_delete_jobs SET "
            "due_at = COALESCE(due_at, CURRENT_TIMESTAMP), "
            "created_at = COALESCE(created_at, CURRENT_TIMESTAMP), "
            "updated_at = COALESCE(updated_at, CURRENT_TIMESTAMP), "
            "attempts = COALESCE(attempts, 0), "
            "last_error = COALESCE(last_error, '')"
        )
    )
    # Invalid zero identifiers can only originate in a partial legacy table;
    # Telegram never assigns either value to a deletable message.
    await conn.execute(
        text(
            "DELETE FROM telegram_delete_jobs "
            "WHERE chat_id = 0 OR message_id <= 0"
        )
    )
    await conn.execute(
        text(
            "UPDATE telegram_delete_jobs AS keeper SET due_at = ("
            "SELECT MIN(candidate.due_at) FROM telegram_delete_jobs AS candidate "
            "WHERE candidate.chat_id = keeper.chat_id "
            "AND candidate.message_id = keeper.message_id) "
            "WHERE keeper.id = ("
            "SELECT MIN(first_row.id) FROM telegram_delete_jobs AS first_row "
            "WHERE first_row.chat_id = keeper.chat_id "
            "AND first_row.message_id = keeper.message_id)"
        )
    )
    await conn.execute(
        text(
            "DELETE FROM telegram_delete_jobs AS duplicate "
            "WHERE EXISTS ("
            "SELECT 1 FROM telegram_delete_jobs AS keeper "
            "WHERE keeper.chat_id = duplicate.chat_id "
            "AND keeper.message_id = duplicate.message_id "
            "AND keeper.id < duplicate.id)"
        )
    )
    if not await _sqlite_named_index_matches(
        conn,
        table="telegram_delete_jobs",
        name="ix_telegram_delete_jobs_message",
        columns=("chat_id", "message_id"),
        unique=True,
    ):
        await conn.execute(text("DROP INDEX IF EXISTS ix_telegram_delete_jobs_message"))
        await conn.execute(
            text(
                "CREATE UNIQUE INDEX ix_telegram_delete_jobs_message "
                "ON telegram_delete_jobs (chat_id, message_id)"
            )
        )
    if not await _sqlite_named_index_matches(
        conn,
        table="telegram_delete_jobs",
        name="ix_telegram_delete_jobs_recovery",
        columns=("due_at", "lease_until"),
        unique=False,
    ):
        await conn.execute(text("DROP INDEX IF EXISTS ix_telegram_delete_jobs_recovery"))
        await conn.execute(
            text(
                "CREATE INDEX ix_telegram_delete_jobs_recovery "
                "ON telegram_delete_jobs (due_at, lease_until)"
            )
        )


async def init_db(
    url: str = "sqlite+aiosqlite:///./data/bot.db",
) -> tuple[AsyncEngine, async_sessionmaker[AsyncSession]]:
    """Create engine, ensure tables exist, return engine + session factory."""
    parsed_url, sqlite_path = _normalize_database_url(url)
    is_sqlite = parsed_url.drivername.startswith("sqlite")
    if sqlite_path is not None:
        sqlite_path.parent.mkdir(parents=True, exist_ok=True)
        _warn_about_sqlite_shadow_paths(sqlite_path)

    engine = create_async_engine(
        parsed_url,
        echo=False,
        connect_args={"timeout": _SQLITE_TIMEOUT_SECONDS} if is_sqlite else {},
    )

    if is_sqlite:
        @event.listens_for(engine.sync_engine, "connect")
        def _configure_sqlite_connection(dbapi_connection, _connection_record) -> None:
            cursor = dbapi_connection.cursor()
            try:
                cursor.execute("PRAGMA foreign_keys=ON")
                cursor.execute("PRAGMA journal_mode=WAL")
                cursor.execute(f"PRAGMA busy_timeout={_SQLITE_BUSY_TIMEOUT_MS}")
                cursor.execute("PRAGMA synchronous=NORMAL")
            finally:
                cursor.close()
            if sqlite_path is not None:
                _restrict_sqlite_file_permissions(sqlite_path)

    async with engine.begin() as conn:
        if is_sqlite:
            # New ORM indexes may reference columns added by an intermediate
            # release.  Add those columns before ``create_all`` attempts to
            # create the indexes on an already-existing table.
            if await _sqlite_table_exists(conn, "webhook_inbox_updates"):
                await _sqlite_ensure_column(
                    conn,
                    "webhook_inbox_updates",
                    "next_attempt_at",
                    "next_attempt_at DATETIME",
                )
                await _sqlite_ensure_column(
                    conn,
                    "webhook_inbox_updates",
                    "dead_lettered_at",
                    "dead_lettered_at DATETIME",
                )
            if await _sqlite_table_exists(conn, "telegram_delete_jobs"):
                await _sqlite_migrate_telegram_delete_jobs(conn)
        await conn.run_sync(Base.metadata.create_all)
        if is_sqlite:
            # Ensure message_vectors schema keeps compatibility with previous versions.
            await _sqlite_ensure_column(
                conn,
                "message_vectors",
                "role",
                "role VARCHAR(16) NOT NULL DEFAULT 'user'",
            )
            await _sqlite_ensure_column(
                conn,
                "message_vectors",
                "content",
                "content TEXT NOT NULL DEFAULT ''",
            )
            await _sqlite_ensure_column(
                conn,
                "message_vectors",
                "embedding",
                "embedding BLOB",
            )
            await _sqlite_ensure_column(
                conn,
                "message_vectors",
                "sender_id",
                "sender_id BIGINT",
            )
            await _sqlite_ensure_column(
                conn,
                "message_vectors",
                "sender_name",
                "sender_name TEXT NOT NULL DEFAULT ''",
            )
            await _sqlite_ensure_column(
                conn,
                "message_vectors",
                "message_type",
                "message_type VARCHAR(64) NOT NULL DEFAULT 'text'",
            )
            await conn.execute(
                text(
                    "CREATE INDEX IF NOT EXISTS ix_message_vectors_group_created "
                    "ON message_vectors (group_id, created_at)"
                )
            )
            await conn.execute(
                text(
                    "CREATE INDEX IF NOT EXISTS ix_message_vectors_message_id "
                    "ON message_vectors (message_id)"
                )
            )
            await _sqlite_migrate_message_vector_timestamps(conn)
            await _sqlite_migrate_join_verifications(conn)
            await _sqlite_ensure_column(
                conn,
                "join_verifications",
                "kind",
                "kind VARCHAR(32) NOT NULL DEFAULT 'join'",
            )
            await _sqlite_ensure_column(
                conn,
                "join_verifications",
                "provider",
                "provider VARCHAR(32) NOT NULL DEFAULT 'turnstile'",
            )
            await _sqlite_ensure_column(
                conn,
                "join_verifications",
                "reason",
                "reason TEXT NOT NULL DEFAULT ''",
            )
            await _sqlite_migrate_join_verification_autoincrement(conn)
            await _sqlite_ensure_column(
                conn,
                "join_verifications",
                "status",
                "status VARCHAR(16) NOT NULL DEFAULT 'pending'",
            )
            await _sqlite_ensure_column(
                conn,
                "join_verifications",
                "lease_until",
                "lease_until DATETIME",
            )
            await conn.execute(
                text(
                    "CREATE INDEX IF NOT EXISTS ix_join_verifications_status_lease "
                    "ON join_verifications (status, lease_until)"
                )
            )
            await _sqlite_migrate_profile_screen_scope(conn)
            await _sqlite_cleanup_stale_group_admins(conn)
            await _sqlite_migrate_admin_authorization_fk(conn)
            await _sqlite_cleanup_orphan_vote_ban_votes(conn)
            await _sqlite_repair_missing_group_parents(conn)
            await _sqlite_migrate_violation_rule_fk(conn)
            await _sqlite_ensure_column(
                conn,
                "group_members",
                "patrol_hash",
                "patrol_hash VARCHAR(64) NOT NULL DEFAULT ''",
            )
            # Vote-ban tables may already exist from an earlier development
            # build. Keep them forward-compatible with the richer audit data.
            await _sqlite_ensure_column(
                conn,
                "vote_ban_sessions",
                "evidence",
                "evidence TEXT NOT NULL DEFAULT ''",
            )
            await _sqlite_ensure_column(
                conn,
                "vote_ban_sessions",
                "source",
                "source VARCHAR(32) NOT NULL DEFAULT 'command'",
            )
            await _sqlite_ensure_column(
                conn,
                "vote_ban_sessions",
                "target_message_id",
                "target_message_id BIGINT NOT NULL DEFAULT 0",
            )
            await _sqlite_ensure_column(
                conn,
                "vote_ban_sessions",
                "enforcing_started_at",
                "enforcing_started_at DATETIME",
            )
            await _sqlite_ensure_column(
                conn,
                "vote_ban_sessions",
                "resolution",
                "resolution VARCHAR(16) NOT NULL DEFAULT ''",
            )
            await _sqlite_ensure_column(
                conn,
                "vote_ban_sessions",
                "resolver_user_id",
                "resolver_user_id BIGINT NOT NULL DEFAULT 0",
            )
            await _sqlite_ensure_column(
                conn,
                "vote_ban_sessions",
                "resolver_display",
                "resolver_display VARCHAR(255) NOT NULL DEFAULT ''",
            )
            await _sqlite_ensure_vote_ban_open_index(conn)
            await _sqlite_ensure_column(
                conn,
                "keyword_replies",
                "buttons",
                "buttons JSON NOT NULL DEFAULT '[]'",
            )
            await _sqlite_ensure_column(
                conn,
                "scheduled_messages",
                "buttons",
                "buttons JSON NOT NULL DEFAULT '[]'",
            )
            await _sqlite_ensure_column(
                conn,
                "webhook_inbox_updates",
                "next_attempt_at",
                "next_attempt_at DATETIME",
            )
            await _sqlite_ensure_column(
                conn,
                "webhook_inbox_updates",
                "dead_lettered_at",
                "dead_lettered_at DATETIME",
            )
            await conn.execute(
                text("DROP INDEX IF EXISTS ix_webhook_inbox_completed_lease")
            )
            if not await _sqlite_named_index_matches(
                conn,
                table="webhook_inbox_updates",
                name="ix_webhook_inbox_recovery",
                columns=(
                    "completed_at",
                    "dead_lettered_at",
                    "next_attempt_at",
                    "lease_until",
                ),
                unique=False,
            ):
                await conn.execute(
                    text("DROP INDEX IF EXISTS ix_webhook_inbox_recovery")
                )
                await conn.execute(
                    text(
                        "CREATE INDEX ix_webhook_inbox_recovery "
                        "ON webhook_inbox_updates "
                        "(completed_at, dead_lettered_at, next_attempt_at, "
                        "lease_until)"
                    )
                )
            await _sqlite_migrate_telegram_delete_jobs(conn)
            await _sqlite_validate_foreign_keys(conn)

    if sqlite_path is not None:
        _restrict_sqlite_file_permissions(sqlite_path)

    session_factory = async_sessionmaker(
        engine,
        class_=SQLiteSafeAsyncSession,
        expire_on_commit=False,
        autoflush=False,
    )
    log.info(
        "Database initialized: %s",
        engine.url.render_as_string(hide_password=True),
    )
    return engine, session_factory
