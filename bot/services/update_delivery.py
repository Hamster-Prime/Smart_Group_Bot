from __future__ import annotations

import asyncio
import logging
import re
import signal
from contextlib import suppress
from dataclasses import dataclass, field
from typing import Awaitable, Callable
from urllib.parse import urlsplit, urlunsplit

import aiohttp
from aiogram import Bot, Dispatcher

from bot.config import Settings

log = logging.getLogger(__name__)

_WEBHOOK_SECRET_RE = re.compile(r"^[A-Za-z0-9_-]{32,256}$")
_WEBHOOK_PATH_RE = re.compile(r"^/(?:[A-Za-z0-9_-]+/)*[A-Za-z0-9_-]+/?$")
_RESERVED_WEBHOOK_PATHS = {
    "/api",
    "/healthz",
    "/settings",
    "/settings-assets",
    "/verify",
}
_RESERVED_WEBHOOK_PREFIXES = ("/api/", "/settings-assets/")
WEBHOOK_MAX_CONCURRENT_UPDATES = 8
_WEBHOOK_WATCH_INTERVAL_SECONDS = 15.0
_DELETE_WEBHOOK_BACKOFF_SECONDS = (1.0, 2.0, 5.0, 10.0, 30.0)
_WEBHOOK_PROBE_TIMEOUT_SECONDS = 8.0

_RouteHook = Callable[[], Awaitable[None] | None]


@dataclass(frozen=True, slots=True)
class WebhookConfig:
    url: str
    path: str
    secret: str = field(repr=False)


@dataclass(frozen=True, slots=True)
class _WebhookRunResult:
    failure_reason: str | None = None
    preserve_pending_updates: bool = False


def resolve_webhook_config(settings: Settings) -> tuple[WebhookConfig | None, str | None]:
    """Validate optional webhook bootstrap settings without aborting startup."""
    raw_url = settings.webhook_url.strip()
    secret = settings.webhook_secret.strip()
    if not raw_url:
        return None, "未配置 WEBHOOK_URL"
    if not secret:
        return None, "已配置 WEBHOOK_URL，但缺少 WEBHOOK_SECRET"
    if len(raw_url) > 256:
        return None, "WEBHOOK_URL 不能超过 256 个字符"

    try:
        parsed = urlsplit(raw_url)
        explicit_port = parsed.port
    except ValueError:
        return None, "WEBHOOK_URL 格式无效"

    if parsed.scheme.lower() != "https":
        return None, "WEBHOOK_URL 必须使用 HTTPS"
    if not parsed.hostname:
        return None, "WEBHOOK_URL 缺少有效域名"
    if parsed.username or parsed.password:
        return None, "WEBHOOK_URL 不允许包含用户名或密码"
    if explicit_port is not None and explicit_port not in {443, 80, 88, 8443}:
        return None, "WEBHOOK_URL 端口必须为 443、80、88 或 8443"
    if parsed.query or parsed.fragment:
        return None, "WEBHOOK_URL 不允许包含查询参数或片段"

    path = parsed.path or "/"
    if not _WEBHOOK_PATH_RE.fullmatch(path):
        return None, "WEBHOOK_URL 路径仅允许字母、数字、下划线、连字符和斜杠"
    if path in _RESERVED_WEBHOOK_PATHS or path.startswith(_RESERVED_WEBHOOK_PREFIXES):
        return None, f"WEBHOOK_URL 路径 {path} 与内置 HTTP 路由冲突"
    if not _WEBHOOK_SECRET_RE.fullmatch(secret):
        return None, "WEBHOOK_SECRET 需为 32-256 位字母、数字、下划线或连字符"

    normalized_url = urlunsplit(
        (
            "https",
            parsed.netloc.lower(),
            path,
            "",
            "",
        )
    )
    return WebhookConfig(url=normalized_url, path=path, secret=secret), None


