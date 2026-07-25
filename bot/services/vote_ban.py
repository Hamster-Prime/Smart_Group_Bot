"""Democratic vote-ban orchestration, persistent quotas, and outcomes.

Both ``/voteban`` and the AI skill call :func:`start_vote_ban`; this is the
only path that may consume a user's per-group trigger quota and open a poll.
"""
from __future__ import annotations

import asyncio
import html
import logging
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, Message
from sqlalchemy import func, insert, literal, or_, select, update
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
from bot.services.message_templates import (
    card_field,
    render_expandable_blockquote,
    render_summary_notice,
)
from bot.services.join_verification import (
    UnbanRecovery,
    ban_member,
    close_private_challenge_message,
    complete_leased_join_verification,
    join_verification_lease_is_current,
    lease_join_verification_for_unban,
    reconcile_moderation_ban_after_lost_lease,
    verification_release_blocked_by_ban,
    verification_restriction_required,
)
from bot.utils.telegram import (
    confirm_telegram_delivery,
    configured_auto_delete_seconds,
    is_reply_target_missing_error,
    schedule_message_auto_delete_durable,
)
from bot.utils.timezone import now_shanghai_naive

log = logging.getLogger(__name__)

VOTE_BAN_CALLBACK_PREFIX = "vban"
VOTE_BAN_ADMIN_RESOLUTION_BAN = "admin_ban"
VOTE_BAN_ADMIN_RESOLUTION_CANCEL = "admin_cancel"
VOTE_BAN_MANUAL_UNBAN_RESOLUTION = "manual_unban"
VOTE_BAN_ENABLED_KEY = "vote_ban_enabled"
VOTE_BAN_THRESHOLD_KEY = "vote_ban_threshold"
VOTE_BAN_DURATION_KEY = "vote_ban_duration_seconds"
VOTE_BAN_TRIGGER_LIMIT_KEY = "vote_ban_trigger_limit"
VOTE_BAN_TRIGGER_WINDOW_KEY = "vote_ban_trigger_window_seconds"
# One enforcement attempt performs two sequential Bot API calls. Aiogram's
# default request timeout is 60 seconds, so the lease must exceed their
# combined worst-case duration; otherwise a recovery worker can take over and
# publish a competing failure while the original ban is still completing.
VOTE_BAN_ENFORCEMENT_LEASE_SECONDS = 180
VOTE_BAN_COUNTDOWN_REFRESH_SECONDS = 60

_expiry_tasks: dict[int, asyncio.Task] = {}
_enforcement_tasks: dict[int, asyncio.Task] = {}
_start_locks: dict[tuple[int, int], asyncio.Lock] = {}
_vote_message_edit_locks: dict[int, asyncio.Lock] = {}


def _consume_vote_task_result(task: asyncio.Task[Any]) -> None:
    if task.cancelled():
        return
    try:
        task.exception()
    except (asyncio.CancelledError, Exception):
        pass


async def flush_vote_ban_tasks(*, timeout_seconds: float = 15.0) -> None:
    """Cancel and boundedly join every vote timer/recovery task.

    These tasks outlive the update handler that created them and may access
    both the database and Telegram hours later.  They therefore have to finish
    before the shared engine and Bot session are closed during application
    shutdown.
    """

    current = asyncio.current_task()
    tasks = {
        task
        for task in (*_expiry_tasks.values(), *_enforcement_tasks.values())
        if task is not current and not task.done()
    }
    for task in tasks:
        task.cancel()
    if tasks:
        done, pending = await asyncio.wait(
            tasks,
            timeout=max(0.0, float(timeout_seconds)),
        )
        for task in done:
            _consume_vote_task_result(task)
        if pending:
            log.error(
                "%d vote-ban task(s) ignored shutdown cancellation",
                len(pending),
            )
            for task in pending:
                task.add_done_callback(_consume_vote_task_result)

    # A task's finally block normally retires itself.  Clean completed entries
    # defensively, but never remove a newer replacement that reused the same
    # session id while an older cancelled task was unwinding.
    for registry in (_expiry_tasks, _enforcement_tasks):
        for session_id, task in tuple(registry.items()):
            if task.done() and registry.get(session_id) is task:
                registry.pop(session_id, None)
    for session_id, lock in tuple(_vote_message_edit_locks.items()):
        if not lock.locked():
            _vote_message_edit_locks.pop(session_id, None)


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

    @property
    def telegram_text(self) -> str:
        """Render command-facing failures without polluting skill summaries."""
        if self.ok:
            return self.summary

        details = [card_field("原因", html.escape(self.summary))]
        if self.quota is not None:
            details.extend(
                [
                    card_field(
                        "已用额度",
                        f"<code>{self.quota.used} / {self.quota.limit}</code>",
                    ),
                    card_field("剩余额度", f"<code>{self.quota.remaining}</code>"),
                ]
            )
        return render_summary_notice(
            "民主投票封禁 · 未发起",
            card_field("处理结果", "未创建投票"),
            details=details,
        )

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


