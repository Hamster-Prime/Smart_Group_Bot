import asyncio
import time
import unittest
from contextlib import ExitStack
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from aiogram import Bot, Dispatcher

from bot.config import Settings
from bot.services import update_delivery
from bot.services import verify_web as verify_web_module
from bot.services.request_priority import (
    ExecutionPriority,
    current_execution_priority,
)
from bot.services.update_completion import current_update_completion
from bot.services.update_delivery import (
    resolve_webhook_config,
    run_update_delivery,
    telegram_update_is_auth_candidate,
    telegram_update_is_privileged,
    telegram_update_priority,
)
from bot.services.verify_web import VerifyWebServer, _WebhookUpdateQueue

WEBHOOK_SECRET = "s" * 32


def _settings(**values: object) -> Settings:
    defaults: dict[str, object] = {
        "bot_token": "42:TEST_TOKEN",
        "super_admin_id": 1,
        "config_master_key": "unit-test-master-key",
    }
    defaults.update(values)
    settings = Settings(_env_file=None, **defaults)
    settings.bot.token = settings.bot_token
    return settings


def _dispatcher() -> SimpleNamespace:
    return SimpleNamespace(
        resolve_used_update_types=Mock(return_value=["message", "callback_query"]),
        start_polling=AsyncMock(),
        emit_startup=AsyncMock(),
        emit_shutdown=AsyncMock(),
        workflow_data={},
    )


class _MiddlewareEndpoint:
    def outer_middleware(self, _middleware: object) -> None:
        return None

    def middleware(self, _middleware: object) -> None:
        return None


class _MainDispatcher(dict):
    def __init__(self) -> None:
        super().__init__()
        self.update = _MiddlewareEndpoint()
        self.message = _MiddlewareEndpoint()
        self.callback_query = _MiddlewareEndpoint()
        self.chat_member = _MiddlewareEndpoint()

    def include_router(self, _router: object) -> None:
        return None


class WebhookConfigTests(unittest.TestCase):
    def test_missing_webhook_url_uses_fallback_reason(self) -> None:
        # Explicitly override any operator WEBHOOK_URL inherited by the test
        # process; this case is about the unconfigured branch itself.
        webhook, reason = resolve_webhook_config(
            _settings(webhook_url="", webhook_secret="")
        )

        self.assertIsNone(webhook)
        self.assertEqual(reason, "未配置 WEBHOOK_URL")

    def test_invalid_webhook_settings_do_not_raise(self) -> None:
        cases = (
            (
                {"webhook_url": "http://bot.example.com/hook", "webhook_secret": "secret"},
                "必须使用 HTTPS",
            ),
            (
                {"webhook_url": "https://bot.example.com/hook", "webhook_secret": ""},
                "缺少 WEBHOOK_SECRET",
            ),
            (
                {"webhook_url": "https://bot.example.com/verify", "webhook_secret": "secret"},
                "内置 HTTP 路由冲突",
            ),
            (
                {"webhook_url": "https://bot.example.com:9443/hook", "webhook_secret": "secret"},
                "端口必须为",
            ),
            (
                {"webhook_url": "https://bot.example.com/hook", "webhook_secret": "bad secret"},
                "WEBHOOK_SECRET 需为",
            ),
            (
                {"webhook_url": "https://bot.example.com/bad%20path", "webhook_secret": "secret"},
                "路径仅允许",
            ),
            (
                {"webhook_url": "https://bot.example.com/a/../hook", "webhook_secret": "secret"},
                "路径仅允许",
            ),
        )

        for values, expected in cases:
            with self.subTest(values=values):
                webhook, reason = resolve_webhook_config(_settings(**values))
                self.assertIsNone(webhook)
                self.assertIn(expected, reason or "")

    def test_valid_webhook_uses_url_path(self) -> None:
        webhook, reason = resolve_webhook_config(
            _settings(
                webhook_url="https://BOT.example.com/telegram/webhook",
                webhook_secret=WEBHOOK_SECRET,
            )
        )

        self.assertIsNone(reason)
        self.assertIsNotNone(webhook)
        assert webhook is not None
        self.assertEqual(webhook.path, "/telegram/webhook")
        self.assertEqual(webhook.url, "https://bot.example.com/telegram/webhook")


class UpdatePriorityClassificationTests(unittest.TestCase):
    def test_security_commands_are_privileged_without_aiogram_parsing(self) -> None:
        privileged = (
            "/ban 42 spam",
            "/UNBAN@SmartGroupBot 42",
            "/authadmin -100 42",
            "/raidguard on",
            "/mute 42",
            "/settings",
        )
        for index, text in enumerate(privileged, start=1):
            with self.subTest(text=text):
                self.assertTrue(
                    telegram_update_is_privileged(
                        {"update_id": index, "message": {"text": text}}
                    )
                )
                self.assertEqual(
                    telegram_update_priority(
                        {"update_id": index, "message": {"text": text}}
                    ),
                    ExecutionPriority.NORMAL,
                )
                self.assertTrue(
                    telegram_update_is_auth_candidate(
                        {"update_id": index, "message": {"text": text}}
                    )
                )

        self.assertFalse(
            telegram_update_is_privileged(
                {"update_id": 99, "message": {"text": "ordinary chat"}}
            )
        )
        self.assertFalse(
            telegram_update_is_privileged(
                {"update_id": 100, "message": {"text": "/av TEST-001"}}
            )
        )
        self.assertEqual(
            telegram_update_priority(
                {"update_id": 102, "message": {"text": "/addrule semantic no spam"}}
            ),
            ExecutionPriority.NORMAL,
        )
        self.assertTrue(
            telegram_update_is_auth_candidate(
                {"update_id": 102, "message": {"text": "/addrule semantic no spam"}}
            )
        )
        self.assertEqual(
            telegram_update_priority(
                SimpleNamespace(
                    update_id=101,
                    message=SimpleNamespace(text="/ban 42"),
                )
            ),
            ExecutionPriority.NORMAL,
        )

    def test_verification_membership_and_moderation_callbacks_are_prioritized(
        self,
    ) -> None:
        updates = (
            {"update_id": 1, "chat_member": {"chat": {"id": -100}}},
            {
                "update_id": 2,
                "message": {"new_chat_members": [{"id": 42}]},
            },
            {"update_id": 3, "callback_query": {"data": "bsc:u:g:42"}},
            {"update_id": 4, "callback_query": {"data": "mact:ban:7"}},
            {"update_id": 5, "callback_query": {"data": "jv:a:7"}},
            {"update_id": 6, "callback_query": {"data": "rgr"}},
            {
                "update_id": 7,
                "message": {"text": "/start verify_n100"},
            },
        )
        self.assertTrue(all(telegram_update_is_privileged(item) for item in updates))
        self.assertEqual(
            [telegram_update_priority(item) for item in updates],
            [
                ExecutionPriority.HIGH,
                ExecutionPriority.HIGH,
                ExecutionPriority.NORMAL,
                ExecutionPriority.NORMAL,
                ExecutionPriority.NORMAL,
                ExecutionPriority.NORMAL,
                ExecutionPriority.HIGH,
            ],
        )
        member_callbacks = (
            {"update_id": 8, "callback_query": {"data": "jv:v:7"}},
            {"update_id": 9, "callback_query": {"data": "vban:vote:1"}},
            {"update_id": 10, "callback_query": {"data": "ptv"}},
            {"update_id": 11, "callback_query": {"data": "rgv"}},
        )
        self.assertTrue(
            all(
                telegram_update_priority(item) == ExecutionPriority.HIGH
                for item in member_callbacks
            )
        )
        update_delivery.mark_privileged_operator(
            777,
            group_id=-100,
            ttl_seconds=60.0,
        )
        admin_callbacks = (
            {
                "update_id": 12,
                "callback_query": {
                    "data": "jv:r:7",
                    "from": {"id": 777},
                    "message": {"chat": {"id": -100}},
                },
            },
            {
                "update_id": 13,
                "callback_query": {
                    "data": "vban:ban:1",
                    "from": {"id": 777},
                    "message": {"chat": {"id": -100}},
                },
            },
            {
                "update_id": 14,
                "callback_query": {
                    "data": "vban:cancel:1",
                    "from": {"id": 777},
                    "message": {"chat": {"id": -100}},
                },
            },
        )
        self.assertTrue(
            all(
                telegram_update_priority(item) == ExecutionPriority.CRITICAL
                for item in admin_callbacks
            )
        )


class WebhookProbeTests(unittest.IsolatedAsyncioTestCase):
    async def test_probe_requires_the_webhook_routes_unique_unauthorized_response(self) -> None:
        webhook, _reason = resolve_webhook_config(
            _settings(
                webhook_url="https://bot.example.com/telegram/webhook",
                webhook_secret=WEBHOOK_SECRET,
            )
        )
        assert webhook is not None
        captured: dict[str, object] = {}

        class FakeResponse:
            status = 401

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                return None

            async def text(self) -> str:
                return "Unauthorized"

        class FakeSession:
            def __init__(self, **_kwargs):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                return None

            def post(self, url: str, **kwargs: object) -> FakeResponse:
                captured["url"] = url
                captured["headers"] = kwargs["headers"]
                return FakeResponse()

        with patch(
            "bot.services.update_delivery.aiohttp.ClientSession",
            FakeSession,
        ):
            issue = await update_delivery._probe_webhook_endpoint(webhook)

        self.assertIsNone(issue)
        self.assertEqual(captured["url"], webhook.url)
        headers = captured["headers"]
        assert isinstance(headers, dict)
        self.assertNotEqual(
            headers["X-Telegram-Bot-Api-Secret-Token"],
            WEBHOOK_SECRET,
        )

    async def test_probe_retries_one_transient_network_failure(self) -> None:
        webhook, _reason = resolve_webhook_config(
            _settings(
                webhook_url="https://bot.example.com/telegram/webhook",
                webhook_secret=WEBHOOK_SECRET,
            )
        )
        assert webhook is not None

        class FakeResponse:
            status = 401

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                return None

            async def text(self) -> str:
                return "Unauthorized"

        class FakeSession:
            attempts = 0

            def __init__(self, **_kwargs):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                return None

            def post(self, *_args, **_kwargs):
                type(self).attempts += 1
                if type(self).attempts == 1:
                    raise RuntimeError("temporary DNS error")
                return FakeResponse()

        with (
            patch("bot.services.update_delivery.aiohttp.ClientSession", FakeSession),
            patch(
                "bot.services.update_delivery.asyncio.sleep",
                new=AsyncMock(),
            ) as sleep_mock,
        ):
            issue = await update_delivery._probe_webhook_endpoint(webhook)

        self.assertIsNone(issue)
        self.assertEqual(FakeSession.attempts, 2)
        sleep_mock.assert_awaited_once_with(0.5)


class UpdateDeliveryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.probe_patcher = patch(
            "bot.services.update_delivery._probe_webhook_endpoint",
            new=AsyncMock(return_value=None),
        )
        self.probe_patcher.start()

    async def asyncTearDown(self) -> None:
        await update_delivery.flush_update_delivery_tasks(timeout_seconds=0.5)
        self.probe_patcher.stop()

    async def test_control_operation_timeout_is_hard_bounded(self) -> None:
        release = asyncio.Event()

        async def cancel_resistant_operation() -> None:
            try:
                await asyncio.Future()
            except asyncio.CancelledError:
                await release.wait()

        try:
            with self.assertRaisesRegex(
                update_delivery._OrphanedOperationTimeout,
                "test control",
            ):
                await asyncio.wait_for(
                    update_delivery._await_with_hard_timeout(
                        cancel_resistant_operation(),
                        timeout=0.01,
                        operation="test control",
                    ),
                    timeout=0.25,
                )
        finally:
            release.set()
            await asyncio.sleep(0)

    async def test_outer_cancellation_tracks_cancel_resistant_control_operation(self) -> None:
        release = asyncio.Event()
        started = asyncio.Event()

        async def cancel_resistant_operation() -> None:
            started.set()
            try:
                await asyncio.Future()
            except asyncio.CancelledError:
                await release.wait()

        operation = asyncio.create_task(
            update_delivery._await_with_hard_timeout(
                cancel_resistant_operation(),
                timeout=60.0,
                operation="cancelled control",
            )
        )
        await started.wait()
        operation.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await asyncio.wait_for(operation, timeout=0.25)

        self.assertEqual(len(update_delivery._CONTROL_ORPHAN_TASKS), 1)
        release.set()
        await update_delivery.flush_update_delivery_tasks(timeout_seconds=0.5)
        self.assertFalse(update_delivery._CONTROL_ORPHAN_TASKS)

    async def test_cancel_resistant_set_webhook_never_falls_back_to_polling(self) -> None:
        release = asyncio.Event()
        started = asyncio.Event()

        async def stuck_set_webhook(**_kwargs) -> bool:
            started.set()
            try:
                await asyncio.Future()
            except asyncio.CancelledError:
                await release.wait()
            return True

        settings = _settings(
            webhook_url="https://bot.example.com/telegram/webhook",
            webhook_secret=WEBHOOK_SECRET,
        )
        webhook, reason = resolve_webhook_config(settings)
        assert webhook is not None
        dispatcher = _dispatcher()
        bot = SimpleNamespace(
            delete_webhook=AsyncMock(return_value=True),
            set_webhook=AsyncMock(side_effect=stuck_set_webhook),
            get_webhook_info=AsyncMock(),
        )

        try:
            with (
                patch(
                    "bot.services.update_delivery._TELEGRAM_CONTROL_TIMEOUT_SECONDS",
                    0.01,
                ),
                patch(
                    "bot.services.update_delivery._CONTROL_CANCELLATION_GRACE_SECONDS",
                    0.01,
                ),
            ):
                with self.assertRaises(update_delivery._UnsafeDeliveryState):
                    await asyncio.wait_for(
                        run_update_delivery(
                            bot=bot,
                            dispatcher=dispatcher,
                            settings=settings,
                            webhook=webhook,
                            fallback_reason=reason,
                        ),
                        timeout=0.5,
                    )
            self.assertTrue(started.is_set())
            dispatcher.start_polling.assert_not_awaited()
        finally:
            release.set()
            await asyncio.sleep(0)

    async def test_missing_webhook_logs_reason_and_starts_polling(self) -> None:
        settings = _settings()
        settings.bot.drop_pending_updates = False
        dispatcher = _dispatcher()
        bot = SimpleNamespace(
            delete_webhook=AsyncMock(return_value=True),
        )

        with self.assertLogs("bot.services.update_delivery", level="INFO") as captured:
            mode = await run_update_delivery(
                bot=bot,
                dispatcher=dispatcher,
                settings=settings,
                webhook=None,
                fallback_reason="未配置 WEBHOOK_URL",
            )

        self.assertEqual(mode, "polling")
        bot.delete_webhook.assert_awaited_once_with(drop_pending_updates=False)
        dispatcher.start_polling.assert_awaited_once_with(
            bot,
            allowed_updates=["message", "callback_query"],
            handle_as_tasks=True,
            tasks_concurrency_limit=8,
            close_bot_session=False,
        )
        logs = "\n".join(captured.output)
        self.assertIn("未配置 WEBHOOK_URL", logs)
        self.assertIn("自动降级为轮询", logs)

    async def test_legacy_drop_pending_setting_is_ignored_on_cold_polling_start(self) -> None:
        settings = _settings(webhook_url="", webhook_secret="")
        settings.bot.drop_pending_updates = True
        dispatcher = _dispatcher()
        bot = SimpleNamespace(delete_webhook=AsyncMock(return_value=True))

        await run_update_delivery(
            bot=bot,
            dispatcher=dispatcher,
            settings=settings,
            webhook=None,
            fallback_reason="未配置 WEBHOOK_URL",
        )

        bot.delete_webhook.assert_awaited_once_with(drop_pending_updates=False)

    async def test_invalid_config_fallback_preserves_pending_updates(self) -> None:
        settings = _settings(
            webhook_url="http://invalid.example/hook",
            webhook_secret=WEBHOOK_SECRET,
        )
        settings.bot.drop_pending_updates = True
        webhook, reason = resolve_webhook_config(settings)
        self.assertIsNone(webhook)
        dispatcher = _dispatcher()
        bot = SimpleNamespace(delete_webhook=AsyncMock(return_value=True))

        mode = await run_update_delivery(
            bot=bot,
            dispatcher=dispatcher,
            settings=settings,
            webhook=webhook,
            fallback_reason=reason,
        )

        self.assertEqual(mode, "polling")
        bot.delete_webhook.assert_awaited_once_with(drop_pending_updates=False)

    async def test_polling_is_marked_active_only_after_webhook_deletion(self) -> None:
        events: list[str] = []
        settings = _settings(webhook_url="", webhook_secret="")
        dispatcher = _dispatcher()
        dispatcher.start_polling.side_effect = lambda *_args, **_kwargs: events.append(
            "poll"
        )
        bot = SimpleNamespace(
            delete_webhook=AsyncMock(
                side_effect=lambda **_kwargs: events.append("delete") or True
            )
        )

        await run_update_delivery(
            bot=bot,
            dispatcher=dispatcher,
            settings=settings,
            webhook=None,
            fallback_reason="未配置 WEBHOOK_URL",
            mark_polling_active=lambda: events.append("active"),
        )

        self.assertEqual(events, ["delete", "active", "poll"])

    async def test_cancelling_delivery_stops_polling_before_runner_exits(self) -> None:
        started = asyncio.Event()
        stop_requested = asyncio.Event()
        exited = asyncio.Event()

        async def start_polling(*_args, **_kwargs) -> None:
            started.set()
            try:
                await stop_requested.wait()
            finally:
                exited.set()

        async def stop_polling() -> None:
            stop_requested.set()
            await exited.wait()

        settings = _settings(webhook_url="", webhook_secret="")
        dispatcher = _dispatcher()
        dispatcher.start_polling = AsyncMock(side_effect=start_polling)
        dispatcher.stop_polling = AsyncMock(side_effect=stop_polling)
        dispatcher._handle_update_tasks = set()
        bot = SimpleNamespace(delete_webhook=AsyncMock(return_value=True))

        delivery = asyncio.create_task(
            run_update_delivery(
                bot=bot,
                dispatcher=dispatcher,
                settings=settings,
                webhook=None,
                fallback_reason="未配置 WEBHOOK_URL",
            )
        )
        await asyncio.wait_for(started.wait(), timeout=0.5)
        delivery.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await asyncio.wait_for(delivery, timeout=0.5)

        dispatcher.stop_polling.assert_awaited_once()
        self.assertTrue(exited.is_set())

    async def test_polling_not_started_race_cancels_runner_before_waiting(self) -> None:
        entered = asyncio.Event()
        runner_cancelled = asyncio.Event()

        async def start_polling(*_args, **_kwargs) -> None:
            entered.set()
            try:
                await asyncio.Future()
            finally:
                runner_cancelled.set()

        settings = _settings(webhook_url="", webhook_secret="")
        dispatcher = _dispatcher()
        dispatcher.start_polling = AsyncMock(side_effect=start_polling)
        dispatcher.stop_polling = AsyncMock(
            side_effect=RuntimeError("Polling is not started")
        )
        dispatcher._handle_update_tasks = set()
        bot = SimpleNamespace(delete_webhook=AsyncMock(return_value=True))

        delivery = asyncio.create_task(
            run_update_delivery(
                bot=bot,
                dispatcher=dispatcher,
                settings=settings,
                webhook=None,
                fallback_reason="未配置 WEBHOOK_URL",
            )
        )
        await asyncio.wait_for(entered.wait(), timeout=0.5)
        delivery.cancel()
        with (
            patch(
                "bot.services.update_delivery._POLLING_STOP_TIMEOUT_SECONDS",
                0.25,
            ),
            self.assertRaises(asyncio.CancelledError),
        ):
            await asyncio.wait_for(delivery, timeout=0.1)

        dispatcher.stop_polling.assert_awaited_once()
        self.assertTrue(runner_cancelled.is_set())

    async def test_polling_shutdown_bounds_cancel_resistant_update_tasks(self) -> None:
        release = asyncio.Event()
        started = asyncio.Event()

        async def stuck_update() -> None:
            started.set()
            try:
                await asyncio.Future()
            except asyncio.CancelledError:
                await release.wait()

        update_task = asyncio.create_task(stuck_update(), name="stuck-poll-update")
        await started.wait()
        settings = _settings(webhook_url="", webhook_secret="")
        dispatcher = _dispatcher()
        dispatcher._handle_update_tasks = {update_task}
        bot = SimpleNamespace(delete_webhook=AsyncMock(return_value=True))

        try:
            with (
                patch(
                    "bot.services.update_delivery._POLLING_UPDATE_DRAIN_TIMEOUT_SECONDS",
                    0.01,
                ),
                patch(
                    "bot.services.update_delivery._POLLING_UPDATE_CANCEL_GRACE_SECONDS",
                    0.01,
                ),
            ):
                mode = await asyncio.wait_for(
                    run_update_delivery(
                        bot=bot,
                        dispatcher=dispatcher,
                        settings=settings,
                        webhook=None,
                        fallback_reason="未配置 WEBHOOK_URL",
                    ),
                    timeout=0.5,
                )

            self.assertEqual(mode, "polling")
            self.assertFalse(update_task.done())
            self.assertIn(update_task, update_delivery._CONTROL_ORPHAN_TASKS)
        finally:
            release.set()
            await asyncio.wait_for(update_task, timeout=0.5)
            await asyncio.sleep(0)
            self.assertNotIn(update_task, update_delivery._CONTROL_ORPHAN_TASKS)

    async def test_webhook_lifecycle_tracks_cancel_resistant_task(self) -> None:
        release = asyncio.Event()
        watcher_started = asyncio.Event()

        async def wait_for_shutdown() -> None:
            await watcher_started.wait()

        async def stuck_watchdog(**_kwargs: object) -> str:
            watcher_started.set()
            try:
                await asyncio.Future()
            except asyncio.CancelledError:
                await release.wait()
            return "released"

        webhook = update_delivery.WebhookConfig(
            url="https://bot.example.com/telegram/webhook",
            path="/telegram/webhook",
            secret=WEBHOOK_SECRET,
        )
        try:
            with (
                patch(
                    "bot.services.update_delivery._wait_for_shutdown_signal",
                    new=wait_for_shutdown,
                ),
                patch(
                    "bot.services.update_delivery._watch_webhook",
                    new=stuck_watchdog,
                ),
                patch(
                    "bot.services.update_delivery._WEBHOOK_TASK_STOP_TIMEOUT_SECONDS",
                    0.01,
                ),
            ):
                result = await asyncio.wait_for(
                    update_delivery._wait_for_webhook_exit(
                        bot=SimpleNamespace(),
                        webhook=webhook,
                        initial_error_date=None,
                    ),
                    timeout=0.25,
                )

            self.assertIsNone(result)
            self.assertEqual(len(update_delivery._CONTROL_ORPHAN_TASKS), 1)
        finally:
            release.set()
            await update_delivery.flush_update_delivery_tasks(timeout_seconds=0.5)
            await asyncio.sleep(0)
        self.assertFalse(update_delivery._CONTROL_ORPHAN_TASKS)

    async def test_valid_webhook_registers_and_does_not_poll(self) -> None:
        settings = _settings(
            webhook_url="https://bot.example.com/telegram/webhook",
            webhook_secret=WEBHOOK_SECRET,
        )
        webhook, reason = resolve_webhook_config(settings)
        assert webhook is not None
        dispatcher = _dispatcher()
        bot = SimpleNamespace(
            set_webhook=AsyncMock(return_value=True),
            get_webhook_info=AsyncMock(
                return_value=SimpleNamespace(url=webhook.url, last_error_date=None)
            ),
            delete_webhook=AsyncMock(return_value=True),
        )

        with patch(
            "bot.services.update_delivery._wait_for_webhook_exit",
            new=AsyncMock(return_value=None),
        ):
            mode = await run_update_delivery(
                bot=bot,
                dispatcher=dispatcher,
                settings=settings,
                webhook=webhook,
                fallback_reason=reason,
            )

        self.assertEqual(mode, "webhook")
        bot.set_webhook.assert_awaited_once_with(
            url=webhook.url,
            allowed_updates=["message", "callback_query"],
            drop_pending_updates=False,
            secret_token=WEBHOOK_SECRET,
            max_connections=8,
        )
        dispatcher.emit_startup.assert_awaited_once()
        dispatcher.emit_shutdown.assert_awaited_once()
        bot.delete_webhook.assert_awaited_once_with(drop_pending_updates=False)
        dispatcher.start_polling.assert_not_awaited()

    async def test_registration_error_logs_and_falls_back(self) -> None:
        settings = _settings(
            webhook_url="https://bot.example.com/telegram/webhook",
            webhook_secret=WEBHOOK_SECRET,
        )
        webhook, reason = resolve_webhook_config(settings)
        assert webhook is not None
        dispatcher = _dispatcher()
        bot = SimpleNamespace(
            set_webhook=AsyncMock(side_effect=RuntimeError("bad certificate")),
            get_webhook_info=AsyncMock(),
            delete_webhook=AsyncMock(return_value=True),
        )

        with self.assertLogs("bot.services.update_delivery", level="INFO") as captured:
            mode = await run_update_delivery(
                bot=bot,
                dispatcher=dispatcher,
                settings=settings,
                webhook=webhook,
                fallback_reason=reason,
            )

        self.assertEqual(mode, "polling")
        self.assertEqual(
            [call.kwargs["drop_pending_updates"] for call in bot.delete_webhook.await_args_list],
            [False, False],
        )
        dispatcher.start_polling.assert_awaited_once()
        logs = "\n".join(captured.output)
        self.assertIn("bad certificate", logs)
        self.assertIn("自动降级为轮询", logs)

    async def test_webhook_runtime_delivery_error_falls_back(self) -> None:
        settings = _settings(
            webhook_url="https://bot.example.com/telegram/webhook",
            webhook_secret=WEBHOOK_SECRET,
        )
        webhook, reason = resolve_webhook_config(settings)
        assert webhook is not None
        dispatcher = _dispatcher()
        bot = SimpleNamespace(
            set_webhook=AsyncMock(return_value=True),
            get_webhook_info=AsyncMock(
                return_value=SimpleNamespace(url=webhook.url, last_error_date=None)
            ),
            delete_webhook=AsyncMock(return_value=True),
        )

        with patch(
            "bot.services.update_delivery._wait_for_webhook_exit",
            new=AsyncMock(return_value="Telegram webhook 投递失败：404 Not Found"),
        ):
            mode = await run_update_delivery(
                bot=bot,
                dispatcher=dispatcher,
                settings=settings,
                webhook=webhook,
                fallback_reason=reason,
            )

        self.assertEqual(mode, "polling")
        self.assertEqual(
            [call.kwargs["drop_pending_updates"] for call in bot.delete_webhook.await_args_list],
            [False, False],
        )
        dispatcher.start_polling.assert_awaited_once()

    async def test_delete_webhook_retries_until_polling_is_possible(self) -> None:
        settings = _settings()
        dispatcher = _dispatcher()
        bot = SimpleNamespace(
            delete_webhook=AsyncMock(
                side_effect=[False, RuntimeError("temporary network error"), True]
            ),
        )

        with patch(
            "bot.services.update_delivery.asyncio.sleep",
            new=AsyncMock(),
        ) as sleep_mock:
            mode = await run_update_delivery(
                bot=bot,
                dispatcher=dispatcher,
                settings=settings,
                webhook=None,
                fallback_reason="未配置 WEBHOOK_URL",
            )

        self.assertEqual(mode, "polling")
        self.assertEqual(bot.delete_webhook.await_count, 3)
        self.assertEqual(
            [call.args[0] for call in sleep_mock.await_args_list],
            [1.0, 2.0],
        )
        dispatcher.start_polling.assert_awaited_once()

    async def test_delete_webhook_retry_budget_is_bounded(self) -> None:
        bot = SimpleNamespace(
            delete_webhook=AsyncMock(side_effect=RuntimeError("offline")),
        )

        with patch(
            "bot.services.update_delivery.asyncio.sleep",
            new=AsyncMock(),
        ):
            with self.assertRaisesRegex(RuntimeError, "重试上限"):
                await update_delivery._delete_webhook_for_polling(
                    bot=bot,
                    drop_pending_updates=False,
                )

        self.assertEqual(
            bot.delete_webhook.await_count,
            update_delivery._DELETE_WEBHOOK_MAX_ATTEMPTS,
        )

    async def test_watchdog_reports_new_telegram_delivery_error(self) -> None:
        settings = _settings(
            webhook_url="https://bot.example.com/telegram/webhook",
            webhook_secret=WEBHOOK_SECRET,
        )
        webhook, _reason = resolve_webhook_config(settings)
        assert webhook is not None
        bot = SimpleNamespace(
            get_webhook_info=AsyncMock(
                side_effect=[
                    SimpleNamespace(
                        url=webhook.url,
                        last_error_date=timestamp,
                        last_error_message="Wrong response: 404 Not Found",
                    )
                    for timestamp in (123, 124, 125)
                ]
            )
        )

        with patch(
            "bot.services.update_delivery.asyncio.sleep",
            new=AsyncMock(),
        ):
            reason = await update_delivery._watch_webhook(
                bot=bot,
                webhook=webhook,
                initial_error_date=None,
            )

        self.assertIn("404 Not Found", reason)
        self.assertEqual(bot.get_webhook_info.await_count, 3)

    async def test_watchdog_recovers_after_one_delivery_error(self) -> None:
        webhook, _reason = resolve_webhook_config(
            _settings(
                webhook_url="https://bot.example.com/telegram/webhook",
                webhook_secret=WEBHOOK_SECRET,
            )
        )
        assert webhook is not None
        bot = SimpleNamespace(
            get_webhook_info=AsyncMock(
                side_effect=[
                    SimpleNamespace(
                        url=webhook.url,
                        last_error_date=123,
                        last_error_message="temporary 503",
                    ),
                    SimpleNamespace(url=webhook.url, last_error_date=None),
                ]
            )
        )
        sleeps = 0

        async def sleep_then_stop(_delay: float) -> None:
            nonlocal sleeps
            sleeps += 1
            if sleeps > 2:
                raise asyncio.CancelledError

        with patch(
            "bot.services.update_delivery.asyncio.sleep",
            side_effect=sleep_then_stop,
        ):
            with self.assertRaises(asyncio.CancelledError):
                await update_delivery._watch_webhook(
                    bot=bot,
                    webhook=webhook,
                    initial_error_date=None,
                )

        self.assertGreaterEqual(bot.get_webhook_info.await_count, 2)

    async def test_one_persisted_telegram_error_is_not_counted_repeatedly(self) -> None:
        webhook, _reason = resolve_webhook_config(
            _settings(
                webhook_url="https://bot.example.com/telegram/webhook",
                webhook_secret=WEBHOOK_SECRET,
            )
        )
        assert webhook is not None
        bot = SimpleNamespace(
            get_webhook_info=AsyncMock(
                return_value=SimpleNamespace(
                    url=webhook.url,
                    last_error_date=123,
                    last_error_message="one transient error",
                )
            )
        )
        sleeps = 0

        async def sleep_then_stop(_delay: float) -> None:
            nonlocal sleeps
            sleeps += 1
            if sleeps > 3:
                raise asyncio.CancelledError

        with patch(
            "bot.services.update_delivery.asyncio.sleep",
            side_effect=sleep_then_stop,
        ):
            with self.assertRaises(asyncio.CancelledError):
                await update_delivery._watch_webhook(
                    bot=bot,
                    webhook=webhook,
                    initial_error_date=None,
                )

        self.assertGreaterEqual(bot.get_webhook_info.await_count, 3)

    async def test_watchdog_detects_reused_error_timestamp_after_clean_state(self) -> None:
        webhook, _reason = resolve_webhook_config(
            _settings(
                webhook_url="https://bot.example.com/telegram/webhook",
                webhook_secret=WEBHOOK_SECRET,
            )
        )
        assert webhook is not None
        old_error = SimpleNamespace(
            url=webhook.url,
            last_error_date=123,
            last_error_message="old error",
        )
        clean = SimpleNamespace(url=webhook.url, last_error_date=None)
        def new_error(timestamp: int) -> SimpleNamespace:
            return SimpleNamespace(
                url=webhook.url,
                last_error_date=timestamp,
                last_error_message="new error in same timestamp bucket",
            )

        bot = SimpleNamespace(
            get_webhook_info=AsyncMock(
                side_effect=[
                    old_error,
                    clean,
                    new_error(123),
                    new_error(124),
                    new_error(125),
                ]
            )
        )

        with patch(
            "bot.services.update_delivery.asyncio.sleep",
            new=AsyncMock(),
        ):
            reason = await update_delivery._watch_webhook(
                bot=bot,
                webhook=webhook,
                initial_error_date=123,
            )

        self.assertIn("new error", reason)
        self.assertEqual(bot.get_webhook_info.await_count, 5)

    async def test_confirmed_local_processor_failure_falls_back_immediately(self) -> None:
        webhook, _reason = resolve_webhook_config(
            _settings(
                webhook_url="https://bot.example.com/telegram/webhook",
                webhook_secret=WEBHOOK_SECRET,
            )
        )
        assert webhook is not None
        bot = SimpleNamespace(get_webhook_info=AsyncMock())

        with patch(
            "bot.services.update_delivery.asyncio.sleep",
            new=AsyncMock(),
        ):
            reason = await update_delivery._watch_webhook(
                bot=bot,
                webhook=webhook,
                initial_error_date=None,
                webhook_runtime_failure=lambda: "worker retry exhausted",
            )

        self.assertIn("worker retry exhausted", reason)
        bot.get_webhook_info.assert_not_awaited()

    async def test_one_local_health_check_exception_does_not_force_fallback(self) -> None:
        webhook, _reason = resolve_webhook_config(
            _settings(
                webhook_url="https://bot.example.com/telegram/webhook",
                webhook_secret=WEBHOOK_SECRET,
            )
        )
        assert webhook is not None
        bot = SimpleNamespace(
            get_webhook_info=AsyncMock(
                return_value=SimpleNamespace(
                    url=webhook.url,
                    last_error_date=None,
                )
            )
        )
        local_check = Mock(side_effect=[RuntimeError("probe bug"), None])
        sleeps = 0

        async def sleep_then_stop(_delay: float) -> None:
            nonlocal sleeps
            sleeps += 1
            if sleeps > 2:
                raise asyncio.CancelledError

        with patch(
            "bot.services.update_delivery.asyncio.sleep",
            side_effect=sleep_then_stop,
        ):
            with self.assertRaises(asyncio.CancelledError):
                await update_delivery._watch_webhook(
                    bot=bot,
                    webhook=webhook,
                    initial_error_date=None,
                    webhook_runtime_failure=local_check,
                )

        self.assertGreaterEqual(local_check.call_count, 2)
        bot.get_webhook_info.assert_awaited()

    async def test_webhook_lifecycle_gates_route_around_dispatcher_hooks(self) -> None:
        events: list[str] = []
        dispatcher = _dispatcher()
        dispatcher.emit_startup = AsyncMock(
            side_effect=lambda **_kwargs: events.append("startup")
        )
        dispatcher.emit_shutdown = AsyncMock(
            side_effect=lambda **_kwargs: events.append("shutdown")
        )
        webhook, _reason = resolve_webhook_config(
            _settings(
                webhook_url="https://bot.example.com/telegram/webhook",
                webhook_secret=WEBHOOK_SECRET,
            )
        )
        assert webhook is not None

        bot = SimpleNamespace(
            delete_webhook=AsyncMock(
                side_effect=lambda **_kwargs: events.append("clear") or True
            ),
            set_webhook=AsyncMock(
                side_effect=lambda **_kwargs: events.append("set") or True
            ),
            get_webhook_info=AsyncMock(
                side_effect=lambda: events.append("get")
                or SimpleNamespace(url=webhook.url, last_error_date=None)
            ),
        )

        with patch(
            "bot.services.update_delivery._wait_for_webhook_exit",
            new=AsyncMock(return_value="delivery failed"),
        ):
            result = await update_delivery._run_webhook_session(
                dispatcher=dispatcher,
                bot=bot,
                settings=_settings(),
                webhook=webhook,
                allowed_updates=["message"],
                enable_webhook_route=lambda: events.append("enable"),
                mark_webhook_active=lambda: events.append("active"),
                disable_webhook_route=lambda: events.append("disable"),
        )

        self.assertEqual(result.failure_reason, "delivery failed")
        self.assertEqual(
            events,
            [
                "clear",
                "startup",
                "enable",
                "set",
                "get",
                "active",
                "disable",
                "shutdown",
            ],
        )

    async def test_public_endpoint_probe_failure_falls_back_before_registration(self) -> None:
        settings = _settings(
            webhook_url="https://bot.example.com/telegram/webhook",
            webhook_secret=WEBHOOK_SECRET,
        )
        webhook, reason = resolve_webhook_config(settings)
        assert webhook is not None
        dispatcher = _dispatcher()
        bot = SimpleNamespace(
            set_webhook=AsyncMock(),
            delete_webhook=AsyncMock(return_value=True),
        )

        with patch(
            "bot.services.update_delivery._probe_webhook_endpoint",
            new=AsyncMock(return_value="公网地址返回 HTTP 200，未命中 bot webhook 路由"),
        ):
            mode = await run_update_delivery(
                bot=bot,
                dispatcher=dispatcher,
                settings=settings,
                webhook=webhook,
                fallback_reason=reason,
            )

        self.assertEqual(mode, "polling")
        bot.set_webhook.assert_not_awaited()
        bot.delete_webhook.assert_awaited_once_with(drop_pending_updates=False)

    async def test_registration_url_mismatch_falls_back(self) -> None:
        settings = _settings(
            webhook_url="https://bot.example.com/telegram/webhook",
            webhook_secret=WEBHOOK_SECRET,
        )
        webhook, reason = resolve_webhook_config(settings)
        assert webhook is not None
        dispatcher = _dispatcher()
        bot = SimpleNamespace(
            set_webhook=AsyncMock(return_value=True),
            get_webhook_info=AsyncMock(
                return_value=SimpleNamespace(url="https://wrong.example.com/hook")
            ),
            delete_webhook=AsyncMock(return_value=True),
        )

        mode = await run_update_delivery(
            bot=bot,
            dispatcher=dispatcher,
            settings=settings,
            webhook=webhook,
            fallback_reason=reason,
        )

        self.assertEqual(mode, "polling")
        self.assertEqual(
            [call.kwargs["drop_pending_updates"] for call in bot.delete_webhook.await_args_list],
            [False, False],
        )
        dispatcher.start_polling.assert_awaited_once()


