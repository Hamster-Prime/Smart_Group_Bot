from __future__ import annotations

import os
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker

from bot.config import BotConfig
from bot.db.engine import init_db
from bot.db.models import Group, GroupMessageArchive, MessageVector
from bot.services.memory import ArchiveVectorCandidate, MemoryService
from bot.utils.timezone import now_shanghai_naive


class _LLM:
    class main:
        model = "test/model"


class _VectorRecallProvider:
    def __init__(self, candidates: list[ArchiveVectorCandidate]) -> None:
        self.candidates = candidates
        self.calls: list[dict[str, object]] = []

    async def recall(self, **kwargs: object) -> list[ArchiveVectorCandidate]:
        self.calls.append(dict(kwargs))
        return list(self.candidates)


class MemoryArchiveIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        fd, self._db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.engine, self.session_factory = await init_db(
            f"sqlite+aiosqlite:///{self._db_path}"
        )
        self.memory = MemoryService(
            BotConfig(),
            _LLM(),  # type: ignore[arg-type]
            session_factory=self.session_factory,
        )

    async def asyncTearDown(self) -> None:
        await self.memory.shutdown(timeout_seconds=1.0)
        await self.engine.dispose()
        for suffix in ("", "-wal", "-shm"):
            try:
                os.remove(self._db_path + suffix)
            except OSError:
                pass

    async def _archive_range(self, group_id: int, count: int) -> None:
        # Keep this test focused on archive/recall SQL rather than launching a
        # retention pass after every synthetic insert.
        self.memory._archive_last_pruned_at[group_id] = time.monotonic()
        base = datetime.now(timezone.utc) - timedelta(minutes=count)
        for message_id in range(1, count + 1):
            await self.memory.archive_message(
                group_id,
                "user",
                f"ordinary message {message_id}",
                message_id=str(message_id),
                telegram_message_id=message_id,
                created_at=base + timedelta(minutes=message_id),
                sender_id=message_id,
                sender_display_name=f"sender-{message_id}",
                message_type="text",
                raw_text=f"ordinary message {message_id}",
            )

    async def test_explicit_anchor_survives_neighbors_and_groups_are_isolated(
        self,
    ) -> None:
        await self._archive_range(-1001, 5)
        await self._archive_range(-2002, 5)
        await self.memory.archive_message(
            -1001,
            "user",
            "alpha-only deployment decision",
            message_id="99",
            telegram_message_id=99,
            sender_id=42,
            sender_display_name="Alice",
            raw_text="alpha-only deployment decision",
        )

        rows = await self.memory.recall_archive(
            -1001,
            message_keys=["-1001:3"],
            before_after=2,
            limit=1,
        )
        other_group = await self.memory.recall_archive(
            -2002,
            query="alpha-only deployment",
            before_after=0,
            limit=8,
        )

        self.assertEqual([row["message_key"] for row in rows], ["-1001:3"])
        self.assertTrue(rows[0]["is_anchor"])
        self.assertEqual(other_group, [])

    async def test_query_can_recall_hot_row_outside_recent_exclusion_tail(self) -> None:
        group_id = -3003
        await self._archive_range(group_id, 30)
        await self.memory.archive_message(
            group_id,
            "user",
            "needle-five deployment detail",
            message_id="5",
            telegram_message_id=5,
            sender_id=5,
            sender_display_name="sender-5",
            raw_text="needle-five deployment detail",
        )
        self.memory._history_loaded.add(group_id)
        self.memory._replace_working_history(
            group_id,
            [
                self.memory._history_item(
                    role="user",
                    content=f"ordinary message {message_id}",
                    created_at=now_shanghai_naive(),
                    sender_id=message_id,
                    sender_name=f"sender-{message_id}",
                    message_type="text",
                    message_id=f"{group_id}:{message_id}",
                )
                for message_id in range(1, 31)
            ],
        )

        rows = await self.memory.recall_archive(
            group_id,
            query="needle-five deployment detail",
            before_after=0,
            limit=4,
        )

        self.assertEqual(rows[0]["message_key"], f"{group_id}:5")
        self.assertEqual(rows[0]["recall_reason"], "bm25")

    async def test_fts_trigram_recalls_chinese_substring_with_bm25(self) -> None:
        group_id = -3503
        await self.memory.archive_message(
            group_id,
            "user",
            "今晚把蓝绿发布切到 canary-42，回滚窗口十分钟。",
            message_id="1",
            telegram_message_id=1,
            sender_id=7,
            sender_display_name="Alice",
            raw_text="今晚把蓝绿发布切到 canary-42，回滚窗口十分钟。",
        )

        rows = await self.memory.recall_archive(
            group_id,
            query="蓝绿发布",
            before_after=0,
            limit=4,
        )

        self.assertEqual(rows[0]["message_key"], f"{group_id}:1")
        self.assertEqual(rows[0]["recall_reason"], "bm25")

    async def test_two_character_cjk_term_supplements_fts_without_compaction(
        self,
    ) -> None:
        group_id = -3553
        await self.memory.archive_message(
            group_id,
            "user",
            "最终部署方案是双机热备。",
            message_id="1",
            telegram_message_id=1,
            raw_text="最终部署方案是双机热备。",
        )

        rows = await self.memory.recall_archive(
            group_id,
            query="部署那个",
            before_after=0,
            limit=4,
        )

        self.assertEqual(rows[0]["message_key"], f"{group_id}:1")
        self.assertEqual(rows[0]["recall_reason"], "lexical_fallback")

    async def test_vector_candidates_are_rrf_merged_and_rechecked_by_group(
        self,
    ) -> None:
        group_id = -3603
        other_group_id = -3604
        await self.memory.archive_message(
            group_id,
            "user",
            "Docker deployment checklist",
            message_id="1",
            telegram_message_id=1,
            raw_text="Docker deployment checklist",
        )
        await self.memory.archive_message(
            group_id,
            "user",
            "purple umbrella decision",
            message_id="2",
            telegram_message_id=2,
            raw_text="purple umbrella decision",
        )
        await self.memory.archive_message(
            other_group_id,
            "user",
            "private semantic record",
            message_id="9",
            telegram_message_id=9,
            raw_text="private semantic record",
        )
        provider = _VectorRecallProvider(
            [
                ArchiveVectorCandidate(f"{group_id}:2", 0.98),
                ArchiveVectorCandidate(f"{other_group_id}:9", 0.99),
            ]
        )
        memory = MemoryService(
            BotConfig(),
            _LLM(),  # type: ignore[arg-type]
            session_factory=self.session_factory,
            vector_recall_provider=provider,
        )
        try:
            rows = await memory.recall_archive(
                group_id,
                query="Docker deployment",
                before_after=0,
                limit=4,
            )
        finally:
            await memory.shutdown(timeout_seconds=1.0)

        by_key = {row["message_key"]: row for row in rows}
        self.assertEqual(set(by_key), {f"{group_id}:1", f"{group_id}:2"})
        self.assertEqual(by_key[f"{group_id}:1"]["recall_reason"], "bm25")
        self.assertEqual(by_key[f"{group_id}:2"]["recall_reason"], "vector")
        self.assertEqual(provider.calls[0]["group_id"], group_id)

    async def test_hot_history_never_loads_rows_older_than_retention(self) -> None:
        group_id = -4004
        now = now_shanghai_naive()
        async with self.session_factory() as session:
            session.add(Group(id=group_id, title="TTL test", settings={}))
            session.add_all(
                [
                    MessageVector(
                        group_id=group_id,
                        message_id=f"{group_id}:old",
                        role="user",
                        vector_id=f"{group_id}:old",
                        content="expired context",
                        created_at=now - timedelta(days=8),
                    ),
                    MessageVector(
                        group_id=group_id,
                        message_id=f"{group_id}:recent",
                        role="user",
                        vector_id=f"{group_id}:recent",
                        content="recent context",
                        created_at=now - timedelta(days=1),
                    ),
                ]
            )
            await session.commit()

        await self.memory._ensure_history_loaded(group_id)

        self.assertEqual(
            [item["content"] for item in self.memory.get_history(group_id)],
            ["recent context"],
        )

    async def test_mark_accessed_works_with_expire_on_commit_session(self) -> None:
        group_id = -5005
        await self._archive_range(group_id, 2)
        expiring_factory = async_sessionmaker(
            self.engine,
            expire_on_commit=True,
        )
        expiring_memory = MemoryService(
            BotConfig(),
            _LLM(),  # type: ignore[arg-type]
            session_factory=expiring_factory,
        )
        try:
            rows = await expiring_memory.recall_archive(
                group_id,
                message_keys=[f"{group_id}:1"],
                before_after=0,
                limit=1,
                mark_accessed=True,
            )
        finally:
            await expiring_memory.shutdown(timeout_seconds=1.0)

        self.assertEqual(rows[0]["message_key"], f"{group_id}:1")

    async def test_stale_replay_cannot_overwrite_edit_or_access_stats(self) -> None:
        group_id = -6006
        self.memory._archive_last_pruned_at[group_id] = time.monotonic()
        sent_at = datetime.now(timezone.utc) - timedelta(hours=1)
        edited_at = datetime.now(timezone.utc) - timedelta(minutes=30)
        await self.memory.archive_message(
            group_id,
            "user",
            "edited truth",
            message_id="7",
            telegram_message_id=7,
            created_at=sent_at,
            edited_at=edited_at,
            sender_id=7,
            sender_display_name="Alice",
            raw_text="edited truth",
        )
        await self.memory.recall_archive(
            group_id,
            message_keys=[f"{group_id}:7"],
            before_after=0,
            limit=1,
            mark_accessed=True,
        )

        await self.memory.archive_message(
            group_id,
            "user",
            "stale original replay",
            message_id="7",
            telegram_message_id=7,
            created_at=sent_at,
            sender_id=7,
            sender_display_name="Alice",
            raw_text="stale original replay",
        )

        async with self.session_factory() as session:
            row = (
                await session.execute(
                    select(GroupMessageArchive).where(
                        GroupMessageArchive.group_id == group_id,
                        GroupMessageArchive.message_key == f"{group_id}:7",
                    )
                )
            ).scalar_one()
            self.assertEqual(row.content, "edited truth")
            self.assertEqual(row.raw_text, "edited truth")
            self.assertEqual(row.access_count, 1)

    async def test_deferred_duplicate_events_publish_enriched_snapshot(self) -> None:
        group_id = -6506
        self.memory._archive_last_pruned_at[group_id] = time.monotonic()
        sent_at = datetime.now(timezone.utc)
        await self.memory.archive_message(
            group_id,
            "user",
            "photo placeholder",
            message_id="8",
            telegram_message_id=8,
            created_at=sent_at,
            raw_text="caption",
            defer_persistence=True,
        )
        await self.memory.archive_message(
            group_id,
            "user",
            "photo placeholder\nOCR detail",
            message_id="8",
            telegram_message_id=8,
            created_at=sent_at,
            sender_id=8,
            sender_display_name="Alice",
            raw_text="caption",
            derived_text="OCR detail",
            extra_metadata={"sender_is_tg_admin": True},
            defer_persistence=True,
        )
        self.assertTrue(await self.memory.flush_pending_writes(timeout_seconds=5.0))

        async with self.session_factory() as session:
            row = (
                await session.execute(
                    select(GroupMessageArchive).where(
                        GroupMessageArchive.group_id == group_id,
                        GroupMessageArchive.message_key == f"{group_id}:8",
                    )
                )
            ).scalar_one()
        self.assertEqual(row.content, "photo placeholder\nOCR detail")
        self.assertEqual(row.raw_text, "caption")
        self.assertEqual(row.derived_text, "OCR detail")
        self.assertEqual(row.sender_display_name, "Alice")
        self.assertTrue(row.extra_metadata["sender_is_tg_admin"])

    async def test_global_retention_physically_removes_inactive_group_rows(self) -> None:
        group_id = -7007
        self.memory._archive_last_pruned_at[group_id] = time.monotonic()
        await self.memory.archive_message(
            group_id,
            "user",
            "expired archive",
            message_id="1",
            telegram_message_id=1,
            created_at=datetime.now(timezone.utc) - timedelta(days=8),
            sender_id=1,
            sender_display_name="Old User",
            raw_text="expired archive",
        )

        result = await self.memory.prune_expired_archive_globally()

        async with self.session_factory() as session:
            remaining = (
                await session.execute(
                    select(func.count(GroupMessageArchive.id)).where(
                        GroupMessageArchive.group_id == group_id
                    )
                )
            ).scalar_one()
        self.assertEqual(result["archive_removed"], 1)
        self.assertEqual(remaining, 0)

    async def test_global_maintenance_caps_rows_for_inactive_groups(self) -> None:
        capped_group_id = -7107
        unaffected_group_id = -7108
        self.memory.memory_archive_max_messages_per_group = 3
        base = now_shanghai_naive() - timedelta(minutes=1)
        async with self.session_factory() as session:
            session.add_all(
                [
                    GroupMessageArchive(
                        group_id=capped_group_id,
                        message_key=f"{capped_group_id}:{message_id}",
                        telegram_message_id=message_id,
                        content=f"capped message {message_id}",
                        raw_text=f"capped message {message_id}",
                        sent_at=base + timedelta(seconds=message_id),
                    )
                    for message_id in range(1, 6)
                ]
                + [
                    GroupMessageArchive(
                        group_id=unaffected_group_id,
                        message_key=f"{unaffected_group_id}:{message_id}",
                        telegram_message_id=message_id,
                        content=f"unaffected message {message_id}",
                        raw_text=f"unaffected message {message_id}",
                        sent_at=base + timedelta(seconds=message_id),
                    )
                    for message_id in range(1, 3)
                ]
            )
            await session.commit()

        self.assertNotIn(capped_group_id, self.memory._history)

        await self.memory.prune_expired_archive_globally()

        async with self.session_factory() as session:
            capped_keys = list(
                (
                    await session.execute(
                        select(GroupMessageArchive.message_key)
                        .where(GroupMessageArchive.group_id == capped_group_id)
                        .order_by(GroupMessageArchive.sent_at.asc())
                    )
                ).scalars()
            )
            unaffected_count = (
                await session.execute(
                    select(func.count(GroupMessageArchive.id)).where(
                        GroupMessageArchive.group_id == unaffected_group_id
                    )
                )
            ).scalar_one()

        self.assertEqual(
            capped_keys,
            [f"{capped_group_id}:3", f"{capped_group_id}:4", f"{capped_group_id}:5"],
        )
        self.assertEqual(unaffected_count, 2)


if __name__ == "__main__":
    unittest.main()
