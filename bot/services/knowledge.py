from __future__ import annotations

import logging
import struct

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from bot.config import KnowledgeConfig
from bot.db.models import KnowledgeEntry
from bot.services.llm import LLMService

log = logging.getLogger(__name__)


def _pack_embedding(vec: list[float]) -> bytes:
    """Pack float list to compact bytes."""
    return struct.pack(f"{len(vec)}f", *vec)


def _unpack_embedding(data: bytes) -> list[float]:
    """Unpack bytes back to float list."""
    n = len(data) // 4
    return list(struct.unpack(f"{n}f", data))


def _cosine_similarity(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = sum(x * x for x in a) ** 0.5
    norm_b = sum(x * x for x in b) ** 0.5
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return dot / (norm_a * norm_b)


class KnowledgeService:
    def __init__(self, config: KnowledgeConfig, llm: LLMService) -> None:
        self.config = config
        self.llm = llm

    async def _generate_embedding(self, text: str) -> bytes | None:
        """Generate embedding for text and pack to bytes."""
        vecs = await self.llm.embed([text])
        if not vecs:
            return None
        return _pack_embedding(vecs[0])

    async def add(
        self, session: AsyncSession, group_id: int, title: str, content: str
    ) -> KnowledgeEntry:
        combined = f"{title}\n{content}"
        emb = await self._generate_embedding(combined)
        entry = KnowledgeEntry(group_id=group_id, title=title, content=content, embedding=emb)
        session.add(entry)
        await session.flush()
        log.info("Knowledge added: [%s] %s (embedding=%s)", group_id, title, "ok" if emb else "failed")
        return entry

    async def search(self, session: AsyncSession, group_id: int, query: str) -> list[dict]:
        """Semantic search via embedding cosine similarity with relaxed fallback."""
        log.info("[KB] 语义搜索: group=%s, query='%s', top_k=%d", group_id, query[:50], self.config.top_k)

        query_vecs = await self.llm.embed([query])
        if not query_vecs:
            log.warning("[KB] 查询嵌入生成失败")
            return []
        query_vec = query_vecs[0]

        stmt = select(KnowledgeEntry).where(
            KnowledgeEntry.group_id == group_id,
            KnowledgeEntry.embedding.isnot(None),
        )
        result = await session.execute(stmt)
        entries = list(result.scalars().all())
        log.info("[KB] 候选条目: %d 条", len(entries))

        if not entries:
            return []

        all_scored: list[tuple[KnowledgeEntry, float]] = []
        strict_scored: list[tuple[KnowledgeEntry, float]] = []
        for entry in entries:
            entry_vec = _unpack_embedding(entry.embedding)
            sim = _cosine_similarity(query_vec, entry_vec)
            all_scored.append((entry, sim))
            if sim >= self.config.similarity_threshold:
                strict_scored.append((entry, sim))

        strict_scored.sort(key=lambda x: x[1], reverse=True)
        if strict_scored:
            top = strict_scored[: self.config.top_k]
            log.info("[KB] 严格命中: %d 条 (阈值=%.2f)", len(top), self.config.similarity_threshold)
            return [
                {"document": f"{e.title}\n{e.content}", "metadata": {"title": e.title}, "score": sim}
                for e, sim in top
            ]

        # 宽松召回：问题场景下避免“明明有KB却空结果”
        relaxed_threshold = max(0.10, self.config.similarity_threshold * 0.5)
        relaxed_scored = [(e, s) for e, s in all_scored if s >= relaxed_threshold]
        relaxed_scored.sort(key=lambda x: x[1], reverse=True)
        if relaxed_scored:
            top = relaxed_scored[: self.config.top_k]
            log.info("[KB] 宽松召回: %d 条 (阈值=%.2f)", len(top), relaxed_threshold)
            return [
                {"document": f"{e.title}\n{e.content}", "metadata": {"title": e.title}, "score": sim}
                for e, sim in top
            ]

        # 最后兜底：返回最相近的 top_k，防止持续空命中
        all_scored.sort(key=lambda x: x[1], reverse=True)
        top = all_scored[: self.config.top_k]
        if top:
            log.info("[KB] 兜底召回: %d 条 (best_score=%.4f)", len(top), top[0][1])
            return [
                {
                    "document": f"{e.title}\n{e.content}",
                    "metadata": {"title": e.title, "fallback": True},
                    "score": sim,
                }
                for e, sim in top
            ]

        return []

    async def backfill_embeddings(self, session: AsyncSession) -> int:
        """Generate embeddings for entries that don't have one yet."""
        stmt = select(KnowledgeEntry).where(KnowledgeEntry.embedding.is_(None))
        result = await session.execute(stmt)
        entries = list(result.scalars().all())
        if not entries:
            return 0

        count = 0
        for entry in entries:
            combined = f"{entry.title}\n{entry.content}"
            emb = await self._generate_embedding(combined)
            if emb:
                entry.embedding = emb
                count += 1
                log.info("[KB] 回填嵌入: id=%d title='%s'", entry.id, entry.title)
        await session.flush()
        log.info("[KB] 回填完成: %d/%d 条", count, len(entries))
        return count

    async def remove(self, session: AsyncSession, group_id: int, title: str) -> bool:
        stmt = select(KnowledgeEntry).where(
            KnowledgeEntry.group_id == group_id,
            KnowledgeEntry.title == title,
        )
        result = await session.execute(stmt)
        entry = result.scalar_one_or_none()
        if not entry:
            return False
        await session.delete(entry)
        return True

    async def list_entries(
        self, session: AsyncSession, group_id: int
    ) -> list[KnowledgeEntry]:
        stmt = select(KnowledgeEntry).where(
            KnowledgeEntry.group_id == group_id
        ).order_by(KnowledgeEntry.id)
        result = await session.execute(stmt)
        return list(result.scalars().all())

    async def list_titles(self, session: AsyncSession, group_id: int) -> list[str]:
        stmt = select(KnowledgeEntry.title).where(
            KnowledgeEntry.group_id == group_id
        ).order_by(KnowledgeEntry.id)
        result = await session.execute(stmt)
        return [row[0] for row in result.all() if row[0]]

    async def get_by_id(
        self, session: AsyncSession, entry_id: int
    ) -> KnowledgeEntry | None:
        return await session.get(KnowledgeEntry, entry_id)

    async def update(
        self,
        session: AsyncSession,
        entry_id: int,
        title: str | None = None,
        content: str | None = None,
    ) -> KnowledgeEntry | None:
        entry = await session.get(KnowledgeEntry, entry_id)
        if not entry:
            return None
        if title is not None:
            entry.title = title
        if content is not None:
            entry.content = content
        await session.flush()
        combined = f"{entry.title}\n{entry.content}"
        emb = await self._generate_embedding(combined)
        if emb:
            entry.embedding = emb
            await session.flush()
        return entry
