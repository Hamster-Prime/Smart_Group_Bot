"""Raid guard: detect join floods, lock the group, retro-challenge suspects.

Detection is event-driven from the member-join handler: each authorized,
non-bot, non-banned join is recorded into an in-memory sliding window per
group. Reaching the configured threshold within the window triggers a
lockdown:

- For its duration every new join is kicked immediately (ban + unban, so the
  member may rejoin after the lockdown ends). No per-join notices are sent —
  the group only sees the single raid-protection announcement. The lockdown
  runs for a fixed duration: repelled joins do not extend it (a single
  bouncing account must not be able to lock the group forever), and a raid
  that outlasts it simply re-triggers on the next threshold of joins.
  Trigger and recovery notices are persistent. A per-lockdown timer announces
  recovery even when no later member joins to drive another event.
- Members who joined within the lookback window before the trigger (the raid
  wave plus the accounts smuggled in just ahead of it) are fully muted and
  challenged: one message per chunk @-mentions them with a shared "真人质询"
  button (callback data carries no user id — the handler resolves the
  clicker's own pending kind="raid" record, so only mentioned suspects can
  use it). Passing the private Mini App challenge restores permissions;
  missing the deadline kicks WITHOUT banning.

Automatically detected lockdowns are intentionally in-memory only. Manual
lockdowns are persisted in ``Group.settings`` so both indefinite and timed
administrator locks survive a restart; startup restores their timers and
atomically claims expired records before announcing recovery. Issued
challenges live in join_verifications and stay enforced by the deadline
sweeper. When the verification service is unavailable the lockdown still
protects the group, but no challenges are issued (muting members with no
challenge path would strand them).
"""
from __future__ import annotations

import asyncio
import html
import logging
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
from bot.services.join_screening import is_globally_banned
from bot.services.join_verification import (
    RAID_VERIFY_CALLBACK_DATA,
    VERIFICATION_KIND_RAID,
    claim_join_verification,
    get_join_verification,
    kick_member,
    restore_member_permissions,
    restrict_new_member,
    upsert_join_verification,
    verification_deadline_passed,
    verification_provider,
    verification_service_ready,
    verification_timeout_seconds_for_kind,
)
from bot.utils.timezone import (
    now_shanghai_naive,
    to_shanghai_datetime,
    to_shanghai_naive,
)

log = logging.getLogger(__name__)

# Suspects mentioned per challenge message: keeps each message far below the
# 4096-char cap and within Telegram's per-message mention-notification limits.
RAID_MENTIONS_PER_MESSAGE = 15
# Gentle pacing between per-member Telegram calls (restrict).
_PER_MEMBER_CALL_PAUSE = 0.05
# Upper bound on remembered joins per group; a raid larger than this still
# triggers long before the cap is reached.
_MAX_TRACKED_JOINS = 4096
# Shared raid challenge callback used by administrators to remove every still
# pending suspect mentioned by the clicked challenge message.
RAID_REMOVE_CALLBACK_DATA = "rgr"
# A command typo must not leave a group locked for an effectively unbounded
# number of years. Indefinite manual lockdown is represented by no duration;
# explicit minute values are capped at one week.
MAX_MANUAL_LOCKDOWN_MINUTES = 7 * 24 * 60
# Private service state. It is deliberately excluded from the Mini App's
# editable group-settings schema, while sharing the same JSON document with
# the group's other settings.
MANUAL_LOCKDOWN_SETTINGS_KEY = "raid_guard_manual_lockdown"
_MANUAL_LOCKDOWN_STATE_VERSION = 1
_PERSISTENCE_RETRIES = 5
_PERSISTENCE_ANY = object()
_PERSISTENCE_DELETE = object()
_INVALID_PERSISTED_STATE = object()

_service: "RaidGuardService | None" = None


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
        return f"@{suspect.username}"
    label = html.escape((suspect.full_name or "").strip() or str(suspect.user_id))
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
    return (
        "🚨 <b>爆破防护已触发</b>\n"
        f"检测到 {_format_duration(window_seconds)}内有 {joined_count} 名成员加入，"
        "疑似遭遇批量爆破。\n"
        f"群组已临时锁定 {_format_duration(lockdown_seconds)}：期间任何新加入的成员"
        "都会被自动移出（不封禁，解除后可重新加入）。"
    )


def build_manual_raid_lockdown_text(*, duration_minutes: int | None) -> str:
    if duration_minutes is None:
        duration_text = "关闭前将持续拒绝新成员加入"
    else:
        duration_text = (
            f"将在 {_format_duration(duration_minutes * 60)} 内拒绝新成员加入"
        )
    return (
        "🛡 <b>爆破防护已手动开启</b>\n"
        f"{duration_text}；被拒绝的成员不会封禁，解除后可以重新加入。"
    )


