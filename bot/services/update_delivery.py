from __future__ import annotations

import asyncio
import logging
import re
import signal
import time
from contextlib import suppress
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable
from urllib.parse import urlsplit, urlunsplit

import aiohttp
from aiogram import Bot, Dispatcher
from aiogram.utils.backoff import Backoff, BackoffConfig

from bot.config import Settings
from bot.services.request_priority import ExecutionPriority

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
# Interactive administration and automatic membership enforcement have
# separate lanes.  A join flood may consume the security lane, but it cannot
# delay an operator's /ban, /unban or permission-management callback.
WEBHOOK_CRITICAL_CONCURRENT_UPDATES = 4
WEBHOOK_SECURITY_CONCURRENT_UPDATES = 4
WEBHOOK_AUTH_CONCURRENT_UPDATES = 2
WEBHOOK_CRITICAL_QUEUE_CAPACITY = 64
WEBHOOK_SECURITY_QUEUE_CAPACITY = 128
WEBHOOK_AUTH_QUEUE_CAPACITY = 64
TELEGRAM_AUTH_CANDIDATE_BURST = 3
_WEBHOOK_WATCH_INTERVAL_SECONDS = 15.0
_WEBHOOK_FAILURE_THRESHOLD = 3
_DELETE_WEBHOOK_BACKOFF_SECONDS = (1.0, 2.0, 5.0, 10.0, 30.0)
_DELETE_WEBHOOK_MAX_ATTEMPTS = 4
_WEBHOOK_PROBE_TIMEOUT_SECONDS = 8.0
_WEBHOOK_PROBE_ATTEMPTS = 3
_WEBHOOK_PROBE_BACKOFF_SECONDS = (0.5, 1.0)
_TELEGRAM_CONTROL_TIMEOUT_SECONDS = 10.0
_WEBHOOK_TASK_STOP_TIMEOUT_SECONDS = 5.0
_DISPATCHER_HOOK_TIMEOUT_SECONDS = 30.0
_POLLING_UPDATE_DRAIN_TIMEOUT_SECONDS = 15.0
_POLLING_UPDATE_CANCEL_GRACE_SECONDS = 2.0
_POLLING_STOP_TIMEOUT_SECONDS = 15.0
_POLLING_TIMEOUT_SECONDS = 15
_POLLING_HTTP_TIMEOUT_SECONDS = 30
_POLLING_REQUEST_TIMEOUT_SECONDS = 35.0
_POLLING_BACKOFF_CONFIG = BackoffConfig(
    min_delay=1.0,
    max_delay=10.0,
    factor=1.7,
    jitter=0.2,
)
_CONTROL_CANCELLATION_GRACE_SECONDS = 0.05

_CONTROL_ORPHAN_TASKS: set[asyncio.Task[Any]] = set()
_POLLING_SHUTDOWN = object()
_TRUSTED_PRIVILEGED_OPERATOR_TTL_SECONDS = 15 * 60.0
_TRUSTED_PRIVILEGED_OPERATORS: dict[tuple[int, int], float] = {}

_PRIVILEGED_COMMANDS = frozenset(
    {
        # Group and delegated-administrator authorization.
        "authgroup",
        "unauthgroup",
        "authlist",
        "authadmin",
        "unauthadmin",
        "adminlist",
        # Enforcement and emergency controls.
        "ban",
        "spam",
        "unban",
        "banlist",
        "voteban",
        "raid",
        "raidguard",
        "mute",
        "unmute",
        # Moderation-policy and warning controls.
        "rules",
        "clearwarnings",
        "clearwarning",
        "clearwarns",
        "clearwarn",
        "warnings",
        "aiexempt",
        "unaiexempt",
        # The settings entry point performs permission checks and is the
        # recovery surface for incorrectly configured enforcement policies.
        "settings",
    }
)
_SECURITY_COMMANDS = frozenset(
    {
        # Rule generation may execute multiple model/tool rounds. It remains
        # above ordinary chat but cannot occupy the emergency ban/unban lane.
        "addrule",
    }
)
_ADMIN_CALLBACK_PREFIXES = (
    "bsc:",  # local/global ban and unban scope
    "mact:",  # moderation action
    "rul:",  # moderation-rule pages
    "rud:",  # moderation-rule deletion
    "atl:",  # authorized-group pages
    "adl:",  # delegated-admin pages
    "wpl:",  # warning pages
)
_ADMIN_CALLBACK_EXACT = frozenset(
    {
        "rgr",  # remove raid-challenged users
    }
)
_SECURITY_CALLBACK_EXACT = frozenset(
    {
        "ptv",  # patrol member self-verification
        "rgv",  # raid member self-verification
    }
)
_MESSAGE_CONTAINER_KEYS = (
    "message",
    "edited_message",
    "channel_post",
    "edited_channel_post",
)


def _telegram_command_name(text: Any) -> str:
    """Return a normalized Bot API command without allocating a parser."""

    if not isinstance(text, str):
        return ""
    stripped = text.lstrip()
    if not stripped.startswith("/"):
        return ""
    token = stripped.split(None, 1)[0][1:]
    if not token:
        return ""
    return token.split("@", 1)[0].casefold()


def _telegram_field(container: Any, name: str) -> Any:
    if isinstance(container, dict):
        return container.get(name)
    return getattr(container, name, None)