def _format_countdown_duration(seconds: int) -> str:
    """Emphasize a countdown value while preserving the readable unit."""
    duration = _format_duration(seconds)
    value, separator, unit = duration.partition(" ")
    if not separator or not value.isdigit():
        return f"<b>{html.escape(duration)}</b>"
    return f"<b><i>{value}</i>{separator}{html.escape(unit)}</b>"


def build_vote_text(record: VoteBanSession, *, approvals: int) -> str:
    # The live vote total belongs on the button. Keeping the body stable makes
    # the countdown the only regular edit and leaves the prompt easier to scan.
    del approvals
    summary = [
        card_field("目标", _mention(record.target_user_id, record.target_display)),
    ]
    reason = str(record.reason or "").strip()
    evidence = str(record.evidence or "").strip()
    if evidence:
        summary.append(card_field("被举报消息", html.escape(_break_user_mentions(evidence[:300]))))

    initiator = card_field(
        "发起人",
        _mention(record.starter_user_id, record.starter_display),
    )
    details: list[str] = []
    if reason:
        details.append(card_field("举报理由", html.escape(_break_user_mentions(reason[:300]))))
    details.append(f"达到 {record.threshold} 票后立即封禁。")
    details.append("管理员可使用下方按钮取消投票或直接封禁。")
    timer = (
        "投票 "
        f"{_format_countdown_duration(_remaining_seconds(record))} "
        "后自动失效。"
    )
    summary_text = "\n".join(summary)
    return "\n\n".join(
        part
        for part in (
            "<b>民主投票封禁 · 进行中</b>",
            f"<blockquote>{summary_text}</blockquote>",
            f"<blockquote>{initiator}</blockquote>",
            render_expandable_blockquote(details),
            timer,
        )
        if part
    )


def _vote_message_edit_lock(session_id: int) -> asyncio.Lock:
    """Return the in-process lock shared by live and timer message edits."""
    if len(_vote_message_edit_locks) > 4096:
        for key, lock in tuple(_vote_message_edit_locks.items()):
            if not lock.locked():
                _vote_message_edit_locks.pop(key, None)
            if len(_vote_message_edit_locks) <= 2048:
                break
    return _vote_message_edit_locks.setdefault(int(session_id), asyncio.Lock())


async def _edit_vote_message_unlocked(
    bot: Bot,
    *,
    group_id: int,
    message_id: int,
    text: str,
    reply_markup: InlineKeyboardMarkup | None,
) -> Any:
    return await bot.edit_message_text(
        chat_id=int(group_id),
        message_id=int(message_id),
        text=text,
        parse_mode="HTML",
        reply_markup=reply_markup,
    )


async def edit_vote_message(
    bot: Bot,
    *,
    session_id: int,
    group_id: int,
    message_id: int,
    text: str,
    reply_markup: InlineKeyboardMarkup | None,
) -> Any:
    """Serialize all in-process edits for one vote prompt.

    The ticker and callback handlers can both edit the same Telegram message.
    A shared lock makes the durable state transition decide their output order:
    a callback that has recorded a newer vote or terminal status always writes
    after an already-running timer refresh.
    """
    async with _vote_message_edit_lock(int(session_id)):
        return await _edit_vote_message_unlocked(
            bot,
            group_id=group_id,
            message_id=message_id,
            text=text,
            reply_markup=reply_markup,
        )


