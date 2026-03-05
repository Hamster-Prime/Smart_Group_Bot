from __future__ import annotations

import logging
from collections import deque
from typing import Any

import litellm
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from bot.config import BotConfig, MemoryV2Config
from bot.services.llm import LLMService
from bot.services.memory_v2 import MemoryV2Manager

log = logging.getLogger(__name__)


class MemoryService:
    """Memory v2 service: working memory + long-term memory manager."""

    def __init__(
        self,
        config: BotConfig,
        llm: LLMService,
        *,
        session_factory: async_sessionmaker[AsyncSession] | None = None,
        memory_v2: MemoryV2Config | None = None,
    ) -> None:
        if session_factory is None:
            raise ValueError("MemoryService requires session_factory in v2 mode")

        self.max_context = config.max_context_tokens
        self.max_output = config.max_output_tokens
        self.llm = llm

        self._cfg = memory_v2 or MemoryV2Config()
        if not self._cfg.enabled:
            raise ValueError("Memory v2 is required; set MEMORY_V2_ENABLED=true")

        self._working_recent_items = max(1, int(self._cfg.working_recent_items))
        self._history: dict[int, deque[dict[str, str]]] = {}
        self._llm_max_history_items = 2000
        self._llm_reserve_tokens = max(1024, self.max_output // 2)

        self._manager = MemoryV2Manager(
            config=self._cfg,
            llm=llm,
            session_factory=session_factory,
        )
        log.info(
            "Memory service initialized: working_recent_items=%d backend=%s",
            self._working_recent_items,
            self._cfg.vector_backend,
        )

    async def bootstrap(self) -> None:
        """Load working memory from long-term storage and run one-time migration."""
        migration_stats = await self._manager.migrate_legacy_if_needed()
        working = await self._manager.load_working_memory(max_items=self._working_recent_items)
        self._history = {
            gid: deque(items, maxlen=self._working_recent_items)
            for gid, items in working.items()
        }

        loaded_groups = len(self._history)
        loaded_messages = sum(len(v) for v in self._history.values())
        log.info(
            "Memory bootstrap done: groups=%d working_messages=%d migrated=%d",
            loaded_groups,
            loaded_messages,
            migration_stats.get("migrated_messages", 0),
        )

    def _working(self, group_id: int) -> deque[dict[str, str]]:
        buf = self._history.get(group_id)
        if buf is None:
            buf = deque(maxlen=self._working_recent_items)
            self._history[group_id] = buf
        return buf

    def get_history(self, group_id: int) -> list[dict[str, str]]:
        return list(self._working(group_id))

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

        buf = self._working(group_id)
        buf.append({"role": role, "content": text})
        self._manager.schedule_index_message(
            group_id=group_id,
            role=role,
            content=text,
            user_id=user_id,
            message_type=message_type,
            message_id=message_id,
        )

    def _count_tokens(self, messages: list[dict[str, str]]) -> int:
        try:
            return litellm.token_counter(model=self.llm.main.model, messages=messages)
        except Exception:
            return sum(len(m.get("content", "")) for m in messages)

    def token_usage(self, group_id: int) -> int:
        return self._count_tokens(self.get_history(group_id))

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
        selected: list[dict[str, str]] = []
        used_chars = 0
        for msg in reversed(messages):
            content = msg.get("content", "")
            item_chars = max(24, len(content))
            if selected and used_chars + item_chars > budget_chars:
                break
            selected.append({"role": msg.get("role", "user"), "content": content})
            used_chars += item_chars
            if len(selected) >= max_items:
                break
        selected.reverse()
        return selected

    async def get_history_for_llm(
        self,
        group_id: int,
        *,
        query: str,
        reserve_tokens: int | None = None,
        max_items: int | None = None,
    ) -> list[dict[str, str]]:
        working = self.get_history(group_id)
        context_messages = await self._manager.build_context_messages(
            group_id=group_id,
            query=query,
            working_messages=working,
            max_working_items=max_items,
        )

        reserve = self._llm_reserve_tokens if reserve_tokens is None else max(0, reserve_tokens)
        item_limit = self._llm_max_history_items if max_items is None else max(1, max_items)
        budget_tokens = max(1024, self.max_context - self.max_output - reserve)
        return self._trim_by_token_budget(
            context_messages,
            budget_tokens=budget_tokens,
            max_items=item_limit,
        )

    async def get_history_for_llm_enhanced(
        self,
        group_id: int,
        *,
        query: str,
        reserve_tokens: int | None = None,
        max_items: int | None = None,
    ) -> list[dict[str, str]]:
        return await self.get_history_for_llm(
            group_id,
            query=query,
            reserve_tokens=reserve_tokens,
            max_items=max_items,
        )

    async def maybe_run_daily_memory_maintenance(self) -> dict[str, int]:
        return await self._manager.maybe_run_daily_maintenance()

    async def collect_metrics(self, group_id: int) -> dict[str, float | int]:
        return await self._manager.metrics.collect_metrics(group_id)

    async def flush_background_tasks(self, timeout_sec: float = 5.0) -> None:
        await self._manager.flush_pending_index_tasks(timeout_sec=timeout_sec)