async def run_update_delivery(
    *,
    bot: Bot,
    dispatcher: Dispatcher,
    settings: Settings,
    webhook: WebhookConfig | None,
    fallback_reason: str | None = None,
    enable_webhook_route: _RouteHook | None = None,
    disable_webhook_route: _RouteHook | None = None,
) -> str:
    """Run webhook delivery when healthy, otherwise fall back to polling."""
    allowed_updates = dispatcher.resolve_used_update_types()
    polling_drop_pending_updates = settings.bot.drop_pending_updates

    if webhook is not None:
        try:
            result = await _run_webhook_session(
                dispatcher=dispatcher,
                bot=bot,
                settings=settings,
                webhook=webhook,
                allowed_updates=allowed_updates,
                enable_webhook_route=enable_webhook_route,
                disable_webhook_route=disable_webhook_route,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            polling_drop_pending_updates = False
            fallback_reason = f"Webhook 生命周期失败：{exc}"
            log.error("%s；自动降级为轮询。", fallback_reason, exc_info=True)
        else:
            if result.failure_reason is None:
                return "webhook"
            if result.preserve_pending_updates:
                polling_drop_pending_updates = False
            fallback_reason = result.failure_reason
            log.error("%s；自动降级为轮询。", fallback_reason)
    else:
        log.warning("%s；自动降级为轮询。", fallback_reason or "Webhook 配置不可用")

    try:
        await _invoke_route_hook(disable_webhook_route)
    except Exception:
        log.exception("关闭 webhook HTTP 路由失败；仍将尝试切换轮询。")
    await _run_polling(
        dispatcher=dispatcher,
        bot=bot,
        drop_pending_updates=polling_drop_pending_updates,
        reason=fallback_reason or "Webhook 配置不可用",
        allowed_updates=allowed_updates,
    )
    return "polling"


async def _run_polling(
    *,
    dispatcher: Dispatcher,
    bot: Bot,
    drop_pending_updates: bool,
    reason: str,
    allowed_updates: list[str],
) -> None:
    await _delete_webhook_for_polling(
        bot=bot,
        drop_pending_updates=drop_pending_updates,
    )
    log.info("Telegram 更新接收模式：轮询 | 原因=%s", reason)
    await dispatcher.start_polling(
        bot,
        allowed_updates=allowed_updates,
        handle_as_tasks=True,
        tasks_concurrency_limit=8,
    )


async def _delete_webhook_for_polling(
    *,
    bot: Bot,
    drop_pending_updates: bool,
) -> None:
    attempt = 0
    while True:
        attempt += 1
        try:
            deleted = await bot.delete_webhook(
                drop_pending_updates=drop_pending_updates
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            issue = f"调用 deleteWebhook 失败：{exc}"
        else:
            if deleted:
                return
            issue = "Telegram deleteWebhook 返回 false"

        delay = _DELETE_WEBHOOK_BACKOFF_SECONDS[
            min(attempt - 1, len(_DELETE_WEBHOOK_BACKOFF_SECONDS) - 1)
        ]
        log.warning(
            "%s；尚不能启动轮询，将在 %.1f 秒后重试（第 %d 次）。",
            issue,
            delay,
            attempt,
        )
        await asyncio.sleep(delay)


async def _run_webhook_session(
    *,
    dispatcher: Dispatcher,
    bot: Bot,
    settings: Settings,
    webhook: WebhookConfig,
    allowed_updates: list[str],
    enable_webhook_route: _RouteHook | None,
    disable_webhook_route: _RouteHook | None,
) -> _WebhookRunResult:
    probe_issue = await _probe_webhook_endpoint(webhook)
    if probe_issue is not None:
        return _WebhookRunResult(
            failure_reason=f"Webhook 公网端点自检失败：{probe_issue}"
        )

    try:
        deleted = await bot.delete_webhook(
            drop_pending_updates=settings.bot.drop_pending_updates
        )
        if not deleted:
            raise RuntimeError("Telegram deleteWebhook 返回 false")
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        return _WebhookRunResult(
            failure_reason=f"Webhook 注册前清理旧地址失败：{exc}"
        )

    workflow_data = {
        "dispatcher": dispatcher,
        **dispatcher.workflow_data,
    }
    workflow_data.pop("bot", None)
    await dispatcher.emit_startup(bot=bot, **workflow_data)
    try:
        await _invoke_route_hook(enable_webhook_route)
        registration_attempted = False
        try:
            registration_attempted = True
            registered = await bot.set_webhook(
                url=webhook.url,
                allowed_updates=allowed_updates,
                drop_pending_updates=settings.bot.drop_pending_updates,
                secret_token=webhook.secret,
                max_connections=WEBHOOK_MAX_CONCURRENT_UPDATES,
            )
            if not registered:
                raise RuntimeError("Telegram setWebhook 返回 false")

            info = await bot.get_webhook_info()
            actual_url = str(info.url or "").strip()
            if actual_url != webhook.url:
                raise RuntimeError("Telegram 返回的 webhook URL 与配置不一致")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            return _WebhookRunResult(
                failure_reason=f"Webhook 注册或校验失败：{exc}",
                preserve_pending_updates=registration_attempted,
            )

        log.info(
            "Telegram 更新接收模式：webhook | url=%s | path=%s",
            webhook.url,
            webhook.path,
        )
        try:
            runtime_failure = await _wait_for_webhook_exit(
                bot=bot,
                webhook=webhook,
                initial_error_date=getattr(info, "last_error_date", None),
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            return _WebhookRunResult(
                failure_reason=f"Webhook 运行失败：{exc}",
                preserve_pending_updates=True,
            )
        return _WebhookRunResult(
            failure_reason=runtime_failure,
            preserve_pending_updates=runtime_failure is not None,
        )
    finally:
        try:
            await _invoke_route_hook(disable_webhook_route)
        finally:
            await dispatcher.emit_shutdown(bot=bot, **workflow_data)


async def _probe_webhook_endpoint(webhook: WebhookConfig) -> str | None:
    invalid_secret = "smart_group_bot_webhook_probe"
    if invalid_secret == webhook.secret:
        invalid_secret += "_invalid"
    timeout = aiohttp.ClientTimeout(total=_WEBHOOK_PROBE_TIMEOUT_SECONDS)
    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(
                webhook.url,
                json={"update_id": 0},
                headers={
                    "X-Telegram-Bot-Api-Secret-Token": invalid_secret,
                    "User-Agent": "Smart-Group-Bot-Webhook-Probe",
                },
            ) as response:
                body = (await response.text()).strip()
                if response.status == 401 and body == "Unauthorized":
                    return None
                return f"公网地址返回 HTTP {response.status}，未命中 bot webhook 路由"
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        return f"无法访问 WEBHOOK_URL：{exc}"


async def _invoke_route_hook(hook: _RouteHook | None) -> None:
    if hook is None:
        return
    result = hook()
    if result is not None:
        await result


async def _wait_for_webhook_exit(
    *,
    bot: Bot,
    webhook: WebhookConfig,
    initial_error_date: object | None,
) -> str | None:
    shutdown_task = asyncio.create_task(
        _wait_for_shutdown_signal(),
        name="webhook-shutdown-waiter",
    )
    watchdog_task = asyncio.create_task(
        _watch_webhook(
            bot=bot,
            webhook=webhook,
            initial_error_date=initial_error_date,
        ),
        name="webhook-health-watchdog",
    )
    tasks = {shutdown_task, watchdog_task}
    try:
        done, _pending = await asyncio.wait(
            tasks,
            return_when=asyncio.FIRST_COMPLETED,
        )
        if shutdown_task in done:
            await shutdown_task
            return None
        return await watchdog_task
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


async def _watch_webhook(
    *,
    bot: Bot,
    webhook: WebhookConfig,
    initial_error_date: object | None,
) -> str:
    last_error_date = initial_error_date
    while True:
        await asyncio.sleep(_WEBHOOK_WATCH_INTERVAL_SECONDS)
        try:
            info = await bot.get_webhook_info()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning("Webhook 状态检查失败，保持 webhook 模式并稍后重试：%s", exc)
            continue

        actual_url = str(info.url or "").strip()
        if actual_url != webhook.url:
            return "Telegram webhook 注册地址已变化"

        current_error_date = getattr(info, "last_error_date", None)
        if current_error_date is not None and current_error_date != last_error_date:
            message = str(
                getattr(info, "last_error_message", None) or "Telegram 未提供详细原因"
            ).replace("\n", " ")
            return f"Telegram webhook 投递失败：{message[:300]}"
        last_error_date = current_error_date


async def _wait_for_shutdown_signal() -> None:
    loop = asyncio.get_running_loop()
    stop_event = asyncio.Event()
    installed: list[signal.Signals] = []

    for sig in (signal.SIGINT, signal.SIGTERM):
        with suppress(NotImplementedError, RuntimeError, ValueError):
            loop.add_signal_handler(sig, stop_event.set)
            installed.append(sig)

    try:
        await stop_event.wait()
    finally:
        for sig in installed:
            with suppress(NotImplementedError, RuntimeError, ValueError):
                loop.remove_signal_handler(sig)


__all__ = [
    "WEBHOOK_MAX_CONCURRENT_UPDATES",
    "WebhookConfig",
    "resolve_webhook_config",
    "run_update_delivery",
]