def build_vote_keyboard(session_id: int, approvals: int, threshold: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=f"投票封禁（{approvals}/{threshold}）",
                    callback_data=f"{VOTE_BAN_CALLBACK_PREFIX}:vote:{int(session_id)}",
                )
            ],
            [
                InlineKeyboardButton(
                    text="取消投票",
                    callback_data=f"{VOTE_BAN_CALLBACK_PREFIX}:cancel:{int(session_id)}",
                ),
                InlineKeyboardButton(
                    text="直接封禁",
                    callback_data=f"{VOTE_BAN_CALLBACK_PREFIX}:ban:{int(session_id)}",
                ),
            ],
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


async def cancel_vote_bans_for_manual_unban(
    session: AsyncSession,
    *,
    target_user_id: int,
    group_ids: tuple[int, ...] | list[int] | set[int] | None = None,
) -> int:
    """Atomically make a newer manual unban win over open vote enforcement.

    An ``enforcing`` vote may already be inside the Telegram ban call.  Merely
    clearing ``UserWarning`` is therefore insufficient: the old worker would
    otherwise publish its successful ban afterwards.  Changing the vote status
    in the same transaction as the unban recovery generation makes its final
    lease CAS fail, after which the worker reconciles Telegram to current policy.
    """

    conditions = [
        VoteBanSession.target_user_id == int(target_user_id),
        VoteBanSession.status.in_(("active", "enforcing")),
    ]
    selected_groups = tuple(
        dict.fromkeys(int(group_id) for group_id in (group_ids or ()))
    )
    if selected_groups:
        conditions.append(VoteBanSession.group_id.in_(selected_groups))
    result = await session.execute(
        update(VoteBanSession)
        .where(*conditions)
        .values(
            status="cancelled",
            enforcing_started_at=None,
            resolution=VOTE_BAN_MANUAL_UNBAN_RESOLUTION,
        )
    )
    return int(result.rowcount or 0)


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
    """Register one approval while the poll is still active.

    The conditional ``INSERT .. SELECT .. FOR UPDATE`` makes the session row
    and ballot insertion one database ordering point.  That matters in
    multi-process deployments where an administrator can cancel the poll
    between the handler's last read and this write.  SQLite ignores
    ``FOR UPDATE`` but serializes the conditional write statement itself.

    Returns ``False`` when the voter already voted or the poll is no longer
    active; callers that need distinct user-facing text can refresh the poll.
    """
    current = now_shanghai_naive()
    source = (
        select(
            literal(int(session_id)).label("session_id"),
            literal(int(voter_id)).label("user_id"),
        )
        .select_from(VoteBanSession)
        .where(
            VoteBanSession.id == int(session_id),
            VoteBanSession.status == "active",
            VoteBanSession.deadline_at > current,
        )
        .with_for_update()
    )
    try:
        async with session.begin_nested():
            result = await session.execute(
                insert(VoteBanVote).from_select(
                    ("session_id", "user_id"),
                    source,
                )
            )
    except IntegrityError:
        return False
    return int(result.rowcount or 0) == 1


