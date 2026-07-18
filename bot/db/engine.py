from __future__ import annotations

import logging
from pathlib import Path

from sqlalchemy import event, text
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


async def _sqlite_table_columns(conn, table: str) -> set[str]:
    result = await conn.execute(text(f"PRAGMA table_info({table})"))
    return {str(row[1]) for row in result.fetchall()}


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
    """Drop older join_verifications schemas (captcha-era / link-token-era).

    Both had NOT NULL columns (answer/attempts, token) that break inserts
    from the current Mini App model. Rows are transient pending
    verifications, so dropping is safe; the affected users just get a fresh
    challenge on their next join.
    """
    columns = await _sqlite_table_columns(conn, "join_verifications")
    if not columns or not ({"answer", "token"} & columns):
        return False
    await conn.execute(text("DROP TABLE join_verifications"))
    await conn.run_sync(Base.metadata.create_all)
    log.info("Migrated: recreated join_verifications for Mini App verification")
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
            "(id, group_id, user_id, kind, provider, reason, display_name, "
            "prompt_message_id, deadline_at, created_at) "
            "SELECT id, group_id, user_id, kind, provider, reason, display_name, "
            "prompt_message_id, deadline_at, created_at FROM join_verifications"
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
    index_sql = str(row[0] or "") if row else ""
    normalized = " ".join(index_sql.upper().split())
    if index_sql and not (
        "CREATE UNIQUE INDEX" in normalized
        and "GROUP_ID" in normalized
        and "TARGET_USER_ID" in normalized
        and "STATUS" in normalized
        and "ACTIVE" in normalized
        and "ENFORCING" in normalized
    ):
        await conn.execute(text("DROP INDEX ix_vote_ban_open_target"))
        log.warning("Migrated: replaced incompatible vote-ban open-session index")

    # Earlier development builds could create more than one active/enforcing
    # row before the partial unique index existed. Prefer an enforcing row
    # (Telegram side effects may already be in progress), otherwise the newest
    # active row, and close every duplicate before CREATE UNIQUE INDEX.
    await conn.execute(
        text(_SQLITE_VOTE_BAN_DEDUPE_SQL)
    )
    await conn.execute(text(_SQLITE_VOTE_BAN_INDEX_SQL))


async def init_db(
    url: str = "sqlite+aiosqlite:///./data/bot.db",
) -> tuple[AsyncEngine, async_sessionmaker[AsyncSession]]:
    """Create engine, ensure tables exist, return engine + session factory."""
    # Ensure data directory exists for SQLite
    if "sqlite" in url:
        db_path = url.split("///")[-1]
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)

    engine = create_async_engine(
        url,
        echo=False,
        connect_args={"timeout": _SQLITE_TIMEOUT_SECONDS} if "sqlite" in url else {},
    )

    if "sqlite" in url:
        @event.listens_for(engine.sync_engine, "connect")
        def _configure_sqlite_connection(dbapi_connection, _connection_record) -> None:
            cursor = dbapi_connection.cursor()
            try:
                cursor.execute("PRAGMA journal_mode=WAL")
                cursor.execute(f"PRAGMA busy_timeout={_SQLITE_BUSY_TIMEOUT_MS}")
                cursor.execute("PRAGMA synchronous=NORMAL")
            finally:
                cursor.close()

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        if "sqlite" in url:
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

    session_factory = async_sessionmaker(
        engine,
        class_=SQLiteSafeAsyncSession,
        expire_on_commit=False,
        autoflush=False,
    )
    log.info("Database initialized: %s", url)
    return engine, session_factory