def mark_privileged_operator(
    user_id: int,
    *,
    group_id: int = 0,
    ttl_seconds: float = _TRUSTED_PRIVILEGED_OPERATOR_TTL_SECONDS,
) -> None:
    """Trust an operator only in the scope that was actually authorized.

    ``group_id=0`` is reserved for the configured super administrator. Group
    and Telegram administrators must be cached with their concrete chat so a
    grant in one group cannot consume the emergency lane in every other group.
    """

    normalized = int(user_id or 0)
    if normalized <= 0:
        return
    scope = int(group_id or 0)
    key = (scope, normalized)
    now = time.monotonic()
    _TRUSTED_PRIVILEGED_OPERATORS[key] = now + max(1.0, float(ttl_seconds))
    if len(_TRUSTED_PRIVILEGED_OPERATORS) > 4096:
        for candidate, expires_at in tuple(_TRUSTED_PRIVILEGED_OPERATORS.items()):
            if expires_at <= now:
                _TRUSTED_PRIVILEGED_OPERATORS.pop(candidate, None)
        overflow = len(_TRUSTED_PRIVILEGED_OPERATORS) - 4096
        if overflow > 0:
            for candidate, _expires_at in sorted(
                _TRUSTED_PRIVILEGED_OPERATORS.items(),
                key=lambda item: item[1],
            )[:overflow]:
                _TRUSTED_PRIVILEGED_OPERATORS.pop(candidate, None)


def privileged_operator_is_trusted(user_id: int, *, group_id: int = 0) -> bool:
    normalized = int(user_id or 0)
    scope = int(group_id or 0)
    now = time.monotonic()
    for key in ((scope, normalized), (0, normalized)):
        expires_at = _TRUSTED_PRIVILEGED_OPERATORS.get(key, 0.0)
        if expires_at > now:
            return True
        if normalized:
            _TRUSTED_PRIVILEGED_OPERATORS.pop(key, None)
    return False


def unmark_privileged_operator(user_id: int, *, group_id: int = 0) -> None:
    _TRUSTED_PRIVILEGED_OPERATORS.pop(
        (int(group_id or 0), int(user_id or 0)),
        None,
    )


def clear_privileged_operator_group(group_id: int) -> None:
    scope = int(group_id or 0)
    for key in tuple(_TRUSTED_PRIVILEGED_OPERATORS):
        if key[0] == scope:
            _TRUSTED_PRIVILEGED_OPERATORS.pop(key, None)


def telegram_update_sender_id(update: Any) -> int:
    callback = _telegram_field(update, "callback_query")
    sender = _telegram_field(callback, "from") or _telegram_field(
        callback, "from_user"
    )
    for key in _MESSAGE_CONTAINER_KEYS:
        if sender is not None:
            break
        message = _telegram_field(update, key)
        sender = _telegram_field(message, "from") or _telegram_field(
            message, "from_user"
        )
    if sender is None:
        membership = _telegram_field(update, "chat_member") or _telegram_field(
            update, "my_chat_member"
        )
        sender = _telegram_field(membership, "from") or _telegram_field(
            membership, "from_user"
        )
    try:
        return int(_telegram_field(sender, "id") or 0)
    except (TypeError, ValueError):
        return 0


def telegram_update_chat_id(update: Any) -> int:
    callback = _telegram_field(update, "callback_query")
    callback_message = _telegram_field(callback, "message")
    chat = _telegram_field(callback_message, "chat")
    for key in _MESSAGE_CONTAINER_KEYS:
        if chat is not None:
            break
        message = _telegram_field(update, key)
        chat = _telegram_field(message, "chat")
    if chat is None:
        membership = _telegram_field(update, "chat_member") or _telegram_field(
            update, "my_chat_member"
        )
        chat = _telegram_field(membership, "chat")
    try:
        return int(_telegram_field(chat, "id") or 0)
    except (TypeError, ValueError):
        return 0


def telegram_message_is_privileged_candidate(message: Any) -> bool:
    """Whether a message text names an authorization-sensitive command.

    This predicate intentionally does not grant queue priority. It lets outer
    middleware avoid expensive profile LLM work before the command handler has
    authoritatively authenticated an otherwise unknown sender.
    """

    text = _telegram_field(message, "text")
    if not isinstance(text, str):
        text = _telegram_field(message, "caption")
    command = _telegram_command_name(text)
    return command in _PRIVILEGED_COMMANDS or command in _SECURITY_COMMANDS


def telegram_update_priority(update: Any) -> ExecutionPriority:
    """Cheaply classify a raw Telegram update before aiogram parsing.

    Classification reads only shallow Bot API fields from either a raw mapping
    or an aiogram model.  It runs before database access, so even a saturated
    normal lane can still admit security and permission work.
    """

    if update is None:
        return ExecutionPriority.NORMAL

    # Membership changes drive join verification, raid protection and admin
    # cache invalidation.  They are security work even when no command exists.
    if any(
        _telegram_field(update, key) is not None
        for key in ("chat_member", "my_chat_member", "chat_join_request")
    ):
        return ExecutionPriority.HIGH

    callback = _telegram_field(update, "callback_query")
    data = _telegram_field(callback, "data")
    if isinstance(data, str):
        normalized = data.casefold()
        trusted_operator = privileged_operator_is_trusted(
            telegram_update_sender_id(update),
            group_id=telegram_update_chat_id(update),
        )
        if normalized in _ADMIN_CALLBACK_EXACT or normalized.startswith(
            _ADMIN_CALLBACK_PREFIXES
        ):
            return (
                ExecutionPriority.CRITICAL
                if trusted_operator
                else ExecutionPriority.NORMAL
            )
        if normalized in _SECURITY_CALLBACK_EXACT:
            return ExecutionPriority.HIGH
        callback_parts = normalized.split(":", 2)
        if len(callback_parts) >= 2 and callback_parts[0] == "jv":
            # Starting/self-service verification is untrusted member traffic;
            # approve/reject are administrator decisions and retain the
            # operator-only lane. Handlers still perform authoritative checks.
            return (
                ExecutionPriority.CRITICAL
                if callback_parts[1] in {"a", "r"} and trusted_operator
                else (
                    ExecutionPriority.HIGH
                    if callback_parts[1] not in {"a", "r"}
                    else ExecutionPriority.NORMAL
                )
            )
        if len(callback_parts) >= 2 and callback_parts[0] == "vban":
            # Ordinary votes can be distributed across many users and must not
            # consume the emergency management lane. Admin cancel/direct-ban
            # actions remain critical and are re-authorized in the handler.
            return (
                ExecutionPriority.CRITICAL
                if callback_parts[1] in {"cancel", "ban"} and trusted_operator
                else (
                    ExecutionPriority.HIGH
                    if callback_parts[1] not in {"cancel", "ban"}
                    else ExecutionPriority.NORMAL
                )
            )

    for key in _MESSAGE_CONTAINER_KEYS:
        message = _telegram_field(update, key)
        if message is None:
            continue
        if (
            _telegram_field(message, "new_chat_members")
            or _telegram_field(message, "left_chat_member") is not None
        ):
            return ExecutionPriority.HIGH
        text = _telegram_field(message, "text")
        if not isinstance(text, str):
            text = _telegram_field(message, "caption")
        command = _telegram_command_name(text)
        if command in _PRIVILEGED_COMMANDS:
            return (
                ExecutionPriority.CRITICAL
                if privileged_operator_is_trusted(
                    telegram_update_sender_id(update),
                    group_id=telegram_update_chat_id(update),
                )
                else ExecutionPriority.NORMAL
            )
        if command in _SECURITY_COMMANDS:
            return (
                ExecutionPriority.HIGH
                if privileged_operator_is_trusted(
                    telegram_update_sender_id(update),
                    group_id=telegram_update_chat_id(update),
                )
                else ExecutionPriority.NORMAL
            )
        # Private /start links are the first interactive step of join
        # verification and must not wait behind ordinary group chatter.
        if command == "start" and isinstance(text, str):
            parts = text.split(maxsplit=1)
            if len(parts) == 2 and parts[1].casefold().startswith("verify"):
                return ExecutionPriority.HIGH
    return ExecutionPriority.NORMAL