async def claim_session_status(
    session: AsyncSession,
    session_id: int,
    *,
    expected: str,
    new_status: str,
    resolution: str = "",
    resolver_user_id: int = 0,
    resolver_display: str = "",
) -> bool:
    values: dict[str, Any] = {"status": new_status}
    if resolution:
        values.update(
            {
                "resolution": str(resolution)[:16],
                "resolver_user_id": int(resolver_user_id or 0),
                "resolver_display": str(resolver_display or "")[:255],
            }
        )
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
    on_delivery: Callable[[], None] | None = None,
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

    # All checks above are read-only. Release their transaction before the
    # Telegram membership lookup, which may otherwise pin a pooled connection
    # for the full network timeout. The session is safely reused below for the
    # quota/session reservation transaction.
    await session.commit()
    member_status = await _target_membership_status(request_message.bot, group_id, target_id)
    if member_status is None:
        return VoteBanStartResult(
            False,
            "target_status_unavailable",
            "暂时无法确认目标用户是否为管理员，请稍后再试。",
        )
    if member_status in {"creator", "administrator"}:
        return VoteBanStartResult(False, "admin_target", "不能对群管理员发起投票。")

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

        warning = await session.scalar(
            select(UserWarning).where(
                UserWarning.group_id == group_id,
                UserWarning.user_id == target_id,
                UserWarning.is_banned.is_(True),
            )
        )
        if (
            warning is not None
            or member_status == "kicked"
            or await is_globally_banned(session, target_id)
        ):
            return VoteBanStartResult(
                False,
                "already_banned",
                "该用户已处于封禁状态，无需重复投票。",
            )

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

        async def _cancel_after_prompt_failure(error: Exception) -> VoteBanStartResult:
            log.error(
                "vote-ban prompt send failed | group=%s session=%s",
                group_id,
                record.id,
                exc_info=(type(error), error, error.__traceback__),
            )
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

        try:
            sent = await request_message.bot.send_message(
                group_id,
                build_vote_text(record, approvals=approvals),
                parse_mode="HTML",
                reply_markup=build_vote_keyboard(int(record.id), approvals, int(record.threshold)),
                reply_to_message_id=target_message_id or None,
            )
        except TelegramBadRequest as exc:
            if not (
                target_message_id
                and is_reply_target_missing_error(str(exc))
            ):
                return await _cancel_after_prompt_failure(exc)
            try:
                sent = await request_message.bot.send_message(
                    group_id,
                    build_vote_text(record, approvals=approvals),
                    parse_mode="HTML",
                    reply_markup=build_vote_keyboard(
                        int(record.id), approvals, int(record.threshold)
                    ),
                )
            except Exception as fallback_exc:
                return await _cancel_after_prompt_failure(fallback_exc)
        except Exception as exc:
            return await _cancel_after_prompt_failure(exc)

        # Telegram has accepted the externally visible poll. Publish that fact
        # before persisting message_id or starting expiry bookkeeping so a later
        # local failure cannot make the caller emit a contradictory failure reply.
        confirm_telegram_delivery(on_delivery)
        record_id = int(record.id)
        delivered_message_id = int(getattr(sent, "message_id", 0) or 0)
        record.message_id = delivered_message_id
        if callable(session_factory):
            # Arm expiry before the post-send commit. If that commit fails, the
            # already-visible poll is still closed by a fresh-session worker.
            schedule_vote_expiry(
                session_factory=session_factory,
                bot=request_message.bot,
                settings=settings,
                session_id=record_id,
                delay_seconds=config.duration_seconds,
            )
        try:
            await session.commit()
        except Exception:
            log.exception(
                "vote-ban message-id commit failed after delivery | group=%s "
                "session=%s message=%s",
                group_id,
                record_id,
                delivered_message_id,
            )
            try:
                await session.rollback()
            except Exception:
                log.exception(
                    "vote-ban rollback failed after message-id commit error | "
                    "group=%s session=%s",
                    group_id,
                    record_id,
                )

            recovered = False
            recovered_record: VoteBanSession | None = None
            if callable(session_factory):
                try:
                    async with session_factory() as recovery_session:
                        persisted = await recovery_session.get(
                            VoteBanSession,
                            record_id,
                        )
                        if persisted is not None:
                            persisted.message_id = delivered_message_id
                            await recovery_session.commit()
                            recovered = True
                            recovered_record = persisted
                except Exception:
                    log.exception(
                        "vote-ban message-id recovery failed | group=%s "
                        "session=%s message=%s",
                        group_id,
                        record_id,
                        delivered_message_id,
                    )
            if not recovered:
                raise
            log.warning(
                "vote-ban message-id persisted through recovery session | "
                "group=%s session=%s message=%s",
                group_id,
                record_id,
                delivered_message_id,
            )
            if recovered_record is not None:
                record = recovered_record
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
    # The target may have been promoted after the poll was created.  Re-check as
    # close as possible to the irreversible action and fail closed when Telegram
    # cannot provide a fresh answer.  Telegram performs its own final permission
    # check too, but this avoids even attempting to ban a newly promoted admin.
    member_status = await _target_membership_status(bot, group_id, target_user_id)
    if member_status is None:
        log.warning(
            "vote ban skipped because target status is unknown | group=%s user=%s",
            group_id,
            target_user_id,
        )
        return False
    if member_status in {"creator", "administrator"}:
        log.warning(
            "vote ban skipped because target is now admin | group=%s user=%s status=%s",
            group_id,
            target_user_id,
            member_status,
        )
        return False
    # Reuse the central enforcement path so an ambiguous Telegram response is
    # confirmed from remote membership state and revoke_messages=True remains
    # mandatory for every confirmed ban.
    return await ban_member(bot, int(group_id), int(target_user_id))


