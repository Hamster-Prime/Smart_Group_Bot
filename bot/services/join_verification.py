"""Human verification via a Telegram Mini App (join + moderation kinds).

Join flow (per-group policy):
- A new member passes ban-check + profile screening, then gets fully muted
  via restrict_chat_member. The group prompt carries a target-locked callback
  that redirects the affected member into the bot's private chat.
- /start in private chat sends a Mini App button (web_app); tapping it opens
  the challenge page inside Telegram — no visible URL, no browser jump.
- The page embeds the selected Turnstile or hCaptcha widget and submits its token together
  with Telegram's signed initData to the bot's built-in HTTP server
  (bot.services.verify_web). The server validates the widget token against
  the provider siteverify API and the initData signature against the bot token, so
  the verified user identity cannot be forged; no secret link tokens needed.
- Passing lifts the restriction (all-True permissions returns the user to a
  plain member governed by the chat's default permissions).
- Missing the deadline kicks the user (ban + unban, so they may rejoin and
  retry later).

Moderation-challenge flow (kind="moderation"):
- A message judged violating with LOW confidence is deleted, the sender is
  fully muted, and the same private-chat deep link + Mini App challenge is
  issued (begin_moderation_challenge).
- Passing restores permissions; missing the deadline bans the user in the
  current group until a group administrator lifts that ban.

Pending records live in join_verifications; a background sweeper enforces
deadlines even across bot restarts.
"""
from __future__ import annotations

import asyncio
import html
import logging
import weakref
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta
from urllib.parse import urlencode

from aiogram import Bot
from aiogram.types import (
    ChatPermissions,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    WebAppInfo,
)
from sqlalchemy import case, delete, or_, select, update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from bot.config import Settings
from bot.db.models import AuthorizedGroup, JoinVerification, UserWarning
from bot.services.authz import is_group_authorized, is_super_admin_user_id
from bot.services.background_health import record_background_failure
from bot.services.join_screening import is_globally_banned
from bot.utils.telegram import (
    configured_auto_delete_seconds,
    schedule_message_auto_delete_durable,
)
from bot.utils.timezone import now_shanghai_naive

log = logging.getLogger(__name__)

_MODERATION_CHALLENGE_LOCKS: weakref.WeakValueDictionary[
    tuple[int, int], asyncio.Lock
] = weakref.WeakValueDictionary()
_BLOCKED_VERIFICATION_CONFIGS: dict[
    tuple[int, str], tuple[Settings, tuple[str, ...], str]
] = {}

# A cancelled aiohttp/aiogram request is not guaranteed to stop immediately.
# Keep every child observable until it really exits, and refuse to create an
# unbounded number of cancellation-resistant requests when Telegram is wedged.
_TELEGRAM_CALL_CAPACITY = 16
_TELEGRAM_CALL_BACKPRESSURE_SECONDS = 0.5
_TELEGRAM_CALL_TASKS: set[asyncio.Future[object]] = set()


@dataclass(slots=True)
class _TelegramCallState:
    capacity: int
    semaphore: asyncio.BoundedSemaphore
    lock: asyncio.Lock
    tasks: set[asyncio.Future[object]]
    draining: bool = False
    waiters: int = 0


_TELEGRAM_CALL_STATES: weakref.WeakKeyDictionary[
    asyncio.AbstractEventLoop, _TelegramCallState
] = weakref.WeakKeyDictionary()


class _TelegramCallCapacityError(RuntimeError):
    pass


def _telegram_call_state() -> _TelegramCallState:
    loop = asyncio.get_running_loop()
    state = _TELEGRAM_CALL_STATES.get(loop)
    if state is None:
        capacity = max(1, int(_TELEGRAM_CALL_CAPACITY))
        state = _TelegramCallState(
            capacity=capacity,
            semaphore=asyncio.BoundedSemaphore(capacity),
            lock=asyncio.Lock(),
            tasks=set(),
        )
        _TELEGRAM_CALL_STATES[loop] = state
    return state


def _active_telegram_call_tasks() -> set[asyncio.Future[object]]:
    loop = asyncio.get_running_loop()
    return {
        task
        for task in _TELEGRAM_CALL_TASKS
        if not task.done() and task.get_loop() is loop
    }


def _discard_telegram_call_state_if_idle(
    loop: asyncio.AbstractEventLoop,
    state: _TelegramCallState,
) -> None:
    if state.draining or state.tasks or state.waiters or state.lock.locked():
        return
    if _TELEGRAM_CALL_STATES.get(loop) is state:
        _TELEGRAM_CALL_STATES.pop(loop, None)


def _observe_telegram_call_task(
    task: asyncio.Future[object],
    state: _TelegramCallState,
) -> None:
    if task in state.tasks:
        state.tasks.discard(task)
        _TELEGRAM_CALL_TASKS.discard(task)
        state.semaphore.release()
    try:
        task.result()
    except (asyncio.CancelledError, Exception):
        pass
    _discard_telegram_call_state_if_idle(task.get_loop(), state)


def _discard_unstarted_awaitable(awaitable: object) -> None:
    """Close a coroutine rejected before it became an asyncio Task."""

    if asyncio.isfuture(awaitable):
        awaitable.cancel()
        return
    close = getattr(awaitable, "close", None)
    if callable(close):
        try:
            close()
        except Exception:
            pass


def _cancel_telegram_acquire_and_return_late_permit(
    acquire_task: asyncio.Task[bool],
    state: _TelegramCallState,
) -> None:
    """Return a permit won in the same tick that its owner was cancelled."""

    def _finished(done: asyncio.Task[bool]) -> None:
        if done.cancelled():
            return
        try:
            done.result()
        except (asyncio.CancelledError, Exception):
            return
        state.semaphore.release()
        _discard_telegram_call_state_if_idle(done.get_loop(), state)

    acquire_task.cancel()
    if acquire_task.done():
        _finished(acquire_task)
    else:
        acquire_task.add_done_callback(_finished)


async def _start_telegram_call(awaitable: object) -> asyncio.Future[object]:
    loop = asyncio.get_running_loop()
    state = _telegram_call_state()
    acquired = False
    state.waiters += 1
    acquire_task = asyncio.create_task(state.semaphore.acquire())
    try:
        wait_seconds = max(0.0, float(_TELEGRAM_CALL_BACKPRESSURE_SECONDS))
        done, _pending = await asyncio.wait(
            {acquire_task},
            timeout=wait_seconds,
        )
        if acquire_task not in done:
            _cancel_telegram_acquire_and_return_late_permit(acquire_task, state)
            raise asyncio.TimeoutError
        await acquire_task
        acquired = True
    except asyncio.TimeoutError as exc:
        _discard_unstarted_awaitable(awaitable)
        raise _TelegramCallCapacityError(
            "join verification Telegram call capacity is saturated"
        ) from exc
    except BaseException:
        if not acquired:
            _cancel_telegram_acquire_and_return_late_permit(acquire_task, state)
        _discard_unstarted_awaitable(awaitable)
        raise
    finally:
        state.waiters -= 1
        if not acquired:
            _discard_telegram_call_state_if_idle(loop, state)

    try:
        async with state.lock:
            if state.draining:
                raise _TelegramCallCapacityError(
                    "join verification Telegram calls are shutting down"
                )
            task = asyncio.ensure_future(awaitable)
            state.tasks.add(task)
            _TELEGRAM_CALL_TASKS.add(task)
            task.add_done_callback(
                lambda done, owned_state=state: _observe_telegram_call_task(
                    done, owned_state
                )
            )
            return task
    except BaseException:
        state.semaphore.release()
        _discard_unstarted_awaitable(awaitable)
        _discard_telegram_call_state_if_idle(loop, state)
        raise

# Constant /start payload used after the target-locked group callback. Mini App
# (web_app) buttons are only allowed in private chats, so the callback redirects
# the affected user there before the actual challenge button is shown.
START_VERIFY_PAYLOAD = "verify"
# Interactive challenges (hCaptcha image rounds, slow proxies) legitimately
# finish shortly after deadline_at. Submissions and admin actions stay valid
# for this long past the deadline, and the sweeper only enforces timeouts once
# the grace has also elapsed, so a member who solved in time is never rejected
# by a race with enforcement.
CHALLENGE_SUBMIT_GRACE = timedelta(seconds=60)
VERIFICATION_KIND_JOIN = "join"
VERIFICATION_KIND_MODERATION = "moderation"
VERIFICATION_KIND_PATROL = "patrol"
VERIFICATION_KIND_RAID = "raid"
VERIFICATION_STATUS_PREPARING = "preparing"
VERIFICATION_STATUS_PENDING = "pending"
VERIFICATION_STATUS_ENFORCING = "enforcing"
VERIFICATION_STATUS_RELEASING = "releasing"
# Manual group/global unban cancellation journal. Unlike a normal successful
# challenge release, recovery must also ensure any stale in-flight permanent
# ban is lifted before this row can be deleted.
VERIFICATION_STATUS_UNBANNING = "unbanning"
PREPARING_LEASE_SECONDS = 90
TERMINAL_LEASE_SECONDS = 90
UNBAN_RECOVERY_GRACE_SECONDS = 15
VERIFICATION_KINDS = frozenset(
    {
        VERIFICATION_KIND_JOIN,
        VERIFICATION_KIND_MODERATION,
        VERIFICATION_KIND_PATROL,
        VERIFICATION_KIND_RAID,
    }
)
# Kinds whose prompt message is shared by several members: the outcome of one
# member must never edit the prompt away from the others.
SHARED_PROMPT_VERIFICATION_KINDS = frozenset(
    {VERIFICATION_KIND_PATROL, VERIFICATION_KIND_RAID}
)
# The combined provider requires solving both base challenges in one session.
COMBINED_VERIFICATION_PROVIDER = "turnstile_hcaptcha"
_BASE_VERIFICATION_PROVIDERS = ("turnstile", "hcaptcha")
VERIFICATION_PROVIDERS = frozenset(
    {*_BASE_VERIFICATION_PROVIDERS, COMBINED_VERIFICATION_PROVIDER}
)
VERIFICATION_CALLBACK_PREFIX = "jv"
VERIFICATION_CALLBACK_START = "v"
VERIFICATION_CALLBACK_APPROVE = "a"
VERIFICATION_CALLBACK_REJECT = "r"
VERIFICATION_CALLBACK_ACTIONS = frozenset(
    {
        VERIFICATION_CALLBACK_START,
        VERIFICATION_CALLBACK_APPROVE,
        VERIFICATION_CALLBACK_REJECT,
    }
)
# The patrol warning message is shared by many violators, so its button has no
# embedded user id: the handler resolves the clicker's own pending record.
PATROL_VERIFY_CALLBACK_DATA = "ptv"
# Same shared-button scheme for the raid-guard retro challenge message.
RAID_VERIFY_CALLBACK_DATA = "rgv"

_GroupBanState = tuple[int, bool] | None


@dataclass(frozen=True, slots=True)
class PreparedVerification:
    verification_id: int
    group_id: int
    user_id: int
    kind: str
    lease_until: datetime
    prompt_message_id: int = 0


@dataclass(frozen=True, slots=True)
class UnbanRecovery:
    verification_id: int
    group_id: int
    user_id: int
    lease_until: datetime
    prompt_message_id: int = 0


def normalize_verification_provider(value: object, *, default: str = "turnstile") -> str:
    provider = str(value or "").strip().lower()
    if provider in VERIFICATION_PROVIDERS:
        return provider
    fallback = str(default or "turnstile").strip().lower()
    return fallback if fallback in VERIFICATION_PROVIDERS else "turnstile"


def verification_subproviders(
    provider: object,
    *,
    default: str = "turnstile",
) -> tuple[str, ...]:
    """Base challenge services the member must solve for a selected provider.

    Base providers map to themselves; the combined provider expands to both
    base services in the order the member is guided through them.
    """
    normalized = normalize_verification_provider(provider, default=default)
    if normalized == COMBINED_VERIFICATION_PROVIDER:
        return _BASE_VERIFICATION_PROVIDERS
    return (normalized,)


def verification_deadline_passed(
    deadline_at: datetime,
    *,
    now: datetime | None = None,
) -> bool:
    """True once the deadline plus CHALLENGE_SUBMIT_GRACE has elapsed."""
    current = now if now is not None else now_shanghai_naive()
    return deadline_at <= current - CHALLENGE_SUBMIT_GRACE


async def mark_group_banned(
    session: AsyncSession,
    group_id: int,
    user_id: int,
) -> _GroupBanState:
    warning = await session.scalar(
        select(UserWarning).where(
            UserWarning.group_id == group_id,
            UserWarning.user_id == user_id,
        )
    )
    if warning is None:
        session.add(
            UserWarning(
                group_id=group_id,
                user_id=user_id,
                count=0,
                is_banned=True,
            )
        )
        return None
    previous = (max(0, int(warning.count or 0)), bool(warning.is_banned))
    warning.is_banned = True
    return previous


