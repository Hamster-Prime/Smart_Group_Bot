"""Execution priority shared by update workers and outbound resource gates.

The bot has a number of expensive, best-effort features (LLM replies, patrol,
media lookup, ...), while moderation and permission changes are latency and
safety sensitive.  A context variable lets the update queue mark security
work once and have every nested Telegram request inherit the same priority.
"""

from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager, contextmanager
from contextvars import ContextVar, Token
from enum import IntEnum
from typing import Final


class ExecutionPriority(IntEnum):
    """Lower numeric values represent more urgent work."""

    CRITICAL = 0
    HIGH = 10
    NORMAL = 100


_CURRENT_PRIORITY: ContextVar[ExecutionPriority] = ContextVar(
    "smart_group_bot_execution_priority",
    default=ExecutionPriority.NORMAL,
)


def current_execution_priority() -> ExecutionPriority:
    return _CURRENT_PRIORITY.get()


def is_privileged_execution() -> bool:
    return current_execution_priority() < ExecutionPriority.NORMAL


@contextmanager
def execution_priority_scope(
    priority: ExecutionPriority,
) -> Iterator[None]:
    token: Token[ExecutionPriority] = _CURRENT_PRIORITY.set(
        ExecutionPriority(priority)
    )
    try:
        yield
    finally:
        _CURRENT_PRIORITY.reset(token)


def privileged_request_scope(*, background: bool = False) -> Iterator[None]:
    """Mark nested work as interactive-critical or privileged background work."""

    return execution_priority_scope(
        ExecutionPriority.HIGH if background else ExecutionPriority.CRITICAL
    )


class _PriorityCapacitySemaphore:
    """Capacity semaphore whose released slots go to urgent waiters first."""

    def __init__(self, capacity: int) -> None:
        self.capacity = max(1, int(capacity))
        self._value = self.capacity
        self._waiters: dict[ExecutionPriority, deque[asyncio.Future[bool]]] = {
            priority: deque() for priority in ExecutionPriority
        }

    async def acquire(self, priority: ExecutionPriority) -> None:
        if self._value > 0 and not any(self._waiters.values()):
            self._value -= 1
            return
        future = asyncio.get_running_loop().create_future()
        self._waiters[priority].append(future)
        try:
            await future
        except BaseException:
            if future.done() and not future.cancelled():
                self.release()
            else:
                future.cancel()
            raise

    def release(self) -> None:
        for priority in ExecutionPriority:
            queue = self._waiters[priority]
            while queue:
                future = queue.popleft()
                if future.done():
                    continue
                future.set_result(True)
                return
        self._value = min(self.capacity, self._value + 1)


class ReservedCapacityGate:
    """Three-tier admission gate with capacity reserved for urgent work.

    ``normal_capacity`` caps ordinary work.  ``noncritical_capacity`` caps the
    combined ordinary + privileged-background workload.  The difference
    between ``total_capacity`` and ``noncritical_capacity`` is therefore always
    available to interactive/security requests.
    """

    def __init__(
        self,
        *,
        total_capacity: int,
        noncritical_capacity: int,
        normal_capacity: int,
    ) -> None:
        total = max(1, int(total_capacity))
        noncritical = max(1, min(total, int(noncritical_capacity)))
        normal = max(1, min(noncritical, int(normal_capacity)))
        self.total_capacity: Final[int] = total
        self.noncritical_capacity: Final[int] = noncritical
        self.normal_capacity: Final[int] = normal
        self._total = _PriorityCapacitySemaphore(total)
        self._noncritical = asyncio.Semaphore(noncritical)
        self._normal = asyncio.Semaphore(normal)
        self._active = {priority: 0 for priority in ExecutionPriority}
        self._waiting = {priority: 0 for priority in ExecutionPriority}

    @staticmethod
    async def _acquire(semaphore: asyncio.Semaphore, timeout: float) -> None:
        async with asyncio.timeout(max(0.01, float(timeout))):
            await semaphore.acquire()

    @staticmethod
    async def _acquire_total(
        semaphore: _PriorityCapacitySemaphore,
        priority: ExecutionPriority,
        timeout: float,
    ) -> None:
        async with asyncio.timeout(max(0.01, float(timeout))):
            await semaphore.acquire(priority)

    @asynccontextmanager
    async def slot(
        self,
        *,
        priority: ExecutionPriority | None = None,
        timeout: float,
    ) -> AsyncIterator[None]:
        selected = ExecutionPriority(
            current_execution_priority() if priority is None else priority
        )
        loop = asyncio.get_running_loop()
        deadline = loop.time() + max(0.01, float(timeout))

        def remaining() -> float:
            value = deadline - loop.time()
            if value <= 0:
                raise TimeoutError("resource admission deadline exceeded")
            return value

        acquired: list[asyncio.Semaphore | _PriorityCapacitySemaphore] = []
        self._waiting[selected] += 1
        try:
            # Acquire from the most restrictive class to the shared total.  On
            # cancellation/timeout, already acquired permits are rolled back.
            if selected >= ExecutionPriority.NORMAL:
                await self._acquire(self._normal, remaining())
                acquired.append(self._normal)
            if selected >= ExecutionPriority.HIGH:
                await self._acquire(self._noncritical, remaining())
                acquired.append(self._noncritical)
            await self._acquire_total(self._total, selected, remaining())
            acquired.append(self._total)
        except BaseException:
            for semaphore in reversed(acquired):
                semaphore.release()
            raise
        finally:
            self._waiting[selected] -= 1

        self._active[selected] += 1
        try:
            yield
        finally:
            self._active[selected] -= 1
            for semaphore in reversed(acquired):
                semaphore.release()

    def snapshot(self) -> dict[str, int]:
        return {
            "total_capacity": self.total_capacity,
            "noncritical_capacity": self.noncritical_capacity,
            "normal_capacity": self.normal_capacity,
            "active_critical": self._active[ExecutionPriority.CRITICAL],
            "active_high": self._active[ExecutionPriority.HIGH],
            "active_normal": self._active[ExecutionPriority.NORMAL],
            "waiting_critical": self._waiting[ExecutionPriority.CRITICAL],
            "waiting_high": self._waiting[ExecutionPriority.HIGH],
            "waiting_normal": self._waiting[ExecutionPriority.NORMAL],
        }
