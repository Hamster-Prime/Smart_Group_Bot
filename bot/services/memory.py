from __future__ import annotations

import asyncio
import logging
import re
from uuid import uuid4

import litellm
from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from bot.config import BotConfig
from bot.db.sqlite_session import is_database_locked_error
from bot.db.models import GroupContextSummary, GroupPermanentMemory, MessageVector
from bot.services.llm import LLMService
from bot.utils.prompts import COMPRESS_SYSTEM

log = logging.getLogger(__name__)


class MemoryService:
    """
    Group memory service:
    - Long-term memory: explicit admin-managed entries.
    - Temporary memory: one continuous per-group dialogue until compact.
    - Compacting clears raw dialogue history and keeps the merged summary.
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

        self._history: dict[int, list[dict[str, str]]] = {}
        self._summary_cache: dict[int, str] = {}
        self._summary_locks: dict[int, asyncio.Lock] = {}
        self._llm_reserve_tokens = max(1024, self.max_output // 2)

        log.info(
            "Memory service initialized: max_context=%d max_output=%d",
            self.max_context,
            self.max_output,
        )

    async def bootstrap(self) -> None:
        """Load cached summaries and active per-group dialogue history from DB."""
        async with self._session_factory() as session:
            summary_rows = await session.execute(select(GroupContextSummary))
            for row in summary_rows.scalars().all():
                summary = (row.summary or "").strip()
                if summary:
                    self._summary_cache[row.group_id] = summary

            message_rows = await session.execute(
                select(MessageVector.group_id, MessageVector.role, MessageVector.content).order_by(
                    MessageVector.group_id.asc(),
                    MessageVector.id.asc(),
                )
            )
            for group_id, role, content in message_rows.all():
                text = str(content or "").strip()
                if not text:
                    continue
                self._working(int(group_id)).append(
                    {
                        "role": str(role or "user"),
                        "content": text,
                    }
                )

        log.info(
            "Memory bootstrap done: groups=%d summaries=%d active_messages=%d",
            len(self._history),
            len(self._summary_cache),
            sum(len(v) for v in self._history.values()),
        )

    def _working(self, group_id: int) -> list[dict[str, str]]:
        buf = self._history.get(group_id)
        if buf is None:
            buf = []
            self._history[group_id] = buf
        return buf

    def get_history(self, group_id: int) -> list[dict[str, str]]:
        return list(self._working(group_id))

    @property
    def session_factory(self) -> async_sessionmaker[AsyncSession]:
        return self._session_factory

    async def add_message(
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

        inserted = await self._persist_message(
            group_id=group_id,
            role=role,
            content=text,
            user_id=user_id,
            message_type=message_type,
            message_id=message_id,
        )
        if inserted is not False:
            self._working(group_id).append(
                {
                    "role": (role or "user")[:16],
                    "content": text,
                }
            )

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
    ) -> bool | None:
        del user_id, message_type

        scoped_id = self._scoped_message_id(group_id, message_id)
        for attempt in range(2):
            async with self._session_factory() as session:
                row = MessageVector(
                    group_id=group_id,
                    message_id=scoped_id,
                    role=(role or "user")[:16],
                    content=content,
                )
                session.add(row)
                try:
                    await session.commit()
                    return True
                except IntegrityError:
                    await session.rollback()
                    return False
                except OperationalError as exc:
                    await session.rollback()
                    if not is_database_locked_error(exc):
                        raise
                    if attempt >= 1:
                        log.warning(
                            "memory persist skipped due sqlite lock: group=%s message_id=%s",
                            group_id,
                            scoped_id,
                        )
                        return None
                    await asyncio.sleep(0.15 * (attempt + 1))
        return None

    def _count_tokens(self, messages: list[dict[str, str]]) -> int:
        try:
            return litellm.token_counter(model=self.llm.main.model, messages=messages)
        except Exception:
            return sum(len(m.get("content", "")) for m in messages)

    def _llm_input_budget(self, reserve_tokens: int | None = None) -> int:
        reserve = self._llm_reserve_tokens if reserve_tokens is None else max(0, reserve_tokens)
        return max(1024, self.max_context - self.max_output - reserve)

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

    async def _clear_group_history_records(self, group_id: int) -> None:
        async with self._session_factory() as session:
            await session.execute(delete(MessageVector).where(MessageVector.group_id == group_id))
            await session.commit()

    @staticmethod
    def _render_compact_history(history: list[dict[str, str]]) -> list[str]:
        lines: list[str] = []
        for item in history:
            role = (item.get("role", "user") or "user").strip().lower()
            content = (item.get("content", "") or "").replace("\n", " ").strip()
            if not content:
                continue
            if len(content) > 300:
                content = content[:300] + "..."
            lines.append(f"{role}: {content}")
        return lines

    async def _compact_group_context_if_needed(
        self,
        group_id: int,
        *,
        budget_tokens: int,
    ) -> bool:
        history = list(self._working(group_id))
        if not history:
            return False

        system_blocks = await self._format_system_memory_blocks(group_id)
        candidate = [*system_blocks, *history]
        if self._count_tokens(candidate) < budget_tokens:
            return False

        lock = self._summary_locks.setdefault(group_id, asyncio.Lock())
        async with lock:
            history = list(self._working(group_id))
            if not history:
                return False

            system_blocks = await self._format_system_memory_blocks(group_id)
            candidate = [*system_blocks, *history]
            if self._count_tokens(candidate) < budget_tokens:
                return False

            history_lines = self._render_compact_history(history)
            if not history_lines:
                return False

            old_summary = await self._get_summary(group_id)
            payload = (
                "[已有摘要]\n"
                f"{old_summary or '(无)'}\n\n"
                "[新增对话片段]\n"
                f"{chr(10).join(history_lines)}\n\n"
                "请输出更新后的中文摘要，保留长期有效的事实、偏好、约定、未完成事项和最近仍然重要的上下文。"
            )
            compressed = (await self.llm.compress(COMPRESS_SYSTEM, payload)).strip()
            if not compressed:
                fallback_lines = history_lines[-10:]
                merged = old_summary.strip()
                delta = "\n".join(fallback_lines)
                compressed = (f"{merged}\n{delta}" if merged else delta).strip()
                if len(compressed) > 2000:
                    compressed = compressed[-2000:]

            try:
                await self._save_summary(group_id, compressed)
                await self._clear_group_history_records(group_id)
            except OperationalError as exc:
                if not is_database_locked_error(exc):
                    raise
                log.warning("memory compact skipped due sqlite lock: group=%s", group_id)
                return False
            self._history[group_id] = []
            log.info(
                "memory context compacted: group=%s cleared_messages=%d summary_chars=%d",
                group_id,
                len(history),
                len(compressed),
            )
            return True

    async def compact_if_needed(
        self,
        group_id: int,
        *,
        reserve_tokens: int | None = None,
    ) -> bool:
        return await self._compact_group_context_if_needed(
            group_id,
            budget_tokens=self._llm_input_budget(reserve_tokens),
        )

    def _trim_by_token_budget(
        self,
        messages: list[dict[str, str]],
        *,
        budget_tokens: int,
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
        selected_tail.reverse()
        return [*selected_systems, *selected_tail]

    async def get_history_for_llm(
        self,
        group_id: int,
        *,
        reserve_tokens: int | None = None,
    ) -> list[dict[str, str]]:
        budget_tokens = self._llm_input_budget(reserve_tokens)
        await self._compact_group_context_if_needed(group_id, budget_tokens=budget_tokens)

        messages = [
            *await self._format_system_memory_blocks(group_id),
            *list(self._working(group_id)),
        ]
        return self._trim_by_token_budget(messages, budget_tokens=budget_tokens)
