"""Democratic vote-ban orchestration, persistent quotas, and outcomes.

Both ``/voteban`` and the AI skill call :func:`start_vote_ban`; this is the
only path that may consume a user's per-group trigger quota and open a poll.
"""
from __future__ import annotations

import asyncio
import html
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from aiogram import Bot
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, Message
from sqlalchemy import func, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from bot.config import Settings
from bot.db.models import (
    Group,
    UserWarning,
    VoteBanQuotaBucket,
    VoteBanSession,
    VoteBanVote,
)
from bot.services.authz import is_group_authorized, is_super_admin_user_id
from bot.services.ban_audit import record_ban_event
from bot.services.join_screening import is_globally_banned
from bot.utils.telegram import configured_auto_delete_seconds, schedule_message_auto_delete
from bot.utils.timezone import now_shanghai_naive

log = logging.getLogger(__name__)

VOTE_BAN_CALLBACK_PREFIX = "vban"
VOTE_BAN_ENABLED_KEY = "vote_ban_enabled"
VOTE_BAN_THRESHOLD_KEY = "vote_ban_threshold"
VOTE_BAN_DURATION_KEY = "vote_ban_duration_seconds"
VOTE_BAN_TRIGGER_LIMIT_KEY = "vote_ban_trigger_limit"
VOTE_BAN_TRIGGER_WINDOW_KEY = "vote_ban_trigger_window_seconds"
VOTE_BAN_ENFORCEMENT_LEASE_SECONDS = 60

_expiry_tasks: dict[int, asyncio.Task] = {}
_enforcement_tasks: dict[int, asyncio.Task] = {}
_start_locks: dict[tuple[int, int], asyncio.Lock] = {}


@dataclass(slots=True)
class VoteBanConfig:
    enabled: bool
    threshold: int
    duration_seconds: int
    trigger_limit: int
    trigger_window_seconds: int


@dataclass(slots=True)
class VoteBanQuotaState:
    allowed: bool
    limit: int
    used: int
    remaining: int
    window_seconds: int
    window_started_at: datetime
    reset_at: datetime
    retry_after_seconds: int

    def payload(self) -> dict[str, Any]:
        return {
            "limit": int(self.limit),
            "used": int(self.used),
            "remaining": int(self.remaining),
            "window_seconds": int(self.window_seconds),
            "window_started_at": self.window_started_at.isoformat(),
            "reset_at": self.reset_at.isoformat(),
            "retry_after_seconds": int(self.retry_after_seconds),
        }


@dataclass(slots=True)
class VoteBanStartResult:
    ok: bool
    code: str
    summary: str
    record: VoteBanSession | None = None
    quota: VoteBanQuotaState | None = None
    sent_message_id: int = 0

    def payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "code": self.code,
            "session_id": int(self.record.id) if self.record is not None else 0,
            "message_id": int(self.sent_message_id or 0),
        }
        if self.quota is not None:
            payload["quota"] = self.quota.payload()
        if self.record is not None:
            payload.update(
                {
                    "target_user_id": int(self.record.target_user_id),
                    "threshold": int(self.record.threshold),
                    "deadline_at": self.record.deadline_at.isoformat(),
                }
            )
        return payload


def _group_int(group_settings: dict | None, key: str) -> int | None:
    if not isinstance(group_settings, dict) or key not in group_settings:
        return None
    raw = group_settings.get(key)
    if raw is None or isinstance(raw, bool):
        return None
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return None
    return value if value > 0 else None


def resolve_vote_ban_config(
    settings: Settings,
    group_settings: dict | None = None,
) -> VoteBanConfig:
    """Effective vote-ban settings for one group, clamped to safe ranges."""
    if isinstance(group_settings, dict) and group_settings.get(VOTE_BAN_ENABLED_KEY) is not None:
        enabled = bool(group_settings.get(VOTE_BAN_ENABLED_KEY))
    else:
        enabled = bool(getattr(settings, "vote_ban_enabled", False))
    threshold = _group_int(group_settings, VOTE_BAN_THRESHOLD_KEY) or int(
        getattr(settings, "vote_ban_threshold", 5)
    )
    duration = _group_int(group_settings, VOTE_BAN_DURATION_KEY) or int(
        getattr(settings, "vote_ban_duration_seconds", 1800)
    )
    trigger_limit = _group_int(group_settings, VOTE_BAN_TRIGGER_LIMIT_KEY) or int(
        getattr(settings, "vote_ban_trigger_limit", 3)
    )
    trigger_window = _group_int(group_settings, VOTE_BAN_TRIGGER_WINDOW_KEY) or int(
        getattr(settings, "vote_ban_trigger_window_seconds", 3600)
    )
    return VoteBanConfig(
        enabled=enabled,
        threshold=min(1000, max(2, threshold)),
        duration_seconds=min(86400, max(60, duration)),
        trigger_limit=min(1000, max(1, trigger_limit)),
        trigger_window_seconds=min(604800, max(60, trigger_window)),
    )


