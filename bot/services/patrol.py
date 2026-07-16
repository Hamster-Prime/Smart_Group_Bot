"""Scheduled profile patrol: re-screen every known member's name/bio.

The Bot API cannot enumerate chat members, so the patrol walks the
group_members roster maintained from joins/leaves and message traffic
(bot.middlewares.member_roster), seeded once from retained dialogue history.

Each run scans the roster in batches (patrol.batch_size, default 500) to
bound memory and API pressure. A member is screened with the same
moderation-rules LLM call as join screening; results are cached per user in
UserProfileScreen.patrol_hash keyed on the profile plus the enabled-rules
fingerprint, so unchanged profiles cost nothing on later runs.

Violators are fully muted and one warning message per chunk @-mentions them
with a shared "真人质询" button (callback data has no embedded user id — the
handler resolves the clicker's own pending patrol record, so only mentioned
violators can use it). Passing the private Mini App challenge restores
permissions; missing the deadline kicks WITHOUT banning, so the member may
rejoin.
"""
from __future__ import annotations

import asyncio
import html
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Iterable

from aiogram import Bot
from aiogram.exceptions import TelegramRetryAfter
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from sqlalchemy import func, select, update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from bot.config import Settings
from bot.db.models import Group, GroupMember, MessageVector, ModerationRule, PatrolRun
from bot.services.authz import is_group_authorized, list_authorized_groups
from bot.services.join_screening import (
    build_join_profile_text,
    is_globally_banned,
    mark_profile_screened,
    moderation_rules_fingerprint,
    profile_screen_signature,
    screen_member_profile_verbose,
)
from bot.services.join_verification import (
    PATROL_VERIFY_CALLBACK_DATA,
    VERIFICATION_KIND_PATROL,
    get_join_verification,
    restore_member_permissions,
    restrict_new_member,
    upsert_join_verification,
    verification_provider,
    verification_service_ready,
    verification_timeout_seconds_for_kind,
)
from bot.services.llm import LLMService
from bot.services.moderation import ModerationService
from bot.utils.timezone import now_shanghai_naive

log = logging.getLogger(__name__)

# Violators mentioned per warning message: keeps each message far below the
# 4096-char cap and within Telegram's per-message mention-notification limits.
PATROL_MENTIONS_PER_MESSAGE = 15
# Gentle pacing between per-member Telegram calls (getChat / restrict).
_PER_MEMBER_CALL_PAUSE = 0.05

_ROSTER_CACHE_MAX = 65536
# (group_id, user_id) -> (full_name, username) already persisted this process.
_roster_cache: dict[tuple[int, int], tuple[str, str]] = {}

_service: "PatrolService | None" = None


def init_patrol_service(service: "PatrolService | None") -> None:
    global _service
    _service = service


def get_patrol_service() -> "PatrolService | None":
    return _service


def reset_roster_cache() -> None:
    _roster_cache.clear()


# ---------------------------------------------------------------------------
# Roster maintenance
# ---------------------------------------------------------------------------


async def track_group_member(
    session: AsyncSession,
    group_id: int,
    *,
    user_id: int,
    full_name: str = "",
    username: str = "",
    is_bot: bool = False,
) -> None:
    """Upsert one roster row; always clears the left flag."""
    values = {
        "full_name": (full_name or "")[:255],
        "username": (username or "").lstrip("@")[:255],
        "is_bot": bool(is_bot),
        "left": False,
        "last_seen_at": now_shanghai_naive(),
    }
    dialect = getattr(getattr(session, "bind", None), "dialect", None)
    if getattr(dialect, "name", "") == "sqlite":
        stmt = sqlite_insert(GroupMember).values(
            group_id=group_id,
            user_id=user_id,
            **values,
        )
        stmt = stmt.on_conflict_do_update(
            index_elements=[GroupMember.group_id, GroupMember.user_id],
            set_=values,
        )
        await session.execute(stmt)
        return

    row = await session.scalar(
        select(GroupMember).where(
            GroupMember.group_id == group_id,
            GroupMember.user_id == user_id,
        )
    )
    if row is None:
        try:
            async with session.begin_nested():
                session.add(GroupMember(group_id=group_id, user_id=user_id, **values))
                await session.flush()
            return
        except IntegrityError:
            row = await session.scalar(
                select(GroupMember).where(
                    GroupMember.group_id == group_id,
                    GroupMember.user_id == user_id,
                )
            )
            if row is None:
                raise
    for key, value in values.items():
        setattr(row, key, value)


