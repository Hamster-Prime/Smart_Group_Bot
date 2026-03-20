import sqlite3
import shutil
import unittest
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from bot.config import BotConfig
from bot.db.engine import init_db
from bot.services.memory import MemoryService


class _StubLLM:
    class main:
        model = "gemini/gemini-2.0-flash"

    async def compress(self, system: str, user_text: str) -> str:
        _ = system, user_text
        return ""


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


if __name__ == "__main__":
    unittest.main()