def _mention(user_id: int, label: str) -> str:
    shown = html.escape(str(label or "").strip() or str(user_id))
    return f'<a href="tg://user?id={int(user_id)}">{shown}</a>'


def _break_user_mentions(text: str) -> str:
    return re.sub(r"@(?=\w)", "@​", str(text or ""))


def _format_duration(seconds: int) -> str:
    seconds = max(1, int(seconds))
    if seconds % 3600 == 0:
        return f"{seconds // 3600} 小时"
    if seconds % 60 == 0:
        return f"{seconds // 60} 分钟"
    if seconds >= 60:
        return f"{(seconds + 59) // 60} 分钟"
    return f"{seconds} 秒"


def _remaining_seconds(record: VoteBanSession) -> int:
    delta = record.deadline_at - now_shanghai_naive()
    return max(0, int(delta.total_seconds()))


def build_vote_text(record: VoteBanSession, *, approvals: int) -> str:
    lines = [
        "🗳 <b>民主投票封禁</b>",
        f"目标：{_mention(record.target_user_id, record.target_display)}",
        f"发起人：{_mention(record.starter_user_id, record.starter_display)}",
    ]
    reason = str(record.reason or "").strip()
    evidence = str(record.evidence or "").strip()
    if reason:
        lines.append(f"举报理由：{html.escape(_break_user_mentions(reason[:300]))}")
    if evidence:
        lines.append(f"被举报消息：{html.escape(_break_user_mentions(evidence[:300]))}")
    lines.append(f"票数：<b>{approvals}/{record.threshold}</b>")
    lines.append(
        f"达到 {record.threshold} 票后立即封禁；"
        f"投票 {_format_duration(_remaining_seconds(record))} 后自动失效。"
    )
    return "\n".join(lines)


def build_vote_keyboard(session_id: int, approvals: int, threshold: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=f"🗳 投票封禁（{approvals}/{threshold}）",
                    callback_data=f"{VOTE_BAN_CALLBACK_PREFIX}:vote:{int(session_id)}",
                )
            ]
        ]
    )


async def get_active_session(
    session: AsyncSession,
    group_id: int,
    target_user_id: int,
) -> VoteBanSession | None:
    """Return an open poll, including one currently enforcing its result."""
    return await session.scalar(
        select(VoteBanSession).where(
            VoteBanSession.group_id == int(group_id),
            VoteBanSession.target_user_id == int(target_user_id),
            VoteBanSession.status.in_(("active", "enforcing")),
        )
    )


async def count_approvals(session: AsyncSession, session_id: int) -> int:
    value = await session.scalar(
        select(func.count(VoteBanVote.id)).where(
            VoteBanVote.session_id == int(session_id)
        )
    )
    return int(value or 0)


async def open_vote_session(
    session: AsyncSession,
    *,
    group_id: int,
    target_user_id: int,
    target_display: str,
    target_username: str,
    starter_user_id: int,
    starter_display: str,
    reason: str,
    config: VoteBanConfig,
    evidence: str = "",
    source: str = "command",
    target_message_id: int = 0,
) -> VoteBanSession | None:
    """Create a poll and the starter's first vote without poisoning the outer transaction."""
    if await get_active_session(session, group_id, target_user_id) is not None:
        return None
    record = VoteBanSession(
        group_id=int(group_id),
        target_user_id=int(target_user_id),
        target_display=str(target_display or "")[:255],
        target_username=str(target_username or "")[:255],
        starter_user_id=int(starter_user_id),
        starter_display=str(starter_display or "")[:255],
        reason=str(reason or "")[:1000],
        evidence=str(evidence or "")[:1000],
        source=str(source or "command")[:32],
        target_message_id=int(target_message_id or 0),
        threshold=int(config.threshold),
        status="active",
        deadline_at=now_shanghai_naive() + timedelta(seconds=config.duration_seconds),
    )
    try:
        async with session.begin_nested():
            session.add(record)
            await session.flush()
            session.add(
                VoteBanVote(
                    session_id=int(record.id),
                    user_id=int(starter_user_id),
                )
            )
            await session.flush()
    except IntegrityError:
        return None
    return record


