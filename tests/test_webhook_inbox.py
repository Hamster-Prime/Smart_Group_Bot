from __future__ import annotations

import asyncio
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from aiogram import Bot, Dispatcher
from sqlalchemy import select, update

from bot.db.engine import init_db
from bot.db.models import WebhookInboxUpdate
from bot.services.verify_web import (
    VerifyWebServer,
    _QueuedWebhookUpdate,
    _WebhookUpdateQueue,
)
from bot.services.update_completion import current_update_completion
from bot.utils.timezone import now_shanghai_naive


WEBHOOK_SECRET = "s" * 32


def _request(payload: dict) -> SimpleNamespace:
    return SimpleNamespace(
        headers={"X-Telegram-Bot-Api-Secret-Token": WEBHOOK_SECRET},
        json=AsyncMock(return_value=payload),
    )


class WebhookInboxPersistenceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        db_path = Path(self.tempdir.name) / "webhook.db"
        self.engine, self.session_factory = await init_db(
            f"sqlite+aiosqlite:///{db_path}"
        )
        self.bot = Bot(token="42:TEST_TOKEN")

    async def asyncTearDown(self) -> None:
        await self.bot.session.close()
        await self.engine.dispose()
        self.tempdir.cleanup()

    def _queue(self) -> _WebhookUpdateQueue:
        return _WebhookUpdateQueue(
            dispatcher=Dispatcher(),
            bot=self.bot,
            session_factory=self.session_factory,
            secret_token=WEBHOOK_SECRET,
            worker_count=1,
        )

    async def _row(self, update_id: int) -> WebhookInboxUpdate | None:
        async with self.session_factory() as session:
            return await session.get(WebhookInboxUpdate, update_id)

    async def _wait_until(self, predicate, *, timeout: float = 1.0) -> None:
        deadline = asyncio.get_running_loop().time() + timeout
        while not predicate():
            if asyncio.get_running_loop().time() >= deadline:
                self.fail("condition was not reached before timeout")
            await asyncio.sleep(0.005)

    async def test_accept_claim_complete_is_durable_and_deduplicated(self) -> None:
        queue = self._queue()
        payload = {"update_id": 1001, "message": {"text": "hello"}}

        self.assertFalse(await queue._ensure_durable_update(1001, payload))
        lease = await queue._claim_durable_update(1001)
        self.assertIsNotNone(lease)
        assert lease is not None
        queued = _QueuedWebhookUpdate(
            update=payload,
            update_id=1001,
            enqueued_at=0.0,
            result=asyncio.get_running_loop().create_future(),
            lease_until=lease,
        )
        self.assertTrue(await queue._complete_durable_update(queued))
        self.assertTrue(await queue._ensure_durable_update(1001, payload))
        self.assertIsNone(await queue._claim_durable_update(1001))

    async def test_overlapping_restart_waits_for_live_lease_then_reclaims(self) -> None:
        first = self._queue()
        payload = {"update_id": 1002, "message": {"text": "recover"}}
        await first._ensure_durable_update(1002, payload)
        original_lease = await first._claim_durable_update(1002)
        self.assertIsNotNone(original_lease)

        restarted = self._queue()
        await restarted._prepare_durable_recovery()
        self.assertIsNone(await restarted._claim_durable_update(1002))
        row = await self._row(1002)
        assert row is not None
        self.assertEqual(row.lease_until, original_lease)

        async with self.session_factory() as session:
            await session.execute(
                update(WebhookInboxUpdate)
                .where(WebhookInboxUpdate.update_id == 1002)
                .values(
                    lease_until=now_shanghai_naive() - timedelta(seconds=1)
                )
            )
            await session.commit()
        self.assertIsNotNone(await restarted._claim_durable_update(1002))

    async def test_durable_ack_precedes_a_real_slow_dispatch_prefix(self) -> None:
        started = asyncio.Event()
        release = asyncio.Event()

        async def slow_dispatch(**_kwargs: object) -> None:
            started.set()
            await release.wait()

        queue = self._queue()
        queue.dispatcher.feed_raw_update = AsyncMock(side_effect=slow_dispatch)
        response = await asyncio.wait_for(
            queue.handle_verified(
                _request({"update_id": 1100, "message": {"text": "slow"}})
            ),
            timeout=0.5,
        )
        self.assertEqual(response.status, 200)
        await asyncio.wait_for(started.wait(), timeout=0.5)
        self.assertFalse(release.is_set())

        release.set()
        await self._wait_until(
            lambda: queue.health_snapshot()["completed_updates"] == 1
        )
        row = await self._row(1100)
        self.assertIsNotNone(row)
        assert row is not None
        self.assertIsNotNone(row.completed_at)
        await queue.stop()

    async def test_durable_ack_does_not_wait_for_immediate_publish_claim(self) -> None:
        queue = self._queue()
        publish_started = asyncio.Event()
        release = asyncio.Event()

        async def slow_publish(_update_id: int) -> bool:
            publish_started.set()
            await release.wait()
            return True

        with patch.object(queue, "_enqueue_durable_update", side_effect=slow_publish):
            response = await asyncio.wait_for(
                queue.handle_verified(
                    _request({"update_id": 1110, "message": {"text": "persisted"}})
                ),
                timeout=0.5,
            )
            self.assertEqual(response.status, 200)
            await asyncio.wait_for(publish_started.wait(), timeout=0.5)
            self.assertFalse(release.is_set())
            release.set()

        await queue.stop()

    async def test_existing_lease_and_queue_pressure_still_ack_persisted_row(self) -> None:
        queue = self._queue()
        recovered = asyncio.Event()

        async def capture(*, update: dict, **_kwargs: object) -> None:
            if update.get("update_id") == 1102:
                recovered.set()

        queue.dispatcher.feed_raw_update = AsyncMock(side_effect=capture)
        payload = {"update_id": 1101, "message": {"text": "leased"}}
        await queue._ensure_durable_update(1101, payload)
        self.assertIsNotNone(await queue._claim_durable_update(1101))

        leased = await queue.handle_verified(_request(payload))
        self.assertEqual(leased.status, 200)

        with patch.object(queue.queue, "full", return_value=True):
            full = await queue.handle_verified(
                _request({"update_id": 1102, "message": {"text": "full"}})
            )
        self.assertEqual(full.status, 200)
        self.assertIsNotNone(await self._row(1102))
        await queue._recover_durable_once()
        await asyncio.wait_for(recovered.wait(), timeout=0.5)
        await queue.stop()

    async def test_concurrent_duplicate_deliveries_ack_and_dispatch_once(self) -> None:
        started = asyncio.Event()
        release = asyncio.Event()

        async def slow(**_kwargs: object) -> None:
            started.set()
            await release.wait()

        queue = self._queue()
        queue.dispatcher.feed_raw_update = AsyncMock(side_effect=slow)
        payload = {"update_id": 1108, "message": {"text": "same"}}
        first, second = await asyncio.gather(
            queue.handle_verified(_request(payload)),
            queue.handle_verified(_request(payload)),
        )
        self.assertEqual((first.status, second.status), (200, 200))
        await asyncio.wait_for(started.wait(), timeout=0.5)
        self.assertEqual(queue.dispatcher.feed_raw_update.await_count, 1)
        release.set()
        await queue.stop()

    async def test_deferred_failure_is_not_reported_as_completed(self) -> None:
        receipt_holder: list[object] = []
        deferred = asyncio.Event()

        async def dispatch(**_kwargs: object) -> None:
            receipt = current_update_completion()
            assert receipt is not None
            receipt.defer()
            receipt_holder.append(receipt)
            deferred.set()

        queue = self._queue()
        queue.dispatcher.feed_raw_update = AsyncMock(side_effect=dispatch)
        response = await queue.handle_verified(
            _request({"update_id": 1109, "message": {"text": "deferred"}})
        )
        self.assertEqual(response.status, 200)
        await asyncio.wait_for(deferred.wait(), timeout=0.5)
        receipt_holder[0].finish(False)  # type: ignore[attr-defined]
        await self._wait_until(
            lambda: (
                queue.health_snapshot()["deferred_failed_updates"] == 1
                and queue.health_snapshot()["retry_scheduled_updates"] == 1
            )
        )
        snapshot = queue.health_snapshot()
        self.assertEqual(snapshot["completed_updates"], 0)
        self.assertEqual(snapshot["retry_scheduled_updates"], 1)
        row = await self._row(1109)
        assert row is not None
        self.assertIsNone(row.completed_at)
        self.assertIsNotNone(row.next_attempt_at)
        await queue.stop()

    async def test_persisted_payload_is_canonical_for_duplicate_update_id(self) -> None:
        seen: list[dict] = []
        dispatched = asyncio.Event()

        async def capture(*, update: dict, **_kwargs: object) -> None:
            seen.append(update)
            dispatched.set()

        queue = self._queue()
        queue.dispatcher.feed_raw_update = AsyncMock(side_effect=capture)
        await queue._ensure_durable_update(
            1103,
            {"update_id": 1103, "message": {"text": "canonical"}},
        )
        response = await queue.handle_verified(
            _request({"update_id": 1103, "message": {"text": "replacement"}})
        )
        self.assertEqual(response.status, 200)
        await asyncio.wait_for(dispatched.wait(), timeout=0.5)
        self.assertEqual(seen[0]["message"]["text"], "canonical")
        await queue.stop()

    async def test_cancellation_resistant_orphan_is_not_replayed_concurrently(self) -> None:
        started = asyncio.Event()
        cancellation_seen = asyncio.Event()
        release = asyncio.Event()
        active = 0
        max_active = 0
        calls = 0

        async def cancellation_resistant(**_kwargs: object) -> None:
            nonlocal active, max_active, calls
            calls += 1
            active += 1
            max_active = max(max_active, active)
            started.set()
            try:
                await asyncio.Future()
            except asyncio.CancelledError:
                cancellation_seen.set()
                await release.wait()
            finally:
                active -= 1

        queue = self._queue()
        queue.dispatcher.feed_raw_update = AsyncMock(
            side_effect=cancellation_resistant
        )
        with (
            patch("bot.services.verify_web._WEBHOOK_UPDATE_TIMEOUT_SECONDS", 0.02),
            patch("bot.services.verify_web._WEBHOOK_UPDATE_CANCEL_GRACE_SECONDS", 0.01),
            patch("bot.services.verify_web._WEBHOOK_INBOX_LEASE_SECONDS", 0.06),
        ):
            response = await queue.handle_verified(
                _request({"update_id": 1104, "message": {"text": "orphan"}})
            )
            self.assertEqual(response.status, 200)
            await asyncio.wait_for(started.wait(), timeout=0.5)
            await asyncio.wait_for(cancellation_seen.wait(), timeout=0.5)
            # Wait beyond the original lease and repeatedly ask recovery to run.
            # The late finalizer renews the lease and the in-memory barrier also
            # keeps this process from dispatching a second copy.
            await asyncio.sleep(0.12)
            for _ in range(3):
                self.assertEqual(await queue._recover_durable_once(), 0)
            self.assertEqual(calls, 1)
            self.assertEqual(max_active, 1)

            release.set()
            await self._wait_until(
                lambda: not queue.health_snapshot()["late_finalizers"]
            )

        row = await self._row(1104)
        self.assertIsNotNone(row)
        assert row is not None
        self.assertIsNotNone(row.completed_at)
        self.assertEqual(calls, 1)
        await queue.stop()

    async def test_handler_finishing_during_cancel_grace_is_not_replayed(self) -> None:
        cancellation_seen = asyncio.Event()
        calls = 0

        async def finishes_after_cancel(**_kwargs: object) -> None:
            nonlocal calls
            calls += 1
            try:
                await asyncio.Future()
            except asyncio.CancelledError:
                cancellation_seen.set()
                return

        queue = self._queue()
        queue.dispatcher.feed_raw_update = AsyncMock(
            side_effect=finishes_after_cancel
        )
        with (
            patch("bot.services.verify_web._WEBHOOK_UPDATE_TIMEOUT_SECONDS", 0.01),
            patch("bot.services.verify_web._WEBHOOK_UPDATE_CANCEL_GRACE_SECONDS", 0.1),
            patch("bot.services.verify_web._WEBHOOK_INBOX_LEASE_SECONDS", 0.05),
        ):
            response = await queue.handle_verified(
                _request({"update_id": 1111, "message": {"text": "grace"}})
            )
            self.assertEqual(response.status, 200)
            await asyncio.wait_for(cancellation_seen.wait(), timeout=0.5)
            await self._wait_until(
                lambda: queue.health_snapshot()["completed_updates"] == 1
            )
            await asyncio.sleep(0.08)
            self.assertEqual(await queue._recover_durable_once(), 0)

        row = await self._row(1111)
        assert row is not None
        self.assertIsNotNone(row.completed_at)
        self.assertEqual(calls, 1)
        await queue.stop()

    async def test_poison_update_uses_backoff_then_dead_letters(self) -> None:
        queue = self._queue()
        payload = {"update_id": 1105, "message": {"text": "poison"}}
        await queue._ensure_durable_update(1105, payload)

        delays: list[float] = []
        with (
            patch("bot.services.verify_web._WEBHOOK_INBOX_MAX_ATTEMPTS", 3),
            patch("bot.services.verify_web._WEBHOOK_INBOX_RETRY_BASE_SECONDS", 0.05),
            patch("bot.services.verify_web._WEBHOOK_INBOX_RETRY_MAX_SECONDS", 1.0),
        ):
            for attempt in range(1, 4):
                lease = await queue._claim_durable_update(1105)
                self.assertIsNotNone(lease)
                assert lease is not None
                queued = _QueuedWebhookUpdate(
                    update=payload,
                    update_id=1105,
                    enqueued_at=0.0,
                    result=asyncio.get_running_loop().create_future(),
                    lease_until=lease,
                )
                status = await queue._release_durable_update(
                    queued,
                    error=f"failure {attempt}",
                )
                row = await self._row(1105)
                assert row is not None
                if attempt < 3:
                    self.assertEqual(status, "retry")
                    self.assertIsNotNone(row.next_attempt_at)
                    assert row.next_attempt_at is not None
                    delays.append(
                        (row.next_attempt_at - row.updated_at).total_seconds()
                    )
                    self.assertIsNone(await queue._claim_durable_update(1105))
                    async with self.session_factory() as session:
                        await session.execute(
                            update(WebhookInboxUpdate)
                            .where(WebhookInboxUpdate.update_id == 1105)
                            .values(
                                next_attempt_at=now_shanghai_naive()
                                - timedelta(seconds=1)
                            )
                        )
                        await session.commit()
                else:
                    self.assertEqual(status, "dead")
                    self.assertIsNotNone(row.dead_lettered_at)
                    self.assertIsNone(row.next_attempt_at)
                    self.assertIsNone(await queue._claim_durable_update(1105))

        self.assertGreaterEqual(delays[0], 0.045)
        self.assertGreaterEqual(delays[1], delays[0] * 1.8)
        snapshot = queue.health_snapshot()
        self.assertEqual(snapshot["dead_lettered_updates"], 1)
        self.assertEqual(snapshot["retry_scheduled_updates"], 2)
        self.assertTrue(snapshot["ok"])

    async def test_route_disable_keeps_durable_recovery_running(self) -> None:
        dispatched = asyncio.Event()
        dispatcher = Dispatcher()

        async def capture(**_kwargs: object) -> None:
            dispatched.set()

        dispatcher.feed_raw_update = AsyncMock(side_effect=capture)
        server = VerifyWebServer(
            bot=self.bot,
            settings=SimpleNamespace(),
            session_factory=self.session_factory,
            webhook_dispatcher=dispatcher,
            webhook_path="/telegram/webhook",
            webhook_secret=WEBHOOK_SECRET,
        )
        # build_app only reads settings fields when serving non-webhook routes.
        server.build_app()
        processor = server._webhook_processor
        assert processor is not None
        await processor.start_recovery()
        server.enable_webhook_route()
        await server.disable_webhook_route()
        self.assertFalse(processor._stopped)
        self.assertIsNotNone(processor._recovery_task)

        await processor._ensure_durable_update(
            1106,
            {"update_id": 1106, "message": {"text": "after switch"}},
        )
        await processor._recover_durable_once()
        await asyncio.wait_for(dispatched.wait(), timeout=0.5)
        await server._stop_webhook_processor()

    async def test_recovery_loop_periodically_prunes_completed_rows(self) -> None:
        queue = self._queue()
        with (
            patch("bot.services.verify_web._WEBHOOK_INBOX_RETENTION_SECONDS", 0.01),
            patch("bot.services.verify_web._WEBHOOK_INBOX_CLEANUP_INTERVAL_SECONDS", 0.01),
            patch("bot.services.verify_web._WEBHOOK_INBOX_RECOVERY_INTERVAL_SECONDS", 0.01),
        ):
            await queue.start_recovery()
            await queue._ensure_durable_update(1107, {"update_id": 1107})
            async with self.session_factory() as session:
                await session.execute(
                    update(WebhookInboxUpdate)
                    .where(WebhookInboxUpdate.update_id == 1107)
                    .values(
                        completed_at=now_shanghai_naive() - timedelta(seconds=1),
                        lease_until=None,
                    )
                )
                await session.commit()
            deadline = asyncio.get_running_loop().time() + 0.5
            while await self._row(1107) is not None:
                if asyncio.get_running_loop().time() >= deadline:
                    self.fail("periodic durable-inbox cleanup did not prune row")
                await asyncio.sleep(0.01)

        await queue.stop()


if __name__ == "__main__":
    unittest.main()
