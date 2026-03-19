from __future__ import annotations

import asyncio
import logging
import re
from collections import deque
from typing import Any
from uuid import uuid4

import litellm
from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from bot.config import BotConfig
from bot.db.models import GroupContextSummary, GroupPermanentMemory, MessageVector
from bot.services.llm import LLMService
from bot.utils.prompts import COMPRESS_SYSTEM

log = logging.getLogger(__name__)


class MemoryService:
    """
    Simplified group memory service:
    - Persistent memory: explicit admin-managed entries.
    - Context memory: rolling conversation + auto summary compression.
    """

    def __init__(
        self,
        config: BotConfig,
        llm: LLMService,
        *,
        session_factory: async_sessionmaker[AsyncSession] | None = None,
    ) -> None:
        if session_factory is None:
            raise ValueError("MemoryService requires session_factory")

        self.max_context = max(1024, int(config.max_context_tokens))
        self.max_output = max(256, int(config.max_output_tokens))
        self.llm = llm
        self._session_factory = session_factory

        # Keep enough rolling context in memory to reduce DB reads.
        self._working_recent_items = max(20, int(config.decision_context_items) * 8)
        self._history: dict[int, deque[dict[str, str]]] = {}
        self._summary_cache: dict[int, str] = {}
        self._summary_locks: dict[int, asyncio.Lock] = {}

        self._llm_max_history_items = 2000
        self._llm_reserve_tokens = max(1024, self.max_output // 2)
        self._compression_trigger_ratio = 0.88
        self._compression_keep_tail = 8

        self._persist_tasks: set[asyncio.Task[None]] = set()

        log.info(
            "Memory service initialized: working_recent_items=%d",
            self._working_recent_items,
        )

    async def bootstrap(self) -> None:
        """
        Load cached context summary and recent per-group messages from DB.
        """
        async with self._session_factory() as session:
            summary_rows = await session.execute(select(GroupContextSummary))
            for row in summary_rows.scalars().all():
                summary = (row.summary or "").strip()
                if summary:
                    self._summary_cache[row.group_id] = summary

            group_rows = await session.execute(select(MessageVector.group_id).distinct())
            group_ids = [int(gid) for gid in group_rows.scalars().all()]

            for group_id in group_ids:
                stmt = (
                    select(MessageVector.role, MessageVector.content)
                    .where(MessageVector.group_id == group_id)
                    .order_by(MessageVector.id.desc())
                    .limit(self._working_recent_items)
                )
                rows = await session.execute(stmt)
                items = [
                    {"role": str(role or "user"), "content": str(content or "")}
                    for role, content in rows.all()
                    if str(content or "").strip()
                ]
                items.reverse()
                self._history[group_id] = deque(items, maxlen=self._working_recent_items)

        log.info(
            "Memory bootstrap done: groups=%d summaries=%d working_messages=%d",
            len(self._history),
            len(self._summary_cache),
            sum(len(v) for v in self._history.values()),
        )

    def _working(self, group_id: int) -> deque[dict[str, str]]:
        buf = self._history.get(group_id)
        if buf is None:
            buf = deque(maxlen=self._working_recent_items)
            self._history[group_id] = buf
        return buf

    def get_history(self, group_id: int) -> list[dict[str, str]]:
        return list(self._working(group_id))

    @property
    def session_factory(self) -> async_sessionmaker[AsyncSession]:
        return self._session_factory

    def add_message(
        self,
        group_id: int,
        role: str,
        content: str,
        *,
        user_id: int | None = None,
        message_type: str = "text",
        message_id: str | None = None,
    ) -> None:
        text = (content or "").strip()
        if not text:
            return

        self._working(group_id).append({"role": role, "content": text})
        task = asyncio.create_task(
            self._persist_message(
                group_id=group_id,
                role=role,
                content=text,
                user_id=user_id,
                message_type=message_type,
                message_id=message_id,
            ),
            name=f"memory-persist-{group_id}",
        )
        self._persist_tasks.add(task)
        task.add_done_callback(self._on_persist_done)

    def _on_persist_done(self, task: asyncio.Task[None]) -> None:
        self._persist_tasks.discard(task)
        if task.cancelled():
            return
        exc = task.exception()
        if exc:
            log.exception("memory persist task failed: %s", exc)

    @staticmethod
    def _scoped_message_id(group_id: int, message_id: str | None) -> str:
        raw = str(message_id or uuid4().hex).strip()
        if not raw:
            raw = uuid4().hex
        scoped = f"{group_id}:{raw}"
        return scoped[:64]

    async def _persist_message(
        self,
        *,
        group_id: int,
        role: str,
        content: str,
        user_id: int | None,
        message_type: str,
        message_id: str | None,
    ) -> None:
        scoped_id = self._scoped_message_id(group_id, message_id)
        async with self._session_factory() as session:
            exists_stmt = select(MessageVector.id).where(MessageVector.message_id == scoped_id)
            exists = await session.execute(exists_stmt)
            if exists.scalar_one_or_none() is not None:
                return

            row = MessageVector(
                group_id=group_id,
                message_id=scoped_id,
                role=(role or "user")[:16],
                content=content,
                importance_score=0.5,
                access_count=0,
                vector_id=scoped_id,
            )
            session.add(row)
            try:
                await session.commit()
            except IntegrityError:
                await session.rollback()

    def _count_tokens(self, messages: list[dict[str, str]]) -> int:
        try:
            return litellm.token_counter(model=self.llm.main.model, messages=messages)
        except Exception:
            return sum(len(m.get("content", "")) for m in messages)

    def token_usage(self, group_id: int) -> int:
        return self._count_tokens(self.get_history(group_id))

    async def _get_summary(self, group_id: int) -> str:
        cached = self._summary_cache.get(group_id)
        if cached is not None:
            return cached

        async with self._session_factory() as session:
            row = await session.get(GroupContextSummary, group_id)
            summary = (row.summary or "").strip() if row else ""
            self._summary_cache[group_id] = summary
            return summary

    async def _save_summary(self, group_id: int, summary: str) -> None:
        normalized = (summary or "").strip()
        self._summary_cache[group_id] = normalized

        async with self._session_factory() as session:
            row = await session.get(GroupContextSummary, group_id)
            if row is None:
                row = GroupContextSummary(group_id=group_id, summary=normalized)
                session.add(row)
            else:
                row.summary = normalized
            await session.commit()

    async def list_permanent_memories(
        self,
        group_id: int,
        *,
        limit: int = 40,
    ) -> list[GroupPermanentMemory]:
        async with self._session_factory() as session:
            stmt = (
                select(GroupPermanentMemory)
                .where(GroupPermanentMemory.group_id == group_id)
                .order_by(GroupPermanentMemory.id.asc())
                .limit(max(1, limit))
            )
            result = await session.execute(stmt)
            return list(result.scalars().all())

    async def add_permanent_memory(
        self,
        group_id: int,
        content: str,
        *,
        created_by: int = 0,
    ) -> tuple[GroupPermanentMemory | None, bool]:
        text = (content or "").strip()
        if not text:
            return None, False

        async with self._session_factory() as session:
            stmt = select(GroupPermanentMemory).where(
                GroupPermanentMemory.group_id == group_id,
                GroupPermanentMemory.content == text,
            )
            result = await session.execute(stmt)
            existing = result.scalars().first()
            if existing:
                return existing, False

            row = GroupPermanentMemory(
                group_id=group_id,
                content=text,
                created_by=created_by,
            )
            session.add(row)
            await session.commit()
            await session.refresh(row)
            return row, True

    async def delete_permanent_memory(
        self,
        group_id: int,
        target: str,
    ) -> list[GroupPermanentMemory]:
        query = (target or "").strip()
        if not query:
            return []

        deleted: list[GroupPermanentMemory] = []
        async with self._session_factory() as session:
            # Allow deleting by "#123" or "123".
            m = re.fullmatch(r"#?(\d+)", query)
            if m:
                mem_id = int(m.group(1))
                row = await session.get(GroupPermanentMemory, mem_id)
                if row and row.group_id == group_id:
                    deleted.append(row)
                    await session.delete(row)
                    await session.commit()
                return deleted

            stmt = (
                select(GroupPermanentMemory)
                .where(
                    GroupPermanentMemory.group_id == group_id,
                    GroupPermanentMemory.content.contains(query),
                )
                .order_by(GroupPermanentMemory.id.asc())
            )
            rows = list((await session.execute(stmt)).scalars().all())
            if not rows:
                return []

            for row in rows:
                deleted.append(row)
                await session.delete(row)
            await session.commit()
        return deleted

    async def clear_permanent_memory(self, group_id: int) -> int:
        async with self._session_factory() as session:
            stmt = delete(GroupPermanentMemory).where(GroupPermanentMemory.group_id == group_id)
            result = await session.execute(stmt)
            await session.commit()
            return int(result.rowcount or 0)

    async def replace_permanent_memory(
        self,
        group_id: int,
        *,
        target: str,
        new_content: str,
        created_by: int = 0,
    ) -> tuple[list[GroupPermanentMemory], GroupPermanentMemory | None, bool]:
        deleted = await self.delete_permanent_memory(group_id, target)
        created, created_new = await self.add_permanent_memory(
            group_id,
            new_content,
            created_by=created_by,
        )
        return deleted, created, created_new

    async def _format_system_memory_blocks(self, group_id: int) -> list[dict[str, str]]:
        blocks: list[dict[str, str]] = []
        permanent = await self.list_permanent_memories(group_id, limit=40)
        if permanent:
            lines = ["[permanent-memory]"]
            for item in permanent:
                text = (item.content or "").replace("\n", " ").strip()
                if len(text) > 200:
                    text = text[:200] + "..."
                lines.append(f"- #{item.id}: {text}")
            blocks.append({"role": "system", "content": "\n".join(lines)})

        summary = await self._get_summary(group_id)
        if summary:
            blocks.append({"role": "system", "content": f"[context-summary]\n{summary}"})
        return blocks

    async def _compress_group_context_if_needed(
        self,
        group_id: int,
        *,
        budget_tokens: int,
    ) -> None:
        buf = self._working(group_id)
        if len(buf) <= self._compression_keep_tail + 1:
            return

        system_blocks = await self._format_system_memory_blocks(group_id)
        candidate = [*system_blocks, *list(buf)]
        current_tokens = self._count_tokens(candidate)
        if current_tokens <= int(budget_tokens * self._compression_trigger_ratio):
            return

        lock = self._summary_locks.setdefault(group_id, asyncio.Lock())
        async with lock:
            buf = self._working(group_id)
            if len(buf) <= self._compression_keep_tail + 1:
                return

            keep_tail = min(max(self._compression_keep_tail, len(buf) // 3), len(buf) - 1)
            to_keep = list(buf)[-keep_tail:]
            to_compress = list(buf)[:-keep_tail]
            if not to_compress:
                return

            old_summary = await self._get_summary(group_id)
            history_lines: list[str] = []
            for item in to_compress:
                role = (item.get("role", "user") or "user").strip().lower()
                content = (item.get("content", "") or "").replace("\n", " ").strip()
                if not content:
                    continue
                if len(content) > 300:
                    content = content[:300] + "..."
                history_lines.append(f"{role}: {content}")

            if not history_lines:
                return

            payload = (
                "[已有摘要]\n"
                f"{old_summary or '(无)'}\n\n"
                "[新增对话片段]\n"
                f"{chr(10).join(history_lines)}\n\n"
                "请输出更新后的中文摘要，保留长期有效事实、约定、偏好与正在进行的重要话题。"
                "删除无意义寒暄和重复信息。"
            )
            compressed = (await self.llm.compress(COMPRESS_SYSTEM, payload)).strip()
            if not compressed:
                merged = old_summary.strip()
                delta = "；".join(history_lines[-10:])
                compressed = (f"{merged}\n{delta}" if merged else delta).strip()
                if len(compressed) > 2000:
                    compressed = compressed[-2000:]

            await self._save_summary(group_id, compressed)
            self._history[group_id] = deque(to_keep, maxlen=self._working_recent_items)
            log.info(
                "memory context compressed: group=%s compressed=%d kept=%d",
                group_id,
                len(to_compress),
                len(to_keep),
            )

    def _trim_by_token_budget(
        self,
        messages: list[dict[str, str]],
        *,
        budget_tokens: int,
        max_items: int,
    ) -> list[dict[str, str]]:
        if not messages:
            return []

        budget_chars = max(2048, budget_tokens * 4)
        systems = [m for m in messages if m.get("role") == "system"]
        others = [m for m in messages if m.get("role") != "system"]

        selected_systems: list[dict[str, str]] = []
        used_chars = 0
        for msg in systems:
            content = str(msg.get("content", ""))
            if selected_systems and used_chars + len(content) > budget_chars:
                break
            selected_systems.append({"role": "system", "content": content})
            used_chars += len(content)

        selected_tail: list[dict[str, str]] = []
        for msg in reversed(others):
            content = str(msg.get("content", ""))
            if selected_tail and used_chars + len(content) > budget_chars:
                break
            selected_tail.append({"role": msg.get("role", "user"), "content": content})
            used_chars += len(content)
            if len(selected_tail) >= max_items:
                break
        selected_tail.reverse()
        return [*selected_systems, *selected_tail]

    async def get_history_for_llm(
        self,
        group_id: int,
        *,
        query: str,
        reserve_tokens: int | None = None,
        max_items: int | None = None,
    ) -> list[dict[str, str]]:
        reserve = self._llm_reserve_tokens if reserve_tokens is None else max(0, reserve_tokens)
        budget_tokens = max(1024, self.max_context - self.max_output - reserve)
        item_limit = self._llm_max_history_items if max_items is None else max(1, max_items)

        await self._compress_group_context_if_needed(group_id, budget_tokens=budget_tokens)

        system_blocks = await self._format_system_memory_blocks(group_id)
        history_tail = list(self._working(group_id))
        if max_items is not None:
            history_tail = history_tail[-max(1, max_items) :]

        messages = [*system_blocks, *history_tail]
        return self._trim_by_token_budget(
            messages,
            budget_tokens=budget_tokens,
            max_items=item_limit,
        )

    async def flush_background_tasks(self, timeout_sec: float = 5.0) -> None:
        if not self._persist_tasks:
            return

        pending = list(self._persist_tasks)
        done, still = await asyncio.wait(pending, timeout=max(0.1, timeout_sec))
        for task in done:
            if task.cancelled():
                continue
            exc = task.exception()
            if exc:
                log.exception("memory persist task failed during flush: %s", exc)

        for task in still:
            task.cancel()
        if still:
            await asyncio.gather(*still, return_exceptions=True)