def telegram_update_is_auth_candidate(
    update: Any,
    *,
    priority: ExecutionPriority | None = None,
) -> bool:
    """Return whether an untrusted update needs fast authorization handling.

    These messages are *not* privileged yet.  They use an isolated, tightly
    bounded worker lane so a first-time real Telegram administrator does not
    wait behind ordinary chat, while command spam cannot consume the trusted
    operator or automatic-security reserves.
    """

    selected_priority = (
        telegram_update_priority(update) if priority is None else priority
    )
    if selected_priority < ExecutionPriority.NORMAL:
        return False
    callback = _telegram_field(update, "callback_query")
    data = _telegram_field(callback, "data")
    if isinstance(data, str):
        normalized = data.casefold()
        if normalized in _ADMIN_CALLBACK_EXACT or normalized.startswith(
            _ADMIN_CALLBACK_PREFIXES
        ):
            return True
        parts = normalized.split(":", 2)
        if len(parts) >= 2 and (
            parts[0] == "jv" and parts[1] in {"a", "r"}
            or parts[0] == "vban" and parts[1] in {"cancel", "ban"}
        ):
            return True
    return any(
        telegram_message_is_privileged_candidate(_telegram_field(update, key))
        for key in _MESSAGE_CONTAINER_KEYS
    )


def telegram_update_is_privileged(update: Any) -> bool:
    """Compatibility predicate for callers that only need two classes."""

    if telegram_update_priority(update) < ExecutionPriority.NORMAL:
        return True
    callback = _telegram_field(update, "callback_query")
    data = _telegram_field(callback, "data")
    if isinstance(data, str):
        normalized = data.casefold()
        if normalized in _ADMIN_CALLBACK_EXACT or normalized.startswith(
            _ADMIN_CALLBACK_PREFIXES
        ):
            return True
        parts = normalized.split(":", 2)
        if len(parts) >= 2 and (
            parts[0] == "jv" and parts[1] in {"a", "r"}
            or parts[0] == "vban" and parts[1] in {"cancel", "ban"}
        ):
            return True
    return any(
        telegram_message_is_privileged_candidate(_telegram_field(update, key))
        for key in _MESSAGE_CONTAINER_KEYS
    )

_RouteHook = Callable[[], Awaitable[None] | None]
_HealthCheck = Callable[[], str | None]
_DurableUpdateIngest = Callable[[Any], Awaitable[int]]


class _OrphanedOperationTimeout(TimeoutError):
    """A timed-out operation ignored cancellation and may still mutate state."""


class _UnsafeDeliveryState(RuntimeError):
    """Delivery mode cannot be changed safely without restarting the process."""


@dataclass(frozen=True, slots=True)
class WebhookConfig:
    url: str
    path: str
    secret: str = field(repr=False)


@dataclass(frozen=True, slots=True)
class _WebhookRunResult:
    failure_reason: str | None = None
    preserve_pending_updates: bool = False


def _consume_task_result(task: asyncio.Task[Any]) -> None:
    _CONTROL_ORPHAN_TASKS.discard(task)
    if task.cancelled():
        return
    try:
        task.exception()
    except (asyncio.CancelledError, Exception):
        pass


def _track_control_orphan(task: asyncio.Task[Any]) -> None:
    if task.done():
        _consume_task_result(task)
        return
    _CONTROL_ORPHAN_TASKS.add(task)
    task.add_done_callback(_consume_task_result)


async def flush_update_delivery_tasks(*, timeout_seconds: float = 5.0) -> None:
    """Cancel and boundedly join detached Telegram control operations."""

    tasks = {task for task in _CONTROL_ORPHAN_TASKS if not task.done()}
    for task in tasks:
        task.cancel()
    if not tasks:
        return
    done, pending = await asyncio.wait(
        tasks,
        timeout=max(0.0, float(timeout_seconds)),
    )
    for task in done:
        _consume_task_result(task)
    if pending:
        log.error(
            "%d Telegram delivery control task(s) ignored shutdown cancellation",
            len(pending),
        )


async def _cancel_control_task_bounded(
    task: asyncio.Task[Any],
    *,
    operation: str,
    timeout: float,
) -> None:
    """Cancel one owned lifecycle task without making shutdown unbounded."""

    if task.done():
        _consume_task_result(task)
        return
    task.cancel()
    try:
        done, _pending = await asyncio.wait(
            {task},
            timeout=max(0.0, float(timeout)),
        )
    except asyncio.CancelledError:
        _track_control_orphan(task)
        raise
    if task in done:
        _consume_task_result(task)
        return
    log.error("%s ignored bounded cancellation", operation)
    _track_control_orphan(task)


