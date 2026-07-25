"""Raid guard: detect join floods, lock the group, retro-challenge suspects.

Detection is event-driven from the member-join handler: each authorized,
non-bot, non-banned join is recorded into an in-memory sliding window per
group. Reaching the configured threshold within the window triggers a
lockdown:

- For its duration every new join is removed immediately without banning or
  deleting message history, so the member may rejoin after the lockdown ends.
  No per-join notices are sent —
  the group only sees the single raid-protection announcement. The lockdown
  runs for a fixed duration: repelled joins do not extend it (a single
  bouncing account must not be able to lock the group forever), and a raid
  that outlasts it simply re-triggers on the next threshold of joins.
  One persistent status message is created for the lockdown and updated in
  place on recovery. A per-lockdown timer updates it even when no later member
  joins to drive another event.
- Members who joined within the lookback window before the trigger (the raid
  wave plus the accounts smuggled in just ahead of it) are fully muted and
  challenged: one message per chunk @-mentions them with a shared "真人质询"
  button (callback data carries no user id — the handler resolves the
  clicker's own pending kind="raid" record, so only mentioned suspects can
  use it). Passing the private Mini App challenge restores permissions;
  missing the deadline kicks WITHOUT banning.

Automatically detected lockdown state is intentionally in-memory only, while
its exact pin-ownership cleanup ledger is persisted so a restart can retire a
stale status pin. Manual lockdowns are persisted in ``Group.settings`` so both indefinite and timed
administrator locks survive a restart; startup restores their timers and
atomically claims expired records before updating their status message. Issued
challenges live in join_verifications and stay enforced by the deadline
sweeper. When the verification service is unavailable the lockdown still
protects the group, but no challenges are issued (muting members with no
challenge path would strand them).
"""
from __future__ import annotations

import asyncio
import html
import logging
import weakref
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Iterable, Mapping

from aiogram import Bot
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from bot.config import Settings
from bot.db.models import AuthorizedGroup, Group, JoinVerification
from bot.services.authz import is_group_authorized
from bot.services.join_screening import is_globally_banned
from bot.services.join_verification import (
    RAID_VERIFY_CALLBACK_DATA,
    TERMINAL_LEASE_SECONDS,
    VERIFICATION_KIND_RAID,
    VERIFICATION_STATUS_ENFORCING,
    VERIFICATION_STATUS_PENDING,
    PreparedVerification,
    activate_prepared_join_verification,
    ban_member,
    chat_member_is_present,
    claim_join_verification,
    close_private_challenge_message,
    complete_leased_join_verification,
    delete_verification_prompt,
    get_join_verification,
    join_verification_lease_is_current,
    kick_member,
    lease_expired_join_verification,
    restrict_new_member,
    prepare_join_verification,
    reconcile_moderation_ban_after_lost_lease,
    reconcile_stale_verification_restriction,
    renew_prepared_join_verification,
    renew_join_verification_lease,
    shield_abort_prepared_join_verification,
    verification_deadline_passed,
    verification_provider,
    verification_release_blocked_by_ban,
    verification_restriction_required,
    verification_service_ready,
    verification_timeout_seconds_for_kind,
)
from bot.services.message_templates import render_progress_notice, render_summary_notice
from bot.services.notification_pins import (
    notification_pin_enabled,
    pin_notification_message,
    unpin_notification_message,
)
from bot.services.recent_messages import (
    delete_messages_since_join,
    member_join_marker,
    retract_removed_member_residue,
)
from bot.utils.timezone import (
    now_shanghai_naive,
    to_shanghai_datetime,
    to_shanghai_naive,
)

log = logging.getLogger(__name__)

# Suspects mentioned per challenge message: keeps each message far below the
# 4096-char cap and within Telegram's per-message mention-notification limits.
# Bound parsed prompt text below Telegram's 4096 UTF-16-unit message limit even
# when every member uses a maximum-length emoji display name.
RAID_MENTIONS_PER_MESSAGE = 8
_RAID_DISPLAY_NAME_LIMIT = 128
# Upper bound on remembered joins per group; a raid larger than this still
# triggers long before the cap is reached.
_MAX_TRACKED_JOINS = 4096
# Shared raid challenge callback used by administrators to remove every still
# pending suspect mentioned by the clicked challenge message.
RAID_REMOVE_CALLBACK_DATA = "rgr"
# The persistent raid-protection status message lets group administrators end
# an active automatic or manual lockdown without typing a command.
RAID_GUARD_DISABLE_CALLBACK_DATA = "rgd"
# A command typo must not leave a group locked for an effectively unbounded
# number of years. Indefinite manual lockdown is represented by no duration;
# explicit minute values are capped at one week.
MAX_MANUAL_LOCKDOWN_MINUTES = 7 * 24 * 60
# Private service state. It is deliberately excluded from the Mini App's
# editable group-settings schema, while sharing the same JSON document with
# the group's other settings.
MANUAL_LOCKDOWN_SETTINGS_KEY = "raid_guard_manual_lockdown"
# Durable exact-message ownership ledger. Every message ID is recorded before
# a pin request is issued and removed only after Telegram confirms an exact
# unpin (or confirms the message no longer exists). This survives both manual
# terminal-state deletion and an automatic-lockdown process crash.
RAID_GUARD_PIN_OWNERSHIP_SETTINGS_KEY = "raid_guard_pin_ownership"
_MANUAL_LOCKDOWN_STATE_VERSION = 1
_MANUAL_LOCKDOWN_STATUS_MESSAGE_ID_KEY = "status_message_id"
_MANUAL_LOCKDOWN_PIN_MESSAGE_KEY = "pin_message"
_PIN_OWNERSHIP_STATE_VERSION = 1
_PIN_OWNERSHIP_MESSAGE_IDS_KEY = "message_ids"
_MAX_PIN_OWNERSHIP_IDS = 64
_PIN_CLEANUP_RETRY_SECONDS = 30.0
_PERSISTENCE_RETRIES = 5
_PERSISTENCE_ANY = object()
_PERSISTENCE_DELETE = object()
_INVALID_PERSISTED_STATE = object()

# Notices are intentionally short, but a cancelled Telegram HTTP request can
# ignore cancellation. Slots remain occupied until the real child exits so a
# broken transport cannot accumulate an unbounded number of detached tasks.
_NOTICE_CALL_CAPACITY = 8
_NOTICE_CALL_BACKPRESSURE_SECONDS = 0.5
_NOTICE_CALL_TASKS: set[asyncio.Future[object]] = set()


@dataclass(slots=True)
class _NoticeCallState:
    capacity: int
    semaphore: asyncio.BoundedSemaphore
    lock: asyncio.Lock
    tasks: set[asyncio.Future[object]]
    draining: bool = False
    waiters: int = 0


_NOTICE_CALL_STATES: weakref.WeakKeyDictionary[
    asyncio.AbstractEventLoop, _NoticeCallState
] = weakref.WeakKeyDictionary()

_service: "RaidGuardService | None" = None


class _NoticeCallCapacityError(RuntimeError):
    pass


def _notice_call_state() -> _NoticeCallState:
    loop = asyncio.get_running_loop()
    state = _NOTICE_CALL_STATES.get(loop)
    if state is None:
        capacity = max(1, int(_NOTICE_CALL_CAPACITY))
        state = _NoticeCallState(
            capacity=capacity,
            semaphore=asyncio.BoundedSemaphore(capacity),
            lock=asyncio.Lock(),
            tasks=set(),
        )
        _NOTICE_CALL_STATES[loop] = state
    return state


def _active_notice_call_tasks() -> set[asyncio.Future[object]]:
    loop = asyncio.get_running_loop()
    return {
        task
        for task in _NOTICE_CALL_TASKS
        if not task.done() and task.get_loop() is loop
    }


def _discard_notice_call_state_if_idle(
    loop: asyncio.AbstractEventLoop,
    state: _NoticeCallState,
) -> None:
    if state.draining or state.tasks or state.waiters or state.lock.locked():
        return
    if _NOTICE_CALL_STATES.get(loop) is state:
        _NOTICE_CALL_STATES.pop(loop, None)


def _observe_notice_call_task(
    task: asyncio.Future[object],
    state: _NoticeCallState,
) -> None:
    if task in state.tasks:
        state.tasks.discard(task)
        _NOTICE_CALL_TASKS.discard(task)
        state.semaphore.release()
    try:
        task.result()
    except (asyncio.CancelledError, Exception):
        pass
    _discard_notice_call_state_if_idle(task.get_loop(), state)


def _discard_unstarted_notice(awaitable: object) -> None:
    if asyncio.isfuture(awaitable):
        awaitable.cancel()
        return
    close = getattr(awaitable, "close", None)
    if callable(close):
        try:
            close()
        except Exception:
            pass


def _cancel_notice_acquire_and_return_late_permit(
    acquire_task: asyncio.Task[bool],
    state: _NoticeCallState,
) -> None:
    """Return a notice permit won concurrently with owner cancellation."""

    def _finished(done: asyncio.Task[bool]) -> None:
        if done.cancelled():
            return
        try:
            done.result()
        except (asyncio.CancelledError, Exception):
            return
        state.semaphore.release()
        _discard_notice_call_state_if_idle(done.get_loop(), state)

    acquire_task.cancel()
    if acquire_task.done():
        _finished(acquire_task)
    else:
        acquire_task.add_done_callback(_finished)


async def _start_notice_call(awaitable: object) -> asyncio.Future[object]:
    loop = asyncio.get_running_loop()
    state = _notice_call_state()
    acquired = False
    state.waiters += 1
    acquire_task = asyncio.create_task(state.semaphore.acquire())
    try:
        wait_seconds = max(0.0, float(_NOTICE_CALL_BACKPRESSURE_SECONDS))
        done, _pending = await asyncio.wait(
            {acquire_task},
            timeout=wait_seconds,
        )
        if acquire_task not in done:
            _cancel_notice_acquire_and_return_late_permit(acquire_task, state)
            raise asyncio.TimeoutError
        await acquire_task
        acquired = True
    except asyncio.TimeoutError as exc:
        _discard_unstarted_notice(awaitable)
        raise _NoticeCallCapacityError(
            "raid guard Telegram notice capacity is saturated"
        ) from exc
    except BaseException:
        if not acquired:
            _cancel_notice_acquire_and_return_late_permit(acquire_task, state)
        _discard_unstarted_notice(awaitable)
        raise
    finally:
        state.waiters -= 1
        if not acquired:
            _discard_notice_call_state_if_idle(loop, state)

    try:
        async with state.lock:
            if state.draining:
                raise _NoticeCallCapacityError(
                    "raid guard Telegram notices are shutting down"
                )
            task = asyncio.ensure_future(awaitable)
            state.tasks.add(task)
            _NOTICE_CALL_TASKS.add(task)
            task.add_done_callback(
                lambda done, owned_state=state: _observe_notice_call_task(
                    done, owned_state
                )
            )
            return task
    except BaseException:
        state.semaphore.release()
        _discard_unstarted_notice(awaitable)
        _discard_notice_call_state_if_idle(loop, state)
        raise


async def flush_raid_guard_telegram_tasks(*, timeout_seconds: float = 2.0) -> None:
    """Cancel and boundedly drain cancellation-resistant notice children."""

    state = _notice_call_state()
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
            "%d raid-guard Telegram notice call(s) ignored bounded shutdown",
            len(pending),
        )


async def _bounded_notice_call(awaitable: object, *, timeout_seconds: float) -> object:
    task = await _start_notice_call(awaitable)

    try:
        done, _ = await asyncio.wait({task}, timeout=max(0.1, timeout_seconds))
    except asyncio.CancelledError:
        task.cancel()
        raise
    if task in done:
        return task.result()
    task.cancel()
    raise asyncio.TimeoutError


def init_raid_guard_service(service: "RaidGuardService | None") -> None:
    global _service
    _service = service


def get_raid_guard_service() -> "RaidGuardService | None":
    return _service


