from __future__ import annotations

import unittest
import asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, Mock

from bot.config import BotConfig
from bot.services.memory import (
    MemoryService,
    _archive_fts_match_query,
    _recall_terms,
)
from bot.utils.timezone import now_shanghai_naive


class _LLM:
    class main:
        model = "test/model"


class MemoryArchiveUnitTests(unittest.IsolatedAsyncioTestCase):
    def _memory(self, **overrides: object) -> MemoryService:
        config = BotConfig(**overrides)
        return MemoryService(
            config,
            _LLM(),  # type: ignore[arg-type]
            session_factory=object(),  # type: ignore[arg-type]
        )

    def test_default_policy_is_500_messages_and_seven_days(self) -> None:
        memory = self._memory()

        self.assertEqual(memory._history_limit(), 500)
        self.assertEqual(memory.memory_retention_days, 7)
        self.assertEqual(memory.memory_archive_max_messages_per_group, 50000)
        self.assertTrue(memory.memory_recall_enabled)
        self.assertFalse(memory.automatic_compaction_enabled)

    def test_runtime_policy_can_override_recent_window(self) -> None:
        memory = self._memory(
            memory_recent_messages=750,
            memory_retention_days=14,
            memory_archive_max_messages_per_group=80000,
            memory_automatic_compaction=True,
        )

        self.assertEqual(memory._history_limit(), 750)
        self.assertEqual(memory.memory_retention_days, 14)
        self.assertEqual(memory.memory_archive_max_messages_per_group, 80000)
        self.assertTrue(memory.automatic_compaction_enabled)

    def test_chinese_recall_terms_include_specific_ngrams(self) -> None:
        terms = _recall_terms("还记得之前讨论的 Docker 部署方案吗？")

        self.assertIn("docker", terms)
        self.assertIn("部署方案", terms)
        self.assertNotIn("之前", terms)

    def test_fts_query_is_quoted_and_group_scoped(self) -> None:
        query = _archive_fts_match_query(
            -10001,
            'Docker "蓝绿发布"',
            _recall_terms('Docker "蓝绿发布"'),
        )

        self.assertIn('group_scope : "group_n_10001"', query)
        self.assertIn('{content raw_text derived_text', query)
        self.assertIn('""蓝绿发布""', query)

    def test_rrf_rewards_candidates_found_by_both_indexes(self) -> None:
        keys, reasons = MemoryService._fuse_archive_rankings(
            ["g:lexical", "g:both"],
            ["g:both", "g:semantic"],
            limit=3,
        )

        self.assertEqual(keys[0], "g:both")
        self.assertEqual(reasons["g:both"], "bm25+vector")
        self.assertEqual(reasons["g:semantic"], "vector")

    def test_archive_values_preserve_sender_and_reply_topology(self) -> None:
        memory = self._memory()
        sent_at = datetime(2026, 7, 30, 9, 30, tzinfo=timezone.utc)

        values = memory._archive_values(
            group_id=-10001,
            scoped_message_id="-10001:42",
            role="user",
            content="就按这个做",
            created_at=sent_at.replace(tzinfo=None),
            message_id="42",
            sender_id=8,
            sender_username="bob",
            sender_first_name="Bob",
            sender_last_name="Li",
            sender_display_name="Bob Li",
            sender_is_bot=False,
            sender_language_code="zh-hans",
            message_type="text",
            raw_text="就按这个做",
            is_reply=True,
            reply_to_message_id=41,
            reply_to_sender_id=7,
            reply_to_sender_name="Alice",
            reply_to_content="之前的部署方案",
            message_thread_id=77,
        )

        self.assertEqual(values["telegram_message_id"], 42)
        self.assertEqual(values["sender_username"], "bob")
        self.assertEqual(values["sender_display_name"], "Bob Li")
        self.assertTrue(values["is_reply"])
        self.assertEqual(values["reply_to_message_id"], 41)
        self.assertEqual(values["reply_to_sender_id"], 7)
        self.assertEqual(values["message_thread_id"], 77)

    async def test_recall_index_discloses_cards_before_full_records(self) -> None:
        memory = self._memory(memory_recall_max_results=4)
        memory.recall_archive = AsyncMock(
            return_value=[
                {
                    "message_key": "-10001:7",
                    "sent_at": "2026-07-29 20:00:00",
                    "sender_display_name": "Alice",
                    "sender_id": 7,
                    "message_type": "text",
                    "reply_to_message_id": None,
                    "raw_text": "部署统一走 Docker Compose。",
                }
            ]
        )  # type: ignore[method-assign]

        index = await memory._build_recall_index_message(-10001, "部署方案")

        self.assertIsNotNone(index)
        content = str(index["content"])
        self.assertIn("[RECALLED_MEMORY_INDEX]", content)
        self.assertIn("message_key=-10001:7", content)
        self.assertIn("conversation_recall", content)
        self.assertIn("snippet=部署统一走 Docker Compose。", content)

    async def test_hot_window_stays_bounded_while_prune_task_is_running(self) -> None:
        memory = self._memory()
        group_id = -10001
        memory._history_loaded.add(group_id)
        memory._replace_working_history(
            group_id,
            [
                memory._history_item(
                    role="user",
                    content=f"message-{index}",
                    created_at="2026-07-30 09:00:00",
                    sender_id=index,
                    sender_name="Alice",
                    message_type="text",
                    message_id=f"{group_id}:{index}",
                )
                for index in range(500)
            ],
        )
        blocker = asyncio.create_task(asyncio.Event().wait())
        memory._history_prune_tasks[group_id] = blocker
        try:
            for index in range(500, 525):
                memory._append_working_history(
                    group_id,
                    memory._history_item(
                        role="user",
                        content=f"message-{index}",
                        created_at="2026-07-30 09:00:00",
                        sender_id=index,
                        sender_name="Alice",
                        message_type="text",
                        message_id=f"{group_id}:{index}",
                    ),
                )
                memory._schedule_history_prune_if_needed(group_id)

            self.assertEqual(len(memory.get_history(group_id)), 500)
            self.assertEqual(
                memory.get_history(group_id)[0]["content"],
                "message-25",
            )
            self.assertEqual(len(memory._history_prune_pending_ids[group_id]), 25)
        finally:
            memory._history_prune_tasks.pop(group_id, None)
            blocker.cancel()
            await asyncio.gather(blocker, return_exceptions=True)

    async def test_reconfigure_immediately_trims_loaded_window_and_ttl(self) -> None:
        memory = self._memory(memory_recent_messages=100, memory_retention_days=7)
        group_id = -10001
        now = now_shanghai_naive()
        memory._history_loaded.add(group_id)
        memory._replace_working_history(
            group_id,
            [
                memory._history_item(
                    role="user",
                    content=f"old-{index}",
                    created_at=now - timedelta(days=2),
                    sender_id=index,
                    sender_name="Alice",
                    message_type="text",
                    message_id=f"{group_id}:old:{index}",
                )
                for index in range(20)
            ]
            + [
                memory._history_item(
                    role="user",
                    content=f"recent-{index}",
                    created_at=now - timedelta(minutes=60 - index),
                    sender_id=index,
                    sender_name="Alice",
                    message_type="text",
                    message_id=f"{group_id}:recent:{index}",
                )
                for index in range(60)
            ],
        )
        memory._schedule_history_prune_if_needed = Mock()  # type: ignore[method-assign]
        memory._schedule_archive_prune_if_needed = Mock()  # type: ignore[method-assign]

        memory.reconfigure(
            BotConfig(memory_recent_messages=50, memory_retention_days=1)
        )

        history = memory.get_history(group_id)
        self.assertEqual(len(history), 50)
        self.assertEqual(history[0]["content"], "recent-10")
        self.assertTrue(all(not item["content"].startswith("old-") for item in history))
        memory._schedule_history_prune_if_needed.assert_called_once_with(group_id)
        memory._schedule_archive_prune_if_needed.assert_called_once_with(
            group_id,
            force=True,
        )

    async def test_recall_index_survives_hot_history_token_trimming(self) -> None:
        memory = self._memory(max_context_tokens=4096, max_output_tokens=2048)
        group_id = -10001
        memory._history_loaded.add(group_id)
        memory._replace_working_history(
            group_id,
            [
                memory._history_item(
                    role="user",
                    content=(f"history-{index} " + "内容" * 120),
                    created_at="2026-07-30 09:00:00",
                    sender_id=index,
                    sender_name="Alice",
                    message_type="text",
                    message_id=f"{group_id}:{index}",
                )
                for index in range(500)
            ],
        )
        memory._format_system_memory_blocks = AsyncMock(return_value=[])  # type: ignore[method-assign]
        memory._build_recall_index_message = AsyncMock(  # type: ignore[method-assign]
            return_value={
                "role": "user",
                "content": "[RECALLED_MEMORY_INDEX]\nmessage_key=-10001:old",
                "memory_source": "recalled_archive_index",
            }
        )

        history = await memory.get_history_for_llm(
            group_id,
            recall_query="old topic",
        )

        self.assertTrue(
            any(
                item.get("memory_source") == "recalled_archive_index"
                for item in history
            )
        )


if __name__ == "__main__":
    unittest.main()