async def _await_with_hard_timeout(
    awaitable: Awaitable[Any],
    *,
    timeout: float,
    operation: str,
) -> Any:
    task = asyncio.create_task(awaitable, name=f"webhook-{operation}")
    try:
        done, _pending = await asyncio.wait({task}, timeout=timeout)
    except asyncio.CancelledError:
        task.cancel()
        try:
            done, _pending = await asyncio.wait(
                {task},
                timeout=_CONTROL_CANCELLATION_GRACE_SECONDS,
            )
        except asyncio.CancelledError:
            _track_control_orphan(task)
            raise
        if task in done:
            _consume_task_result(task)
        else:
            _track_control_orphan(task)
            log.error(
                "%s ignored cancellation while update delivery was stopping",
                operation,
            )
        raise
    if task in done:
        return await task
    task.cancel()
    done, _pending = await asyncio.wait(
        {task},
        timeout=_CONTROL_CANCELLATION_GRACE_SECONDS,
    )
    if task in done:
        _consume_task_result(task)
        raise TimeoutError(f"{operation} 超过 {timeout:.1f} 秒")
    _track_control_orphan(task)
    raise _OrphanedOperationTimeout(
        f"{operation} 超过 {timeout:.1f} 秒且拒绝取消"
    )


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


def _dispatcher_workflow_data(dispatcher: Dispatcher) -> dict[str, Any]:
    workflow_data = {
        "dispatcher": dispatcher,
        **dispatcher.workflow_data,
    }
    workflow_data.pop("bot", None)
    return workflow_data


async def _emit_dispatcher_startup(
    *,
    dispatcher: Dispatcher,
    bot: Bot,
    workflow_data: dict[str, Any],
) -> None:
    try:
        await _await_with_hard_timeout(
            dispatcher.emit_startup(bot=bot, **workflow_data),
            timeout=_DISPATCHER_HOOK_TIMEOUT_SECONDS,
            operation="dispatcher startup",
        )
    except _OrphanedOperationTimeout as exc:
        raise _UnsafeDeliveryState(
            "dispatcher startup hook ignored cancellation; restart required"
        ) from exc


async def _emit_dispatcher_shutdown(
    *,
    dispatcher: Dispatcher,
    bot: Bot,
    workflow_data: dict[str, Any],
) -> None:
    try:
        await _await_with_hard_timeout(
            dispatcher.emit_shutdown(bot=bot, **workflow_data),
            timeout=_DISPATCHER_HOOK_TIMEOUT_SECONDS,
            operation="dispatcher shutdown",
        )
    except _OrphanedOperationTimeout as exc:
        raise _UnsafeDeliveryState(
            "dispatcher shutdown hook ignored cancellation; restart required"
        ) from exc
    except TimeoutError:
        log.exception("Dispatcher shutdown hook timed out")


async def run_update_delivery(
    *,
    bot: Bot,
    dispatcher: Dispatcher,
    settings: Settings,
    webhook: WebhookConfig | None,
    fallback_reason: str | None = None,
    enable_webhook_route: _RouteHook | None = None,
    mark_webhook_active: _RouteHook | None = None,
    disable_webhook_route: _RouteHook | None = None,
    mark_polling_active: _RouteHook | None = None,
    webhook_runtime_failure: _HealthCheck | None = None,
    start_update_processor: _RouteHook | None = None,
    stop_update_processor: _RouteHook | None = None,
    durable_update_ingest: _DurableUpdateIngest | None = None,
) -> str:
    """Run one dispatcher lifecycle across webhook and polling transports."""

    mark_privileged_operator(
        int(settings.super_admin_id or 0),
        ttl_seconds=365 * 24 * 60 * 60.0,
    )
    allowed_updates = dispatcher.resolve_used_update_types()
    if durable_update_ingest is None:
        if start_update_processor is not None or stop_update_processor is not None:
            raise ValueError(
                "durable update processor hooks require durable_update_ingest"
            )
        # Compatibility path for callers without a durable inbox. Webhook's
        # helper and aiogram.start_polling retain ownership of their own hooks;
        # wrapping start_polling here would emit startup/shutdown twice.
        return await _run_update_delivery_started(
            bot=bot,
            dispatcher=dispatcher,
            settings=settings,
            webhook=webhook,
            allowed_updates=allowed_updates,
            fallback_reason=fallback_reason,
            enable_webhook_route=enable_webhook_route,
            mark_webhook_active=mark_webhook_active,
            disable_webhook_route=disable_webhook_route,
            mark_polling_active=mark_polling_active,
            webhook_runtime_failure=webhook_runtime_failure,
            durable_update_ingest=None,
            manage_webhook_lifecycle=True,
        )

    workflow_data = _dispatcher_workflow_data(dispatcher)
    startup_completed = False
    shutdown_signal_task: asyncio.Task[None] | None = None
    transport_task: asyncio.Task[str] | None = None
    try:
        await _emit_dispatcher_startup(
            dispatcher=dispatcher,
            bot=bot,
            workflow_data=workflow_data,
        )
        startup_completed = True
        # Recovered durable rows must never enter handlers before dispatcher
        # startup has completed. This consumer remains live while transports
        # switch, so a webhook ACK followed by polling fallback is not stranded.
        await _invoke_route_hook(start_update_processor)
        # Own one signal waiter for the complete durable transport lifecycle.
        # Webhook and a later polling fallback must never install competing
        # handlers or leave polling without SIGTERM handling after webhook has
        # removed its local waiter.
        shutdown_signal_task = asyncio.create_task(
            _wait_for_shutdown_signal(),
            name="telegram-delivery-shutdown-signal",
        )
        transport_task = asyncio.create_task(
            _run_update_delivery_started(
                bot=bot,
                dispatcher=dispatcher,
                settings=settings,
                webhook=webhook,
                allowed_updates=allowed_updates,
                fallback_reason=fallback_reason,
                enable_webhook_route=enable_webhook_route,
                mark_webhook_active=mark_webhook_active,
                disable_webhook_route=disable_webhook_route,
                mark_polling_active=mark_polling_active,
                webhook_runtime_failure=webhook_runtime_failure,
                durable_update_ingest=durable_update_ingest,
                manage_webhook_lifecycle=False,
                shutdown_signal_task=shutdown_signal_task,
            ),
            name="telegram-durable-transport",
        )
        done, _pending = await asyncio.wait(
            {transport_task, shutdown_signal_task},
            return_when=asyncio.FIRST_COMPLETED,
        )
        if transport_task in done:
            return await transport_task

        # The shared waiter completed first. The transport-specific code also
        # observes this task, but cancel it here as a lifecycle-wide backstop so
        # a signal during webhook registration/deletion/backoff is bounded too.
        await shutdown_signal_task
        return "shutdown"
    finally:
        if transport_task is not None:
            await _cancel_control_task_bounded(
                transport_task,
                operation="durable Telegram transport",
                timeout=_POLLING_STOP_TIMEOUT_SECONDS,
            )
        if shutdown_signal_task is not None:
            await _cancel_control_task_bounded(
                shutdown_signal_task,
                operation="Telegram shutdown signal waiter",
                timeout=_CONTROL_CANCELLATION_GRACE_SECONDS,
            )
        if startup_completed:
            try:
                await _invoke_route_hook(stop_update_processor)
            finally:
                await _emit_dispatcher_shutdown(
                    dispatcher=dispatcher,
                    bot=bot,
                    workflow_data=workflow_data,
                )


