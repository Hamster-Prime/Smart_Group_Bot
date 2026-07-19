import asyncio
import shutil
import sqlite3
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, patch
from uuid import uuid4

from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError, OperationalError

from bot.config import BotConfig
from bot.db.engine import init_db
from bot.db.models import GroupContextSummary, GroupPermanentMemory, MessageVector
from bot.services.memory import MemoryService
from bot.utils.security import sanitize_history_for_llm


class _StubLLM:
    class main:
        model = "gemini/gemini-2.0-flash"

    async def compress(self, system: str, user_text: str) -> str:
        _ = system, user_text
        return ""


class _SummaryStubLLM(_StubLLM):
    async def compress(self, system: str, user_text: str) -> str:
        _ = system, user_text
        return "压缩后摘要"


class _BlockingSummaryStubLLM(_StubLLM):
    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.resume = asyncio.Event()
        self.payload = ""

    async def compress(self, system: str, user_text: str) -> str:
        _ = system
        self.payload = user_text
        self.started.set()
        await self.resume.wait()
        return "压缩期间之前的摘要"


def _create_legacy_message_vectors_table(db_path: Path, *, include_embedding: bool = False) -> None:
    embedding_column = "embedding BLOB,\n" if include_embedding else ""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    cur = conn.cursor()
    cur.execute(
        f"""
        CREATE TABLE message_vectors (
            id INTEGER NOT NULL PRIMARY KEY AUTOINCREMENT,
            group_id BIGINT NOT NULL,
            message_id VARCHAR(64) NOT NULL,
            role VARCHAR(16) NOT NULL,
            content TEXT NOT NULL,
            importance_score FLOAT NOT NULL,
            access_count INTEGER NOT NULL,
            vector_id VARCHAR(64) NOT NULL,
            {embedding_column}
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL,
            last_accessed DATETIME
        )
        """
    )
    conn.commit()
    conn.close()