async def record_vote(
    session: AsyncSession,
    session_id: int,
    voter_id: int,
) -> bool:
    """Register one approval; False when the voter already voted."""
    try:
        async with session.begin_nested():
            session.add(VoteBanVote(session_id=int(session_id), user_id=int(voter_id)))
            await session.flush()
    except IntegrityError:
        return False
    return True


async def claim_session_status(
    session: AsyncSession,
    session_id: int,
    *,
    expected: str,
    new_status: str,
) -> bool:
    values: dict[str, Any] = {"status": new_status}
    if new_status == "enforcing":
        values["enforcing_started_at"] = now_shanghai_naive()
    elif expected == "enforcing":
        values["enforcing_started_at"] = None
    result = await session.execute(
        update(VoteBanSession)
        .where(
            VoteBanSession.id == int(session_id),
            VoteBanSession.status == expected,
        )
        .values(**values)
    )
    return int(result.rowcount or 0) == 1


def enforcement_is_stale(
    record: VoteBanSession,
    *,
    now: datetime | None = None,
) -> bool:
    if record.status != "enforcing":
        return False
    started_at = record.enforcing_started_at
    if started_at is None:
        return True
    current = now or now_shanghai_naive()
    return started_at <= current - timedelta(
        seconds=VOTE_BAN_ENFORCEMENT_LEASE_SECONDS
    )


def _quota_state(
    *,
    allowed: bool,
    config: VoteBanConfig,
    used: int,
    window_started_at: datetime,
    now: datetime,
) -> VoteBanQuotaState:
    reset_at = window_started_at + timedelta(seconds=config.trigger_window_seconds)
    retry_after = max(0, int((reset_at - now).total_seconds()))
    return VoteBanQuotaState(
        allowed=allowed,
        limit=int(config.trigger_limit),
        used=max(0, int(used)),
        remaining=max(0, int(config.trigger_limit) - max(0, int(used))),
        window_seconds=int(config.trigger_window_seconds),
        window_started_at=window_started_at,
        reset_at=reset_at,
        retry_after_seconds=retry_after,
    )


async def reserve_vote_ban_quota(
    session: AsyncSession,
    *,
    group_id: int,
    user_id: int,
    config: VoteBanConfig,
    now: datetime | None = None,
) -> VoteBanQuotaState:
    """Atomically reserve one use in a persistent rolling fixed window."""
    current = now or now_shanghai_naive()
    identity = (int(group_id), int(user_id))
    for _attempt in range(6):
        row = await session.get(VoteBanQuotaBucket, identity, populate_existing=True)
        if row is None:
            try:
                async with session.begin_nested():
                    session.add(
                        VoteBanQuotaBucket(
                            group_id=identity[0],
                            user_id=identity[1],
                            window_started_at=current,
                            used_count=1,
                            updated_at=current,
                        )
                    )
                    await session.flush()
                return _quota_state(
                    allowed=True,
                    config=config,
                    used=1,
                    window_started_at=current,
                    now=current,
                )
            except IntegrityError:
                session.expire_all()
                continue

        started = row.window_started_at or current
        used = max(0, int(row.used_count or 0))
        reset_at = started + timedelta(seconds=config.trigger_window_seconds)
        if current >= reset_at:
            result = await session.execute(
                update(VoteBanQuotaBucket)
                .where(
                    VoteBanQuotaBucket.group_id == identity[0],
                    VoteBanQuotaBucket.user_id == identity[1],
                    VoteBanQuotaBucket.window_started_at == started,
                )
                .values(
                    window_started_at=current,
                    used_count=1,
                    updated_at=current,
                )
            )
            if int(result.rowcount or 0) == 1:
                return _quota_state(
                    allowed=True,
                    config=config,
                    used=1,
                    window_started_at=current,
                    now=current,
                )
            session.expire_all()
            continue

        if used >= int(config.trigger_limit):
            return _quota_state(
                allowed=False,
                config=config,
                used=used,
                window_started_at=started,
                now=current,
            )

        result = await session.execute(
            update(VoteBanQuotaBucket)
            .where(
                VoteBanQuotaBucket.group_id == identity[0],
                VoteBanQuotaBucket.user_id == identity[1],
                VoteBanQuotaBucket.window_started_at == started,
                VoteBanQuotaBucket.used_count == used,
                VoteBanQuotaBucket.used_count < int(config.trigger_limit),
            )
            .values(used_count=used + 1, updated_at=current)
        )
        if int(result.rowcount or 0) == 1:
            return _quota_state(
                allowed=True,
                config=config,
                used=used + 1,
                window_started_at=started,
                now=current,
            )
        session.expire_all()

    # Heavy cross-process contention should fail closed instead of exceeding
    # the configured quota. A short retry gives the user a useful instruction.
    return VoteBanQuotaState(
        allowed=False,
        limit=int(config.trigger_limit),
        used=int(config.trigger_limit),
        remaining=0,
        window_seconds=int(config.trigger_window_seconds),
        window_started_at=current,
        reset_at=current + timedelta(seconds=1),
        retry_after_seconds=1,
    )