async def track_group_member_cached(
    session_factory: async_sessionmaker[AsyncSession],
    group_id: int,
    user: Any,
) -> None:
    """Message-path roster upsert; hits the DB only when the profile changed."""
    user_id = int(getattr(user, "id", 0) or 0)
    if user_id <= 0:
        return
    full_name = str(getattr(user, "full_name", "") or "").strip()
    username = str(getattr(user, "username", "") or "").strip().lstrip("@")
    key = (int(group_id), user_id)
    if _roster_cache.get(key) == (full_name, username):
        return
    async with session_factory() as session:
        if not await is_group_authorized(session, int(group_id)):
            return
        await track_group_member(
            session,
            int(group_id),
            user_id=user_id,
            full_name=full_name,
            username=username,
            is_bot=bool(getattr(user, "is_bot", False)),
        )
        await session.commit()
    if len(_roster_cache) >= _ROSTER_CACHE_MAX:
        _roster_cache.pop(next(iter(_roster_cache)), None)
    _roster_cache[key] = (full_name, username)


async def mark_group_member_left(
    session: AsyncSession, group_id: int, user_id: int
) -> None:
    await session.execute(
        update(GroupMember)
        .where(
            GroupMember.group_id == group_id,
            GroupMember.user_id == user_id,
        )
        .values(left=True)
    )
    _roster_cache.pop((int(group_id), int(user_id)), None)


async def get_patrol_screen_hash(
    session: AsyncSession, group_id: int, user_id: int
) -> str:
    """Per-(group,user) patrol pass cache; keyed per group so patrols in one
    group cannot thrash the cache of another (rules fingerprints differ)."""
    with session.no_autoflush:
        value = await session.scalar(
            select(GroupMember.patrol_hash).where(
                GroupMember.group_id == group_id,
                GroupMember.user_id == user_id,
            )
        )
    return str(value or "")


async def mark_patrol_screened(
    session: AsyncSession,
    group_id: int,
    user_id: int,
    *,
    patrol_hash: str,
) -> None:
    await session.execute(
        update(GroupMember)
        .where(
            GroupMember.group_id == group_id,
            GroupMember.user_id == user_id,
        )
        .values(patrol_hash=patrol_hash)
    )


async def seed_roster_from_history(session: AsyncSession, group_id: int) -> int:
    """One-time backfill from retained dialogue history for existing groups."""
    stmt = (
        select(
            MessageVector.sender_id,
            func.max(MessageVector.sender_name),
        )
        .where(
            MessageVector.group_id == group_id,
            MessageVector.role == "user",
            MessageVector.sender_id.isnot(None),
            MessageVector.sender_id > 0,
        )
        .group_by(MessageVector.sender_id)
    )
    known_stmt = select(GroupMember.user_id).where(GroupMember.group_id == group_id)
    with session.no_autoflush:
        rows = (await session.execute(stmt)).all()
        known = {int(value) for value in (await session.scalars(known_stmt)).all()}
    seeded = 0
    for sender_id, sender_name in rows:
        if int(sender_id) in known:
            continue
        await track_group_member(
            session,
            group_id,
            user_id=int(sender_id),
            full_name=str(sender_name or ""),
        )
        seeded += 1
    return seeded