async def rollback_group_ban(
    session: AsyncSession,
    group_id: int,
    user_id: int,
    previous: _GroupBanState,
) -> bool:
    """Undo our local-ban write only when no concurrent update replaced it."""
    if previous is None:
        result = await session.execute(
            delete(UserWarning).where(
                UserWarning.group_id == group_id,
                UserWarning.user_id == user_id,
                UserWarning.count == 0,
                UserWarning.is_banned.is_(True),
            )
        )
        return int(result.rowcount or 0) == 1

    previous_count, was_banned = previous
    if was_banned:
        return True
    result = await session.execute(
        update(UserWarning)
        .where(
            UserWarning.group_id == group_id,
            UserWarning.user_id == user_id,
            UserWarning.count == previous_count,
            UserWarning.is_banned.is_(True),
        )
        .values(is_banned=False)
    )
    return int(result.rowcount or 0) == 1


def verification_timeout_seconds_for_kind(
    settings: Settings,
    kind: str,
    group_settings: dict | None = None,
) -> int:
    """Challenge window for a verification kind, min-clamped to 60 seconds.

    Raid challenges honor the per-group raid_guard_challenge_timeout_seconds
    override when the caller can supply the group's settings dict; the other
    kinds only have global timeouts.
    """
    if kind == VERIFICATION_KIND_MODERATION:
        raw = settings.moderation.challenge_timeout_seconds
    elif kind == VERIFICATION_KIND_PATROL:
        raw = (
            getattr(settings, "patrol_challenge_timeout_seconds", 0)
            or settings.join_verification_timeout_seconds
        )
    elif kind == VERIFICATION_KIND_RAID:
        raw = 0
        if isinstance(group_settings, dict):
            override = group_settings.get("raid_guard_challenge_timeout_seconds")
            if not isinstance(override, bool):
                try:
                    raw = max(0, int(override))
                except (TypeError, ValueError):
                    raw = 0
        raw = (
            raw
            or getattr(settings, "raid_guard_challenge_timeout_seconds", 0)
            or settings.join_verification_timeout_seconds
        )
    else:
        raw = settings.join_verification_timeout_seconds
    return max(60, int(raw))


def verification_provider(
    settings: Settings,
    group_settings: dict | None = None,
) -> str:
    default = normalize_verification_provider(
        getattr(settings, "join_verification_provider", "turnstile")
    )
    if isinstance(group_settings, dict) and "join_verification_provider" in group_settings:
        return normalize_verification_provider(
            group_settings.get("join_verification_provider"),
            default=default,
        )
    return default


def verification_keys_for_provider(
    settings: Settings,
    provider: str,
) -> tuple[str, str]:
    """Site/secret pair for one base challenge service.

    The combined provider has no single key pair; callers handling it must
    iterate verification_subproviders() instead. An unknown value falls back
    to Turnstile, matching normalize_verification_provider.
    """
    if normalize_verification_provider(provider) == "hcaptcha":
        return (
            settings.join_verification_hcaptcha_site_key.strip(),
            settings.join_verification_hcaptcha_secret_key.strip(),
        )
    return (
        settings.join_verification_turnstile_site_key.strip(),
        settings.join_verification_turnstile_secret_key.strip(),
    )


def verification_keys(settings: Settings) -> tuple[str, str]:
    return verification_keys_for_provider(settings, verification_provider(settings))


def join_verification_policy(
    settings: Settings,
    group_settings: dict | None = None,
) -> tuple[bool, str]:
    enabled = bool(settings.join_verification_enabled)
    if isinstance(group_settings, dict) and "join_verification_enabled" in group_settings:
        raw_enabled = group_settings.get("join_verification_enabled")
        if isinstance(raw_enabled, str):
            normalized = raw_enabled.strip().lower()
            if normalized in {"1", "true", "yes", "on", "enable", "enabled"}:
                enabled = True
            elif normalized in {"0", "false", "no", "off", "disable", "disabled"}:
                enabled = False
        elif raw_enabled is not None:
            enabled = bool(raw_enabled)
    return enabled, verification_provider(settings, group_settings)


def _verification_config_fingerprint(
    settings: Settings,
    provider: str | None = None,
) -> tuple[str, ...]:
    selected_provider = normalize_verification_provider(
        provider,
        default=verification_provider(settings),
    )
    parts: list[str] = [selected_provider]
    for subprovider in verification_subproviders(selected_provider):
        parts.extend(verification_keys_for_provider(settings, subprovider))
    parts.append(settings.join_verification_public_base_url.strip().rstrip("/"))
    return tuple(parts)


def mark_turnstile_configuration_unavailable(
    settings: Settings,
    *,
    reason: str,
    provider: str | None = None,
) -> None:
    """Block one provider configuration until its credentials change.

    Blocks are stored per base service; a combined provider blocks each base
    service it expands to (callers that know which one failed should pass it).
    """
    selected_provider = normalize_verification_provider(
        provider,
        default=verification_provider(settings),
    )
    for subprovider in verification_subproviders(selected_provider):
        fingerprint = _verification_config_fingerprint(settings, subprovider)
        _BLOCKED_VERIFICATION_CONFIGS[(id(settings), subprovider)] = (
            settings,
            fingerprint,
            (reason or f"{subprovider} 配置不可用").strip(),
        )


def clear_turnstile_configuration_unavailable(
    settings: Settings,
    *,
    provider: str | None = None,
) -> None:
    """Clear a provider block after the same configuration verifies successfully."""
    selected_provider = normalize_verification_provider(
        provider,
        default=verification_provider(settings),
    )
    for subprovider in verification_subproviders(selected_provider):
        fingerprint = _verification_config_fingerprint(settings, subprovider)
        key = (id(settings), subprovider)
        blocked = _BLOCKED_VERIFICATION_CONFIGS.get(key)
        if blocked is not None and blocked[0] is settings and blocked[1] == fingerprint:
            _BLOCKED_VERIFICATION_CONFIGS.pop(key, None)


def turnstile_runtime_configuration_issue(
    settings: Settings,
    provider: str | None = None,
) -> str:
    """Return a provider's runtime block, clearing it after config edits.

    A combined provider reports the first blocked base service it expands to.
    """
    selected_provider = normalize_verification_provider(
        provider,
        default=verification_provider(settings),
    )
    for subprovider in verification_subproviders(selected_provider):
        fingerprint = _verification_config_fingerprint(settings, subprovider)
        key = (id(settings), subprovider)
        blocked = _BLOCKED_VERIFICATION_CONFIGS.get(key)
        if blocked is None or blocked[0] is not settings:
            continue
        if blocked[1] != fingerprint:
            _BLOCKED_VERIFICATION_CONFIGS.pop(key, None)
            continue
        return blocked[2]
    return ""

_FULL_RESTRICT = ChatPermissions(
    can_send_messages=False,
    can_send_audios=False,
    can_send_documents=False,
    can_send_photos=False,
    can_send_videos=False,
    can_send_video_notes=False,
    can_send_voice_notes=False,
    can_send_polls=False,
    can_send_other_messages=False,
    can_add_web_page_previews=False,
    can_react_to_messages=False,
    can_edit_tag=False,
    can_change_info=False,
    can_invite_users=False,
    can_pin_messages=False,
    can_manage_topics=False,
)
# Bot API: passing True for all permissions lifts the restriction entirely,
# returning the user to a normal member governed by chat default permissions.
_FULL_ALLOW = ChatPermissions(
    can_send_messages=True,
    can_send_audios=True,
    can_send_documents=True,
    can_send_photos=True,
    can_send_videos=True,
    can_send_video_notes=True,
    can_send_voice_notes=True,
    can_send_polls=True,
    can_send_other_messages=True,
    can_add_web_page_previews=True,
    can_react_to_messages=True,
    can_edit_tag=True,
    can_change_info=True,
    can_invite_users=True,
    can_pin_messages=True,
    can_manage_topics=True,
)


def turnstile_verification_configured(
    settings: Settings,
    provider: str | None = None,
) -> bool:
    """Return whether the selected challenge provider has complete config.

    The combined provider is only configured when every base service it
    expands to is configured and unblocked.
    """
    selected_provider = normalize_verification_provider(
        provider,
        default=verification_provider(settings),
    )
    if not settings.join_verification_public_base_url.strip():
        return False
    for subprovider in verification_subproviders(selected_provider):
        site_key, secret_key = verification_keys_for_provider(settings, subprovider)
        if (
            not site_key
            or not secret_key
            or site_key == secret_key
            or turnstile_runtime_configuration_issue(settings, subprovider)
        ):
            return False
    return True


def join_verification_ready(
    settings: Settings,
    group_settings: dict | None = None,
) -> bool:
    """Return whether new-member verification is enabled and configured."""
    enabled, provider = join_verification_policy(settings, group_settings)
    return bool(enabled and turnstile_verification_configured(settings, provider))


def moderation_challenge_ready(settings: Settings) -> bool:
    """Return whether low-confidence moderation challenges can be issued."""
    provider = verification_provider(settings)
    return bool(
        settings.moderation.enabled
        and turnstile_verification_configured(settings, provider)
    )


def verification_service_ready(
    settings: Settings,
    provider: str | None = None,
) -> bool:
    """The shared service also drains pending records after toggles are off."""
    return turnstile_verification_configured(settings, provider)


def build_mini_app_url(
    settings: Settings,
    provider: str | None = None,
    verification_id: int | None = None,
) -> str:
    base = settings.join_verification_public_base_url.strip().rstrip("/")
    url = f"{base}/verify"
    query: dict[str, str] = {}
    has_verification_id = verification_id is not None and int(verification_id) > 0
    if (
        provider is not None
        and (
            has_verification_id
            or normalize_verification_provider(provider)
            != verification_provider(settings)
        )
    ):
        query["provider"] = normalize_verification_provider(provider)
    if has_verification_id:
        query["verification_id"] = str(int(verification_id))
    if not query:
        return url
    return f"{url}?{urlencode(query)}"


def build_private_deep_link(
    bot_username: str,
    group_id: int | None = None,
) -> str:
    username = (bot_username or "").strip().lstrip("@")
    payload = START_VERIFY_PAYLOAD
    if group_id is not None:
        group_id = int(group_id)
        sign = "n" if group_id < 0 else "p"
        payload = f"{START_VERIFY_PAYLOAD}_{sign}{abs(group_id)}"
    return f"https://t.me/{username}?start={payload}"


def parse_private_verify_group_id(payload: str) -> int | None:
    normalized = str(payload or "").strip()
    prefix = f"{START_VERIFY_PAYLOAD}_"
    if not normalized.startswith(prefix):
        return None
    encoded = normalized[len(prefix) :]
    if len(encoded) < 2 or encoded[0] not in {"n", "p"} or not encoded[1:].isdigit():
        return None
    group_id = int(encoded[1:])
    return -group_id if encoded[0] == "n" else group_id


def _format_timeout(timeout_seconds: int) -> str:
    seconds = max(1, int(timeout_seconds))
    if seconds % 60 == 0:
        return f"{seconds // 60} 分钟"
    return f"{seconds} 秒"


def build_group_prompt_text(
    *,
    user_id: int,
    display_name: str,
    timeout_seconds: int,
) -> str:
    shown = html.escape((display_name or "").strip() or str(user_id))
    return (
        f'👋 欢迎 <a href="tg://user?id={user_id}">{shown}</a>！\n'
        "本群已开启入群真人验证，验证通过前你暂时无法发言。\n\n"
        f"请在 {_format_timeout(timeout_seconds)} 内点击下方按钮，"
        "与我私聊并完成人机验证；\n"
        "超时将被移出群聊（可重新加入再试）。"
    )


def build_verification_callback_data(action: str, user_id: int) -> str:
    normalized_action = str(action or "").strip().lower()
    if normalized_action not in VERIFICATION_CALLBACK_ACTIONS:
        raise ValueError(f"unsupported verification callback action: {action}")
    return f"{VERIFICATION_CALLBACK_PREFIX}:{normalized_action}:{int(user_id)}"


def parse_verification_callback_data(value: str) -> tuple[str, int] | None:
    parts = str(value or "").split(":")
    if len(parts) != 3 or parts[0] != VERIFICATION_CALLBACK_PREFIX:
        return None
    action = parts[1].strip().lower()
    if action not in VERIFICATION_CALLBACK_ACTIONS:
        return None
    try:
        user_id = int(parts[2])
    except (TypeError, ValueError):
        return None
    return (action, user_id) if user_id > 0 else None


def build_group_prompt_keyboard(user_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="🔐 开始验证",
                    callback_data=build_verification_callback_data(
                        VERIFICATION_CALLBACK_START,
                        user_id,
                    ),
                )
            ],
            [
                InlineKeyboardButton(
                    text="✅ 管理员通过",
                    callback_data=build_verification_callback_data(
                        VERIFICATION_CALLBACK_APPROVE,
                        user_id,
                    ),
                ),
                InlineKeyboardButton(
                    text="❌ 管理员拒绝",
                    callback_data=build_verification_callback_data(
                        VERIFICATION_CALLBACK_REJECT,
                        user_id,
                    ),
                ),
            ],
        ]
    )