async def release_vote_ban_quota(
    session: AsyncSession,
    *,
    group_id: int,
    user_id: int,
    reservation: VoteBanQuotaState,
) -> None:
    """Return a reservation when no Telegram poll was actually delivered."""
    await session.execute(
        update(VoteBanQuotaBucket)
        .where(
            VoteBanQuotaBucket.group_id == int(group_id),
            VoteBanQuotaBucket.user_id == int(user_id),
            VoteBanQuotaBucket.window_started_at == reservation.window_started_at,
            VoteBanQuotaBucket.used_count > 0,
        )
        .values(
            used_count=VoteBanQuotaBucket.used_count - 1,
            updated_at=now_shanghai_naive(),
        )
    )


def _display_name(user: Any) -> str:
    return (
        str(getattr(user, "full_name", "") or "").strip()
        or str(getattr(user, "username", "") or "").strip()
        or str(int(getattr(user, "id", 0) or 0))
    )


async def _target_membership_status(
    bot: Bot,
    group_id: int,
    target_user_id: int,
) -> str | None:
    try:
        member = await bot.get_chat_member(int(group_id), int(target_user_id))
    except Exception:
        log.warning(
            "vote-ban target membership lookup failed | group=%s target=%s",
            group_id,
            target_user_id,
            exc_info=True,
        )
        return None
    return str(getattr(member, "status", "") or "").lower()


async def _expire_stale_open_session(
    *,
    session: AsyncSession,
    bot: Bot,
    settings: Settings,
    record: VoteBanSession,
) -> None:
    if record.status == "enforcing":
        await recover_stale_vote_enforcement(
            bot=bot,
            session=session,
            settings=settings,
            record=record,
        )
        return
    if record.status != "active" or not expire_overdue(record):
        return
    if not await claim_session_status(
        session,
        int(record.id),
        expected="active",
        new_status="expired",
    ):
        await session.rollback()
        return
    approvals = await count_approvals(session, int(record.id))
    await session.commit()
    cancel_vote_expiry(int(record.id))
    await finalize_vote_message(
        bot,
        settings,
        record,
        outcome_line="投票超时，未达到封禁票数",
        approvals=approvals,
    )


def _start_lock(group_id: int, starter_user_id: int) -> asyncio.Lock:
    if len(_start_locks) > 4096:
        for key, lock in list(_start_locks.items()):
            if not lock.locked():
                _start_locks.pop(key, None)
            if len(_start_locks) <= 2048:
                break
    return _start_locks.setdefault(
        (int(group_id), int(starter_user_id)),
        asyncio.Lock(),
    )