async def acknowledge_patrol_pass(
    bot: Bot,
    session_factory: async_sessionmaker[AsyncSession],
    *,
    group_id: int,
    user_id: int,
    fetch_bio: bool = True,
) -> bool:
    """Whitelist the member's current profile after a passed patrol challenge.

    Without this, the same unchanged profile would be re-flagged by the next
    patrol run (a daily mute loop) and instantly global-banned by the
    on-message profile middleware — defeating "pass restores permissions".
    Both caches are keyed on the profile plus the enabled-rules fingerprint,
    so a later rename, bio edit, or rule change re-screens as usual.
    """
    full_name = ""
    username = ""
    try:
        member = await bot.get_chat_member(group_id, user_id)
        user = getattr(member, "user", None)
        if user is not None:
            full_name = str(getattr(user, "full_name", "") or "")
            username = str(getattr(user, "username", "") or "")
    except Exception:
        # Fall back to the roster snapshot: without at least the bio-less
        # whitelist, the member's next message would be re-screened by the
        # profile middleware and could global-ban them right after passing.
        log.warning(
            "patrol pass acknowledgement profile fetch failed; using roster | "
            "group=%s user=%s",
            group_id,
            user_id,
        )
        async with session_factory() as session:
            row = await session.scalar(
                select(GroupMember).where(
                    GroupMember.group_id == group_id,
                    GroupMember.user_id == user_id,
                )
            )
        if row is None:
            return False
        full_name = str(row.full_name or "")
        username = str(row.username or "")
    # Mirror the scan's bio semantics: with patrol_fetch_bio off the scan
    # signature is bio-less, and a bio-inclusive whitelist hash would never
    # match it — the passing member would be re-flagged by every later run.
    bio = ""
    if fetch_bio:
        try:
            chat = await bot.get_chat(user_id)
            bio = str(getattr(chat, "bio", "") or "")
        except Exception:
            pass

    async with session_factory() as session:
        rules_fp = await moderation_rules_fingerprint(session, group_id)
        await mark_patrol_screened(
            session,
            group_id,
            user_id,
            patrol_hash=profile_screen_signature(
                full_name=full_name,
                username=username,
                bio=bio,
                rules_fingerprint=rules_fp,
            ),
        )
        await mark_profile_screened(
            session,
            user_id,
            profile_hash=profile_screen_signature(
                full_name=full_name,
                username=username,
                rules_fingerprint=rules_fp,
            ),
        )
        await session.commit()
    log.info(
        "patrol pass acknowledged | group=%s user=%s", group_id, user_id
    )
    return True


# ---------------------------------------------------------------------------
# Policy / scheduling helpers
# ---------------------------------------------------------------------------


def patrol_policy(settings: Settings, group_settings: dict | None = None) -> bool:
    """Per-group patrol_enabled override; None inherits the global default."""
    enabled = bool(getattr(settings, "patrol_enabled", False))
    if isinstance(group_settings, dict) and "patrol_enabled" in group_settings:
        raw = group_settings.get("patrol_enabled")
        if isinstance(raw, str):
            normalized = raw.strip().lower()
            if normalized in {"1", "true", "yes", "on", "enable", "enabled"}:
                enabled = True
            elif normalized in {"0", "false", "no", "off", "disable", "disabled"}:
                enabled = False
        elif raw is not None:
            enabled = bool(raw)
    return enabled


def parse_schedule_time(raw: str) -> tuple[int, int] | None:
    text = str(raw or "").strip()
    parts = text.split(":")
    if len(parts) != 2:
        return None
    try:
        hour, minute = int(parts[0]), int(parts[1])
    except ValueError:
        return None
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return None
    return hour, minute


def patrol_due(
    *,
    schedule_time: str,
    last_run_at: datetime | None,
    now: datetime | None = None,
) -> bool:
    """True once per day after the configured HH:MM (Asia/Shanghai).

    Comparing last_run_at against today's scheduled instant (instead of a
    sleep-until timer) survives restarts without double-running.
    """
    parsed = parse_schedule_time(schedule_time)
    if parsed is None:
        return False
    current = now if now is not None else now_shanghai_naive()
    due_at = current.replace(hour=parsed[0], minute=parsed[1], second=0, microsecond=0)
    if current < due_at:
        return False
    return last_run_at is None or last_run_at < due_at


# ---------------------------------------------------------------------------
# Warning message
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class PatrolViolator:
    user_id: int
    full_name: str
    username: str
    reason: str


def _mention(violator: PatrolViolator) -> str:
    if violator.username:
        return f"@{violator.username}"
    label = html.escape((violator.full_name or "").strip() or str(violator.user_id))
    return f'<a href="tg://user?id={violator.user_id}">{label}</a>'


def _format_timeout(timeout_seconds: int) -> str:
    seconds = max(1, int(timeout_seconds))
    if seconds % 60 == 0:
        return f"{seconds // 60} 分钟"
    return f"{seconds} 秒"


