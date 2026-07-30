from __future__ import annotations

import os
import tempfile
import unittest
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from sqlalchemy import select, text, update

from bot.config import BotConfig, EmbedConfig, EmbedEndpointConfig, ModelConfig
from bot.db.engine import init_db
from bot.db.models import GroupMessageArchive, GroupMessageArchiveEmbedding
from bot.services.archive_vector import SQLiteArchiveVectorRecallProvider
from bot.services.llm import EmbeddingBatchResult, LLMService
from bot.services.memory import MemoryService
from bot.utils.timezone import now_shanghai_naive


class _DeterministicEmbeddingService:
    def __init__(self) -> None:
        self.space_id = "test-space-a"
        self.calls: list[tuple[list[str], float | None]] = []
        self.available = True

    def primary_embedding_space_id(self) -> str:
        return self.space_id

    @staticmethod
    def _vector(text: str) -> list[float]:
        normalized = text.lower()
        if "cat" in normalized or "feline" in normalized:
            return [1.0, 0.0, 0.0]
        if "deploy" in normalized or "release" in normalized:
            return [0.0, 1.0, 0.0]
        return [0.0, 0.0, 1.0]

    async def embed_primary_with_space(
        self,
        texts: list[str],
        *,
        total_deadline_sec: float | None = None,
    ) -> EmbeddingBatchResult | None:
        self.calls.append((list(texts), total_deadline_sec))
        if not self.available:
            return None
        vectors = [self._vector(value) for value in texts]
        return EmbeddingBatchResult(
            vectors=vectors,
            space_id=self.space_id,
            model="test/embed",
            dimensions=3,
        )


class SQLiteArchiveVectorProviderTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        fd, self.path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.engine, self.session_factory = await init_db(
            f"sqlite+aiosqlite:///{self.path}"
        )
        self.llm = _DeterministicEmbeddingService()
        self.provider = SQLiteArchiveVectorRecallProvider(
            session_factory=self.session_factory,
            llm=self.llm,  # type: ignore[arg-type]
            retention_days=7,
            maintenance_interval_seconds=60,
        )

    async def asyncTearDown(self) -> None:
        await self.provider.shutdown(timeout_seconds=0.5)
        await self.engine.dispose()
        for suffix in ("", "-wal", "-shm"):
            try:
                os.remove(self.path + suffix)
            except OSError:
                pass

    async def _add_archive(
        self,
        group_id: int,
        message_id: int,
        content: str,
    ) -> int:
        async with self.session_factory() as session:
            row = GroupMessageArchive(
                group_id=group_id,
                message_key=f"{group_id}:{message_id}",
                telegram_message_id=message_id,
                role="user",
                direction="inbound",
                sender_kind="user",
                sender_display_name="Alice",
                message_type="text",
                content=content,
                raw_text=content,
                sent_at=now_shanghai_naive(),
                ingested_at=now_shanghai_naive(),
            )
            session.add(row)
            await session.commit()
            await session.refresh(row)
            return int(row.id)

    async def test_trigger_lifecycle_and_semantic_recall_are_group_scoped(self) -> None:
        cat_id = await self._add_archive(-101, 1, "cat adoption notes")
        await self._add_archive(-101, 2, "deployment release checklist")
        other_id = await self._add_archive(-202, 1, "private feline record")

        async with self.session_factory() as session:
            initial = list(
                (
                    await session.execute(
                        select(GroupMessageArchiveEmbedding).order_by(
                            GroupMessageArchiveEmbedding.archive_id
                        )
                    )
                ).scalars()
            )
        self.assertEqual([row.status for row in initial], ["pending"] * 3)

        processed = await self.provider.index_pending_once(max_rows=16)
        self.assertEqual(processed, 3)
        async with self.session_factory() as session:
            await session.execute(
                update(GroupMessageArchiveEmbedding)
                .where(GroupMessageArchiveEmbedding.archive_id == other_id)
                .values(group_id=-101)
            )
            await session.commit()

        rows = await self.provider.recall(
            group_id=-101,
            query="feline information",
            cutoff=now_shanghai_naive() - timedelta(days=7),
            limit=8,
            exclude_message_keys=(),
        )
        excluded = await self.provider.recall(
            group_id=-101,
            query="feline information",
            cutoff=now_shanghai_naive() - timedelta(days=7),
            limit=8,
            exclude_message_keys=("-101:1",),
        )

        self.assertEqual(rows[0].message_key, "-101:1")
        self.assertNotIn("-202:1", [row.message_key for row in rows])
        self.assertNotIn("-101:1", [row.message_key for row in excluded])

        async with self.session_factory() as session:
            await session.execute(
                text(
                    "UPDATE group_message_archive SET content='release changed', "
                    "raw_text='release changed' WHERE id=:archive_id"
                ),
                {"archive_id": cat_id},
            )
            await session.commit()
            invalidated = await session.get(
                GroupMessageArchiveEmbedding,
                cat_id,
            )
            self.assertIsNotNone(invalidated)
            self.assertEqual(invalidated.status, "pending")
            self.assertIsNone(invalidated.embedding)

            await session.execute(
                text("DELETE FROM group_message_archive WHERE id=:archive_id"),
                {"archive_id": cat_id},
            )
            await session.commit()
            session.expire_all()
            self.assertIsNone(
                await session.get(GroupMessageArchiveEmbedding, cat_id)
            )

    async def test_model_space_change_reindexes_and_failures_degrade_to_empty(
        self,
    ) -> None:
        archive_id = await self._add_archive(-303, 1, "cat memory")
        await self.provider.index_pending_once(max_rows=4)
        async with self.session_factory() as session:
            ready = await session.get(GroupMessageArchiveEmbedding, archive_id)
            self.assertIsNotNone(ready)
            self.assertEqual(ready.space_id, "test-space-a")
            self.assertEqual(ready.status, "ready")

        self.llm.space_id = "test-space-b"
        await self.provider.index_pending_once(max_rows=4)
        async with self.session_factory() as session:
            reindexed = await session.get(
                GroupMessageArchiveEmbedding,
                archive_id,
            )
            self.assertIsNotNone(reindexed)
            self.assertEqual(reindexed.space_id, "test-space-b")
            self.assertEqual(reindexed.status, "ready")

        self.llm.available = False
        rows = await self.provider.recall(
            group_id=-303,
            query="feline",
            cutoff=now_shanghai_naive() - timedelta(days=7),
            limit=4,
            exclude_message_keys=(),
        )
        self.assertEqual(rows, [])


class MemoryVectorProviderLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def test_memory_starts_notifies_reconfigures_and_stops_provider(self) -> None:
        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        engine, session_factory = await init_db(
            f"sqlite+aiosqlite:///{path}"
        )
        provider = SimpleNamespace(
            start=Mock(),
            notify_archive_changed=Mock(),
            reconfigure=Mock(),
            shutdown=AsyncMock(),
            recall=AsyncMock(return_value=[]),
        )
        llm = SimpleNamespace(main=SimpleNamespace(model="test/model"))
        memory = MemoryService(
            BotConfig(),
            llm,  # type: ignore[arg-type]
            session_factory=session_factory,
            vector_recall_provider=provider,
        )
        try:
            await memory.bootstrap()
            provider.start.assert_called_once_with()

            await memory.archive_message(
                -404,
                "user",
                "wake vector indexer",
                message_id="1",
                telegram_message_id=1,
                raw_text="wake vector indexer",
            )
            provider.notify_archive_changed.assert_called()

            updated = BotConfig(memory_retention_days=9)
            memory.reconfigure(updated)
            provider.reconfigure.assert_called_with(retention_days=9)
        finally:
            await memory.shutdown(timeout_seconds=1.0)
            provider.shutdown.assert_awaited()
            await engine.dispose()
            for suffix in ("", "-wal", "-shm"):
                try:
                    os.remove(path + suffix)
                except OSError:
                    pass


class StablePrimaryEmbeddingTests(unittest.IsolatedAsyncioTestCase):
    async def test_primary_persistent_embedding_never_uses_fallback(self) -> None:
        main = ModelConfig(model="openai/main", api_key="main-key")
        primary = EmbedConfig(
            model="openai/primary-embed",
            provider="openai",
            api_key="secret-primary-key",
            retry_attempts=1,
            fallbacks=[
                EmbedEndpointConfig(
                    model="openai/fallback-embed",
                    provider="openai",
                    api_key="fallback-key",
                    retry_attempts=1,
                )
            ],
        )
        llm = LLMService(main, main, embed=primary)
        embedding_call = AsyncMock(side_effect=RuntimeError("primary down"))

        with patch("bot.services.llm.litellm.aembedding", embedding_call):
            result = await llm.embed_primary_with_space(["hello"])

        self.assertIsNone(result)
        self.assertEqual(embedding_call.await_count, 1)
        self.assertEqual(
            embedding_call.await_args.kwargs["model"],
            "openai/primary-embed",
        )
        self.assertNotIn("secret-primary-key", llm.primary_embedding_space_id())

    async def test_primary_result_reports_stable_space_and_dimensions(self) -> None:
        main = ModelConfig(model="openai/main", api_key="main-key")
        primary = EmbedConfig(
            model="openai/primary-embed",
            provider="openai",
            api_key="secret-key",
            retry_attempts=1,
        )
        llm = LLMService(main, main, embed=primary)
        response = SimpleNamespace(
            data=[
                {"embedding": [0.1, 0.2, 0.3]},
                {"embedding": [0.4, 0.5, 0.6]},
            ]
        )

        with patch(
            "bot.services.llm.litellm.aembedding",
            AsyncMock(return_value=response),
        ):
            result = await llm.embed_primary_with_space(["one", "two"])

        self.assertIsNotNone(result)
        assert result is not None
        self.assertEqual(result.dimensions, 3)
        self.assertEqual(result.space_id, llm.primary_embedding_space_id())
        self.assertEqual(result.model, "openai/primary-embed")