async def start_vote_ban(
    request_message: Message,
    session: AsyncSession,
    settings: Settings,
    *,
    reason_override: str = "",
    trigger_source: str = "command",
    session_factory: Any | None = None,
) -> VoteBanStartResult:
    """Validate, reserve quota, open, and deliver a vote-ban poll."""
    chat = getattr(request_message, "chat", None)
    if chat is None or getattr(chat, "type", "") not in {"group", "supergroup"}:
        return VoteBanStartResult(False, "group_only", "该操作只能在群内使用。")

    starter = getattr(request_message, "from_user", None)
    if starter is None or getattr(request_message, "sender_chat", None) is not None:
        return VoteBanStartResult(False, "anonymous_starter", "匿名身份不能发起投票。")
    starter_id = int(getattr(starter, "id", 0) or 0)
    if starter_id <= 0:
        return VoteBanStartResult(False, "invalid_starter", "无法确认发起人身份。")

    group_id = int(chat.id)
    if not await is_group_authorized(session, group_id):
        return VoteBanStartResult(False, "unauthorized_group", "当前群组未授权。")

    group_row = await session.get(Group, group_id)
    group_settings = (group_row.settings if group_row and group_row.settings else {})
    config = resolve_vote_ban_config(settings, group_settings)
    if not config.enabled:
        return VoteBanStartResult(False, "disabled", "本群未启用民主投票封禁。")

    reply = getattr(request_message, "reply_to_message", None)
    target = getattr(reply, "from_user", None)
    if reply is None or target is None or getattr(reply, "sender_chat", None) is not None:
        return VoteBanStartResult(
            False,
            "missing_reply_target",
            "请回复目标用户的消息后再发起民主投票。",
        )

    target_id = int(getattr(target, "id", 0) or 0)
    if target_id <= 0:
        return VoteBanStartResult(False, "invalid_target", "无法确认被投票用户。")
    if bool(getattr(target, "is_bot", False)):
        return VoteBanStartResult(False, "bot_target", "不能对机器人发起投票。")
    if target_id == starter_id:
        return VoteBanStartResult(False, "self_target", "不能对自己发起投票。")
    if is_super_admin_user_id(target_id, settings):
        return VoteBanStartResult(False, "owner_target", "不能对最高管理员发起投票。")

    member_status = await _target_membership_status(request_message.bot, group_id, target_id)
    if member_status is None:
        return VoteBanStartResult(
            False,
            "target_status_unavailable",
            "暂时无法确认目标用户是否为管理员，请稍后再试。",
        )
    if member_status in {"creator", "administrator"}:
        return VoteBanStartResult(False, "admin_target", "不能对群管理员发起投票。")

    warning = await session.scalar(
        select(UserWarning).where(
            UserWarning.group_id == group_id,
            UserWarning.user_id == target_id,
            UserWarning.is_banned.is_(True),
        )
    )
    if warning is not None or member_status == "kicked" or await is_globally_banned(session, target_id):
        return VoteBanStartResult(False, "already_banned", "该用户已处于封禁状态，无需重复投票。")

    reason = " ".join(str(reason_override or "").split()).strip()[:1000]
    evidence = " ".join(
        str(getattr(reply, "text", None) or getattr(reply, "caption", None) or "").split()
    ).strip()[:1000]
    target_message_id = int(getattr(reply, "message_id", 0) or 0)

    async with _start_lock(group_id, starter_id):
        existing = await get_active_session(session, group_id, target_id)
        if existing is not None:
            await _expire_stale_open_session(
                session=session,
                bot=request_message.bot,
                settings=settings,
                record=existing,
            )
            existing = await get_active_session(session, group_id, target_id)
        if existing is not None:
            message = (
                "该用户的投票结果正在执行，请等待处理完成。"
                if existing.status == "enforcing"
                else "该用户已有进行中的投票。"
            )
            return VoteBanStartResult(False, "active_vote_exists", message, record=existing)

        quota = await reserve_vote_ban_quota(
            session,
            group_id=group_id,
            user_id=starter_id,
            config=config,
        )
        if not quota.allowed:
            return VoteBanStartResult(
                False,
                "starter_quota_exhausted",
                (
                    f"你在 {_format_duration(config.trigger_window_seconds)}内最多只能发起 "
                    f"{config.trigger_limit} 次民主投票；额度已用完，请 "
                    f"{_format_duration(max(1, quota.retry_after_seconds))} 后再试。"
                ),
                quota=quota,
            )

        record = await open_vote_session(
            session,
            group_id=group_id,
            target_user_id=target_id,
            target_display=_display_name(target),
            target_username=str(getattr(target, "username", "") or ""),
            starter_user_id=starter_id,
            starter_display=_display_name(starter),
            reason=reason,
            evidence=evidence,
            source=trigger_source,
            target_message_id=target_message_id,
            config=config,
        )
        if record is None:
            await release_vote_ban_quota(
                session,
                group_id=group_id,
                user_id=starter_id,
                reservation=quota,
            )
            await session.commit()
            return VoteBanStartResult(False, "active_vote_exists", "该用户已有进行中的投票。")

        try:
            await session.commit()
        except Exception:
            await session.rollback()
            log.exception("vote-ban session commit failed | group=%s starter=%s", group_id, starter_id)
            return VoteBanStartResult(False, "database_failed", "投票创建失败，请稍后重试。")

        approvals = 1
        try:
            sent = await request_message.bot.send_message(
                group_id,
                build_vote_text(record, approvals=approvals),
                parse_mode="HTML",
                reply_markup=build_vote_keyboard(int(record.id), approvals, int(record.threshold)),
                reply_to_message_id=target_message_id or None,
            )
        except Exception:
            try:
                sent = await request_message.bot.send_message(
                    group_id,
                    build_vote_text(record, approvals=approvals),
                    parse_mode="HTML",
                    reply_markup=build_vote_keyboard(
                        int(record.id), approvals, int(record.threshold)
                    ),
                )
            except Exception:
                log.exception("vote-ban prompt send failed | group=%s session=%s", group_id, record.id)
                record.status = "cancelled"
                await release_vote_ban_quota(
                    session,
                    group_id=group_id,
                    user_id=starter_id,
                    reservation=quota,
                )
                await session.commit()
                released_quota = _quota_state(
                    allowed=True,
                    config=config,
                    used=max(0, quota.used - 1),
                    window_started_at=quota.window_started_at,
                    now=now_shanghai_naive(),
                )
                return VoteBanStartResult(
                    False,
                    "send_failed",
                    "投票消息发送失败，本次未扣除额度，请稍后重试。",
                    record=record,
                    quota=released_quota,
                )

        record.message_id = int(getattr(sent, "message_id", 0) or 0)
        await session.commit()
        if session_factory is not None:
            schedule_vote_expiry(
                session_factory=session_factory,
                bot=request_message.bot,
                settings=settings,
                session_id=int(record.id),
                delay_seconds=config.duration_seconds,
            )
        log.info(
            "[%s] democratic vote opened | target=%s starter=%s threshold=%s source=%s quota=%s/%s",
            group_id,
            target_id,
            starter_id,
            record.threshold,
            trigger_source,
            quota.used,
            quota.limit,
        )
        return VoteBanStartResult(
            True,
            "started",
            (
                f"已发起民主投票（1/{record.threshold}）；"
                f"本统计周期还可发起 {quota.remaining} 次。"
            ),
            record=record,
            quota=quota,
            sent_message_id=record.message_id,
        )


