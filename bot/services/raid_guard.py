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
- Members who joined within the lookback window before the trigger (the raid
  wave plus the accounts smuggled in just ahead of it) are fully muted and
  challenged: one message per chunk @-mentions them with a shared "真人质询"
  button (callback data carries no user id — the handler resolves the
  clicker's own pending kind="raid" record, so only mentioned suspects can
  use it). Passing the private Mini App challenge restores permissions;
  missing the deadline kicks WITHOUT banning.

Lockdown state is in-memory only: a restart drops an active lockdown, but the
issued challenges live in join_verifications and stay enforced by the
deadline sweeper. When the verification service is unavailable the lockdown
still protects the group, but no challenges are issued (muting members with
no challenge path would strand them).
"""
from __future__ import annotations

import asyncio
import html
import logging
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Iterable

from aiogram import Bot
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from bot.config import Settings
from bot.services.join_screening import is_globally_banned
from bot.services.join_verification import (
    RAID_VERIFY_CALLBACK_DATA,
    VERIFICATION_KIND_RAID,
    get_join_verification,
    kick_member,
    restore_member_permissions,
    restrict_new_member,
    upsert_join_verification,
    verification_provider,
    verification_service_ready,
)
from bot.utils.timezone import now_shanghai_naive

log = logging.getLogger(__name__)

# Suspects mentioned per challenge message: keeps each message far below the
# 4096-char cap and within Telegram's per-message mention-notification limits.
RAID_MENTIONS_PER_MESSAGE = 15
# Gentle pacing between per-member Telegram calls (restrict).
_PER_MEMBER_CALL_PAUSE = 0.05
# Upper bound on remembered joins per group; a raid larger than this still
# triggers long before the cap is reached.
_MAX_TRACKED_JOINS = 4096

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
            ]
        ]
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

    No background loop: lockdown expiry is passive (checked on the next
    join), and challenge deadlines are enforced by the shared verification
    sweeper.
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
        self._lockdown_until: dict[int, datetime] = {}

    def lockdown_active(self, group_id: int, *, now: datetime | None = None) -> bool:
        until = self._lockdown_until.get(int(group_id))
        if until is None:
            return False
        current = now if now is not None else now_shanghai_naive()
        if current < until:
            return True
        self._lockdown_until.pop(int(group_id), None)
        return False

    def reset(self, group_id: int | None = None) -> None:
        if group_id is None:
            self._recent_joins.clear()
            self._lockdown_until.clear()
            return
        self._recent_joins.pop(int(group_id), None)
        self._lockdown_until.pop(int(group_id), None)

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
        if not config.enabled:
            return False
        now = now_shanghai_naive()

        if self.lockdown_active(group_id, now=now):
            kicked = await kick_member(self.bot, group_id, user_id)
            log.info(
                "raid lockdown join repelled | group=%s user=%s kicked=%s",
                group_id,
                user_id,
                kicked,
            )
            return kicked

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

        self._lockdown_until[group_id] = now + timedelta(
            seconds=config.lockdown_seconds
        )
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