def build_moderation_prompt_text(
    *,
    user_id: int,
    display_name: str,
    reason: str,
    timeout_seconds: int,
) -> str:
    shown = html.escape((display_name or "").strip() or str(user_id))
    safe_reason = html.escape((reason or "疑似命中群规").strip())
    return (
        f'⚠️ <a href="tg://user?id={user_id}">{shown}</a> 的消息触发了低置信度违规判定。\n'
        f"原因：{safe_reason}\n\n"
        "验证通过前你暂时无法继续发言。\n"
        f"请在 {_format_timeout(timeout_seconds)} 内点击下方按钮完成真人验证；\n"
        "验证成功后自动恢复发言权限，超时将被封禁。"
    )


def build_private_challenge_text(
    *,
    deadline_at: datetime,
    kind: str = VERIFICATION_KIND_JOIN,
    reason: str = "",
) -> str:
    deadline = deadline_at.strftime("%H:%M:%S")
    if kind == VERIFICATION_KIND_MODERATION:
        safe_reason = html.escape((reason or "疑似命中群规").strip())
        return (
            "<b>消息审查真人验证</b>\n"
            f"待核验原因：{safe_reason}\n"
            "点击下方按钮完成人机验证，通过后即可恢复群内发言权限。\n\n"
            f"请在今天 {deadline} 前完成，超时将被封禁。"
        )
    if kind == VERIFICATION_KIND_PATROL:
        safe_reason = html.escape((reason or "资料疑似命中群规").strip())
        return (
            "<b>资料巡检真人质询</b>\n"
            f"待核验原因：{safe_reason}\n"
            "点击下方按钮完成人机验证，通过后即可恢复群内发言权限。\n\n"
            f"请在今天 {deadline} 前完成，超时将被移出群聊（可重新加入）。"
        )
    if kind == VERIFICATION_KIND_RAID:
        safe_reason = html.escape((reason or "群组正遭遇批量加入").strip())
        return (
            "<b>爆破防护真人质询</b>\n"
            f"待核验原因：{safe_reason}\n"
            "点击下方按钮完成人机验证，通过后即可恢复群内发言权限。\n\n"
            f"请在今天 {deadline} 前完成，超时将被移出群聊（可重新加入）。"
        )
    return (
        "<b>入群真人验证</b>\n"
        "点击下方按钮，在弹出的窗口中完成人机验证，通过后即可在群内发言。\n\n"
        f"请在今天 {deadline} 前完成，超时将被移出群聊。"
    )


def build_private_challenge_keyboard(
    settings: Settings,
    provider: str | None = None,
    verification_id: int | None = None,
) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="✅ 开始验证",
                    web_app=WebAppInfo(
                        url=build_mini_app_url(
                            settings,
                            provider,
                            verification_id,
                        )
                    ),
                )
            ]
        ]
    )


async def get_join_verification(
    session: AsyncSession, group_id: int, user_id: int
) -> JoinVerification | None:
    # populate_existing: the sqlite upsert below writes past the identity map,
    # so a row loaded earlier in this session must be refreshed from the DB.
    stmt = (
        select(JoinVerification)
        .where(
            JoinVerification.group_id == group_id,
            JoinVerification.user_id == user_id,
        )
        .execution_options(populate_existing=True)
    )
    with session.no_autoflush:
        result = await session.execute(stmt)
    return result.scalar_one_or_none()


async def get_pending_verification_for_user(
    session: AsyncSession, user_id: int
) -> JoinVerification | None:
    stmt = (
        select(JoinVerification)
        .where(
            JoinVerification.user_id == user_id,
            JoinVerification.status == VERIFICATION_STATUS_PENDING,
        )
        .order_by(JoinVerification.deadline_at.desc())
        .limit(1)
        .execution_options(populate_existing=True)
    )
    with session.no_autoflush:
        result = await session.execute(stmt)
    return result.scalar_one_or_none()


async def get_sole_pending_verification_for_user(
    session: AsyncSession, user_id: int
) -> JoinVerification | None:
    """The user's pending record, but only when it is unambiguous.

    Used to recover a submission whose page pinned a stale verification id:
    with records pending in several groups there is no way to tell which one
    the page belonged to, so recovery must not guess.
    """
    stmt = (
        select(JoinVerification)
        .where(
            JoinVerification.user_id == user_id,
            JoinVerification.status == VERIFICATION_STATUS_PENDING,
        )
        .limit(2)
        .execution_options(populate_existing=True)
    )
    with session.no_autoflush:
        result = await session.execute(stmt)
    rows = list(result.scalars().all())
    return rows[0] if len(rows) == 1 else None


async def get_pending_verification_by_id_for_user(
    session: AsyncSession,
    verification_id: int,
    user_id: int,
) -> JoinVerification | None:
    stmt = (
        select(JoinVerification)
        .where(
            JoinVerification.id == int(verification_id),
            JoinVerification.user_id == int(user_id),
            JoinVerification.status == VERIFICATION_STATUS_PENDING,
        )
        .execution_options(populate_existing=True)
    )
    with session.no_autoflush:
        result = await session.execute(stmt)
    return result.scalar_one_or_none()


def _prepared_snapshot(row: JoinVerification) -> PreparedVerification:
    lease_until = getattr(row, "lease_until", None)
    if lease_until is None:
        raise ValueError("preparing verification has no lease")
    return PreparedVerification(
        verification_id=int(row.id),
        group_id=int(row.group_id),
        user_id=int(row.user_id),
        kind=str(row.kind),
        lease_until=lease_until,
        prompt_message_id=int(row.prompt_message_id or 0),
    )


async def prepare_join_verification(
    session: AsyncSession,
    *,
    group_id: int,
    user_id: int,
    deadline_at: datetime,
    kind: str = VERIFICATION_KIND_JOIN,
    reason: str = "",
    display_name: str = "",
    prompt_message_id: int = 0,
    provider: str = "turnstile",
    lease_until: datetime | None = None,
) -> PreparedVerification | None:
    """Persist a short preparation lease before muting or posting a prompt.

    A worker crash after this commit leaves a durable record that the sweeper
    can safely unmute and remove.  Existing records of another kind are never
    overwritten. Duplicate join prompts refresh their existing pending row via
    a separate generation CAS instead of converting it to preparation state.
    """
    if kind not in VERIFICATION_KINDS:
        raise ValueError(f"unsupported verification kind: {kind}")
    selected_provider = normalize_verification_provider(provider)
    preparing_lease = lease_until or (
        now_shanghai_naive() + timedelta(seconds=PREPARING_LEASE_SECONDS)
    )
    values = {
        "kind": kind,
        "provider": selected_provider,
        "reason": (reason or "")[:500],
        "display_name": (display_name or "")[:255],
        "prompt_message_id": int(prompt_message_id or 0),
        "deadline_at": deadline_at,
        "status": VERIFICATION_STATUS_PREPARING,
        "lease_until": preparing_lease,
    }
    await session.flush()
    dialect = getattr(getattr(session, "bind", None), "dialect", None)
    if getattr(dialect, "name", "") == "sqlite":
        stmt = sqlite_insert(JoinVerification).values(
            group_id=group_id,
            user_id=user_id,
            **values,
        )
        stmt = stmt.on_conflict_do_nothing(
            index_elements=[JoinVerification.group_id, JoinVerification.user_id]
        )
        result = await session.execute(stmt)
        if int(result.rowcount or 0) != 1:
            return None
    else:
        row = await get_join_verification(session, group_id, user_id)
        if row is None:
            try:
                async with session.begin_nested():
                    session.add(
                        JoinVerification(
                            group_id=group_id,
                            user_id=user_id,
                            **values,
                        )
                    )
                    await session.flush()
            except IntegrityError:
                return None
        else:
            return None

    row = await get_join_verification(session, group_id, user_id)
    if (
        row is None
        or str(row.status or "") != VERIFICATION_STATUS_PREPARING
        or row.lease_until != preparing_lease
    ):
        return None
    return _prepared_snapshot(row)


async def activate_prepared_join_verification(
    session: AsyncSession,
    *,
    prepared: PreparedVerification,
    prompt_message_id: int,
    deadline_at: datetime | None = None,
    now: datetime | None = None,
) -> bool:
    """Atomically turn this worker's unexpired preparation into pending."""
    values: dict[str, object] = {
        "status": VERIFICATION_STATUS_PENDING,
        "lease_until": None,
        "prompt_message_id": int(prompt_message_id or 0),
    }
    if deadline_at is not None:
        values["deadline_at"] = deadline_at
    current = now or now_shanghai_naive()
    result = await session.execute(
        update(JoinVerification)
        .where(
            JoinVerification.id == prepared.verification_id,
            JoinVerification.group_id == prepared.group_id,
            JoinVerification.user_id == prepared.user_id,
            JoinVerification.kind == prepared.kind,
            JoinVerification.status == VERIFICATION_STATUS_PREPARING,
            JoinVerification.lease_until == prepared.lease_until,
            JoinVerification.lease_until.is_not(None),
            JoinVerification.lease_until > current,
        )
        .values(**values)
    )
    return bool(result.rowcount)


async def commit_prepared_join_verification(
    session: AsyncSession,
    *,
    prepared: PreparedVerification,
    prompt_message_id: int,
    deadline_at: datetime | None = None,
) -> bool:
    """Activate and commit as one cancellation-shieldable unit."""
    activated = await activate_prepared_join_verification(
        session,
        prepared=prepared,
        prompt_message_id=prompt_message_id,
        deadline_at=deadline_at,
    )
    if not activated:
        await session.rollback()
        return False
    await session.commit()
    return True


async def delete_prepared_join_verification(
    session: AsyncSession,
    *,
    prepared: PreparedVerification,
) -> bool:
    """Delete only the exact preparation lease owned by this worker."""
    result = await session.execute(
        delete(JoinVerification).where(
            JoinVerification.id == prepared.verification_id,
            JoinVerification.status == VERIFICATION_STATUS_PREPARING,
            JoinVerification.lease_until == prepared.lease_until,
        )
    )
    return bool(result.rowcount)


async def lease_prepared_verification_release(
    session: AsyncSession,
    *,
    prepared: PreparedVerification,
    lease_until: datetime,
    prompt_message_id: int = 0,
) -> bool:
    """CAS one owned preparation into durable permission-recovery work."""
    result = await session.execute(
        update(JoinVerification)
        .where(
            JoinVerification.id == prepared.verification_id,
            JoinVerification.status == VERIFICATION_STATUS_PREPARING,
            JoinVerification.lease_until == prepared.lease_until,
        )
        .values(
            status=VERIFICATION_STATUS_RELEASING,
            lease_until=lease_until,
            prompt_message_id=int(
                prompt_message_id or prepared.prompt_message_id or 0
            ),
        )
    )
    return bool(result.rowcount)


async def renew_prepared_join_verification(
    session: AsyncSession,
    *,
    prepared: PreparedVerification,
    lease_until: datetime | None = None,
) -> PreparedVerification | None:
    """Heartbeat an exact preparation lease before the next Telegram call."""
    renewed_until = lease_until or (
        now_shanghai_naive() + timedelta(seconds=PREPARING_LEASE_SECONDS)
    )
    result = await session.execute(
        update(JoinVerification)
        .where(
            JoinVerification.id == prepared.verification_id,
            JoinVerification.status == VERIFICATION_STATUS_PREPARING,
            JoinVerification.lease_until == prepared.lease_until,
        )
        .values(lease_until=renewed_until)
    )
    if not result.rowcount:
        return None
    return PreparedVerification(
        verification_id=prepared.verification_id,
        group_id=prepared.group_id,
        user_id=prepared.user_id,
        kind=prepared.kind,
        lease_until=renewed_until,
        prompt_message_id=prepared.prompt_message_id,
    )


async def abort_prepared_join_verification(
    bot: Bot,
    session: AsyncSession,
    *,
    prepared: PreparedVerification,
    prompt_message_id: int = 0,
    restore_permissions: bool = True,
) -> bool:
    """Compensate a failed/cancelled preparation without deleting newer work."""
    if not restore_permissions:
        deleted = await delete_prepared_join_verification(session, prepared=prepared)
        if not deleted:
            await session.rollback()
            return False
        await session.commit()
    else:
        recovery_lease = now_shanghai_naive() + timedelta(
            seconds=TERMINAL_LEASE_SECONDS
        )
        leased = await lease_prepared_verification_release(
            session,
            prepared=prepared,
            lease_until=recovery_lease,
            prompt_message_id=prompt_message_id,
        )
        if not leased:
            # Never restore first: a concurrent activation may now own the
            # challenge. Losing this CAS means this worker must not change the
            # member's permissions.
            await session.rollback()
            return False
        await session.commit()
        restored = await restore_member_permissions(
            bot,
            prepared.group_id,
            prepared.user_id,
        )
        if not restored:
            # Leave the releasing lease for restart/sweeper recovery.
            return False
        completed = await complete_leased_join_verification(
            session,
            verification_id=prepared.verification_id,
            lease_until=recovery_lease,
            status=VERIFICATION_STATUS_RELEASING,
        )
        if not completed:
            await session.rollback()
            return False
        await session.commit()

    stale_prompts = {
        int(message_id)
        for message_id in (prompt_message_id, prepared.prompt_message_id)
        if int(message_id or 0) > 0
    }
    for stale_prompt in stale_prompts:
        await delete_verification_prompt(bot, prepared.group_id, stale_prompt)
    return True


