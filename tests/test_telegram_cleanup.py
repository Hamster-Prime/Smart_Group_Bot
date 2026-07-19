from __future__ import annotations

import asyncio
import os
import tempfile
import unittest
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

from aiogram.exceptions import TelegramBadRequest
from aiogram.methods import DeleteMessage
from sqlalchemy import func, select, update

from bot.db.engine import init_db
from bot.db.models import TelegramDeleteJob
from bot.services.telegram_cleanup import TelegramCleanupScheduler
from bot.utils.telegram import (
    configure_telegram_cleanup_scheduler,
    schedule_message_auto_delete_durable,
)
from bot.utils.timezone import now_shanghai_naive


async def _wait_until(predicate, *, timeout: float = 2.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while not await predicate():
        if asyncio.get_running_loop().time() >= deadline:
            raise AssertionError("condition was not reached before timeout")
        await asyncio.sleep(0.01)


class TelegramCleanupQueueTests(unittest.IsolatedAsyncioTestCase):
    async def test_startup_database_failure_is_synchronous_and_visible(self) -> None:
        class BrokenSessionFactory:
            def __call__(self):
                return self

            async def __aenter__(self):
                raise RuntimeError("database unavailable")

            async def __aexit__(self, *_args):
                return False

        scheduler = TelegramCleanupScheduler(
            bot=SimpleNamespace(),
            session_factory=BrokenSessionFactory(),
            startup_timeout_seconds=0.2,
        )
        with self.assertRaisesRegex(RuntimeError, "database unavailable"):
            await scheduler.start()
        self.assertIsNotNone(scheduler.failure)
        self.assertFalse(scheduler.enqueue(
            chat_id=-100,
            message_id=1,
            due_at=now_shanghai_naive(),
        ))

    async def test_bounded_queue_overload_is_explicitly_unhealthy(self) -> None:
        scheduler = TelegramCleanupScheduler(
            bot=SimpleNamespace(),
            session_factory=object(),
            queue_size=1,
        )
        # This unit test isolates the synchronous intake contract; production
        # reaches the same state through ``await scheduler.start()``.
        scheduler._accepting = True
        due_at = now_shanghai_naive() + timedelta(minutes=1)
        self.assertTrue(scheduler.enqueue(chat_id=-100, message_id=1, due_at=due_at))
        self.assertFalse(scheduler.enqueue(chat_id=-100, message_id=2, due_at=due_at))
        self.assertEqual(scheduler.dropped_requests, 1)
        self.assertIsNotNone(scheduler.failure)

    async def test_monitor_surfaces_overload_without_canceling_worker(self) -> None:
        scheduler = TelegramCleanupScheduler(
            bot=SimpleNamespace(),
            session_factory=object(),
            queue_size=1,
        )

        async def worker() -> None:
            await asyncio.Future()

        worker_task = asyncio.create_task(worker())
        scheduler._worker_task = worker_task
        scheduler._accepting = True
        due_at = now_shanghai_naive() + timedelta(minutes=1)
        scheduler.enqueue(chat_id=-100, message_id=1, due_at=due_at)
        scheduler.enqueue(chat_id=-100, message_id=2, due_at=due_at)

        with self.assertRaisesRegex(RuntimeError, "became unhealthy"):
            await scheduler.monitor()
        self.assertFalse(worker_task.cancelled())

        scheduler._stopping = True
        worker_task.cancel()
        await asyncio.gather(worker_task, return_exceptions=True)


class TelegramCleanupPersistenceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        fd, self.path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.engine, self.session_factory = await init_db(
            f"sqlite+aiosqlite:///{self.path}"
        )
        self.schedulers: list[TelegramCleanupScheduler] = []

    async def asyncTearDown(self) -> None:
        configure_telegram_cleanup_scheduler(None)
        for scheduler in reversed(self.schedulers):
            try:
                await scheduler.stop(timeout_seconds=0.5)
            except Exception:
                pass
        await self.engine.dispose()
        for suffix in ("", "-wal", "-shm"):
            try:
                os.remove(self.path + suffix)
            except OSError:
                pass

    def _scheduler(self, bot: object, **overrides) -> TelegramCleanupScheduler:
        options = {
            "bot": bot,
            "session_factory": self.session_factory,
            "poll_interval_seconds": 0.02,
            "delete_timeout_seconds": 0.05,
            "cancel_grace_seconds": 0.01,
            "lease_seconds": 0.15,
            "retry_base_seconds": 0.02,
            "retry_max_seconds": 0.05,
            "max_attempts": 3,
        }
        options.update(overrides)
        scheduler = TelegramCleanupScheduler(**options)
        self.schedulers.append(scheduler)
        return scheduler

    async def _job_count(self) -> int:
        async with self.session_factory() as session:
            return int(
                (
                    await session.execute(
                        select(func.count()).select_from(TelegramDeleteJob)
                    )
                ).scalar_one()
            )

    async def test_shutdown_persists_queue_and_restart_recovers_due_job(self) -> None:
        first_bot = SimpleNamespace(delete_message=AsyncMock(return_value=True))
        first = self._scheduler(first_bot)
        await first.start()
        self.assertTrue(
            first.enqueue(
                chat_id=-100,
                message_id=10,
                due_at=now_shanghai_naive() + timedelta(hours=1),
            )
        )
        await first.stop(timeout_seconds=0.5)
        self.assertEqual(await self._job_count(), 1)
        first_bot.delete_message.assert_not_awaited()

        async with self.session_factory() as session:
            await session.execute(
                update(TelegramDeleteJob).values(
                    due_at=now_shanghai_naive() - timedelta(seconds=1)
                )
            )
            await session.commit()

        second_bot = SimpleNamespace(delete_message=AsyncMock(return_value=True))
        second = self._scheduler(second_bot)
        await second.start()

        await _wait_until(lambda: self._job_count_is(0))
        second_bot.delete_message.assert_awaited_once_with(
            chat_id=-100,
            message_id=10,
        )

    async def test_durable_enqueue_is_committed_before_send_path_returns(self) -> None:
        bot = SimpleNamespace(delete_message=AsyncMock(return_value=True))
        scheduler = self._scheduler(bot)
        await scheduler.start()
        configure_telegram_cleanup_scheduler(scheduler)
        sent = SimpleNamespace(
            chat=SimpleNamespace(id=-100),
            message_id=20,
        )

        self.assertTrue(await schedule_message_auto_delete_durable(sent, 3600))
        self.assertEqual(await self._job_count(), 1)

        # Simulate an abrupt process loss: no graceful intake flush is needed,
        # because the helper returned only after the row commit.
        worker = scheduler.worker_task
        assert worker is not None
        worker.cancel()
        await asyncio.gather(worker, return_exceptions=True)
        async with self.session_factory() as session:
            await session.execute(
                update(TelegramDeleteJob).values(
                    due_at=now_shanghai_naive() - timedelta(seconds=1)
                )
            )
            await session.commit()

        recovered_bot = SimpleNamespace(delete_message=AsyncMock(return_value=True))
        recovered = self._scheduler(recovered_bot)
        await recovered.start()
        await _wait_until(lambda: self._job_count_is(0))
        recovered_bot.delete_message.assert_awaited_once_with(
            chat_id=-100,
            message_id=20,
        )

    async def test_concurrent_durable_enqueue_deduplicates_and_keeps_earliest_due(self) -> None:
        scheduler = self._scheduler(
            SimpleNamespace(delete_message=AsyncMock(return_value=True))
        )
        await scheduler.start()
        base = now_shanghai_naive() + timedelta(hours=2)
        due_values = [base - timedelta(minutes=index) for index in range(12)]

        accepted = await asyncio.gather(
            *(
                scheduler.enqueue_durable(
                    chat_id=-100,
                    message_id=21,
                    due_at=due,
                )
                for due in due_values
            )
        )
        self.assertTrue(all(accepted))
        self.assertEqual(await self._job_count(), 1)
        async with self.session_factory() as session:
            due_at = await session.scalar(select(TelegramDeleteJob.due_at))
        self.assertEqual(due_at, min(due_values))

    async def test_durable_persistence_failure_marks_scheduler_unhealthy(self) -> None:
        scheduler = self._scheduler(
            SimpleNamespace(delete_message=AsyncMock(return_value=True))
        )
        await scheduler.start()

        class BrokenFactory:
            def __call__(self):
                return self

            async def __aenter__(self):
                raise RuntimeError("database unavailable")

            async def __aexit__(self, *_args):
                return False

        scheduler.session_factory = BrokenFactory()
        self.assertFalse(
            await scheduler.enqueue_durable(
                chat_id=-100,
                message_id=22,
                due_at=now_shanghai_naive() + timedelta(minutes=1),
            )
        )
        self.assertIsNotNone(scheduler.failure)
        self.assertTrue(scheduler._fatal_event.is_set())

    async def _job_count_is(self, expected: int) -> bool:
        return await self._job_count() == expected

    async def test_transient_failure_retries_then_removes_row(self) -> None:
        bot = SimpleNamespace(
            delete_message=AsyncMock(
                side_effect=[RuntimeError("temporary"), True]
            )
        )
        scheduler = self._scheduler(bot)
        await scheduler.start()
        scheduler.enqueue(
            chat_id=-100,
            message_id=11,
            due_at=now_shanghai_naive() - timedelta(seconds=1),
        )

        async def retried_and_removed() -> bool:
            return bot.delete_message.await_count >= 2 and await self._job_count_is(0)

        await _wait_until(retried_and_removed)
        self.assertEqual(bot.delete_message.await_count, 2)

    async def test_retry_limit_discards_poison_job(self) -> None:
        bot = SimpleNamespace(
            delete_message=AsyncMock(side_effect=RuntimeError("still broken"))
        )
        scheduler = self._scheduler(bot, max_attempts=2)
        await scheduler.start()
        scheduler.enqueue(
            chat_id=-100,
            message_id=12,
            due_at=now_shanghai_naive() - timedelta(seconds=1),
        )

        async def exhausted_and_removed() -> bool:
            return bot.delete_message.await_count >= 2 and await self._job_count_is(0)

        await _wait_until(exhausted_and_removed)
        self.assertEqual(bot.delete_message.await_count, 2)

    async def test_permanent_telegram_error_is_not_retried(self) -> None:
        failure = TelegramBadRequest(
            method=DeleteMessage(chat_id=-100, message_id=13),
            message="message to delete not found",
        )
        bot = SimpleNamespace(delete_message=AsyncMock(side_effect=failure))
        scheduler = self._scheduler(bot)
        await scheduler.start()
        scheduler.enqueue(
            chat_id=-100,
            message_id=13,
            due_at=now_shanghai_naive() - timedelta(seconds=1),
        )

        async def failed_and_removed() -> bool:
            return bot.delete_message.await_count >= 1 and await self._job_count_is(0)

        await _wait_until(failed_and_removed)
        bot.delete_message.assert_awaited_once()

    async def test_cancel_resistant_delete_is_not_replayed_concurrently(self) -> None:
        release = asyncio.Event()
        calls = 0

        async def stubborn_delete(**_kwargs) -> bool:
            nonlocal calls
            calls += 1
            try:
                await asyncio.Future()
            except asyncio.CancelledError:
                await release.wait()
                return True

        bot = SimpleNamespace(delete_message=AsyncMock(side_effect=stubborn_delete))
        scheduler = self._scheduler(bot)
        await scheduler.start()
        scheduler.enqueue(
            chat_id=-100,
            message_id=14,
            due_at=now_shanghai_naive() - timedelta(seconds=1),
        )

        async def orphan_registered() -> bool:
            return scheduler.orphan_count == 1

        await _wait_until(orphan_registered)
        # Wait past the deliberately short DB lease.  The in-process orphan
        # registry must still prevent a second Telegram side effect.
        await asyncio.sleep(0.25)
        self.assertEqual(calls, 1)
        self.assertEqual(await self._job_count(), 1)

        release.set()
        await _wait_until(lambda: self._job_count_is(0))
        self.assertEqual(calls, 1)

    async def test_stop_tracks_cancellation_resistant_inflight_delete(self) -> None:
        started = asyncio.Event()
        release = asyncio.Event()
        calls = 0

        async def stubborn_delete(**_kwargs) -> bool:
            nonlocal calls
            calls += 1
            started.set()
            while not release.is_set():
                try:
                    await release.wait()
                except asyncio.CancelledError:
                    continue
            return True

        bot = SimpleNamespace(delete_message=AsyncMock(side_effect=stubborn_delete))
        scheduler = self._scheduler(
            bot,
            delete_timeout_seconds=5.0,
            cancel_grace_seconds=0.01,
            lease_seconds=6.0,
        )
        await scheduler.start()
        self.assertTrue(
            await scheduler.enqueue_durable(
                chat_id=-100,
                message_id=16,
                due_at=now_shanghai_naive() - timedelta(seconds=1),
            )
        )
        await asyncio.wait_for(started.wait(), timeout=1.0)

        loop = asyncio.get_running_loop()
        started_at = loop.time()
        await asyncio.wait_for(
            scheduler.stop(timeout_seconds=0.02),
            timeout=0.5,
        )
        self.assertLess(loop.time() - started_at, 0.5)
        self.assertEqual(scheduler.orphan_count, 1)
        self.assertEqual(await self._job_count(), 1)
        self.assertEqual(calls, 1)

        release.set()
        await _wait_until(lambda: self._job_count_is(0))

        async def orphan_finalized() -> bool:
            return scheduler.orphan_count == 0

        await _wait_until(orphan_finalized)
        self.assertEqual(calls, 1)

    async def test_stale_orphan_cannot_complete_new_workers_lease(self) -> None:
        old_release = asyncio.Event()

        async def old_delete(**_kwargs) -> bool:
            try:
                await asyncio.Future()
            except asyncio.CancelledError:
                await old_release.wait()
                return True

        first = self._scheduler(
            SimpleNamespace(delete_message=AsyncMock(side_effect=old_delete))
        )
        await first.start()
        first.enqueue(
            chat_id=-100,
            message_id=15,
            due_at=now_shanghai_naive() - timedelta(seconds=1),
        )

        async def first_orphaned() -> bool:
            return first.orphan_count == 1

        await _wait_until(first_orphaned)
        await asyncio.sleep(0.2)

        new_started = asyncio.Event()
        new_release = asyncio.Event()

        async def new_delete(**_kwargs) -> bool:
            new_started.set()
            await new_release.wait()
            return True

        second = self._scheduler(
            SimpleNamespace(delete_message=AsyncMock(side_effect=new_delete)),
            delete_timeout_seconds=1.0,
            lease_seconds=2.0,
        )
        await second.start()
        await asyncio.wait_for(new_started.wait(), timeout=1.0)
        async with self.session_factory() as session:
            new_lease = await session.scalar(
                select(TelegramDeleteJob.lease_until).where(
                    TelegramDeleteJob.message_id == 15
                )
            )
        self.assertIsNotNone(new_lease)

        old_release.set()

        async def old_finalized() -> bool:
            return first.orphan_count == 0

        await _wait_until(old_finalized)
        async with self.session_factory() as session:
            row = await session.scalar(
                select(TelegramDeleteJob).where(TelegramDeleteJob.message_id == 15)
            )
            self.assertIsNotNone(row)
            self.assertEqual(row.lease_until, new_lease)
            self.assertEqual(row.attempts, 2)

        new_release.set()
        await _wait_until(lambda: self._job_count_is(0))


if __name__ == "__main__":
    unittest.main()
