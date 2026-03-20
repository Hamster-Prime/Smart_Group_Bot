import sqlite3
import shutil
import tempfile
import unittest
from pathlib import Path

from bot.config import BotConfig
from bot.db.engine import init_db
from bot.services.memory import MemoryService


class _StubLLM:
    class main:
        model = "gemini/gemini-2.0-flash"

    async def compress(self, system: str, user_text: str) -> str:
        _ = system, user_text
        return ""


def _create_legacy_message_vectors_table(db_path: Path) -> None:
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    cur.execute(
        """
        CREATE TABLE message_vectors (
            id INTEGER NOT NULL PRIMARY KEY AUTOINCREMENT,
            group_id BIGINT NOT NULL,
            message_id VARCHAR(64) NOT NULL,
            role VARCHAR(16) NOT NULL,
            content TEXT NOT NULL,
            importance_score FLOAT NOT NULL,
            access_count INTEGER NOT NULL,
            vector_id VARCHAR(64) NOT NULL,
            embedding BLOB,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL,
            last_accessed DATETIME
        )
        """
    )
    conn.commit()
    conn.close()


class MemoryServiceCompatibilityTests(unittest.IsolatedAsyncioTestCase):
    def _workspace_tmpdir(self) -> Path:
        return Path(tempfile.mkdtemp(prefix="memory-tests-", dir=".")).resolve()

    async def test_add_message_writes_into_legacy_message_vectors_schema(self) -> None:
        tmpdir = self._workspace_tmpdir()
        try:
            db_path = tmpdir / "bot.db"
            _create_legacy_message_vectors_table(db_path)

            engine, session_factory = await init_db(f"sqlite+aiosqlite:///{db_path.as_posix()}")
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
            )

            await engine.dispose()

            conn = sqlite3.connect(db_path)
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT group_id, message_id, role, content, importance_score, access_count, vector_id, sender_id, sender_name, message_type "
                "FROM message_vectors"
            ).fetchone()
            conn.close()

            self.assertIsNotNone(row)
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
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

    async def test_history_for_llm_includes_structured_memory_rules(self) -> None:
        tmpdir = self._workspace_tmpdir()
        try:
            db_path = tmpdir / "bot.db"
            engine, session_factory = await init_db(f"sqlite+aiosqlite:///{db_path.as_posix()}")
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
            permanent_block = next(text for text in system_texts if "[permanent-memory]" in text)
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