async def shield_abort_prepared_join_verification(
    bot: Bot,
    session: AsyncSession,
    *,
    prepared: PreparedVerification,
    prompt_message_id: int = 0,
    restore_permissions: bool = True,
) -> bool:
    """Run preparation compensation to completion while propagating cancellation."""
    task = asyncio.create_task(
        abort_prepared_join_verification(
            bot,
            session,
            prepared=prepared,
            prompt_message_id=prompt_message_id,
            restore_permissions=restore_permissions,
        ),
        name=f"verification-abort:{prepared.group_id}:{prepared.user_id}",
    )
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        # A second shutdown cancellation must not detach a task that still uses
        # the caller's AsyncSession. Drain the shielded compensation first, then
        # propagate cancellation to the caller.
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                continue
        try:
            task.result()
        except asyncio.CancelledError:
            pass
        except Exception:
            log.exception(
                "verification abort failed while cancellation was pending | "
                "group=%s user=%s",
                prepared.group_id,
                prepared.user_id,
            )
        raise


async def upsert_join_verification(
    session: AsyncSession,
    *,
    group_id: int,
    user_id: int,
    deadline_at: datetime,
    kind: str = VERIFICATION_KIND_JOIN,
    reason: str = "",
    display_name: str = "",
    prompt_message_id: int = 0,
    provider: str = "turnstile",
) -> None:
    """Create or replace the pending verification for (group, user).

    A rejoin while a stale record exists must issue a fresh deadline, so this
    always resets every mutable field.
    """
    if kind not in VERIFICATION_KINDS:
        raise ValueError(f"unsupported verification kind: {kind}")
    selected_provider = normalize_verification_provider(provider)
    await session.flush()
    values = {
        "kind": kind,
        "provider": selected_provider,
        "reason": (reason or "")[:500],
        "display_name": (display_name or "")[:255],
        "prompt_message_id": prompt_message_id,
        "deadline_at": deadline_at,
        "status": "pending",
        "lease_until": None,
    }
    dialect = getattr(getattr(session, "bind", None), "dialect", None)
    if getattr(dialect, "name", "") == "sqlite":
        stmt = sqlite_insert(JoinVerification).values(
            group_id=group_id,
            user_id=user_id,
            **values,
        )
        stmt = stmt.on_conflict_do_update(
            index_elements=[JoinVerification.group_id, JoinVerification.user_id],
            set_=values,
            where=JoinVerification.status == "pending",
        )
        await session.execute(stmt)
        return

    row = await get_join_verification(session, group_id, user_id)
    if row is None:
        try:
            async with session.begin_nested():
                session.add(
                    JoinVerification(group_id=group_id, user_id=user_id, **values)
                )
                await session.flush()
            return
        except IntegrityError:
            row = await get_join_verification(session, group_id, user_id)
            if row is None:
                raise
    if str(getattr(row, "status", "pending") or "pending") != "pending":
        return
    for key, value in values.items():
        setattr(row, key, value)


async def refresh_pending_join_verification(
    session: AsyncSession,
    *,
    verification_id: int,
    deadline_at: datetime,
    kind: str,
    new_deadline_at: datetime,
    prompt_message_id: int,
    provider: str,
    display_name: str,
) -> bool:
    """Refresh a duplicate join prompt without discarding the old work item.

    The previous pending row remains authoritative until this exact-generation
    CAS commits. Prompt/restrict failures therefore keep the old challenge and
    its recovery state instead of deleting it or leaving its mute orphaned.
    """
    result = await session.execute(
        update(JoinVerification)
        .where(
            JoinVerification.id == int(verification_id),
            JoinVerification.deadline_at == deadline_at,
            JoinVerification.kind == kind,
            JoinVerification.status == VERIFICATION_STATUS_PENDING,
        )
        .values(
            deadline_at=new_deadline_at,
            prompt_message_id=int(prompt_message_id or 0),
            provider=normalize_verification_provider(provider),
            display_name=(display_name or "")[:255],
            lease_until=None,
        )
    )
    return bool(result.rowcount)


async def delete_join_verification(
    session: AsyncSession, group_id: int, user_id: int
) -> bool:
    await session.flush()
    result = await session.execute(
        delete(JoinVerification).where(
            JoinVerification.group_id == group_id,
            JoinVerification.user_id == user_id,
        )
    )
    return bool(result.rowcount)


async def delete_join_verifications_for_user(
    session: AsyncSession, user_id: int
) -> int:
    """Drop every pending verification for a user across all groups
    (used by /ban and /unban, which operate on the global registry)."""
    await session.flush()
    result = await session.execute(
        delete(JoinVerification).where(JoinVerification.user_id == user_id)
    )
    return int(result.rowcount or 0)


async def join_verification_prompts_for_user(
    session: AsyncSession,
    user_id: int,
) -> tuple[tuple[int, int], ...]:
    """Snapshot visible prompts before a bulk terminal delete.

    Global ban/unban paths delete several verification records at once. The
    Telegram messages are separate side effects, so callers retain these ids,
    commit the database transition, then remove every stale keyboard.
    """
    await session.flush()
    rows = (
        await session.execute(
            select(
                JoinVerification.group_id,
                JoinVerification.prompt_message_id,
            ).where(JoinVerification.user_id == int(user_id))
        )
    ).all()
    return tuple(
        (int(group_id), int(message_id))
        for group_id, message_id in rows
        if int(message_id or 0) > 0
    )


async def lease_join_verification_for_unban(
    session: AsyncSession,
    group_id: int,
    user_id: int,
    *,
    now: datetime | None = None,
) -> UnbanRecovery | None:
    """Persist cancellation before a manual group unban touches Telegram."""

    current = now or now_shanghai_naive()
    # Every enforcing heartbeat is capped at TERMINAL_LEASE_SECONDS and the
    # Telegram ban call is bounded.  This fixed barrier outlives both even when
    # the stale worker refreshed immediately before this atomic cancellation.
    recovery_at = current + timedelta(
        seconds=TERMINAL_LEASE_SECONDS + UNBAN_RECOVERY_GRACE_SECONDS
    )
    values = {
        "group_id": int(group_id),
        "user_id": int(user_id),
        "kind": VERIFICATION_KIND_MODERATION,
        "provider": "turnstile",
        "reason": "管理员解封恢复工单",
        "display_name": "",
        "prompt_message_id": 0,
        "deadline_at": current,
        "status": VERIFICATION_STATUS_UNBANNING,
        "lease_until": recovery_at,
    }
    dialect = getattr(getattr(session, "bind", None), "dialect", None)
    if getattr(dialect, "name", "") == "sqlite":
        await session.execute(
            sqlite_insert(JoinVerification)
            .values(**values)
            .on_conflict_do_nothing(
                index_elements=[JoinVerification.group_id, JoinVerification.user_id]
            )
        )
    else:
        existing = await get_join_verification(session, int(group_id), int(user_id))
        if existing is None:
            try:
                async with session.begin_nested():
                    session.add(JoinVerification(**values))
                    await session.flush()
            except IntegrityError:
                pass
    result = await session.execute(
        update(JoinVerification)
        .where(
            JoinVerification.group_id == int(group_id),
            JoinVerification.user_id == int(user_id),
        )
        .values(
            status=VERIFICATION_STATUS_UNBANNING,
            lease_until=recovery_at,
        )
    )
    if not result.rowcount:
        return None
    row = await get_join_verification(session, int(group_id), int(user_id))
    if (
        row is None
        or str(row.status or "") != VERIFICATION_STATUS_UNBANNING
        or row.lease_until != recovery_at
    ):
        return None
    return UnbanRecovery(
        verification_id=int(row.id),
        group_id=int(row.group_id),
        user_id=int(row.user_id),
        lease_until=recovery_at,
        prompt_message_id=int(row.prompt_message_id or 0),
    )


async def lease_join_verifications_for_user_unban(
    session: AsyncSession,
    user_id: int,
    *,
    group_ids: Iterable[int] = (),
    now: datetime | None = None,
) -> tuple[UnbanRecovery, ...]:
    """Persist per-group recovery journals before a global unban commit."""

    current = now or now_shanghai_naive()
    recovery_at = current + timedelta(
        seconds=TERMINAL_LEASE_SECONDS + UNBAN_RECOVERY_GRACE_SECONDS
    )
    for group_id in dict.fromkeys(int(value) for value in group_ids):
        await lease_join_verification_for_unban(
            session,
            group_id,
            int(user_id),
            now=current,
        )
    await session.execute(
        update(JoinVerification)
        .where(JoinVerification.user_id == int(user_id))
        .values(
            status=VERIFICATION_STATUS_UNBANNING,
            lease_until=recovery_at,
        )
    )
    rows = list(
        (
            await session.execute(
                select(JoinVerification)
                .where(
                    JoinVerification.user_id == int(user_id),
                    JoinVerification.status == VERIFICATION_STATUS_UNBANNING,
                    JoinVerification.lease_until == recovery_at,
                )
                .order_by(JoinVerification.id)
                .execution_options(populate_existing=True)
            )
        )
        .scalars()
        .all()
    )
    return tuple(
        UnbanRecovery(
            verification_id=int(row.id),
            group_id=int(row.group_id),
            user_id=int(row.user_id),
            lease_until=recovery_at,
            prompt_message_id=int(row.prompt_message_id or 0),
        )
        for row in rows
    )


async def claim_join_verification(
    session: AsyncSession,
    *,
    verification_id: int,
    deadline_at: datetime,
    kind: str,
    now: datetime,
    expired: bool,
    lease_until: datetime | None = None,
    target_status: str = VERIFICATION_STATUS_ENFORCING,
) -> bool:
    """Lease an unchanged pending record for one crash-safe terminal action.

    ``enforcing`` means the recovery worker must kick/ban; ``releasing`` means
    it must restore permissions.  The row remains durable until the Telegram
    side effect succeeds and :func:`complete_leased_join_verification` performs
    an exact lease CAS delete.

    The deadline is enforced with CHALLENGE_SUBMIT_GRACE: a pass may still be
    claimed until deadline+grace, and a timeout only after it. The two
    conditions stay complementary, so exactly one side can win at any instant.
    """
    if target_status not in {
        VERIFICATION_STATUS_ENFORCING,
        VERIFICATION_STATUS_RELEASING,
        VERIFICATION_STATUS_UNBANNING,
    }:
        raise ValueError(f"unsupported terminal verification status: {target_status}")
    await session.flush()
    cutoff = now - CHALLENGE_SUBMIT_GRACE
    deadline_condition = (
        JoinVerification.deadline_at <= cutoff
        if expired
        else JoinVerification.deadline_at > cutoff
    )
    terminal_lease = lease_until or (
        now + timedelta(seconds=TERMINAL_LEASE_SECONDS)
    )
    result = await session.execute(
        update(JoinVerification)
        .where(
            JoinVerification.id == verification_id,
            JoinVerification.deadline_at == deadline_at,
            JoinVerification.kind == kind,
            JoinVerification.status == VERIFICATION_STATUS_PENDING,
            deadline_condition,
        )
        .values(status=target_status, lease_until=terminal_lease)
    )
    return bool(result.rowcount)


async def lease_expired_join_verification(
    session: AsyncSession,
    *,
    record: JoinVerification,
    now: datetime,
    lease_until: datetime,
    target_status: str = VERIFICATION_STATUS_ENFORCING,
) -> bool:
    """Atomically lease one expired record for crash-safe terminal recovery."""

    if target_status not in {
        VERIFICATION_STATUS_ENFORCING,
        VERIFICATION_STATUS_RELEASING,
        VERIFICATION_STATUS_UNBANNING,
    }:
        raise ValueError(f"unsupported terminal verification status: {target_status}")
    await session.flush()
    status = str(
        getattr(record, "status", VERIFICATION_STATUS_PENDING)
        or VERIFICATION_STATUS_PENDING
    )
    conditions = [
        JoinVerification.id == int(record.id),
        JoinVerification.deadline_at == record.deadline_at,
        JoinVerification.kind == record.kind,
    ]
    if status in {
        VERIFICATION_STATUS_ENFORCING,
        VERIFICATION_STATUS_RELEASING,
        VERIFICATION_STATUS_UNBANNING,
    }:
        expected_lease = getattr(record, "lease_until", None)
        conditions.extend(
            [
                JoinVerification.status == status,
                JoinVerification.lease_until == expected_lease,
                JoinVerification.lease_until.is_not(None),
                JoinVerification.lease_until <= now,
            ]
        )
    else:
        conditions.extend(
            [
                JoinVerification.status == VERIFICATION_STATUS_PENDING,
                JoinVerification.deadline_at <= now - CHALLENGE_SUBMIT_GRACE,
            ]
        )
    result = await session.execute(
        update(JoinVerification)
        .where(*conditions)
        .values(status=target_status, lease_until=lease_until)
    )
    if not result.rowcount:
        return False
    record.status = target_status
    record.lease_until = lease_until
    return True