async def apply_vote_ban(
    bot: Bot,
    session: AsyncSession,
    *,
    group_id: int,
    target_user_id: int,
) -> bool:
    """Execute only the Telegram side effect.

    Local ban state is intentionally written later, in the same transaction
    as the final vote status and audit event.  A crash between the Telegram
    call and that transaction therefore leaves an ``enforcing`` lease that a
    recovery worker can safely retry instead of a false local "banned" fact.
    """
    del session  # Kept in the signature for existing callers and tests.
    try:
        banned = await bot.ban_chat_member(int(group_id), int(target_user_id))
        if banned is False:
            raise RuntimeError("Telegram returned false")
    except Exception:
        log.exception("vote ban failed | group=%s user=%s", group_id, target_user_id)
        return False
    return True


async def record_vote_ban_outcome(
    session: AsyncSession,
    record: VoteBanSession,
    *,
    approvals: int,
    banned: bool,
) -> None:
    """Persist the actual Telegram result and a trusted audit event."""
    if banned:
        warning = await session.scalar(
            select(UserWarning).where(
                UserWarning.group_id == int(record.group_id),
                UserWarning.user_id == int(record.target_user_id),
            )
        )
        if warning is None:
            session.add(
                UserWarning(
                    group_id=int(record.group_id),
                    user_id=int(record.target_user_id),
                    count=0,
                    is_banned=True,
                )
            )
        else:
            warning.is_banned = True
    record.status = "passed" if banned else "failed"
    record.enforcing_started_at = None
    source = "democratic_vote_skill" if record.source == "skill" else "democratic_vote_command"
    await record_ban_event(
        session,
        group_id=int(record.group_id),
        target_user_id=int(record.target_user_id),
        target_display=record.target_display,
        target_username=record.target_username,
        action="ban",
        source=source,
        outcome="succeeded" if banned else "failed",
        reason=record.reason or "民主投票达到封禁阈值",
        evidence=record.evidence,
        actor_user_id=int(record.starter_user_id),
        actor_display=record.starter_display,
        reference_type="vote_session",
        reference_id=int(record.id),
        details={
            "approvals": int(approvals),
            "threshold": int(record.threshold),
            "trigger_source": str(record.source or "command"),
            "deadline_at": record.deadline_at.isoformat(),
        },
    )
    await session.commit()


