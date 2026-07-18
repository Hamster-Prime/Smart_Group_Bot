import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from aiogram import Bot, Dispatcher

from bot.config import Settings
from bot.services import update_delivery
from bot.services.update_delivery import (
    resolve_webhook_config,
    run_update_delivery,
)
from bot.services.verify_web import VerifyWebServer

WEBHOOK_SECRET = "s" * 32


def _settings(**values: object) -> Settings:
    settings = Settings(_env_file=None, **values)
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


class UpdateDeliveryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.probe_patcher = patch(
            "bot.services.update_delivery._probe_webhook_endpoint",
            new=AsyncMock(return_value=None),
        )
        self.probe_patcher.start()

    async def asyncTearDown(self) -> None:
        self.probe_patcher.stop()

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
        )
        logs = "\n".join(captured.output)
        self.assertIn("未配置 WEBHOOK_URL", logs)
        self.assertIn("自动降级为轮询", logs)

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
            drop_pending_updates=True,
            secret_token=WEBHOOK_SECRET,
            max_connections=8,
        )
        dispatcher.emit_startup.assert_awaited_once()
        dispatcher.emit_shutdown.assert_awaited_once()
        bot.delete_webhook.assert_awaited_once_with(drop_pending_updates=True)
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
            [True, False],
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
            [True, False],
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

    async def test_watchdog_reports_new_telegram_delivery_error(self) -> None:
        settings = _settings(
            webhook_url="https://bot.example.com/telegram/webhook",
            webhook_secret=WEBHOOK_SECRET,
        )
        webhook, _reason = resolve_webhook_config(settings)
        assert webhook is not None
        bot = SimpleNamespace(
            get_webhook_info=AsyncMock(
                return_value=SimpleNamespace(
                    url=webhook.url,
                    last_error_date=123,
                    last_error_message="Wrong response: 404 Not Found",
                )
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
                disable_webhook_route=lambda: events.append("disable"),
        )

        self.assertEqual(result.failure_reason, "delivery failed")
        self.assertEqual(
            events,
            ["clear", "startup", "enable", "set", "get", "disable", "shutdown"],
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
        bot.delete_webhook.assert_awaited_once_with(drop_pending_updates=True)

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
            [True, False],
        )
        dispatcher.start_polling.assert_awaited_once()


class WebhookRouteTests(unittest.IsolatedAsyncioTestCase):
    async def test_webhook_route_registration_error_is_exposed_for_fallback(self) -> None:
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
            app = server.build_app()

        self.assertIsNotNone(app)
        self.assertIn("invalid route", server.webhook_route_error or "")

    async def test_disabling_route_drains_updates_already_acknowledged(self) -> None:
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
        try:
            response = await handler(
                SimpleNamespace(
                    headers={
                        "X-Telegram-Bot-Api-Secret-Token": WEBHOOK_SECRET
                    },
                    json=AsyncMock(return_value={"update_id": 1}),
                )
            )
            self.assertEqual(response.status, 200)
            await asyncio.wait_for(started.wait(), timeout=1.0)

            disable_task = asyncio.create_task(server.disable_webhook_route())
            await asyncio.sleep(0)
            self.assertFalse(disable_task.done())
            release.set()
            await asyncio.wait_for(disable_task, timeout=1.0)
            dispatcher.feed_raw_update.assert_awaited_once()
        finally:
            release.set()
            await server.disable_webhook_route()
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
            overflow_responses = await asyncio.gather(
                *(handler(request) for request in overflow_requests)
            )
            self.assertEqual(
                [response.status for response in overflow_responses].count(503),
                1,
            )

            release.set()
            initial_responses = await asyncio.gather(*initial_tasks)
            accepted_count = initial_count + queued_count
            for _ in range(100):
                if dispatcher.feed_raw_update.await_count == accepted_count:
                    break
                await asyncio.sleep(0)

            self.assertTrue(
                all(response.status == 200 for response in initial_responses)
            )
            self.assertEqual(dispatcher.feed_raw_update.await_count, accepted_count)
            self.assertEqual(
                max_active,
                update_delivery.WEBHOOK_MAX_CONCURRENT_UPDATES,
            )
        finally:
            release.set()
            await asyncio.gather(*initial_tasks, return_exceptions=True)
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


if __name__ == "__main__":
    unittest.main()