async def complete_leased_join_verification(
    session: AsyncSession,
    *,
    verification_id: int,
    lease_until: datetime,
    status: str = VERIFICATION_STATUS_ENFORCING,
) -> bool:
    """Delete a record only if this worker still owns its enforcement lease."""

    if status not in {
        VERIFICATION_STATUS_ENFORCING,
        VERIFICATION_STATUS_RELEASING,
        VERIFICATION_STATUS_UNBANNING,
    }:
        raise ValueError(f"unsupported terminal verification status: {status}")
    result = await session.execute(
        delete(JoinVerification).where(
            JoinVerification.id == int(verification_id),
            JoinVerification.status == status,
            JoinVerification.lease_until == lease_until,
        )
    )
    return bool(result.rowcount)


async def release_join_verification_lease(
    session: AsyncSession,
    *,
    verification_id: int,
    lease_until: datetime,
    retry_at: datetime,
    status: str = VERIFICATION_STATUS_ENFORCING,
) -> bool:
    """Return a failed enforcement attempt to the pending queue."""

    if status not in {
        VERIFICATION_STATUS_ENFORCING,
        VERIFICATION_STATUS_RELEASING,
        VERIFICATION_STATUS_UNBANNING,
    }:
        raise ValueError(f"unsupported terminal verification status: {status}")
    result = await session.execute(
        update(JoinVerification)
        .where(
            JoinVerification.id == int(verification_id),
            JoinVerification.status == status,
            JoinVerification.lease_until == lease_until,
        )
        .values(
            status=VERIFICATION_STATUS_PENDING,
            lease_until=None,
            deadline_at=retry_at,
        )
    )
    return bool(result.rowcount)


async def renew_join_verification_lease(
    session: AsyncSession,
    *,
    verification_id: int,
    lease_until: datetime,
    new_lease_until: datetime,
    status: str,
) -> bool:
    """CAS-renew one terminal lease without changing its recovery intent."""
    if status not in {
        VERIFICATION_STATUS_ENFORCING,
        VERIFICATION_STATUS_RELEASING,
        VERIFICATION_STATUS_UNBANNING,
    }:
        raise ValueError(f"unsupported terminal verification status: {status}")
    result = await session.execute(
        update(JoinVerification)
        .where(
            JoinVerification.id == int(verification_id),
            JoinVerification.status == status,
            JoinVerification.lease_until == lease_until,
        )
        .values(lease_until=new_lease_until)
    )
    return bool(result.rowcount)


async def list_expired_preparing_verifications(
    session: AsyncSession,
    *,
    now: datetime,
    limit: int = 50,
) -> list[JoinVerification]:
    stmt = (
        select(JoinVerification)
        .where(
            JoinVerification.status == VERIFICATION_STATUS_PREPARING,
            JoinVerification.lease_until.is_not(None),
            JoinVerification.lease_until <= now,
        )
        .order_by(JoinVerification.lease_until)
        .limit(max(1, limit))
    )
    with session.no_autoflush:
        result = await session.execute(stmt)
    return list(result.scalars().all())


async def lease_expired_preparing_verification(
    session: AsyncSession,
    *,
    record: JoinVerification,
    now: datetime,
    lease_until: datetime,
) -> bool:
    """Claim one expired preparation for idempotent permission recovery."""
    expected_lease = getattr(record, "lease_until", None)
    if expected_lease is None:
        return False
    result = await session.execute(
        update(JoinVerification)
        .where(
            JoinVerification.id == int(record.id),
            JoinVerification.status == VERIFICATION_STATUS_PREPARING,
            JoinVerification.lease_until == expected_lease,
            JoinVerification.lease_until <= now,
        )
        .values(lease_until=lease_until)
    )
    if not result.rowcount:
        return False
    record.lease_until = lease_until
    return True


async def list_expired_verifications(
    session: AsyncSession, *, now: datetime, limit: int = 50
) -> list[JoinVerification]:
    stmt = (
        select(JoinVerification)
        .where(
            or_(
                (
                    (JoinVerification.status == VERIFICATION_STATUS_PENDING)
                    & (JoinVerification.deadline_at <= now - CHALLENGE_SUBMIT_GRACE)
                ),
                (
                    JoinVerification.status.in_(
                        (
                            VERIFICATION_STATUS_ENFORCING,
                            VERIFICATION_STATUS_RELEASING,
                            VERIFICATION_STATUS_UNBANNING,
                        )
                    )
                    & JoinVerification.lease_until.is_not(None)
                    & (JoinVerification.lease_until <= now)
                ),
            )
        )
        .order_by(
            case(
                (JoinVerification.status == VERIFICATION_STATUS_UNBANNING, 0),
                (JoinVerification.status == VERIFICATION_STATUS_RELEASING, 1),
                else_=2,
            ),
            case(
                (
                    JoinVerification.status.in_(
                        (
                            VERIFICATION_STATUS_ENFORCING,
                            VERIFICATION_STATUS_RELEASING,
                            VERIFICATION_STATUS_UNBANNING,
                        )
                    ),
                    JoinVerification.lease_until,
                ),
                else_=JoinVerification.deadline_at,
            ),
        )
        .limit(max(1, limit))
    )
    with session.no_autoflush:
        result = await session.execute(stmt)
    return list(result.scalars().all())


async def verification_release_blocked_by_ban(
    session: AsyncSession,
    *,
    group_id: int,
    user_id: int,
) -> bool:
    if await is_globally_banned(session, user_id):
        return True
    return bool(
        await session.scalar(
            select(UserWarning.id).where(
                UserWarning.group_id == int(group_id),
                UserWarning.user_id == int(user_id),
                UserWarning.is_banned.is_(True),
            )
        )
    )


async def extend_pending_verification_deadlines(
    session: AsyncSession,
    *,
    settings: Settings,
    now: datetime | None = None,
    provider: str | None = None,
) -> int:
    """Give affected pending users a fresh window while a provider is unavailable."""
    current = now or now_shanghai_naive()
    stmt = (
        select(JoinVerification)
        .join(
            AuthorizedGroup,
            AuthorizedGroup.group_id == JoinVerification.group_id,
        )
        .where(JoinVerification.status == VERIFICATION_STATUS_PENDING)
    )
    if provider is not None:
        normalized = normalize_verification_provider(provider)
        # An unavailable base service also stalls combined records that
        # include it, so those must receive the same deadline extension.
        affected = sorted(
            candidate
            for candidate in VERIFICATION_PROVIDERS
            if candidate == normalized
            or normalized in verification_subproviders(candidate)
        )
        stmt = stmt.where(JoinVerification.provider.in_(affected))
    result = await session.execute(stmt)
    extended = 0
    for record in result.scalars().all():
        timeout_seconds = verification_timeout_seconds_for_kind(settings, record.kind)
        target = current + timedelta(seconds=timeout_seconds)
        if record.deadline_at < target:
            record.deadline_at = target
            extended += 1
    return extended


async def restrict_new_member(bot: Bot, chat_id: int, user_id: int) -> bool:
    try:
        restricted = await _bounded_telegram_call(
            bot.restrict_chat_member(
                chat_id,
                user_id,
                permissions=_FULL_RESTRICT,
            ),
            timeout_seconds=10.0,
        )
        return restricted is not False
    except Exception:
        log.exception("join verification restrict failed | chat=%s user=%s", chat_id, user_id)
    try:
        member = await _bounded_telegram_call(
            bot.get_chat_member(chat_id, user_id),
            timeout_seconds=4.0,
        )
        if (
            str(getattr(member, "status", "") or "") == "restricted"
            and not bool(getattr(member, "can_send_messages", False))
        ):
            log.info(
                "join verification restrict confirmed after ambiguous response | "
                "chat=%s user=%s",
                chat_id,
                user_id,
            )
            return True
    except Exception:
        log.debug(
            "join verification restrict status check failed | chat=%s user=%s",
            chat_id,
            user_id,
            exc_info=True,
        )
    return False


async def restore_member_permissions(bot: Bot, chat_id: int, user_id: int) -> bool:
    last_error: Exception | None = None
    for attempt in range(1, 4):
        try:
            restored = await _bounded_telegram_call(
                bot.restrict_chat_member(
                    chat_id,
                    user_id,
                    permissions=_FULL_ALLOW,
                    use_independent_chat_permissions=True,
                ),
                timeout_seconds=4.0,
            )
            if restored:
                return True
        except Exception as exc:
            last_error = exc
            log.warning(
                "join verification restore request failed | chat=%s user=%s attempt=%s/3 error=%s",
                chat_id,
                user_id,
                attempt,
                exc,
            )

        # Telegram may apply the permission update even when the HTTP response
        # is lost. Confirm the actual member state before retrying or reporting
        # failure to the Mini App.
        try:
            member = await _bounded_telegram_call(
                bot.get_chat_member(chat_id, user_id),
                timeout_seconds=4.0,
            )
            status = str(getattr(member, "status", "") or "")
            if status in {"member", "administrator", "creator", "left"} or (
                status == "restricted"
                and bool(getattr(member, "can_send_messages", False))
            ):
                log.info(
                    "join verification restore confirmed after ambiguous response | chat=%s user=%s",
                    chat_id,
                    user_id,
                )
                return True
        except Exception:
            log.debug(
                "join verification restore status check failed | chat=%s user=%s",
                chat_id,
                user_id,
                exc_info=True,
            )

        if attempt < 3:
            await asyncio.sleep(0.35 * attempt)

    log.error(
        "join verification restore failed | chat=%s user=%s error=%s",
        chat_id,
        user_id,
        last_error or "Telegram returned false",
    )
    return False


_KICK_CLEANUP_TASKS: set[asyncio.Task[bool]] = set()


async def flush_join_verification_telegram_tasks(
    *, timeout_seconds: float = 2.0
) -> None:
    """Cancel and boundedly drain Telegram children owned by this module."""

    state = _telegram_call_state()
    async with state.lock:
        state.draining = True
        tasks = {task for task in state.tasks if not task.done()}
    for task in tasks:
        task.cancel()
    if not tasks:
        return
    _done, pending = await asyncio.wait(
        tasks,
        timeout=max(0.0, float(timeout_seconds)),
    )
    if pending:
        log.error(
            "%d join-verification Telegram call(s) ignored bounded shutdown",
            len(pending),
        )


async def flush_kick_cleanup_tasks(*, timeout_seconds: float = 15.0) -> None:
    """Drain cleanup parents and their Telegram children within one deadline."""

    loop = asyncio.get_running_loop()
    timeout = max(0.0, float(timeout_seconds))
    deadline = loop.time() + timeout
    tasks = {
        task
        for task in _KICK_CLEANUP_TASKS
        if not task.done() and task.get_loop() is loop
    }
    if tasks:
        # Preserve most of the deadline for the safety-critical unban/reconcile
        # work, while retaining time to cancel any HTTP child that ignores the
        # parent's cancellation.
        cleanup_budget = timeout * 0.75
        _done, pending = await asyncio.wait(tasks, timeout=cleanup_budget)
        if pending:
            log.critical(
                "kick cleanup shutdown deadline reached | pending=%d",
                len(pending),
            )
            for task in pending:
                task.cancel()
            remaining = max(0.0, deadline - loop.time())
            if remaining:
                await asyncio.wait(pending, timeout=min(0.25, remaining))

    await flush_join_verification_telegram_tasks(
        timeout_seconds=max(0.0, deadline - loop.time())
    )


async def _bounded_telegram_call(awaitable: object, *, timeout_seconds: float) -> object:
    task = await _start_telegram_call(awaitable)

    try:
        done, _ = await asyncio.wait({task}, timeout=max(0.1, timeout_seconds))
    except asyncio.CancelledError:
        task.cancel()
        raise
    if task in done:
        return task.result()
    task.cancel()
    raise asyncio.TimeoutError