async def _run_update_delivery_started(
    *,
    bot: Bot,
    dispatcher: Dispatcher,
    settings: Settings,
    webhook: WebhookConfig | None,
    allowed_updates: list[str],
    fallback_reason: str | None = None,
    enable_webhook_route: _RouteHook | None = None,
    mark_webhook_active: _RouteHook | None = None,
    disable_webhook_route: _RouteHook | None = None,
    mark_polling_active: _RouteHook | None = None,
    webhook_runtime_failure: _HealthCheck | None = None,
    durable_update_ingest: _DurableUpdateIngest | None = None,
    manage_webhook_lifecycle: bool = False,
    shutdown_signal_task: asyncio.Task[None] | None = None,
) -> str:
    """Run transports after dispatcher startup has completed."""

    # Never destroy Telegram's delivery backlog as a side effect of starting or
    # changing transports. Older runtime-config rows defaulted this flag to
    # true, so merely changing the model default is insufficient for deployed
    # databases. Backlog deletion must be a separate explicit operator action,
    # not part of the bot's automatic lifecycle.
    polling_drop_pending_updates = False
    if settings.bot.drop_pending_updates:
        log.debug(
            "ignored deprecated drop_pending_updates=true; "
            "Telegram backlog preservation remains enforced"
        )

    if webhook is not None:
        try:
            result = await _run_webhook_session(
                dispatcher=dispatcher,
                bot=bot,
                settings=settings,
                webhook=webhook,
                allowed_updates=allowed_updates,
                enable_webhook_route=enable_webhook_route,
                mark_webhook_active=mark_webhook_active,
                disable_webhook_route=disable_webhook_route,
                webhook_runtime_failure=webhook_runtime_failure,
                manage_dispatcher_lifecycle=manage_webhook_lifecycle,
                shutdown_signal_task=shutdown_signal_task,
            )
        except asyncio.CancelledError:
            raise
        except _UnsafeDeliveryState:
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
        mark_polling_active=mark_polling_active,
        durable_update_ingest=durable_update_ingest,
        shutdown_signal_task=shutdown_signal_task,
    )
    return "polling"


async def _run_polling(
    *,
    dispatcher: Dispatcher,
    bot: Bot,
    drop_pending_updates: bool,
    reason: str,
    allowed_updates: list[str],
    mark_polling_active: _RouteHook | None = None,
    durable_update_ingest: _DurableUpdateIngest | None = None,
    shutdown_signal_task: asyncio.Task[None] | None = None,
) -> None:
    await _delete_webhook_for_polling(
        bot=bot,
        drop_pending_updates=drop_pending_updates,
    )
    await _invoke_route_hook(mark_polling_active)
    log.info("Telegram 更新接收模式：轮询 | 原因=%s", reason)
    if durable_update_ingest is not None:
        await _run_durable_polling(
            bot=bot,
            allowed_updates=allowed_updates,
            durable_update_ingest=durable_update_ingest,
            shutdown_signal_task=shutdown_signal_task,
        )
        return

    polling_task = asyncio.create_task(
        dispatcher.start_polling(
            bot,
            allowed_updates=allowed_updates,
            handle_as_tasks=True,
            tasks_concurrency_limit=8,
            close_bot_session=False,
        ),
        name="aiogram-polling-runner",
    )
    try:
        # Do not propagate an outer cancellation directly into aiogram's
        # ``start_polling`` coroutine.  In current aiogram releases that skips
        # the block which cancels its internal long-polling task, leaving a
        # detached getUpdates loop alive while the application closes the Bot
        # session and database.  Ask the dispatcher to stop normally first.
        await asyncio.shield(polling_task)
    except asyncio.CancelledError:
        stop_polling = getattr(dispatcher, "stop_polling", None)
        polling_never_started = False
        if callable(stop_polling) and not polling_task.done():
            try:
                await _await_with_hard_timeout(
                    stop_polling(),
                    timeout=_POLLING_STOP_TIMEOUT_SECONDS,
                    operation="stopPolling",
                )
            except RuntimeError as exc:
                # Cancellation can race the first scheduling turn before
                # start_polling acquires its running lock.  In that narrow
                # window there is no internal polling task yet.  Cancel the
                # runner immediately, before yielding and accidentally letting
                # it start after stop_polling already failed.
                if "not started" in str(exc).lower():
                    polling_never_started = True
                else:
                    log.exception("Failed to stop polling after cancellation")
            except Exception:
                log.exception("Failed to stop polling after cancellation")

        if polling_never_started and not polling_task.done():
            polling_task.cancel()
            done, _pending = await asyncio.wait(
                {polling_task},
                timeout=_POLLING_UPDATE_CANCEL_GRACE_SECONDS,
            )
            if polling_task in done:
                _consume_task_result(polling_task)
            else:
                log.error("Polling runner ignored pre-start cancellation")
                _track_control_orphan(polling_task)
        elif not polling_task.done():
            done, _pending = await asyncio.wait(
                {polling_task},
                timeout=_POLLING_STOP_TIMEOUT_SECONDS,
            )
            if polling_task not in done:
                log.error(
                    "Aiogram polling did not stop within %.1fs; cancelling runner",
                    _POLLING_STOP_TIMEOUT_SECONDS,
                )
                polling_task.cancel()
                _track_control_orphan(polling_task)
        else:
            _consume_task_result(polling_task)
        raise
    finally:
        await _drain_polling_update_tasks(dispatcher)