class MemoryServiceCompatibilityTests(unittest.IsolatedAsyncioTestCase):
    def _workspace_tmpdir(self) -> Path:
        root = (Path.cwd() / "data" / "_test_tmp").resolve()
        root.mkdir(parents=True, exist_ok=True)
        path = (root / f"memory-tests-{uuid4().hex}").resolve()
        path.mkdir(parents=True, exist_ok=True)
        return path

    @staticmethod
    def _sqlite_url(db_path: Path) -> str:
        return f"sqlite+aiosqlite:///{db_path.resolve().as_posix()}"

    async def test_add_message_writes_into_legacy_message_vectors_schema(self) -> None:
        tmpdir = self._workspace_tmpdir()
        try:
            db_path = tmpdir / "bot.db"
            _create_legacy_message_vectors_table(db_path)

            engine, session_factory = await init_db(self._sqlite_url(db_path))
            memory = MemoryService(
                BotConfig(max_context_tokens=4096, max_output_tokens=2048),
                _StubLLM(),
                session_factory=session_factory,
            )

            await memory.add_message(
                12345,
                "user",
                "[id:42 username:@tester is_owner:no is_tg_admin:no trusted_source:none name:Alice] hello",
                user_id=42,
                sender_name="Alice",
                message_type="text",
                message_id="msg-1",
                created_at=datetime(2026, 3, 20, 15, 30, tzinfo=timezone.utc),
            )

            await engine.dispose()

            conn = sqlite3.connect(str(db_path))
            conn.row_factory = sqlite3.Row
            columns = {
                row["name"]
                for row in conn.execute("PRAGMA table_info(message_vectors)").fetchall()
            }
            row = conn.execute(
                "SELECT group_id, message_id, role, content, importance_score, access_count, vector_id, sender_id, sender_name, message_type, created_at "
                "FROM message_vectors"
            ).fetchone()
            conn.close()

            self.assertIsNotNone(row)
            self.assertIn("embedding", columns)
            self.assertEqual(row["group_id"], 12345)
            self.assertEqual(row["message_id"], "12345:msg-1")
            self.assertEqual(row["role"], "user")
            self.assertEqual(row["content"], "[id:42 username:@tester is_owner:no is_tg_admin:no trusted_source:none name:Alice] hello")
            self.assertEqual(row["importance_score"], 0.0)
            self.assertEqual(row["access_count"], 0)
            self.assertEqual(row["vector_id"], "12345:msg-1")
            self.assertEqual(row["sender_id"], 42)
            self.assertEqual(row["sender_name"], "Alice")
            self.assertEqual(row["message_type"], "text")
            self.assertEqual(row["created_at"], "2026-03-20 23:30:00.000000")
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

    async def test_context_budget_is_capped_by_known_model_input_limit(self) -> None:
        llm = _StubLLM()
        llm.model_input_token_limit = lambda _cfg: 8192
        memory = MemoryService(
            BotConfig(max_context_tokens=256000, max_output_tokens=2048),
            llm,
            session_factory=object(),  # type: ignore[arg-type]
        )

        self.assertEqual(memory.max_context, 10240)

    async def test_sqlite_lock_exhaustion_is_not_recorded_as_memory_success(self) -> None:
        class _LockedSession:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                return False

            def add(self, _row) -> None:
                return None

            async def commit(self) -> None:
                raise OperationalError(
                    "INSERT",
                    {},
                    sqlite3.OperationalError("database is locked"),
                )

            async def rollback(self) -> None:
                return None

        memory = MemoryService(
            BotConfig(max_context_tokens=4096, max_output_tokens=2048),
            _StubLLM(),
            session_factory=lambda: _LockedSession(),  # type: ignore[arg-type]
        )

        with (
            patch("bot.services.memory.asyncio.sleep", new=AsyncMock()),
            self.assertRaises(OperationalError),
        ):
            await memory.add_message(12345, "user", "must-not-be-ram-only")

        self.assertEqual(memory.get_history(12345), [])

    async def test_bootstrap_converts_legacy_utc_message_timestamps_to_shanghai(self) -> None:
        tmpdir = self._workspace_tmpdir()
        try:
            db_path = tmpdir / "bot.db"
            _create_legacy_message_vectors_table(db_path)

            conn = sqlite3.connect(str(db_path))
            conn.execute(
                """
                INSERT INTO message_vectors (
                    group_id, message_id, role, content, importance_score, access_count, vector_id, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    12345,
                    "12345:legacy-msg",
                    "user",
                    "[id:42 username:@tester is_owner:no is_tg_admin:no trusted_source:none name:Alice] hello",
                    0.0,
                    0,
                    "12345:legacy-msg",
                    "2026-03-20 15:30:00",
                ),
            )
            conn.commit()
            conn.close()

            engine, session_factory = await init_db(self._sqlite_url(db_path))
            memory = MemoryService(
                BotConfig(max_context_tokens=4096, max_output_tokens=2048),
                _StubLLM(),
                session_factory=session_factory,
            )
            await memory.bootstrap()
            history = memory.get_history(12345)
            await engine.dispose()

            self.assertEqual(len(history), 1)
            self.assertEqual(history[0]["created_at"], "2026-03-20 23:30:00")

            conn = sqlite3.connect(str(db_path))
            migrated = conn.execute(
                "SELECT created_at FROM message_vectors WHERE message_id = ?",
                ("12345:legacy-msg",),
            ).fetchone()
            conn.close()

            self.assertIsNotNone(migrated)
            self.assertEqual(migrated[0], "2026-03-20 23:30:00")
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

    async def test_history_for_llm_includes_structured_memory_rules(self) -> None:
        tmpdir = self._workspace_tmpdir()
        try:
            db_path = tmpdir / "bot.db"
            engine, session_factory = await init_db(self._sqlite_url(db_path))
            memory = MemoryService(
                BotConfig(max_context_tokens=4096, max_output_tokens=2048),
                _StubLLM(),
                session_factory=session_factory,
            )

            await memory.add_permanent_memory(12345, "Alice is the project lead", created_by=42)
            await memory.add_message(
                12345,
                "user",
                "[id:42 username:@tester is_owner:no is_tg_admin:yes trusted_source:tg_admin name:Alice] ship it today",
                user_id=42,
                sender_name="Alice",
                message_type="text",
                message_id="msg-2",
            )
            history = await memory.get_history_for_llm(12345, reserve_tokens=0)
            await engine.dispose()

            system_texts = [msg["content"] for msg in history if msg.get("role") == "system"]
            user_msgs = [msg for msg in history if msg.get("role") == "user"]

            self.assertTrue(any("[MEMORY_SOURCE_RULES]" in text for text in system_texts))
            permanent_block = next(
                text for text in system_texts if text.startswith("[permanent-memory]")
            )
            self.assertIn("priority: high", permanent_block)
            self.assertIn("memory_id: 1", permanent_block)
            self.assertEqual(len(user_msgs), 1)
            self.assertEqual(user_msgs[0]["sender_id"], 42)
            self.assertEqual(user_msgs[0]["sender_name"], "Alice")
            self.assertEqual(user_msgs[0]["message_type"], "text")
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

    async def test_permanent_memory_replace_rolls_back_delete_when_insert_fails(self) -> None:
        tmpdir = self._workspace_tmpdir()
        try:
            db_path = tmpdir / "bot.db"
            engine, session_factory = await init_db(self._sqlite_url(db_path))
            memory = MemoryService(
                BotConfig(max_context_tokens=4096, max_output_tokens=512),
                _StubLLM(),
                session_factory=session_factory,
            )
            original, created = await memory.add_permanent_memory(
                12345,
                "must survive",
                created_by=42,
            )
            self.assertTrue(created)
            self.assertIsNotNone(original)
            async with session_factory() as session:
                await session.execute(
                    text(
                        "CREATE TRIGGER reject_replacement BEFORE INSERT "
                        "ON group_permanent_memories "
                        "WHEN NEW.content = 'reject me' "
                        "BEGIN SELECT RAISE(ABORT, 'replacement rejected'); END"
                    )
                )
                await session.commit()

            with self.assertRaises(IntegrityError):
                await memory.replace_permanent_memory(
                    12345,
                    target=f"#{original.id}",
                    new_content="reject me",
                    created_by=43,
                )

            async with session_factory() as session:
                rows = list(
                    (
                        await session.execute(
                            select(GroupPermanentMemory).where(
                                GroupPermanentMemory.group_id == 12345
                            )
                        )
                    ).scalars()
                )
            self.assertEqual([(row.id, row.content) for row in rows], [(original.id, "must survive")])
            await engine.dispose()
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

    async def test_history_compacts_when_final_prompt_is_near_limit(self) -> None:
        tmpdir = self._workspace_tmpdir()
        try:
            db_path = tmpdir / "bot.db"
            engine, session_factory = await init_db(self._sqlite_url(db_path))
            memory = MemoryService(
                BotConfig(max_context_tokens=5000, max_output_tokens=512),
                _SummaryStubLLM(),
                session_factory=session_factory,
            )

            for idx in range(3):
                await memory.add_message(
                    12345,
                    "user",
                    f"[id:42 username:@tester is_owner:no is_tg_admin:no trusted_source:none name:Alice] hi {idx}",
                    user_id=42,
                    sender_name="Alice",
                    message_type="text",
                    message_id=f"msg-{idx}",
                )

            raw_history = await memory.get_history_for_llm(12345, reserve_tokens=0)
            self.assertTrue(any(msg.get("role") == "user" for msg in raw_history))

            prompt_history = await memory.get_history_for_llm(
                12345,
                reserve_tokens=0,
                prompt_payload_builder=lambda history: {
                    "messages": [
                        {"role": "system", "content": "guard " * 5000},
                        *sanitize_history_for_llm(history, max_items=len(history)),
                        {"role": "user", "content": "ping"},
                    ]
                },
            )
            await engine.dispose()

            self.assertEqual(memory.get_history(12345), [])
            self.assertEqual(await memory._get_summary(12345), "压缩后摘要")
            self.assertFalse(any(msg.get("role") == "user" for msg in prompt_history))
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

    async def test_failed_compaction_preserves_raw_history_and_database_rows(self) -> None:
        tmpdir = self._workspace_tmpdir()
        try:
            db_path = tmpdir / "bot.db"
            engine, session_factory = await init_db(self._sqlite_url(db_path))
            memory = MemoryService(
                BotConfig(max_context_tokens=4096, max_output_tokens=512),
                _StubLLM(),
                session_factory=session_factory,
            )
            memory._count_tokens = lambda _messages: memory.max_context  # type: ignore[method-assign]
            group_id = 12345

            await memory.add_message(
                group_id,
                "user",
                "must-survive-failed-compression",
                message_id="preserved-message",
            )

            self.assertFalse(await memory.compact_if_needed(group_id))
            self.assertEqual(
                [item["content"] for item in memory.get_history(group_id)],
                ["must-survive-failed-compression"],
            )
            self.assertEqual(await memory._get_summary(group_id), "")

            async with session_factory() as session:
                rows = list(
                    (
                        await session.execute(
                            select(MessageVector).where(MessageVector.group_id == group_id)
                        )
                    )
                    .scalars()
                    .all()
                )
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0].content, "must-survive-failed-compression")
            await engine.dispose()
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

    async def test_compaction_summary_and_source_delete_roll_back_together(self) -> None:
        tmpdir = self._workspace_tmpdir()
        try:
            db_path = tmpdir / "bot.db"
            engine, session_factory = await init_db(self._sqlite_url(db_path))
            memory = MemoryService(
                BotConfig(max_context_tokens=4096, max_output_tokens=512),
                _SummaryStubLLM(),
                session_factory=session_factory,
            )
            memory._count_tokens = lambda _messages: memory.max_context  # type: ignore[method-assign]
            group_id = 12345
            await memory.add_message(
                group_id,
                "user",
                "atomic-compaction-source",
                message_id="atomic-source",
            )
            async with session_factory() as session:
                await session.execute(
                    text(
                        "CREATE TRIGGER reject_history_delete BEFORE DELETE "
                        "ON message_vectors "
                        "BEGIN SELECT RAISE(ABORT, 'delete rejected'); END"
                    )
                )
                await session.commit()

            with self.assertRaises(IntegrityError):
                await memory.compact_if_needed(group_id)

            async with session_factory() as session:
                summary = await session.get(GroupContextSummary, group_id)
                rows = list(
                    (
                        await session.execute(
                            select(MessageVector).where(
                                MessageVector.group_id == group_id
                            )
                        )
                    ).scalars()
                )
            self.assertIsNone(summary)
            self.assertEqual([row.content for row in rows], ["atomic-compaction-source"])
            self.assertEqual(
                [item["content"] for item in memory.get_history(group_id)],
                ["atomic-compaction-source"],
            )
            await engine.dispose()
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

    async def test_compaction_preserves_messages_added_while_llm_is_in_flight(self) -> None:
        tmpdir = self._workspace_tmpdir()
        engine = None
        compact_task = None
        llm = _BlockingSummaryStubLLM()
        try:
            db_path = tmpdir / "bot.db"
            engine, session_factory = await init_db(self._sqlite_url(db_path))
            memory = MemoryService(
                BotConfig(max_context_tokens=4096, max_output_tokens=512),
                llm,
                session_factory=session_factory,
            )
            memory._count_tokens = lambda _messages: memory.max_context  # type: ignore[method-assign]
            group_id = 12345

            await memory.add_message(
                group_id,
                "user",
                "snapshot-before-compression",
                message_id="before-compression",
            )
            compact_task = asyncio.create_task(memory.compact_if_needed(group_id))
            await asyncio.wait_for(llm.started.wait(), timeout=5)

            await memory.add_message(
                group_id,
                "user",
                "arrived-during-compression",
                message_id="during-compression",
            )
            llm.resume.set()
            self.assertTrue(await asyncio.wait_for(compact_task, timeout=5))

            self.assertIn("snapshot-before-compression", llm.payload)
            self.assertNotIn("arrived-during-compression", llm.payload)
            current_history = memory.get_history(group_id)
            self.assertEqual(
                [item["content"] for item in current_history],
                ["arrived-during-compression"],
            )

            async with session_factory() as session:
                rows = list(
                    (
                        await session.execute(
                            select(MessageVector)
                            .where(MessageVector.group_id == group_id)
                            .order_by(MessageVector.id.asc())
                        )
                    )
                    .scalars()
                    .all()
                )
            self.assertEqual(
                [(row.message_id, row.content) for row in rows],
                [(f"{group_id}:during-compression", "arrived-during-compression")],
            )

            reloaded = MemoryService(
                BotConfig(max_context_tokens=4096, max_output_tokens=512),
                _StubLLM(),
                session_factory=session_factory,
            )
            await reloaded.bootstrap()
            self.assertEqual(
                [item["content"] for item in reloaded.get_history(group_id)],
                ["arrived-during-compression"],
            )
        finally:
            llm.resume.set()
            if compact_task is not None and not compact_task.done():
                compact_task.cancel()
                await asyncio.gather(compact_task, return_exceptions=True)
            if engine is not None:
                await engine.dispose()
            shutil.rmtree(tmpdir, ignore_errors=True)

    async def test_compact_now_forces_compaction_below_token_budget(self) -> None:
        tmpdir = self._workspace_tmpdir()
        try:
            db_path = tmpdir / "bot.db"
            engine, session_factory = await init_db(self._sqlite_url(db_path))
            memory = MemoryService(
                BotConfig(max_context_tokens=200000, max_output_tokens=512),
                _SummaryStubLLM(),
                session_factory=session_factory,
            )
            group_id = 12345
            other_group_id = 67890

            for idx in range(3):
                await memory.add_message(
                    group_id,
                    "user",
                    f"manual-compact-source-{idx}",
                    message_id=f"manual-{idx}",
                )
            await memory.add_message(
                other_group_id,
                "user",
                "other-group-history",
                message_id="other-1",
            )

            # Way below the automatic budget, so only compact_now can trigger this.
            self.assertFalse(await memory.compact_if_needed(group_id))

            result = await memory.compact_now(group_id)
            self.assertEqual(result["status"], "ok")
            self.assertEqual(result["compacted_messages"], 3)
            self.assertEqual(memory.get_history(group_id), [])
            self.assertEqual(await memory._get_summary(group_id), "压缩后摘要")

            # Other groups are untouched.
            self.assertEqual(
                [item["content"] for item in memory.get_history(other_group_id)],
                ["other-group-history"],
            )

            async with session_factory() as session:
                rows = list(
                    (
                        await session.execute(
                            select(MessageVector).order_by(MessageVector.id.asc())
                        )
                    ).scalars()
                )
            self.assertEqual(
                [(row.group_id, row.content) for row in rows],
                [(other_group_id, "other-group-history")],
            )
            await engine.dispose()
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

    async def test_compact_now_reports_empty_history(self) -> None:
        tmpdir = self._workspace_tmpdir()
        try:
            db_path = tmpdir / "bot.db"
            engine, session_factory = await init_db(self._sqlite_url(db_path))
            memory = MemoryService(
                BotConfig(max_context_tokens=4096, max_output_tokens=512),
                _SummaryStubLLM(),
                session_factory=session_factory,
            )

            result = await memory.compact_now(12345)
            self.assertEqual(result, {"status": "empty", "compacted_messages": 0})
            await engine.dispose()
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

    async def test_compact_now_preserves_history_when_llm_returns_empty(self) -> None:
        tmpdir = self._workspace_tmpdir()
        try:
            db_path = tmpdir / "bot.db"
            engine, session_factory = await init_db(self._sqlite_url(db_path))
            memory = MemoryService(
                BotConfig(max_context_tokens=4096, max_output_tokens=512),
                _StubLLM(),
                session_factory=session_factory,
            )
            group_id = 12345
            await memory.add_message(
                group_id,
                "user",
                "must-survive-manual-compact",
                message_id="manual-preserved",
            )

            result = await memory.compact_now(group_id)
            self.assertEqual(result, {"status": "llm_empty", "compacted_messages": 0})
            self.assertEqual(
                [item["content"] for item in memory.get_history(group_id)],
                ["must-survive-manual-compact"],
            )
            self.assertEqual(await memory._get_summary(group_id), "")

            async with session_factory() as session:
                rows = list(
                    (
                        await session.execute(
                            select(MessageVector).where(MessageVector.group_id == group_id)
                        )
                    ).scalars()
                )
            self.assertEqual([row.content for row in rows], ["must-survive-manual-compact"])
            await engine.dispose()
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