# ---------------------------------------------------------------------------
# Policy / config resolution
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class RaidGuardConfig:
    enabled: bool
    pin_message: bool
    join_threshold: int
    window_seconds: int
    lockdown_seconds: int
    lookback_seconds: int
    challenge_timeout_seconds: int


def _group_bool(group_settings: dict | None, key: str, default: bool) -> bool:
    if not isinstance(group_settings, dict) or key not in group_settings:
        return default
    raw = group_settings.get(key)
    if isinstance(raw, str):
        normalized = raw.strip().lower()
        if normalized in {"1", "true", "yes", "on", "enable", "enabled"}:
            return True
        if normalized in {"0", "false", "no", "off", "disable", "disabled"}:
            return False
        return default
    if raw is None:
        return default
    return bool(raw)


def _group_int(
    group_settings: dict | None,
    key: str,
    *,
    minimum: int = 1,
) -> int | None:
    """Per-group integer override; absent/invalid values inherit the global."""
    if not isinstance(group_settings, dict) or key not in group_settings:
        return None
    raw = group_settings.get(key)
    if raw is None or isinstance(raw, bool):
        return None
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return None
    return value if value >= minimum else None


def raid_guard_policy(settings: Settings, group_settings: dict | None = None) -> bool:
    """Per-group raid_guard_enabled override; None inherits the global default."""
    return _group_bool(
        group_settings,
        "raid_guard_enabled",
        bool(getattr(settings, "raid_guard_enabled", False)),
    )


def resolve_raid_guard_config(
    settings: Settings,
    group_settings: dict | None = None,
) -> RaidGuardConfig:
    """Effective raid-guard knobs for one group, clamped to sane ranges."""
    threshold = _group_int(group_settings, "raid_guard_join_threshold") or int(
        getattr(settings, "raid_guard_join_threshold", 8)
    )
    window = _group_int(group_settings, "raid_guard_window_seconds") or int(
        getattr(settings, "raid_guard_window_seconds", 60)
    )
    lockdown = _group_int(group_settings, "raid_guard_lockdown_seconds") or int(
        getattr(settings, "raid_guard_lockdown_seconds", 600)
    )
    lookback_override = _group_int(
        group_settings, "raid_guard_lookback_seconds", minimum=0
    )
    lookback = (
        lookback_override
        if lookback_override is not None
        else int(getattr(settings, "raid_guard_lookback_seconds", 300))
    )
    timeout = _group_int(
        group_settings, "raid_guard_challenge_timeout_seconds"
    ) or int(getattr(settings, "raid_guard_challenge_timeout_seconds", 600))
    return RaidGuardConfig(
        enabled=raid_guard_policy(settings, group_settings),
        pin_message=notification_pin_enabled(
            settings,
            group_settings,
            "raid_guard",
        ),
        join_threshold=max(2, threshold),
        window_seconds=max(5, window),
        lockdown_seconds=max(60, lockdown),
        lookback_seconds=max(0, lookback),
        challenge_timeout_seconds=max(60, timeout),
    )


# ---------------------------------------------------------------------------
# Messages
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class RaidSuspect:
    user_id: int
    full_name: str
    username: str
    joined_at: datetime


def _mention(suspect: RaidSuspect) -> str:
    if suspect.username:
        return f"@{html.escape(str(suspect.username).strip()[:32])}"
    raw_label = (suspect.full_name or "").strip() or str(suspect.user_id)
    if len(raw_label) > _RAID_DISPLAY_NAME_LIMIT:
        raw_label = raw_label[: _RAID_DISPLAY_NAME_LIMIT - 3].rstrip() + "..."
    label = html.escape(raw_label)
    return f'<a href="tg://user?id={suspect.user_id}">{label}</a>'


def _format_duration(seconds: int) -> str:
    seconds = max(1, int(seconds))
    if seconds % 60 == 0:
        return f"{seconds // 60} 分钟"
    return f"{seconds} 秒"


def build_raid_lockdown_text(
    *,
    joined_count: int,
    window_seconds: int,
    lockdown_seconds: int,
) -> str:
    return render_summary_notice(
        "爆破防护 · 已触发",
        (
            f"检测到 {_format_duration(window_seconds)}内有 {joined_count} 名成员加入，"
            "已临时锁定新成员入口。"
        ),
        emphasis=f"<b>锁定时长：{_format_duration(lockdown_seconds)}。</b>",
        details=(
            "疑似遭遇批量爆破。\n"
            "期间新加入的成员会被自动移出（不封禁，解除后可重新加入）。"
        ),
    )


def build_manual_raid_lockdown_text(*, duration_minutes: int | None) -> str:
    if duration_minutes is None:
        duration_text = "锁定将持续到管理员手动解除。"
    else:
        duration_text = f"锁定时长：{_format_duration(duration_minutes * 60)}。"
    return render_summary_notice(
        "爆破防护 · 已开启",
        "已手动锁定新成员入口。",
        emphasis=f"<b>{duration_text}</b>",
        details=(
            "期间新加入的成员会被自动移出（不封禁，解除后可重新加入）。"
        ),
    )


def build_raid_unlock_text() -> str:
    return render_summary_notice(
        "爆破防护 · 已解除",
        "群组已恢复接收新成员。",
        details="此前被临时移出的用户现在可以重新加入。",
    )


def build_raid_lockdown_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="解除爆破防护",
                    callback_data=RAID_GUARD_DISABLE_CALLBACK_DATA,
                )
            ]
        ]
    )


def normalize_manual_lockdown_minutes(value: object | None) -> int | None:
    """Validate the optional ``/command <minutes>`` service argument.

    ``None`` means an indefinite manual lockdown. A supplied value is always
    interpreted as minutes, never seconds.
    """
    if value is None or str(value).strip() == "":
        return None
    if isinstance(value, bool):
        raise ValueError("爆破防护时长必须是分钟数字")
    try:
        minutes = int(str(value).strip())
    except (TypeError, ValueError) as exc:
        raise ValueError("爆破防护时长必须是分钟数字") from exc
    if minutes < 1 or minutes > MAX_MANUAL_LOCKDOWN_MINUTES:
        raise ValueError(
            f"爆破防护时长必须在 1-{MAX_MANUAL_LOCKDOWN_MINUTES} 分钟之间"
        )
    return minutes


def _manual_lockdown_state(
    until: datetime | None,
    *,
    status_message_id: int | None = None,
    pin_message: bool | None = None,
) -> dict[str, object]:
    """Return the canonical JSON document stored for one manual lockdown."""
    state: dict[str, object]
    if until is None:
        state = {
            "version": _MANUAL_LOCKDOWN_STATE_VERSION,
            "indefinite": True,
        }
    else:
        state = {
            "version": _MANUAL_LOCKDOWN_STATE_VERSION,
            # Include the UTC offset so the deadline remains unambiguous if the
            # host timezone changes. Runtime comparisons still use the project's
            # Asia/Shanghai-naive convention.
            "until": to_shanghai_datetime(until).isoformat(),
        }
    if status_message_id is not None and int(status_message_id) > 0:
        state[_MANUAL_LOCKDOWN_STATUS_MESSAGE_ID_KEY] = int(status_message_id)
    if pin_message is not None:
        state[_MANUAL_LOCKDOWN_PIN_MESSAGE_KEY] = bool(pin_message)
    return state


def _manual_lockdown_status_message_id(value: object) -> int | None:
    if not isinstance(value, Mapping):
        return None
    raw_message_id = value.get(_MANUAL_LOCKDOWN_STATUS_MESSAGE_ID_KEY)
    if isinstance(raw_message_id, bool):
        return None
    try:
        message_id = int(raw_message_id)
    except (TypeError, ValueError):
        return None
    return message_id if message_id > 0 else None


def _manual_lockdown_pin_message(value: object, *, default: bool) -> bool:
    if not isinstance(value, Mapping):
        return bool(default)
    raw = value.get(_MANUAL_LOCKDOWN_PIN_MESSAGE_KEY)
    if raw is None:
        return bool(default)
    if isinstance(raw, str):
        normalized = raw.strip().lower()
        if normalized in {"1", "true", "yes", "on", "enable", "enabled"}:
            return True
        if normalized in {"0", "false", "no", "off", "disable", "disabled"}:
            return False
        return bool(default)
    return bool(raw)


def _pin_ownership_message_ids(value: object) -> set[int]:
    """Parse the private durable exact-unpin ledger defensively."""

    if not isinstance(value, Mapping):
        return set()
    if value.get("version") != _PIN_OWNERSHIP_STATE_VERSION:
        return set()
    raw_ids = value.get(_PIN_OWNERSHIP_MESSAGE_IDS_KEY)
    if not isinstance(raw_ids, list):
        return set()
    message_ids: set[int] = set()
    for raw in raw_ids[:_MAX_PIN_OWNERSHIP_IDS]:
        if isinstance(raw, bool):
            continue
        try:
            message_id = int(raw)
        except (TypeError, ValueError):
            continue
        if message_id > 0:
            message_ids.add(message_id)
    return message_ids


def _pin_ownership_state(message_ids: Iterable[int]) -> dict[str, object]:
    normalized = sorted(
        {
            int(message_id)
            for message_id in message_ids
            if not isinstance(message_id, bool) and int(message_id) > 0
        }
    )[-_MAX_PIN_OWNERSHIP_IDS:]
    return {
        "version": _PIN_OWNERSHIP_STATE_VERSION,
        _PIN_OWNERSHIP_MESSAGE_IDS_KEY: normalized,
    }


def _parse_manual_lockdown_state(value: object) -> datetime | None | object:
    """Parse persisted state.

    ``None`` is the valid indefinite value. ``_INVALID_PERSISTED_STATE`` is
    returned for malformed documents so callers can distinguish them from an
    indefinite lock without another sentinel type.
    """
    if not isinstance(value, Mapping):
        return _INVALID_PERSISTED_STATE
    if value.get("version") != _MANUAL_LOCKDOWN_STATE_VERSION:
        return _INVALID_PERSISTED_STATE
    indefinite = value.get("indefinite")
    until_text = str(value.get("until") or "").strip()
    if indefinite is True:
        return None if not until_text else _INVALID_PERSISTED_STATE
    if indefinite not in (None, False) or not until_text:
        return _INVALID_PERSISTED_STATE
    try:
        parsed = datetime.fromisoformat(until_text.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return _INVALID_PERSISTED_STATE
    return to_shanghai_naive(parsed)


def build_raid_challenge_text(
    suspects: Iterable[RaidSuspect],
    *,
    timeout_seconds: int,
) -> str:
    members = list(suspects)
    details = ["涉及成员："]
    details.extend(f"• {_mention(suspect)}" for suspect in members)
    details.append("超时处理：移出群聊（不会封禁，可重新加入）。")
    return render_progress_notice(
        "爆破防护 · 真人质询",
        completed="已完成爆破风险识别",
        current="已暂时限制相关成员发言",
        next_step="完成人机验证后自动恢复权限",
        action=(
            "请被点名成员点击下方「真人质询」按钮。\n"
            f"<b>请在 {_format_duration(timeout_seconds)} 内完成。</b>"
        ),
        details=details,
    )


def build_raid_challenge_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="真人质询（仅被点名成员可用）",
                    callback_data=RAID_VERIFY_CALLBACK_DATA,
                )
            ],
            [
                InlineKeyboardButton(
                    text="一键移除被追溯用户（仅管理员）",
                    callback_data=RAID_REMOVE_CALLBACK_DATA,
                )
            ],
        ]
    )


@dataclass(slots=True)
class RaidRemovalResult:
    pending_count: int
    removed_user_ids: tuple[int, ...]
    failed_user_ids: tuple[int, ...]


