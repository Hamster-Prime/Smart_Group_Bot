from __future__ import annotations

import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.exceptions import TelegramNetworkError

from bot.services.request_priority import (
    ExecutionPriority,
    ReservedCapacityGate,
    execution_priority_scope,
)
from bot.services.telegram_session import PriorityAiohttpSession


class ReservedCapacityGateTests(unittest.IsolatedAsyncioTestCase):
    async def test_critical_waiter_overtakes_queued_normal_work(self) -> None:
        gate = ReservedCapacityGate(
            total_capacity=1,
            noncritical_capacity=1,
            normal_capacity=1,
        )
        release_first = asyncio.Event()
        release_critical = asyncio.Event()
        first_entered = asyncio.Event()
        normal_entered = asyncio.Event()
        critical_entered = asyncio.Event()

        async def first() -> None:
            with execution_priority_scope(ExecutionPriority.NORMAL):
                async with gate.slot(timeout=1.0):
                    first_entered.set()
                    await release_first.wait()

        async def normal_waiter() -> None:
            with execution_priority_scope(ExecutionPriority.NORMAL):
                async with gate.slot(timeout=1.0):
                    normal_entered.set()

        async def critical_waiter() -> None:
            with execution_priority_scope(ExecutionPriority.CRITICAL):
                async with gate.slot(timeout=1.0):
                    critical_entered.set()
                    await release_critical.wait()

        tasks = [asyncio.create_task(first())]
        await asyncio.wait_for(first_entered.wait(), timeout=0.2)
        tasks.append(asyncio.create_task(normal_waiter()))
        await asyncio.sleep(0)
        tasks.append(asyncio.create_task(critical_waiter()))
        await asyncio.sleep(0)
        release_first.set()
        await asyncio.wait_for(critical_entered.wait(), timeout=0.2)
        self.assertFalse(normal_entered.is_set())
        release_critical.set()
        await asyncio.wait_for(normal_entered.wait(), timeout=0.2)
        await asyncio.gather(*tasks)

    async def test_normal_work_cannot_consume_privileged_reserve(self) -> None:
        gate = ReservedCapacityGate(
            total_capacity=4,
            noncritical_capacity=3,
            normal_capacity=2,
        )
        release = asyncio.Event()

        async def occupy(priority: ExecutionPriority) -> asyncio.Event:
            entered = asyncio.Event()

            async def worker() -> None:
                with execution_priority_scope(priority):
                    async with gate.slot(timeout=0.2):
                        entered.set()
                        await release.wait()

            task = asyncio.create_task(worker())
            self.addAsyncCleanup(self._finish_task, task, release)
            await asyncio.wait_for(entered.wait(), timeout=0.2)
            return entered

        await occupy(ExecutionPriority.NORMAL)
        await occupy(ExecutionPriority.NORMAL)

        with self.assertRaises(TimeoutError):
            with execution_priority_scope(ExecutionPriority.NORMAL):
                async with gate.slot(timeout=0.01):
                    self.fail("ordinary work unexpectedly consumed reserved capacity")

        await occupy(ExecutionPriority.HIGH)
        await occupy(ExecutionPriority.CRITICAL)
        snapshot = gate.snapshot()
        self.assertEqual(snapshot["active_normal"], 2)
        self.assertEqual(snapshot["active_high"], 1)
        self.assertEqual(snapshot["active_critical"], 1)
        release.set()

    @staticmethod
    async def _finish_task(task: asyncio.Task[None], release: asyncio.Event) -> None:
        release.set()
        await asyncio.wait_for(task, timeout=0.2)


class PriorityTelegramSessionTests(unittest.IsolatedAsyncioTestCase):
    async def test_critical_request_uses_short_timeout(self) -> None:
        session = PriorityAiohttpSession(timeout=30.0, limit=8)
        method = SimpleNamespace(__api_method__="sendMessage")
        with patch.object(
            AiohttpSession,
            "make_request",
            new=AsyncMock(return_value=True),
        ) as request:
            with execution_priority_scope(ExecutionPriority.CRITICAL):
                result = await session.make_request(SimpleNamespace(), method)
        self.assertTrue(result)
        self.assertEqual(request.await_args.kwargs["timeout"], 8.0)

    async def test_repeated_network_failures_rebuild_connector(self) -> None:
        session = PriorityAiohttpSession(timeout=30.0, limit=8)
        method = SimpleNamespace(__api_method__="sendMessage")
        failure = TelegramNetworkError(method=method, message="broken connector")
        with (
            patch.object(
                AiohttpSession,
                "make_request",
                new=AsyncMock(side_effect=failure),
            ),
            patch.object(AiohttpSession, "close", new=AsyncMock()) as close,
        ):
            for _ in range(3):
                with self.assertRaises(TelegramNetworkError):
                    await session.make_request(SimpleNamespace(), method)
        self.assertGreaterEqual(close.await_count, 1)
        snapshot = session.health_snapshot()
        self.assertEqual(snapshot["connector_resets"], 1)
        self.assertFalse(snapshot["reset_requested"])


if __name__ == "__main__":
    unittest.main()
