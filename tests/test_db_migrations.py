from __future__ import annotations

import sqlite3
import os
import tempfile
import unittest
from datetime import timedelta

from sqlalchemy import create_engine, inspect, text

from bot.db.engine import (
    _SQLITE_VOTE_BAN_DEDUPE_SQL,
    _SQLITE_VOTE_BAN_INDEX_SQL,
    init_db,
)
from bot.db.models import (
    Base,
    GroupMessageArchive,
    KeywordReply,
    ScheduledMessage,
    VoteBanSession,
)
from bot.utils.timezone import now_shanghai_naive


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


class PerformanceIndexTests(unittest.TestCase):
    def test_hot_path_indexes_exist_in_canonical_schema(self) -> None:
        engine = create_engine("sqlite:///:memory:")
        self.addCleanup(engine.dispose)
        Base.metadata.create_all(engine)
        inspector = inspect(engine)

        expected = {
            "moderation_rules": "ix_moderation_rules_group_enabled_id",
            "violations": "ix_violations_group_user_ban",
            "ban_audit_events": "ix_ban_audit_group_id_desc",
            "join_verifications": "ix_join_verifications_status_deadline",
            "vote_ban_sessions": "ix_vote_ban_status_deadline",
            "message_vectors": "ix_message_vectors_group_row",
        }
        for table, index_name in expected.items():
            names = {index["name"] for index in inspector.get_indexes(table)}
            self.assertIn(index_name, names, table)


class GroupMessageArchiveSchemaTests(unittest.TestCase):
    def test_archive_has_lossless_fields_and_group_scoped_indexes(self) -> None:
        engine = create_engine("sqlite:///:memory:")
        self.addCleanup(engine.dispose)
        Base.metadata.create_all(engine)
        inspector = inspect(engine)

        columns = {
            column["name"]
            for column in inspector.get_columns(GroupMessageArchive.__tablename__)
        }
        self.assertTrue(
            {
                "id",
                "group_id",
                "message_key",
                "telegram_message_id",
                "role",
                "direction",
                "sender_kind",
                "sender_id",
                "sender_username",
                "sender_first_name",
                "sender_last_name",
                "sender_display_name",
                "sender_is_bot",
                "sender_is_premium",
                "sender_language_code",
                "sender_chat_id",
                "sender_chat_type",
                "sender_chat_title",
                "author_signature",
                "message_type",
                "content",
                "raw_text",
                "derived_text",
                "sent_at",
                "edited_at",
                "ingested_at",
                "is_reply",
                "reply_to_message_id",
                "reply_to_sender_id",
                "reply_to_sender_name",
                "reply_to_content",
                "message_thread_id",
                "media_group_id",
                "media_metadata",
                "forward_metadata",
                "entities",
                "extra_metadata",
                "access_count",
                "last_accessed",
            }
            <= columns
        )

        indexes = {
            index["name"]: index
            for index in inspector.get_indexes(GroupMessageArchive.__tablename__)
        }
        expected = {
            "ix_group_message_archive_group_message_key": (
                ["group_id", "message_key"],
                True,
            ),
            "ix_group_message_archive_group_sent_id": (
                ["group_id", "sent_at", "id"],
                False,
            ),
            "ix_group_message_archive_group_telegram_message": (
                ["group_id", "telegram_message_id"],
                False,
            ),
            "ix_group_message_archive_group_reply_to_message": (
                ["group_id", "reply_to_message_id"],
                False,
            ),
        }
        for name, (expected_columns, unique) in expected.items():
            self.assertIn(name, indexes)
            self.assertEqual(indexes[name]["column_names"], expected_columns)
            self.assertEqual(bool(indexes[name]["unique"]), unique)


class GroupMessageArchiveMigrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_fts5_projection_is_backfilled_and_tracks_edits_and_deletes(
        self,
    ) -> None:
        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        engine = None
        try:
            engine, session_factory = await init_db(
                f"sqlite+aiosqlite:///{path}"
            )
            async with session_factory() as session:
                trigger_names = set(
                    (
                        await session.execute(
                            text(
                                "SELECT name FROM sqlite_master "
                                "WHERE type='trigger' "
                                "AND name LIKE 'trg_group_message_archive_fts_%'"
                            )
                        )
                    ).scalars()
                )
                await session.execute(
                    text(
                        "INSERT INTO groups (id, title, settings) "
                        "VALUES (-123, 'fts', '{}')"
                    )
                )
                session.add(
                    GroupMessageArchive(
                        group_id=-123,
                        message_key="-123:1",
                        role="user",
                        direction="inbound",
                        sender_kind="user",
                        message_type="text",
                        content="蓝绿发布 deployment",
                        raw_text="蓝绿发布 deployment",
                        sent_at=now_shanghai_naive(),
                        ingested_at=now_shanghai_naive(),
                    )
                )
                await session.commit()
                inserted = (
                    await session.execute(
                        text(
                            "SELECT archive.message_key "
                            "FROM group_message_archive_fts "
                            "JOIN group_message_archive AS archive "
                            "ON archive.id = group_message_archive_fts.rowid "
                            "WHERE group_message_archive_fts MATCH "
                            "'group_scope : \"group_n_123\" "
                            "AND {content raw_text} : \"蓝绿发布\"' "
                            "AND archive.group_id = -123 "
                            "ORDER BY bm25(group_message_archive_fts)"
                        )
                    )
                ).scalars().all()
                await session.execute(
                    text(
                        "UPDATE group_message_archive "
                        "SET content='金丝雀发布', raw_text='金丝雀发布' "
                        "WHERE group_id=-123 AND message_key='-123:1'"
                    )
                )
                await session.commit()
                old_count = (
                    await session.execute(
                        text(
                            "SELECT COUNT(*) FROM group_message_archive_fts "
                            "WHERE group_message_archive_fts MATCH '\"蓝绿发布\"'"
                        )
                    )
                ).scalar_one()
                new_count = (
                    await session.execute(
                        text(
                            "SELECT COUNT(*) FROM group_message_archive_fts "
                            "WHERE group_message_archive_fts MATCH '\"金丝雀发布\"'"
                        )
                    )
                ).scalar_one()
                await session.execute(
                    text(
                        "DELETE FROM group_message_archive "
                        "WHERE group_id=-123 AND message_key='-123:1'"
                    )
                )
                await session.commit()
                final_count = (
                    await session.execute(
                        text("SELECT COUNT(*) FROM group_message_archive_fts")
                    )
                ).scalar_one()

            self.assertEqual(len(trigger_names), 3)
            self.assertEqual(inserted, ["-123:1"])
            self.assertEqual(old_count, 0)
            self.assertEqual(new_count, 1)
            self.assertEqual(final_count, 0)
        finally:
            if engine is not None:
                await engine.dispose()
            for suffix in ("", "-wal", "-shm"):
                try:
                    os.remove(path + suffix)
                except OSError:
                    pass

    async def test_legacy_vectors_are_backfilled_without_rewriting_either_table(
        self,
    ) -> None:
        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        connection = sqlite3.connect(path)
        connection.executescript(
            """
            CREATE TABLE message_vectors (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                group_id BIGINT NOT NULL,
                message_id VARCHAR(64) NOT NULL,
                role VARCHAR(16) NOT NULL DEFAULT 'user',
                importance_score FLOAT NOT NULL DEFAULT 0,
                access_count INTEGER NOT NULL DEFAULT 0,
                vector_id VARCHAR(64) NOT NULL DEFAULT '',
                sender_id BIGINT,
                sender_name TEXT NOT NULL DEFAULT '',
                message_type VARCHAR(64) NOT NULL DEFAULT 'text',
                content TEXT NOT NULL DEFAULT '',
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL,
                last_accessed DATETIME
            );
            INSERT INTO message_vectors (
                group_id, message_id, role, access_count, vector_id,
                sender_id, sender_name, message_type, content,
                created_at, last_accessed
            ) VALUES
                (-100, '-100:77', 'user', 3, '-100:77',
                 42, 'Alice', 'text', 'legacy hello',
                 '2026-07-20 01:02:03', '2026-07-20 02:03:04'),
                (-100, '-100:bot-a', 'assistant', 1, '-100:bot-a',
                 NULL, 'Smart Bot', 'text', 'legacy reply',
                 '2026-07-20 01:03:00', NULL);
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
                            "SELECT message_key, telegram_message_id, role, "
                            "direction, sender_kind, sender_id, "
                            "sender_display_name, sender_is_bot, message_type, "
                            "content, raw_text, derived_text, sent_at, "
                            "ingested_at, access_count, last_accessed "
                            "FROM group_message_archive ORDER BY id"
                        )
                    )
                ).all()
            self.assertEqual(len(rows), 2)
            self.assertEqual(
                tuple(rows[0]),
                (
                    "-100:77",
                    77,
                    "user",
                    "inbound",
                    "user",
                    42,
                    "Alice",
                    0,
                    "text",
                    "legacy hello",
                    "legacy hello",
                    "legacy hello",
                    "2026-07-20 09:02:03",
                    "2026-07-20 09:02:03",
                    3,
                    "2026-07-20 10:03:04",
                ),
            )
            self.assertEqual(
                rows[1][0:8],
                (
                    "-100:bot-a",
                    None,
                    "assistant",
                    "outbound",
                    "bot",
                    None,
                    "Smart Bot",
                    1,
                ),
            )
            await engine.dispose()
            engine = None

            connection = sqlite3.connect(path)
            message_indexes = {
                row[1]: bool(row[2])
                for row in connection.execute(
                    "PRAGMA index_list(message_vectors)"
                ).fetchall()
            }
            self.assertTrue(message_indexes["ix_message_vectors_message_id"])
            connection.execute(
                "UPDATE group_message_archive SET sender_username='richer' "
                "WHERE message_key='-100:77'"
            )
            connection.execute(
                "INSERT INTO message_vectors ("
                "group_id, message_id, role, vector_id, sender_id, sender_name, "
                "message_type, content, created_at"
                ") VALUES (-100, '-100:78', 'user', '-100:78', 43, 'Bob', "
                "'text', 'arrived later', '2026-07-21 12:00:00')"
            )
            connection.commit()
            connection.close()

            engine, session_factory = await init_db(
                f"sqlite+aiosqlite:///{path}"
            )
            async with session_factory() as session:
                archive_count = (
                    await session.execute(
                        text("SELECT COUNT(*) FROM group_message_archive")
                    )
                ).scalar_one()
                original_rows = (
                    await session.execute(
                        text(
                            "SELECT message_id, content FROM message_vectors "
                            "ORDER BY id"
                        )
                    )
                ).all()
                preserved_username = (
                    await session.execute(
                        text(
                            "SELECT sender_username FROM group_message_archive "
                            "WHERE message_key='-100:77'"
                        )
                    )
                ).scalar_one()
                later_timestamp = (
                    await session.execute(
                        text(
                            "SELECT sent_at FROM group_message_archive "
                            "WHERE message_key='-100:78'"
                        )
                    )
                ).scalar_one()

            self.assertEqual(archive_count, 3)
            self.assertEqual(
                original_rows,
                [
                    ("-100:77", "legacy hello"),
                    ("-100:bot-a", "legacy reply"),
                    ("-100:78", "arrived later"),
                ],
            )
            self.assertEqual(preserved_username, "richer")
            self.assertEqual(later_timestamp, "2026-07-21 12:00:00")
        finally:
            if engine is not None:
                await engine.dispose()
            for suffix in ("", "-wal", "-shm"):
                try:
                    os.remove(path + suffix)
                except OSError:
                    pass

    async def test_legacy_unscoped_message_ids_become_group_scoped_and_unique(
        self,
    ) -> None:
        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        connection = sqlite3.connect(path)
        connection.executescript(
            """
            CREATE TABLE message_vectors (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                group_id BIGINT NOT NULL,
                message_id VARCHAR(64) NOT NULL,
                role VARCHAR(16) NOT NULL DEFAULT 'user',
                importance_score FLOAT NOT NULL DEFAULT 0,
                access_count INTEGER NOT NULL DEFAULT 0,
                vector_id VARCHAR(64) NOT NULL DEFAULT '',
                content TEXT NOT NULL DEFAULT '',
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL,
                last_accessed DATETIME
            );
            CREATE INDEX ix_message_vectors_message_id
                ON message_vectors (message_id);
            INSERT INTO message_vectors (
                group_id, message_id, role, vector_id, content, created_at
            ) VALUES
                (-100, '77', 'user', '77', 'group one', '2026-07-20 01:00:00'),
                (-200, '77', 'user', '77', 'group two', '2026-07-20 01:00:01');
            """
        )
        connection.commit()
        connection.close()

        engine = None
        try:
            engine, _session_factory = await init_db(
                f"sqlite+aiosqlite:///{path}"
            )
            await engine.dispose()
            engine = None

            connection = sqlite3.connect(path)
            self.addCleanup(connection.close)
            rows = connection.execute(
                "SELECT group_id, message_id, vector_id FROM message_vectors "
                "ORDER BY group_id DESC"
            ).fetchall()
            indexes = {
                row[1]: bool(row[2])
                for row in connection.execute(
                    "PRAGMA index_list(message_vectors)"
                ).fetchall()
            }
            archive_rows = connection.execute(
                "SELECT group_id, message_key FROM group_message_archive "
                "ORDER BY group_id DESC"
            ).fetchall()

            self.assertEqual(
                rows,
                [(-100, "-100:77", "-100:77"), (-200, "-200:77", "-200:77")],
            )
            self.assertTrue(indexes["ix_message_vectors_message_id"])
            self.assertEqual(
                archive_rows,
                [(-100, "-100:77"), (-200, "-200:77")],
            )
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(
                    "INSERT INTO message_vectors ("
                    "group_id, message_id, role, vector_id, content"
                    ") VALUES (-100, '-100:77', 'user', 'duplicate', 'duplicate')"
                )
        finally:
            if engine is not None:
                await engine.dispose()
            for suffix in ("", "-wal", "-shm"):
                try:
                    os.remove(path + suffix)
                except OSError:
                    pass


class LinkPreviewMigrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_legacy_keyword_and_scheduled_rows_default_to_disabled_preview(
        self,
    ) -> None:
        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        engine = None
        try:
            engine, session_factory = await init_db(f"sqlite+aiosqlite:///{path}")
            async with session_factory() as session:
                session.add(
                    KeywordReply(
                        group_id=-100,
                        keyword="legacy-keyword",
                        reply_text="reply",
                        disable_link_preview=False,
                    )
                )
                session.add(
                    ScheduledMessage(
                        group_id=-100,
                        text="legacy-scheduled",
                        disable_link_preview=False,
                    )
                )
                await session.commit()
            await engine.dispose()
            engine = None

            connection = sqlite3.connect(path)
            connection.execute(
                "ALTER TABLE keyword_replies DROP COLUMN disable_link_preview"
            )
            connection.execute(
                "ALTER TABLE scheduled_messages DROP COLUMN disable_link_preview"
            )
            connection.commit()
            connection.close()

            engine, session_factory = await init_db(f"sqlite+aiosqlite:///{path}")
            async with session_factory() as session:
                keyword_info = (
                    await session.execute(text("PRAGMA table_info(keyword_replies)"))
                ).all()
                scheduled_info = (
                    await session.execute(text("PRAGMA table_info(scheduled_messages)"))
                ).all()
                keyword_value = (
                    await session.execute(
                        text(
                            "SELECT disable_link_preview FROM keyword_replies "
                            "WHERE keyword = 'legacy-keyword'"
                        )
                    )
                ).scalar_one()
                scheduled_value = (
                    await session.execute(
                        text(
                            "SELECT disable_link_preview FROM scheduled_messages "
                            "WHERE text = 'legacy-scheduled'"
                        )
                    )
                ).scalar_one()

                keyword_row = next(
                    row for row in keyword_info if row[1] == "disable_link_preview"
                )
                scheduled_row = next(
                    row for row in scheduled_info if row[1] == "disable_link_preview"
                )
                self.assertEqual(keyword_row[3], 1)
                self.assertEqual(scheduled_row[3], 1)
                self.assertEqual(str(keyword_row[4]).strip("'\""), "1")
                self.assertEqual(str(scheduled_row[4]).strip("'\""), "1")
                self.assertEqual(keyword_value, 1)
                self.assertEqual(scheduled_value, 1)
        finally:
            if engine is not None:
                await engine.dispose()
            for suffix in ("", "-wal", "-shm"):
                try:
                    os.remove(path + suffix)
                except OSError:
                    pass


class ForeignKeyMigrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_vote_ban_terminal_and_pin_columns_upgrade_existing_rows(self) -> None:
        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        engine = None
        try:
            engine, session_factory = await init_db(
                f"sqlite+aiosqlite:///{path}"
            )
            async with session_factory() as session:
                session.add(
                    VoteBanSession(
                        group_id=-100,
                        target_user_id=7,
                        target_display="target",
                        target_username="",
                        starter_user_id=1,
                        starter_display="starter",
                        reason="",
                        evidence="",
                        source="command",
                        target_message_id=0,
                        threshold=3,
                        message_id=0,
                        status="active",
                        deadline_at=now_shanghai_naive() + timedelta(hours=1),
                    )
                )
                await session.commit()
            await engine.dispose()
            engine = None

            connection = sqlite3.connect(path)
            for column in (
                "resolution",
                "resolver_user_id",
                "resolver_display",
                "pin_message",
            ):
                connection.execute(
                    f"ALTER TABLE vote_ban_sessions DROP COLUMN {column}"
                )
            connection.commit()
            connection.close()

            engine, session_factory = await init_db(
                f"sqlite+aiosqlite:///{path}"
            )
            async with session_factory() as session:
                columns = {
                    row[1]
                    for row in (
                        await session.execute(
                            text("PRAGMA table_info(vote_ban_sessions)")
                        )
                    ).all()
                }
                values = (
                    await session.execute(
                        text(
                            "SELECT resolution, resolver_user_id, resolver_display, "
                            "pin_message "
                            "FROM vote_ban_sessions WHERE target_user_id=7"
                        )
                    )
                ).one()
            self.assertTrue(
                {"resolution", "resolver_user_id", "resolver_display", "pin_message"}
                <= columns
            )
            self.assertEqual(tuple(values), ("", 0, "", 0))
        finally:
            if engine is not None:
                await engine.dispose()
            for suffix in ("", "-wal", "-shm"):
                try:
                    os.remove(path + suffix)
                except OSError:
                    pass

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
                self.assertIn("priority", webhook_columns)
                self.assertIn("auth_candidate", webhook_columns)
                webhook_indexes = {
                    str(row[1])
                    for row in (
                        await session.execute(
                            text("PRAGMA index_list(webhook_inbox_updates)")
                        )
                    ).all()
                }
                self.assertIn("ix_webhook_inbox_recovery", webhook_indexes)
                self.assertIn(
                    "ix_webhook_inbox_completed_retention",
                    webhook_indexes,
                )
                self.assertIn(
                    "ix_webhook_inbox_dead_letter_retention",
                    webhook_indexes,
                )
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
                        "priority",
                        "auth_candidate",
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