async def _ensure_kick_unbanned(
    bot: Bot,
    chat_id: int,
    user_id: int,
    preserve_ban: Callable[[], Awaitable[bool]] | None = None,
) -> bool:
    """Finish the second half of Telegram's ban+unban kick sequence."""

    for attempt in range(1, 4):
        if preserve_ban is not None:
            try:
                if await preserve_ban():
                    log.info(
                        "kick unban skipped | reason=current_ban_policy chat=%s user=%s",
                        chat_id,
                        user_id,
                    )
                    return True
            except Exception:
                log.exception(
                    "kick ban-policy check failed | chat=%s user=%s",
                    chat_id,
                    user_id,
                )
                # Failing open here could lift a newly-created durable ban.
                return False
        try:
            unbanned = await _bounded_telegram_call(
                bot.unban_chat_member(
                    chat_id,
                    user_id,
                    only_if_banned=True,
                ),
                timeout_seconds=4.0,
            )
            if unbanned is not False:
                return True
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning(
                "join verification kick cleanup failed | chat=%s user=%s "
                "attempt=%s/3 error=%s",
                chat_id,
                user_id,
                attempt,
                exc,
            )
        if attempt < 3:
            await asyncio.sleep(0.25 * attempt)
    return False


async def _wait_for_kick_cleanup(task: asyncio.Task[bool]) -> bool:
    """Give cleanup a bounded chance to finish even while caller is cancelling."""

    done, _ = await asyncio.wait({task}, timeout=15.0)
    if task in done:
        try:
            return bool(task.result())
        except (asyncio.CancelledError, Exception):
            return False
    log.critical("kick cleanup still pending after deadline | task=%s", task.get_name())
    return False


async def kick_member(
    bot: Bot,
    chat_id: int,
    user_id: int,
    *,
    preserve_ban: Callable[[], Awaitable[bool]] | None = None,
) -> bool:
    """Kick without leaving a permanent ban if cancellation races the calls."""

    try:
        banned = await _bounded_telegram_call(
            bot.ban_chat_member(chat_id, user_id),
            timeout_seconds=10.0,
        )
    except asyncio.CancelledError:
        # The HTTP response may have been lost after Telegram applied the ban.
        cleanup = asyncio.create_task(
            _ensure_kick_unbanned(bot, chat_id, user_id, preserve_ban),
            name=f"kick-unban:{chat_id}:{user_id}",
        )
        _KICK_CLEANUP_TASKS.add(cleanup)
        cleanup.add_done_callback(_KICK_CLEANUP_TASKS.discard)
        await _wait_for_kick_cleanup(cleanup)
        raise
    except Exception:
        log.exception("join verification kick ban failed | chat=%s user=%s", chat_id, user_id)
        # Treat the response as ambiguous and issue an idempotent unban.
        cleanup = asyncio.create_task(
            _ensure_kick_unbanned(bot, chat_id, user_id, preserve_ban),
            name=f"kick-unban:{chat_id}:{user_id}",
        )
        _KICK_CLEANUP_TASKS.add(cleanup)
        cleanup.add_done_callback(_KICK_CLEANUP_TASKS.discard)
        await _wait_for_kick_cleanup(cleanup)
        return False

    if banned is False:
        return False

    cleanup = asyncio.create_task(
        _ensure_kick_unbanned(bot, chat_id, user_id, preserve_ban),
        name=f"kick-unban:{chat_id}:{user_id}",
    )
    _KICK_CLEANUP_TASKS.add(cleanup)
    cleanup.add_done_callback(_KICK_CLEANUP_TASKS.discard)
    try:
        return await asyncio.shield(cleanup)
    except asyncio.CancelledError:
        await _wait_for_kick_cleanup(cleanup)
        raise


async def unban_member(
    bot: Bot,
    chat_id: int,
    user_id: int,
    *,
    preserve_ban: Callable[[], Awaitable[bool]] | None = None,
) -> bool:
    """Bounded idempotent unban, optionally preserving a newly-created policy."""
    if preserve_ban is not None:
        try:
            if await preserve_ban():
                await ban_member(bot, int(chat_id), int(user_id))
                return False
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception(
                "manual unban policy check failed | chat=%s user=%s",
                chat_id,
                user_id,
            )
            return False
    unbanned = await _ensure_kick_unbanned(
        bot,
        int(chat_id),
        int(user_id),
        preserve_ban,
    )
    if preserve_ban is not None:
        try:
            if await preserve_ban():
                await ban_member(bot, int(chat_id), int(user_id))
                return False
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception(
                "manual unban post-check failed | chat=%s user=%s",
                chat_id,
                user_id,
            )
            return False
    return unbanned


async def ban_member(bot: Bot, chat_id: int, user_id: int) -> bool:
    """Permanently ban a member; unlike kick_member this never unbans."""
    try:
        banned = await _bounded_telegram_call(
            bot.ban_chat_member(chat_id, user_id),
            timeout_seconds=10.0,
        )
        return banned is not False
    except Exception:
        log.exception("moderation challenge ban failed | chat=%s user=%s", chat_id, user_id)
    # The HTTP response can be lost after Telegram applies the ban. Confirm the
    # remote state before rolling back the durable local ban/releasing the work
    # item, otherwise a banned member could be left with a fresh pending prompt.
    try:
        member = await _bounded_telegram_call(
            bot.get_chat_member(chat_id, user_id),
            timeout_seconds=4.0,
        )
        if str(getattr(member, "status", "") or "") == "kicked":
            log.info(
                "moderation challenge ban confirmed after ambiguous response | "
                "chat=%s user=%s",
                chat_id,
                user_id,
            )
            return True
    except Exception:
        log.debug(
            "moderation challenge ban status check failed | chat=%s user=%s",
            chat_id,
            user_id,
            exc_info=True,
        )
    return False


async def reconcile_moderation_ban_after_lost_lease(
    bot: Bot,
    chat_id: int,
    user_id: int,
    preserve_ban: Callable[[], Awaitable[bool]],
) -> bool:
    """Reconcile a successful Telegram ban whose durable generation was lost.

    A manual group/global unban may delete both the local policy and the
    verification row after an enforcement worker's heartbeat but before its
    Telegram ban completes.  If the exact completion CAS then loses, retaining
    the remote ban would resurrect a policy the administrator just removed.
    Policy reads fail closed; a policy created during cleanup is re-enforced.
    """

    async def policy_blocks_release() -> bool:
        try:
            return bool(await preserve_ban())
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception(
                "moderation ban reconciliation policy check failed | "
                "chat=%s user=%s",
                chat_id,
                user_id,
            )
            return True

    async def reconcile() -> bool:
        if await policy_blocks_release():
            # The durable policy is authoritative. This also repairs a manual
            # unban that reached Telegram but crashed before its DB CAS commit.
            return await ban_member(bot, chat_id, user_id)
        if not await _ensure_kick_unbanned(
            bot,
            chat_id,
            user_id,
            policy_blocks_release,
        ):
            return False
        # A new durable ban created after the unban check wins and must be
        # reflected back to Telegram instead of being lifted by this cleanup.
        if await policy_blocks_release():
            return await ban_member(bot, chat_id, user_id)
        return await restore_member_permissions(bot, chat_id, user_id)

    task = asyncio.create_task(
        reconcile(),
        name=f"moderation-ban-reconcile:{chat_id}:{user_id}",
    )
    _KICK_CLEANUP_TASKS.add(task)
    task.add_done_callback(_KICK_CLEANUP_TASKS.discard)
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        await _wait_for_kick_cleanup(task)
        raise


async def delete_verification_prompt(
    bot: Bot,
    chat_id: int,
    message_id: int,
) -> bool:
    """Best-effort cleanup for a terminal or superseded challenge prompt.

    Verification records and Telegram messages are separate side effects. If
    an edit fails, a member leaves, or a rejoin replaces the record, deleting
    the old prompt prevents a live-looking keyboard from pointing at a record
    that no longer exists.
    """
    message_id = int(message_id or 0)
    if not message_id:
        return True
    try:
        deleted = await bot.delete_message(int(chat_id), message_id)
        return deleted is not False
    except Exception:
        log.debug(
            "verification prompt delete skipped | group=%s message=%s",
            chat_id,
            message_id,
            exc_info=True,
        )
    # If Telegram refuses deletion (for example a transient permission/state
    # mismatch), at least retire the live keyboard so subsequent taps cannot
    # keep reporting a confusing expired challenge.
    try:
        edited = await bot.edit_message_reply_markup(
            chat_id=int(chat_id),
            message_id=message_id,
            reply_markup=None,
        )
        return edited is not False
    except Exception:
        log.debug(
            "verification prompt keyboard cleanup skipped | group=%s message=%s",
            chat_id,
            message_id,
            exc_info=True,
        )
        return False


async def delete_verification_prompts(
    bot: Bot,
    prompts: Iterable[tuple[int, int]],
) -> int:
    """Best-effort parallel cleanup for a set of stale prompt keyboards."""
    unique = {
        (int(group_id), int(message_id))
        for group_id, message_id in prompts
        if int(message_id or 0) > 0
    }
    if not unique:
        return 0
    results = await asyncio.gather(
        *(
            delete_verification_prompt(bot, group_id, message_id)
            for group_id, message_id in unique
        ),
        return_exceptions=True,
    )
    return sum(result is True for result in results)


async def begin_moderation_challenge(
    *,
    bot: Bot,
    session: AsyncSession,
    settings: Settings,
    group_id: int,
    user_id: int,
    display_name: str,
    bot_username: str,
    reason: str,
) -> bool:
    """Mute a sender, issue a provider challenge, and persist its deadline.

    Returns False when the challenge is unavailable or cannot be presented.
    A prompt failure restores permissions so callers can safely fall back to
    the rule's normal high-confidence action without stranding the member.
    """
    key = (group_id, user_id)
    lock = _MODERATION_CHALLENGE_LOCKS.get(key)
    if lock is None:
        lock = asyncio.Lock()
        _MODERATION_CHALLENGE_LOCKS[key] = lock

    async with lock:
        return await _begin_moderation_challenge_locked(
            bot=bot,
            session=session,
            settings=settings,
            group_id=group_id,
            user_id=user_id,
            display_name=display_name,
            bot_username=bot_username,
            reason=reason,
        )


async def _begin_moderation_challenge_locked(
    *,
    bot: Bot,
    session: AsyncSession,
    settings: Settings,
    group_id: int,
    user_id: int,
    display_name: str,
    bot_username: str,
    reason: str,
) -> bool:
    if not moderation_challenge_ready(settings):
        return False

    # This handler has already performed several reads. Close any stale
    # snapshot before checking the record created by a concurrent message.
    await session.commit()
    current = await get_join_verification(session, group_id, user_id)
    if current is not None:
        current_status = str(current.status or VERIFICATION_STATUS_PENDING)
        # The duplicate path only needs this immutable challenge snapshot.
        # End the SELECT transaction before the Telegram restriction call.
        await session.commit()
        if (
            current.kind == VERIFICATION_KIND_MODERATION
            and current_status
            in {VERIFICATION_STATUS_PENDING, VERIFICATION_STATUS_ENFORCING}
        ):
            # Another concurrent message already issued the challenge. Keep
            # the original deadline and prompt instead of extending it.
            await restrict_new_member(bot, group_id, user_id)
            return True
        return False

    timeout_seconds = max(60, int(settings.moderation.challenge_timeout_seconds))
    deadline = now_shanghai_naive() + timedelta(seconds=timeout_seconds)
    prepared = await prepare_join_verification(
        session,
        group_id=group_id,
        user_id=user_id,
        deadline_at=deadline,
        kind=VERIFICATION_KIND_MODERATION,
        reason=(reason or "疑似命中群规")[:500],
        display_name=display_name,
        provider=verification_provider(settings),
    )
    if prepared is None:
        await session.rollback()
        return False
    await session.commit()

    prompt_message_id = 0
    activated = False
    try:
        if not await restrict_new_member(bot, group_id, user_id):
            await shield_abort_prepared_join_verification(
                bot,
                session,
                prepared=prepared,
            )
            return False
        sent = await bot.send_message(
            group_id,
            build_moderation_prompt_text(
                user_id=user_id,
                display_name=display_name,
                reason=reason,
                timeout_seconds=timeout_seconds,
            ),
            parse_mode="HTML",
            reply_markup=build_group_prompt_keyboard(user_id),
        )
        prompt_message_id = int(getattr(sent, "message_id", 0) or 0)

        activation_task = asyncio.create_task(
            commit_prepared_join_verification(
                session,
                prepared=prepared,
                prompt_message_id=prompt_message_id,
                deadline_at=deadline,
            ),
            name=f"moderation-challenge-activate:{group_id}:{user_id}",
        )
        try:
            activated = await asyncio.shield(activation_task)
        except asyncio.CancelledError:
            try:
                activated = await activation_task
            except Exception:
                activated = False
                log.exception(
                    "moderation challenge activation failed while cancellation was pending | "
                    "group=%s user=%s",
                    group_id,
                    user_id,
                )
            raise
        if not activated:
            await shield_abort_prepared_join_verification(
                bot,
                session,
                prepared=prepared,
                prompt_message_id=prompt_message_id,
            )
            return False
    except asyncio.CancelledError:
        if not activated:
            await shield_abort_prepared_join_verification(
                bot,
                session,
                prepared=prepared,
                prompt_message_id=prompt_message_id,
            )
        raise
    except Exception:
        log.exception(
            "moderation challenge setup failed | group=%s user=%s",
            group_id,
            user_id,
        )
        if not activated:
            await shield_abort_prepared_join_verification(
                bot,
                session,
                prepared=prepared,
                prompt_message_id=prompt_message_id,
            )
        return False
    log.info(
        "moderation challenge issued | group=%s user=%s timeout=%ss",
        group_id,
        user_id,
        timeout_seconds,
    )
    return True


