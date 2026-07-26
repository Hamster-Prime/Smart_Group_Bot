import asyncio
import shutil
import sqlite3
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch
from uuid import uuid4

from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError, OperationalError

from bot.config import BotConfig
from bot.db.engine import init_db
from bot.db.models import GroupContextSummary, GroupPermanentMemory, MessageVector
from bot.services.memory import MemoryService
from bot.services.update_completion import (
    UpdateCompletionReceipt,
    bind_update_completion,
    reset_update_completion,
)
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


class _ConcurrentSummaryStubLLM(_StubLLM):
    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.calls = 0
        self.active = 0
        self.max_active = 0

    async def compress(self, system: str, user_text: str) -> str:
        _ = system, user_text
        self.calls += 1
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        self.started.set()
        try:
            await self.release.wait()
            return "bounded summary"
        finally:
            self.active -= 1


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
        memory._history_loaded.add(12345)
        memory._replace_working_history(12345, [])

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

    async def test_successful_history_hard_cap_cleanup_logs_at_info(self) -> None:
        tmpdir = self._workspace_tmpdir()
        try:
            db_path = tmpdir / "bot.db"
            engine, session_factory = await init_db(self._sqlite_url(db_path))
            group_id = 12345
            async with session_factory() as session:
                session.add_all(
                    MessageVector(
                        group_id=group_id,
                        message_id=f"{group_id}:cap-{idx}",
                        role="user",
                        content=f"message {idx}",
                        vector_id=f"{group_id}:cap-{idx}",
                    )
                    for idx in range(3)
                )
                await session.commit()

            memory = MemoryService(
                BotConfig(max_context_tokens=4096, max_output_tokens=2048),
                _StubLLM(),
                session_factory=session_factory,
            )
            with (
                patch("bot.services.memory._HISTORY_MAX_MESSAGES_PER_GROUP", 2),
                self.assertLogs("bot.services.memory", level="INFO") as captured,
            ):
                await memory._ensure_history_loaded(group_id)
                prune_task = memory._history_prune_tasks[group_id]
                await asyncio.wait_for(prune_task, timeout=5)

            cleanup_log = next(
                entry for entry in captured.output if "history hard cap applied" in entry
            )
            self.assertTrue(cleanup_log.startswith("INFO:"), cleanup_log)
            async with session_factory() as session:
                rows = list(
                    (
                        await session.execute(
                            select(MessageVector).where(MessageVector.group_id == group_id)
                        )
                    ).scalars()
                )
            self.assertEqual(len(rows), 2)
            await engine.dispose()
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

    async def test_foreground_history_trims_without_waiting_for_compaction(self) -> None:
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

            self.assertEqual(len(memory.get_history(12345)), 3)
            self.assertEqual(await memory._get_summary(12345), "")
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

    async def test_compaction_failure_uses_backoff_before_retrying(self) -> None:
        memory = MemoryService(
            BotConfig(max_context_tokens=4096, max_output_tokens=512),
            _StubLLM(),
            session_factory=Mock(),
        )
        group_id = 12345
        memory._history_loaded.add(group_id)
        memory._replace_working_history(
            group_id,
            [
                memory._history_item(
                    role="user",
                    content="needs compaction",
                    created_at=datetime.now(timezone.utc),
                    sender_id=42,
                    sender_name="Alice",
                    message_type="text",
                    message_id="12345:one",
                )
            ],
        )
        memory._count_tokens = lambda _messages: memory.max_context  # type: ignore[method-assign]
        memory._format_system_memory_blocks = AsyncMock(return_value=[])  # type: ignore[method-assign]
        memory._compress_and_publish_locked = AsyncMock(return_value="llm_empty")  # type: ignore[method-assign]

        self.assertFalse(await memory.compact_if_needed(group_id))
        self.assertFalse(await memory.compact_if_needed(group_id))

        memory._compress_and_publish_locked.assert_awaited_once()
        self.assertGreater(memory._compaction_retry_at[group_id], 0.0)

    async def test_foreground_history_does_not_invoke_compression(self) -> None:
        llm = _BlockingSummaryStubLLM()
        memory = MemoryService(
            BotConfig(max_context_tokens=4096, max_output_tokens=512),
            llm,
            session_factory=Mock(),
        )
        group_id = 12345
        memory._history_loaded.add(group_id)
        memory._replace_working_history(
            group_id,
            [
                memory._history_item(
                    role="user",
                    content="reply immediately",
                    created_at=datetime.now(timezone.utc),
                    sender_id=42,
                    sender_name="Alice",
                    message_type="text",
                    message_id="12345:one",
                )
            ],
        )
        memory._format_system_memory_blocks = AsyncMock(return_value=[])  # type: ignore[method-assign]

        history = await asyncio.wait_for(
            memory.get_history_for_llm(group_id, reserve_tokens=0),
            timeout=0.2,
        )

        self.assertEqual([item["content"] for item in history], ["reply immediately"])
        self.assertFalse(llm.started.is_set())

    async def test_compaction_has_one_dedicated_concurrency_slot(self) -> None:
        llm = _ConcurrentSummaryStubLLM()
        memory = MemoryService(
            BotConfig(max_context_tokens=4096, max_output_tokens=512),
            llm,
            session_factory=Mock(),
        )
        memory._get_summary = AsyncMock(return_value="")  # type: ignore[method-assign]
        memory._save_summary_and_clear_history = AsyncMock()  # type: ignore[method-assign]
        for group_id in (1, 2):
            memory._history_loaded.add(group_id)
            memory._replace_working_history(
                group_id,
                [
                    memory._history_item(
                        role="user",
                        content=f"group {group_id}",
                        created_at=datetime.now(timezone.utc),
                        sender_id=42,
                        sender_name="Alice",
                        message_type="text",
                        message_id=f"{group_id}:one",
                    )
                ],
            )

        tasks = [asyncio.create_task(memory.compact_now(group_id)) for group_id in (1, 2)]
        try:
            await asyncio.wait_for(llm.started.wait(), timeout=0.5)
            await asyncio.sleep(0.02)
            self.assertEqual(llm.calls, 1)
            self.assertEqual(llm.max_active, 1)
            llm.release.set()
            results = await asyncio.wait_for(asyncio.gather(*tasks), timeout=0.5)
            self.assertEqual([result["status"] for result in results], ["ok", "ok"])
            self.assertEqual(llm.max_active, 1)
        finally:
            llm.release.set()
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    async def test_deferred_message_write_does_not_block_caller(self) -> None:
        memory = MemoryService(
            BotConfig(max_context_tokens=4096, max_output_tokens=512),
            _StubLLM(),
            session_factory=Mock(),
        )
        group_id = 12345
        memory._history_loaded.add(group_id)
        memory._replace_working_history(group_id, [])
        started = asyncio.Event()
        release = asyncio.Event()

        async def persist(batch) -> None:
            self.assertEqual(len(batch), 1)
            started.set()
            await release.wait()

        memory._persist_message_batch = AsyncMock(side_effect=persist)  # type: ignore[method-assign]

        await asyncio.wait_for(
            memory.add_message(
                group_id,
                "user",
                "queued without sqlite wait",
                message_id="deferred-one",
                defer_persistence=True,
            ),
            timeout=0.1,
        )
        await asyncio.wait_for(started.wait(), timeout=0.1)
        self.assertEqual(
            [item["content"] for item in memory.get_history(group_id)],
            ["queued without sqlite wait"],
        )
        self.assertFalse(memory._pending_write_idle.is_set())

        release.set()
        self.assertTrue(await memory.flush_pending_writes(timeout_seconds=0.2))
        await memory.shutdown(timeout_seconds=0.2)

    async def test_deferred_write_keeps_update_receipt_pending_until_persisted(self) -> None:
        memory = MemoryService(
            BotConfig(max_context_tokens=4096, max_output_tokens=512),
            _StubLLM(),
            session_factory=Mock(),
        )
        group_id = 12346
        memory._history_loaded.add(group_id)
        memory._replace_working_history(group_id, [])
        started = asyncio.Event()
        release = asyncio.Event()

        async def persist(_batch) -> None:
            started.set()
            await release.wait()

        memory._persist_message_batch = AsyncMock(side_effect=persist)  # type: ignore[method-assign]
        receipt = UpdateCompletionReceipt()
        token = bind_update_completion(receipt)
        try:
            await memory.add_message(
                group_id,
                "user",
                "durable deferred memory",
                message_id="deferred-receipt",
                defer_persistence=True,
            )
        finally:
            reset_update_completion(token)

        await asyncio.wait_for(started.wait(), timeout=0.1)
        waiter = asyncio.create_task(receipt.wait())
        await asyncio.sleep(0)
        self.assertFalse(waiter.done())
        release.set()
        self.assertTrue(await asyncio.wait_for(waiter, timeout=0.2))
        await memory.shutdown(timeout_seconds=0.2)

    async def test_proactive_compaction_triggers_before_full_budget(self) -> None:
        """Threshold compaction: 85% of the budget compacts without waiting for 100%."""
        memory = MemoryService(
            BotConfig(max_context_tokens=64000, max_output_tokens=512),
            _SummaryStubLLM(),
            session_factory=Mock(),
        )
        memory._get_summary = AsyncMock(return_value="")  # type: ignore[method-assign]
        memory._save_summary_and_clear_history = AsyncMock()  # type: ignore[method-assign]
        memory._format_system_memory_blocks = AsyncMock(return_value=[])  # type: ignore[method-assign]
        group_id = 12345
        memory._history_loaded.add(group_id)

        budget = memory._soft_budget_tokens(memory._llm_input_budget(None))
        # Chinese content sized between 85% and 100% of the reply budget: the
        # old ==100% trigger would skip it, threshold compaction must fire.
        chinese_chars = int(budget * 0.90)
        memory._replace_working_history(
            group_id,
            [
                memory._history_item(
                    role="user",
                    content="喵" * chinese_chars,
                    created_at=datetime.now(timezone.utc),
                    sender_id=42,
                    sender_name="Alice",
                    message_type="text",
                    message_id="12345:cn",
                )
            ],
        )

        self.assertTrue(await memory.compact_if_needed(group_id))
        memory._save_summary_and_clear_history.assert_awaited_once()

    async def test_rough_estimate_counts_cjk_near_one_token_per_char(self) -> None:
        memory = MemoryService(
            BotConfig(max_context_tokens=256000, max_output_tokens=512),
            _StubLLM(),
            session_factory=Mock(),
        )
        group_id = 12345
        memory._history_loaded.add(group_id)
        memory._replace_working_history(
            group_id,
            [
                memory._history_item(
                    role="user",
                    content="中文内容" * 300,
                    created_at=datetime.now(timezone.utc),
                    sender_id=42,
                    sender_name="Alice",
                    message_type="text",
                    message_id="12345:cjk",
                )
            ],
        )
        # 1200 CJK chars must estimate near 1200 tokens, not chars/3=400.
        self.assertGreaterEqual(memory._rough_history_tokens(group_id), 1200)

    async def test_message_count_threshold_compacts_but_keeps_recent_tail(self) -> None:
        """Hitting the count threshold summarizes old rows instead of dropping them."""
        memory = MemoryService(
            BotConfig(max_context_tokens=256000, max_output_tokens=512),
            _SummaryStubLLM(),
            session_factory=Mock(),
        )
        memory._get_summary = AsyncMock(return_value="")  # type: ignore[method-assign]
        memory._save_summary_and_clear_history = AsyncMock()  # type: ignore[method-assign]
        memory._format_system_memory_blocks = AsyncMock(return_value=[])  # type: ignore[method-assign]
        group_id = 12345
        memory._history_loaded.add(group_id)
        total_messages = 800
        memory._replace_working_history(
            group_id,
            [
                memory._history_item(
                    role="user",
                    content=f"message {idx}",
                    created_at=datetime.now(timezone.utc),
                    sender_id=42,
                    sender_name="Alice",
                    message_type="text",
                    message_id=f"12345:{idx}",
                )
                for idx in range(total_messages)
            ],
        )

        self.assertTrue(await memory.compact_if_needed(group_id))
        memory._save_summary_and_clear_history.assert_awaited_once()
        compacted_ids = memory._save_summary_and_clear_history.await_args.kwargs[
            "message_ids"
        ]
        # The compacted snapshot must be the OLDEST prefix: compressing the
        # newest slice instead would delete from the DB exactly the rows the
        # bot keeps in RAM.
        self.assertEqual(len(compacted_ids), total_messages - 50)
        self.assertEqual(compacted_ids[0], "12345:0")
        self.assertEqual(compacted_ids[-1], f"12345:{total_messages - 51}")
        remaining = memory.get_history(group_id)
        self.assertEqual(len(remaining), 50)
        self.assertEqual(remaining[-1]["content"], f"message {total_messages - 1}")
        self.assertEqual(remaining[0]["content"], f"message {total_messages - 50}")

    async def test_compaction_below_thresholds_stays_idle(self) -> None:
        memory = MemoryService(
            BotConfig(max_context_tokens=256000, max_output_tokens=512),
            _SummaryStubLLM(),
            session_factory=Mock(),
        )
        memory._save_summary_and_clear_history = AsyncMock()  # type: ignore[method-assign]
        memory._format_system_memory_blocks = AsyncMock(return_value=[])  # type: ignore[method-assign]
        group_id = 12345
        memory._history_loaded.add(group_id)
        memory._replace_working_history(
            group_id,
            [
                memory._history_item(
                    role="user",
                    content="普通的一条消息",
                    created_at=datetime.now(timezone.utc),
                    sender_id=42,
                    sender_name="Alice",
                    message_type="text",
                    message_id="12345:small",
                )
            ],
        )

        self.assertFalse(await memory.compact_if_needed(group_id))
        memory._save_summary_and_clear_history.assert_not_awaited()

    async def test_compaction_stays_idle_at_half_budget(self) -> None:
        """History far above the 512-token floor but below 85% must not compact."""
        memory = MemoryService(
            BotConfig(max_context_tokens=64000, max_output_tokens=512),
            _SummaryStubLLM(),
            session_factory=Mock(),
        )
        memory._save_summary_and_clear_history = AsyncMock()  # type: ignore[method-assign]
        memory._format_system_memory_blocks = AsyncMock(return_value=[])  # type: ignore[method-assign]
        group_id = 12345
        memory._history_loaded.add(group_id)

        budget = memory._soft_budget_tokens(memory._llm_input_budget(None))
        chinese_chars = int(budget * 0.50)
        memory._replace_working_history(
            group_id,
            [
                memory._history_item(
                    role="user",
                    content="喵" * chinese_chars,
                    created_at=datetime.now(timezone.utc),
                    sender_id=42,
                    sender_name="Alice",
                    message_type="text",
                    message_id="12345:half",
                )
            ],
        )

        self.assertFalse(await memory.compact_if_needed(group_id))
        memory._save_summary_and_clear_history.assert_not_awaited()

    async def test_summary_dominated_group_does_not_churn_compression(self) -> None:
        """A standing summary plus a small kept tail must not re-compact per message.

        Post-compaction steady state on a small budget: the summary alone keeps
        the trigger estimate saturated while the retained tail has almost no
        compressible content. Without the snapshot-content gate this fires one
        compress LLM call per group message forever.
        """
        memory = MemoryService(
            BotConfig(max_context_tokens=4096, max_output_tokens=512),
            _SummaryStubLLM(),
            session_factory=Mock(),
        )
        memory._save_summary_and_clear_history = AsyncMock()  # type: ignore[method-assign]
        memory._format_system_memory_blocks = AsyncMock(return_value=[])  # type: ignore[method-assign]
        group_id = 12345
        memory._history_loaded.add(group_id)
        memory._summary_cache[group_id] = "背景摘要" * 300
        memory._replace_working_history(
            group_id,
            [
                memory._history_item(
                    role="user",
                    content=f"hi {idx}",
                    created_at=datetime.now(timezone.utc),
                    sender_id=42,
                    sender_name="Alice",
                    message_type="text",
                    message_id=f"12345:tail-{idx}",
                )
                for idx in range(51)
            ],
        )

        self.assertFalse(await memory.compact_if_needed(group_id))
        memory._save_summary_and_clear_history.assert_not_awaited()

    async def test_shutdown_fails_receipt_for_blocked_memory_write(self) -> None:
        memory = MemoryService(
            BotConfig(max_context_tokens=4096, max_output_tokens=512),
            _StubLLM(),
            session_factory=Mock(),
        )
        group_id = 12347
        memory._history_loaded.add(group_id)
        memory._replace_working_history(group_id, [])
        started = asyncio.Event()

        async def persist(_batch) -> None:
            started.set()
            await asyncio.Event().wait()

        memory._persist_message_batch = AsyncMock(side_effect=persist)  # type: ignore[method-assign]
        receipt = UpdateCompletionReceipt()
        token = bind_update_completion(receipt)
        try:
            await memory.add_message(
                group_id,
                "user",
                "must replay",
                message_id="deferred-cancel",
                defer_persistence=True,
            )
        finally:
            reset_update_completion(token)
        await asyncio.wait_for(started.wait(), timeout=0.1)
        await memory.shutdown(timeout_seconds=0.01)
        self.assertFalse(await asyncio.wait_for(receipt.wait(), timeout=0.2))


if __name__ == "__main__":
    unittest.main()