class WebhookRouteTests(unittest.IsolatedAsyncioTestCase):
    async def test_durable_inbox_ack_does_not_occupy_worker_for_full_ai_reply(self) -> None:
        from datetime import timedelta

        from bot.utils.timezone import now_shanghai_naive

        bot = Bot(token="42:TEST_TOKEN")
        dispatcher = Dispatcher()
        receipt_holder: list[object] = []
        dispatch_started = asyncio.Event()

        async def feed_update(**_kwargs: object) -> None:
            receipt = current_update_completion()
            assert receipt is not None
            receipt.defer()
            receipt_holder.append(receipt)
            dispatch_started.set()

        dispatcher.feed_raw_update = AsyncMock(side_effect=feed_update)
        server = VerifyWebServer(
            bot=bot,
            settings=_settings(),
            # Callable enables the production durable-inbox branch; persistence
            # methods are isolated below so this remains a pure unit test.
            session_factory=lambda: None,
            webhook_dispatcher=dispatcher,
            webhook_path="/telegram/webhook",
            webhook_secret=WEBHOOK_SECRET,
        )
        app = server.build_app()
        handler = next(
            route.handler
            for route in app.router.routes()
            if route.method == "POST"
            and route.resource.canonical == "/telegram/webhook"
        )
        processor = server._webhook_processor
        assert processor is not None
        complete = AsyncMock(return_value=True)
        server.enable_webhook_route()
        try:
            with (
                patch.object(
                    processor,
                    "_ensure_durable_update",
                    new=AsyncMock(return_value=False),
                ),
                patch.object(
                    processor,
                    "_claim_durable_update",
                    new=AsyncMock(
                        return_value=now_shanghai_naive() + timedelta(minutes=5)
                    ),
                ),
                patch.object(
                    processor,
                    "_load_durable_payload",
                    new=AsyncMock(return_value={"update_id": 900}),
                ),
                patch.object(
                    processor,
                    "_complete_durable_update",
                    new=complete,
                ),
                patch.object(
                    processor,
                    "_release_durable_update",
                    new=AsyncMock(),
                ),
            ):
                response = await asyncio.wait_for(
                    handler(
                        SimpleNamespace(
                            headers={
                                "X-Telegram-Bot-Api-Secret-Token": WEBHOOK_SECRET
                            },
                            json=AsyncMock(return_value={"update_id": 900}),
                        )
                    ),
                    timeout=1.0,
                )
                self.assertEqual(response.status, 200)
                await asyncio.wait_for(dispatch_started.wait(), timeout=1.0)
                self.assertEqual(len(receipt_holder), 1)
                complete.assert_not_awaited()

                receipt_holder[0].finish(True)
                for _ in range(20):
                    if complete.await_count:
                        break
                    await asyncio.sleep(0)
                complete.assert_awaited_once()
        finally:
            await server.disable_webhook_route()
            await server._stop_webhook_processor()
            await bot.session.close()

    async def test_webhook_ack_waits_for_detached_group_reply_completion(self) -> None:
        bot = Bot(token="42:TEST_TOKEN")
        dispatcher = Dispatcher()
        deferred_started = asyncio.Event()
        receipt_holder: list[object] = []

        async def feed_update(**_kwargs: object) -> None:
            receipt = current_update_completion()
            self.assertIsNotNone(receipt)
            assert receipt is not None
            receipt.defer()
            receipt_holder.append(receipt)
            deferred_started.set()

        dispatcher.feed_raw_update = AsyncMock(side_effect=feed_update)
        server = VerifyWebServer(
            bot=bot,
            settings=_settings(),
            session_factory=SimpleNamespace(),
            webhook_dispatcher=dispatcher,
            webhook_path="/telegram/webhook",
            webhook_secret=WEBHOOK_SECRET,
        )
        app = server.build_app()
        handler = next(
            route.handler
            for route in app.router.routes()
            if route.method == "POST"
            and route.resource.canonical == "/telegram/webhook"
        )
        server.enable_webhook_route()
        request = SimpleNamespace(
            headers={"X-Telegram-Bot-Api-Secret-Token": WEBHOOK_SECRET},
            json=AsyncMock(return_value={"update_id": 901}),
        )
        response_task = asyncio.create_task(handler(request))
        try:
            await asyncio.wait_for(deferred_started.wait(), timeout=1.0)
            await asyncio.sleep(0)
            self.assertFalse(response_task.done())
            receipt_holder[0].finish(True)
            response = await asyncio.wait_for(response_task, timeout=1.0)
            self.assertEqual(response.status, 200)
        finally:
            if not response_task.done():
                response_task.cancel()
            await asyncio.gather(response_task, return_exceptions=True)
            await server.disable_webhook_route()
            await bot.session.close()

    async def test_failed_detached_group_reply_returns_retryable_503(self) -> None:
        bot = Bot(token="42:TEST_TOKEN")
        dispatcher = Dispatcher()

        async def feed_update(**_kwargs: object) -> None:
            receipt = current_update_completion()
            assert receipt is not None
            receipt.defer()
            receipt.finish(False)

        dispatcher.feed_raw_update = AsyncMock(side_effect=feed_update)
        server = VerifyWebServer(
            bot=bot,
            settings=_settings(),
            session_factory=SimpleNamespace(),
            webhook_dispatcher=dispatcher,
            webhook_path="/telegram/webhook",
            webhook_secret=WEBHOOK_SECRET,
        )
        app = server.build_app()
        handler = next(
            route.handler
            for route in app.router.routes()
            if route.method == "POST"
            and route.resource.canonical == "/telegram/webhook"
        )
        server.enable_webhook_route()
        try:
            response = await handler(
                SimpleNamespace(
                    headers={
                        "X-Telegram-Bot-Api-Secret-Token": WEBHOOK_SECRET
                    },
                    json=AsyncMock(return_value={"update_id": 902}),
                )
            )
            self.assertEqual(response.status, 503)
        finally:
            await server.disable_webhook_route()
            await bot.session.close()

    async def test_durable_processor_construction_error_fails_startup(self) -> None:
        server = VerifyWebServer(
            bot=SimpleNamespace(token="42:TEST_TOKEN"),
            settings=_settings(),
            session_factory=SimpleNamespace(),
            webhook_dispatcher=Dispatcher(),
            webhook_path="/telegram/webhook",
            webhook_secret=WEBHOOK_SECRET,
        )

        with patch(
            "bot.services.verify_web._WebhookUpdateQueue",
            side_effect=RuntimeError("invalid route"),
        ):
            with self.assertRaisesRegex(RuntimeError, "invalid route"):
                server.build_app()

    async def test_web_app_shutdown_does_not_close_shared_bot_session(self) -> None:
        session = SimpleNamespace(close=AsyncMock())
        bot = SimpleNamespace(token="42:TEST_TOKEN", session=session)
        server = VerifyWebServer(
            bot=bot,
            settings=_settings(),
            session_factory=SimpleNamespace(),
            webhook_dispatcher=Dispatcher(),
            webhook_path="/telegram/webhook",
            webhook_secret=WEBHOOK_SECRET,
        )
        app = server.build_app()

        for callback in app.on_shutdown:
            await callback(app)

        session.close.assert_not_awaited()

    async def test_health_is_unavailable_while_switching_then_recovers_for_polling(self) -> None:
        server = VerifyWebServer(
            bot=SimpleNamespace(token="42:TEST_TOKEN"),
            settings=_settings(),
            session_factory=SimpleNamespace(),
        )

        await server.disable_webhook_route()
        switching = await server.handle_health(SimpleNamespace())
        server.mark_polling_active()
        polling = await server.handle_health(SimpleNamespace())

        self.assertEqual(switching.status, 503)
        self.assertEqual(polling.status, 200)

    async def test_webhook_health_waits_for_registration_confirmation(self) -> None:
        bot = Bot(token="42:TEST_TOKEN")
        server = VerifyWebServer(
            bot=bot,
            settings=_settings(),
            session_factory=SimpleNamespace(),
            webhook_dispatcher=Dispatcher(),
            webhook_path="/telegram/webhook",
            webhook_secret=WEBHOOK_SECRET,
        )
        server.build_app()
        try:
            server.enable_webhook_route()
            registering = await server.handle_health(SimpleNamespace())
            server.mark_webhook_active()
            active = await server.handle_health(SimpleNamespace())

            self.assertEqual(registering.status, 503)
            self.assertEqual(active.status, 200)
        finally:
            await server.disable_webhook_route()
            await bot.session.close()

    async def test_failed_update_returns_503_and_telegram_retry_can_succeed(self) -> None:
        bot = Bot(token="42:TEST_TOKEN")
        dispatcher = Dispatcher()
        dispatcher.feed_raw_update = AsyncMock(
            side_effect=[RuntimeError("temporary"), None]
        )
        server = VerifyWebServer(
            bot=bot,
            settings=_settings(),
            session_factory=SimpleNamespace(),
            webhook_dispatcher=dispatcher,
            webhook_path="/telegram/webhook",
            webhook_secret=WEBHOOK_SECRET,
        )
        app = server.build_app()
        handler = next(
            route.handler
            for route in app.router.routes()
            if route.method == "POST"
            and route.resource.canonical == "/telegram/webhook"
        )
        server.enable_webhook_route()
        try:
            failed = await handler(
                SimpleNamespace(
                    headers={
                        "X-Telegram-Bot-Api-Secret-Token": WEBHOOK_SECRET
                    },
                    json=AsyncMock(return_value={"update_id": 10}),
                )
                )
            succeeded = await handler(
                SimpleNamespace(
                    headers={
                        "X-Telegram-Bot-Api-Secret-Token": WEBHOOK_SECRET
                    },
                    json=AsyncMock(return_value={"update_id": 10}),
                )
            )
            self.assertEqual(failed.status, 503)
            self.assertEqual(succeeded.status, 200)
            assert server._webhook_processor is not None
            snapshot = server._webhook_processor.health_snapshot()
            self.assertTrue(snapshot["ok"])
            self.assertEqual(snapshot["completed_updates"], 1)
            self.assertEqual(snapshot["retried_updates"], 1)
            self.assertEqual(dispatcher.feed_raw_update.await_count, 2)
        finally:
            await server.disable_webhook_route()
            await bot.session.close()

    async def test_duplicate_delivery_shares_one_inflight_dispatch(self) -> None:
        bot = Bot(token="42:TEST_TOKEN")
        dispatcher = Dispatcher()
        started = asyncio.Event()
        release = asyncio.Event()

        async def feed_update(**_kwargs: object) -> None:
            started.set()
            await release.wait()

        dispatcher.feed_raw_update = AsyncMock(side_effect=feed_update)
        server = VerifyWebServer(
            bot=bot,
            settings=_settings(),
            session_factory=SimpleNamespace(),
            webhook_dispatcher=dispatcher,
            webhook_path="/telegram/webhook",
            webhook_secret=WEBHOOK_SECRET,
        )
        app = server.build_app()
        handler = next(
            route.handler
            for route in app.router.routes()
            if route.method == "POST"
            and route.resource.canonical == "/telegram/webhook"
        )
        server.enable_webhook_route()

        def request() -> SimpleNamespace:
            return SimpleNamespace(
                headers={
                    "X-Telegram-Bot-Api-Secret-Token": WEBHOOK_SECRET
                },
                json=AsyncMock(return_value={"update_id": 101}),
            )

        first = asyncio.create_task(handler(request()))
        await asyncio.wait_for(started.wait(), timeout=1.0)
        duplicate = asyncio.create_task(handler(request()))
        await asyncio.sleep(0)
        self.assertEqual(dispatcher.feed_raw_update.await_count, 1)
        release.set()
        try:
            first_response, duplicate_response = await asyncio.gather(
                first,
                duplicate,
            )
            self.assertEqual(first_response.status, 200)
            self.assertEqual(duplicate_response.status, 200)

            completed_duplicate = await handler(request())
            self.assertEqual(completed_duplicate.status, 200)
            self.assertEqual(dispatcher.feed_raw_update.await_count, 1)
        finally:
            release.set()
            await asyncio.gather(first, duplicate, return_exceptions=True)
            await server.disable_webhook_route()
            await bot.session.close()

    async def test_exhausted_update_retries_fail_health_and_reject_new_updates(self) -> None:
        bot = Bot(token="42:TEST_TOKEN")
        dispatcher = Dispatcher()
        dispatcher.feed_raw_update = AsyncMock(side_effect=RuntimeError("broken"))
        server = VerifyWebServer(
            bot=bot,
            settings=_settings(),
            session_factory=SimpleNamespace(),
            webhook_dispatcher=dispatcher,
            webhook_path="/telegram/webhook",
            webhook_secret=WEBHOOK_SECRET,
        )
        app = server.build_app()
        handler = next(
            route.handler
            for route in app.router.routes()
            if route.method == "POST"
            and route.resource.canonical == "/telegram/webhook"
        )
        server.enable_webhook_route()
        request = SimpleNamespace(
            headers={"X-Telegram-Bot-Api-Secret-Token": WEBHOOK_SECRET},
            json=AsyncMock(return_value={"update_id": 11}),
        )
        try:
            failures = [await handler(request) for _ in range(3)]
            self.assertTrue(all(response.status == 503 for response in failures))
            assert server._webhook_processor is not None

            rejected = await handler(request)
            health = await server.handle_health(SimpleNamespace())
            self.assertEqual(rejected.status, 503)
            self.assertEqual(health.status, 503)
            self.assertIn("broken", server.webhook_runtime_failure() or "")
            self.assertEqual(dispatcher.feed_raw_update.await_count, 3)
        finally:
            await server.disable_webhook_route()
            await bot.session.close()

    async def test_update_deadline_marks_processor_unhealthy(self) -> None:
        bot = Bot(token="42:TEST_TOKEN")
        dispatcher = Dispatcher()

        async def slow_update(**_kwargs: object) -> None:
            await asyncio.sleep(60)

        dispatcher.feed_raw_update = AsyncMock(side_effect=slow_update)
        server = VerifyWebServer(
            bot=bot,
            settings=_settings(),
            session_factory=SimpleNamespace(),
            webhook_dispatcher=dispatcher,
            webhook_path="/telegram/webhook",
            webhook_secret=WEBHOOK_SECRET,
        )
        app = server.build_app()
        handler = next(
            route.handler
            for route in app.router.routes()
            if route.method == "POST"
            and route.resource.canonical == "/telegram/webhook"
        )
        server.enable_webhook_route()
        try:
            with (
                patch("bot.services.verify_web._WEBHOOK_UPDATE_TIMEOUT_SECONDS", 0.01),
                patch("bot.services.verify_web._WEBHOOK_UPDATE_CANCEL_GRACE_SECONDS", 0.01),
            ):
                response = await handler(
                    SimpleNamespace(
                        headers={
                            "X-Telegram-Bot-Api-Secret-Token": WEBHOOK_SECRET
                        },
                        json=AsyncMock(return_value={"update_id": 13}),
                    )
                )
                self.assertEqual(response.status, 503)
                assert server._webhook_processor is not None
                snapshot = server._webhook_processor.health_snapshot()

            self.assertFalse(snapshot["ok"])
            self.assertEqual(snapshot["timed_out_updates"], 1)
            self.assertIn("exceeded", server.webhook_runtime_failure() or "")
        finally:
            await server.disable_webhook_route()
            await bot.session.close()

    async def test_disabling_route_has_bounded_drain_for_stuck_update(self) -> None:
        bot = Bot(token="42:TEST_TOKEN")
        dispatcher = Dispatcher()
        started = asyncio.Event()
        release = asyncio.Event()

        async def stuck_update(**_kwargs: object) -> None:
            started.set()
            try:
                await asyncio.Future()
            except asyncio.CancelledError:
                await release.wait()

        dispatcher.feed_raw_update = AsyncMock(side_effect=stuck_update)
        server = VerifyWebServer(
            bot=bot,
            settings=_settings(),
            session_factory=SimpleNamespace(),
            webhook_dispatcher=dispatcher,
            webhook_path="/telegram/webhook",
            webhook_secret=WEBHOOK_SECRET,
        )
        app = server.build_app()
        handler = next(
            route.handler
            for route in app.router.routes()
            if route.method == "POST"
            and route.resource.canonical == "/telegram/webhook"
        )
        server.enable_webhook_route()
        request_task: asyncio.Task | None = None
        try:
            request_task = asyncio.create_task(
                handler(
                    SimpleNamespace(
                        headers={
                            "X-Telegram-Bot-Api-Secret-Token": WEBHOOK_SECRET
                        },
                        json=AsyncMock(return_value={"update_id": 12}),
                    )
                )
            )
            await asyncio.wait_for(started.wait(), timeout=1.0)
            with (
                patch("bot.services.verify_web._WEBHOOK_DRAIN_TIMEOUT_SECONDS", 0.01),
                patch("bot.services.verify_web._WEBHOOK_REQUEST_DRAIN_TIMEOUT_SECONDS", 0.01),
                patch("bot.services.verify_web._WEBHOOK_WORKER_STOP_TIMEOUT_SECONDS", 0.01),
                patch("bot.services.verify_web._WEBHOOK_UPDATE_CANCEL_GRACE_SECONDS", 0.01),
            ):
                await asyncio.wait_for(
                    server.disable_webhook_route(),
                    timeout=0.5,
                )
            result = await asyncio.gather(request_task, return_exceptions=True)
            self.assertTrue(
                isinstance(result[0], asyncio.CancelledError)
                or getattr(result[0], "status", None) == 503
            )
        finally:
            release.set()
            if request_task is not None:
                await asyncio.gather(request_task, return_exceptions=True)
            await asyncio.sleep(0)
            await bot.session.close()

    async def test_disabling_route_waits_for_inflight_update_before_response(self) -> None:
        bot = Bot(token="42:TEST_TOKEN")
        dispatcher = Dispatcher()
        started = asyncio.Event()
        release = asyncio.Event()

        async def feed_update(**_kwargs: object) -> None:
            started.set()
            await release.wait()

        dispatcher.feed_raw_update = AsyncMock(side_effect=feed_update)
        server = VerifyWebServer(
            bot=bot,
            settings=_settings(),
            session_factory=SimpleNamespace(),
            webhook_dispatcher=dispatcher,
            webhook_path="/telegram/webhook",
            webhook_secret=WEBHOOK_SECRET,
        )
        app = server.build_app()
        handler = next(
            route.handler
            for route in app.router.routes()
            if route.method == "POST"
            and route.resource.canonical == "/telegram/webhook"
        )
        server.enable_webhook_route()
        request_task: asyncio.Task | None = None
        try:
            request_task = asyncio.create_task(
                handler(
                    SimpleNamespace(
                        headers={
                            "X-Telegram-Bot-Api-Secret-Token": WEBHOOK_SECRET
                        },
                        json=AsyncMock(return_value={"update_id": 1}),
                    )
                )
            )
            await asyncio.wait_for(started.wait(), timeout=1.0)

            disable_task = asyncio.create_task(server.disable_webhook_route())
            await asyncio.sleep(0)
            self.assertFalse(disable_task.done())
            release.set()
            await asyncio.wait_for(disable_task, timeout=1.0)
            response = await asyncio.wait_for(request_task, timeout=1.0)
            self.assertEqual(response.status, 200)
            dispatcher.feed_raw_update.assert_awaited_once()
        finally:
            release.set()
            if request_task is not None:
                await asyncio.gather(request_task, return_exceptions=True)
            await server.disable_webhook_route()
            await bot.session.close()

    async def test_privileged_lane_runs_while_ordinary_workers_and_queue_are_full(
        self,
    ) -> None:
        bot = Bot(token="42:TEST_TOKEN")
        dispatcher = Dispatcher()
        ordinary_started = asyncio.Event()
        release_ordinary = asyncio.Event()
        privileged_started = asyncio.Event()
        ordinary_calls = 0
        observed_priorities: list[ExecutionPriority] = []

        async def feed_update(*, update: dict, **_kwargs: object) -> None:
            nonlocal ordinary_calls
            if telegram_update_is_privileged(update):
                observed_priorities.append(current_execution_priority())
                privileged_started.set()
                return
            ordinary_calls += 1
            if ordinary_calls == 1:
                ordinary_started.set()
                await release_ordinary.wait()

        dispatcher.feed_raw_update = AsyncMock(side_effect=feed_update)
        queue = _WebhookUpdateQueue(
            dispatcher=dispatcher,
            bot=bot,
            session_factory=None,
            secret_token=WEBHOOK_SECRET,
            worker_count=1,
            critical_worker_count=1,
            security_worker_count=1,
            critical_queue_capacity=2,
            security_queue_capacity=2,
        )
        operator_id = 8101
        group_id = -1008101
        update_delivery.mark_privileged_operator(operator_id, group_id=group_id)

        def request(update_id: int, text: str) -> SimpleNamespace:
            return SimpleNamespace(
                json=AsyncMock(
                    return_value={
                        "update_id": update_id,
                        "message": {
                            "text": text,
                            "chat": {"id": group_id},
                            "from": {"id": operator_id},
                        },
                    }
                )
            )

        ordinary_tasks: list[asyncio.Task] = []
        try:
            ordinary_tasks.append(
                asyncio.create_task(queue.handle_verified(request(1, "slow")))
            )
            await asyncio.wait_for(ordinary_started.wait(), timeout=1.0)

            # One ordinary worker plus its four reserved queue positions are
            # occupied.  The priority lane must remain independently usable.
            ordinary_tasks.extend(
                asyncio.create_task(
                    queue.handle_verified(request(update_id, "queued"))
                )
                for update_id in range(2, 6)
            )
            for _ in range(100):
                if queue.health_snapshot()["ordinary_queue_size"] == 4:
                    break
                await asyncio.sleep(0)
            self.assertEqual(
                queue.health_snapshot()["ordinary_queue_size"],
                4,
            )

            privileged_task = asyncio.create_task(
                queue.handle_verified(request(100, "/ban 42 spam"))
            )
            await asyncio.wait_for(privileged_started.wait(), timeout=0.5)
            privileged_response = await asyncio.wait_for(
                privileged_task,
                timeout=0.5,
            )
            self.assertEqual(privileged_response.status, 200)
            self.assertFalse(release_ordinary.is_set())
            self.assertTrue(all(not task.done() for task in ordinary_tasks))
            self.assertEqual(observed_priorities, [ExecutionPriority.CRITICAL])
            snapshot = queue.health_snapshot()
            self.assertEqual(snapshot["accepted_privileged_updates"], 1)
            self.assertEqual(snapshot["completed_privileged_updates"], 1)
            self.assertEqual(snapshot["critical_workers_alive"], 1)

            release_ordinary.set()
            responses = await asyncio.gather(*ordinary_tasks)
            self.assertTrue(all(response.status == 200 for response in responses))
        finally:
            release_ordinary.set()
            await asyncio.gather(*ordinary_tasks, return_exceptions=True)
            await queue.stop()
            await bot.session.close()

    async def test_unknown_admin_candidate_uses_isolated_unprivileged_lane(
        self,
    ) -> None:
        bot = Bot(token="42:TEST_TOKEN")
        dispatcher = Dispatcher()
        ordinary_started = asyncio.Event()
        release_ordinary = asyncio.Event()
        auth_started = asyncio.Event()
        observed_priorities: list[ExecutionPriority] = []

        async def feed_update(*, update: dict, **_kwargs: object) -> None:
            if telegram_update_is_auth_candidate(update):
                observed_priorities.append(current_execution_priority())
                auth_started.set()
                return
            ordinary_started.set()
            await release_ordinary.wait()

        dispatcher.feed_raw_update = AsyncMock(side_effect=feed_update)
        queue = _WebhookUpdateQueue(
            dispatcher=dispatcher,
            bot=bot,
            session_factory=None,
            secret_token=WEBHOOK_SECRET,
            worker_count=1,
            auth_worker_count=1,
            critical_worker_count=1,
            security_worker_count=1,
            auth_queue_capacity=2,
            critical_queue_capacity=2,
            security_queue_capacity=2,
        )

        def request(update_id: int, text: str) -> SimpleNamespace:
            return SimpleNamespace(
                json=AsyncMock(
                    return_value={
                        "update_id": update_id,
                        "message": {
                            "text": text,
                            "chat": {"id": -1009001},
                            "from": {"id": 9001},
                        },
                    }
                )
            )

        ordinary_tasks: list[asyncio.Task] = []
        try:
            ordinary_tasks.append(
                asyncio.create_task(queue.handle_verified(request(1, "slow")))
            )
            await asyncio.wait_for(ordinary_started.wait(), timeout=0.5)
            ordinary_tasks.extend(
                asyncio.create_task(
                    queue.handle_verified(request(update_id, "queued"))
                )
                for update_id in range(2, 6)
            )
            for _ in range(100):
                if queue.health_snapshot()["ordinary_queue_size"] == 4:
                    break
                await asyncio.sleep(0)
            self.assertEqual(queue.health_snapshot()["ordinary_queue_size"], 4)

            auth_task = asyncio.create_task(
                queue.handle_verified(request(100, "/ban 42 spam"))
            )
            await asyncio.wait_for(auth_started.wait(), timeout=0.5)
            response = await asyncio.wait_for(auth_task, timeout=0.5)
            self.assertEqual(response.status, 200)
            self.assertFalse(release_ordinary.is_set())
            self.assertEqual(observed_priorities, [ExecutionPriority.HIGH])
            snapshot = queue.health_snapshot()
            self.assertEqual(snapshot["accepted_auth_updates"], 1)
            self.assertEqual(snapshot["completed_auth_updates"], 1)
            self.assertEqual(snapshot["accepted_privileged_updates"], 0)
            self.assertEqual(snapshot["auth_workers_alive"], 1)
        finally:
            release_ordinary.set()
            await asyncio.gather(*ordinary_tasks, return_exceptions=True)
            await queue.stop()
            await bot.session.close()

    async def test_worker_cancellation_always_balances_queue_task_accounting(
        self,
    ) -> None:
        bot = Bot(token="42:TEST_TOKEN")
        dispatcher = Dispatcher()
        started = asyncio.Event()

        async def stuck(**_kwargs: object) -> None:
            started.set()
            await asyncio.Future()

        dispatcher.feed_raw_update = AsyncMock(side_effect=stuck)
        queue = _WebhookUpdateQueue(
            dispatcher=dispatcher,
            bot=bot,
            session_factory=None,
            secret_token=WEBHOOK_SECRET,
            worker_count=1,
            auth_worker_count=1,
            critical_worker_count=1,
            security_worker_count=1,
        )
        request = SimpleNamespace(
            json=AsyncMock(
                return_value={"update_id": 150, "message": {"text": "slow"}}
            )
        )
        response_task = asyncio.create_task(queue.handle_verified(request))
        try:
            await asyncio.wait_for(started.wait(), timeout=0.5)
            queue._stopped = True
            worker = queue._ordinary_workers[0]
            worker.cancel()
            await asyncio.gather(worker, return_exceptions=True)
            response = await asyncio.wait_for(response_task, timeout=0.5)
            self.assertEqual(response.status, 503)
            await asyncio.wait_for(queue.queue.join(), timeout=0.5)
        finally:
            if not response_task.done():
                response_task.cancel()
                await asyncio.gather(response_task, return_exceptions=True)
            await queue.stop()
            await bot.session.close()

    async def test_ban_command_bypasses_a_full_membership_security_lane(
        self,
    ) -> None:
        bot = Bot(token="42:TEST_TOKEN")
        dispatcher = Dispatcher()
        membership_started = asyncio.Event()
        release_membership = asyncio.Event()
        ban_started = asyncio.Event()
        security_calls = 0
        observed: list[ExecutionPriority] = []

        async def feed_update(*, update: dict, **_kwargs: object) -> None:
            nonlocal security_calls
            priority = telegram_update_priority(update)
            observed.append(current_execution_priority())
            if priority <= ExecutionPriority.CRITICAL:
                ban_started.set()
                return
            security_calls += 1
            if security_calls == 1:
                membership_started.set()
                await release_membership.wait()

        dispatcher.feed_raw_update = AsyncMock(side_effect=feed_update)
        queue = _WebhookUpdateQueue(
            dispatcher=dispatcher,
            bot=bot,
            session_factory=None,
            secret_token=WEBHOOK_SECRET,
            worker_count=1,
            critical_worker_count=1,
            security_worker_count=1,
            critical_queue_capacity=2,
            security_queue_capacity=4,
        )
        operator_id = 8102
        group_id = -1008102
        update_delivery.mark_privileged_operator(operator_id, group_id=group_id)

        def membership_request(update_id: int) -> SimpleNamespace:
            return SimpleNamespace(
                json=AsyncMock(
                    return_value={
                        "update_id": update_id,
                        "chat_member": {"chat": {"id": -100}},
                    }
                )
            )

        def ban_request() -> SimpleNamespace:
            return SimpleNamespace(
                json=AsyncMock(
                    return_value={
                        "update_id": 999,
                        "message": {
                            "text": "/ban 42 raid",
                            "chat": {"id": group_id},
                            "from": {"id": operator_id},
                        },
                    }
                )
            )

        security_tasks: list[asyncio.Task] = []
        try:
            security_tasks.append(
                asyncio.create_task(
                    queue.handle_verified(membership_request(301))
                )
            )
            await asyncio.wait_for(membership_started.wait(), timeout=0.5)
            security_tasks.extend(
                asyncio.create_task(
                    queue.handle_verified(membership_request(update_id))
                )
                for update_id in range(302, 306)
            )
            for _ in range(100):
                if queue.health_snapshot()["security_queue_size"] == 4:
                    break
                await asyncio.sleep(0)
            self.assertEqual(queue.health_snapshot()["security_queue_size"], 4)

            ban_task = asyncio.create_task(queue.handle_verified(ban_request()))
            await asyncio.wait_for(ban_started.wait(), timeout=0.5)
            ban_response = await asyncio.wait_for(ban_task, timeout=0.5)
            self.assertEqual(ban_response.status, 200)
            self.assertFalse(release_membership.is_set())
            self.assertTrue(all(not task.done() for task in security_tasks))
            self.assertIn(ExecutionPriority.HIGH, observed)
            self.assertEqual(observed[-1], ExecutionPriority.CRITICAL)

            release_membership.set()
            responses = await asyncio.gather(*security_tasks)
            self.assertTrue(all(response.status == 200 for response in responses))
        finally:
            release_membership.set()
            await asyncio.gather(*security_tasks, return_exceptions=True)
            await queue.stop()
            await bot.session.close()

    async def test_privileged_lane_preserves_fifo_order(self) -> None:
        bot = Bot(token="42:TEST_TOKEN")
        dispatcher = Dispatcher()
        first_started = asyncio.Event()
        release_first = asyncio.Event()
        order: list[int] = []

        async def feed_update(*, update: dict, **_kwargs: object) -> None:
            update_id = int(update["update_id"])
            order.append(update_id)
            if update_id == 201:
                first_started.set()
                await release_first.wait()

        dispatcher.feed_raw_update = AsyncMock(side_effect=feed_update)
        queue = _WebhookUpdateQueue(
            dispatcher=dispatcher,
            bot=bot,
            session_factory=None,
            secret_token=WEBHOOK_SECRET,
            worker_count=1,
            critical_worker_count=1,
            security_worker_count=1,
            critical_queue_capacity=4,
            security_queue_capacity=2,
        )
        operator_id = 8103
        group_id = -1008103
        update_delivery.mark_privileged_operator(operator_id, group_id=group_id)

        def request(update_id: int) -> SimpleNamespace:
            return SimpleNamespace(
                json=AsyncMock(
                    return_value={
                        "update_id": update_id,
                        "message": {
                            "text": "/unban 42",
                            "chat": {"id": group_id},
                            "from": {"id": operator_id},
                        },
                    }
                )
            )

        tasks: list[asyncio.Task] = []
        try:
            tasks.append(
                asyncio.create_task(queue.handle_verified(request(201)))
            )
            await asyncio.wait_for(first_started.wait(), timeout=0.5)
            tasks.extend(
                asyncio.create_task(queue.handle_verified(request(update_id)))
                for update_id in (202, 203)
            )
            for _ in range(100):
                if queue.health_snapshot()["critical_queue_size"] == 2:
                    break
                await asyncio.sleep(0)
            self.assertEqual(
                queue.health_snapshot()["critical_queue_size"],
                2,
            )
            release_first.set()
            responses = await asyncio.gather(*tasks)
            self.assertTrue(all(response.status == 200 for response in responses))
            self.assertEqual(order, [201, 202, 203])
        finally:
            release_first.set()
            await asyncio.gather(*tasks, return_exceptions=True)
            await queue.stop()
            await bot.session.close()

    async def test_webhook_handler_bounds_concurrent_updates(self) -> None:
        bot = Bot(token="42:TEST_TOKEN")
        dispatcher = Dispatcher()
        release = asyncio.Event()
        saturated = asyncio.Event()
        active = 0
        max_active = 0

        async def feed_update(**_kwargs: object) -> None:
            nonlocal active, max_active
            active += 1
            max_active = max(max_active, active)
            if active == update_delivery.WEBHOOK_MAX_CONCURRENT_UPDATES:
                saturated.set()
            try:
                await release.wait()
            finally:
                active -= 1

        dispatcher.feed_raw_update = AsyncMock(side_effect=feed_update)
        server = VerifyWebServer(
            bot=bot,
            settings=_settings(),
            session_factory=SimpleNamespace(),
            webhook_dispatcher=dispatcher,
            webhook_path="/telegram/webhook",
            webhook_secret=WEBHOOK_SECRET,
        )
        app = server.build_app()
        handler = next(
            route.handler
            for route in app.router.routes()
            if route.method == "POST"
            and route.resource.canonical == "/telegram/webhook"
        )
        server.enable_webhook_route()
        initial_count = update_delivery.WEBHOOK_MAX_CONCURRENT_UPDATES
        initial_requests = [
            SimpleNamespace(
                headers={
                    "X-Telegram-Bot-Api-Secret-Token": WEBHOOK_SECRET
                },
                json=AsyncMock(return_value={"update_id": index}),
            )
            for index in range(initial_count)
        ]
        initial_tasks = [
            asyncio.create_task(handler(request)) for request in initial_requests
        ]
        overflow_tasks: list[asyncio.Task] = []
        try:
            await asyncio.wait_for(saturated.wait(), timeout=1.0)
            self.assertEqual(
                max_active,
                update_delivery.WEBHOOK_MAX_CONCURRENT_UPDATES,
            )

            queued_count = update_delivery.WEBHOOK_MAX_CONCURRENT_UPDATES * 4
            overflow_requests = [
                SimpleNamespace(
                    headers={
                        "X-Telegram-Bot-Api-Secret-Token": WEBHOOK_SECRET
                    },
                    json=AsyncMock(
                        return_value={"update_id": initial_count + index}
                    ),
                )
                for index in range(queued_count + 1)
            ]
            overflow_tasks = [
                asyncio.create_task(handler(request))
                for request in overflow_requests
            ]
            done, pending = await asyncio.wait(overflow_tasks, timeout=0.2)
            self.assertEqual(len(done), 1)
            self.assertEqual(next(iter(done)).result().status, 503)

            release.set()
            initial_responses = await asyncio.gather(*initial_tasks)
            overflow_responses = await asyncio.gather(*pending)
            accepted_count = initial_count + queued_count
            for _ in range(100):
                if dispatcher.feed_raw_update.await_count == accepted_count:
                    break
                await asyncio.sleep(0)

            self.assertTrue(
                all(response.status == 200 for response in initial_responses)
            )
            self.assertTrue(
                all(response.status == 200 for response in overflow_responses)
            )
            self.assertEqual(dispatcher.feed_raw_update.await_count, accepted_count)
            self.assertEqual(
                max_active,
                update_delivery.WEBHOOK_MAX_CONCURRENT_UPDATES,
            )
        finally:
            release.set()
            await asyncio.gather(*initial_tasks, return_exceptions=True)
            await asyncio.gather(*overflow_tasks, return_exceptions=True)
            await server.disable_webhook_route()
            await bot.session.close()

    async def test_webhook_route_requires_secret_and_forwards_update(self) -> None:
        bot = Bot(token="42:TEST_TOKEN")
        dispatcher = Dispatcher()
        dispatcher.feed_raw_update = AsyncMock(return_value=None)
        server = VerifyWebServer(
            bot=bot,
            settings=_settings(),
            session_factory=SimpleNamespace(),
            webhook_dispatcher=dispatcher,
            webhook_path="/telegram/webhook",
            webhook_secret=WEBHOOK_SECRET,
        )
        app = server.build_app()
        routes = [
            route
            for route in app.router.routes()
            if route.method == "POST"
            and route.resource.canonical == "/telegram/webhook"
        ]
        self.assertEqual(len(routes), 1)
        handler = routes[0].handler
        try:
            rejected = await handler(
                SimpleNamespace(
                    headers={
                        "X-Telegram-Bot-Api-Secret-Token": "wrong_secret"
                    },
                    json=AsyncMock(return_value={"update_id": 1}),
                )
            )
            server.enable_webhook_route()
            accepted = await handler(
                SimpleNamespace(
                    headers={
                        "X-Telegram-Bot-Api-Secret-Token": WEBHOOK_SECRET
                    },
                    json=AsyncMock(return_value={"update_id": 2}),
                )
            )
            for _ in range(10):
                if dispatcher.feed_raw_update.await_count:
                    break
                await asyncio.sleep(0)

            self.assertEqual(rejected.status, 401)
            self.assertEqual(accepted.status, 200)
            dispatcher.feed_raw_update.assert_awaited_once_with(
                bot=bot,
                update={"update_id": 2},
            )

            await server.disable_webhook_route()
            disabled = await handler(
                SimpleNamespace(
                    headers={
                        "X-Telegram-Bot-Api-Secret-Token": WEBHOOK_SECRET
                    },
                    json=AsyncMock(return_value={"update_id": 3}),
                )
            )
            self.assertEqual(disabled.status, 503)
            self.assertEqual(dispatcher.feed_raw_update.await_count, 1)
        finally:
            await server.disable_webhook_route()
            await bot.session.close()


class WebhookOrphanHealthTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def _queue() -> _WebhookUpdateQueue:
        return _WebhookUpdateQueue(
            dispatcher=Dispatcher(),
            bot=SimpleNamespace(),  # type: ignore[arg-type]
            session_factory=None,
            secret_token=WEBHOOK_SECRET,
            worker_count=1,
            critical_worker_count=1,
            security_worker_count=1,
        )

    async def test_untrusted_command_flood_cannot_consume_emergency_lane(self) -> None:
        queue = self._queue()
        user_id = 987654
        scope = (-100, user_id)
        update_delivery._TRUSTED_PRIVILEGED_OPERATORS.pop(scope, None)
        classifications = [
            queue._classification_for_update(
                {
                    "update_id": update_id,
                    "message": {
                        "text": "/ban 42",
                        "from": {"id": user_id},
                        "chat": {"id": -100},
                    },
                }
            )
            for update_id in range(1, 8)
        ]
        self.assertEqual(
            [priority for priority, _auth in classifications],
            [ExecutionPriority.NORMAL] * 7,
        )
        self.assertEqual(
            [auth for _priority, auth in classifications],
            [True, True, True, False, False, False, False],
        )
        self.assertEqual(queue._demoted_untrusted_auth_updates, 4)
        update_delivery.mark_privileged_operator(user_id, group_id=-100)
        self.assertEqual(
            queue._priority_for_update(
                {
                    "update_id": 8,
                    "message": {
                        "text": "/ban 42",
                        "from": {"id": user_id},
                        "chat": {"id": -100},
                    },
                }
            ),
            ExecutionPriority.CRITICAL,
        )
        self.assertEqual(
            queue._priority_for_update(
                {
                    "update_id": 9,
                    "message": {
                        "text": "/ban 42",
                        "from": {"id": user_id},
                        "chat": {"id": -200},
                    },
                }
            ),
            ExecutionPriority.NORMAL,
        )

    async def test_cached_priority_and_auth_classification_are_atomic(self) -> None:
        queue = self._queue()
        user_id = 987655
        group_id = -300
        update = {
            "update_id": 100,
            "message": {
                "text": "/unban 42",
                "from": {"id": user_id},
                "chat": {"id": group_id},
            },
        }
        update_delivery.unmark_privileged_operator(user_id, group_id=group_id)
        self.assertEqual(
            queue._classification_for_update(update),
            (ExecutionPriority.NORMAL, True),
        )
        update_delivery.mark_privileged_operator(user_id, group_id=group_id)
        self.assertEqual(
            queue._classification_for_update(update),
            (ExecutionPriority.NORMAL, True),
        )

        trusted_update = {**update, "update_id": 101}
        self.assertEqual(
            queue._classification_for_update(trusted_update),
            (ExecutionPriority.CRITICAL, False),
        )
        update_delivery.unmark_privileged_operator(user_id, group_id=group_id)
        self.assertEqual(
            queue._classification_for_update(trusted_update),
            (ExecutionPriority.CRITICAL, False),
        )

    async def test_auth_queue_pressure_is_reported_without_global_restart_issue(
        self,
    ) -> None:
        queue = _WebhookUpdateQueue(
            dispatcher=Dispatcher(),
            bot=SimpleNamespace(),  # type: ignore[arg-type]
            session_factory=None,
            secret_token=WEBHOOK_SECRET,
            worker_count=1,
            auth_worker_count=1,
            critical_worker_count=1,
            security_worker_count=1,
            auth_queue_capacity=1,
        )
        queued = verify_web_module._QueuedWebhookUpdate(
            update={"update_id": 200},
            update_id=200,
            enqueued_at=time.monotonic(),
            result=asyncio.get_running_loop().create_future(),
            auth_candidate=True,
        )
        queue.queue.put_nowait(queued)
        snapshot = queue.health_snapshot()
        self.assertTrue(snapshot["auth_queue_saturated"])
        self.assertIsNone(snapshot["issue"])
        self.assertEqual(await queue._discard_queued_updates(), 1)

    async def test_stale_orphan_sets_fatal_and_reports_age(self) -> None:
        queue = self._queue()
        release = asyncio.Event()
        task = asyncio.create_task(release.wait())
        await asyncio.sleep(0)
        queue._track_orphan(task)
        queue._orphaned_started_at[task] = (
            time.monotonic()
            - verify_web_module._WEBHOOK_ORPHAN_FATAL_AGE_SECONDS
            - 1.0
        )

        self.assertIn("orphaned webhook update", queue.fatal_issue())
        snapshot = queue.health_snapshot()
        self.assertFalse(snapshot["ok"])
        self.assertIn("orphaned webhook update", snapshot["issue"])
        self.assertGreaterEqual(
            snapshot["oldest_orphaned_update_age_seconds"],
            verify_web_module._WEBHOOK_ORPHAN_FATAL_AGE_SECONDS,
        )
        self.assertIsNotNone(queue.fatal_issue())

        release.set()
        await asyncio.wait_for(task, timeout=0.2)
        await asyncio.sleep(0)
        self.assertEqual(queue._orphaned_started_at, {})

    async def test_stale_late_finalizer_sets_fatal_and_reports_age(self) -> None:
        queue = self._queue()
        release = asyncio.Event()
        task = asyncio.create_task(release.wait())
        queue._late_finalizers[42] = task
        queue._late_finalizer_started_at[42] = (
            time.monotonic()
            - verify_web_module._WEBHOOK_LATE_FINALIZER_FATAL_AGE_SECONDS
            - 1.0
        )

        self.assertIn("late finalizer", queue.fatal_issue())
        snapshot = queue.health_snapshot()
        self.assertFalse(snapshot["ok"])
        self.assertIn("late finalizer", snapshot["issue"])
        self.assertGreaterEqual(
            snapshot["oldest_late_finalizer_age_seconds"],
            verify_web_module._WEBHOOK_LATE_FINALIZER_FATAL_AGE_SECONDS,
        )

        release.set()
        await asyncio.wait_for(task, timeout=0.2)
        queue._late_finalizers.clear()
        queue._late_finalizer_started_at.clear()


class MainLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def test_bootstrap_validation_runs_before_database_initialization(self) -> None:
        from bot import __main__ as bot_main

        cases = (
            (_settings(bot_token=""), "BOT_TOKEN"),
            (_settings(bot_token="your_bot_token_here"), "BOT_TOKEN"),
            (_settings(super_admin_id=0), "SUPER_ADMIN_ID"),
            (_settings(config_master_key=""), "CONFIG_MASTER_KEY"),
        )
        for settings, expected in cases:
            with self.subTest(expected=expected):
                init_db = AsyncMock()
                with (
                    patch(
                        "bot.__main__.load_bootstrap_settings",
                        return_value=settings,
                    ),
                    patch("bot.__main__.init_db", new=init_db),
                ):
                    with self.assertRaisesRegex(ValueError, expected):
                        await bot_main.main()
                init_db.assert_not_awaited()

    async def _assert_startup_failure_has_no_background_runners(
        self,
        stage: str,
    ) -> None:
        from bot import __main__ as bot_main

        settings = _settings(webhook_url="", webhook_secret="")
        engine = SimpleNamespace(dispose=AsyncMock())
        session_factory = object()
        runtime_config = SimpleNamespace(
            set_apply_callback=Mock(),
            config=SimpleNamespace(
                logging=SimpleNamespace(model_dump=Mock(return_value={}))
            ),
        )
        llm = SimpleNamespace(reconfigure=Mock())
        memory = SimpleNamespace(reconfigure=Mock())
        bot = SimpleNamespace(
            me=AsyncMock(
                return_value=SimpleNamespace(
                    id=42,
                    username="test_bot",
                    full_name="Test Bot",
                    first_name="Test",
                )
            ),
            session=SimpleNamespace(close=AsyncMock()),
        )
        proactive = SimpleNamespace(run_forever=AsyncMock())
        sweeper = SimpleNamespace(run_forever=AsyncMock(), check_interval_seconds=5.0)
        patrol = SimpleNamespace(run_forever=AsyncMock(), shutdown=AsyncMock())
        scheduled = SimpleNamespace(run_forever=AsyncMock())
        permissions = SimpleNamespace(run_forever=AsyncMock())
        raid = SimpleNamespace(
            restore_manual_lockdowns=AsyncMock(),
            shutdown=AsyncMock(),
        )
        telegram_cleanup = SimpleNamespace(
            start=AsyncMock(),
            stop=AsyncMock(),
            monitor=Mock(),
        )
        verify_web = SimpleNamespace(
            start=AsyncMock(),
            stop=AsyncMock(),
            quiesce_update_processor=AsyncMock(return_value=True),
            stop_update_processor=AsyncMock(),
            webhook_route_error=None,
            enable_webhook_route=Mock(),
            mark_webhook_active=Mock(),
            disable_webhook_route=AsyncMock(),
            mark_polling_active=Mock(),
            webhook_runtime_failure=Mock(return_value=None),
        )
        if stage == "restore":
            raid.restore_manual_lockdowns.side_effect = RuntimeError("restore failed")
        elif stage == "web":
            verify_web.start.side_effect = RuntimeError("web start failed")

        proactive_constructor = (
            patch(
                "bot.__main__.ProactiveTopicService",
                side_effect=RuntimeError("construction failed"),
            )
            if stage == "construction"
            else patch("bot.__main__.ProactiveTopicService", return_value=proactive)
        )
        patchers = [
            patch("bot.__main__.load_bootstrap_settings", return_value=settings),
            patch(
                "bot.__main__.init_db",
                new=AsyncMock(return_value=(engine, session_factory)),
            ),
            patch(
                "bot.__main__._initialize_runtime_services",
                new=AsyncMock(return_value=(runtime_config, llm, memory)),
            ),
            patch("bot.__main__.dp", new=_MainDispatcher()),
            patch("bot.__main__.create_bot", return_value=bot),
            patch("bot.__main__.restore_vote_ban_tasks", new=AsyncMock()),
            patch(
                "bot.__main__.warm_privileged_operator_cache",
                new=AsyncMock(return_value=0),
            ),
            patch(
                "bot.__main__.TelegramCleanupScheduler",
                return_value=telegram_cleanup,
            ),
            patch("bot.__main__.configure_telegram_cleanup_scheduler"),
            proactive_constructor,
            patch("bot.__main__.JoinVerificationSweeper", return_value=sweeper),
            patch("bot.__main__.PatrolService", return_value=patrol),
            patch("bot.__main__.ScheduledMessageService", return_value=scheduled),
            patch("bot.__main__.GroupPermissionService", return_value=permissions),
            patch("bot.__main__.RaidGuardService", return_value=raid),
            patch("bot.__main__.init_patrol_service"),
            patch("bot.__main__.init_group_permission_service"),
            patch("bot.__main__.init_raid_guard_service"),
            patch(
                "bot.__main__.resolve_webhook_config",
                return_value=(None, "not configured"),
            ),
            patch("bot.services.verify_web.VerifyWebServer", return_value=verify_web),
            patch("bot.__main__.flush_update_delivery_tasks", new=AsyncMock()),
            patch("bot.__main__.flush_skill_execution_tasks", new=AsyncMock()),
            patch("bot.__main__.flush_llm_request_tasks", new=AsyncMock()),
            patch("bot.__main__.flush_vote_ban_tasks", new=AsyncMock()),
            patch("bot.__main__.flush_kick_cleanup_tasks", new=AsyncMock()),
            patch("bot.__main__.flush_telegram_background_tasks", new=AsyncMock()),
            patch("bot.__main__.group.flush_pending_inbound_batches", new=AsyncMock()),
        ]
        with ExitStack() as stack:
            for patcher in patchers:
                stack.enter_context(patcher)
            with self.assertRaisesRegex(RuntimeError, "failed"):
                await bot_main.main()

        proactive.run_forever.assert_not_called()
        sweeper.run_forever.assert_not_called()
        patrol.run_forever.assert_not_called()
        scheduled.run_forever.assert_not_called()
        permissions.run_forever.assert_not_called()
        telegram_cleanup.start.assert_awaited_once()
        telegram_cleanup.stop.assert_awaited_once()
        bot.session.close.assert_awaited_once()
        engine.dispose.assert_awaited_once()

    async def test_service_construction_failure_cleans_without_starting_runners(self) -> None:
        await self._assert_startup_failure_has_no_background_runners("construction")

    async def test_raid_restore_failure_cleans_without_starting_runners(self) -> None:
        await self._assert_startup_failure_has_no_background_runners("restore")

    async def test_web_start_failure_cleans_without_starting_runners(self) -> None:
        await self._assert_startup_failure_has_no_background_runners("web")

    async def test_cleanup_scheduler_stays_live_through_pending_reply_flush(self) -> None:
        from bot import __main__ as bot_main

        events: list[str] = []
        settings = _settings(webhook_url="", webhook_secret="")
        engine = SimpleNamespace(
            dispose=AsyncMock(side_effect=lambda: events.append("engine"))
        )
        session_factory = object()
        runtime_config = SimpleNamespace(
            set_apply_callback=Mock(),
            config=SimpleNamespace(
                logging=SimpleNamespace(model_dump=Mock(return_value={}))
            ),
        )
        llm = SimpleNamespace(reconfigure=Mock())
        memory = SimpleNamespace(reconfigure=Mock())
        bot = SimpleNamespace(
            me=AsyncMock(
                return_value=SimpleNamespace(
                    id=42,
                    username="test_bot",
                    full_name="Test Bot",
                    first_name="Test",
                )
            ),
            session=SimpleNamespace(
                close=AsyncMock(side_effect=lambda: events.append("bot-close"))
            ),
        )

        async def forever() -> None:
            await asyncio.Future()

        cleanup = SimpleNamespace(
            start=AsyncMock(side_effect=lambda: events.append("cleanup-start")),
            stop=AsyncMock(side_effect=lambda: events.append("cleanup-stop")),
            monitor=forever,
        )
        proactive = SimpleNamespace(run_forever=forever)
        sweeper = SimpleNamespace(run_forever=forever, check_interval_seconds=5.0)
        patrol = SimpleNamespace(run_forever=forever, shutdown=AsyncMock())
        scheduled = SimpleNamespace(run_forever=forever)
        permissions = SimpleNamespace(run_forever=forever)
        raid = SimpleNamespace(
            restore_manual_lockdowns=AsyncMock(),
            shutdown=AsyncMock(),
        )
        verify_web = SimpleNamespace(
            start=AsyncMock(),
            stop=AsyncMock(side_effect=lambda: events.append("web-stop")),
            quiesce_update_processor=AsyncMock(
                side_effect=lambda: events.append("processor-quiesce") or True
            ),
            stop_update_processor=AsyncMock(
                side_effect=lambda: events.append("processor-stop")
            ),
            webhook_route_error=None,
            enable_webhook_route=Mock(),
            mark_webhook_active=Mock(),
            disable_webhook_route=AsyncMock(),
            mark_polling_active=Mock(),
            webhook_runtime_failure=Mock(return_value=None),
        )
        dispatcher = _MainDispatcher()
        verify_constructor = Mock(return_value=verify_web)

        patchers = [
            patch("bot.__main__.load_bootstrap_settings", return_value=settings),
            patch(
                "bot.__main__.init_db",
                new=AsyncMock(return_value=(engine, session_factory)),
            ),
            patch(
                "bot.__main__._initialize_runtime_services",
                new=AsyncMock(return_value=(runtime_config, llm, memory)),
            ),
            patch("bot.__main__.dp", new=dispatcher),
            patch("bot.__main__.create_bot", return_value=bot),
            patch("bot.__main__.TelegramCleanupScheduler", return_value=cleanup),
            patch("bot.__main__.configure_telegram_cleanup_scheduler"),
            patch("bot.__main__.restore_vote_ban_tasks", new=AsyncMock()),
            patch(
                "bot.__main__.warm_privileged_operator_cache",
                new=AsyncMock(return_value=0),
            ),
            patch("bot.__main__.ProactiveTopicService", return_value=proactive),
            patch("bot.__main__.JoinVerificationSweeper", return_value=sweeper),
            patch("bot.__main__.PatrolService", return_value=patrol),
            patch("bot.__main__.ScheduledMessageService", return_value=scheduled),
            patch("bot.__main__.GroupPermissionService", return_value=permissions),
            patch("bot.__main__.RaidGuardService", return_value=raid),
            patch("bot.__main__.init_patrol_service"),
            patch("bot.__main__.init_group_permission_service"),
            patch("bot.__main__.init_raid_guard_service"),
            patch(
                "bot.__main__.resolve_webhook_config",
                return_value=(None, "not configured"),
            ),
            patch(
                "bot.services.verify_web.VerifyWebServer",
                new=verify_constructor,
            ),
            patch(
                "bot.__main__.run_update_delivery",
                new=AsyncMock(return_value="stopped"),
            ),
            patch("bot.__main__.verification_service_ready", return_value=False),
            patch("bot.__main__.flush_update_delivery_tasks", new=AsyncMock()),
            patch("bot.__main__.flush_skill_execution_tasks", new=AsyncMock()),
            patch("bot.__main__.flush_llm_request_tasks", new=AsyncMock()),
            patch("bot.__main__.flush_vote_ban_tasks", new=AsyncMock()),
            patch("bot.__main__.flush_kick_cleanup_tasks", new=AsyncMock()),
            patch("bot.__main__.flush_telegram_background_tasks", new=AsyncMock()),
            patch(
                "bot.__main__.group.flush_pending_inbound_batches",
                new=AsyncMock(side_effect=lambda: events.append("pending-flush")),
            ),
        ]
        with ExitStack() as stack:
            for patcher in patchers:
                stack.enter_context(patcher)
            await bot_main.main()

        self.assertLess(events.index("cleanup-start"), events.index("pending-flush"))
        self.assertIs(
            verify_constructor.call_args.kwargs["webhook_dispatcher"],
            dispatcher,
        )
        self.assertLess(
            events.index("processor-quiesce"),
            events.index("pending-flush"),
        )
        self.assertLess(events.index("pending-flush"), events.index("processor-stop"))
        self.assertLess(events.index("pending-flush"), events.index("cleanup-stop"))
        self.assertLess(events.index("cleanup-stop"), events.index("bot-close"))
        self.assertLess(events.index("bot-close"), events.index("engine"))

    async def test_background_failure_cancels_update_delivery_and_surfaces(self) -> None:
        from bot import __main__ as bot_main

        delivery_cancelled = asyncio.Event()

        async def delivery() -> str:
            try:
                await asyncio.Future()
            finally:
                delivery_cancelled.set()

        async def fail_background() -> None:
            raise RuntimeError("service crashed")

        background = asyncio.create_task(
            fail_background(),
            name="test-background-service",
        )
        with self.assertRaisesRegex(RuntimeError, "test-background-service"):
            await bot_main._run_with_background_supervision(
                delivery(),
                background_tasks=[background],
            )

        self.assertTrue(delivery_cancelled.is_set())

    async def test_cleanup_timeout_does_not_wait_forever_for_cancel_resistant_task(self) -> None:
        from bot import __main__ as bot_main

        release = asyncio.Event()

        async def cancel_resistant_cleanup() -> None:
            try:
                await asyncio.Future()
            except asyncio.CancelledError:
                await release.wait()

        try:
            with patch.object(bot_main, "_CANCELLATION_GRACE_SECONDS", 0.01):
                await asyncio.wait_for(
                    bot_main._await_cleanup_bounded(
                        cancel_resistant_cleanup(),
                        label="test cleanup",
                        timeout=0.01,
                    ),
                    timeout=0.25,
                )
        finally:
            release.set()
            await asyncio.sleep(0)


if __name__ == "__main__":
    unittest.main()