def build_patrol_warning_text(
    violators: Iterable[PatrolViolator],
    *,
    timeout_seconds: int,
) -> str:
    lines = [
        "🚨 <b>资料巡检警告</b>",
        "以下成员的名字或简介涉嫌违反群规，已暂时禁言：",
    ]
    for violator in violators:
        reason = html.escape((violator.reason or "资料命中群规").strip()[:80])
        lines.append(f"• {_mention(violator)} — {reason}")
    lines.append("")
    lines.append(
        f"请相关成员在 {_format_timeout(timeout_seconds)} 内点击下方「真人质询」按钮，"
        "与我私聊完成人机验证；"
    )
    lines.append("通过后自动恢复发言权限，超时将被移出群聊（不会封禁，可重新加入）。")
    return "\n".join(lines)


def build_patrol_challenge_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="🙋 真人质询（仅被点名成员可用）",
                    callback_data=PATROL_VERIFY_CALLBACK_DATA,
                )
            ]
        ]
    )


# ---------------------------------------------------------------------------
# The patrol service
# ---------------------------------------------------------------------------


async def _tg_call(coro_factory, *, retries: int = 2):
    """Run one Telegram call, honoring flood-control waits."""
    for attempt in range(retries + 1):
        try:
            return await coro_factory()
        except TelegramRetryAfter as exc:
            if attempt >= retries:
                raise
            await asyncio.sleep(max(0.5, float(getattr(exc, "retry_after", 1.0))) + 0.2)
    return None