def build_raid_unlock_text() -> str:
    return (
        "✅ <b>爆破防护已解除</b>\n"
        "群组已恢复接收新成员；此前被临时移出的用户现在可以重新加入。"
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


def _manual_lockdown_state(until: datetime | None) -> dict[str, object]:
    """Return the canonical JSON document stored for one manual lockdown."""
    if until is None:
        return {
            "version": _MANUAL_LOCKDOWN_STATE_VERSION,
            "indefinite": True,
        }
    return {
        "version": _MANUAL_LOCKDOWN_STATE_VERSION,
        # Include the UTC offset so the deadline remains unambiguous if the
        # host timezone changes. Runtime comparisons still use the project's
        # Asia/Shanghai-naive convention.
        "until": to_shanghai_datetime(until).isoformat(),
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
    lines = [
        "🚨 <b>爆破防护真人质询</b>",
        "以下近期加入的成员需要完成真人验证，已暂时禁言：",
    ]
    for suspect in suspects:
        lines.append(f"• {_mention(suspect)}")
    lines.append("")
    lines.append(
        f"请相关成员在 {_format_duration(timeout_seconds)} 内点击下方「真人质询」按钮，"
        "与我私聊完成人机验证；"
    )
    lines.append("通过后自动恢复发言权限，超时将被移出群聊（不会封禁，可重新加入）。")
    return "\n".join(lines)


def build_raid_challenge_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="🙋 真人质询（仅被点名成员可用）",
                    callback_data=RAID_VERIFY_CALLBACK_DATA,
                )
            ],
            [
                InlineKeyboardButton(
                    text="🚫 一键移除被追溯用户（仅管理员）",
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
) -> RaidRemovalResult:
    """Atomically claim and kick pending raid suspects from one prompt.

    The prompt message id scopes the bulk action to exactly the displayed
    chunk. Each record is claimed with the same compare-and-delete primitive
    used by Mini App passes and the timeout sweeper, preventing double actions.
    Failed Telegram kicks are requeued with a fresh raid deadline so an
    administrator can retry instead of silently losing enforcement state.
    """
    group_id = int(group_id)
    prompt_message_id = int(prompt_message_id)
    now = now_shanghai_naive()
    result = await session.execute(
        select(JoinVerification)
        .where(
            JoinVerification.group_id == group_id,
            JoinVerification.kind == VERIFICATION_KIND_RAID,
            JoinVerification.prompt_message_id == prompt_message_id,
        )
        .order_by(JoinVerification.id)
    )
    records = list(result.scalars().all())
    claimed: list[tuple[dict[str, object], bool]] = []
    for record in records:
        globally_banned = await is_globally_banned(session, int(record.user_id))
        won = await claim_join_verification(
            session,
            verification_id=int(record.id),
            deadline_at=record.deadline_at,
            kind=record.kind,
            now=now,
            expired=verification_deadline_passed(record.deadline_at, now=now),
        )
        if not won:
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
                },
                globally_banned,
            )
        )
    await session.commit()

    removed: list[int] = []
    failed: list[int] = []
    retry_deadline = now_shanghai_naive() + timedelta(
        seconds=verification_timeout_seconds_for_kind(
            settings,
            VERIFICATION_KIND_RAID,
            group_settings,
        )
    )
    for snapshot, globally_banned in claimed:
        user_id = int(snapshot["user_id"])
        # A global ban already keeps this account out. kick_member would ban
        # and immediately unban it, accidentally lifting Telegram enforcement.
        if globally_banned or await kick_member(bot, group_id, user_id):
            removed.append(user_id)
            continue
        failed.append(user_id)
        await upsert_join_verification(
            session,
            group_id=group_id,
            user_id=user_id,
            deadline_at=retry_deadline,
            kind=VERIFICATION_KIND_RAID,
            provider=str(snapshot["provider"]),
            reason=str(snapshot["reason"]),
            display_name=str(snapshot["display_name"]),
            prompt_message_id=int(snapshot["prompt_message_id"]),
        )
    if failed:
        await session.commit()
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
        # Exact raw JSON values are retained so expiry/disable can use an
        # optimistic compare-and-delete. Only the process that removes the
        # matching record emits the recovery notice.
        self._manual_persisted_state: dict[int, dict[str, object]] = {}
        self._manual_state_locks: dict[int, asyncio.Lock] = {}

    def _manual_state_lock(self, group_id: int) -> asyncio.Lock:
        group_id = int(group_id)
        lock = self._manual_state_locks.get(group_id)
        if lock is None:
            lock = asyncio.Lock()
            self._manual_state_locks[group_id] = lock
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
        )
        return until

    def _arm_lockdown_until(
        self,
        group_id: int,
        *,
        until: datetime | None,
        source: str,
        persisted_state: Mapping[str, object] | None = None,
        schedule_expiry: bool = True,
    ) -> None:
        """Install an exact deadline, used both at activation and restore."""
        group_id = int(group_id)
        self._cancel_lockdown_timer(group_id)
        self._lockdown_until[group_id] = until
        self._lockdown_source[group_id] = str(source or "automatic")
        if source == "manual" and persisted_state is not None:
            self._manual_persisted_state[group_id] = dict(persisted_state)
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
            if MANUAL_LOCKDOWN_SETTINGS_KEY not in group_settings:
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
                continue

            if isinstance(parsed_until, datetime) and parsed_until <= now:
                canonical = (
                    dict(raw_state) if isinstance(raw_state, Mapping) else {}
                )
                self._arm_lockdown_until(
                    group_id,
                    until=parsed_until,
                    source="manual",
                    persisted_state=canonical,
                    schedule_expiry=False,
                )
                if await self._expire_lockdown(group_id, parsed_until):
                    expired += 1
                continue

            canonical = dict(raw_state) if isinstance(raw_state, Mapping) else {}
            self._arm_lockdown_until(
                group_id,
                until=parsed_until if isinstance(parsed_until, datetime) else None,
                source="manual",
                persisted_state=canonical,
            )
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

    async def _send_unlock_notice(self, group_id: int, *, source: str) -> None:
        try:
            # Protection state notices are intentionally persistent and do
            # not participate in any general moderation auto-delete policy.
            await self.bot.send_message(
                int(group_id),
                build_raid_unlock_text(),
                parse_mode="HTML",
            )
        except Exception:
            log.exception(
                "[%s] raid unlock notice failed | source=%s", group_id, source
            )

    async def _expire_lockdown(
        self,
        group_id: int,
        expected_until: datetime,
    ) -> bool:
        group_id = int(group_id)
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
            async with self._manual_state_lock(group_id):
                # A concurrent /raidguard on/off may have replaced the state
                # while this expiry task waited for the per-group lock.
                if (
                    self._lockdown_until.get(group_id) != expected_until
                    or self._lockdown_source.get(group_id) != "manual"
                ):
                    return False
                persisted = self._manual_persisted_state.get(
                    group_id,
                    _manual_lockdown_state(expected_until),
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
        if self._lockdown_source.get(group_id) == "manual":
            # Persistence cleanup and the exactly-once recovery notice require
            # async I/O. Treat the deadline as expired immediately, but let the
            # compare-and-delete task own state removal and notification.
            self._cancel_lockdown_timer(group_id)
            self._schedule_lockdown_expiry(group_id, until)
            return False
        source = self._clear_lockdown(group_id)
        if source is not None:
            try:
                asyncio.create_task(
                    self._send_unlock_notice(group_id, source=source),
                    name=f"raid-unlock:{group_id}",
                )
            except RuntimeError:
                log.debug(
                    "[%s] raid unlock notification could not be scheduled",
                    group_id,
                )
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
        persisted = _manual_lockdown_state(until)
        async with self._manual_state_lock(group_id):
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
                persisted_state=persisted,
            )
            self._recent_joins.pop(group_id, None)
        try:
            # Manual state notices, like automatic trigger/unlock notices, are
            # persistent by design.
            await self.bot.send_message(
                group_id,
                build_manual_raid_lockdown_text(duration_minutes=minutes),
                parse_mode="HTML",
            )
        except Exception:
            log.exception("[%s] manual raid lockdown notice failed", group_id)
        return until

    async def disable_manual_lockdown(self, group_id: int) -> bool:
        """End the current lockdown immediately and announce the recovery."""
        group_id = int(group_id)
        async with self._manual_state_lock(group_id):
            source = self._lockdown_source.get(group_id)
            if source is None:
                return False
            if source == "manual":
                expected = self._manual_persisted_state.get(
                    group_id,
                    _manual_lockdown_state(self._lockdown_until.get(group_id)),
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
            self._recent_joins.clear()
            self._lockdown_until.clear()
            self._lockdown_source.clear()
            self._manual_persisted_state.clear()
            self._manual_state_locks.clear()
            return
        self._cancel_lockdown_timer(int(group_id))
        task = self._lockdown_expiry_tasks.pop(int(group_id), None)
        if task is not None:
            task.cancel()
        self._recent_joins.pop(int(group_id), None)
        self._lockdown_until.pop(int(group_id), None)
        self._lockdown_source.pop(int(group_id), None)
        self._manual_persisted_state.pop(int(group_id), None)
        self._manual_state_locks.pop(int(group_id), None)

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
            kicked = await kick_member(self.bot, group_id, user_id)
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
        try:
            await self.bot.send_message(
                group_id,
                build_raid_lockdown_text(
                    joined_count=config.join_threshold,
                    window_seconds=config.window_seconds,
                    lockdown_seconds=config.lockdown_seconds,
                ),
                parse_mode="HTML",
            )
        except Exception:
            # The lockdown itself still protects the group.
            log.exception("[%s] raid lockdown notice failed", group_id)

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
        """Mute suspects, post chunked challenges, persist raid records."""
        timeout_seconds = config.challenge_timeout_seconds
        enforced: list[RaidSuspect] = []
        for start in range(0, len(suspects), RAID_MENTIONS_PER_MESSAGE):
            # Mute, warn, and persist one chunk at a time: a crash mid-run
            # then strands at most one chunk muted without durable records.
            chunk: list[RaidSuspect] = []
            for suspect in suspects[start : start + RAID_MENTIONS_PER_MESSAGE]:
                if await restrict_new_member(self.bot, group_id, suspect.user_id):
                    chunk.append(suspect)
                else:
                    log.warning(
                        "[%s] raid mute failed; skipping user %s",
                        group_id,
                        suspect.user_id,
                    )
                await asyncio.sleep(_PER_MEMBER_CALL_PAUSE)
            if not chunk:
                continue
            prompt_message_id = 0
            try:
                sent = await self.bot.send_message(
                    group_id,
                    build_raid_challenge_text(chunk, timeout_seconds=timeout_seconds),
                    parse_mode="HTML",
                    reply_markup=build_raid_challenge_keyboard(),
                )
                prompt_message_id = int(getattr(sent, "message_id", 0) or 0)
            except Exception:
                log.exception("[%s] raid challenge message failed", group_id)
                await self._restore_unowned(group_id, chunk)
                continue

            deadline = now_shanghai_naive() + timedelta(seconds=timeout_seconds)
            persisted: list[RaidSuspect] = []
            try:
                async with self.session_factory() as session:
                    for suspect in chunk:
                        # A challenge issued since the filter pass governs
                        # this member; overwriting a moderation record would
                        # downgrade its ban-on-timeout to a kick.
                        existing = await get_join_verification(
                            session, group_id, suspect.user_id
                        )
                        if (
                            existing is not None
                            and existing.kind != VERIFICATION_KIND_RAID
                        ):
                            continue
                        await upsert_join_verification(
                            session,
                            group_id=group_id,
                            user_id=suspect.user_id,
                            deadline_at=deadline,
                            kind=VERIFICATION_KIND_RAID,
                            reason="爆破防护：短时间内批量加入",
                            display_name=suspect.full_name
                            or (f"@{suspect.username}" if suspect.username else ""),
                            prompt_message_id=prompt_message_id,
                            provider=provider,
                        )
                        persisted.append(suspect)
                    # The mute and challenge are already visible; the durable
                    # records must land before this pass ends or the sweeper
                    # could never lift/kick these members.
                    await session.commit()
            except Exception:
                log.exception("[%s] raid challenge persistence failed", group_id)
                await self._restore_unowned(group_id, chunk)
                continue
            enforced.extend(persisted)
            if persisted:
                log.info(
                    "[%s] raid challenge issued | users=%s timeout=%ss",
                    group_id,
                    ",".join(str(item.user_id) for item in persisted),
                    timeout_seconds,
                )
        return enforced

    async def _restore_unowned(
        self, group_id: int, chunk: list[RaidSuspect]
    ) -> None:
        """Compensation unmute for a failed chunk, but only for members not
        governed by another active challenge (whose mute must survive)."""
        for suspect in chunk:
            try:
                async with self.session_factory() as session:
                    existing = await get_join_verification(
                        session, group_id, suspect.user_id
                    )
                if existing is not None and existing.kind != VERIFICATION_KIND_RAID:
                    continue
            except Exception:
                log.debug(
                    "[%s] raid compensation record check failed | user=%s",
                    group_id,
                    suspect.user_id,
                    exc_info=True,
                )
            await restore_member_permissions(self.bot, group_id, suspect.user_id)