async def warn_if_bot_cannot_verify(
    bot: Bot,
    settings: Settings,
    session_factory: "async_sessionmaker[AsyncSession] | None" = None,
) -> bool:
    """Startup self-check: verification silently does nothing unless the
    bot is a group admin with the restrict right.

    Telegram only delivers chat_member updates (member joins) to admin bots,
    and restrict_chat_member requires can_restrict_members. Checks every
    authorized group; returns True when all of them are in place.
    """
    if session_factory is None:
        return False
    from bot.services.authz import list_authorized_groups

    async with session_factory() as session:
        rows = await list_authorized_groups(session)
    group_ids = [int(row.group_id) for row in rows]
    if not group_ids:
        log.warning(
            "真人验证已启用，但当前没有任何已授权群（/authgroup），验证不会生效。"
        )
        return False

    try:
        me = await bot.me()
    except Exception:
        log.warning("join verification self-check failed: cannot resolve bot identity")
        return False

    all_ok = True
    for group_id in group_ids:
        try:
            member = await bot.get_chat_member(group_id, me.id)
        except Exception:
            log.warning(
                "join verification self-check failed: cannot query bot membership "
                "in group %s (is the bot in the group?)",
                group_id,
            )
            all_ok = False
            continue

        status = str(getattr(member, "status", "") or "")
        can_restrict = bool(getattr(member, "can_restrict_members", False))
        if status == "creator" or (status == "administrator" and can_restrict):
            continue
        log.warning(
            "真人验证已启用，但 bot 在群 %s 中%s：禁言/踢人需要「封禁用户」权限，"
            "入群事件也只会推送给管理员 bot。请将 bot 提升为管理员并勾选 Ban users，"
            "否则入群验证和消息审查质询不会生效。",
            group_id,
            "不是管理员" if status != "administrator" else "缺少「封禁用户」权限",
        )
        all_ok = False
    return all_ok


async def maybe_send_private_verification(
    message: Message,
    session: AsyncSession,
    settings: Settings,
    *,
    group_id: int | None = None,
) -> bool:
    """Handle /start in private chat for a user with a pending verification.

    Returns True when the Mini App button was sent (the caller should not
    fall through to the normal welcome text).
    """
    user = getattr(message, "from_user", None)
    if user is None:
        return False

    record = (
        await get_join_verification(session, int(group_id), user.id)
        if group_id is not None
        else await get_pending_verification_for_user(session, user.id)
    )
    if record is None:
        return False
    if verification_deadline_passed(record.deadline_at):
        # The sweeper will kick shortly; a fresh button would be useless.
        return False
    provider = normalize_verification_provider(record.provider)
    if not verification_service_ready(settings, provider):
        return False

    # Reaching the private chat is when the interactive solve actually starts;
    # the original deadline started at join/challenge time and may already be
    # mostly consumed by the group-prompt hop. Restart the window here so an
    # hCaptcha image challenge cannot be raced by the sweeper mid-solve.
    # Capped at 3x the timeout since issuance: repeated /start must not defer
    # the kick/ban forever.
    record_group_settings: dict | None = None
    if record.kind == VERIFICATION_KIND_RAID:
        from bot.db.models import Group

        group_row = await session.get(Group, int(record.group_id))
        if group_row is not None and isinstance(group_row.settings, dict):
            record_group_settings = group_row.settings
    timeout_seconds = verification_timeout_seconds_for_kind(
        settings, record.kind, record_group_settings
    )
    now = now_shanghai_naive()
    lifetime_cap = (record.created_at or now) + timedelta(seconds=3 * timeout_seconds)
    fresh_deadline = min(now + timedelta(seconds=timeout_seconds), lifetime_cap)
    shown_deadline = record.deadline_at
    if fresh_deadline > record.deadline_at:
        # Targeted UPDATE instead of mutating the ORM row: a concurrent pass
        # may have consumed the record, and 0 matched rows must not raise.
        result = await session.execute(
            update(JoinVerification)
            .where(
                JoinVerification.id == record.id,
                JoinVerification.deadline_at < fresh_deadline,
            )
            .values(deadline_at=fresh_deadline)
        )
        await session.commit()
        if result.rowcount:
            shown_deadline = fresh_deadline

    # The no-extension path above only performed SELECTs. Explicitly release
    # that transaction as well before waiting on Telegram; otherwise repeated
    # private /start requests can occupy the connection pool for the full Bot
    # API timeout even though no further database state is needed to render the
    # already-snapshotted challenge.
    await session.commit()
    await message.answer(
        build_private_challenge_text(
            deadline_at=shown_deadline,
            kind=record.kind,
            reason=record.reason,
        ),
        parse_mode="HTML",
        reply_markup=build_private_challenge_keyboard(
            settings,
            provider,
            int(record.id),
        ),
    )
    log.info(
        "verification mini app sent | kind=%s group=%s user=%s",
        record.kind,
        record.group_id,
        user.id,
    )
    return True


