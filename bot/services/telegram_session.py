"""Starvation-resistant Telegram HTTP session.

All aiogram API calls pass through the Bot session.  Keeping admission control
here gives security commands reserved connector capacity even when unrelated
background jobs are busy, and lets a repeatedly broken aiohttp connector be
rebuilt without restarting the whole process.
"""

from __future__ import annotations

import asyncio
import logging
import time
import weakref
from collections.abc import AsyncGenerator
from typing import TYPE_CHECKING, Any

from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.exceptions import TelegramNetworkError

from bot.services.request_priority import (
    ExecutionPriority,
    ReservedCapacityGate,
    current_execution_priority,
)
from bot.services.resource_health import register_resource_health_provider

if TYPE_CHECKING:
    from aiogram import Bot
    from aiogram.methods import TelegramMethod
    from aiogram.methods.base import TelegramType

log = logging.getLogger(__name__)

TELEGRAM_TOTAL_CAPACITY = 64
TELEGRAM_NONCRITICAL_CAPACITY = 60
TELEGRAM_NORMAL_CAPACITY = 44
TELEGRAM_PRIVILEGED_TIMEOUT_SECONDS = 8.0
TELEGRAM_CRITICAL_ADMISSION_TIMEOUT_SECONDS = 1.5
TELEGRAM_HIGH_ADMISSION_TIMEOUT_SECONDS = 4.0
TELEGRAM_NORMAL_ADMISSION_TIMEOUT_SECONDS = 15.0
TELEGRAM_CONNECTOR_RESET_FAILURES = 2

_LIVE_SESSIONS: weakref.WeakSet[PriorityAiohttpSession] = weakref.WeakSet()