class PatrolService:
    """Background loop running the daily profile patrol per authorized group."""

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
        self._group_locks: dict[int, asyncio.Lock] = {}
        self._manual_tasks: set[asyncio.Task] = set()

    def is_running(self, group_id: int) -> bool:
        lock = self._group_locks.get(int(group_id))
        return bool(lock is not None and lock.locked())

    async def shutdown(self) -> None:
        """Cancel in-flight manual runs so they release their group locks."""
        tasks = list(self._manual_tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def run_forever(self) -> None:
        log.info("profile patrol service started")
        await self._reset_stale_running_flags()
        while True:
            try:
                await self.run_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("profile patrol pass failed")
            await asyncio.sleep(
                max(15.0, float(getattr(self.settings, "patrol_check_interval_seconds", 60.0)))
            )

    async def _reset_stale_running_flags(self) -> None:
        try:
            async with self.session_factory() as session:
                await session.execute(
                    update(PatrolRun)
                    .where(PatrolRun.running.is_(True))
                    .values(running=False)
                )
                await session.commit()
        except Exception:
            log.exception("patrol running-flag cleanup failed")

    async def run_once(self) -> None:
        """Fire the patrol for every enabled group whose schedule has passed."""
        async with self.session_factory() as session:
            groups = await list_authorized_groups(session)
            group_ids = [int(row.group_id) for row in groups]
            group_settings: dict[int, dict] = {}
            last_runs: dict[int, datetime | None] = {}
            for group_id in group_ids:
                group = await session.get(Group, group_id)
                group_settings[group_id] = dict(group.settings or {}) if group else {}
                run_row = await session.get(PatrolRun, group_id)
                last_runs[group_id] = run_row.last_run_at if run_row else None

        schedule_time = str(getattr(self.settings, "patrol_schedule_time", "") or "")
        now = now_shanghai_naive()
        for group_id in group_ids:
            if not patrol_policy(self.settings, group_settings.get(group_id)):
                continue
            if not patrol_due(
                schedule_time=schedule_time,
                last_run_at=last_runs.get(group_id),
                now=now,
            ):
                continue
            try:
                summary = await self.run_group_patrol(group_id)
                log.info("[%s] scheduled patrol finished | %s", group_id, summary)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("[%s] scheduled patrol failed", group_id)

    def start_manual_patrol(self, group_id: int) -> dict[str, Any]:
        """Fire-and-forget manual trigger (Mini App); returns immediately."""
        group_id = int(group_id)
        if self.is_running(group_id):
            return {"started": False, "status": "already_running"}
        task = asyncio.create_task(
            self._manual_patrol(group_id),
            name=f"manual-patrol:{group_id}",
        )
        self._manual_tasks.add(task)
        task.add_done_callback(self._manual_tasks.discard)
        return {"started": True, "status": "started"}

    async def _manual_patrol(self, group_id: int) -> None:
        try:
            summary = await self.run_group_patrol(group_id)
            log.info("[%s] manual patrol finished | %s", group_id, summary)
        except Exception:
            log.exception("[%s] manual patrol failed", group_id)

    async def run_group_patrol(self, group_id: int) -> dict[str, Any]:
        group_id = int(group_id)
        lock = self._group_locks.setdefault(group_id, asyncio.Lock())
        if lock.locked():
            return {"status": "already_running"}
        async with lock:
            return await self._run_group_patrol_locked(group_id)

    async def _run_group_patrol_locked(self, group_id: int) -> dict[str, Any]:
        settings = self.settings
        if not settings.moderation.enabled:
            return {"status": "moderation_disabled"}
        provider = verification_provider(settings)
        if not verification_service_ready(settings, provider):
            # Muting without a working challenge path would strand members.
            log.warning(
                "[%s] patrol skipped: verification provider unavailable", group_id
            )
            return {"status": "verification_unavailable"}

        moderation = ModerationService(settings.moderation, _build_llm(settings))
        async with self.session_factory() as session:
            rules_fp = await moderation_rules_fingerprint(session, group_id)
            has_rules = bool(
                await session.scalar(
                    select(func.count())
                    .select_from(ModerationRule)
                    .where(
                        ModerationRule.group_id == group_id,
                        ModerationRule.enabled == True,  # noqa: E712
                    )
                )
            )
        if not has_rules:
            await self._finish_run(group_id, scanned=0, violations=0)
            return {"status": "no_rules"}

        await self._mark_running(group_id, True)
        started_at = now_shanghai_naive()
        scanned = 0
        violators: list[PatrolViolator] = []
        try:
            admin_ids = await self._admin_user_ids(group_id)
            async with self.session_factory() as session:
                seeded = await seed_roster_from_history(session, group_id)
                await session.commit()
            if seeded:
                log.info("[%s] patrol roster seeded from history | +%d", group_id, seeded)

            batch_size = max(10, int(getattr(settings, "patrol_batch_size", 500)))
            batch_pause = max(
                0.0, float(getattr(settings, "patrol_batch_pause_seconds", 5.0))
            )
            fetch_bio = bool(getattr(settings, "patrol_fetch_bio", True))
            last_user_id = 0
            while True:
                async with self.session_factory() as session:
                    stmt = (
                        select(GroupMember)
                        .where(
                            GroupMember.group_id == group_id,
                            GroupMember.user_id > last_user_id,
                            GroupMember.left.is_(False),
                            GroupMember.is_bot.is_(False),
                        )
                        .order_by(GroupMember.user_id)
                        .limit(batch_size)
                    )
                    members = list((await session.execute(stmt)).scalars().all())
                if not members:
                    break
                last_user_id = int(members[-1].user_id)
                batch_violators, batch_scanned = await self._scan_batch(
                    group_id,
                    members,
                    moderation=moderation,
                    rules_fp=rules_fp,
                    admin_ids=admin_ids,
                    fetch_bio=fetch_bio,
                )
                scanned += batch_scanned
                violators.extend(batch_violators)
                if len(members) < batch_size:
                    break
                if batch_pause:
                    await asyncio.sleep(batch_pause)

            enforced = await self._enforce_violators(group_id, violators)
        finally:
            await self._mark_running(group_id, False)

        await self._finish_run(
            group_id,
            scanned=scanned,
            violations=len(enforced),
            run_at=started_at,
        )
        return {
            "status": "completed",
            "scanned": scanned,
            "violations": len(enforced),
        }

    async def _admin_user_ids(self, group_id: int) -> set[int]:
        admin_ids: set[int] = set()
        if getattr(self.settings, "super_admin_id", 0):
            admin_ids.add(int(self.settings.super_admin_id))
        try:
            admins = await _tg_call(
                lambda: self.bot.get_chat_administrators(group_id)
            )
            for member in admins or []:
                user = getattr(member, "user", None)
                if user is not None:
                    admin_ids.add(int(user.id))
        except Exception:
            log.warning(
                "[%s] patrol could not list chat administrators", group_id, exc_info=True
            )
        return admin_ids

    async def _fresh_profile(
        self, member: GroupMember, *, fetch_bio: bool
    ) -> tuple[str, str, str]:
        """(full_name, username, bio); falls back to the roster snapshot."""
        full_name = str(member.full_name or "")
        username = str(member.username or "")
        bio = ""
        if not fetch_bio:
            return full_name, username, bio
        try:
            chat = await _tg_call(lambda: self.bot.get_chat(int(member.user_id)))
        except Exception:
            return full_name, username, bio
        if chat is None:
            return full_name, username, bio
        first = str(getattr(chat, "first_name", "") or "")
        last = str(getattr(chat, "last_name", "") or "")
        fresh_name = " ".join(part for part in (first, last) if part).strip()
        # The fetch succeeded, so its username is authoritative even when
        # empty — falling back to the roster value would @-mention a handle
        # the member no longer owns.
        return (
            fresh_name or full_name,
            str(getattr(chat, "username", "") or ""),
            str(getattr(chat, "bio", "") or ""),
        )

    async def _scan_batch(
        self,
        group_id: int,
        members: list[GroupMember],
        *,
        moderation: ModerationService,
        rules_fp: str,
        admin_ids: set[int],
        fetch_bio: bool,
    ) -> tuple[list[PatrolViolator], int]:
        violators: list[PatrolViolator] = []
        scanned = 0
        for member in members:
            user_id = int(member.user_id)
            if user_id in admin_ids:
                continue
            full_name, username, bio = await self._fresh_profile(
                member, fetch_bio=fetch_bio
            )
            if fetch_bio:
                await asyncio.sleep(_PER_MEMBER_CALL_PAUSE)
            profile_text = build_join_profile_text(
                full_name=full_name, username=username, bio=bio
            )
            if not profile_text.strip():
                continue
            signature = profile_screen_signature(
                full_name=full_name,
                username=username,
                bio=bio,
                rules_fingerprint=rules_fp,
            )
            async with self.session_factory() as session:
                if await get_patrol_screen_hash(session, group_id, user_id) == signature:
                    continue
                if await is_globally_banned(session, user_id):
                    continue
                if await get_join_verification(session, group_id, user_id) is not None:
                    # An in-flight join/moderation/patrol challenge already
                    # governs this member; do not clobber its deadline.
                    continue
                if await moderation.is_user_exempt(session, group_id, user_id):
                    continue
                scanned += 1
                try:
                    violated, reason, conclusive = await screen_member_profile_verbose(
                        session,
                        moderation,
                        group_id=group_id,
                        user_id=user_id,
                        profile_text=profile_text,
                    )
                except Exception:
                    # One flaky LLM call must not abort the whole run; the
                    # member is re-screened on the next patrol.
                    log.exception(
                        "[%s] patrol screening failed | user=%s", group_id, user_id
                    )
                    continue
                if violated:
                    violators.append(
                        PatrolViolator(
                            user_id=user_id,
                            full_name=full_name,
                            username=username,
                            reason=reason or "资料命中群规",
                        )
                    )
                elif conclusive:
                    await mark_patrol_screened(
                        session, group_id, user_id, patrol_hash=signature
                    )
                    await session.commit()
        return violators, scanned

    async def _enforce_violators(
        self, group_id: int, violators: list[PatrolViolator]
    ) -> list[PatrolViolator]:
        """Mute violators, post chunked warnings, persist patrol challenges."""
        if not violators:
            return []
        timeout_seconds = verification_timeout_seconds_for_kind(
            self.settings, VERIFICATION_KIND_PATROL
        )
        provider = verification_provider(self.settings)

        enforced: list[PatrolViolator] = []
        for start in range(0, len(violators), PATROL_MENTIONS_PER_MESSAGE):
            # Mute, warn, and persist one chunk at a time: a crash mid-run
            # then strands at most one chunk muted without durable records.
            chunk: list[PatrolViolator] = []
            for violator in violators[start : start + PATROL_MENTIONS_PER_MESSAGE]:
                if await restrict_new_member(self.bot, group_id, violator.user_id):
                    chunk.append(violator)
                else:
                    log.warning(
                        "[%s] patrol mute failed; skipping user %s",
                        group_id,
                        violator.user_id,
                    )
                await asyncio.sleep(_PER_MEMBER_CALL_PAUSE)
            if not chunk:
                continue
            prompt_message_id = 0
            try:
                sent = await _tg_call(
                    lambda: self.bot.send_message(
                        group_id,
                        build_patrol_warning_text(
                            chunk, timeout_seconds=timeout_seconds
                        ),
                        parse_mode="HTML",
                        reply_markup=build_patrol_challenge_keyboard(),
                    )
                )
                prompt_message_id = int(getattr(sent, "message_id", 0) or 0)
            except Exception:
                log.exception("[%s] patrol warning message failed", group_id)
                for violator in chunk:
                    await restore_member_permissions(
                        self.bot, group_id, violator.user_id
                    )
                continue

            deadline = now_shanghai_naive() + timedelta(seconds=timeout_seconds)
            try:
                async with self.session_factory() as session:
                    for violator in chunk:
                        # A join/moderation challenge issued since the scan
                        # governs this member; overwriting it would downgrade
                        # a ban-on-timeout to a kick. The member stays muted
                        # under that other challenge either way.
                        existing = await get_join_verification(
                            session, group_id, violator.user_id
                        )
                        if (
                            existing is not None
                            and existing.kind != VERIFICATION_KIND_PATROL
                        ):
                            continue
                        await upsert_join_verification(
                            session,
                            group_id=group_id,
                            user_id=violator.user_id,
                            deadline_at=deadline,
                            kind=VERIFICATION_KIND_PATROL,
                            reason=(violator.reason or "资料命中群规")[:500],
                            display_name=violator.full_name
                            or (f"@{violator.username}" if violator.username else ""),
                            prompt_message_id=prompt_message_id,
                            provider=provider,
                        )
                    # The mute and warning are already visible; the durable
                    # records must land before this run ends or the sweeper
                    # could never lift/kick these members.
                    await session.commit()
            except Exception:
                log.exception("[%s] patrol challenge persistence failed", group_id)
                for violator in chunk:
                    await restore_member_permissions(
                        self.bot, group_id, violator.user_id
                    )
                continue
            enforced.extend(chunk)
            log.info(
                "[%s] patrol challenge issued | users=%s timeout=%ss",
                group_id,
                ",".join(str(v.user_id) for v in chunk),
                timeout_seconds,
            )
        return enforced

    async def _mark_running(self, group_id: int, running: bool) -> None:
        try:
            async with self.session_factory() as session:
                row = await session.get(PatrolRun, group_id)
                if row is None:
                    session.add(PatrolRun(group_id=group_id, running=running))
                else:
                    row.running = running
                await session.commit()
        except Exception:
            log.debug("[%s] patrol running-flag update failed", group_id, exc_info=True)

    async def _finish_run(
        self,
        group_id: int,
        *,
        scanned: int,
        violations: int,
        run_at: datetime | None = None,
    ) -> None:
        async with self.session_factory() as session:
            row = await session.get(PatrolRun, group_id)
            if row is None:
                row = PatrolRun(group_id=group_id)
                session.add(row)
            row.last_run_at = run_at or now_shanghai_naive()
            row.last_scanned = int(scanned)
            row.last_violations = int(violations)
            row.running = False
            await session.commit()

    async def status(self, group_id: int) -> dict[str, Any]:
        async with self.session_factory() as session:
            row = await session.get(PatrolRun, int(group_id))
        return {
            "running": self.is_running(group_id),
            "last_run_at": (
                row.last_run_at.isoformat() if row and row.last_run_at else None
            ),
            "last_scanned": int(row.last_scanned or 0) if row else 0,
            "last_violations": int(row.last_violations or 0) if row else 0,
        }


def _build_llm(settings: Settings) -> LLMService:
    return LLMService(
        settings.bot.main_model,
        settings.bot.decision_model,
        settings.bot.compress_model,
        moderation=settings.bot.moderation_model,
        vision=settings.bot.vision_model,
        embed=settings.bot.embed_model,
        max_context_tokens=settings.bot.max_context_tokens,
    )