def _polling_update_id(update: Any) -> int:
    if isinstance(update, dict):
        value = update.get("update_id")
    else:
        value = getattr(update, "update_id", None)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError("Telegram getUpdates returned an invalid update_id")
    return int(value)


async def _run_durable_polling(
    *,
    bot: Bot,
    allowed_updates: list[str],
    durable_update_ingest: _DurableUpdateIngest,
    shutdown_signal_task: asyncio.Task[None] | None = None,
) -> None:
    """Long-poll Telegram without confirming an update before durable commit.

    Telegram confirms all updates below the *next* getUpdates offset. We only
    advance the local offset after ``durable_update_ingest`` returns, so a DB
    failure or process crash leaves the item redeliverable. Handler execution is
    intentionally absent here; the shared durable inbox workers own concurrency,
    retry and receipt-based terminal completion for both transports.
    """

    offset: int | None = None
    network_backoff = Backoff(_POLLING_BACKOFF_CONFIG)
    persistence_backoff = Backoff(_POLLING_BACKOFF_CONFIG)
    network_failed = False
    while True:
        try:
            request_task = asyncio.create_task(
                _await_with_hard_timeout(
                    bot.get_updates(
                        offset=offset,
                        timeout=_POLLING_TIMEOUT_SECONDS,
                        allowed_updates=allowed_updates,
                        request_timeout=_POLLING_HTTP_TIMEOUT_SECONDS,
                    ),
                    timeout=_POLLING_REQUEST_TIMEOUT_SECONDS,
                    operation="getUpdates",
                ),
                name="telegram-durable-get-updates",
            )
            wait_set: set[asyncio.Task[Any]] = {request_task}
            if shutdown_signal_task is not None:
                wait_set.add(shutdown_signal_task)
            try:
                done, _pending = await asyncio.wait(
                    wait_set,
                    return_when=asyncio.FIRST_COMPLETED,
                )
            except asyncio.CancelledError:
                await _cancel_control_task_bounded(
                    request_task,
                    operation="durable getUpdates",
                    timeout=_POLLING_UPDATE_CANCEL_GRACE_SECONDS,
                )
                raise
            if (
                shutdown_signal_task is not None
                and shutdown_signal_task in done
            ):
                await shutdown_signal_task
                await _cancel_control_task_bounded(
                    request_task,
                    operation="durable getUpdates after shutdown signal",
                    timeout=_POLLING_UPDATE_CANCEL_GRACE_SECONDS,
                )
                updates = _POLLING_SHUTDOWN
            else:
                updates = await request_task
        except asyncio.CancelledError:
            raise
        except _OrphanedOperationTimeout as exc:
            # A second long poll must not be issued while a cancellation-
            # resistant request can still return the same batch.
            raise _UnsafeDeliveryState(
                "getUpdates ignored cancellation; restart required"
            ) from exc
        except Exception as exc:
            network_failed = True
            delay = network_backoff.next_delay
            log.error(
                "Failed to fetch Telegram updates (%s: %s); retrying in %.1fs",
                type(exc).__name__,
                exc,
                delay,
            )
            await network_backoff.asleep()
            continue

        if updates is _POLLING_SHUTDOWN:
            return

        if network_failed:
            log.info(
                "Telegram polling connection restored after %d attempt(s)",
                network_backoff.counter,
            )
            network_backoff.reset()
            network_failed = False

        batch = [
            (index, item, _polling_update_id(item))
            for index, item in enumerate(updates)
        ]
        # Persist and publish interactive/security updates before ordinary
        # chatter from the same getUpdates batch.  Telegram's offset is still
        # advanced only through the contiguous original-order prefix below, so
        # this latency optimization does not weaken the durable ACK boundary.
        fast_auth_positions: set[int] = set()
        auth_counts: dict[tuple[int, int], int] = {}
        for position, item, _update_id in batch:
            if not telegram_update_is_auth_candidate(item):
                continue
            scope = (
                telegram_update_chat_id(item),
                telegram_update_sender_id(item),
            )
            count = auth_counts.get(scope, 0)
            if count < TELEGRAM_AUTH_CANDIDATE_BURST:
                fast_auth_positions.add(position)
            auth_counts[scope] = count + 1

        def persistence_order(entry: tuple[int, Any, int]) -> tuple[int, int, int]:
            priority = telegram_update_priority(entry[1])
            auth_rank = 0 if entry[0] in fast_auth_positions else 1
            return int(priority), auth_rank, entry[0]

        ordered_batch = sorted(batch, key=persistence_order)
        persisted_positions: set[int] = set()
        next_ack_position = 0
        batch_persisted = True
        for position, item, update_id in ordered_batch:
            try:
                persisted_id = await durable_update_ingest(item)
                if int(persisted_id) != update_id:
                    raise RuntimeError(
                        "durable inbox returned a mismatched Telegram update_id"
                    )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                batch_persisted = False
                delay = persistence_backoff.next_delay
                log.error(
                    "Refusing to confirm Telegram update %s before durable "
                    "persistence (%s: %s); retrying in %.1fs",
                    update_id,
                    type(exc).__name__,
                    exc,
                    delay,
                )
                await persistence_backoff.asleep()
                break

            persisted_positions.add(position)
            while next_ack_position in persisted_positions:
                contiguous_update_id = batch[next_ack_position][2]
                # Supplying this value on the next request is Telegram's ACK. A
                # crash before that request merely produces a harmless
                # duplicate, which the inbox primary key absorbs.
                next_offset = contiguous_update_id + 1
                offset = next_offset if offset is None else max(offset, next_offset)
                next_ack_position += 1

        if batch_persisted:
            persistence_backoff.reset()