async def recover_stale_vote_enforcement(
    *,
    bot: Bot,
    session: AsyncSession,
    settings: Settings,
    record: VoteBanSession,
) -> str | None:
    """Retry one abandoned ``enforcing`` lease and persist its real outcome.

    Telegram bans are idempotent.  The compare-and-swap lease renewal lets a
    single worker retry after a crash without allowing two workers to append
    competing final audit facts.
    """
    current = now_shanghai_naive()
    if not enforcement_is_stale(record, now=current):
        return None
    cutoff = current - timedelta(seconds=VOTE_BAN_ENFORCEMENT_LEASE_SECONDS)
    claimed = await session.execute(
        update(VoteBanSession)
        .where(
            VoteBanSession.id == int(record.id),
            VoteBanSession.status == "enforcing",
            or_(
                VoteBanSession.enforcing_started_at.is_(None),
                VoteBanSession.enforcing_started_at <= cutoff,
            ),
        )
        .values(enforcing_started_at=current)
    )
    if int(claimed.rowcount or 0) != 1:
        await session.rollback()
        return None
    await session.commit()

    fresh = await session.get(VoteBanSession, int(record.id), populate_existing=True)
    if fresh is None or fresh.status != "enforcing":
        return None
    approvals = await count_approvals(session, int(fresh.id))
    banned = await apply_vote_ban(
        bot,
        session,
        group_id=int(fresh.group_id),
        target_user_id=int(fresh.target_user_id),
    )
    try:
        if banned:
            # Keep pending verification state consistent with the confirmed
            # Telegram ban; this participates in the final transaction.
            from bot.services.join_verification import delete_join_verification

            await delete_join_verification(
                session,
                int(fresh.group_id),
                int(fresh.target_user_id),
            )
        await record_vote_ban_outcome(
            session,
            fresh,
            approvals=approvals,
            banned=banned,
        )
    except Exception:
        await session.rollback()
        log.exception(
            "vote-ban recovered outcome persistence failed | group=%s session=%s",
            fresh.group_id,
            fresh.id,
        )
        return None

    cancel_vote_expiry(int(fresh.id))
    outcome_line = (
        "票数达标，已封禁该用户"
        if banned
        else "票数达标，但 Telegram 封禁失败，请管理员手动处理"
    )
    await finalize_vote_message(
        bot,
        settings,
        fresh,
        outcome_line=outcome_line,
        approvals=approvals,
    )
    log.info(
        "[%s] recovered democratic vote enforcement | session=%s target=%s status=%s",
        fresh.group_id,
        fresh.id,
        fresh.target_user_id,
        fresh.status,
    )
    return str(fresh.status)


async def finalize_vote_message(
    bot: Bot,
    settings: Settings,
    record: VoteBanSession,
    *,
    outcome_line: str,
    approvals: int,
) -> None:
    if not record.message_id:
        return
    lines = [
        "🗳 <b>民主投票封禁</b>",
        f"目标：{_mention(record.target_user_id, record.target_display)}",
        f"票数：<b>{approvals}/{record.threshold}</b>",
        f"<b>处理结果</b>: {outcome_line}",
    ]
    try:
        edited = await bot.edit_message_text(
            chat_id=int(record.group_id),
            message_id=int(record.message_id),
            text="\n".join(lines),
            parse_mode="HTML",
            reply_markup=None,
        )
        schedule_message_auto_delete(
            edited if not isinstance(edited, bool) else None,
            configured_auto_delete_seconds(settings, "vote"),
        )
    except Exception:
        log.debug(
            "vote message finalize failed | group=%s message=%s",
            record.group_id,
            record.message_id,
            exc_info=True,
        )


def schedule_vote_expiry(
    *,
    session_factory: Any,
    bot: Bot,
    settings: Settings,
    session_id: int,
    delay_seconds: int,
) -> None:
    existing = _expiry_tasks.pop(int(session_id), None)
    if existing is not None and not existing.done():
        existing.cancel()

    async def _expire() -> None:
        await asyncio.sleep(max(1, int(delay_seconds)))
        try:
            async with session_factory() as session:
                record = await session.get(VoteBanSession, int(session_id))
                if record is None or record.status != "active":
                    return
                if not await claim_session_status(
                    session, session_id, expected="active", new_status="expired"
                ):
                    return
                approvals = await count_approvals(session, session_id)
                await session.commit()
            await finalize_vote_message(
                bot,
                settings,
                record,
                outcome_line="投票超时，未达到封禁票数",
                approvals=approvals,
            )
        except Exception:
            log.exception("vote expiry failed | session=%s", session_id)
        finally:
            _expiry_tasks.pop(int(session_id), None)

    try:
        _expiry_tasks[int(session_id)] = asyncio.create_task(
            _expire(), name=f"vote-ban-expiry:{session_id}"
        )
    except RuntimeError:
        log.debug("vote expiry scheduling failed | session=%s", session_id)