class PriorityAiohttpSession(AiohttpSession):
    """Aiohttp session with reserved capacity and connector self-healing."""

    def __init__(self, *, timeout: float, limit: int = TELEGRAM_TOTAL_CAPACITY) -> None:
        total = max(8, int(limit))
        noncritical = max(4, total - min(4, total // 4))
        normal = max(2, noncritical - min(16, max(2, total // 4)))
        super().__init__(timeout=timeout, limit=total)
        self._capacity_gate = ReservedCapacityGate(
            total_capacity=total,
            noncritical_capacity=noncritical,
            normal_capacity=normal,
        )
        self._active_requests = 0
        self._active_started_at: dict[asyncio.Task[Any], float] = {}
        self._consecutive_network_failures = 0
        self._connector_resets = 0
        self._reset_requested = False
        self._reset_lock = asyncio.Lock()
        self._last_success_monotonic = time.monotonic()
        self._last_failure_monotonic = 0.0
        _LIVE_SESSIONS.add(self)

    @staticmethod
    def _admission_timeout(priority: ExecutionPriority) -> float:
        if priority <= ExecutionPriority.CRITICAL:
            return TELEGRAM_CRITICAL_ADMISSION_TIMEOUT_SECONDS
        if priority <= ExecutionPriority.HIGH:
            return TELEGRAM_HIGH_ADMISSION_TIMEOUT_SECONDS
        return TELEGRAM_NORMAL_ADMISSION_TIMEOUT_SECONDS

    async def _reset_connector_if_idle(self) -> None:
        if not self._reset_requested or self._active_requests:
            return
        async with self._reset_lock:
            if not self._reset_requested or self._active_requests:
                return
            try:
                await super().close()
            finally:
                # ``create_session`` will build a fresh connector/session on the
                # next request.  Do not recreate eagerly while the service is idle.
                self._should_reset_connector = True
                self._reset_requested = False
                self._consecutive_network_failures = 0
                self._connector_resets += 1
                log.warning(
                    "Telegram HTTP connector reset after repeated network failures"
                )

    async def make_request(
        self,
        bot: Bot,
        method: TelegramMethod[TelegramType],
        timeout: int | None = None,
    ) -> TelegramType:
        priority = current_execution_priority()
        admission_timeout = self._admission_timeout(priority)
        effective_timeout: int | float | None = timeout
        if priority <= ExecutionPriority.CRITICAL and timeout is None:
            effective_timeout = min(
                float(self.timeout),
                TELEGRAM_PRIVILEGED_TIMEOUT_SECONDS,
            )

        try:
            async with self._capacity_gate.slot(
                priority=priority,
                timeout=admission_timeout,
            ):
                await self._reset_connector_if_idle()
                self._active_requests += 1
                owner_task = asyncio.current_task()
                if owner_task is not None:
                    self._active_started_at.setdefault(
                        owner_task,
                        time.monotonic(),
                    )
                try:
                    result = await super().make_request(
                        bot,
                        method,
                        timeout=effective_timeout,
                    )
                except TelegramNetworkError:
                    self._consecutive_network_failures += 1
                    self._last_failure_monotonic = time.monotonic()
                    if (
                        self._consecutive_network_failures
                        >= TELEGRAM_CONNECTOR_RESET_FAILURES
                    ):
                        self._reset_requested = True
                    raise
                else:
                    self._consecutive_network_failures = 0
                    self._last_success_monotonic = time.monotonic()
                    return result
                finally:
                    self._active_requests -= 1
                    if owner_task is not None:
                        self._active_started_at.pop(owner_task, None)
                    await self._reset_connector_if_idle()
        except TimeoutError as exc:
            raise TelegramNetworkError(
                method=method,
                message=(
                    "Telegram request admission timed out; reserved capacity "
                    f"priority={priority.name.lower()}"
                ),
            ) from exc

    async def stream_content(
        self,
        url: str,
        headers: dict[str, Any] | None = None,
        timeout: int = 30,
        chunk_size: int = 65536,
        raise_for_status: bool = True,
    ) -> AsyncGenerator[bytes, None]:
        priority = current_execution_priority()
        async with self._capacity_gate.slot(
            priority=priority,
            timeout=self._admission_timeout(priority),
        ):
            self._active_requests += 1
            owner_task = asyncio.current_task()
            if owner_task is not None:
                self._active_started_at.setdefault(owner_task, time.monotonic())
            try:
                async for chunk in super().stream_content(
                    url,
                    headers=headers,
                    timeout=timeout,
                    chunk_size=chunk_size,
                    raise_for_status=raise_for_status,
                ):
                    yield chunk
            finally:
                self._active_requests -= 1
                if owner_task is not None:
                    self._active_started_at.pop(owner_task, None)
                await self._reset_connector_if_idle()

    def health_snapshot(self) -> dict[str, Any]:
        now = time.monotonic()
        oldest_active = max(
            (now - started for started in self._active_started_at.values()),
            default=0.0,
        )
        return {
            **self._capacity_gate.snapshot(),
            "active_requests": self._active_requests,
            "oldest_active_seconds": round(oldest_active, 3),
            "consecutive_network_failures": self._consecutive_network_failures,
            "connector_resets": self._connector_resets,
            "reset_requested": self._reset_requested,
            "seconds_since_success": max(0.0, now - self._last_success_monotonic),
            "seconds_since_failure": (
                max(0.0, now - self._last_failure_monotonic)
                if self._last_failure_monotonic
                else None
            ),
        }


def telegram_session_health_snapshot() -> dict[str, Any]:
    sessions = list(_LIVE_SESSIONS)
    snapshots = [session.health_snapshot() for session in sessions]
    fatal = any(
        item["oldest_active_seconds"] >= 60.0
        and (
            item["active_requests"] >= item["total_capacity"]
            or item["reset_requested"]
            or item["waiting_critical"] > 0
        )
        for item in snapshots
    )
    degraded = fatal or any(
        item["reset_requested"]
        or item["waiting_critical"] > 0
        and item["active_requests"] >= item["total_capacity"]
        for item in snapshots
    )
    return {
        "ok": not degraded,
        "fatal": fatal,
        "sessions": snapshots,
    }


register_resource_health_provider("telegram_http", telegram_session_health_snapshot)