async def record_vote_ban_outcome(
    session: AsyncSession,
    record: VoteBanSession,
    *,
    approvals: int,
    banned: bool,
    lease_token: datetime | None,
    recovery: UnbanRecovery | None,
) -> bool:
    """Persist the real outcome only while this worker owns the lease.

    The terminal status CAS, local ban mirror, and audit append share one
    transaction.  A slow worker whose lease was taken over cannot overwrite
    a newer result or append a second final fact.
    """
    if lease_token is None or recovery is None:
        await session.rollback()
        return False
    if banned:
        recovery_claimed = await complete_leased_join_verification(
            session,
            verification_id=int(recovery.verification_id),
            lease_until=recovery.lease_until,
            status="unbanning",
        )
    else:
        # An unconfirmed/failed Telegram attempt keeps its compensation row for
        # the sweeper, but this worker must still prove it owns that exact row.
        recovery_claimed = await join_verification_lease_is_current(
            session,
            verification_id=int(recovery.verification_id),
            lease_until=recovery.lease_until,
            status="unbanning",
        )
    if not recovery_claimed:
        await session.rollback()
        return False
    terminal_status = "passed" if banned else "failed"
    claimed = await session.execute(
        update(VoteBanSession)
        .where(
            VoteBanSession.id == int(record.id),
            VoteBanSession.status == "enforcing",
            VoteBanSession.enforcing_started_at == lease_token,
        )
        .values(
            status=terminal_status,
            enforcing_started_at=None,
        )
    )
    if int(claimed.rowcount or 0) != 1:
        await session.rollback()
        return False
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
    resolution = str(getattr(record, "resolution", "") or "")
    if resolution == VOTE_BAN_ADMIN_RESOLUTION_BAN:
        source = "democratic_vote_admin_ban"
        actor_user_id = int(getattr(record, "resolver_user_id", 0) or 0)
        actor_display = str(getattr(record, "resolver_display", "") or "")
        reason = record.reason or "管理员在民主投票中直接封禁"
    else:
        source = (
            "democratic_vote_skill"
            if record.source == "skill"
            else "democratic_vote_command"
        )
        actor_user_id = int(record.starter_user_id)
        actor_display = record.starter_display
        reason = record.reason or "民主投票达到封禁阈值"
    await record_ban_event(
        session,
        group_id=int(record.group_id),
        target_user_id=int(record.target_user_id),
        target_display=record.target_display,
        target_username=record.target_username,
        action="ban",
        source=source,
        outcome="succeeded" if banned else "failed",
        reason=reason,
        evidence=record.evidence,
        actor_user_id=actor_user_id,
        actor_display=actor_display,
        reference_type="vote_session",
        reference_id=int(record.id),
        details={
            "approvals": int(approvals),
            "threshold": int(record.threshold),
            "trigger_source": str(record.source or "command"),
            "resolution": resolution,
            "deadline_at": record.deadline_at.isoformat(),
        },
    )
    await session.commit()
    await session.refresh(record)
    # The refresh is only for callers that render the final poll state. Do not
    # leave its read transaction checked out across Telegram message editing.
    await session.commit()
    return True


async def reconcile_vote_ban_after_lost_generation(
    bot: Bot,
    session: AsyncSession,
    *,
    group_id: int,
    target_user_id: int,
) -> bool:
    """Make Telegram match durable policy after a vote worker loses either CAS."""

    async def preserve_ban() -> bool:
        await session.rollback()
        blocked = await verification_release_blocked_by_ban(
            session,
            group_id=int(group_id),
            user_id=int(target_user_id),
        )
        if not blocked:
            blocked = bool(
                await session.scalar(
                    select(VoteBanSession.id).where(
                        VoteBanSession.group_id == int(group_id),
                        VoteBanSession.target_user_id == int(target_user_id),
                        VoteBanSession.status == "enforcing",
                    )
                )
            )
        await session.commit()
        return bool(blocked)

    async def restriction_required() -> bool:
        await session.rollback()
        required = await verification_restriction_required(
            session,
            group_id=int(group_id),
            user_id=int(target_user_id),
        )
        await session.commit()
        return bool(required)

    return await reconcile_moderation_ban_after_lost_lease(
        bot,
        int(group_id),
        int(target_user_id),
        preserve_ban,
        restriction_required=restriction_required,
    )