async def _drain_polling_update_tasks(dispatcher: Dispatcher) -> None:
    """Bound aiogram's detached per-update tasks before shared resources close."""
    tracked = getattr(dispatcher, "_handle_update_tasks", None)
    if not tracked:
        return
    tasks = {task for task in tracked if isinstance(task, asyncio.Task)}
    if not tasks:
        return

    done, pending = await asyncio.wait(
        tasks,
        timeout=_POLLING_UPDATE_DRAIN_TIMEOUT_SECONDS,
    )
    for task in done:
        _consume_task_result(task)
    if not pending:
        return

    log.warning(
        "Polling stopped with %d in-flight updates after %.1fs; cancelling them",
        len(pending),
        _POLLING_UPDATE_DRAIN_TIMEOUT_SECONDS,
    )
    for task in pending:
        task.cancel()
    done_after_cancel, still_pending = await asyncio.wait(
        pending,
        timeout=_POLLING_UPDATE_CANCEL_GRACE_SECONDS,
    )
    for task in done_after_cancel:
        _consume_task_result(task)
    if still_pending:
        log.error(
            "%d polling update tasks ignored cancellation",
            len(still_pending),
        )
        for task in still_pending:
            _track_control_orphan(task)


async def _delete_webhook_for_polling(
    *,
    bot: Bot,
    drop_pending_updates: bool,
) -> None:
    attempt = 0
    while attempt < _DELETE_WEBHOOK_MAX_ATTEMPTS:
        attempt += 1
        try:
            deleted = await _await_with_hard_timeout(
                bot.delete_webhook(drop_pending_updates=drop_pending_updates),
                timeout=_TELEGRAM_CONTROL_TIMEOUT_SECONDS,
                operation="deleteWebhook",
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            issue = f"调用 deleteWebhook 失败：{exc}"
        else:
            if deleted:
                return
            issue = "Telegram deleteWebhook 返回 false"

        if attempt >= _DELETE_WEBHOOK_MAX_ATTEMPTS:
            raise RuntimeError(
                f"{issue}；已达到 {_DELETE_WEBHOOK_MAX_ATTEMPTS} 次重试上限"
            )

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
    mark_webhook_active: _RouteHook | None = None,
    webhook_runtime_failure: _HealthCheck | None = None,
    manage_dispatcher_lifecycle: bool = True,
    shutdown_signal_task: asyncio.Task[None] | None = None,
) -> _WebhookRunResult:
    probe_issue = await _probe_webhook_endpoint(webhook)
    if probe_issue is not None:
        return _WebhookRunResult(
            failure_reason=f"Webhook 公网端点自检失败：{probe_issue}",
            preserve_pending_updates=True,
        )

    try:
        deleted = await _await_with_hard_timeout(
            bot.delete_webhook(drop_pending_updates=False),
            timeout=_TELEGRAM_CONTROL_TIMEOUT_SECONDS,
            operation="deleteWebhook before registration",
        )
        if not deleted:
            raise RuntimeError("Telegram deleteWebhook 返回 false")
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        return _WebhookRunResult(
            failure_reason=f"Webhook 注册前清理旧地址失败：{exc}",
            preserve_pending_updates=True,
        )

    workflow_data = _dispatcher_workflow_data(dispatcher)
    if manage_dispatcher_lifecycle:
        await _emit_dispatcher_startup(
            dispatcher=dispatcher,
            bot=bot,
            workflow_data=workflow_data,
        )
    try:
        await _invoke_route_hook(enable_webhook_route)
        registration_attempted = False
        try:
            registration_attempted = True
            registered = await _await_with_hard_timeout(
                bot.set_webhook(
                    url=webhook.url,
                    allowed_updates=allowed_updates,
                    drop_pending_updates=False,
                    secret_token=webhook.secret,
                    max_connections=WEBHOOK_MAX_CONCURRENT_UPDATES,
                ),
                timeout=_TELEGRAM_CONTROL_TIMEOUT_SECONDS,
                operation="setWebhook",
            )
            if not registered:
                raise RuntimeError("Telegram setWebhook 返回 false")

            info = await _await_with_hard_timeout(
                bot.get_webhook_info(),
                timeout=_TELEGRAM_CONTROL_TIMEOUT_SECONDS,
                operation="getWebhookInfo after registration",
            )
            actual_url = str(info.url or "").strip()
            if actual_url != webhook.url:
                raise RuntimeError("Telegram 返回的 webhook URL 与配置不一致")
            await _invoke_route_hook(mark_webhook_active)
        except asyncio.CancelledError:
            raise
        except _OrphanedOperationTimeout as exc:
            raise _UnsafeDeliveryState(
                "setWebhook ignored cancellation; refusing an unsafe polling fallback"
            ) from exc
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
                webhook_runtime_failure=webhook_runtime_failure,
                shutdown_signal_task=shutdown_signal_task,
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
            if manage_dispatcher_lifecycle:
                await _emit_dispatcher_shutdown(
                    dispatcher=dispatcher,
                    bot=bot,
                    workflow_data=workflow_data,
                )


