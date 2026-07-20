from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from aiogram import Bot, Dispatcher
from aiogram.types import Update

from bot.config import Settings
from bot.db.engine import init_db
from bot.db.models import WebhookInboxUpdate
from bot.services import update_delivery
from bot.services.update_completion import current_update_completion
from bot.services.verify_web import VerifyWebServer, _WebhookUpdateQueue


WEBHOOK_SECRET = "s" * 32


class DurablePollingLoopTests(unittest.IsolatedAsyncioTestCase):
    async def test_polling_ingest_serializes_aiogram_update_for_shared_inbox(self) -> None:
        bot = Bot(token="42:TEST_TOKEN")
        server = VerifyWebServer(
            bot=bot,
            settings=Settings(_env_file=None),
            session_factory=lambda: None,  # type: ignore[arg-type]
            webhook_dispatcher=Dispatcher(),
            webhook_path="/telegram/webhook",
            webhook_secret=WEBHOOK_SECRET,
        )
        server.build_app()
        processor = server._webhook_processor
        assert processor is not None
        accept = AsyncMock(return_value=401)
        try:
            with patch.object(processor, "accept_durable_update", new=accept):
                accepted_id = await server.accept_polling_update(Update(update_id=401))
            self.assertEqual(accepted_id, 401)
            self.assertEqual(accept.await_args.args[0], {"update_id": 401})
        finally:
            await bot.session.close()

    async def test_offset_is_not_sent_until_update_is_durably_persisted(self) -> None:
        persist_started = asyncio.Event()
        allow_persist = asyncio.Event()
        next_poll_started = asyncio.Event()
        calls: list[dict[str, object]] = []

        async def get_updates(**kwargs: object) -> list[object]:
            calls.append(dict(kwargs))
            if len(calls) == 1:
                return [SimpleNamespace(update_id=501)]
            next_poll_started.set()
            await asyncio.Future()
            return []

        async def ingest(update: object) -> int:
            persist_started.set()
            await allow_persist.wait()
            return int(getattr(update, "update_id"))

        bot = SimpleNamespace(get_updates=get_updates)
        runner = asyncio.create_task(
            update_delivery._run_durable_polling(
                bot=bot,
                allowed_updates=["message", "callback_query"],
                durable_update_ingest=ingest,
            )
        )
        try:
            await asyncio.wait_for(persist_started.wait(), timeout=0.5)
            await asyncio.sleep(0)
            self.assertEqual(len(calls), 1)
            self.assertIsNone(calls[0]["offset"])

            allow_persist.set()
            await asyncio.wait_for(next_poll_started.wait(), timeout=0.5)
            self.assertEqual(calls[1]["offset"], 502)
            self.assertEqual(
                calls[1]["allowed_updates"],
                ["message", "callback_query"],
            )
        finally:
            runner.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await asyncio.wait_for(runner, timeout=0.5)

    async def test_privileged_item_is_persisted_first_without_skipping_ack_prefix(
        self,
    ) -> None:
        poll_calls: list[int | None] = []
        ingest_order: list[int] = []
        second_poll = asyncio.Event()

        async def get_updates(**kwargs: object) -> list[object]:
            poll_calls.append(kwargs.get("offset"))  # type: ignore[arg-type]
            if len(poll_calls) == 1:
                return [
                    SimpleNamespace(
                        update_id=510,
                        message=SimpleNamespace(text="ordinary"),
                    ),
                    SimpleNamespace(
                        update_id=511,
                        message=SimpleNamespace(text="/ban 42"),
                    ),
                ]
            second_poll.set()
            await asyncio.Future()
            return []

        async def ingest(update: object) -> int:
            update_id = int(getattr(update, "update_id"))
            ingest_order.append(update_id)
            return update_id

        runner = asyncio.create_task(
            update_delivery._run_durable_polling(
                bot=SimpleNamespace(get_updates=get_updates),
                allowed_updates=["message", "callback_query", "chat_member"],
                durable_update_ingest=ingest,
            )
        )
        try:
            await asyncio.wait_for(second_poll.wait(), timeout=0.5)
            self.assertEqual(ingest_order, [511, 510])
            # Offset advances only after original update 510 is durable, then
            # safely covers the already-durable critical update 511 as well.
            self.assertEqual(poll_calls, [None, 512])
        finally:
            runner.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await asyncio.wait_for(runner, timeout=0.5)

    async def test_prioritized_persistence_never_acks_across_an_ordinary_gap(
        self,
    ) -> None:
        poll_calls: list[int | None] = []
        ingest_order: list[int] = []
        second_poll = asyncio.Event()

        async def get_updates(**kwargs: object) -> list[object]:
            poll_calls.append(kwargs.get("offset"))  # type: ignore[arg-type]
            if len(poll_calls) == 1:
                return [
                    SimpleNamespace(
                        update_id=610,
                        message=SimpleNamespace(text="ordinary"),
                    ),
                    SimpleNamespace(
                        update_id=611,
                        message=SimpleNamespace(text="/unban 42"),
                    ),
                ]
            second_poll.set()
            await asyncio.Future()
            return []

        async def ingest(update: object) -> int:
            update_id = int(getattr(update, "update_id"))
            ingest_order.append(update_id)
            if update_id == 610:
                raise RuntimeError("ordinary persistence failed")
            return update_id

        runner = asyncio.create_task(
            update_delivery._run_durable_polling(
                bot=SimpleNamespace(get_updates=get_updates),
                allowed_updates=["message"],
                durable_update_ingest=ingest,
            )
        )
        try:
            with patch.object(
                update_delivery.Backoff,
                "asleep",
                new=AsyncMock(),
            ):
                await asyncio.wait_for(second_poll.wait(), timeout=0.5)
            self.assertEqual(ingest_order, [611, 610])
            self.assertEqual(poll_calls, [None, None])
        finally:
            runner.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await asyncio.wait_for(runner, timeout=0.5)

    async def test_persistence_failure_repolls_same_update_without_ack(self) -> None:
        calls: list[int | None] = []
        third_poll = asyncio.Event()
        ingest_calls = 0

        async def get_updates(**kwargs: object) -> list[object]:
            calls.append(kwargs.get("offset"))  # type: ignore[arg-type]
            if len(calls) <= 2:
                return [SimpleNamespace(update_id=601)]
            third_poll.set()
            await asyncio.Future()
            return []

        async def ingest(_update: object) -> int:
            nonlocal ingest_calls
            ingest_calls += 1
            if ingest_calls == 1:
                raise RuntimeError("database unavailable")
            return 601

        runner = asyncio.create_task(
            update_delivery._run_durable_polling(
                bot=SimpleNamespace(get_updates=get_updates),
                allowed_updates=["message"],
                durable_update_ingest=ingest,
            )
        )
        try:
            with patch.object(
                update_delivery.Backoff,
                "asleep",
                new=AsyncMock(),
            ):
                await asyncio.wait_for(third_poll.wait(), timeout=0.5)
            self.assertEqual(calls, [None, None, 602])
            self.assertEqual(ingest_calls, 2)
        finally:
            runner.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await asyncio.wait_for(runner, timeout=0.5)

    async def test_network_failure_retries_with_same_offset_and_allowed_updates(self) -> None:
        calls: list[dict[str, object]] = []
        third_poll = asyncio.Event()

        async def get_updates(**kwargs: object) -> list[object]:
            calls.append(dict(kwargs))
            if len(calls) == 1:
                raise ConnectionError("offline")
            if len(calls) == 2:
                return [SimpleNamespace(update_id=701)]
            third_poll.set()
            await asyncio.Future()
            return []

        runner = asyncio.create_task(
            update_delivery._run_durable_polling(
                bot=SimpleNamespace(get_updates=get_updates),
                allowed_updates=["message", "chat_member"],
                durable_update_ingest=AsyncMock(return_value=701),
            )
        )
        try:
            with patch.object(
                update_delivery.Backoff,
                "asleep",
                new=AsyncMock(),
            ):
                await asyncio.wait_for(third_poll.wait(), timeout=0.5)
            self.assertEqual(
                [call["offset"] for call in calls],
                [None, None, 702],
            )
            self.assertTrue(
                all(
                    call["allowed_updates"] == ["message", "chat_member"]
                    for call in calls
                )
            )
        finally:
            runner.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await asyncio.wait_for(runner, timeout=0.5)

    async def test_cancel_resistant_long_poll_has_bounded_shutdown(self) -> None:
        started = asyncio.Event()
        cancellation_seen = asyncio.Event()
        release = asyncio.Event()

        async def get_updates(**_kwargs: object) -> list[object]:
            started.set()
            try:
                await asyncio.Future()
            except asyncio.CancelledError:
                cancellation_seen.set()
                await release.wait()
            return []

        runner = asyncio.create_task(
            update_delivery._run_durable_polling(
                bot=SimpleNamespace(get_updates=get_updates),
                allowed_updates=["message"],
                durable_update_ingest=AsyncMock(),
            )
        )
        await asyncio.wait_for(started.wait(), timeout=0.5)
        try:
            with patch.object(
                update_delivery,
                "_CONTROL_CANCELLATION_GRACE_SECONDS",
                0.01,
            ):
                runner.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await asyncio.wait_for(runner, timeout=0.2)
            await asyncio.wait_for(cancellation_seen.wait(), timeout=0.2)
            self.assertTrue(update_delivery._CONTROL_ORPHAN_TASKS)
        finally:
            release.set()
            deadline = asyncio.get_running_loop().time() + 0.5
            while update_delivery._CONTROL_ORPHAN_TASKS:
                if asyncio.get_running_loop().time() >= deadline:
                    self.fail("cancel-resistant getUpdates task did not finish")
                await asyncio.sleep(0.005)

    async def test_sigterm_waiter_stops_direct_durable_polling_and_runs_lifecycle_cleanup(
        self,
    ) -> None:
        events: list[str] = []
        signal_release = asyncio.Event()
        signal_waiting = asyncio.Event()
        poll_started = asyncio.Event()
        poll_cancelled = asyncio.Event()

        async def wait_for_signal() -> None:
            signal_waiting.set()
            await signal_release.wait()

        async def get_updates(**_kwargs: object) -> list[object]:
            events.append("poll")
            poll_started.set()
            try:
                await asyncio.Future()
            except asyncio.CancelledError:
                events.append("poll-cancelled")
                poll_cancelled.set()
                raise

        dispatcher = SimpleNamespace(
            resolve_used_update_types=Mock(return_value=["message"]),
            workflow_data={},
            emit_startup=AsyncMock(
                side_effect=lambda **_kwargs: events.append("startup")
            ),
            emit_shutdown=AsyncMock(
                side_effect=lambda **_kwargs: events.append("shutdown")
            ),
            start_polling=AsyncMock(),
        )
        bot = SimpleNamespace(
            delete_webhook=AsyncMock(
                side_effect=lambda **_kwargs: events.append("delete") or True
            ),
            get_updates=get_updates,
        )

        async def start_consumer() -> None:
            events.append("consumer-start")

        async def stop_consumer() -> None:
            events.append("consumer-stop")

        signal_waiter = AsyncMock(side_effect=wait_for_signal)
        with patch.object(
            update_delivery,
            "_wait_for_shutdown_signal",
            new=signal_waiter,
        ):
            runner = asyncio.create_task(
                update_delivery.run_update_delivery(
                    bot=bot,
                    dispatcher=dispatcher,
                    settings=Settings(_env_file=None),
                    webhook=None,
                    fallback_reason="not configured",
                    start_update_processor=start_consumer,
                    stop_update_processor=stop_consumer,
                    durable_update_ingest=AsyncMock(),
                )
            )
            await asyncio.wait_for(signal_waiting.wait(), timeout=0.5)
            await asyncio.wait_for(poll_started.wait(), timeout=0.5)
            signal_release.set()
            mode = await asyncio.wait_for(runner, timeout=0.5)

        self.assertEqual(mode, "shutdown")
        await asyncio.wait_for(poll_cancelled.wait(), timeout=0.2)
        self.assertEqual(signal_waiter.await_count, 1)
        self.assertLess(events.index("poll-cancelled"), events.index("consumer-stop"))
        self.assertLess(events.index("consumer-stop"), events.index("shutdown"))
        dispatcher.start_polling.assert_not_awaited()

    async def test_webhook_fallback_and_polling_share_one_signal_waiter(self) -> None:
        events: list[str] = []
        signal_release = asyncio.Event()
        signal_waiting = asyncio.Event()
        polling_started = asyncio.Event()
        webhook_signal_tasks: list[asyncio.Task[None]] = []
        polling_signal_tasks: list[asyncio.Task[None]] = []

        async def wait_for_signal() -> None:
            signal_waiting.set()
            await signal_release.wait()

        async def webhook_exit(**kwargs: object) -> str:
            shared = kwargs.get("shutdown_signal_task")
            self.assertIsInstance(shared, asyncio.Task)
            webhook_signal_tasks.append(shared)  # type: ignore[arg-type]
            return "webhook delivery failed"

        async def durable_polling(**kwargs: object) -> None:
            shared = kwargs.get("shutdown_signal_task")
            self.assertIsInstance(shared, asyncio.Task)
            polling_signal_tasks.append(shared)  # type: ignore[arg-type]
            polling_started.set()
            await shared  # type: ignore[misc]

        dispatcher = SimpleNamespace(
            resolve_used_update_types=Mock(return_value=["message"]),
            workflow_data={},
            emit_startup=AsyncMock(
                side_effect=lambda **_kwargs: events.append("startup")
            ),
            emit_shutdown=AsyncMock(
                side_effect=lambda **_kwargs: events.append("shutdown")
            ),
            start_polling=AsyncMock(),
        )
        webhook = update_delivery.WebhookConfig(
            url="https://bot.example.com/telegram/webhook",
            path="/telegram/webhook",
            secret=WEBHOOK_SECRET,
        )
        bot = SimpleNamespace(
            delete_webhook=AsyncMock(return_value=True),
            set_webhook=AsyncMock(return_value=True),
            get_webhook_info=AsyncMock(
                return_value=SimpleNamespace(
                    url=webhook.url,
                    last_error_date=None,
                )
            ),
        )
        signal_waiter = AsyncMock(side_effect=wait_for_signal)

        with (
            patch.object(
                update_delivery,
                "_wait_for_shutdown_signal",
                new=signal_waiter,
            ),
            patch.object(
                update_delivery,
                "_probe_webhook_endpoint",
                new=AsyncMock(return_value=None),
            ),
            patch.object(
                update_delivery,
                "_wait_for_webhook_exit",
                new=AsyncMock(side_effect=webhook_exit),
            ),
            patch.object(
                update_delivery,
                "_run_durable_polling",
                new=AsyncMock(side_effect=durable_polling),
            ),
        ):
            runner = asyncio.create_task(
                update_delivery.run_update_delivery(
                    bot=bot,
                    dispatcher=dispatcher,
                    settings=Settings(_env_file=None),
                    webhook=webhook,
                    start_update_processor=lambda: events.append("consumer-start"),
                    stop_update_processor=lambda: events.append("consumer-stop"),
                    durable_update_ingest=AsyncMock(),
                )
            )
            await asyncio.wait_for(signal_waiting.wait(), timeout=0.5)
            await asyncio.wait_for(polling_started.wait(), timeout=0.5)
            signal_release.set()
            mode = await asyncio.wait_for(runner, timeout=0.5)

        self.assertIn(mode, {"polling", "shutdown"})
        self.assertEqual(signal_waiter.await_count, 1)
        self.assertEqual(len(webhook_signal_tasks), 1)
        self.assertEqual(len(polling_signal_tasks), 1)
        self.assertIs(webhook_signal_tasks[0], polling_signal_tasks[0])
        self.assertLess(events.index("consumer-stop"), events.index("shutdown"))
        dispatcher.start_polling.assert_not_awaited()

    async def test_polling_uses_one_dispatcher_lifecycle_and_stops_consumer_first(self) -> None:
        events: list[str] = []
        poll_started = asyncio.Event()

        async def get_updates(**_kwargs: object) -> list[object]:
            events.append("poll")
            poll_started.set()
            await asyncio.Future()
            return []

        dispatcher = SimpleNamespace(
            resolve_used_update_types=Mock(return_value=["message"]),
            workflow_data={},
            emit_startup=AsyncMock(side_effect=lambda **_kwargs: events.append("startup")),
            emit_shutdown=AsyncMock(
                side_effect=lambda **_kwargs: events.append("shutdown")
            ),
            start_polling=AsyncMock(),
        )
        bot = SimpleNamespace(
            delete_webhook=AsyncMock(
                side_effect=lambda **_kwargs: events.append("delete") or True
            ),
            get_updates=get_updates,
        )

        async def start_consumer() -> None:
            events.append("consumer-start")

        async def stop_consumer() -> None:
            events.append("consumer-stop")

        runner = asyncio.create_task(
            update_delivery.run_update_delivery(
                bot=bot,
                dispatcher=dispatcher,
                settings=Settings(_env_file=None),
                webhook=None,
                fallback_reason="not configured",
                mark_polling_active=lambda: events.append("active"),
                start_update_processor=start_consumer,
                stop_update_processor=stop_consumer,
                durable_update_ingest=AsyncMock(),
            )
        )
        await asyncio.wait_for(poll_started.wait(), timeout=0.5)
        runner.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await asyncio.wait_for(runner, timeout=0.5)

        self.assertEqual(
            events,
            [
                "startup",
                "consumer-start",
                "delete",
                "active",
                "poll",
                "consumer-stop",
                "shutdown",
            ],
        )
        dispatcher.start_polling.assert_not_awaited()

    async def test_webhook_fallback_keeps_one_lifecycle_and_live_inbox_consumer(
        self,
    ) -> None:
        events: list[str] = []
        poll_started = asyncio.Event()
        dispatcher = SimpleNamespace(
            resolve_used_update_types=Mock(return_value=["message"]),
            workflow_data={},
            emit_startup=AsyncMock(side_effect=lambda **_kwargs: events.append("startup")),
            emit_shutdown=AsyncMock(
                side_effect=lambda **_kwargs: events.append("shutdown")
            ),
            start_polling=AsyncMock(),
        )

        async def get_updates(**_kwargs: object) -> list[object]:
            events.append("poll")
            poll_started.set()
            await asyncio.Future()
            return []

        bot = SimpleNamespace(
            delete_webhook=AsyncMock(
                side_effect=lambda **_kwargs: events.append("clear") or True
            ),
            set_webhook=AsyncMock(
                side_effect=lambda **_kwargs: events.append("set") or True
            ),
            get_webhook_info=AsyncMock(
                side_effect=lambda: events.append("get")
                or SimpleNamespace(
                    url="https://bot.example.com/telegram/webhook",
                    last_error_date=None,
                )
            ),
            get_updates=get_updates,
        )
        webhook = update_delivery.WebhookConfig(
            url="https://bot.example.com/telegram/webhook",
            path="/telegram/webhook",
            secret=WEBHOOK_SECRET,
        )

        async def start_consumer() -> None:
            # Recovery may feed stored rows here, and startup must already be
            # complete before that becomes possible.
            self.assertEqual(events, ["startup"])
            events.append("consumer-start")

        async def stop_consumer() -> None:
            events.append("consumer-stop")

        with (
            patch(
                "bot.services.update_delivery._probe_webhook_endpoint",
                new=AsyncMock(return_value=None),
            ),
            patch(
                "bot.services.update_delivery._wait_for_webhook_exit",
                new=AsyncMock(return_value="webhook delivery failed"),
            ),
        ):
            runner = asyncio.create_task(
                update_delivery.run_update_delivery(
                    bot=bot,
                    dispatcher=dispatcher,
                    settings=Settings(_env_file=None),
                    webhook=webhook,
                    enable_webhook_route=lambda: events.append("route-enable"),
                    mark_webhook_active=lambda: events.append("webhook-active"),
                    disable_webhook_route=lambda: events.append("route-disable"),
                    mark_polling_active=lambda: events.append("polling-active"),
                    start_update_processor=start_consumer,
                    stop_update_processor=stop_consumer,
                    durable_update_ingest=AsyncMock(),
                )
            )
            await asyncio.wait_for(poll_started.wait(), timeout=0.5)
            self.assertEqual(dispatcher.emit_startup.await_count, 1)
            dispatcher.emit_shutdown.assert_not_awaited()
            self.assertNotIn("consumer-stop", events)

            runner.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await asyncio.wait_for(runner, timeout=0.5)

        self.assertEqual(dispatcher.emit_startup.await_count, 1)
        self.assertEqual(dispatcher.emit_shutdown.await_count, 1)
        self.assertLess(events.index("startup"), events.index("consumer-start"))
        self.assertLess(events.index("consumer-start"), events.index("route-enable"))
        self.assertLess(events.index("route-disable"), events.index("poll"))
        self.assertLess(events.index("poll"), events.index("consumer-stop"))
        self.assertLess(events.index("consumer-stop"), events.index("shutdown"))
        dispatcher.start_polling.assert_not_awaited()


class DurablePollingInboxTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        db_path = Path(self.tempdir.name) / "polling.db"
        self.engine, self.session_factory = await init_db(
            f"sqlite+aiosqlite:///{db_path}"
        )
        self.bot = Bot(token="42:TEST_TOKEN")

    async def asyncTearDown(self) -> None:
        await self.bot.session.close()
        await self.engine.dispose()
        self.tempdir.cleanup()

    async def _row(self, update_id: int) -> WebhookInboxUpdate | None:
        async with self.session_factory() as session:
            return await session.get(WebhookInboxUpdate, update_id)

    async def _wait_for_row_completed(self, update_id: int) -> WebhookInboxUpdate:
        deadline = asyncio.get_running_loop().time() + 1.0
        while True:
            row = await self._row(update_id)
            if row is not None and row.completed_at is not None:
                return row
            if asyncio.get_running_loop().time() >= deadline:
                self.fail("durable polling row did not reach terminal completion")
            await asyncio.sleep(0.01)

    def _queue(self, dispatcher: Dispatcher | None = None) -> _WebhookUpdateQueue:
        return _WebhookUpdateQueue(
            dispatcher=dispatcher or Dispatcher(),
            bot=self.bot,
            session_factory=self.session_factory,
            secret_token=WEBHOOK_SECRET,
            worker_count=1,
        )

    async def test_polling_and_webhook_duplicate_execute_once_and_receipt_completes_row(
        self,
    ) -> None:
        dispatcher = Dispatcher()
        dispatch_started = asyncio.Event()
        receipts: list[object] = []

        async def dispatch(**_kwargs: object) -> None:
            receipt = current_update_completion()
            assert receipt is not None
            receipt.defer()
            receipts.append(receipt)
            dispatch_started.set()

        dispatcher.feed_raw_update = AsyncMock(side_effect=dispatch)
        queue = self._queue(dispatcher)
        payload = {"update_id": 801, "message": {"text": "once"}}
        try:
            await queue.start_recovery()
            # First acceptance represents polling; the immediate duplicate
            # represents a webhook whose HTTP ACK was lost during fallback.
            self.assertEqual(await queue.accept_durable_update(payload), 801)
            self.assertEqual(await queue.accept_durable_update(payload), 801)
            await asyncio.wait_for(dispatch_started.wait(), timeout=0.5)
            await asyncio.sleep(0.05)
            self.assertEqual(dispatcher.feed_raw_update.await_count, 1)

            pending = await self._row(801)
            self.assertIsNotNone(pending)
            assert pending is not None
            self.assertIsNone(pending.completed_at)
            self.assertEqual(len(receipts), 1)

            receipts[0].finish(True)  # type: ignore[attr-defined]
            completed = await self._wait_for_row_completed(801)
            self.assertEqual(completed.payload, {})
            self.assertEqual(dispatcher.feed_raw_update.await_count, 1)
        finally:
            await queue.stop()

    async def test_recovery_consumes_row_accepted_before_polling_fallback(self) -> None:
        old_owner = self._queue()
        await old_owner._ensure_durable_update(
            802,
            {"update_id": 802, "message": {"text": "old webhook ACK"}},
        )

        dispatcher = Dispatcher()
        consumed = asyncio.Event()
        dispatcher.feed_raw_update = AsyncMock(
            side_effect=lambda **_kwargs: consumed.set()
        )
        restarted = self._queue(dispatcher)
        try:
            await restarted.start_recovery()
            await asyncio.wait_for(consumed.wait(), timeout=0.5)
            await self._wait_for_row_completed(802)
            dispatcher.feed_raw_update.assert_awaited_once()
        finally:
            await restarted.stop()


if __name__ == "__main__":
    unittest.main()
