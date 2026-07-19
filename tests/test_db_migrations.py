from __future__ import annotations

import sqlite3
import os
import tempfile
import unittest

from sqlalchemy import text

from bot.db.engine import (
    _SQLITE_VOTE_BAN_DEDUPE_SQL,
    _SQLITE_VOTE_BAN_INDEX_SQL,
    init_db,
)


class VoteBanMigrationTests(unittest.TestCase):
    def test_duplicate_open_sessions_are_closed_before_unique_index(self) -> None:
        connection = sqlite3.connect(":memory:")
        self.addCleanup(connection.close)
        connection.execute(
            "CREATE TABLE vote_ban_sessions ("
            "id INTEGER PRIMARY KEY, group_id BIGINT NOT NULL, "
            "target_user_id BIGINT NOT NULL, status TEXT NOT NULL)"
        )
        connection.executemany(
            "INSERT INTO vote_ban_sessions "
            "(id, group_id, target_user_id, status) VALUES (?, ?, ?, ?)",
            [
                (1, -100, 7, "active"),
                (2, -100, 7, "enforcing"),
                (3, -100, 7, "active"),
                (4, -100, 8, "active"),
            ],
        )

        connection.execute(_SQLITE_VOTE_BAN_DEDUPE_SQL)
        connection.execute(_SQLITE_VOTE_BAN_INDEX_SQL)

        open_rows = connection.execute(
            "SELECT id, target_user_id, status FROM vote_ban_sessions "
            "WHERE status IN ('active', 'enforcing') ORDER BY id"
        ).fetchall()
        self.assertEqual(open_rows, [(2, 7, "enforcing"), (4, 8, "active")])
        with self.assertRaises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO vote_ban_sessions "
                "(id, group_id, target_user_id, status) "
                "VALUES (5, -100, 7, 'active')"
            )


class ForeignKeyMigrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_duplicate_violation_source_keys_are_preserved_but_detached(self) -> None:
        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        engine = None
        try:
            engine, _ = await init_db(f"sqlite+aiosqlite:///{path}")
            await engine.dispose()
            engine = None

            connection = sqlite3.connect(path)
            connection.executescript(
                """
                DROP INDEX ix_violations_group_source_message;
                INSERT INTO groups (id, title, settings) VALUES (-100, '', '{}');
                INSERT INTO violations
                    (id, group_id, user_id, message_text, action_taken,
                     source_message_id)
                    VALUES
                    (1, -100, 7, 'first', 'warn', 55),
                    (2, -100, 7, 'duplicate', 'warn', 55);
                """
            )
            connection.commit()
            connection.close()

            engine, session_factory = await init_db(
                f"sqlite+aiosqlite:///{path}"
            )
            async with session_factory() as session:
                rows = (
                    await session.execute(
                        text(
                            "SELECT id, source_message_id FROM violations "
                            "ORDER BY id"
                        )
                    )
                ).all()
                index_rows = (
                    await session.execute(text("PRAGMA index_list(violations)"))
                ).all()
            self.assertEqual(rows, [(1, 55), (2, None)])
            source_index = next(
                row
                for row in index_rows
                if row[1] == "ix_violations_group_source_message"
            )
            self.assertTrue(bool(source_index[2]))
        finally:
            if engine is not None:
                await engine.dispose()
            for suffix in ("", "-wal", "-shm"):
                try:
                    os.remove(path + suffix)
                except OSError:
                    pass

    async def test_partial_profile_cache_nulls_are_safely_invalidated(self) -> None:
        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        connection = sqlite3.connect(path)
        connection.executescript(
            """
            CREATE TABLE groups (
                id BIGINT PRIMARY KEY, title VARCHAR(255) NOT NULL DEFAULT '',
                settings JSON NOT NULL DEFAULT '{}', created_at DATETIME
            );
            CREATE TABLE user_profile_screens (
                group_id BIGINT, user_id BIGINT,
                profile_hash VARCHAR(64), checked_at DATETIME
            );
            INSERT INTO groups (id, title, settings) VALUES (-100, '', '{}');
            INSERT INTO user_profile_screens
                (group_id, user_id, profile_hash, checked_at) VALUES
                (-100, 1, NULL, NULL),
                (NULL, 2, 'invalid-group', CURRENT_TIMESTAMP),
                (-100, NULL, 'invalid-user', CURRENT_TIMESTAMP);
            """
        )
        connection.commit()
        connection.close()

        engine = None
        try:
            engine, session_factory = await init_db(
                f"sqlite+aiosqlite:///{path}"
            )
            async with session_factory() as session:
                rows = (
                    await session.execute(
                        text(
                            "SELECT group_id, user_id, profile_hash, checked_at "
                            "FROM user_profile_screens"
                        )
                    )
                ).all()
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0][0:3], (-100, 1, ""))
            self.assertIsNotNone(rows[0][3])
        finally:
            if engine is not None:
                await engine.dispose()
            for suffix in ("", "-wal", "-shm"):
                try:
                    os.remove(path + suffix)
                except OSError:
                    pass

    async def test_superficially_similar_vote_ban_index_is_rebuilt_canonically(self) -> None:
        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        engine = None
        try:
            engine, _ = await init_db(f"sqlite+aiosqlite:///{path}")
            await engine.dispose()
            engine = None

            connection = sqlite3.connect(path)
            connection.execute("DROP INDEX ix_vote_ban_open_target")
            connection.execute(
                "CREATE UNIQUE INDEX ix_vote_ban_open_target "
                "ON vote_ban_sessions (group_id, target_user_id, status) "
                "WHERE status IN ('active', 'enforcing')"
            )
            base = (
                -100,
                7,
                "target",
                "",
                1,
                "starter",
                "",
                "",
                "command",
                0,
                5,
                0,
            )
            connection.execute(
                "INSERT INTO vote_ban_sessions "
                "(group_id, target_user_id, target_display, target_username, "
                "starter_user_id, starter_display, reason, evidence, source, "
                "target_message_id, threshold, message_id, status, deadline_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'active', "
                "datetime('now', '+1 hour'))",
                base,
            )
            connection.execute(
                "INSERT INTO vote_ban_sessions "
                "(group_id, target_user_id, target_display, target_username, "
                "starter_user_id, starter_display, reason, evidence, source, "
                "target_message_id, threshold, message_id, status, deadline_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'enforcing', "
                "datetime('now', '+1 hour'))",
                base,
            )
            connection.commit()
            connection.close()

            engine, _ = await init_db(f"sqlite+aiosqlite:///{path}")
            await engine.dispose()
            engine = None

            connection = sqlite3.connect(path)
            open_rows = connection.execute(
                "SELECT status FROM vote_ban_sessions "
                "WHERE group_id=-100 AND target_user_id=7 "
                "AND status IN ('active', 'enforcing')"
            ).fetchall()
            index_sql = connection.execute(
                "SELECT sql FROM sqlite_master WHERE type='index' "
                "AND name='ix_vote_ban_open_target'"
            ).fetchone()[0]
            self.assertEqual(open_rows, [("enforcing",)])
            self.assertIn("(group_id, target_user_id)", index_sql)
            self.assertNotIn("target_user_id, status", index_sql)
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(
                    "INSERT INTO vote_ban_sessions "
                    "(group_id, target_user_id, target_display, target_username, "
                    "starter_user_id, starter_display, reason, evidence, source, "
                    "target_message_id, threshold, message_id, status, deadline_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'active', "
                    "datetime('now', '+1 hour'))",
                    base,
                )
            connection.close()
        finally:
            if engine is not None:
                await engine.dispose()
            for suffix in ("", "-wal", "-shm"):
                try:
                    os.remove(path + suffix)
                except OSError:
                    pass

    async def test_legacy_orphans_and_profile_cache_are_migrated_safely(self) -> None:
        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        connection = sqlite3.connect(path)
        connection.executescript(
            """
            CREATE TABLE groups (
                id BIGINT PRIMARY KEY, title VARCHAR(255) NOT NULL DEFAULT '',
                settings JSON NOT NULL DEFAULT '{}', created_at DATETIME
            );
            CREATE TABLE moderation_rules (
                id INTEGER PRIMARY KEY, group_id BIGINT NOT NULL,
                rule_type VARCHAR(32) NOT NULL, pattern TEXT NOT NULL DEFAULT '',
                action VARCHAR(32) NOT NULL DEFAULT 'warn', enabled BOOLEAN NOT NULL DEFAULT 1
            );
            CREATE TABLE violations (
                id INTEGER PRIMARY KEY, group_id BIGINT NOT NULL,
                user_id BIGINT NOT NULL, rule_id INTEGER,
                message_text TEXT NOT NULL DEFAULT '',
                action_taken VARCHAR(32) NOT NULL DEFAULT 'warn',
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY(group_id) REFERENCES groups(id),
                FOREIGN KEY(rule_id) REFERENCES moderation_rules(id)
            );
            CREATE TABLE user_profile_screens (
                user_id BIGINT PRIMARY KEY,
                profile_hash VARCHAR(64) NOT NULL DEFAULT '',
                checked_at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL
            );
            CREATE TABLE knowledge_entries (
                id INTEGER PRIMARY KEY, group_id BIGINT NOT NULL,
                content TEXT NOT NULL DEFAULT '',
                FOREIGN KEY(group_id) REFERENCES groups(id)
            );
            CREATE TABLE authorized_groups (
                group_id BIGINT PRIMARY KEY, authorized_by BIGINT,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE admins (
                id INTEGER PRIMARY KEY, group_id BIGINT NOT NULL,
                user_id BIGINT NOT NULL, role VARCHAR(32) NOT NULL DEFAULT 'admin',
                FOREIGN KEY(group_id) REFERENCES authorized_groups(group_id)
                    ON DELETE CASCADE
            );
            INSERT INTO groups (id, title, settings) VALUES (-100, '', '{}');
            INSERT INTO authorized_groups (group_id) VALUES (-100);
            INSERT INTO admins (id, group_id, user_id) VALUES
                (1, -100, 42), (2, -999, 43);
            INSERT INTO moderation_rules (id, group_id, rule_type, pattern, action, enabled)
                VALUES (7, -100, 'llm', 'rule', 'warn', 1);
            INSERT INTO violations (id, group_id, user_id, rule_id) VALUES
                (1, -100, 10, 7), (2, -100, 11, 999),
                (3, -999, 12, NULL);
            INSERT INTO user_profile_screens (user_id, profile_hash) VALUES (10, 'legacy');
            INSERT INTO knowledge_entries (id, group_id, content)
                VALUES (1, -777, 'legacy orphan knowledge');
            """
        )
        connection.commit()
        connection.close()

        engine = None
        try:
            engine, session_factory = await init_db(
                f"sqlite+aiosqlite:///{path}"
            )
            async with session_factory() as session:
                self.assertEqual(
                    (await session.execute(text("PRAGMA foreign_keys"))).scalar_one(),
                    1,
                )
                self.assertEqual(
                    (
                        await session.execute(
                            text("SELECT COUNT(*) FROM groups WHERE id = -777")
                        )
                    ).scalar_one(),
                    1,
                )
                table_sql = (
                    await session.execute(
                        text(
                            "SELECT sql FROM sqlite_master "
                            "WHERE type='table' AND name='violations'"
                        )
                    )
                ).scalar_one()
                self.assertIn("ON DELETE SET NULL", table_sql.upper())
                violation_columns = {
                    row[1]
                    for row in (
                        await session.execute(text("PRAGMA table_info(violations)"))
                    ).all()
                }
                self.assertTrue(
                    {
                        "source_message_id",
                        "warning_count",
                        "ban_enforced",
                        "notice_sent_at",
                    }.issubset(violation_columns)
                )
                violation_indexes = {
                    row[1]: bool(row[2])
                    for row in (
                        await session.execute(text("PRAGMA index_list(violations)"))
                    ).all()
                }
                self.assertTrue(
                    violation_indexes["ix_violations_group_source_message"]
                )
                source_index_columns = tuple(
                    row[2]
                    for row in (
                        await session.execute(
                            text(
                                "PRAGMA index_info("
                                "ix_violations_group_source_message)"
                            )
                        )
                    ).all()
                )
                self.assertEqual(
                    source_index_columns,
                    ("group_id", "source_message_id"),
                )
                rule_ids = list(
                    (
                        await session.execute(
                            text("SELECT rule_id FROM violations ORDER BY id")
                        )
                    ).scalars()
                )
                self.assertEqual(rule_ids, [7, None, None])
                legacy_sources = list(
                    (
                        await session.execute(
                            text(
                                "SELECT source_message_id FROM violations "
                                "ORDER BY id"
                            )
                        )
                    ).scalars()
                )
                self.assertEqual(legacy_sources, [None, None, None])
                self.assertEqual(
                    (
                        await session.execute(
                            text("SELECT COUNT(*) FROM groups WHERE id = -999")
                        )
                    ).scalar_one(),
                    1,
                )
                profile_columns = {
                    row[1]: row[5]
                    for row in (
                        await session.execute(
                            text("PRAGMA table_info(user_profile_screens)")
                        )
                    ).all()
                }
                self.assertEqual(profile_columns["group_id"], 1)
                self.assertEqual(profile_columns["user_id"], 2)
                self.assertEqual(
                    (
                        await session.execute(
                            text("SELECT COUNT(*) FROM user_profile_screens")
                        )
                    ).scalar_one(),
                    0,
                )
                admin_groups = list(
                    (
                        await session.execute(
                            text("SELECT group_id FROM admins ORDER BY id")
                        )
                    ).scalars()
                )
                self.assertEqual(admin_groups, [-100])
                admin_table_sql = (
                    await session.execute(
                        text(
                            "SELECT sql FROM sqlite_master "
                            "WHERE type='table' AND name='admins'"
                        )
                    )
                ).scalar_one()
                self.assertIn("REFERENCES AUTHORIZED_GROUPS", admin_table_sql.upper())
                self.assertIn("ON DELETE CASCADE", admin_table_sql.upper())

                await session.execute(
                    text("DELETE FROM authorized_groups WHERE group_id = -100")
                )
                await session.commit()
                self.assertEqual(
                    (
                        await session.execute(text("SELECT COUNT(*) FROM admins"))
                    ).scalar_one(),
                    0,
                )

                await session.execute(
                    text("DELETE FROM moderation_rules WHERE id = 7")
                )
                await session.commit()
                remaining = (
                    await session.execute(
                        text("SELECT rule_id FROM violations WHERE id = 1")
                    )
                ).scalar_one()
                self.assertIsNone(remaining)
        finally:
            if engine is not None:
                await engine.dispose()
            for suffix in ("", "-wal", "-shm"):
                try:
                    os.remove(path + suffix)
                except OSError:
                    pass


class DurableQueueMigrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_legacy_webhook_and_telegram_cleanup_tables_upgrade_before_indexes(self) -> None:
        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        connection = sqlite3.connect(path)
        connection.executescript(
            """
            CREATE TABLE webhook_inbox_updates (
                update_id BIGINT PRIMARY KEY,
                payload JSON NOT NULL DEFAULT '{}',
                attempts INTEGER NOT NULL DEFAULT 0,
                lease_until DATETIME,
                completed_at DATETIME,
                last_error TEXT NOT NULL DEFAULT '',
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
            );
            CREATE INDEX ix_webhook_inbox_completed_lease
                ON webhook_inbox_updates (completed_at, lease_until);
            CREATE INDEX ix_webhook_inbox_recovery
                ON webhook_inbox_updates (completed_at, lease_until);

            CREATE TABLE telegram_delete_jobs (
                chat_id BIGINT NOT NULL,
                message_id BIGINT NOT NULL,
                due_at DATETIME
            );
            INSERT INTO telegram_delete_jobs
                (chat_id, message_id, due_at) VALUES
                (-100, 9, datetime('now', '+2 hours')),
                (-100, 9, datetime('now', '+1 hour'));
            """
        )
        connection.commit()
        connection.close()

        engine = None
        try:
            engine, session_factory = await init_db(
                f"sqlite+aiosqlite:///{path}"
            )
            async with session_factory() as session:
                webhook_columns = {
                    str(row[1])
                    for row in (
                        await session.execute(
                            text("PRAGMA table_info(webhook_inbox_updates)")
                        )
                    ).all()
                }
                self.assertIn("next_attempt_at", webhook_columns)
                self.assertIn("dead_lettered_at", webhook_columns)
                webhook_indexes = {
                    str(row[1])
                    for row in (
                        await session.execute(
                            text("PRAGMA index_list(webhook_inbox_updates)")
                        )
                    ).all()
                }
                self.assertIn("ix_webhook_inbox_recovery", webhook_indexes)
                self.assertNotIn("ix_webhook_inbox_completed_lease", webhook_indexes)
                webhook_recovery_columns = tuple(
                    str(row[2])
                    for row in (
                        await session.execute(
                            text("PRAGMA index_info(ix_webhook_inbox_recovery)")
                        )
                    ).all()
                )
                self.assertEqual(
                    webhook_recovery_columns,
                    (
                        "completed_at",
                        "dead_lettered_at",
                        "next_attempt_at",
                        "lease_until",
                    ),
                )

                cleanup_columns = {
                    str(row[1])
                    for row in (
                        await session.execute(
                            text("PRAGMA table_info(telegram_delete_jobs)")
                        )
                    ).all()
                }
                self.assertTrue(
                    {
                        "attempts",
                        "lease_until",
                        "last_error",
                        "created_at",
                        "updated_at",
                    }
                    <= cleanup_columns
                )
                cleanup_indexes = {
                    str(row[1]): bool(row[2])
                    for row in (
                        await session.execute(
                            text("PRAGMA index_list(telegram_delete_jobs)")
                        )
                    ).all()
                }
                self.assertTrue(cleanup_indexes["ix_telegram_delete_jobs_message"])
                self.assertIn("ix_telegram_delete_jobs_recovery", cleanup_indexes)
                rows = (
                    await session.execute(
                        text(
                            "SELECT chat_id, message_id, due_at, attempts, last_error "
                            "FROM telegram_delete_jobs"
                        )
                    )
                ).all()
                self.assertEqual(len(rows), 1)
                self.assertEqual(rows[0][0:2], (-100, 9))
                self.assertIsNotNone(rows[0][2])
                self.assertEqual(rows[0][3:], (0, ""))

                occurrence_columns = {
                    str(row[1])
                    for row in (
                        await session.execute(
                            text(
                                "PRAGMA table_info(scheduled_message_occurrences)"
                            )
                        )
                    ).all()
                }
                self.assertTrue(
                    {
                        "scheduled_message_id",
                        "occurrence_at",
                        "attempts",
                        "lease_until",
                        "next_attempt_at",
                        "last_error",
                    }
                    <= occurrence_columns
                )
                occurrence_indexes = {
                    str(row[1]): bool(row[2])
                    for row in (
                        await session.execute(
                            text(
                                "PRAGMA index_list(scheduled_message_occurrences)"
                            )
                        )
                    ).all()
                }
                self.assertTrue(
                    occurrence_indexes[
                        "ix_scheduled_message_occurrence_unique"
                    ]
                )
                self.assertIn(
                    "ix_scheduled_message_occurrence_recovery",
                    occurrence_indexes,
                )
        finally:
            if engine is not None:
                await engine.dispose()
            for suffix in ("", "-wal", "-shm"):
                try:
                    os.remove(path + suffix)
                except OSError:
                    pass


if __name__ == "__main__":
    unittest.main()