def cancel_vote_expiry(session_id: int) -> None:
    task = _expiry_tasks.pop(int(session_id), None)
    if task is not None and not task.done():
        task.cancel()


def schedule_vote_enforcement_recovery(
    *,
    session_factory: Any,
    bot: Bot,
    settings: Settings,
    session_id: int,
    delay_seconds: int | float = VOTE_BAN_ENFORCEMENT_LEASE_SECONDS,
) -> None:
    existing = _enforcement_tasks.pop(int(session_id), None)
    if existing is not None and not existing.done():
        existing.cancel()

    async def _recover_loop() -> None:
        delay = max(1.0, float(delay_seconds))
        try:
            while True:
                await asyncio.sleep(delay)
                async with session_factory() as recovery_session:
                    record = await recovery_session.get(
                        VoteBanSession,
                        int(session_id),
                    )
                    if record is None or record.status != "enforcing":
                        return
                    if not enforcement_is_stale(record):
                        started_at = record.enforcing_started_at or now_shanghai_naive()
                        age = max(
                            0.0,
                            (now_shanghai_naive() - started_at).total_seconds(),
                        )
                        delay = max(
                            1.0,
                            VOTE_BAN_ENFORCEMENT_LEASE_SECONDS - age,
                        )
                        continue
                    status = await recover_stale_vote_enforcement(
                        bot=bot,
                        session=recovery_session,
                        settings=settings,
                        record=record,
                    )
                    if status in {"passed", "failed"}:
                        return
                # A transient persistence problem keeps the row enforcing;
                # renew it after the next lease rather than abandoning it.
                delay = float(VOTE_BAN_ENFORCEMENT_LEASE_SECONDS)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("vote enforcement recovery failed | session=%s", session_id)
        finally:
            current = asyncio.current_task()
            if _enforcement_tasks.get(int(session_id)) is current:
                _enforcement_tasks.pop(int(session_id), None)

    try:
        _enforcement_tasks[int(session_id)] = asyncio.create_task(
            _recover_loop(),
            name=f"vote-ban-enforcement-recovery:{session_id}",
        )
    except RuntimeError:
        log.debug("vote enforcement recovery scheduling failed | session=%s", session_id)


def cancel_vote_enforcement_recovery(session_id: int) -> None:
    task = _enforcement_tasks.pop(int(session_id), None)
    if (
        task is not None
        and task is not asyncio.current_task()
        and not task.done()
    ):
        task.cancel()


async def restore_vote_ban_tasks(
    *,
    session_factory: Any,
    bot: Bot,
    settings: Settings,
) -> None:
    """Restore active expiry timers and abandoned enforcement recovery."""
    async with session_factory() as session:
        rows = list(
            (
                await session.scalars(
                    select(VoteBanSession).where(
                        VoteBanSession.status.in_(("active", "enforcing"))
                    )
                )
            ).all()
        )

    now = now_shanghai_naive()
    active_count = 0
    enforcing_count = 0
    for record in rows:
        if record.status == "active":
            delay = max(1, int((record.deadline_at - now).total_seconds()))
            schedule_vote_expiry(
                session_factory=session_factory,
                bot=bot,
                settings=settings,
                session_id=int(record.id),
                delay_seconds=delay,
            )
            active_count += 1
            continue
        started_at = record.enforcing_started_at
        delay = 1.0
        if started_at is not None:
            age = max(0.0, (now - started_at).total_seconds())
            delay = max(1.0, VOTE_BAN_ENFORCEMENT_LEASE_SECONDS - age)
        schedule_vote_enforcement_recovery(
            session_factory=session_factory,
            bot=bot,
            settings=settings,
            session_id=int(record.id),
            delay_seconds=delay,
        )
        enforcing_count += 1
    if active_count or enforcing_count:
        log.info(
            "restored vote-ban tasks | active=%s enforcing=%s",
            active_count,
            enforcing_count,
        )


def expire_overdue(record: VoteBanSession) -> bool:
    return record.deadline_at <= now_shanghai_naive()