async def remove_raid_challenged_users(
    *,
    bot: Bot,
    session: AsyncSession,
    settings: Settings,
    group_id: int,
    prompt_message_id: int,
    group_settings: dict | None = None,
    session_factory: async_sessionmaker[AsyncSession] | None = None,
) -> RaidRemovalResult:
    """Lease and kick pending raid suspects from one prompt crash-safely.

    The prompt message id scopes the bulk action to exactly the displayed
    chunk. Each record is moved to ``enforcing`` with a durable lease before
    Telegram is called. A process crash leaves a sweeper-recoverable work item;
    failed kicks CAS-release the exact generation back to pending.
    """
    group_id = int(group_id)
    prompt_message_id = int(prompt_message_id)
    del settings, group_settings
    now = now_shanghai_naive()
    if not await is_group_authorized(session, group_id):
        await session.rollback()
        return RaidRemovalResult(0, (), ())
    result = await session.execute(
        select(JoinVerification)
        .where(
            JoinVerification.group_id == group_id,
            JoinVerification.kind == VERIFICATION_KIND_RAID,
            JoinVerification.prompt_message_id == prompt_message_id,
            JoinVerification.status.in_(
                (VERIFICATION_STATUS_PENDING, VERIFICATION_STATUS_ENFORCING)
            ),
        )
        .order_by(JoinVerification.id)
    )
    records = list(result.scalars().all())
    claimed: list[tuple[dict[str, object], bool]] = []
    unclaimed_user_ids: list[int] = []
    for record in records:
        if not await is_group_authorized(session, group_id):
            await session.rollback()
            return RaidRemovalResult(
                pending_count=len(records),
                removed_user_ids=(),
                failed_user_ids=tuple(int(item.user_id) for item in records),
            )
        globally_banned = await is_globally_banned(session, int(record.user_id))
        lease_until = now + timedelta(seconds=TERMINAL_LEASE_SECONDS)
        status = str(record.status or VERIFICATION_STATUS_PENDING)
        if status == VERIFICATION_STATUS_PENDING:
            won = await claim_join_verification(
                session,
                verification_id=int(record.id),
                deadline_at=record.deadline_at,
                kind=record.kind,
                now=now,
                expired=verification_deadline_passed(record.deadline_at, now=now),
                lease_until=lease_until,
                target_status=VERIFICATION_STATUS_ENFORCING,
            )
        elif record.lease_until is not None and record.lease_until <= now:
            won = await lease_expired_join_verification(
                session,
                record=record,
                now=now,
                lease_until=lease_until,
                target_status=VERIFICATION_STATUS_ENFORCING,
            )
        else:
            won = False
        if not won:
            unclaimed_user_ids.append(int(record.user_id))
            continue
        claimed.append(
            (
                {
                    "group_id": int(record.group_id),
                    "user_id": int(record.user_id),
                    "provider": str(record.provider),
                    "reason": str(record.reason or ""),
                    "display_name": str(record.display_name or ""),
                    "prompt_message_id": int(record.prompt_message_id or 0),
                    "private_message_id": int(
                        getattr(record, "private_message_id", 0) or 0
                    ),
                    "verification_id": int(record.id),
                    "lease_until": lease_until,
                },
                globally_banned,
            )
        )
    await session.commit()

    if session_factory is not None:
        work_session_factory = session_factory
    elif getattr(session, "bind", None) is not None:
        work_session_factory = async_sessionmaker(
            bind=session.bind,
            class_=type(session),
            expire_on_commit=False,
            autoflush=False,
        )
    else:
        # Compatibility for isolated helper tests and non-SQLAlchemy adapters.
        # Production handlers always inject a real factory, so concurrent work
        # never shares one AsyncSession.
        class _BorrowedSession:
            async def __aenter__(self) -> AsyncSession:
                return session

            async def __aexit__(self, *_args: object) -> None:
                rollback = getattr(session, "rollback", None)
                if callable(rollback):
                    await rollback()

        work_session_factory = _BorrowedSession
    semaphore = asyncio.Semaphore(4)

    async def remove_one(
        snapshot: dict[str, object],
        globally_banned: bool,
    ) -> tuple[int, bool]:
        del globally_banned  # refresh policy after the queue/lease wait below
        user_id = int(snapshot["user_id"])
        async with semaphore:
            renewed_lease = now_shanghai_naive() + timedelta(
                seconds=TERMINAL_LEASE_SECONDS
            )
            async with work_session_factory() as work_session:
                if not await is_group_authorized(work_session, group_id):
                    await work_session.rollback()
                    return user_id, False
                renewed = await renew_join_verification_lease(
                    work_session,
                    verification_id=int(snapshot["verification_id"]),
                    lease_until=snapshot["lease_until"],
                    new_lease_until=renewed_lease,
                    status=VERIFICATION_STATUS_ENFORCING,
                )
                if not renewed:
                    await work_session.rollback()
                    return user_id, False
                current_global_ban = await is_globally_banned(work_session, user_id)
                lease_is_current = await join_verification_lease_is_current(
                    work_session,
                    verification_id=int(snapshot["verification_id"]),
                    lease_until=renewed_lease,
                    status=VERIFICATION_STATUS_ENFORCING,
                )
                await work_session.commit()
                if not lease_is_current:
                    return user_id, False

            member_present = await chat_member_is_present(bot, group_id, user_id)
            if member_present is None:
                return user_id, False
            if not member_present:
                async with work_session_factory() as completion_session:
                    completed = await complete_leased_join_verification(
                        completion_session,
                        verification_id=int(snapshot["verification_id"]),
                        lease_until=renewed_lease,
                        status=VERIFICATION_STATUS_ENFORCING,
                    )
                    if completed:
                        await completion_session.commit()
                    else:
                        await completion_session.rollback()
                if completed:
                    await close_private_challenge_message(
                        bot,
                        user_id,
                        int(snapshot.get("private_message_id") or 0),
                    )
                return user_id, bool(completed)

            async def preserve_ban() -> bool:
                async with work_session_factory() as policy_session:
                    return await verification_release_blocked_by_ban(
                        policy_session,
                        group_id=group_id,
                        user_id=user_id,
                    )

            # Snapshot before the removal: its leave update clears the live
            # join marker concurrently, and the post-removal residue sweep
            # still needs the membership window.
            residue_marker = member_join_marker(group_id, user_id)
            # No database session or application lock is held across these
            # Telegram calls. Each member has a hard outer deadline, while the
            # lower-level wrappers retain their cancellation-safe journals.
            try:
                async with asyncio.timeout(30.0):
                    if current_global_ban:
                        enforced = await ban_member(bot, group_id, user_id)
                    else:
                        enforced = await kick_member(
                            bot,
                            group_id,
                            user_id,
                            preserve_ban=preserve_ban,
                        )
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception(
                    "raid bulk removal failed | group=%s user=%s",
                    group_id,
                    user_id,
                )
                return user_id, False
            if not enforced:
                # Keep the removal intent durable. The sweeper retries after
                # the renewed lease instead of exposing a passable challenge.
                return user_id, False
            # The removed raider's join announcement, raced-in residue, and
            # "X was removed" notice stay visible to other members; retract
            # them so the unverified name gets no post-removal exposure.
            await retract_removed_member_residue(
                bot,
                group_id,
                user_id,
                marker=residue_marker,
            )

            async with work_session_factory() as completion_session:
                completed = await complete_leased_join_verification(
                    completion_session,
                    verification_id=int(snapshot["verification_id"]),
                    lease_until=renewed_lease,
                    status=VERIFICATION_STATUS_ENFORCING,
                )
                if completed:
                    await completion_session.commit()
                else:
                    await completion_session.rollback()
            if not completed and current_global_ban:
                async def current_policy_blocks_release() -> bool:
                    async with work_session_factory() as policy_session:
                        return await verification_release_blocked_by_ban(
                            policy_session,
                            group_id=group_id,
                            user_id=user_id,
                        )

                async def current_restriction_required() -> bool:
                    async with work_session_factory() as policy_session:
                        return await verification_restriction_required(
                            policy_session,
                            group_id=group_id,
                            user_id=user_id,
                        )

                await reconcile_moderation_ban_after_lost_lease(
                    bot,
                    group_id,
                    user_id,
                    current_policy_blocks_release,
                    restriction_required=current_restriction_required,
                )
            if completed:
                await close_private_challenge_message(
                    bot,
                    user_id,
                    int(snapshot.get("private_message_id") or 0),
                )
            return user_id, bool(completed)

    results = await asyncio.gather(
        *(remove_one(snapshot, globally_banned) for snapshot, globally_banned in claimed)
    )
    removed = [user_id for user_id, succeeded in results if succeeded]
    failed = [*unclaimed_user_ids, *(user_id for user_id, succeeded in results if not succeeded)]
    return RaidRemovalResult(
        pending_count=len(records),
        removed_user_ids=tuple(removed),
        failed_user_ids=tuple(failed),
    )


# ---------------------------------------------------------------------------
# The service
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class _JoinEvent:
    user_id: int
    full_name: str
    username: str
    at: datetime