def admin_cancel_outcome_line(record: VoteBanSession) -> str:
    """User-visible result line for a poll cancelled by a group admin."""
    resolver = _mention(
        int(getattr(record, "resolver_user_id", 0) or 0),
        str(getattr(record, "resolver_display", "") or "管理员"),
    )
    return f"管理员 {resolver} 已取消本次投票"


def enforcement_outcome_line(record: VoteBanSession, *, banned: bool) -> str:
    """User-visible result line for a finished enforcement attempt."""
    if str(getattr(record, "resolution", "") or "") == VOTE_BAN_ADMIN_RESOLUTION_BAN:
        resolver = _mention(
            int(getattr(record, "resolver_user_id", 0) or 0),
            str(getattr(record, "resolver_display", "") or "管理员"),
        )
        if banned:
            return f"管理员 {resolver} 直接封禁了该用户"
        return f"管理员 {resolver} 尝试直接封禁，但 Telegram 封禁失败，请手动处理"
    if banned:
        return "票数达标，已封禁该用户"
    return "票数达标，但 Telegram 封禁失败，请管理员手动处理"


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
        await session.commit()
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
    recovery = await lease_join_verification_for_unban(
        session,
        int(record.group_id),
        int(record.target_user_id),
        manual_unban=False,
    )
    if recovery is None:
        await session.rollback()
        return None
    await session.commit()

    fresh = await session.get(VoteBanSession, int(record.id), populate_existing=True)
    if fresh is None or fresh.status != "enforcing":
        await session.commit()
        return None
    owns_generation = await join_verification_lease_is_current(
        session,
        verification_id=int(recovery.verification_id),
        lease_until=recovery.lease_until,
        status="unbanning",
    )
    if not owns_generation:
        await session.commit()
        return None
    approvals = await count_approvals(session, int(fresh.id))
    # The lease and approval snapshot are now authoritative for this attempt.
    # End the read transaction before the Bot API call so a slow/ambiguous ban
    # cannot pin a pooled connection for its network timeout. ``expire_on_commit``
    # is disabled for the application session factory, so the detached values
    # below remain safe to use for the final CAS transaction.
    await session.commit()
    banned = await apply_vote_ban(
        bot,
        session,
        group_id=int(fresh.group_id),
        target_user_id=int(fresh.target_user_id),
    )
    try:
        persisted = await record_vote_ban_outcome(
            session,
            fresh,
            approvals=approvals,
            banned=banned,
            lease_token=current,
            recovery=recovery,
        )
    except Exception:
        await session.rollback()
        log.exception(
            "vote-ban recovered outcome persistence failed | group=%s session=%s",
            fresh.group_id,
            fresh.id,
        )
        return None

    if not persisted:
        try:
            await reconcile_vote_ban_after_lost_generation(
                bot,
                session,
                group_id=int(fresh.group_id),
                target_user_id=int(fresh.target_user_id),
            )
        except Exception:
            log.exception(
                "vote-ban lost-generation reconciliation failed | group=%s session=%s",
                fresh.group_id,
                fresh.id,
            )
        latest = await session.get(
            VoteBanSession,
            int(fresh.id),
            populate_existing=True,
        )
        latest_status = (
            str(latest.status)
            if latest is not None and latest.status in {"passed", "failed"}
            else None
        )
        await session.commit()
        if latest_status is not None:
            return latest_status
        return None

    cancel_vote_expiry(int(fresh.id))
    if banned:
        await close_private_challenge_message(
            bot,
            int(fresh.target_user_id),
            int(getattr(recovery, "private_message_id", 0) or 0),
        )
    await finalize_vote_message(
        bot,
        settings,
        fresh,
        outcome_line=enforcement_outcome_line(fresh, banned=banned),
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


async def _finalize_vote_message_unlocked(
    bot: Bot,
    settings: Settings,
    record: VoteBanSession,
    *,
    outcome_line: str,
    approvals: int,
) -> None:
    target_message_id = int(getattr(record, "target_message_id", 0) or 0)
    if record.status == "passed" and target_message_id > 0:
        try:
            async with asyncio.timeout(5.0):
                await bot.delete_message(
                    chat_id=int(record.group_id),
                    message_id=target_message_id,
                )
        except Exception:
            log.debug(
                "passed vote-ban target message delete failed | group=%s "
                "session=%s message=%s",
                record.group_id,
                record.id,
                target_message_id,
                exc_info=True,
            )
    if not record.message_id:
        return
    summary = [
        card_field("目标", _mention(record.target_user_id, record.target_display)),
        card_field("票数", f"<code>{approvals}/{record.threshold}</code>"),
        card_field("处理结果", outcome_line),
    ]
    try:
        edited = await _edit_vote_message_unlocked(
            bot,
            group_id=int(record.group_id),
            message_id=int(record.message_id),
            text=render_summary_notice(
                "民主投票封禁 · 已结束",
                summary,
                details="投票已关闭，不能再提交票数。",
            ),
            reply_markup=None,
        )
        await schedule_message_auto_delete_durable(
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


async def finalize_vote_message(
    bot: Bot,
    settings: Settings,
    record: VoteBanSession,
    *,
    outcome_line: str,
    approvals: int,
) -> None:
    """Render a terminal vote result after any in-flight live edit."""
    async with _vote_message_edit_lock(int(record.id)):
        await _finalize_vote_message_unlocked(
            bot,
            settings,
            record,
            outcome_line=outcome_line,
            approvals=approvals,
        )


async def _refresh_active_vote_message(
    *,
    session_factory: Any,
    bot: Bot,
    settings: Settings,
    session_id: int,
) -> float | None:
    """Refresh the active prompt and return its remaining lifetime.

    The vote callback commits its ballot before it takes the same message lock.
    Therefore a timer that started with an older count writes first, and the
    newer callback follows it; a terminal callback likewise follows with the
    closed result instead of being overwritten by a timer edit.
    """
    session_id = int(session_id)
    async with _vote_message_edit_lock(session_id):
        async with session_factory() as session:
            record = await session.get(VoteBanSession, session_id)
            if record is None or record.status != "active":
                await session.commit()
                return None

            remaining = _remaining_seconds(record)
            if remaining <= 0:
                if not await claim_session_status(
                    session,
                    session_id,
                    expected="active",
                    new_status="expired",
                ):
                    await session.rollback()
                    return None
                approvals = await count_approvals(session, session_id)
                await session.commit()
                await _finalize_vote_message_unlocked(
                    bot,
                    settings,
                    record,
                    outcome_line="投票超时，未达到封禁票数",
                    approvals=approvals,
                )
                return None

            approvals = await count_approvals(session, session_id)
            await session.commit()

        if not int(record.message_id or 0):
            return float(remaining)
        try:
            await _edit_vote_message_unlocked(
                bot,
                group_id=int(record.group_id),
                message_id=int(record.message_id),
                text=build_vote_text(record, approvals=approvals),
                reply_markup=build_vote_keyboard(
                    session_id,
                    approvals,
                    int(record.threshold),
                ),
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            log.debug(
                "vote countdown refresh failed | group=%s session=%s",
                record.group_id,
                session_id,
                exc_info=True,
            )
        return float(remaining)


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
        remaining = max(0.01, float(delay_seconds))
        refresh_interval = max(0.01, float(VOTE_BAN_COUNTDOWN_REFRESH_SECONDS))
        try:
            while True:
                await asyncio.sleep(min(refresh_interval, remaining))
                try:
                    remaining = await _refresh_active_vote_message(
                        session_factory=session_factory,
                        bot=bot,
                        settings=settings,
                        session_id=int(session_id),
                    )
                except asyncio.CancelledError:
                    raise
                except Exception:
                    # A short database/Telegram outage should not permanently
                    # drop the expiration timer. Retry on the next refresh.
                    log.exception("vote countdown refresh failed | session=%s", session_id)
                    remaining = refresh_interval
                    continue
                if remaining is None:
                    return
                remaining = max(0.01, float(remaining))
        except asyncio.CancelledError:
            raise
        finally:
            current = asyncio.current_task()
            if _expiry_tasks.get(int(session_id)) is current:
                _expiry_tasks.pop(int(session_id), None)

    try:
        _expiry_tasks[int(session_id)] = asyncio.create_task(
            _expire(), name=f"vote-ban-expiry:{session_id}"
        )
    except RuntimeError:
        log.debug("vote expiry scheduling failed | session=%s", session_id)


def cancel_vote_expiry(session_id: int) -> None:
    task = _expiry_tasks.pop(int(session_id), None)
    if (
        task is not None
        and task is not asyncio.current_task()
        and not task.done()
    ):
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