class JoinVerificationSweeper:
    """Background loop enforcing verification deadlines.

    Records survive restarts, so on each pass every expired (group, user) is
    kicked, its prompt message updated, and the record removed.
    """

    def __init__(
        self,
        *,
        bot: Bot,
        session_factory: async_sessionmaker[AsyncSession],
        check_interval_seconds: float = 30.0,
        settings: Settings | None = None,
    ) -> None:
        self.bot = bot
        self.session_factory = session_factory
        self.check_interval_seconds = max(5.0, float(check_interval_seconds))
        self.settings = settings
        self._paused_providers: set[str] = set()

    async def run_forever(self) -> None:
        log.info("verification sweeper started")
        consecutive_failures = 0
        while True:
            try:
                await self.sweep_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.exception("join verification sweep failed")
                consecutive_failures = record_background_failure(
                    service="join verification sweeper",
                    previous_failures=consecutive_failures,
                    error=exc,
                )
            else:
                consecutive_failures = 0
            await asyncio.sleep(self.check_interval_seconds)

    async def sweep_once(self) -> int:
        async with self.session_factory() as session:
            now = now_shanghai_naive()
            unavailable_providers: set[str] = set()
            if self.settings is not None:
                for provider in sorted(VERIFICATION_PROVIDERS):
                    if verification_service_ready(self.settings, provider):
                        continue
                    unavailable_providers.add(provider)
                    extended = await extend_pending_verification_deadlines(
                        session,
                        settings=self.settings,
                        now=now,
                        provider=provider,
                    )
                    if extended and provider not in self._paused_providers:
                        log.warning(
                            "verification timeout enforcement paused | provider=%s reason=%s "
                            "pending_extended=%d",
                            provider,
                            turnstile_runtime_configuration_issue(self.settings, provider)
                            or f"{provider} 配置不完整",
                            extended,
                        )
                resumed = self._paused_providers - unavailable_providers
                for provider in sorted(resumed):
                    log.info(
                        "verification timeout enforcement resumed | provider=%s",
                        provider,
                    )
                self._paused_providers = unavailable_providers
                await session.commit()
            expired_preparing = await list_expired_preparing_verifications(
                session,
                now=now,
            )
            preparing_claimed: list[JoinVerification] = []
            for record in expired_preparing:
                recovery_lease = now + timedelta(seconds=PREPARING_LEASE_SECONDS)
                won = await lease_expired_preparing_verification(
                    session,
                    record=record,
                    now=now,
                    lease_until=recovery_lease,
                )
                if won:
                    preparing_claimed.append(record)

            expired = await list_expired_verifications(session, now=now)
            if not expired and not preparing_claimed:
                return 0
            claimed: list[tuple[JoinVerification, bool, bool]] = []
            releasing_claimed: list[JoinVerification] = []
            unbanning_claimed: list[JoinVerification] = []
            unauthorized_enforcing_claimed: list[JoinVerification] = []
            authorization_cache: dict[int, bool] = {}
            for record in expired:
                group_id = int(record.group_id)
                authorized = authorization_cache.get(group_id)
                if authorized is None:
                    authorized = await is_group_authorized(session, group_id)
                    authorization_cache[group_id] = authorized
                record_status = str(
                    getattr(record, "status", VERIFICATION_STATUS_PENDING)
                    or VERIFICATION_STATUS_PENDING
                )
                record_provider = normalize_verification_provider(record.provider)
                if (
                    authorized
                    and record_status == VERIFICATION_STATUS_PENDING
                    and (
                        record_provider in unavailable_providers
                        or self.settings is not None
                        and not verification_service_ready(self.settings, record_provider)
                    )
                ):
                    if self.settings is not None:
                        timeout_seconds = verification_timeout_seconds_for_kind(
                            self.settings, record.kind
                        )
                        record.deadline_at = now + timedelta(seconds=timeout_seconds)
                    continue
                lease_until = now + timedelta(seconds=90)
                target_status = (
                    VERIFICATION_STATUS_UNBANNING
                    if record_status == VERIFICATION_STATUS_UNBANNING
                    else (
                        VERIFICATION_STATUS_RELEASING
                        if record_status == VERIFICATION_STATUS_RELEASING
                        or not authorized
                        and record_status == VERIFICATION_STATUS_PENDING
                        else VERIFICATION_STATUS_ENFORCING
                    )
                )
                won = await lease_expired_join_verification(
                    session,
                    record=record,
                    now=now,
                    lease_until=lease_until,
                    target_status=target_status,
                )
                if not won:
                    continue
                if (
                    not authorized
                    and record_status == VERIFICATION_STATUS_ENFORCING
                ):
                    unauthorized_enforcing_claimed.append(record)
                    continue
                if target_status == VERIFICATION_STATUS_UNBANNING:
                    unbanning_claimed.append(record)
                    continue
                if target_status == VERIFICATION_STATUS_RELEASING:
                    if not authorized:
                        log.info(
                            "verification enforcement converted to release | "
                            "reason=group_unauthorized group=%s user=%s",
                            record.group_id,
                            record.user_id,
                        )
                    releasing_claimed.append(record)
                    continue
                globally_banned = bool(
                    record.kind != VERIFICATION_KIND_MODERATION
                    and await is_globally_banned(session, record.user_id)
                )
                protected_owner = bool(
                    self.settings is not None
                    and is_super_admin_user_id(record.user_id, self.settings)
                )
                claimed.append((record, protected_owner, globally_banned))
            # Persist leases before Telegram calls.  A crash/cancellation leaves
            # the record recoverable after lease_until instead of orphaning a
            # muted/banned member with no durable work item.
            await session.commit()

        for record in preparing_claimed:
            await self._recover_preparing(record)

        for record in releasing_claimed:
            await self._recover_releasing(record)

        for record in unbanning_claimed:
            await self._recover_unbanning(record)

        for record in unauthorized_enforcing_claimed:
            await self._recover_unauthorized_enforcing(record)

        for record, protected_owner, globally_banned in claimed:
            if not await self._renew_terminal_record(
                record,
                status=VERIFICATION_STATUS_ENFORCING,
            ):
                continue
            log.info(
                "verification timeout | kind=%s group=%s user=%s",
                record.kind,
                record.group_id,
                record.user_id,
            )
            shown = html.escape(
                (record.display_name or "").strip() or str(record.user_id)
            )
            if globally_banned:
                # A kick is ban+unban and would lift the independent global
                # ban. Consume only this obsolete challenge.
                await self._complete_enforcement(record)
                continue
            if protected_owner:
                if not await self._recover_protected_owner(record):
                    continue
                await self._finalize_prompt(
                    record,
                    f"✅ <b>{shown}</b> 最高管理员无需消息审查验证，发言权限已恢复。",
                )
                continue
            if record.kind == VERIFICATION_KIND_MODERATION:
                enforced = await ban_member(self.bot, record.group_id, record.user_id)
                if not enforced:
                    released = await self._release_enforcement(record)
                    log.warning(
                        "moderation verification timeout ban failed | group=%s user=%s requeued=%s",
                        record.group_id,
                        record.user_id,
                        released,
                    )
                    continue
                if not await self._complete_enforcement(record, mark_banned=True):
                    continue
                text = f"⏰ <b>{shown}</b> 消息审查验证超时，已封禁。请联系管理员处理。"
            else:
                async def preserve_ban() -> bool:
                    async with self.session_factory() as session:
                        return await verification_release_blocked_by_ban(
                            session,
                            group_id=int(record.group_id),
                            user_id=int(record.user_id),
                        )

                enforced = await kick_member(
                    self.bot,
                    record.group_id,
                    record.user_id,
                    preserve_ban=preserve_ban,
                )
                if not enforced:
                    await self._release_enforcement(record)
                    log.warning(
                        "join verification timeout kick failed; requeued | "
                        "group=%s user=%s",
                        record.group_id,
                        record.user_id,
                    )
                    continue
                if not await self._complete_enforcement(record):
                    continue
                if record.kind == VERIFICATION_KIND_PATROL:
                    text = (
                        f"⏰ <b>{shown}</b> 资料巡检质询超时，"
                        "已移出群聊（未封禁，可重新加入）。"
                    )
                elif record.kind == VERIFICATION_KIND_RAID:
                    text = (
                        f"⏰ <b>{shown}</b> 爆破防护质询超时，"
                        "已移出群聊（未封禁，可重新加入）。"
                    )
                else:
                    text = (
                        f"⏰ <b>{shown}</b> 验证超时，已移出群聊。可重新加入再次验证。"
                    )
            await self._finalize_prompt(record, text)
        return (
            len(preparing_claimed)
            + len(releasing_claimed)
            + len(unbanning_claimed)
            + len(unauthorized_enforcing_claimed)
            + len(claimed)
        )

    async def _recover_preparing(self, record: JoinVerification) -> bool:
        """Restore and delete an expired preparation after owning its lease."""
        prepared = _prepared_snapshot(record)
        async with self.session_factory() as session:
            renewed = await renew_prepared_join_verification(
                session,
                prepared=prepared,
            )
            if renewed is None:
                await session.rollback()
                return False
            await session.commit()
            prepared = renewed
            blocked = await verification_release_blocked_by_ban(
                session,
                group_id=prepared.group_id,
                user_id=prepared.user_id,
            )
        if blocked:
            async with self.session_factory() as session:
                deleted = await delete_prepared_join_verification(
                    session,
                    prepared=prepared,
                )
                if deleted:
                    await session.commit()
                else:
                    await session.rollback()
                return deleted
        log.warning(
            "recovering expired verification preparation | kind=%s group=%s user=%s",
            prepared.kind,
            prepared.group_id,
            prepared.user_id,
        )
        restored = await restore_member_permissions(
            self.bot,
            prepared.group_id,
            prepared.user_id,
        )
        if not restored:
            log.warning(
                "preparing verification restore failed; retry after lease | group=%s user=%s",
                prepared.group_id,
                prepared.user_id,
            )
            return False
        async with self.session_factory() as session:
            deleted = await delete_prepared_join_verification(
                session,
                prepared=prepared,
            )
            if not deleted:
                await session.rollback()
                return False
            await session.commit()
        if prepared.prompt_message_id:
            await delete_verification_prompt(
                self.bot,
                prepared.group_id,
                prepared.prompt_message_id,
            )
        return True

    async def _recover_releasing(self, record: JoinVerification) -> bool:
        """Finish a crashed pass/approval/deauthorization permission release."""
        if not await self._renew_terminal_record(
            record,
            status=VERIFICATION_STATUS_RELEASING,
        ):
            return False
        lease_until = getattr(record, "lease_until", None)
        if lease_until is None:
            return False
        async with self.session_factory() as session:
            blocked = await verification_release_blocked_by_ban(
                session,
                group_id=int(record.group_id),
                user_id=int(record.user_id),
            )
        if blocked:
            async with self.session_factory() as session:
                completed = await complete_leased_join_verification(
                    session,
                    verification_id=int(record.id),
                    lease_until=lease_until,
                    status=VERIFICATION_STATUS_RELEASING,
                )
                if completed:
                    await session.commit()
                else:
                    await session.rollback()
                return completed
        restored = await restore_member_permissions(
            self.bot,
            int(record.group_id),
            int(record.user_id),
        )
        if not restored:
            log.warning(
                "verification release recovery failed; retry after lease | "
                "group=%s user=%s",
                record.group_id,
                record.user_id,
            )
            return False
        async with self.session_factory() as session:
            completed = await complete_leased_join_verification(
                session,
                verification_id=int(record.id),
                lease_until=lease_until,
                status=VERIFICATION_STATUS_RELEASING,
            )
            if not completed:
                await session.rollback()
                return False
            await session.commit()
        if (
            int(record.prompt_message_id or 0) > 0
            and record.kind not in SHARED_PROMPT_VERIFICATION_KINDS
        ):
            await delete_verification_prompt(
                self.bot,
                int(record.group_id),
                int(record.prompt_message_id),
            )
        return True

    async def _recover_unbanning(self, record: JoinVerification) -> bool:
        """Finish a crash-safe manual unban after stale enforcers have expired."""

        if not await self._renew_terminal_record(
            record,
            status=VERIFICATION_STATUS_UNBANNING,
        ):
            return False
        lease_until = getattr(record, "lease_until", None)
        if lease_until is None:
            return False

        async def preserve_ban() -> bool:
            async with self.session_factory() as session:
                return await verification_release_blocked_by_ban(
                    session,
                    group_id=int(record.group_id),
                    user_id=int(record.user_id),
                )

        reconciled = await reconcile_moderation_ban_after_lost_lease(
            self.bot,
            int(record.group_id),
            int(record.user_id),
            preserve_ban,
        )
        if not reconciled:
            return False
        async with self.session_factory() as session:
            completed = await complete_leased_join_verification(
                session,
                verification_id=int(record.id),
                lease_until=lease_until,
                status=VERIFICATION_STATUS_UNBANNING,
            )
            if completed:
                await session.commit()
            else:
                await session.rollback()
                return False
        if (
            int(record.prompt_message_id or 0) > 0
            and record.kind not in SHARED_PROMPT_VERIFICATION_KINDS
        ):
            await delete_verification_prompt(
                self.bot,
                int(record.group_id),
                int(record.prompt_message_id),
            )
        return True

    async def _recover_unauthorized_enforcing(
        self,
        record: JoinVerification,
    ) -> bool:
        """Stop deauthorized enforcement without leaving a half-kick ban."""
        if not await self._renew_terminal_record(
            record,
            status=VERIFICATION_STATUS_ENFORCING,
        ):
            return False
        lease_until = getattr(record, "lease_until", None)
        if lease_until is None:
            return False
        async with self.session_factory() as session:
            blocked = await verification_release_blocked_by_ban(
                session,
                group_id=int(record.group_id),
                user_id=int(record.user_id),
            )
        if not blocked:
            async def preserve_ban() -> bool:
                async with self.session_factory() as session:
                    return await verification_release_blocked_by_ban(
                        session,
                        group_id=int(record.group_id),
                        user_id=int(record.user_id),
                    )

            if not await _ensure_kick_unbanned(
                self.bot,
                int(record.group_id),
                int(record.user_id),
                preserve_ban,
            ):
                return False
            blocked = await preserve_ban()
            if not blocked:
                restored = await restore_member_permissions(
                    self.bot,
                    int(record.group_id),
                    int(record.user_id),
                )
            else:
                restored = True
            if not restored:
                try:
                    member = await self.bot.get_chat_member(
                        int(record.group_id),
                        int(record.user_id),
                    )
                    if str(getattr(member, "status", "") or "") not in {
                        "left",
                        "kicked",
                    }:
                        return False
                except Exception:
                    return False
        async with self.session_factory() as session:
            completed = await complete_leased_join_verification(
                session,
                verification_id=int(record.id),
                lease_until=lease_until,
                status=VERIFICATION_STATUS_ENFORCING,
            )
            if completed:
                await session.commit()
            else:
                await session.rollback()
            return completed

    async def _recover_protected_owner(self, record: JoinVerification) -> bool:
        """Never let any verification kind kick/ban the configured owner."""
        if not await _ensure_kick_unbanned(
            self.bot,
            int(record.group_id),
            int(record.user_id),
        ):
            return False
        if not await restore_member_permissions(
            self.bot,
            int(record.group_id),
            int(record.user_id),
        ):
            return False
        return await self._complete_enforcement(record)

    async def _renew_terminal_record(
        self,
        record: JoinVerification,
        *,
        status: str,
    ) -> bool:
        old_lease = getattr(record, "lease_until", None)
        if old_lease is None:
            return False
        new_lease = now_shanghai_naive() + timedelta(seconds=TERMINAL_LEASE_SECONDS)
        async with self.session_factory() as session:
            renewed = await renew_join_verification_lease(
                session,
                verification_id=int(record.id),
                lease_until=old_lease,
                new_lease_until=new_lease,
                status=status,
            )
            if not renewed:
                await session.rollback()
                return False
            await session.commit()
        record.lease_until = new_lease
        record.status = status
        return True

    async def _complete_enforcement(
        self,
        record: JoinVerification,
        *,
        mark_banned: bool = False,
    ) -> bool:
        lease_until = getattr(record, "lease_until", None)
        if lease_until is None:
            return False
        try:
            async with self.session_factory() as session:
                if mark_banned:
                    await mark_group_banned(session, record.group_id, record.user_id)
                completed = await complete_leased_join_verification(
                    session,
                    verification_id=record.id,
                    lease_until=lease_until,
                )
                if not completed:
                    await session.rollback()
                    if mark_banned:
                        await self._reconcile_lost_moderation_ban(record)
                    return False
                await session.commit()
                return True
        except asyncio.CancelledError:
            if mark_banned:
                await self._reconcile_lost_moderation_ban(record)
            raise
        except Exception:
            log.exception(
                "verification enforcement completion failed | group=%s user=%s",
                record.group_id,
                record.user_id,
            )
            if mark_banned:
                await self._reconcile_lost_moderation_ban(record)
            return False

    async def _reconcile_lost_moderation_ban(
        self,
        record: JoinVerification,
    ) -> bool:
        async def preserve_ban() -> bool:
            async with self.session_factory() as session:
                return await verification_release_blocked_by_ban(
                    session,
                    group_id=int(record.group_id),
                    user_id=int(record.user_id),
                )

        return await reconcile_moderation_ban_after_lost_lease(
            self.bot,
            int(record.group_id),
            int(record.user_id),
            preserve_ban,
        )

    async def _release_enforcement(self, record: JoinVerification) -> bool:
        lease_until = getattr(record, "lease_until", None)
        if lease_until is None:
            return False
        try:
            async with self.session_factory() as session:
                retry_lease = now_shanghai_naive() + timedelta(
                    seconds=TERMINAL_LEASE_SECONDS
                )
                released = await renew_join_verification_lease(
                    session,
                    verification_id=record.id,
                    lease_until=lease_until,
                    new_lease_until=retry_lease,
                    status=VERIFICATION_STATUS_ENFORCING,
                )
                await session.commit()
                if released:
                    record.lease_until = retry_lease
                return released
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception(
                "verification enforcement lease release failed | group=%s user=%s",
                record.group_id,
                record.user_id,
            )
            return False

    def _moderation_auto_delete_seconds(self) -> int:
        """Group's retention for moderation outcome notices ("审核通知")."""
        if self.settings is None:
            return 0
        return configured_auto_delete_seconds(self.settings, "moderation")

    async def _finalize_prompt(self, record: JoinVerification, text: str) -> None:
        # The member's name is already embedded in `text` (built by the sweep
        # loop), so each outcome is identifiable in both branches. The outcome
        # is a moderation notice ("审核通知"): honor the group's auto-delete
        # retention like the ban notice does.
        seconds = self._moderation_auto_delete_seconds()
        # A patrol/raid prompt is shared by several members; editing it would
        # remove the warning and its challenge button for the others, so post
        # the outcome as a separate message instead. The shared challenge
        # prompt itself is a different message, deliberately left untouched.
        if record.kind in SHARED_PROMPT_VERIFICATION_KINDS:
            try:
                sent = await self.bot.send_message(
                    record.group_id,
                    text,
                    parse_mode="HTML",
                )
                await schedule_message_auto_delete_durable(sent, seconds)
            except Exception:
                log.debug(
                    "patrol timeout notice failed | group=%s user=%s",
                    record.group_id,
                    record.user_id,
                )
            return
        message_id = int(record.prompt_message_id or 0)
        if not message_id:
            return
        try:
            # Repurpose the challenge prompt into the outcome, then clean it up
            # on the same moderation retention as the standalone notices.
            edited = await self.bot.edit_message_text(
                chat_id=record.group_id,
                message_id=message_id,
                text=text,
                parse_mode="HTML",
                reply_markup=None,
            )
            await schedule_message_auto_delete_durable(
                edited if not isinstance(edited, bool) else None, seconds
            )
        except Exception:
            log.debug(
                "join verification prompt finalize skipped | group=%s message=%s",
                record.group_id,
                message_id,
            )
            # The database row is already terminal. Do not leave a clickable
            # keyboard behind when Telegram rejected the in-place edit.
            await delete_verification_prompt(
                self.bot,
                record.group_id,
                message_id,
            )