async def _probe_webhook_endpoint(webhook: WebhookConfig) -> str | None:
    invalid_secret = "smart_group_bot_webhook_probe"
    if invalid_secret == webhook.secret:
        invalid_secret += "_invalid"
    timeout = aiohttp.ClientTimeout(total=_WEBHOOK_PROBE_TIMEOUT_SECONDS)
    issue = "Webhook probe did not run"
    for attempt in range(1, _WEBHOOK_PROBE_ATTEMPTS + 1):
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
                    content = getattr(response, "content", None)
                    read = getattr(content, "read", None)
                    if callable(read):
                        raw_body = await read(4097)
                        body = raw_body[:4096].decode("utf-8", errors="ignore").strip()
                    else:
                        # Test doubles and older aiohttp-compatible clients.
                        body = (await response.text()).strip()[:4096]
                    if response.status == 401 and body == "Unauthorized":
                        return None
                    issue = (
                        f"公网地址返回 HTTP {response.status}，"
                        "未命中 bot webhook 路由"
                    )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            issue = f"无法访问 WEBHOOK_URL：{exc}"

        if attempt >= _WEBHOOK_PROBE_ATTEMPTS:
            break
        delay = _WEBHOOK_PROBE_BACKOFF_SECONDS[
            min(attempt - 1, len(_WEBHOOK_PROBE_BACKOFF_SECONDS) - 1)
        ]
        log.warning(
            "Webhook 公网端点自检失败，%.1f 秒后重试（%d/%d）：%s",
            delay,
            attempt,
            _WEBHOOK_PROBE_ATTEMPTS,
            issue,
        )
        await asyncio.sleep(delay)
    return issue


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
    webhook_runtime_failure: _HealthCheck | None = None,
    shutdown_signal_task: asyncio.Task[None] | None = None,
) -> str | None:
    owns_shutdown_task = shutdown_signal_task is None
    shutdown_task = shutdown_signal_task or asyncio.create_task(
        _wait_for_shutdown_signal(),
        name="webhook-shutdown-waiter",
    )
    watchdog_task = asyncio.create_task(
        _watch_webhook(
            bot=bot,
            webhook=webhook,
            initial_error_date=initial_error_date,
            webhook_runtime_failure=webhook_runtime_failure,
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
        owned_tasks = {watchdog_task}
        if owns_shutdown_task:
            owned_tasks.add(shutdown_task)
        for task in owned_tasks:
            if not task.done():
                task.cancel()
        done, pending = await asyncio.wait(
            owned_tasks,
            timeout=_WEBHOOK_TASK_STOP_TIMEOUT_SECONDS,
        )
        for task in done:
            _consume_task_result(task)
        if pending:
            log.error(
                "%d webhook lifecycle tasks ignored cancellation",
                len(pending),
            )
            for task in pending:
                _track_control_orphan(task)


async def _watch_webhook(
    *,
    bot: Bot,
    webhook: WebhookConfig,
    initial_error_date: object | None,
    webhook_runtime_failure: _HealthCheck | None = None,
) -> str:
    consecutive_failures = 0
    last_seen_error_date = initial_error_date
    while True:
        await asyncio.sleep(_WEBHOOK_WATCH_INTERVAL_SECONDS)
        issue: str | None = None
        if webhook_runtime_failure is not None:
            try:
                local_failure = webhook_runtime_failure()
            except Exception as exc:
                local_failure = None
                issue = f"本地 webhook 健康检查异常：{exc}"
            if local_failure:
                # The local processor only exposes a fatal issue after its own
                # bounded retry policy has been exhausted.  Unlike one remote
                # Telegram error sample, this is already a confirmed failure.
                return f"Webhook 本地更新处理失败：{local_failure}"

        if issue is None:
            try:
                info = await _await_with_hard_timeout(
                    bot.get_webhook_info(),
                    timeout=_TELEGRAM_CONTROL_TIMEOUT_SECONDS,
                    operation="getWebhookInfo watchdog",
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                issue = f"Webhook 状态检查失败：{exc}"
            else:
                actual_url = str(info.url or "").strip()
                if actual_url != webhook.url:
                    issue = "Telegram webhook 注册地址已变化"
                else:
                    current_error_date = getattr(info, "last_error_date", None)
                    if (
                        current_error_date is not None
                        and current_error_date != last_seen_error_date
                    ):
                        message = str(
                            getattr(info, "last_error_message", None)
                            or "Telegram 未提供详细原因"
                        ).replace("\n", " ")
                        issue = f"Telegram webhook 投递失败：{message[:300]}"
                        last_seen_error_date = current_error_date
                    elif current_error_date is None:
                        # Once Telegram reports a clean state, a later error is
                        # new even if its coarse timestamp happens to equal the
                        # error that existed before this process registered it.
                        last_seen_error_date = None

        if issue is None:
            if consecutive_failures:
                log.info("Webhook 连续故障计数已恢复为 0。")
            consecutive_failures = 0
            continue

        consecutive_failures += 1
        if consecutive_failures < _WEBHOOK_FAILURE_THRESHOLD:
            log.warning(
                "%s；保持 webhook 模式并复查（%d/%d）。",
                issue,
                consecutive_failures,
                _WEBHOOK_FAILURE_THRESHOLD,
            )
            continue
        return (
            f"{issue}（连续 {_WEBHOOK_FAILURE_THRESHOLD} 次健康检查失败）"
        )


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
    "WEBHOOK_AUTH_CONCURRENT_UPDATES",
    "WEBHOOK_AUTH_QUEUE_CAPACITY",
    "WEBHOOK_CRITICAL_CONCURRENT_UPDATES",
    "WEBHOOK_CRITICAL_QUEUE_CAPACITY",
    "WEBHOOK_MAX_CONCURRENT_UPDATES",
    "WEBHOOK_SECURITY_CONCURRENT_UPDATES",
    "WEBHOOK_SECURITY_QUEUE_CAPACITY",
    "TELEGRAM_AUTH_CANDIDATE_BURST",
    "WebhookConfig",
    "resolve_webhook_config",
    "run_update_delivery",
    "telegram_update_is_auth_candidate",
]