class RaidGuardService:
    """Event-driven join-flood detector with per-group lockdown state.

    No polling loop is needed: an asyncio timer sends the unlock notice at the
    exact deadline, with the next join acting as a passive expiry fallback.
    Challenge deadlines are enforced by the shared verification sweeper.
    """

    def __init__(
        self,
        *,
        bot: Bot,
        settings: Settings,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        self.bot = bot
        self.settings = settings
        self.session_factory = session_factory
        self._recent_joins: dict[int, deque[_JoinEvent]] = {}
        # None is an explicit indefinite manual lockdown; absence means idle.
        self._lockdown_until: dict[int, datetime | None] = {}
        self._lockdown_source: dict[int, str] = {}
        self._lockdown_timers: dict[int, asyncio.TimerHandle] = {}
        self._lockdown_expiry_tasks: dict[int, asyncio.Task[bool]] = {}
        self._unlock_notice_tasks: dict[int, asyncio.Task[None]] = {}
        self._pin_cleanup_tasks: dict[int, asyncio.Task[None]] = {}
        # One status message represents one protection lifecycle. Its ID is
        # retained in the manual state as well, so an unlock after restart can
        # still retire the same message instead of posting a second notice.
        self._status_message_ids: dict[int, int] = {}
        # Whether this lifecycle asked the bot to pin its status message.  The
        # intent is retained even when Telegram rejected the original pin so a
        # later exact unpin remains safe and lifecycle-complete.
        self._status_pin_intents: dict[int, bool] = {}
        # Exact message IDs whose pin ownership has been durably claimed but
        # not yet durably released. Multiple IDs are possible when Telegram
        # rejects an edit and the old exact unpin is temporarily unavailable.
        self._owned_pin_message_ids: dict[int, set[int]] = {}
        # Serialize Telegram pin/unpin calls and their durable ledger changes
        # independently from the broader lifecycle lock. Cleanup retries must
        # not race a reconfiguration that is re-pinning the same exact status
        # message, while terminal paths may safely await cleanup while already
        # holding the lifecycle lock.
        self._pin_operation_locks: dict[int, asyncio.Lock] = {}
        # Exact raw JSON values are retained so expiry/disable can use an
        # optimistic compare-and-delete. Only the process that removes the
        # matching record updates the terminal state.
        self._manual_persisted_state: dict[int, dict[str, object]] = {}
        # Serialize every visible lockdown lifecycle transition for one group.
        # Telegram publication/retirement stays inside this lock so an
        # automatic activation cannot finish pinning after a concurrent off
        # action has already cleared the in-memory state.
        self._lifecycle_locks: dict[int, asyncio.Lock] = {}

    def _lifecycle_lock(self, group_id: int) -> asyncio.Lock:
        group_id = int(group_id)
        lock = self._lifecycle_locks.get(group_id)
        if lock is None:
            lock = asyncio.Lock()
            self._lifecycle_locks[group_id] = lock
        return lock

    def _pin_operation_lock(self, group_id: int) -> asyncio.Lock:
        group_id = int(group_id)
        lock = self._pin_operation_locks.get(group_id)
        if lock is None:
            lock = asyncio.Lock()
            self._pin_operation_locks[group_id] = lock
        return lock

    def _cancel_lockdown_timer(self, group_id: int) -> None:
        handle = self._lockdown_timers.pop(int(group_id), None)
        if handle is not None:
            handle.cancel()

    def _arm_lockdown(
        self,
        group_id: int,
        *,
        duration_seconds: int | None,
        source: str,
        pin_message: bool,
    ) -> datetime | None:
        until = (
            None
            if duration_seconds is None
            else now_shanghai_naive() + timedelta(seconds=max(1, int(duration_seconds)))
        )
        self._arm_lockdown_until(
            int(group_id),
            until=until,
            source=source,
            pin_message=pin_message,
        )
        return until

    def _arm_lockdown_until(
        self,
        group_id: int,
        *,
        until: datetime | None,
        source: str,
        pin_message: bool,
        persisted_state: Mapping[str, object] | None = None,
        schedule_expiry: bool = True,
    ) -> None:
        """Install an exact deadline, used both at activation and restore."""
        group_id = int(group_id)
        self._cancel_lockdown_timer(group_id)
        self._lockdown_until[group_id] = until
        self._lockdown_source[group_id] = str(source or "automatic")
        # Preserve the previous lifecycle's intent while reusing its status
        # message.  _publish_lockdown_status updates it after the edit succeeds
        # (and can then unpin when a newly resolved setting turns the option off).
        if group_id not in self._status_pin_intents:
            self._status_pin_intents[group_id] = bool(pin_message)
        if source == "manual" and persisted_state is not None:
            persisted = dict(persisted_state)
            self._manual_persisted_state[group_id] = persisted
            status_message_id = _manual_lockdown_status_message_id(persisted)
            if status_message_id is not None:
                self._status_message_ids[group_id] = status_message_id
        elif source != "manual":
            self._manual_persisted_state.pop(group_id, None)
        if until is not None and schedule_expiry:
            try:
                loop = asyncio.get_running_loop()
                self._lockdown_timers[group_id] = loop.call_later(
                    max(0.0, (until - now_shanghai_naive()).total_seconds()),
                    self._schedule_lockdown_expiry,
                    group_id,
                    until,
                )
            except RuntimeError:
                log.debug("[%s] raid unlock timer could not be scheduled", group_id)

    def _schedule_lockdown_expiry(
        self,
        group_id: int,
        expected_until: datetime,
    ) -> None:
        group_id = int(group_id)
        self._lockdown_timers.pop(group_id, None)
        existing = self._lockdown_expiry_tasks.get(group_id)
        if existing is not None and not existing.done():
            return
        try:
            loop = asyncio.get_running_loop()
            task = loop.create_task(
                self._expire_lockdown(group_id, expected_until),
                name=f"raid-unlock:{group_id}",
            )
            self._lockdown_expiry_tasks[group_id] = task

            def _finished(done: asyncio.Task[bool]) -> None:
                if self._lockdown_expiry_tasks.get(group_id) is done:
                    self._lockdown_expiry_tasks.pop(group_id, None)
                if done.cancelled():
                    return
                try:
                    done.result()
                except Exception:
                    log.exception(
                        "[%s] raid unlock task failed",
                        group_id,
                    )

            task.add_done_callback(_finished)
        except RuntimeError:
            log.debug("[%s] raid unlock notification could not be scheduled", group_id)

    async def _update_persisted_manual_state(
        self,
        group_id: int,
        *,
        value: Mapping[str, object] | object,
        expected: object = _PERSISTENCE_ANY,
    ) -> bool | None:
        """Optimistically set/delete the private manual-lockdown JSON key.

        ``True`` means this call changed the stored document, ``False`` means
        the requested value was already present or the expected prior state
        no longer matched, and ``None`` means repeated concurrent writes kept
        the update from committing. Comparing the full JSON snapshot mirrors
        the established Group.settings update strategy used by the proactive
        service and prevents us from replacing unrelated fields with a stale
        copy.
        """
        group_id = int(group_id)
        deleting = value is _PERSISTENCE_DELETE
        if not deleting and not isinstance(value, Mapping):
            raise TypeError("manual raid state must be a JSON object")
        normalized_value = None if deleting else dict(value)
        for _attempt in range(_PERSISTENCE_RETRIES):
            async with self.session_factory() as session:
                row = await session.get(Group, group_id)
                if row is None:
                    if deleting:
                        return False
                    settings_value = {
                        MANUAL_LOCKDOWN_SETTINGS_KEY: normalized_value,
                    }
                    session.add(Group(id=group_id, title="", settings=settings_value))
                    try:
                        await session.commit()
                    except IntegrityError:
                        # Another handler created the Group between SELECT and
                        # INSERT; retry against its fresh JSON document.
                        await session.rollback()
                        continue
                    return True

                stored_settings = row.settings
                updated_settings = dict(stored_settings or {})
                current = updated_settings.get(
                    MANUAL_LOCKDOWN_SETTINGS_KEY,
                    _PERSISTENCE_ANY,
                )
                if expected is not _PERSISTENCE_ANY and current != expected:
                    return False
                if deleting:
                    if current is _PERSISTENCE_ANY:
                        return False
                    updated_settings.pop(MANUAL_LOCKDOWN_SETTINGS_KEY, None)
                else:
                    if current == normalized_value:
                        return False
                    updated_settings[MANUAL_LOCKDOWN_SETTINGS_KEY] = normalized_value

                stmt = (
                    update(Group)
                    .where(
                        Group.id == group_id,
                        Group.settings == stored_settings,
                    )
                    .values(settings=updated_settings)
                    .execution_options(synchronize_session=False)
                )
                result = await session.execute(stmt)
                if int(result.rowcount or 0) == 1:
                    await session.commit()
                    return True
                await session.rollback()

        log.warning(
            "[%s] manual raid state update lost repeated concurrent races",
            group_id,
        )
        return None

    async def _update_persisted_pin_ownership(
        self,
        group_id: int,
        *,
        add: Iterable[int] = (),
        remove: Iterable[int] = (),
    ) -> bool:
        """CAS-update the private exact-unpin ledger without losing settings."""

        group_id = int(group_id)
        add_ids = {int(value) for value in add if int(value) > 0}
        remove_ids = {int(value) for value in remove if int(value) > 0}

        # A few lifecycle unit tests intentionally construct the service
        # without a database. Keep their exact-message ownership semantics in
        # memory; production always supplies the async session factory.
        if not callable(self.session_factory):
            current = set(self._owned_pin_message_ids.get(group_id, set()))
            updated = (current | add_ids) - remove_ids
            if updated:
                self._owned_pin_message_ids[group_id] = updated
            else:
                self._owned_pin_message_ids.pop(group_id, None)
            return True

        for _attempt in range(_PERSISTENCE_RETRIES):
            async with self.session_factory() as session:
                row = await session.get(Group, group_id)
                stored_settings = dict(row.settings or {}) if row is not None else {}
                current = _pin_ownership_message_ids(
                    stored_settings.get(RAID_GUARD_PIN_OWNERSHIP_SETTINGS_KEY)
                )
                updated = (current | add_ids) - remove_ids
                if len(updated) > _MAX_PIN_OWNERSHIP_IDS:
                    updated = set(sorted(updated)[-_MAX_PIN_OWNERSHIP_IDS:])

                next_settings = dict(stored_settings)
                if updated:
                    next_settings[RAID_GUARD_PIN_OWNERSHIP_SETTINGS_KEY] = (
                        _pin_ownership_state(updated)
                    )
                else:
                    next_settings.pop(RAID_GUARD_PIN_OWNERSHIP_SETTINGS_KEY, None)

                if next_settings == stored_settings:
                    if updated:
                        self._owned_pin_message_ids[group_id] = set(updated)
                    else:
                        self._owned_pin_message_ids.pop(group_id, None)
                    return True

                if row is None:
                    session.add(Group(id=group_id, title="", settings=next_settings))
                    try:
                        await session.commit()
                    except IntegrityError:
                        await session.rollback()
                        continue
                else:
                    result = await session.execute(
                        update(Group)
                        .where(
                            Group.id == group_id,
                            Group.settings == row.settings,
                        )
                        .values(settings=next_settings)
                        .execution_options(synchronize_session=False)
                    )
                    if int(result.rowcount or 0) != 1:
                        await session.rollback()
                        continue
                    await session.commit()

                if updated:
                    self._owned_pin_message_ids[group_id] = set(updated)
                else:
                    self._owned_pin_message_ids.pop(group_id, None)
                return True

        log.warning(
            "[%s] raid pin ownership update lost repeated concurrent races",
            group_id,
        )
        return False

    async def restore_manual_lockdowns(self) -> dict[str, int]:
        """Restore persisted manual lockdowns for every authorized group.

        Expired records are compare-and-deleted before an unlock notice is
        sent. Calling this method repeatedly (or starting overlapping bot
        processes) therefore cannot announce the same persisted expiry twice.
        """
        async with self.session_factory() as session:
            result = await session.execute(
                select(Group.id, Group.settings)
                .join(AuthorizedGroup, AuthorizedGroup.group_id == Group.id)
                .where(AuthorizedGroup.bot_present.is_(True))
                .order_by(Group.id)
            )
            rows = list(result.all())

        restored = 0
        expired = 0
        invalid = 0
        now = now_shanghai_naive()
        for raw_group_id, raw_settings in rows:
            group_id = int(raw_group_id)
            group_settings = dict(raw_settings or {})
            owned_pin_ids = _pin_ownership_message_ids(
                group_settings.get(RAID_GUARD_PIN_OWNERSHIP_SETTINGS_KEY)
            )
            if owned_pin_ids:
                self._owned_pin_message_ids[group_id] = set(owned_pin_ids)
            else:
                self._owned_pin_message_ids.pop(group_id, None)
            if MANUAL_LOCKDOWN_SETTINGS_KEY not in group_settings:
                await self._release_owned_status_pins(group_id)
                self._schedule_pin_cleanup(group_id)
                continue
            raw_state = group_settings[MANUAL_LOCKDOWN_SETTINGS_KEY]
            parsed_until = _parse_manual_lockdown_state(raw_state)
            if parsed_until is _INVALID_PERSISTED_STATE:
                try:
                    removed = await self._update_persisted_manual_state(
                        group_id,
                        value=_PERSISTENCE_DELETE,
                        expected=raw_state,
                    )
                except asyncio.CancelledError:
                    raise
                except Exception:
                    removed = None
                    log.exception(
                        "[%s] malformed manual raid state cleanup failed",
                        group_id,
                    )
                if removed:
                    invalid += 1
                log.warning(
                    "[%s] discarded malformed persisted manual raid state",
                    group_id,
                )
                await self._release_owned_status_pins(group_id)
                self._schedule_pin_cleanup(group_id)
                continue

            pin_message = _manual_lockdown_pin_message(
                raw_state,
                default=notification_pin_enabled(
                    self.settings,
                    group_settings,
                    "raid_guard",
                ),
            )

            if isinstance(parsed_until, datetime) and parsed_until <= now:
                canonical = (
                    dict(raw_state) if isinstance(raw_state, Mapping) else {}
                )
                self._arm_lockdown_until(
                    group_id,
                    until=parsed_until,
                    source="manual",
                    pin_message=pin_message,
                    persisted_state=canonical,
                    schedule_expiry=False,
                )
                self._status_pin_intents[group_id] = bool(pin_message)
                if await self._expire_lockdown(group_id, parsed_until):
                    expired += 1
                continue

            canonical = dict(raw_state) if isinstance(raw_state, Mapping) else {}
            self._arm_lockdown_until(
                group_id,
                until=parsed_until if isinstance(parsed_until, datetime) else None,
                source="manual",
                pin_message=pin_message,
                persisted_state=canonical,
            )
            # Repeated restore calls must follow the persisted lifecycle, not
            # preserve an older in-memory intent from a previous generation.
            self._status_pin_intents[group_id] = bool(pin_message)
            status_message_id = _manual_lockdown_status_message_id(canonical)
            if pin_message and status_message_id is not None:
                # Telegram normally preserves pins across a restart, but an
                # administrator may have removed it or the original pin call
                # may have failed ambiguously. Re-applying the exact persisted
                # message is idempotent and restores the active-state contract.
                await self._pin_status_message(group_id, status_message_id)
            await self._release_owned_status_pins(group_id)
            self._schedule_pin_cleanup(group_id)
            restored += 1

        if restored or expired or invalid:
            log.info(
                "manual raid state restored | active=%d expired=%d invalid=%d",
                restored,
                expired,
                invalid,
            )
        return {
            "restored": restored,
            "expired": expired,
            "invalid": invalid,
        }

    def _clear_lockdown(self, group_id: int) -> str | None:
        group_id = int(group_id)
        if group_id not in self._lockdown_until:
            return None
        self._cancel_lockdown_timer(group_id)
        self._lockdown_until.pop(group_id, None)
        self._recent_joins.pop(group_id, None)
        self._manual_persisted_state.pop(group_id, None)
        return self._lockdown_source.pop(group_id, "automatic")

    async def _pin_status_message(self, group_id: int, message_id: int) -> bool:
        async with self._pin_operation_lock(group_id):
            return await self._pin_status_message_locked(group_id, message_id)

    async def _pin_status_message_locked(
        self,
        group_id: int,
        message_id: int,
    ) -> bool:
        """Pin while the caller owns this group's pin-operation lock."""

        try:
            claimed = await self._update_persisted_pin_ownership(
                group_id,
                add=(int(message_id),),
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            claimed = False
            log.exception(
                "[%s] raid status pin ownership persistence failed | message=%s",
                group_id,
                message_id,
            )
        if not claimed:
            # Never issue an outcome-ambiguous pin that cannot be recovered
            # after this process exits.
            return False
        operation = asyncio.create_task(
            _bounded_notice_call(
                pin_notification_message(
                    self.bot,
                    chat_id=group_id,
                    message_id=message_id,
                    kind="raid_guard",
                ),
                timeout_seconds=10.0,
            ),
            name=f"raid-pin:{group_id}:{message_id}",
        )
        try:
            pinned = await asyncio.shield(operation)
        except asyncio.CancelledError:
            # Do not release the lifecycle lock while a pin request can still
            # land after a concurrent terminal exact-unpin. The durable ledger
            # remains owned and shutdown/startup recovery can reconcile it.
            try:
                await asyncio.shield(operation)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.warning(
                    "[%s] raid status pin completed ambiguously during cancellation "
                    "| message=%s",
                    group_id,
                    message_id,
                    exc_info=True,
                )
            raise
        except Exception:
            log.warning(
                "[%s] raid status pin call failed | message=%s",
                group_id,
                message_id,
                exc_info=True,
            )
            return False
        return bool(pinned)

    async def _unpin_status_message(
        self,
        group_id: int,
        message_id: int,
        *,
        timeout_seconds: float = 10.0,
    ) -> bool:
        async with self._pin_operation_lock(group_id):
            return await self._unpin_status_message_locked(
                group_id,
                message_id,
                timeout_seconds=timeout_seconds,
            )

    async def _unpin_status_message_locked(
        self,
        group_id: int,
        message_id: int,
        *,
        timeout_seconds: float = 10.0,
    ) -> bool:
        """Unpin and release ownership under the pin-operation lock."""

        try:
            unpinned = await _bounded_notice_call(
                unpin_notification_message(
                    self.bot,
                    chat_id=group_id,
                    message_id=message_id,
                    kind="raid_guard",
                ),
                timeout_seconds=max(0.1, float(timeout_seconds)),
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            log.warning(
                "[%s] raid status unpin call failed | message=%s",
                group_id,
                message_id,
                exc_info=True,
            )
            return False
        if not unpinned:
            return False
        try:
            released = await self._update_persisted_pin_ownership(
                group_id,
                remove=(int(message_id),),
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            released = False
            log.exception(
                "[%s] raid status pin release persistence failed | message=%s",
                group_id,
                message_id,
            )
        return bool(released)

    def _pin_cleanup_candidates(self, group_id: int) -> tuple[int, ...]:
        group_id = int(group_id)
        candidates = set(self._owned_pin_message_ids.get(group_id, set()))
        current_message_id = self._status_message_ids.get(group_id)
        if (
            group_id in self._lockdown_until
            and self._status_pin_intents.get(group_id, False)
            and current_message_id is not None
        ):
            candidates.discard(int(current_message_id))
        return tuple(sorted(candidates))

    async def _release_owned_status_pins(self, group_id: int) -> bool:
        """Release every non-active exact pin owned by this raid guard."""

        group_id = int(group_id)
        async with self._pin_operation_lock(group_id):
            # Recompute only after serialization: a delayed cleanup task may
            # have been queued while the status was configured unpinned, then
            # wake after a newer /raidguard on has made that same ID active.
            for message_id in self._pin_cleanup_candidates(group_id):
                await self._unpin_status_message_locked(group_id, message_id)
            return not self._pin_cleanup_candidates(group_id)

    def _schedule_pin_cleanup(
        self,
        group_id: int,
        *,
        delay_seconds: float = _PIN_CLEANUP_RETRY_SECONDS,
    ) -> None:
        group_id = int(group_id)
        if not self._pin_cleanup_candidates(group_id):
            return
        existing = self._pin_cleanup_tasks.get(group_id)
        if existing is not None and not existing.done():
            if delay_seconds > 0:
                return
            # A terminal transition must not wait behind an earlier delayed
            # stale-ID retry. Replace it with an immediate cleanup generation.
            existing.cancel()
            if self._pin_cleanup_tasks.get(group_id) is existing:
                self._pin_cleanup_tasks.pop(group_id, None)

        async def cleanup() -> None:
            if delay_seconds > 0:
                await asyncio.sleep(float(delay_seconds))
            await self._release_owned_status_pins(group_id)

        try:
            task = asyncio.create_task(
                cleanup(),
                name=f"raid-pin-cleanup:{group_id}",
            )
        except RuntimeError:
            log.debug("[%s] raid pin cleanup could not be scheduled", group_id)
            return
        self._pin_cleanup_tasks[group_id] = task

        def finished(done: asyncio.Task[None]) -> None:
            if self._pin_cleanup_tasks.get(group_id) is done:
                self._pin_cleanup_tasks.pop(group_id, None)
            if done.cancelled():
                return
            try:
                done.result()
            except Exception:
                log.exception("[%s] raid pin cleanup task failed", group_id)
            if self._pin_cleanup_candidates(group_id):
                self._schedule_pin_cleanup(group_id)

        task.add_done_callback(finished)

    async def _publish_lockdown_status(
        self,
        group_id: int,
        text: str,
        *,
        pin_message: bool,
        defer_pin: bool = False,
    ) -> int | None:
        """Create or refresh the one visible status message for a lockdown."""
        group_id = int(group_id)
        message_id = self._status_message_ids.get(group_id)
        previous_pin_intent = bool(self._status_pin_intents.get(group_id, False))
        keyboard = build_raid_lockdown_keyboard()
        if message_id is not None:
            try:
                async with self._pin_operation_lock(group_id):
                    await _bounded_notice_call(
                        self.bot.edit_message_text(
                            chat_id=group_id,
                            message_id=message_id,
                            text=text,
                            parse_mode="HTML",
                            reply_markup=keyboard,
                        ),
                        timeout_seconds=10.0,
                    )
                    # Publish the newer desired state before reconciling the
                    # exact pin. A delayed cleanup that runs next will therefore
                    # protect this active ID, while non-deferred pin/unpin calls
                    # remain serialized under the same operation lock.
                    self._status_pin_intents[group_id] = bool(pin_message)
                    if pin_message:
                        if not defer_pin:
                            await self._pin_status_message_locked(
                                group_id,
                                message_id,
                            )
                    elif previous_pin_intent:
                        await self._unpin_status_message_locked(
                            group_id,
                            message_id,
                        )
                if not pin_message:
                    self._schedule_pin_cleanup(group_id)
                return message_id
            except asyncio.CancelledError:
                raise
            except Exception:
                # The original notice may have been deleted or aged out of
                # Telegram's edit window. A fresh active status is preferable
                # to leaving the group with no visible way to release it.
                if self._status_message_ids.get(group_id) == message_id:
                    self._status_message_ids.pop(group_id, None)
                if previous_pin_intent:
                    await self._unpin_status_message(group_id, message_id)
                    self._schedule_pin_cleanup(group_id)
                log.warning(
                    "[%s] raid status update failed; replacing message",
                    group_id,
                    exc_info=True,
                )

        try:
            sent = await _bounded_notice_call(
                self.bot.send_message(
                    group_id,
                    text,
                    parse_mode="HTML",
                    reply_markup=keyboard,
                ),
                timeout_seconds=10.0,
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("[%s] raid lockdown status delivery failed", group_id)
            return None
        message_id = int(getattr(sent, "message_id", 0) or 0)
        if message_id > 0:
            async with self._pin_operation_lock(group_id):
                self._status_message_ids[group_id] = message_id
                self._status_pin_intents[group_id] = bool(pin_message)
                if pin_message and not defer_pin:
                    await self._pin_status_message_locked(group_id, message_id)
            return message_id
        log.warning("[%s] raid lockdown status has no message ID", group_id)
        return None

    async def _remember_manual_status_message_locked(
        self,
        group_id: int,
        message_id: int | None,
    ) -> bool:
        """Persist a manual status ID while the caller owns the group lock."""
        if message_id is None or int(message_id) <= 0:
            return False
        group_id = int(group_id)
        message_id = int(message_id)
        if self._lockdown_source.get(group_id) != "manual":
            return False
        persisted = self._manual_persisted_state.get(group_id)
        if persisted is None:
            return False
        if _manual_lockdown_status_message_id(persisted) == message_id:
            return True
        updated = dict(persisted)
        updated[_MANUAL_LOCKDOWN_STATUS_MESSAGE_ID_KEY] = message_id
        try:
            saved = await self._update_persisted_manual_state(
                group_id,
                value=updated,
                expected=persisted,
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception(
                "[%s] manual raid status message persistence failed",
                group_id,
            )
            return False
        if saved:
            self._manual_persisted_state[group_id] = updated
            return True
        if saved is None:
            log.warning(
                "[%s] manual raid status message persistence lost races",
                group_id,
            )
        else:
            log.warning(
                "[%s] manual raid status message persistence changed generation",
                group_id,
            )
        return False

    async def _send_unlock_notice(
        self,
        group_id: int,
        *,
        source: str,
        expected_message_id: int | None = None,
    ) -> None:
        """Retire the current status message without sending a second notice."""
        group_id = int(group_id)
        if expected_message_id is not None:
            if (
                group_id in self._lockdown_until
                or self._status_message_ids.get(group_id) != expected_message_id
            ):
                return
        message_id = self._status_message_ids.get(group_id)
        pin_message = bool(self._status_pin_intents.get(group_id, False))
        if message_id is None:
            log.debug(
                "[%s] raid unlock status has no tracked message | source=%s",
                group_id,
                source,
            )
            self._status_pin_intents.pop(group_id, None)
            self._schedule_pin_cleanup(group_id, delay_seconds=0.0)
            return
        try:
            # Protection state notices are intentionally persistent and do not
            # participate in any general moderation auto-delete policy.
            await _bounded_notice_call(
                self.bot.edit_message_text(
                    chat_id=group_id,
                    message_id=message_id,
                    text=build_raid_unlock_text(),
                    parse_mode="HTML",
                    reply_markup=None,
                ),
                timeout_seconds=10.0,
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception(
                "[%s] raid unlock status update failed | source=%s",
                group_id,
                source,
            )
        finally:
            if self._status_message_ids.get(group_id) == message_id:
                self._status_message_ids.pop(group_id, None)
            self._status_pin_intents.pop(group_id, None)
            if pin_message:
                try:
                    async with self._pin_operation_lock(group_id):
                        if message_id not in self._owned_pin_message_ids.get(
                            group_id,
                            set(),
                        ):
                            await self._update_persisted_pin_ownership(
                                group_id,
                                add=(message_id,),
                            )
                except asyncio.CancelledError:
                    # The durable manual/automatic ledger is preferable, but
                    # cancellation must still leave an in-memory cleanup owner.
                    self._owned_pin_message_ids.setdefault(group_id, set()).add(
                        message_id
                    )
                except Exception:
                    self._owned_pin_message_ids.setdefault(group_id, set()).add(
                        message_id
                    )
                    log.exception(
                        "[%s] raid terminal pin ownership recovery failed | message=%s",
                        group_id,
                        message_id,
                    )
            self._schedule_pin_cleanup(group_id, delay_seconds=0.0)
            cleanup_task = self._pin_cleanup_tasks.get(group_id)
            if cleanup_task is not None and cleanup_task is not asyncio.current_task():
                try:
                    await asyncio.shield(cleanup_task)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    log.exception(
                        "[%s] raid immediate pin cleanup failed | source=%s",
                        group_id,
                        source,
                    )

    def _schedule_unlock_notice(self, group_id: int, *, source: str) -> None:
        group_id = int(group_id)
        existing = self._unlock_notice_tasks.get(group_id)
        if existing is not None and not existing.done():
            return
        message_id = self._status_message_ids.get(group_id)
        if message_id is None:
            self._status_pin_intents.pop(group_id, None)
            return
        try:
            task = asyncio.get_running_loop().create_task(
                self._send_unlock_notice(
                    group_id,
                    source=source,
                    expected_message_id=message_id,
                ),
                name=f"raid-unlock-notice:{group_id}",
            )
        except RuntimeError:
            log.debug("[%s] raid unlock notification could not be scheduled", group_id)
            return
        self._unlock_notice_tasks[group_id] = task

        def finished(done: asyncio.Task[None]) -> None:
            if self._unlock_notice_tasks.get(group_id) is done:
                self._unlock_notice_tasks.pop(group_id, None)
            if done.cancelled():
                return
            try:
                done.result()
            except Exception:
                log.exception("[%s] raid unlock notice task failed", group_id)

        task.add_done_callback(finished)

    async def _expire_lockdown(
        self,
        group_id: int,
        expected_until: datetime,
    ) -> bool:
        group_id = int(group_id)
        async with self._lifecycle_lock(group_id):
            # A concurrent activation, reconfiguration, or off action may have
            # replaced the state while this expiry task waited for the lock.
            current = self._lockdown_until.get(group_id)
            if current != expected_until:
                return False
            now = now_shanghai_naive()
            if now < expected_until:
                # Wall-clock adjustments may make call_later fire early; re-arm
                # for the remaining interval without changing the deadline.
                self._cancel_lockdown_timer(group_id)
                try:
                    loop = asyncio.get_running_loop()
                    self._lockdown_timers[group_id] = loop.call_later(
                        max(0.0, (expected_until - now).total_seconds()),
                        self._schedule_lockdown_expiry,
                        group_id,
                        expected_until,
                    )
                except RuntimeError:
                    pass
                return False
            source = self._lockdown_source.get(group_id)
            if source == "manual":
                persisted = self._manual_persisted_state.get(
                    group_id,
                    _manual_lockdown_state(
                        expected_until,
                        pin_message=self._status_pin_intents.get(group_id, False),
                    ),
                )
                try:
                    removed = await self._update_persisted_manual_state(
                        group_id,
                        value=_PERSISTENCE_DELETE,
                        expected=persisted,
                    )
                except asyncio.CancelledError:
                    raise
                except Exception:
                    removed = None
                    log.exception(
                        "[%s] manual raid expiry persistence failed; retrying",
                        group_id,
                    )
                if removed is None:
                    # Keep the expired in-memory marker and retry shortly. A
                    # passive join/status check treats its deadline as expired,
                    # so this retry does not unnecessarily repel new members.
                    try:
                        loop = asyncio.get_running_loop()
                        self._lockdown_timers[group_id] = loop.call_later(
                            5.0,
                            self._schedule_lockdown_expiry,
                            group_id,
                            expected_until,
                        )
                    except RuntimeError:
                        pass
                    return False
                self._clear_lockdown(group_id)
                if not removed:
                    # Another expiry/disable already changed this exact
                    # persisted state and owns the recovery notification.
                    return False
                await self._send_unlock_notice(group_id, source="manual")
                return True

            source = self._clear_lockdown(group_id)
            if source is None:
                return False
            await self._send_unlock_notice(group_id, source=source)
            return True

    def lockdown_active(self, group_id: int, *, now: datetime | None = None) -> bool:
        group_id = int(group_id)
        if group_id not in self._lockdown_until:
            return False
        until = self._lockdown_until[group_id]
        if until is None:
            return True
        current = now if now is not None else now_shanghai_naive()
        if current < until:
            return True
        # State removal and exact status retirement require async I/O and must
        # share the lifecycle lock with activation/off. Treat the deadline as
        # expired immediately, but let the expiry task own the transition.
        self._cancel_lockdown_timer(group_id)
        self._schedule_lockdown_expiry(group_id, until)
        return False

    async def enable_manual_lockdown(
        self,
        group_id: int,
        *,
        duration_minutes: object | None = None,
    ) -> datetime | None:
        """Immediately reject joins until disabled or the minute duration ends."""
        minutes = normalize_manual_lockdown_minutes(duration_minutes)
        group_id = int(group_id)
        until = (
            None
            if minutes is None
            else now_shanghai_naive() + timedelta(minutes=minutes)
        )
        async with self._lifecycle_lock(group_id):
            async with self.session_factory() as session:
                group = await session.get(Group, group_id)
                group_settings = (
                    dict(group.settings or {}) if group is not None else None
                )
            pin_message = notification_pin_enabled(
                self.settings,
                group_settings,
                "raid_guard",
            )
            existing_status_message_id = self._status_message_ids.get(group_id)
            if existing_status_message_id is None:
                existing_status_message_id = _manual_lockdown_status_message_id(
                    self._manual_persisted_state.get(group_id)
                )
            persisted = _manual_lockdown_state(
                until,
                status_message_id=existing_status_message_id,
                pin_message=pin_message,
            )
            saved = await self._update_persisted_manual_state(
                group_id,
                value=persisted,
            )
            if saved is None:
                raise RuntimeError("手动爆破防护状态保存失败，请稍后重试")
            self._arm_lockdown_until(
                group_id,
                until=until,
                source="manual",
                pin_message=pin_message,
                persisted_state=persisted,
            )
            self._recent_joins.pop(group_id, None)
            status_message_id = await self._publish_lockdown_status(
                group_id,
                build_manual_raid_lockdown_text(duration_minutes=minutes),
                pin_message=pin_message,
                defer_pin=True,
            )
            remembered = await self._remember_manual_status_message_locked(
                group_id,
                status_message_id,
            )
            if remembered and pin_message and status_message_id is not None:
                await self._pin_status_message(group_id, status_message_id)
            return until

    async def disable_manual_lockdown(self, group_id: int) -> bool:
        """End the current lockdown immediately and announce the recovery."""
        group_id = int(group_id)
        async with self._lifecycle_lock(group_id):
            source = self._lockdown_source.get(group_id)
            if source is None:
                return False
            if source == "manual":
                expected = self._manual_persisted_state.get(
                    group_id,
                    _manual_lockdown_state(
                        self._lockdown_until.get(group_id),
                        pin_message=self._status_pin_intents.get(group_id, False),
                    ),
                )
                removed = await self._update_persisted_manual_state(
                    group_id,
                    value=_PERSISTENCE_DELETE,
                    expected=expected,
                )
                if removed is None:
                    raise RuntimeError("手动爆破防护状态保存失败，请稍后重试")
                self._clear_lockdown(group_id)
                if not removed:
                    # A competing expiry/disable already won the persisted
                    # transition and is responsible for its unlock notice.
                    return True
            else:
                self._clear_lockdown(group_id)
            await self._send_unlock_notice(group_id, source=source)
            return True

    def manual_lockdown_active(self, group_id: int) -> bool:
        return bool(
            self.lockdown_active(int(group_id))
            and self._lockdown_source.get(int(group_id)) == "manual"
        )

    def lockdown_status_message_matches(self, group_id: int, message_id: int) -> bool:
        """Whether an inline action belongs to the currently active status."""
        try:
            group_id = int(group_id)
            message_id = int(message_id)
        except (TypeError, ValueError):
            return False
        return bool(
            self.lockdown_active(group_id)
            and self._status_message_ids.get(group_id) == message_id
        )

    def lockdown_status(self, group_id: int) -> dict[str, object]:
        """Current in-memory state for commands and Mini App status hints."""
        group_id = int(group_id)
        active = self.lockdown_active(group_id)
        if not active:
            return {"active": False, "source": "", "until": None}
        return {
            "active": True,
            "source": self._lockdown_source.get(group_id, "automatic"),
            "until": self._lockdown_until.get(group_id),
        }

    def reset(self, group_id: int | None = None) -> None:
        if group_id is None:
            for tracked_group in tuple(self._lockdown_timers):
                self._cancel_lockdown_timer(tracked_group)
            for task in tuple(self._lockdown_expiry_tasks.values()):
                task.cancel()
            self._lockdown_expiry_tasks.clear()
            for task in tuple(self._unlock_notice_tasks.values()):
                task.cancel()
            self._unlock_notice_tasks.clear()
            for task in tuple(self._pin_cleanup_tasks.values()):
                task.cancel()
            self._pin_cleanup_tasks.clear()
            self._recent_joins.clear()
            self._lockdown_until.clear()
            self._lockdown_source.clear()
            self._status_message_ids.clear()
            self._status_pin_intents.clear()
            self._owned_pin_message_ids.clear()
            self._manual_persisted_state.clear()
            self._lifecycle_locks.clear()
            for tracked_group, lock in tuple(self._pin_operation_locks.items()):
                if not lock.locked():
                    self._pin_operation_locks.pop(tracked_group, None)
            return
        self._cancel_lockdown_timer(int(group_id))
        task = self._lockdown_expiry_tasks.pop(int(group_id), None)
        if task is not None:
            task.cancel()
        notice_task = self._unlock_notice_tasks.pop(int(group_id), None)
        if notice_task is not None:
            notice_task.cancel()
        cleanup_task = self._pin_cleanup_tasks.pop(int(group_id), None)
        if cleanup_task is not None:
            cleanup_task.cancel()
        self._recent_joins.pop(int(group_id), None)
        self._lockdown_until.pop(int(group_id), None)
        self._lockdown_source.pop(int(group_id), None)
        self._status_message_ids.pop(int(group_id), None)
        self._status_pin_intents.pop(int(group_id), None)
        self._owned_pin_message_ids.pop(int(group_id), None)
        self._manual_persisted_state.pop(int(group_id), None)
        self._lifecycle_locks.pop(int(group_id), None)
        pin_lock = self._pin_operation_locks.get(int(group_id))
        if pin_lock is not None and not pin_lock.locked():
            self._pin_operation_locks.pop(int(group_id), None)

    async def shutdown(self, *, timeout_seconds: float = 2.0) -> None:
        """Cancel service-owned tasks without trusting them to cooperate."""

        tasks = {
            *self._lockdown_expiry_tasks.values(),
            *self._unlock_notice_tasks.values(),
            *self._pin_cleanup_tasks.values(),
        }
        # Automatic lockdowns are intentionally not restored after a process
        # restart.  Retire their exact pinned status messages during a graceful
        # shutdown so a lost in-memory lifecycle does not leave stale pins.
        shutdown_pins: set[tuple[int, int]] = set()
        for group_id, message_ids in self._owned_pin_message_ids.items():
            protected_manual_id = (
                self._status_message_ids.get(group_id)
                if self._lockdown_source.get(group_id) == "manual"
                and group_id in self._lockdown_until
                and self._status_pin_intents.get(group_id, False)
                else None
            )
            shutdown_pins.update(
                (group_id, message_id)
                for message_id in message_ids
                if message_id != protected_manual_id
            )
        # Backward-compatible in-memory state from before the durable ledger
        # may still own an automatic current pin.
        shutdown_pins.update(
            (group_id, message_id)
            for group_id, message_id in self._status_message_ids.items()
            if self._lockdown_source.get(group_id) != "manual"
            and self._status_pin_intents.get(group_id, False)
        )
        self.reset()

        async def drain_service_tasks() -> None:
            if not tasks:
                return
            _done, pending = await asyncio.wait(
                tasks,
                timeout=max(0.0, float(timeout_seconds)),
            )
            if pending:
                log.error(
                    "%d raid-guard service task(s) ignored bounded shutdown",
                    len(pending),
                )

        async def retire_shutdown_pins() -> None:
            if not shutdown_pins:
                return
            await asyncio.gather(
                *(
                    self._unpin_status_message(
                        group_id,
                        message_id,
                        timeout_seconds=timeout_seconds,
                    )
                    for group_id, message_id in shutdown_pins
                ),
                return_exceptions=True,
            )

        # Automatic pins retire in parallel before the global Telegram notice
        # drain starts. The two remaining bounded drains then run in parallel.
        await retire_shutdown_pins()
        await asyncio.gather(
            drain_service_tasks(),
            flush_raid_guard_telegram_tasks(timeout_seconds=timeout_seconds),
        )

    async def handle_join(
        self,
        *,
        group_id: int,
        user_id: int,
        full_name: str = "",
        username: str = "",
        group_settings: dict | None = None,
    ) -> bool:
        """Feed one join into the detector; True means the join was consumed.

        A consumed join was either kicked (active lockdown) or challenged as
        part of a fresh lockdown's retro sweep — the caller must not continue
        with screening or join verification for it. When enforcement for THIS
        member failed (kick failed, challenge could not be issued), the join
        is NOT consumed, so the member still faces the normal screening and
        join-verification pipeline instead of walking in unchecked.
        """
        group_id = int(group_id)
        user_id = int(user_id)
        if not await self._group_authorized(group_id):
            return False
        config = resolve_raid_guard_config(self.settings, group_settings)
        now = now_shanghai_naive()

        manual_until = self._lockdown_until.get(group_id)
        if (
            self._lockdown_source.get(group_id) == "manual"
            and isinstance(manual_until, datetime)
            and manual_until <= now
        ):
            # Complete the persisted compare-and-delete before this join can
            # start a fresh automatic lockdown and overwrite the in-memory
            # source marker needed by the expiry task.
            await self._expire_lockdown(group_id, manual_until)
            if self._lockdown_source.get(group_id) == "manual":
                # Persistence cleanup is retrying. The manual deadline is
                # already over, so do not repel this member, but also do not
                # overwrite the state with a fresh automatic lockdown yet.
                return False

        # A manual lockdown must continue to repel joins even when automatic
        # detection is disabled in this group's persisted policy.
        if self.lockdown_active(group_id, now=now):
            kicked = await self._kick_lockdown_join_durably(
                group_id=group_id,
                user_id=user_id,
                full_name=full_name,
                now=now,
            )
            log.info(
                "raid lockdown join repelled | group=%s user=%s kicked=%s",
                group_id,
                user_id,
                kicked,
            )
            return kicked
        if not config.enabled:
            return False

        suspects = self._observe(
            group_id,
            _JoinEvent(
                user_id=user_id,
                full_name=(full_name or "")[:255],
                username=(username or "").lstrip("@")[:255],
                at=now,
            ),
            config,
            now=now,
        )
        if suspects is None:
            return False

        self._arm_lockdown(
            group_id,
            duration_seconds=config.lockdown_seconds,
            source="automatic",
            pin_message=config.pin_message,
        )

        log.warning(
            "raid detected | group=%s joins=%d window=%ss lockdown=%ss suspects=%d",
            group_id,
            config.join_threshold,
            config.window_seconds,
            config.lockdown_seconds,
            len(suspects),
        )
        enforced = await self._activate_lockdown(group_id, suspects, config)
        # Only the triggering member's own outcome decides consumption: if
        # their challenge was not actually persisted (filtered, mute/message
        # failure, provider unavailable), the normal pipeline must run.
        return any(item.user_id == user_id for item in enforced)

    async def _kick_lockdown_join_durably(
        self,
        *,
        group_id: int,
        user_id: int,
        full_name: str,
        now: datetime,
    ) -> bool:
        """Persist/lease lockdown removal before the Telegram request."""
        if not await self._group_authorized(group_id):
            return False
        lease_until = now + timedelta(seconds=TERMINAL_LEASE_SECONDS)
        verification_id: int | None = None
        async with self.session_factory() as session:
            record = await get_join_verification(session, group_id, user_id)
            if record is None:
                kick_deadline = now - timedelta(minutes=2)
                prepared = await prepare_join_verification(
                    session,
                    group_id=group_id,
                    user_id=user_id,
                    deadline_at=kick_deadline,
                    kind=VERIFICATION_KIND_RAID,
                    reason="爆破防护：封锁期间加入",
                    display_name=full_name,
                    prompt_message_id=0,
                    provider=verification_provider(self.settings),
                    lease_until=lease_until,
                )
                if prepared is not None:
                    activated = await activate_prepared_join_verification(
                        session,
                        prepared=prepared,
                        prompt_message_id=0,
                        deadline_at=kick_deadline,
                        now=now,
                    )
                    won = bool(activated) and await claim_join_verification(
                        session,
                        verification_id=prepared.verification_id,
                        deadline_at=kick_deadline,
                        kind=VERIFICATION_KIND_RAID,
                        now=now,
                        expired=True,
                        lease_until=lease_until,
                        target_status=VERIFICATION_STATUS_ENFORCING,
                    )
                    if not won:
                        await session.rollback()
                        return False
                    await session.commit()
                    verification_id = prepared.verification_id
                    record = None
                else:
                    # The insert-if-absent lost to a concurrent challenge. Read
                    # that committed generation and never overwrite it.
                    record = await get_join_verification(session, group_id, user_id)
            if record is None and verification_id is None:
                return False
            if record is not None and record.kind != VERIFICATION_KIND_RAID:
                # The single durable verification row belongs to another
                # workflow.  Do not report the lockdown kick as consumed: the
                # membership handler must continue into its normal pending
                # challenge path so a rejoin is restricted again.
                return False
            if record is not None:
                status = str(record.status or VERIFICATION_STATUS_PENDING)
                if status == VERIFICATION_STATUS_PENDING:
                    won = await claim_join_verification(
                        session,
                        verification_id=int(record.id),
                        deadline_at=record.deadline_at,
                        kind=record.kind,
                        now=now,
                        expired=verification_deadline_passed(record.deadline_at, now=now),
                        lease_until=lease_until,
                        target_status=VERIFICATION_STATUS_ENFORCING,
                    )
                elif (
                    status == VERIFICATION_STATUS_ENFORCING
                    and record.lease_until is not None
                    and record.lease_until <= now
                ):
                    won = await lease_expired_join_verification(
                        session,
                        record=record,
                        now=now,
                        lease_until=lease_until,
                        target_status=VERIFICATION_STATUS_ENFORCING,
                    )
                else:
                    # Another durable preparation/terminal worker owns this member.
                    return True
                if not won:
                    await session.rollback()
                    return True
                await session.commit()
                verification_id = int(record.id)

        if verification_id is None:
            return False

        member_present = await chat_member_is_present(self.bot, group_id, user_id)
        if member_present is None:
            return False
        if not member_present:
            async with self.session_factory() as session:
                completed = await complete_leased_join_verification(
                    session,
                    verification_id=verification_id,
                    lease_until=lease_until,
                    status=VERIFICATION_STATUS_ENFORCING,
                )
                if completed:
                    await session.commit()
                else:
                    await session.rollback()
            return True

        renewed_lease = now_shanghai_naive() + timedelta(
            seconds=TERMINAL_LEASE_SECONDS
        )
        async with self.session_factory() as session:
            renewed = await renew_join_verification_lease(
                session,
                verification_id=verification_id,
                lease_until=lease_until,
                new_lease_until=renewed_lease,
                status=VERIFICATION_STATUS_ENFORCING,
            )
            if renewed:
                await session.commit()
            else:
                await session.rollback()
                # A manual unban/release is a higher-priority terminal intent.
                # Consume this stale join event instead of falling through into
                # normal verification and restricting the user again.
                return True
        lease_until = renewed_lease

        async def preserve_ban() -> bool:
            async with self.session_factory() as session:
                return await verification_release_blocked_by_ban(
                    session,
                    group_id=group_id,
                    user_id=user_id,
                )

        if not await self._group_authorized(group_id):
            return False
        # Snapshot before the kick: its leave update clears the live join
        # marker concurrently, and the post-kick residue sweep still needs
        # the rejoin window (which includes the join service message).
        residue_marker = member_join_marker(group_id, user_id)
        kicked = await kick_member(
            self.bot,
            group_id,
            user_id,
            preserve_ban=preserve_ban,
        )
        if not kicked:
            return False
        await retract_removed_member_residue(
            self.bot,
            group_id,
            user_id,
            marker=residue_marker,
        )
        async with self.session_factory() as session:
            completed = await complete_leased_join_verification(
                session,
                verification_id=verification_id,
                lease_until=lease_until,
                status=VERIFICATION_STATUS_ENFORCING,
            )
            if completed:
                await session.commit()
            else:
                await session.rollback()
        if not completed:
            await reconcile_stale_verification_restriction(
                self.bot,
                self.session_factory,
                group_id,
                user_id,
            )
        return True

    async def _group_authorized(self, group_id: int) -> bool:
        async with self.session_factory() as session:
            return await is_group_authorized(session, int(group_id))

    def _observe(
        self,
        group_id: int,
        event: _JoinEvent,
        config: RaidGuardConfig,
        *,
        now: datetime,
    ) -> list[RaidSuspect] | None:
        """Record one join; on trigger return the deduplicated suspect list."""
        joins = self._recent_joins.setdefault(group_id, deque())
        joins.append(event)
        horizon = now - timedelta(
            seconds=max(config.lookback_seconds, config.window_seconds)
        )
        while joins and (joins[0].at < horizon or len(joins) > _MAX_TRACKED_JOINS):
            joins.popleft()

        window_start = now - timedelta(seconds=config.window_seconds)
        # Distinct users, not raw events: one account bouncing leave/rejoin
        # must not be able to trigger a lockdown on its own.
        distinct_in_window = {
            item.user_id for item in joins if item.at >= window_start
        }
        if len(distinct_in_window) < config.join_threshold:
            return None

        # Latest event per user: a leave/rejoin bouncer is challenged once.
        latest: dict[int, _JoinEvent] = {}
        for item in joins:
            latest[item.user_id] = item
        suspects = [
            RaidSuspect(
                user_id=item.user_id,
                full_name=item.full_name,
                username=item.username,
                joined_at=item.at,
            )
            for item in sorted(latest.values(), key=lambda entry: entry.at)
        ]
        joins.clear()
        return suspects

    async def _activate_lockdown(
        self,
        group_id: int,
        suspects: list[RaidSuspect],
        config: RaidGuardConfig,
    ) -> list[RaidSuspect]:
        if not await self._group_authorized(group_id):
            return []
        async with self._lifecycle_lock(group_id):
            # The detector arms before doing Telegram I/O. A concurrent button
            # or command may have ended/replaced that lifecycle while the
            # authorization check was in flight; never publish a late orphan.
            if (
                group_id not in self._lockdown_until
                or self._lockdown_source.get(group_id) != "automatic"
            ):
                return []
            await self._publish_lockdown_status(
                group_id,
                build_raid_lockdown_text(
                    joined_count=config.join_threshold,
                    window_seconds=config.window_seconds,
                    lockdown_seconds=config.lockdown_seconds,
                ),
                pin_message=config.pin_message,
            )

        provider = verification_provider(self.settings)
        if not verification_service_ready(self.settings, provider):
            # Muting without a working challenge path would strand members.
            log.warning(
                "[%s] raid retro challenge skipped: verification provider "
                "unavailable",
                group_id,
            )
            return []
        eligible = await self._filter_suspects(group_id, suspects)
        if not eligible:
            return []
        return await self._challenge_suspects(group_id, eligible, config, provider)

    async def _filter_suspects(
        self, group_id: int, suspects: list[RaidSuspect]
    ) -> list[RaidSuspect]:
        admin_ids: set[int] = set()
        if getattr(self.settings, "super_admin_id", 0):
            admin_ids.add(int(self.settings.super_admin_id))
        try:
            admins = await self.bot.get_chat_administrators(group_id)
            for member in admins or []:
                user = getattr(member, "user", None)
                if user is not None:
                    admin_ids.add(int(user.id))
        except Exception:
            log.warning(
                "[%s] raid guard could not list chat administrators",
                group_id,
                exc_info=True,
            )

        eligible: list[RaidSuspect] = []
        async with self.session_factory() as session:
            for suspect in suspects:
                if suspect.user_id in admin_ids:
                    continue
                if await is_globally_banned(session, suspect.user_id):
                    continue
                if (
                    await get_join_verification(session, group_id, suspect.user_id)
                    is not None
                ):
                    # An in-flight join/moderation/patrol challenge already
                    # governs this member; do not clobber its deadline.
                    continue
                eligible.append(suspect)
        return eligible

    async def _challenge_suspects(
        self,
        group_id: int,
        suspects: list[RaidSuspect],
        config: RaidGuardConfig,
        provider: str,
    ) -> list[RaidSuspect]:
        """Prepare durably, then mute, prompt, and activate raid challenges."""
        timeout_seconds = config.challenge_timeout_seconds

        async def abort_one(
            prepared: PreparedVerification,
            *,
            restore_permissions: bool,
        ) -> bool:
            try:
                async with self.session_factory() as session:
                    compensated = await shield_abort_prepared_join_verification(
                        self.bot,
                        session,
                        prepared=prepared,
                        restore_permissions=restore_permissions,
                    )
                if not compensated and restore_permissions:
                    return await reconcile_stale_verification_restriction(
                        self.bot,
                        self.session_factory,
                        group_id,
                        prepared.user_id,
                    )
                return compensated
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception(
                    "[%s] raid preparation compensation failed | user=%s",
                    group_id,
                    prepared.user_id,
                )
                return False

        async def abort_many(
            pairs: list[tuple[RaidSuspect, PreparedVerification]],
            *,
            restore_ids: set[int] | None = None,
        ) -> None:
            semaphore = asyncio.Semaphore(4)

            async def abort_bounded(prepared: PreparedVerification) -> None:
                async with semaphore:
                    await abort_one(
                        prepared,
                        restore_permissions=(
                            restore_ids is None
                            or prepared.verification_id in restore_ids
                        ),
                    )

            await asyncio.gather(
                *(abort_bounded(prepared) for _suspect, prepared in pairs)
            )

        async def activate_chunk(
            pairs: list[tuple[RaidSuspect, PreparedVerification]],
            *,
            prompt_message_id: int,
            deadline: datetime,
        ) -> set[int]:
            activated: set[int] = set()
            async with self.session_factory() as session:
                for _suspect, prepared in pairs:
                    if not await is_group_authorized(session, group_id):
                        break
                    if await activate_prepared_join_verification(
                        session,
                        prepared=prepared,
                        prompt_message_id=prompt_message_id,
                        deadline_at=deadline,
                    ):
                        activated.add(prepared.verification_id)
                await session.commit()
            return activated

        async def renew_chunk(
            pairs: list[tuple[RaidSuspect, PreparedVerification]],
        ) -> list[tuple[RaidSuspect, PreparedVerification]]:
            renewed_pairs: list[tuple[RaidSuspect, PreparedVerification]] = []
            lost_pairs: list[tuple[RaidSuspect, PreparedVerification]] = []
            async with self.session_factory() as session:
                for suspect, prepared in pairs:
                    renewed = await renew_prepared_join_verification(
                        session,
                        prepared=prepared,
                    )
                    if renewed is not None:
                        renewed_pairs.append((suspect, renewed))
                    else:
                        lost_pairs.append((suspect, prepared))
                await session.commit()
            await asyncio.gather(
                *(
                    reconcile_stale_verification_restriction(
                        self.bot,
                        self.session_factory,
                        group_id,
                        suspect.user_id,
                    )
                    for suspect, _prepared in lost_pairs
                )
            )
            return renewed_pairs

        enforced: list[RaidSuspect] = []
        for start in range(0, len(suspects), RAID_MENTIONS_PER_MESSAGE):
            if not await self._group_authorized(group_id):
                return enforced
            candidates = suspects[start : start + RAID_MENTIONS_PER_MESSAGE]
            deadline = now_shanghai_naive() + timedelta(seconds=timeout_seconds)
            prepared_pairs: list[tuple[RaidSuspect, PreparedVerification]] = []
            try:
                async with self.session_factory() as session:
                    for suspect in candidates:
                        if not await is_group_authorized(session, group_id):
                            await session.rollback()
                            prepared_pairs.clear()
                            break
                        prepared = await prepare_join_verification(
                            session,
                            group_id=group_id,
                            user_id=suspect.user_id,
                            deadline_at=deadline,
                            kind=VERIFICATION_KIND_RAID,
                            reason="爆破防护：短时间内批量加入",
                            display_name=suspect.full_name
                            or (f"@{suspect.username}" if suspect.username else ""),
                            provider=provider,
                        )
                        if prepared is not None:
                            prepared_pairs.append((suspect, prepared))
                    await session.commit()
            except Exception:
                log.exception("[%s] raid preparation persistence failed", group_id)
                continue
            if not prepared_pairs:
                continue

            muted_pairs: list[tuple[RaidSuspect, PreparedVerification]] = []
            try:
                semaphore = asyncio.Semaphore(4)

                async def mute_one(
                    suspect: RaidSuspect,
                    prepared: PreparedVerification,
                ) -> tuple[RaidSuspect, PreparedVerification, bool, bool]:
                    async with semaphore:
                        if not await self._group_authorized(group_id):
                            return suspect, prepared, False, False
                        member_present = await chat_member_is_present(
                            self.bot,
                            group_id,
                            suspect.user_id,
                        )
                        if member_present is None:
                            return suspect, prepared, False, True
                        if not member_present:
                            await abort_one(prepared, restore_permissions=False)
                            return suspect, prepared, False, True
                        async with self.session_factory() as lease_session:
                            renewed = await renew_prepared_join_verification(
                                lease_session,
                                prepared=prepared,
                            )
                            if renewed is None:
                                await lease_session.rollback()
                                return suspect, prepared, False, True
                            await lease_session.commit()
                        prepared = renewed
                        try:
                            async with asyncio.timeout(20.0):
                                muted = await restrict_new_member(
                                    self.bot,
                                    group_id,
                                    suspect.user_id,
                                )
                        except asyncio.CancelledError:
                            raise
                        except Exception:
                            muted = False
                            log.exception(
                                "[%s] raid mute failed | user=%s",
                                group_id,
                                suspect.user_id,
                            )
                        if not muted:
                            log.warning(
                                "[%s] raid mute failed; skipping user %s",
                                group_id,
                                suspect.user_id,
                            )
                            await abort_one(prepared, restore_permissions=True)
                            return suspect, prepared, False, True
                        # A raid joiner may have raced messages in before this
                        # mute; none of them passed any check.
                        await delete_messages_since_join(
                            self.bot,
                            group_id,
                            suspect.user_id,
                        )
                        async with self.session_factory() as lease_session:
                            renewed = await renew_prepared_join_verification(
                                lease_session,
                                prepared=prepared,
                            )
                            if renewed is None:
                                await lease_session.rollback()
                                await reconcile_stale_verification_restriction(
                                    self.bot,
                                    self.session_factory,
                                    group_id,
                                    suspect.user_id,
                                )
                                return suspect, prepared, False, True
                            await lease_session.commit()
                        return suspect, renewed, True, True

                mute_results = await asyncio.gather(
                    *(mute_one(suspect, prepared) for suspect, prepared in prepared_pairs)
                )
                muted_pairs = [
                    (suspect, prepared)
                    for suspect, prepared, muted, _authorized in mute_results
                    if muted
                ]
                if any(not authorized for *_prefix, authorized in mute_results):
                    muted_ids = {
                        prepared.verification_id for _suspect, prepared in muted_pairs
                    }
                    await abort_many(prepared_pairs, restore_ids=muted_ids)
                    return enforced
            except asyncio.CancelledError:
                # The mute may have reached Telegram before cancellation was
                # delivered locally. Recover every prepared member instead of
                # deleting an ambiguously applied side effect.
                await abort_many(prepared_pairs)
                raise
            if not muted_pairs:
                continue
            muted_pairs = await renew_chunk(muted_pairs)
            if not muted_pairs:
                continue

            if not await self._group_authorized(group_id):
                await abort_many(muted_pairs)
                return enforced

            chunk = [suspect for suspect, _prepared in muted_pairs]
            prompt_message_id = 0
            try:
                sent = await _bounded_notice_call(
                    self.bot.send_message(
                        group_id,
                        build_raid_challenge_text(
                            chunk,
                            timeout_seconds=timeout_seconds,
                        ),
                        parse_mode="HTML",
                        reply_markup=build_raid_challenge_keyboard(),
                    ),
                    timeout_seconds=10.0,
                )
                prompt_message_id = int(getattr(sent, "message_id", 0) or 0)
            except asyncio.CancelledError:
                await abort_many(muted_pairs)
                raise
            except Exception:
                log.exception("[%s] raid challenge message failed", group_id)
                await abort_many(muted_pairs)
                continue

            activation_task = asyncio.create_task(
                activate_chunk(
                    muted_pairs,
                    prompt_message_id=prompt_message_id,
                    deadline=deadline,
                ),
                name=f"raid-activate:{group_id}:{start}",
            )
            try:
                activated_ids = await asyncio.shield(activation_task)
            except asyncio.CancelledError:
                try:
                    activated_ids = await activation_task
                except asyncio.CancelledError:
                    activated_ids = set()
                except Exception:
                    activated_ids = set()
                    log.exception(
                        "[%s] raid activation failed while cancellation was pending",
                        group_id,
                    )
                await abort_many(
                    [
                        pair
                        for pair in muted_pairs
                        if pair[1].verification_id not in activated_ids
                    ]
                )
                if not activated_ids and prompt_message_id:
                    await delete_verification_prompt(
                        self.bot, group_id, prompt_message_id
                    )
                raise
            except Exception:
                log.exception("[%s] raid challenge activation failed", group_id)
                await abort_many(muted_pairs)
                if prompt_message_id:
                    await delete_verification_prompt(
                        self.bot, group_id, prompt_message_id
                    )
                continue

            activated_pairs = [
                (suspect, prepared)
                for suspect, prepared in muted_pairs
                if prepared.verification_id in activated_ids
            ]
            await abort_many(
                [
                    pair
                    for pair in muted_pairs
                    if pair[1].verification_id not in activated_ids
                ]
            )
            if not activated_pairs:
                if prompt_message_id:
                    await delete_verification_prompt(
                        self.bot, group_id, prompt_message_id
                    )
                continue
            active_chunk = [suspect for suspect, _prepared in activated_pairs]
            enforced.extend(active_chunk)
            log.info(
                "[%s] raid challenge issued | users=%s timeout=%ss",
                group_id,
                ",".join(str(item.user_id) for item in active_chunk),
                timeout_seconds,
            )
        return enforced
