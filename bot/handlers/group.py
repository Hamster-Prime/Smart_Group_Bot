from __future__ import annotations

import asyncio
import base64
import html
import io
import json
import logging
import re
import time
import weakref
from collections import Counter
from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager
from contextvars import Context
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from aiogram import F, Router
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from sqlalchemy import delete, select, update
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from bot.config import Settings
from bot.db.models import (
    Group,
    ModerationExemption,
    ModerationRule,
    ReplyMute,
    UserWarning,
    Violation,
    VoteBanSession,
)
from bot.db.sqlite_session import is_database_locked_error
from bot.services import memory_holder
from bot.services.admin_status import is_user_admin_cached
from bot.services.at_reply import is_at_reply_enabled
from bot.services.authz import (
    ensure_group_authorized,
    is_group_authorized,
    is_super_admin_user_id,
)
from bot.services.callback_auth import is_group_admin_or_higher
from bot.services.ban_audit import record_ban_event
from bot.services.call_admin import (
    CALL_ADMIN_RESOLVE_CALLBACK_DATA,
    handle_call_admin,
    is_call_admin_trigger,
    remove_call_admin_resolution_button,
)
from bot.services.api_model_query import api_model_query_tool_enabled
from bot.services.group_settings import acquire_group_settings_write_intent
from bot.services.vote_ban import (
    VOTE_BAN_ADMIN_RESOLUTION_BAN,
    VOTE_BAN_ADMIN_RESOLUTION_CANCEL,
    VOTE_BAN_CALLBACK_PREFIX,
    admin_cancel_outcome_line,
    apply_vote_ban,
    build_vote_keyboard,
    build_vote_text,
    cancel_vote_enforcement_recovery,
    cancel_vote_expiry,
    claim_session_status,
    count_approvals,
    edit_vote_message,
    enforcement_outcome_line,
    expire_overdue,
    finalize_vote_message,
    recover_stale_vote_enforcement,
    reconcile_vote_ban_after_lost_generation,
    record_vote_ban_outcome,
    record_vote,
    schedule_vote_enforcement_recovery,
)
from bot.services.casual import CasualService
from bot.services.decision import DecisionService
from bot.services.doubao_tts import (
    DoubaoTTSService,
    TTSDeliveryResult,
    is_tts_always_enabled,
    is_tts_tool_enabled,
    normalize_tts_mode,
    tts_sender_accepts_delivery_callback,
)
from bot.services.speech_style import (
    SpeechStyleService,
    build_style_profile_context,
    get_style_state,
)
from bot.services.llm import LLMService
from bot.services.join_verification import (
    UnbanRecovery,
    activate_manual_unban_recovery,
    ban_member,
    begin_moderation_challenge,
    close_private_challenge_message,
    complete_leased_join_verification,
    delete_join_verification,
    enforce_ban_with_policy_reconciliation_result,
    join_verification_lease_is_current,
    lease_join_verification_for_unban,
    manual_unban_generation_is_active,
    moderation_challenge_ready,
    release_moderation_restriction_after_exemption,
    restore_member_permissions,
    telegram_ban_failure_is_deterministic,
    unban_member,
    verification_release_blocked_by_ban,
    verification_restriction_required,
)
from bot.services.join_screening import (
    is_globally_banned,
    is_join_screening_exempt,
    moderation_rules_fingerprint,
)
from bot.services.bot_screening import (
    is_bot_whitelisted,
    record_bot_screening_pass,
    reset_bot_screening,
)
from bot.services.keyword_reply import find_keyword_reply, send_keyword_reply
from bot.services.member_identity import member_display_name
from bot.services.message_templates import card_field, render_summary_notice
from bot.services.moderation import ModerationService
from bot.services.moderation import ModerationVerdict
from bot.services.notification_pins import unpin_notification_message
from bot.services.reply_mode import ReplyModeService
from bot.services.reply_output import ReplyMessageSpec, parse_reply_output
from bot.services.reply_progress import ReplyProgressTracker
from bot.services.proactive import note_group_activity, record_group_activity
from bot.services.privileged_tasks import submit_privileged_task
from bot.services.resource_health import register_resource_health_provider
from bot.services.skills import SkillService
from bot.services.skills.vote_ban import is_explicit_vote_ban_request
from bot.services.sticker_library import sticker_library
from bot.services.update_completion import (
    UpdateCompletionReceipt,
    current_update_completion,
    request_current_update_retry,
)
from bot.utils.security import format_history_message_line
from bot.utils.timezone import now_shanghai_naive
from bot.utils.telegram import (
    answer_with_auto_delete,
    confirm_telegram_delivery,
    configured_auto_delete_seconds,
    DELETE_BUTTON_CALLBACK_PREFIX,
    extract_reply_context,
    extract_message_text,
    has_explicit_bot_mention,
    is_bot_mentioned,
    is_reply_to_bot,
    is_group,
    is_reply_message,
    mentions_other_user,
    sanitize_outgoing_mentions,
    sanitize_outgoing_text,
    schedule_message_auto_delete_durable,
    ReplyMessageOverlay,
    send_reply,
    typing_action,
)

router = Router()
log = logging.getLogger(__name__)
_OWNER_SALUTATION_RE = re.compile(
    r"^\s*(?:(?:好(?:的)?|嗯|嗨|嘿|哈喽|收到|明白|行|是的|当然|ok)\s*)?主人(?:[，,：:!！。\s]|$)+",
    re.IGNORECASE,
)
_SILENT_REPLY_MARKERS = {
    "NO_TRUSTED_ANSWER",
    "NO_RELEVANT_INFO",
    "NO_ANSWER",
    "NO_RESPONSE",
}
_SHORT_UNCERTAIN_REPLY_RE = re.compile(
    r"^(?:我)?(?:不知道|不确定|无法(?:确定|判断|回答)|信息不足|暂无可信来源|无可信来源|无法根据可信来源(?:回答|解释))(?:[，,。.!！?？].*)?$"
)
_SEMANTIC_ABUSE_HINTS = {"骂人", "辱骂", "脏话", "人身攻击", "侮辱", "喷人"}
_ABUSE_LLM_PATTERN = "禁止辱骂、脏话、人身攻击（含谐音、缩写、变体、阴阳怪气）"
_PENDING_REPLY_QUESTION_RE = re.compile(
    r"[?？]|什么|哪个|哪款|怎么|咋|如何|为什么|为啥|推荐|求推荐|帮我|有没有|是不是|行不行|可不可以|能不能|最好用|值不值得|吗|呢|么|嘛",
    re.IGNORECASE,
)
_MODERATION_ACTION_CALLBACK_PREFIX = "mact"
_MODERATION_ACTION_BASES = {"warn", "ban_warning", "ban_applied"}
_MODERATION_ACTION_MARKERS = ("direct", "reverted")
_MODERATION_USER_LOCKS: weakref.WeakValueDictionary[
    tuple[int, int], asyncio.Lock
] = weakref.WeakValueDictionary()
_MAX_VISION_IMAGE_BYTES = 5 * 1024 * 1024
_VISION_DOWNLOAD_TIMEOUT_SEC = 20.0
_VISION_IMAGE_MIME_TYPES = {"image/jpeg", "image/png", "image/webp", "image/gif"}


class _LimitedBytesIO(io.BytesIO):
    def __init__(self, limit: int) -> None:
        super().__init__()
        self._limit = max(1, int(limit))

    def write(self, data: bytes | bytearray | memoryview) -> int:
        if self.tell() + len(data) > self._limit:
            raise ValueError("vision_image_too_large")
        return super().write(data)


def _moderation_user_lock(group_id: int, user_id: int) -> asyncio.Lock:
    key = (int(group_id), int(user_id))
    lock = _MODERATION_USER_LOCKS.get(key)
    if lock is None:
        lock = asyncio.Lock()
        _MODERATION_USER_LOCKS[key] = lock
    return lock


@asynccontextmanager
async def _bounded_moderation_user_lock(
    group_id: int,
    user_id: int,
    *,
    timeout_seconds: float = 2.0,
):
    lock = _moderation_user_lock(group_id, user_id)
    try:
        await asyncio.wait_for(
            lock.acquire(),
            timeout=max(0.01, float(timeout_seconds)),
        )
    except asyncio.TimeoutError:
        yield False
        return
    try:
        yield True
    finally:
        lock.release()


class _DetachedCallbackProxy:
    """Deliver late callback outcomes after the spinner was acknowledged."""

    def __init__(self, callback: CallbackQuery) -> None:
        self.bot = callback.bot
        self.message = callback.message
        self.from_user = callback.from_user
        self.data = callback.data

    async def answer(self, text: str = "", **_kwargs: Any) -> None:
        if not text:
            return
        message_answer = getattr(self.message, "answer", None)
        if callable(message_answer):
            await message_answer(text)
            return
        chat = getattr(self.message, "chat", None)
        chat_id = int(getattr(chat, "id", 0) or 0)
        if chat_id:
            await self.bot.send_message(chat_id, text)


def _source_message_id(message: Message) -> int | None:
    try:
        message_id = int(getattr(message, "message_id", 0) or 0)
    except (TypeError, ValueError):
        return None
    return message_id if message_id > 0 else None


def _violation_event_created(violation: Violation) -> bool:
    # Legacy unit-test doubles and manual rows have no idempotency marker and
    # retain the historical "new event" behavior.
    return bool(getattr(violation, "_source_event_created", True))


def _violation_nullable_bool(violation: Violation, attribute: str) -> bool | None:
    value = getattr(violation, attribute, None)
    return value if isinstance(value, bool) else None


async def _persist_violation_ban_result(
    session: AsyncSession,
    violation: Violation,
    *,
    enforced: bool,
) -> tuple[bool, bool]:
    """CAS one Telegram ban result without letting stale failure beat success.

    ``True`` is the only terminal value.  ``NULL`` means never attempted and
    ``False`` is a durable retry intent.  A failed retry only changes NULL to
    False, while a successful retry may promote either pending value to True.
    The returned pair is ``(effective_result, changed_by_this_worker)``.
    """

    execute = getattr(session, "execute", None)
    try:
        violation_id = int(getattr(violation, "id", 0) or 0)
    except (TypeError, ValueError):
        violation_id = 0
    if not isinstance(violation, Violation) or not callable(execute) or violation_id <= 0:
        # Compatibility for isolated unit doubles. Production violations are
        # always flushed and use the CAS path below.
        violation.ban_enforced = bool(enforced)
        return bool(enforced), True

    condition = (
        Violation.ban_enforced.is_not(True)
        if enforced
        else Violation.ban_enforced.is_(None)
    )
    result = await session.execute(
        update(Violation)
        .where(
            Violation.id == violation_id,
            condition,
        )
        .values(ban_enforced=bool(enforced))
        .execution_options(synchronize_session=False)
    )
    changed = int(result.rowcount or 0) == 1
    if changed:
        return bool(enforced), True
    current = await session.scalar(
        select(Violation.ban_enforced).where(Violation.id == violation_id)
    )
    return bool(current), False


async def _refresh_violation_notice_state(
    session: AsyncSession,
    violation: Violation,
) -> None:
    refresh = getattr(session, "refresh", None)
    if not callable(refresh) or not int(getattr(violation, "id", 0) or 0):
        return
    await refresh(violation, attribute_names=["notice_sent_at"])


async def _send_moderation_notice_once_locked(
    *,
    session: AsyncSession,
    violation: Violation,
    message: Message,
    notice: str,
    auto_delete_seconds: int,
    reply_markup: InlineKeyboardMarkup | None = None,
) -> bool:
    """Send and durably acknowledge one moderation notice.

    The caller owns the per-(group,user) moderation lock.  A process crash
    between Telegram accepting the send and the marker commit can still cause
    one duplicate, but every ordinary retry observes the marker and skips it.
    """

    await _refresh_violation_notice_state(session, violation)
    if getattr(violation, "notice_sent_at", None) is not None:
        # ``refresh`` opens a read transaction.  Even the already-delivered
        # fast path must release it before returning to the outer handler.
        await session.commit()
        return False
    # Telegram delivery can block for the full Bot API timeout.  The durable
    # event already exists and the caller holds the per-user moderation lock,
    # so retaining this read snapshot cannot make the send atomic; it only pins
    # a scarce pooled connection.  Persisted retries remain at-least-once across
    # the unavoidable send-success/process-crash-before-marker window.
    await session.commit()
    await answer_with_auto_delete(
        message,
        notice,
        auto_delete_seconds=auto_delete_seconds,
        reply_markup=reply_markup,
    )
    violation.notice_sent_at = now_shanghai_naive()
    await session.commit()
    return True


async def _fresh_group_authorized_for_moderation(
    session: AsyncSession,
    group_id: int,
) -> bool:
    """Revalidate authorization after a long model/network decision.

    The entry check may be minutes old by the time a verdict arrives. A separate
    short session is intentional: the handler session can still own an old WAL
    read snapshot and cache the AuthorizedGroup row, while rolling it back here
    would expire ORM rule objects contained in the verdict.
    """

    try:
        del session
        session_factory = memory_holder.get().session_factory
        async with session_factory() as fresh_session:
            authorized = await is_group_authorized(fresh_session, int(group_id))
        if not authorized:
            log.info(
                "[%s] moderation result discarded | reason=group_deauthorized",
                group_id,
            )
        return authorized
    except asyncio.CancelledError:
        raise
    except Exception:
        log.exception(
            "[%s] moderation authorization recheck failed; action suppressed",
            group_id,
        )
        return False


async def _claim_current_moderation_verdict(
    session: AsyncSession,
    *,
    group_id: int,
    user_id: int,
    verdict: ModerationVerdict,
) -> bool:
    """Linearize an old verdict against newer policy before side effects.

    ``ModerationService.evaluate`` deliberately releases its DB transaction
    before a potentially slow LLM call.  The verdict therefore carries the
    exact enabled-rule fingerprint it saw.  Re-entering SQLite as a writer makes
    authorization, exemptions, manual-unban generations and the rule snapshot
    one atomic claim; a newer administrative mutation wins instead of being
    overwritten by this stale worker.
    """

    await session.rollback()
    await acquire_group_settings_write_intent(session, int(group_id))
    if manual_unban_generation_is_active(int(group_id), int(user_id)):
        await session.rollback()
        return False
    if not await is_group_authorized(session, int(group_id)):
        await session.rollback()
        return False
    if await is_join_screening_exempt(session, int(user_id)):
        await session.rollback()
        return False
    if await session.scalar(
        select(ModerationExemption.id).where(
            ModerationExemption.group_id == int(group_id),
            ModerationExemption.user_id == int(user_id),
        )
    ) is not None:
        await session.rollback()
        return False
    expected = str(getattr(verdict, "rules_fingerprint", "") or "")
    if expected:
        current = await moderation_rules_fingerprint(session, int(group_id))
        if current != expected:
            await session.rollback()
            return False
    return True


@dataclass(slots=True)
class _SenderIdentity:
    actor_id: int
    username: str
    display_name: str
    is_chat: bool


def _uses_sender_chat_identity(message: Message) -> bool:
    sender_chat = getattr(message, "sender_chat", None)
    user = getattr(message, "from_user", None)
    if sender_chat is None:
        return False
    return user is None or bool(getattr(user, "is_bot", False))


def _resolve_sender_identity(message: Message) -> _SenderIdentity:
    user = getattr(message, "from_user", None)
    if _uses_sender_chat_identity(message):
        sender_chat = getattr(message, "sender_chat", None)
        actor_id = int(getattr(sender_chat, "id", 0) or 0)
        username = (getattr(sender_chat, "username", None) or "").strip()
        display_name = (
            (getattr(sender_chat, "title", None) or "").strip()
            or (getattr(message, "author_signature", None) or "").strip()
            or (f"@{username}" if username else "")
            or f"chat:{actor_id}"
        )
        return _SenderIdentity(
            actor_id=actor_id,
            username=username,
            display_name=display_name,
            is_chat=True,
        )

    actor_id = int(getattr(user, "id", 0) or 0)
    username = (getattr(user, "username", None) or "").strip()
    display_name = ((getattr(user, "full_name", None) or "").strip() or "unknown") if user else "unknown"
    return _SenderIdentity(
        actor_id=actor_id,
        username=username,
        display_name=display_name,
        is_chat=False,
    )


def _build_user_mention(*, user_id: int, username: str, full_name: str) -> str:
    """Render a user reference the way moderation notices show the target:
    a plain @handle when available, otherwise a name/id profile link.

    Shared by the "用户" line and the manual-intervention outcome so both use
    identical display logic.
    """
    handle = (username or "").strip()
    if handle:
        return f"@{handle}"
    label = (full_name or "").strip() or str(user_id)
    return f'<a href="tg://user?id={user_id}">{html.escape(label)}</a>'


def _build_warn_target(
    *,
    user: object | None,
    actor_id: int,
    display_name: str,
    sender_username: str,
    sender_is_chat: bool,
) -> str:
    if sender_is_chat:
        safe_name = html.escape((display_name or "该频道").strip() or "该频道")
        safe_username = html.escape((sender_username or "").strip())
        if safe_username:
            return f'<a href="https://t.me/{safe_username}">{safe_name}</a>'
        return safe_name

    return _build_user_mention(
        user_id=actor_id,
        username=(getattr(user, "username", "") or "") if user else "",
        full_name=(getattr(user, "full_name", "") or "") if user else "",
    )


def _normalize_owner_address(reply: str, sender_is_owner: bool) -> str:
    """Avoid misaddressing non-owner users as '主人'."""
    if sender_is_owner:
        return reply
    if "主人" not in reply:
        return reply

    cleaned = _OWNER_SALUTATION_RE.sub("", reply, count=1).lstrip()
    if not cleaned:
        return "好的，我在。"
    return cleaned


def _strip_reply_marker_payload(text: str) -> str:
    stripped = (text or "").strip()
    if stripped.startswith("```") and stripped.endswith("```"):
        stripped = re.sub(r"^```(?:text|md|markdown)?", "", stripped, flags=re.IGNORECASE).strip()
        stripped = re.sub(r"```$", "", stripped).strip()
    return stripped.strip("`").strip()


def _is_silent_marker_reply(text: str) -> bool:
    payload = _strip_reply_marker_payload(text)
    if not payload:
        return False

    normalized = re.sub(r"[“”\"'`]", "", payload)
    normalized = re.sub(r"\s+", "", normalized)
    normalized = normalized.strip("。.!！?？，,：:;；")
    upper = normalized.upper()
    if upper in _SILENT_REPLY_MARKERS:
        return True
    return any(marker in upper and len(upper) <= len(marker) + 8 for marker in _SILENT_REPLY_MARKERS)


def _should_silence_generated_reply(reply: str) -> tuple[bool, str]:
    text = (reply or "").strip()
    if not text:
        return True, "empty"
    if _is_silent_marker_reply(text):
        return True, "silent_marker"

    compact = re.sub(r"\s+", "", text)
    if len(compact) <= 24 and _SHORT_UNCERTAIN_REPLY_RE.match(text):
        return True, "uncertain_short_reply"
    return False, ""


def _truncate_text(text: str, max_len: int) -> str:
    cleaned = (text or "").replace("\n", " ").strip()
    if len(cleaned) <= max_len:
        return cleaned
    return cleaned[:max_len] + "..."


def _build_moderation_notice(
    warn_target: str,
    reason: str,
    rule: ModerationRule | None,
    *,
    hit_action: str,
    count: int | None = None,
    threshold: int | None = None,
    should_ban: bool = False,
    failure_note: str = "",
) -> str:
    rule_type_labels = {
        "keyword": "关键词",
        "regex": "正则",
        "llm": "语义",
    }
    reason_text = html.escape((reason or "命中群审核规则（AI判定）").strip())
    action_norm = (hit_action or "").strip().lower()

    title = "内容审核 · 已警告"
    action_result = "已发出警告"
    show_warning_count = False
    if action_norm == "delete":
        title = "内容审核 · 已删除"
        action_result = "已删除违规消息"
    elif action_norm == "ban":
        title = "内容审核 · 已封禁" if should_ban else "内容审核 · 已处理"
        action_result = "已删除违规消息并封禁用户" if should_ban else "已删除违规消息并发出警告"
        show_warning_count = True

    if rule:
        rule_type = rule_type_labels.get((rule.rule_type or "").lower(), rule.rule_type or "未知")
        pattern_preview = _truncate_text(rule.pattern or "", 60)
        if pattern_preview:
            rule_ref = f"#{rule.id}（{html.escape(rule_type)}） {html.escape(pattern_preview)}"
        else:
            rule_ref = f"#{rule.id}（{html.escape(rule_type)}）"
    else:
        rule_ref = "未定位具体规则（AI语义判定）"

    summary = [
        card_field("用户", warn_target),
        card_field("处理结果", action_result),
    ]
    details: list[str] = []
    if show_warning_count and count is not None and threshold is not None:
        details.append(card_field("警告次数", f"<code>{count}/{threshold}</code>"))
    details.extend(
        [
            card_field("原因", reason_text),
            card_field("依据规则", rule_ref),
        ]
    )
    clean_failure_note = str(failure_note or "").strip()
    return render_summary_notice(
        title,
        summary,
        emphasis=(f"<b>{html.escape(clean_failure_note)}</b>" if clean_failure_note else None),
        details=details,
    )


_MODERATION_OUTCOME_LABELS = {
    "ban": "手动封禁",
    "undo": "确认误封",
    "exempt": "永久豁免",
}
# Matches the "处理结果" line inside a rendered notice (message.html_text keeps
# the <b> entity and the surrounding <blockquote>). The value is separated by a
# full-width space and never contains a raw "<", so stopping at "<" keeps the
# closing </blockquote> tag intact. The legacy ": " separator is still accepted
# so a notice posted just before this style change is rewritten in place rather
# than gaining a duplicate line. "处理结果" is not always the last line — a
# failed-ban notice appends a ⚠️ warning after the card — so rewrite only it.
_MODERATION_RESULT_LINE_RE = re.compile(r"(<b>处理结果</b>(?:　|: ))[^\n<]*")


async def _apply_moderation_outcome_notice(
    callback: CallbackQuery,
    settings: Settings,
    *,
    outcome: str,
) -> bool:
    """Finalize the notice after a manual intervention: rewrite the "处理结果"
    line to name the operator and action, and drop the action buttons.

    Mirrors the join-verification admin outcome (``_edit_verification_prompt``):
    edit the message in place, remove the keyboard, and re-apply the group's
    "moderation" auto-delete retention. Best-effort — never raises. Returns
    whether the rewrite happened so the caller can fall back to a plain
    answer when the notice is inaccessible or already deleted.
    """
    verb = _MODERATION_OUTCOME_LABELS.get(outcome)
    message = callback.message
    chat = getattr(message, "chat", None)
    operator = callback.from_user
    current = getattr(message, "html_text", None)
    if (
        verb is None
        or message is None
        or chat is None
        or operator is None
        or not isinstance(current, str)
        or not current.strip()
    ):
        return False

    mention = _build_user_mention(
        user_id=int(getattr(operator, "id", 0) or 0),
        username=str(getattr(operator, "username", "") or ""),
        full_name=str(getattr(operator, "full_name", "") or ""),
    )
    # Sanitize like the send path so the operator handle renders the same way
    # as the "用户" line (monospace @handle / stripped profile link).
    body = sanitize_outgoing_mentions(f"已被 {mention} {verb}")
    # group(1) already carries the label separator (full-width space or the
    # legacy ": "), so append the body directly without inserting another gap.
    new_text, replaced = _MODERATION_RESULT_LINE_RE.subn(
        lambda m: f"{m.group(1)}{body}", current, count=1
    )
    if replaced == 0:
        # Notice format changed unexpectedly: append the outcome instead of
        # silently dropping it.
        new_text = f"{current.rstrip()}\n" + sanitize_outgoing_mentions(
            card_field("处理结果", f"已被 {mention} {verb}")
        )

    try:
        edited = await callback.bot.edit_message_text(
            chat_id=chat.id,
            message_id=message.message_id,
            text=new_text,
            parse_mode="HTML",
            reply_markup=None,
        )
        # The card is now a moderation outcome notice ("审核通知"): honor the
        # group's auto-delete retention like the verification outcomes do.
        await schedule_message_auto_delete_durable(
            edited if not isinstance(edited, bool) else None,
            configured_auto_delete_seconds(settings, "moderation"),
        )
    except Exception:
        log.debug(
            "moderation outcome notice edit failed | group=%s message=%s",
            getattr(chat, "id", "?"),
            getattr(message, "message_id", "?"),
            exc_info=True,
        )
        return False
    return True


async def _screen_bot_sender_message(
    *,
    moderation: ModerationService,
    session: AsyncSession,
    settings: Settings,
    message: Message,
    group_id: int,
    bot_id: int,
    input_text: str,
    warn_target: str,
    flow_started: float,
) -> None:
    """Moderate a message from another bot until it earns the whitelist.

    Bots cannot complete the human verification challenge, so any conclusive
    violation deletes the message and resets the clean-message counter; a
    high-confidence hit on a ban rule also bans the bot. Clean conclusive
    messages count toward the per-group whitelist threshold.
    """
    if await moderation.is_user_exempt(session, group_id, bot_id):
        log.info("[%s] bot screening skipped | reason=manual_exempt bot=%s", group_id, bot_id)
        return

    # Admin bots (e.g. other management bots) get the same auto-exemption as
    # human admins. Not cached as a whitelist so a demotion re-screens them.
    if await _is_user_admin_cached(message):
        log.info("[%s] bot screening skipped | reason=tg_admin_bot bot=%s", group_id, bot_id)
        return

    # With no enabled rules every message would trivially "pass" and grant a
    # permanent whitelist before any real screening happened. Keep the counter
    # frozen until the group actually has rules.
    rules_stmt = select(ModerationRule.id).where(
        ModerationRule.group_id == group_id,
        ModerationRule.enabled == True,  # noqa: E712
    ).limit(1)
    with session.no_autoflush:
        rules_result = await session.execute(rules_stmt)
    if rules_result.scalar_one_or_none() is None:
        log.info("[%s] bot screening skipped | reason=no_enabled_rules bot=%s", group_id, bot_id)
        return

    threshold = max(1, int(settings.moderation.bot_screening_message_count))
    moderation_started = time.perf_counter()
    verdict = await moderation.evaluate(session, group_id, input_text)
    log.info(
        "[%s]【流程】bot审核 | 完成 | bot=%s | 违规=%s | 置信度=%.2f | 原因=%s | 耗时=%dms",
        group_id,
        bot_id,
        verdict.violated,
        verdict.confidence,
        verdict.reason,
        int((time.perf_counter() - moderation_started) * 1000),
    )
    if not await _fresh_group_authorized_for_moderation(session, group_id):
        return

    if verdict.conclusive and not await _claim_current_moderation_verdict(
        session,
        group_id=group_id,
        user_id=bot_id,
        verdict=verdict,
    ):
        log.info(
            "[%s] bot screening verdict discarded | reason=policy_changed bot=%s",
            group_id,
            bot_id,
        )
        return

    if not verdict.violated:
        if not verdict.conclusive:
            # Unparseable moderation output: neither punish nor credit a pass.
            return
        count, whitelisted = await record_bot_screening_pass(
            session, group_id, bot_id, threshold
        )
        await session.commit()
        log.info(
            "[%s] bot screening pass | bot=%s count=%d/%d whitelisted=%s",
            group_id,
            bot_id,
            count,
            threshold,
            whitelisted,
        )
        return

    if not verdict.conclusive:
        log.warning(
            "[%s] inconclusive bot moderation confidence; no direct action | bot=%s",
            group_id,
            bot_id,
        )
        return

    rule = verdict.rule
    rule_action = str(rule.action if rule else "delete").strip().lower()
    ban_now = rule_action == "ban" and moderation.is_high_confidence(verdict)

    await reset_bot_screening(session, group_id, bot_id)
    # Never wait for an application lock while holding SQLite's process-wide
    # writer lock.  Other moderation paths take these resources in the
    # opposite order, so publishing this small reset first removes the lock
    # inversion and keeps privileged database work moving.
    await session.commit()

    if ban_now:
        async with _moderation_user_lock(group_id, bot_id):
            if not await _claim_current_moderation_verdict(
                session,
                group_id=group_id,
                user_id=bot_id,
                verdict=verdict,
            ):
                return
            violation = await moderation.record_violation(
                session,
                group_id,
                bot_id,
                input_text,
                "bot_ban",
                rule,
                source_message_id=_source_message_id(message),
            )
            await session.flush()
            prior_ban_result = _violation_nullable_bool(
                violation,
                "ban_enforced",
            )
            ban_retryable = False
            ban_terminal_failure = False
            if prior_ban_result is not True:
                # Persist the event and compensation journal before Telegram.
                # If the response is lost, recovery can safely unban.
                warning = await session.scalar(
                    select(UserWarning).where(
                        UserWarning.group_id == group_id,
                        UserWarning.user_id == bot_id,
                    )
                )
                if warning is None:
                    session.add(
                        UserWarning(
                            group_id=group_id,
                            user_id=bot_id,
                            count=0,
                            is_banned=True,
                        )
                    )
                else:
                    warning.is_banned = True
                recovery = await lease_join_verification_for_unban(
                    session,
                    group_id,
                    bot_id,
                    manual_unban=False,
                )
                if recovery is None:
                    await session.rollback()
                    return
                await session.commit()
                try:
                    await message.delete()
                except Exception:
                    log.warning(
                        "[%s] bot message delete failed | bot=%s",
                        group_id,
                        bot_id,
                    )
                async def preserve_latest_ban() -> bool:
                    await session.rollback()
                    blocked = await verification_release_blocked_by_ban(
                        session,
                        group_id=group_id,
                        user_id=bot_id,
                    )
                    await session.commit()
                    return bool(blocked)

                async def preserve_latest_restriction() -> bool:
                    await session.rollback()
                    required = await verification_restriction_required(
                        session,
                        group_id=group_id,
                        user_id=bot_id,
                    )
                    await session.commit()
                    return bool(required)

                enforcement = await enforce_ban_with_policy_reconciliation_result(
                    message.bot,
                    group_id,
                    bot_id,
                    preserve_latest_ban,
                    restriction_required=preserve_latest_restriction,
                )
                attempted_ban = enforcement.final_banned is True
                ban_retryable = enforcement.retryable
                ban_terminal_failure = (
                    enforcement.final_banned is None and not enforcement.retryable
                )
                ban_policy_released = enforcement.final_banned is False
                if attempted_ban:
                    await session.rollback()
                    claimed = await complete_leased_join_verification(
                        session,
                        verification_id=int(recovery.verification_id),
                        lease_until=recovery.lease_until,
                        status="unbanning",
                    )
                    if not claimed:
                        await session.rollback()
                        attempted_ban = False
                        enforcement = (
                            await enforce_ban_with_policy_reconciliation_result(
                                message.bot,
                                group_id,
                                bot_id,
                                preserve_latest_ban,
                                restriction_required=preserve_latest_restriction,
                            )
                        )
                        attempted_ban = enforcement.final_banned is True
                        ban_retryable = enforcement.retryable
                        ban_terminal_failure = (
                            enforcement.final_banned is None
                            and not enforcement.retryable
                        )
                        ban_policy_released = enforcement.final_banned is False
                if ban_policy_released:
                    ban_enforced, result_changed = False, False
                else:
                    ban_enforced, result_changed = await _persist_violation_ban_result(
                        session,
                        violation,
                        enforced=attempted_ban,
                    )
                violation.action_taken = (
                    "bot_ban" if ban_enforced else "bot_delete"
                )
                try:
                    if result_changed and callable(getattr(session, "scalar", None)) and callable(
                        getattr(session, "add", None)
                    ):
                        if ban_enforced:
                            warning = await session.scalar(
                                select(UserWarning).where(
                                    UserWarning.group_id == group_id,
                                    UserWarning.user_id == bot_id,
                                )
                            )
                            if warning is None:
                                session.add(
                                    UserWarning(
                                        group_id=group_id,
                                        user_id=bot_id,
                                        count=0,
                                        is_banned=True,
                                    )
                                )
                            else:
                                warning.is_banned = True
                        await record_ban_event(
                            session,
                            group_id=group_id,
                            target_user_id=bot_id,
                            target_display=warn_target,
                            action="ban",
                            source="bot_screening",
                            outcome="succeeded" if ban_enforced else "pending",
                            reason=(
                                verdict.reason
                                or "Bot 消息命中高置信度封禁规则"
                            ),
                            evidence=input_text,
                            reference_type="violation",
                            reference_id=int(violation.id),
                            details={
                                "rule_id": int(rule.id) if rule is not None else 0
                            },
                        )
                    await session.commit()
                except Exception:
                    await session.rollback()
                    ban_retryable = True
                    log.exception(
                        "bot-screening ban state/audit write failed | group=%s bot=%s",
                        group_id,
                        bot_id,
                    )
            else:
                ban_enforced = bool(prior_ban_result)
                await session.commit()
            notice = _build_moderation_notice(
                warn_target=warn_target,
                reason=verdict.reason,
                rule=rule,
                hit_action="ban" if ban_enforced else "delete",
                should_ban=ban_enforced,
                failure_note=(
                    "⚠️ Telegram 封禁该 bot 失败，请管理员手动处理。"
                    if not ban_enforced and (ban_retryable or ban_terminal_failure)
                    else ""
                ),
            )
            await _send_moderation_notice_once_locked(
                session=session,
                violation=violation,
                message=message,
                notice=notice,
                auto_delete_seconds=configured_auto_delete_seconds(
                    settings,
                    "moderation",
                ),
            )
            if ban_retryable:
                request_current_update_retry()
        log.info(
            "[%s]【结束】bot审核拦截 | bot=%s | 动作=ban | 封禁=%s | 总耗时=%dms",
            group_id,
            bot_id,
            ban_enforced,
            int((time.perf_counter() - flow_started) * 1000),
        )
        return

    # Non-immediate violations go through the counted warning path, so a
    # flooding ad bot is auto-banned after warn_threshold hits instead of
    # producing an unbounded delete/notice loop.
    warn_threshold = max(1, int(settings.moderation.warn_threshold))
    counted_outcome = await _apply_counted_moderation_ban(
            moderation=moderation,
            session=session,
            message=message,
            group_id=group_id,
            user_id=bot_id,
            input_text=input_text,
            rule=rule,
            message_deleted=False,
            verdict=verdict,
        )
    if counted_outcome is None:
        return
    count, violation, ban_enforced, ban_failure_note, ban_retryable = counted_outcome
    violation_id = int(violation.id)
    notice = _build_moderation_notice(
        warn_target=warn_target,
        reason=verdict.reason,
        rule=rule,
        hit_action="ban",
        count=count,
        threshold=warn_threshold,
        should_ban=ban_enforced,
        failure_note=ban_failure_note,
    )
    async with _moderation_user_lock(group_id, bot_id):
        await _send_moderation_notice_once_locked(
            session=session,
            violation=violation,
            message=message,
            notice=notice,
            auto_delete_seconds=configured_auto_delete_seconds(
                settings,
                "moderation",
            ),
            reply_markup=_build_moderation_action_keyboard(violation_id),
        )
    if ban_retryable:
        request_current_update_retry()
    log.info(
        "[%s]【结束】bot审核拦截 | bot=%s | 动作=%s | 封禁=%s | 警告=%s/%s | 总耗时=%dms",
        group_id,
        bot_id,
        rule_action,
        ban_enforced,
        count,
        warn_threshold,
        int((time.perf_counter() - flow_started) * 1000),
    )


def _build_moderation_action_keyboard(violation_id: int) -> InlineKeyboardMarkup:
    prefix = _MODERATION_ACTION_CALLBACK_PREFIX
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="直接封禁",
                    callback_data=f"{prefix}:ban:{violation_id}",
                ),
                InlineKeyboardButton(
                    text="误封解除",
                    callback_data=f"{prefix}:undo:{violation_id}",
                ),
                InlineKeyboardButton(
                    text="永久豁免",
                    callback_data=f"{prefix}:exempt:{violation_id}",
                ),
            ]
        ]
    )


def _parse_moderation_action_state(value: str) -> tuple[str, set[str]]:
    base = (value or "").strip().lower()
    markers: set[str] = set()
    while base:
        matched = False
        for marker in _MODERATION_ACTION_MARKERS:
            suffix = f"_{marker}"
            if base.endswith(suffix):
                markers.add(marker)
                base = base[: -len(suffix)]
                matched = True
                break
        if not matched:
            break
    return base, markers


async def _append_violation_action_marker(
    session: AsyncSession,
    *,
    violation: Violation,
    current_state: str,
    marker: str,
) -> str | None:
    new_state = f"{current_state}_{marker}"
    result = await session.execute(
        update(Violation)
        .where(
            Violation.id == violation.id,
            Violation.group_id == violation.group_id,
            Violation.action_taken == current_state,
        )
        .values(action_taken=new_state)
    )
    return new_state if int(result.rowcount or 0) == 1 else None


async def _rollback_failed_automatic_ban(
    session: AsyncSession,
    *,
    group_id: int,
    user_id: int,
    violation_id: int,
    warning_count: int,
) -> bool:
    violation_rollback = await session.execute(
        update(Violation)
        .where(
            Violation.id == violation_id,
            Violation.group_id == group_id,
            Violation.action_taken == "ban_applied",
        )
        .values(action_taken="ban_warning")
    )
    warning_rollback = await session.execute(
        update(UserWarning)
        .where(
            UserWarning.group_id == group_id,
            UserWarning.user_id == user_id,
            UserWarning.count == warning_count,
            UserWarning.is_banned.is_(True),
        )
        .values(is_banned=False)
    )
    rollback_clean = (
        int(violation_rollback.rowcount or 0) == 1
        and int(warning_rollback.rowcount or 0) == 1
    )
    if not rollback_clean:
        await session.rollback()
        return False
    await session.commit()
    return True


async def _apply_counted_moderation_ban(
    *,
    moderation: ModerationService,
    session: AsyncSession,
    message: Message,
    group_id: int,
    user_id: int,
    input_text: str,
    rule: ModerationRule | None,
    message_deleted: bool,
    verdict: ModerationVerdict | None = None,
) -> tuple[int, Violation, bool, str, bool] | None:
    async with _moderation_user_lock(group_id, user_id):
        if verdict is not None and not await _claim_current_moderation_verdict(
            session,
            group_id=group_id,
            user_id=user_id,
            verdict=verdict,
        ):
            return None
        recovery: UnbanRecovery | None = None
        source_message_id = _source_message_id(message)
        if source_message_id is None:
            # Compatibility for synthetic/manual Message objects that have no
            # Telegram id and therefore cannot participate in durable replay.
            count, should_ban = await moderation.add_warning(
                session,
                group_id,
                user_id,
            )
            violation = await moderation.record_violation(
                session,
                group_id,
                user_id,
                input_text,
                "ban_applied" if should_ban else "ban_warning",
                rule,
            )
            violation.warning_count = int(count)
            violation.action_taken = "ban_applied" if should_ban else "ban_warning"
            await session.flush()
            if should_ban:
                recovery = await lease_join_verification_for_unban(
                    session,
                    group_id,
                    user_id,
                    manual_unban=False,
                )
                if recovery is None:
                    await session.rollback()
                    return None
            await session.commit()
        else:
            violation = await moderation.record_violation(
                session,
                group_id,
                user_id,
                input_text,
                "ban_warning",
                rule,
                source_message_id=source_message_id,
            )
            created = _violation_event_created(violation)
            stored_count = getattr(violation, "warning_count", None)
            if created or stored_count is None:
                count, should_ban = await moderation.add_warning(
                    session,
                    group_id,
                    user_id,
                )
                violation.warning_count = int(count)
                violation.action_taken = (
                    "ban_applied" if should_ban else "ban_warning"
                )
                await session.flush()
                if should_ban:
                    recovery = await lease_join_verification_for_unban(
                        session,
                        group_id,
                        user_id,
                        manual_unban=False,
                    )
                    if recovery is None:
                        await session.rollback()
                        return None
                await session.commit()
            else:
                count = max(0, int(stored_count))
                action_base, _ = _parse_moderation_action_state(
                    str(getattr(violation, "action_taken", "") or "")
                )
                should_ban = action_base == "ban_applied"
                if should_ban and _violation_nullable_bool(
                    violation,
                    "ban_enforced",
                ) is not True:
                    recovery = await lease_join_verification_for_unban(
                        session,
                        group_id,
                        user_id,
                        manual_unban=False,
                    )
                    if recovery is None:
                        await session.rollback()
                        return None
                # The conflict-safe insert starts a transaction even on reuse.
                # Release it before Telegram calls.
                await session.commit()

        violation_id = int(violation.id)

        try:
            if not message_deleted:
                await message.delete()
        except Exception:
            pass

        prior_ban_result = _violation_nullable_bool(violation, "ban_enforced")
        ban_enforced = bool(prior_ban_result) if prior_ban_result is not None else False
        ban_failure_note = ""
        ban_retryable = False
        if should_ban and prior_ban_result is not True:
            async def preserve_latest_ban() -> bool:
                await session.rollback()
                blocked = await verification_release_blocked_by_ban(
                    session,
                    group_id=group_id,
                    user_id=user_id,
                )
                await session.commit()
                return bool(blocked)

            async def preserve_latest_restriction() -> bool:
                await session.rollback()
                required = await verification_restriction_required(
                    session,
                    group_id=group_id,
                    user_id=user_id,
                )
                await session.commit()
                return bool(required)

            enforcement = await enforce_ban_with_policy_reconciliation_result(
                message.bot,
                group_id,
                user_id,
                preserve_latest_ban,
                restriction_required=preserve_latest_restriction,
            )
            attempted_ban = enforcement.final_banned is True
            failure_retryable = enforcement.retryable
            failure_group_unreachable = enforcement.group_unreachable
            failure_operator_action = enforcement.operator_action_required
            failure_present = enforcement.final_banned is None
            ban_policy_released = enforcement.final_banned is False
            if attempted_ban and recovery is not None:
                await session.rollback()
                claimed = await complete_leased_join_verification(
                    session,
                    verification_id=int(recovery.verification_id),
                    lease_until=recovery.lease_until,
                    status="unbanning",
                )
                if not claimed:
                    await session.rollback()
                    attempted_ban = False
                    enforcement = (
                        await enforce_ban_with_policy_reconciliation_result(
                            message.bot,
                            group_id,
                            user_id,
                            preserve_latest_ban,
                            restriction_required=preserve_latest_restriction,
                        )
                    )
                    attempted_ban = enforcement.final_banned is True
                    failure_retryable = enforcement.retryable
                    failure_group_unreachable = enforcement.group_unreachable
                    failure_operator_action = enforcement.operator_action_required
                    failure_present = enforcement.final_banned is None
                    ban_policy_released = enforcement.final_banned is False
            if ban_policy_released:
                ban_enforced, result_changed = False, False
            else:
                ban_enforced, result_changed = await _persist_violation_ban_result(
                    session,
                    violation,
                    enforced=attempted_ban,
                )
            if not ban_enforced and failure_present:
                log.error(
                    "[%s] moderation automatic ban unconfirmed; durable policy retained | "
                    "user=%s violation=%s",
                    group_id,
                    user_id,
                    violation_id,
                )
                if failure_group_unreachable:
                    ban_failure_note = (
                        "\n⚠️ Telegram 群不可达或 bot 已不在群中，请检查群授权和成员状态。"
                    )
                elif failure_operator_action:
                    ban_failure_note = (
                        "\n⚠️ Telegram 拒绝了自动封禁（bot 缺少「封禁用户」权限或"
                        "对方是管理员），请管理员修正后手动处理。"
                    )
                elif failure_retryable:
                    ban_retryable = True
                    ban_failure_note = (
                        "\n⚠️ Telegram 自动封禁结果未确认，已保留封禁状态等待重试。"
                    )
            try:
                if result_changed and callable(getattr(session, "add", None)):
                    await record_ban_event(
                        session,
                        group_id=group_id,
                        target_user_id=user_id,
                        action="ban",
                        source="moderation_threshold",
                        outcome="succeeded" if ban_enforced else "pending",
                        reason="审核警告次数达到自动封禁阈值",
                        evidence=input_text,
                        reference_type="violation",
                        reference_id=violation_id,
                        details={
                            "warning_count": int(count),
                            "rule_id": int(rule.id) if rule is not None else 0,
                        },
                    )
                # Commit the audit fact and the completion marker together.
                # Once this succeeds, a durable update retry performs neither
                # another Telegram ban nor another audit append.
                await session.commit()
            except Exception:
                await session.rollback()
                ban_retryable = True
                log.exception(
                    "moderation ban audit failed | group=%s user=%s violation=%s",
                    group_id,
                    user_id,
                    violation_id,
                )
        elif should_ban and not ban_enforced:
            ban_retryable = True
            ban_failure_note = (
                "\n⚠️ Telegram 自动封禁结果未确认，已保留封禁状态等待重试。"
            )
        return count, violation, ban_enforced, ban_failure_note, ban_retryable


async def _moderation_direct_ban(
    callback: CallbackQuery,
    session: AsyncSession,
    settings: Settings,
    violation: Violation,
    current_state: str,
    markers: set[str],
) -> str | None:
    violation_id = int(violation.id)
    group_id = int(violation.group_id)
    target_id = int(violation.user_id)
    violation_evidence = str(violation.message_text or "")
    if is_super_admin_user_id(target_id, settings):
        await callback.answer("不能封禁最高管理员", show_alert=True)
        return
    if await is_globally_banned(session, target_id):
        await callback.answer("该用户已处于全局封禁，需由最高管理员处理", show_alert=True)
        return
    prior_ban_result = _violation_nullable_bool(violation, "ban_enforced")
    if "direct" in markers and prior_ban_result is True:
        await callback.answer("该事件已执行过直接封禁", show_alert=True)
        return

    if "direct" not in markers:
        claimed_state = await _append_violation_action_marker(
            session,
            violation=violation,
            current_state=current_state,
            marker="direct",
        )
        if claimed_state is None:
            await session.rollback()
            await callback.answer("事件状态已变化，请重新点击", show_alert=True)
            return

        result = await session.execute(
            select(UserWarning).where(
                UserWarning.group_id == group_id,
                UserWarning.user_id == target_id,
            )
        )
        warning = result.scalar_one_or_none()
        previous_warning = (
            (max(0, int(warning.count or 0)), bool(warning.is_banned))
            if warning is not None
            else None
        )
        if warning is None:
            warning = UserWarning(
                group_id=group_id,
                user_id=target_id,
                count=0,
                is_banned=True,
            )
            session.add(warning)
        else:
            warning_lock = await session.execute(
                update(UserWarning)
                .where(
                    UserWarning.id == warning.id,
                    UserWarning.count == previous_warning[0],
                    UserWarning.is_banned.is_(previous_warning[1]),
                )
                .values(is_banned=True)
            )
            if int(warning_lock.rowcount or 0) != 1:
                await session.rollback()
                await callback.answer("警告状态已变化，请重新点击", show_alert=True)
                return

        recovery = await lease_join_verification_for_unban(
            session,
            group_id,
            target_id,
            manual_unban=False,
        )
        if recovery is None:
            await session.rollback()
            await callback.answer("无法建立封禁恢复工单，请重试", show_alert=True)
            return "retry"
        try:
            await session.commit()
        except Exception:
            await session.rollback()
            raise
    else:
        # A previous Telegram attempt was unconfirmed. The durable local policy
        # and direct marker already exist; release this read before retrying.
        recovery = await lease_join_verification_for_unban(
            session,
            group_id,
            target_id,
            manual_unban=False,
        )
        if recovery is None:
            await session.rollback()
            await callback.answer("无法建立封禁恢复工单，请重试", show_alert=True)
            return "retry"
        await session.commit()

    async def preserve_latest_ban() -> bool:
        await session.rollback()
        blocked = await verification_release_blocked_by_ban(
            session,
            group_id=group_id,
            user_id=target_id,
        )
        await session.commit()
        return bool(blocked)

    async def preserve_latest_restriction() -> bool:
        await session.rollback()
        required = await verification_restriction_required(
            session,
            group_id=group_id,
            user_id=target_id,
        )
        await session.commit()
        return bool(required)

    enforcement = await enforce_ban_with_policy_reconciliation_result(
        callback.bot,
        group_id,
        target_id,
        preserve_latest_ban,
        restriction_required=preserve_latest_restriction,
    )
    attempted_ban = enforcement.final_banned is True
    failure_present = enforcement.final_banned is None
    failure_retryable = enforcement.retryable
    failure_group_unreachable = enforcement.group_unreachable
    failure_operator_action = enforcement.operator_action_required
    ban_policy_released = enforcement.final_banned is False
    if attempted_ban:
        await session.rollback()
        claimed = await complete_leased_join_verification(
            session,
            verification_id=int(recovery.verification_id),
            lease_until=recovery.lease_until,
            status="unbanning",
        )
        if claimed:
            await close_private_challenge_message(
                callback.bot,
                target_id,
                int(getattr(recovery, "private_message_id", 0) or 0),
            )
        if not claimed:
            await session.rollback()
            attempted_ban = False
            enforcement = await enforce_ban_with_policy_reconciliation_result(
                callback.bot,
                group_id,
                target_id,
                preserve_latest_ban,
                restriction_required=preserve_latest_restriction,
            )
            attempted_ban = enforcement.final_banned is True
            failure_present = enforcement.final_banned is None
            failure_retryable = enforcement.retryable
            failure_group_unreachable = enforcement.group_unreachable
            failure_operator_action = enforcement.operator_action_required
            ban_policy_released = enforcement.final_banned is False
    if ban_policy_released:
        ban_enforced, result_changed = False, False
    else:
        ban_enforced, result_changed = await _persist_violation_ban_result(
            session,
            violation,
            enforced=attempted_ban,
        )
    if not ban_enforced:
        # The local policy was committed before Telegram. A timeout plus failed
        # status confirmation is ambiguous, so rolling it back could strand a
        # remotely banned user with no durable record. Keep the intent instead.
        log.error(
            "moderation direct ban unconfirmed; durable policy retained | "
            "group=%s user=%s violation=%s",
            group_id,
            target_id,
            violation_id,
        )
        try:
            if result_changed:
                operator = callback.from_user
                await record_ban_event(
                    session,
                    group_id=group_id,
                    target_user_id=target_id,
                    action="ban",
                    source="moderation_direct",
                    outcome="pending",
                    reason="管理员从审核通知执行直接封禁",
                    evidence=violation_evidence,
                    actor_user_id=int(getattr(operator, "id", 0) or 0),
                    actor_display=str(getattr(operator, "full_name", "") or ""),
                    reference_type="violation",
                    reference_id=violation_id,
                )
            await session.commit()
        except Exception:
            await session.rollback()
            log.exception("moderation direct-ban failure audit write failed")
            request_current_update_retry()
            await callback.answer(
                "封禁结果保存失败，已安排后台重试",
                show_alert=True,
            )
            return "retry"
        if not failure_present:
            await callback.answer(
                "封禁状态已由更新的策略接管，无需重试",
                show_alert=True,
            )
        elif failure_group_unreachable:
            await callback.answer(
                "Telegram 群不可达或 bot 已不在群中，请检查群授权后重试",
                show_alert=True,
            )
        elif failure_operator_action:
            await callback.answer(
                "Telegram 拒绝了封禁：bot 缺少「封禁用户」权限或对方是管理员，"
                "请修正后重试",
                show_alert=True,
            )
        elif failure_retryable:
            request_current_update_retry()
            await callback.answer(
                "Telegram 封禁结果未确认，已保留本群封禁状态等待重试",
                show_alert=True,
            )
            return "retry"
        else:
            await callback.answer("Telegram 封禁未生效，请管理员手动处理", show_alert=True)
        return

    try:
        operator = callback.from_user
        if result_changed:
            await record_ban_event(
                session,
                group_id=group_id,
                target_user_id=target_id,
                action="ban",
                source="moderation_direct",
                outcome="succeeded",
                reason="管理员从审核通知执行直接封禁",
                evidence=violation_evidence,
                actor_user_id=int(getattr(operator, "id", 0) or 0),
                actor_display=str(getattr(operator, "full_name", "") or ""),
                reference_type="violation",
                reference_id=violation_id,
            )
        await session.commit()
    except Exception:
        await session.rollback()
        log.exception(
            "moderation direct ban persistence failed | group=%s user=%s violation=%s",
            group_id,
            target_id,
            violation_id,
        )
        request_current_update_retry()
        await callback.answer("用户已被 Telegram 封禁，但状态保存失败", show_alert=True)
        return "retry"

    # The rewritten notice ("已被 … 手动封禁") is the group-visible feedback;
    # through the detached proxy a text answer would post a duplicate message.
    await callback.answer()
    return "ban"


async def _moderation_undo_false_positive(
    callback: CallbackQuery,
    session: AsyncSession,
    settings: Settings,
    violation: Violation,
    *,
    base_state: str,
    current_state: str,
    markers: set[str],
) -> str | None:
    if "reverted" in markers:
        await callback.answer("该事件已撤销，无需重复操作", show_alert=True)
        return None
    violation_id = int(violation.id)

    if base_state == "warn":
        claimed_state = await _append_violation_action_marker(
            session,
            violation=violation,
            current_state=current_state,
            marker="reverted",
        )
        if claimed_state is None:
            await session.rollback()
            await callback.answer("事件状态已变化，请重新点击", show_alert=True)
            return None
        await session.commit()
        await callback.answer("已撤销本次误判；该警告未产生封禁计数", show_alert=True)
        return "undo"

    group_id = int(violation.group_id)
    target_id = int(violation.user_id)
    result = await session.execute(
        select(UserWarning).where(
            UserWarning.group_id == group_id,
            UserWarning.user_id == target_id,
        )
    )
    warning = result.scalar_one_or_none()
    previous_count = max(0, int(warning.count or 0)) if warning else 0
    previous_banned = bool(warning.is_banned) if warning else False
    if warning is not None:
        warning_lock = await session.execute(
            update(UserWarning)
            .where(
                UserWarning.id == warning.id,
                UserWarning.count == previous_count,
                UserWarning.is_banned.is_(previous_banned),
            )
            .values(count=previous_count)
        )
        if int(warning_lock.rowcount or 0) != 1:
            await session.rollback()
            await callback.answer("警告状态已变化，请重新点击", show_alert=True)
            return None

    states_result = await session.execute(
        select(Violation.id, Violation.action_taken)
        .where(
            Violation.group_id == group_id,
            Violation.user_id == target_id,
        )
        .order_by(Violation.id.desc())
    )
    states = list(states_result.all())
    active_counted_ids: list[int] = []
    for row_id, state in states:
        row_base, row_markers = _parse_moderation_action_state(str(state or ""))
        if "reverted" in row_markers or row_base not in {"ban_warning", "ban_applied"}:
            continue
        active_counted_ids.append(int(row_id))
        if len(active_counted_ids) >= previous_count:
            break

    belongs_to_current_generation = bool(
        previous_count > 0
        and len(active_counted_ids) == previous_count
        and violation_id in active_counted_ids
    )
    claimed_state = await _append_violation_action_marker(
        session,
        violation=violation,
        current_state=current_state,
        marker="reverted",
    )
    if claimed_state is None:
        await session.rollback()
        await callback.answer("事件状态已变化，请重新点击", show_alert=True)
        return None
    if not belongs_to_current_generation:
        await session.commit()
        await callback.answer(
            "该事件已不属于当前警告周期，未修改现有计数",
            show_alert=True,
        )
        return "undo"

    new_count = max(0, previous_count - 1)

    # A deliberate direct ban may be recorded on any of the user's violation
    # rows in the active warning generation. Older rows must not keep a user
    # banned after /clearwarnings or a manual unban started a new generation.
    generation_floor = min(active_counted_ids)
    has_direct_ban_marker = any(
        int(row_id) >= generation_floor
        and "direct" in row_markers
        and "reverted" not in row_markers
        for row_id, state in states
        for _row_base, row_markers in [_parse_moderation_action_state(str(state or ""))]
    )
    globally_banned = await is_globally_banned(session, target_id)
    threshold = max(1, int(settings.moderation.warn_threshold))
    should_restore = bool(
        base_state == "ban_applied"
        and previous_banned
        and new_count < threshold
        and not has_direct_ban_marker
        and not globally_banned
    )
    remaining_banned = previous_banned and not should_restore
    if new_count <= 0 and not remaining_banned:
        warning_update = await session.execute(
            delete(UserWarning).where(
                UserWarning.id == warning.id,
                UserWarning.count == previous_count,
                UserWarning.is_banned.is_(previous_banned),
            )
        )
    else:
        warning_update = await session.execute(
            update(UserWarning)
            .where(
                UserWarning.id == warning.id,
                UserWarning.count == previous_count,
                UserWarning.is_banned.is_(previous_banned),
            )
            .values(count=new_count, is_banned=remaining_banned)
        )
    if int(warning_update.rowcount or 0) != 1:
        await session.rollback()
        await callback.answer("警告状态已变化，请重新点击", show_alert=True)
        return None

    restored = False
    release_deferred = False
    release_blocked = False
    if should_restore:
        # Commit the false-positive rollback together with a durable unban
        # journal before touching Telegram. A crash at any later point is
        # completed by the sweeper, while a newly-created ban policy wins.
        recovery = await lease_join_verification_for_unban(
            session,
            group_id,
            target_id,
        )
        if recovery is None:
            await session.rollback()
            await callback.answer("无法建立撤销恢复工单，请稍后重试", show_alert=True)
            return "retry"
        try:
            await session.commit()
        except Exception:
            await session.rollback()
            log.exception(
                "moderation undo persistence failed | group=%s user=%s violation=%s",
                group_id,
                target_id,
                violation_id,
            )
            await callback.answer("撤销状态保存失败，请稍后重试", show_alert=True)
            return "retry"
        activate_manual_unban_recovery(recovery)

        async def preserve_ban() -> bool:
            await session.rollback()
            blocked = await verification_release_blocked_by_ban(
                session,
                group_id=group_id,
                user_id=target_id,
            )
            await session.commit()
            return blocked

        unbanned = await unban_member(
            callback.bot,
            group_id,
            target_id,
            preserve_ban=preserve_ban,
        )
        try:
            blocked = await preserve_ban()
        except Exception:
            blocked = False
            unbanned = False
            log.exception(
                "moderation undo post-unban policy check failed | group=%s user=%s",
                group_id,
                target_id,
            )
        release_blocked = blocked
        if unbanned and not blocked:
            restored = await restore_member_permissions(
                callback.bot,
                group_id,
                target_id,
            )
        release_deferred = not restored and not blocked
    else:
        try:
            await session.commit()
        except Exception:
            await session.rollback()
            log.exception(
                "moderation undo persistence failed | group=%s user=%s violation=%s",
                group_id,
                target_id,
                violation_id,
            )
            await callback.answer("撤销状态保存失败，请稍后重试", show_alert=True)
            return "retry"
    suffix = "，并已解除自动封禁" if restored else ""
    if release_deferred:
        suffix = "，解封已交由后台继续重试"
    if release_blocked:
        suffix = "；解封期间出现新的封禁政策，现有封禁保持不变"
    if (has_direct_ban_marker or globally_banned) and remaining_banned:
        suffix = "；现有封禁保持不变"
    await callback.answer(
        f"已撤销本次封禁计数，当前为 {new_count} 次{suffix}",
        show_alert=True,
    )
    return "undo"


async def _moderation_add_permanent_exemption(
    callback: CallbackQuery,
    session: AsyncSession,
    violation: Violation,
) -> str | None:
    operator_id = int(callback.from_user.id)
    result = await session.execute(
        select(ModerationExemption).where(
            ModerationExemption.group_id == violation.group_id,
            ModerationExemption.user_id == violation.user_id,
        )
    )
    existing = result.scalar_one_or_none()
    if existing is None:
        session.add(
            ModerationExemption(
                group_id=violation.group_id,
                user_id=violation.user_id,
                created_by=operator_id,
            )
        )
    recovery = await lease_join_verification_for_unban(
        session,
        int(violation.group_id),
        int(violation.user_id),
        manual_unban=False,
    )
    if recovery is None:
        await session.rollback()
        await callback.answer("无法建立豁免恢复工单，请重试", show_alert=True)
        return "retry"
    try:
        await session.commit()
    except IntegrityError:
        await session.rollback()
        await callback.answer("该用户已在当前群永久豁免 AI 审核", show_alert=True)
        return None
    activate_manual_unban_recovery(recovery)
    released = await release_moderation_restriction_after_exemption(
        callback.bot,
        session,
        recovery,
    )
    if existing is not None:
        text = "该用户已在当前群永久豁免 AI 审核"
    else:
        text = "已永久豁免该用户的当前群 AI 审核"
    if not released:
        text += "；旧限制正在由恢复任务继续校准"
        request_current_update_retry()
    await callback.answer(text, show_alert=True)
    return "exempt" if existing is None else None


@router.callback_query(F.data.startswith(f"{_MODERATION_ACTION_CALLBACK_PREFIX}:"))
async def on_moderation_action(
    callback: CallbackQuery,
    settings: Settings,
    session: AsyncSession | None = None,
    session_factory: async_sessionmaker[AsyncSession] | None = None,
    _outcome_sink: list[str | None] | None = None,
) -> None:
    if session is None:
        await callback.answer("会话未就绪，请等待下一次审核通知", show_alert=True)
        return
    if not callback.data:
        await callback.answer("操作参数无效", show_alert=True)
        return

    parts = callback.data.split(":")
    if len(parts) != 3 or parts[0] != _MODERATION_ACTION_CALLBACK_PREFIX:
        await callback.answer("操作参数无效", show_alert=True)
        return
    action = parts[1]
    if action not in {"ban", "undo", "exempt"}:
        await callback.answer("不支持的审核操作", show_alert=True)
        return
    try:
        violation_id = int(parts[2])
    except (TypeError, ValueError):
        violation_id = 0
    if violation_id <= 0:
        await callback.answer("审核事件无效", show_alert=True)
        return

    message = callback.message
    chat = getattr(message, "chat", None)
    user = callback.from_user
    if chat is None or getattr(chat, "type", "") not in {"group", "supergroup"}:
        await callback.answer("该操作只能在原群审核通知中执行", show_alert=True)
        return
    if user is None:
        await callback.answer("无法识别操作者", show_alert=True)
        return

    group_id = int(chat.id)
    if session_factory is not None:
        # Raw callback data is attacker-controlled. Keep authoritative
        # authorization in the HIGH update lane and only allocate a CRITICAL
        # job after it succeeds. Keep the callback unanswered until then so a
        # denial remains a private Telegram alert instead of a group message.
        if not await is_group_authorized(session, group_id):
            await session.commit()
            await callback.answer(
                "当前群组未授权，不能执行审核操作",
                show_alert=True,
            )
            return
        await session.commit()
        if not await is_group_admin_or_higher(
            bot=callback.bot,
            session=session,
            settings=settings,
            group_id=group_id,
            user_id=int(user.id),
        ):
            await callback.answer(
                "仅群管理员及以上权限可执行该操作",
                show_alert=True,
            )
            return
        in_transaction = getattr(session, "in_transaction", None)
        if callable(in_transaction) and in_transaction():
            await session.commit()
        target_hint = violation_id
        try:
            violation_hint = await session.get(Violation, violation_id)
            if violation_hint is not None and int(violation_hint.group_id) == group_id:
                target_hint = int(violation_hint.user_id)
        finally:
            await session.commit()

        deferred_callback = _DetachedCallbackProxy(callback)
        callback_acknowledged = asyncio.Event()

        async def operation() -> None:
            await callback_acknowledged.wait()
            outcomes: list[str | None] = []
            async with session_factory() as work_session:
                await on_moderation_action(
                    deferred_callback,  # type: ignore[arg-type]
                    settings,
                    session=work_session,
                    session_factory=None,
                    _outcome_sink=outcomes,
                )
                if outcomes and outcomes[-1] == "retry":
                    raise RuntimeError("moderation action requires durable retry")

        submission = submit_privileged_task(
            key=f"moderation-action:{group_id}:{target_hint}",
            label=f"moderation action {action} for {target_hint} in {group_id}",
            operation=operation,
            lane="critical",
            priority=0,
            timeout_seconds=120.0,
        )
        try:
            if not submission.accepted:
                request_current_update_retry()
                await callback.answer(
                    "审核任务队列正忙，本次操作会自动重试。",
                    show_alert=True,
                )
            elif not submission.created:
                await callback.answer(
                    "该用户的审核操作正在执行，未重复提交。",
                    show_alert=True,
                )
            else:
                await callback.answer("审核操作已受理，正在后台复验权限并执行")
        finally:
            callback_acknowledged.set()
        return

    if not await is_group_authorized(session, group_id):
        await session.commit()
        if isinstance(callback, _DetachedCallbackProxy):
            log.warning(
                "moderation action cancelled after group authorization changed | "
                "group=%s violation=%s",
                group_id,
                violation_id,
            )
        else:
            await callback.answer(
                "当前群组未授权，不能执行审核操作",
                show_alert=True,
            )
        return
    if not await is_group_admin_or_higher(
        bot=callback.bot,
        session=session,
        settings=settings,
        group_id=group_id,
        user_id=int(user.id),
    ):
        if isinstance(callback, _DetachedCallbackProxy):
            log.warning(
                "moderation action cancelled after operator permission changed | "
                "group=%s operator=%s violation=%s",
                group_id,
                user.id,
                violation_id,
            )
        else:
            await callback.answer(
                "仅群管理员及以上权限可执行该操作",
                show_alert=True,
            )
        return

    violation = await session.get(Violation, violation_id)
    if violation is None or int(violation.group_id) != group_id:
        await session.commit()
        await callback.answer("审核事件不存在或不属于当前群", show_alert=True)
        return
    target_id = int(violation.user_id)
    await session.commit()

    async with _bounded_moderation_user_lock(group_id, target_id) as acquired:
        if not acquired:
            await callback.answer("该用户的另一项审核操作正在执行，请稍后重试", show_alert=True)
            return
        violation = await session.get(Violation, violation_id, populate_existing=True)
        if violation is None or int(violation.group_id) != group_id:
            await session.commit()
            await callback.answer("审核事件不存在或不属于当前群", show_alert=True)
            return
        base_state, markers = _parse_moderation_action_state(violation.action_taken)
        if base_state not in _MODERATION_ACTION_BASES:
            await session.commit()
            await callback.answer("该审核事件不支持此操作", show_alert=True)
            return

        current_state = str(violation.action_taken or "")
        outcome: str | None = None
        try:
            if action == "ban":
                outcome = await _moderation_direct_ban(
                    callback,
                    session,
                    settings,
                    violation,
                    current_state,
                    markers,
                )
            elif action == "undo":
                outcome = await _moderation_undo_false_positive(
                    callback,
                    session,
                    settings,
                    violation,
                    base_state=base_state,
                    current_state=current_state,
                    markers=markers,
                )
            else:
                outcome = await _moderation_add_permanent_exemption(
                    callback, session, violation
                )
        except Exception:
            await session.rollback()
            log.exception(
                "moderation callback failed | action=%s group=%s violation=%s",
                action,
                group_id,
                violation_id,
            )
            await callback.answer("审核操作失败，请稍后重试", show_alert=True)
            request_current_update_retry()
            outcome = "retry"

        if outcome and outcome != "retry":
            rewritten = await _apply_moderation_outcome_notice(
                callback, settings, outcome=outcome
            )
            if not rewritten and outcome == "ban":
                # The ban succeeded but the notice could not carry the outcome
                # (inaccessible >48h message or already-deleted notice). The
                # silent success ack relies on that rewrite, so restore the
                # explicit confirmation here.
                await callback.answer("已在当前群直接封禁该用户", show_alert=True)
        if _outcome_sink is not None:
            _outcome_sink.append(outcome)


async def _delete_callback_message(callback: CallbackQuery) -> bool:
    """Delete the pressed message, tolerating >48h-old callbacks.

    Old callbacks carry an InaccessibleMessage without a usable .delete();
    the chat/message ids are still present, so fall back to the raw API.
    """
    message = callback.message
    try:
        delete = getattr(message, "delete", None)
        if callable(delete):
            await delete()
            return True
    except TypeError:
        pass
    except Exception:
        return False
    try:
        await callback.bot.delete_message(
            chat_id=int(message.chat.id),
            message_id=int(message.message_id),
        )
        return True
    except Exception:
        return False


@router.callback_query(F.data.startswith(f"{DELETE_BUTTON_CALLBACK_PREFIX}:"))
async def on_delete_button(
    callback: CallbackQuery,
    settings: Settings,
    session: AsyncSession | None = None,
) -> None:
    """Inline delete button for button-mode auto-delete categories.

    Group admins and higher may delete; ordinary members are refused so the
    button cannot be abused to strip moderation notices.
    """
    message = callback.message
    chat = getattr(message, "chat", None)
    user = callback.from_user
    if message is None or chat is None or user is None:
        await callback.answer("操作参数无效", show_alert=True)
        return
    if session is None:
        await callback.answer("会话未就绪，请稍后重试", show_alert=True)
        return
    # Private chats: the chat owner may always clear the bot's own notices.
    if getattr(chat, "type", "") not in {"group", "supergroup"}:
        if not await _delete_callback_message(callback):
            await callback.answer("删除失败，消息可能已被删除", show_alert=True)
            return
        await callback.answer("已删除")
        return
    if not await is_group_admin_or_higher(
        bot=callback.bot,
        session=session,
        settings=settings,
        group_id=int(chat.id),
        user_id=int(user.id),
    ):
        await callback.answer("仅群管理员及以上权限可删除", show_alert=True)
        return
    if not await _delete_callback_message(callback):
        log.debug(
            "delete button failed | group=%s message=%s",
            chat.id,
            getattr(message, "message_id", "?"),
        )
        await callback.answer("删除失败，消息可能已被删除", show_alert=True)
        return
    await callback.answer("已删除")


@router.callback_query(F.data == CALL_ADMIN_RESOLVE_CALLBACK_DATA)
async def on_call_admin_resolved(
    callback: CallbackQuery,
    settings: Settings,
    session: AsyncSession | None = None,
) -> None:
    """Let an administrator close and unpin one urgent @admin notice."""

    message = callback.message
    chat = getattr(message, "chat", None)
    user = callback.from_user
    if session is None:
        await callback.answer("会话未就绪，请稍后重试", show_alert=True)
        return
    if (
        message is None
        or chat is None
        or user is None
        or getattr(chat, "type", "") not in {"group", "supergroup"}
    ):
        await callback.answer("操作参数无效", show_alert=True)
        return
    group_id = int(chat.id)
    authorized = await is_group_authorized(session, group_id)
    await session.commit()
    if not authorized:
        await callback.answer("当前群组未授权", show_alert=True)
        return
    if not await is_group_admin_or_higher(
        bot=callback.bot,
        session=session,
        settings=settings,
        group_id=group_id,
        user_id=int(user.id),
    ):
        if session.in_transaction():
            await session.commit()
        await callback.answer("仅群管理员可标记已处理", show_alert=True)
        return
    if session.in_transaction():
        await session.commit()

    message_id = int(getattr(message, "message_id", 0) or 0)
    unpinned = await unpin_notification_message(
        callback.bot,
        chat_id=group_id,
        message_id=message_id,
        kind="call_admin",
    )
    if not unpinned:
        await callback.answer(
            "取消置顶失败，请稍后重试",
            show_alert=True,
        )
        return
    try:
        await callback.bot.edit_message_reply_markup(
            chat_id=group_id,
            message_id=message_id,
            reply_markup=remove_call_admin_resolution_button(
                getattr(message, "reply_markup", None)
            ),
        )
    except Exception:
        log.debug(
            "call-admin resolved keyboard cleanup failed | group=%s message=%s",
            group_id,
            message_id,
            exc_info=True,
        )
    await callback.answer("已标记处理并取消置顶", show_alert=False)


async def _finalize_vote_enforcement(
    callback: CallbackQuery,
    settings: Settings,
    session: AsyncSession,
    session_factory: Any | None,
    *,
    record: VoteBanSession,
    session_id: int,
    approvals: int,
    recovery: UnbanRecovery,
) -> tuple[bool, bool]:
    """Run the Telegram ban and persist the outcome after a successful
    active→enforcing claim (threshold or admin resolution).

    Returns ``(banned, outcome_persisted)``.  When persistence fails the
    enforcement lease is retained for the recovery worker.
    """
    await session.refresh(record)
    lease_token = record.enforcing_started_at
    group_id = int(record.group_id)
    target_id = int(record.target_user_id)
    owns_generation = bool(
        record.status == "enforcing"
        and lease_token is not None
        and await join_verification_lease_is_current(
            session,
            verification_id=int(recovery.verification_id),
            lease_until=recovery.lease_until,
            status="unbanning",
        )
    )
    # ``refresh`` opened a new read transaction. Snapshot all values needed
    # by the idempotent Telegram side effect, then return the connection to
    # the pool before the Bot API timeout can elapse.
    await session.commit()
    if not owns_generation:
        if record.status == "enforcing" and session_factory is not None:
            # A compatible newer ban generation may have replaced only the
            # recovery token. Keep the durable vote from remaining stuck until
            # restart; its recovery worker will retry after the normal lease.
            cancel_vote_expiry(session_id)
            schedule_vote_enforcement_recovery(
                session_factory=session_factory,
                bot=callback.bot,
                settings=settings,
                session_id=session_id,
            )
        return False, False
    cancel_vote_expiry(session_id)
    if session_factory is not None:
        schedule_vote_enforcement_recovery(
            session_factory=session_factory,
            bot=callback.bot,
            settings=settings,
            session_id=session_id,
        )

    banned = await apply_vote_ban(
        callback.bot,
        session,
        group_id=group_id,
        target_user_id=target_id,
    )
    outcome_persisted = False
    generation_lost = False
    try:
        outcome_persisted = await record_vote_ban_outcome(
            session,
            record,
            approvals=approvals,
            banned=banned,
            lease_token=lease_token,
            recovery=recovery,
        )
        generation_lost = not outcome_persisted
    except Exception:
        await session.rollback()
        log.exception(
            "vote-ban outcome persistence failed; enforcement lease retained | group=%s session=%s",
            group_id,
            session_id,
        )

    if generation_lost:
        try:
            await reconcile_vote_ban_after_lost_generation(
                callback.bot,
                session,
                group_id=group_id,
                target_user_id=target_id,
            )
        except Exception:
            log.exception(
                "vote-ban superseded-state reconciliation failed | group=%s session=%s",
                group_id,
                session_id,
            )

    if outcome_persisted:
        cancel_vote_enforcement_recovery(session_id)
        if banned:
            await close_private_challenge_message(
                callback.bot,
                target_id,
                int(getattr(recovery, "private_message_id", 0) or 0),
            )
        await finalize_vote_message(
            callback.bot,
            settings,
            record,
            outcome_line=enforcement_outcome_line(record, banned=banned),
            approvals=approvals,
        )
    return banned, outcome_persisted


async def _refresh_closed_vote_message(
    callback: CallbackQuery,
    settings: Settings,
    session: AsyncSession,
    *,
    session_id: int,
) -> str | None:
    """Refresh a poll after a possible cross-process terminal transition.

    Returns ``None`` while the poll remains active, otherwise its current
    status. Terminal states are rendered again so a slower live-vote edit
    cannot overwrite an administrator's cancellation/final result.
    """
    record = await session.get(
        VoteBanSession,
        int(session_id),
        populate_existing=True,
    )
    if record is None:
        await session.commit()
        return "missing"
    status = str(record.status or "")
    if status == "active":
        await session.commit()
        return None
    if status == "enforcing":
        await session.commit()
        return status

    approvals = await count_approvals(session, int(session_id))
    await session.commit()
    if status == "cancelled":
        outcome_line = (
            admin_cancel_outcome_line(record)
            if str(record.resolution or "") == VOTE_BAN_ADMIN_RESOLUTION_CANCEL
            else "投票已取消"
        )
    elif status == "expired":
        outcome_line = "投票超时，未达到封禁票数"
    elif status in {"passed", "failed"}:
        outcome_line = enforcement_outcome_line(
            record,
            banned=status == "passed",
        )
    else:
        return status

    cancel_vote_expiry(int(session_id))
    if status in {"passed", "failed"}:
        cancel_vote_enforcement_recovery(int(session_id))
    await finalize_vote_message(
        callback.bot,
        settings,
        record,
        outcome_line=outcome_line,
        approvals=approvals,
    )
    return status


@router.callback_query(F.data.startswith(f"{VOTE_BAN_CALLBACK_PREFIX}:"))
async def on_vote_ban_action(
    callback: CallbackQuery,
    settings: Settings,
    session: AsyncSession | None = None,
    session_factory: Any | None = None,
) -> None:
    if session is None:
        await callback.answer("会话未就绪，请稍后重试", show_alert=True)
        return
    parts = (callback.data or "").split(":")
    if (
        len(parts) != 3
        or parts[0] != VOTE_BAN_CALLBACK_PREFIX
        or parts[1] not in {"vote", "cancel", "ban"}
    ):
        await callback.answer("操作参数无效", show_alert=True)
        return
    action = parts[1]
    try:
        session_id = int(parts[2])
    except (TypeError, ValueError):
        session_id = 0
    message = callback.message
    chat = getattr(message, "chat", None)
    voter = callback.from_user
    if session_id <= 0 or chat is None or voter is None:
        await callback.answer("投票参数无效", show_alert=True)
        return
    if getattr(chat, "type", "") not in {"group", "supergroup"}:
        await callback.answer("该操作只能在群内执行", show_alert=True)
        return
    group_id = int(chat.id)
    if not await is_group_authorized(session, group_id):
        await session.commit()
        await callback.answer("当前群组未授权", show_alert=True)
        return

    record = await session.get(VoteBanSession, session_id)
    if record is None or int(record.group_id) != group_id:
        await session.commit()
        await callback.answer("投票不存在或不属于当前群", show_alert=True)
        return
    if record.status not in {"active", "enforcing"}:
        await session.commit()
        await callback.answer("该投票已结束", show_alert=True)
        return
    target_id = int(record.target_user_id)
    # The prompt-send commit can fail after the message went out, leaving
    # message_id=0; the pressed button knows the real id, so self-heal here.
    # Only trust messages that actually carry this poll's button — MTProto
    # clients can forge callback data against arbitrary bot messages.
    live_message_id = int(getattr(message, "message_id", 0) or 0)
    trusted_live_message_id = 0
    if not int(record.message_id or 0) and live_message_id:
        markup_rows = getattr(getattr(message, "reply_markup", None), "inline_keyboard", None) or []
        carries_button = any(
            getattr(button, "callback_data", None) == f"{VOTE_BAN_CALLBACK_PREFIX}:vote:{session_id}"
            for row in markup_rows
            for button in row
        )
        if carries_button:
            trusted_live_message_id = live_message_id

    # Admin-only resolutions are authorized before taking the per-target lock:
    # the check may call Telegram and must not extend the critical section.
    if action in {"cancel", "ban"}:
        admin_authorized = await is_group_admin_or_higher(
            bot=callback.bot,
            session=session,
            settings=settings,
            group_id=group_id,
            user_id=int(voter.id),
        )
        # The super-admin fast path does not need a database lookup, so the
        # callback authorization helper may legitimately return while the
        # poll read transaction is still open. Release that snapshot before
        # entering the compare-and-swap state transition below.
        if session.in_transaction():
            await session.commit()
        if not admin_authorized:
            await callback.answer("仅群管理员可执行该操作", show_alert=True)
            return

    moderation_lock = _moderation_user_lock(group_id, target_id)
    try:
        await asyncio.wait_for(moderation_lock.acquire(), timeout=1.5)
    except TimeoutError:
        if session.in_transaction():
            await session.rollback()
        await callback.answer("该操作正在处理中，请稍后重试")
        return
    try:
        record = await session.get(VoteBanSession, session_id, populate_existing=True)
        if record is None or record.status not in {"active", "enforcing"}:
            await session.commit()
            await callback.answer("该投票已结束", show_alert=True)
            return
        if record.status == "enforcing":
            recovered_status = await recover_stale_vote_enforcement(
                bot=callback.bot,
                session=session,
                settings=settings,
                record=record,
            )
            if recovered_status == "passed":
                cancel_vote_enforcement_recovery(session_id)
                await callback.answer("投票结果已恢复：该用户已封禁", show_alert=True)
            elif recovered_status == "failed":
                cancel_vote_enforcement_recovery(session_id)
                await callback.answer("投票通过，但封禁失败", show_alert=True)
            else:
                await callback.answer("投票结果正在执行，请稍后", show_alert=True)
            return
        if not int(record.message_id or 0) and trusted_live_message_id:
            record.message_id = trusted_live_message_id

        # Lazy expiry: the in-memory timer dies on restart, so an overdue
        # session is finalized on the next button press.
        if expire_overdue(record):
            if await claim_session_status(
                session, session_id, expected="active", new_status="expired"
            ):
                approvals = await count_approvals(session, session_id)
                await session.commit()
                cancel_vote_expiry(session_id)
                await finalize_vote_message(
                    callback.bot,
                    settings,
                    record,
                    outcome_line="投票超时，未达到封禁票数",
                    approvals=approvals,
                )
            else:
                await session.rollback()
            await callback.answer("该投票已超时结束", show_alert=True)
            return

        voter_id = int(voter.id)

        if action == "cancel":
            resolver_display = member_display_name(
                voter_id,
                full_name=getattr(voter, "full_name", ""),
                username=getattr(voter, "username", ""),
            )
            if not await claim_session_status(
                session,
                session_id,
                expected="active",
                new_status="cancelled",
                resolution=VOTE_BAN_ADMIN_RESOLUTION_CANCEL,
                resolver_user_id=voter_id,
                resolver_display=resolver_display,
            ):
                await session.rollback()
                await callback.answer("投票已由其他操作结束", show_alert=True)
                return
            approvals = await count_approvals(session, session_id)
            await record_ban_event(
                session,
                group_id=group_id,
                target_user_id=target_id,
                target_display=record.target_display,
                target_username=record.target_username,
                action="vote_cancel",
                source="democratic_vote_admin_cancel",
                outcome="cancelled",
                reason="管理员取消民主投票",
                evidence=record.evidence,
                actor_user_id=voter_id,
                actor_display=resolver_display,
                reference_type="vote_session",
                reference_id=session_id,
                details={
                    "approvals": int(approvals),
                    "threshold": int(record.threshold),
                    "trigger_source": str(record.source or "command"),
                    "resolution": VOTE_BAN_ADMIN_RESOLUTION_CANCEL,
                    "deadline_at": record.deadline_at.isoformat(),
                },
            )
            await session.commit()
            record = await session.get(
                VoteBanSession, session_id, populate_existing=True
            )
            await session.commit()
            cancel_vote_expiry(session_id)
            if record is not None:
                await finalize_vote_message(
                    callback.bot,
                    settings,
                    record,
                    outcome_line=admin_cancel_outcome_line(record),
                    approvals=approvals,
                )
            await callback.answer("已取消本次投票", show_alert=True)
            log.info(
                "[%s]【取消】民主投票封禁 | target=%s admin=%s approvals=%s",
                group_id,
                target_id,
                voter_id,
                approvals,
            )
            return

        if action == "ban":
            resolver_display = member_display_name(
                voter_id,
                full_name=getattr(voter, "full_name", ""),
                username=getattr(voter, "username", ""),
            )
            if not await claim_session_status(
                session,
                session_id,
                expected="active",
                new_status="enforcing",
                resolution=VOTE_BAN_ADMIN_RESOLUTION_BAN,
                resolver_user_id=voter_id,
                resolver_display=resolver_display,
            ):
                await session.rollback()
                await callback.answer("投票已由其他操作结束", show_alert=True)
                return
            recovery = await lease_join_verification_for_unban(
                session,
                group_id,
                target_id,
                manual_unban=False,
            )
            if recovery is None:
                await session.rollback()
                await callback.answer("无法建立封禁恢复工单，请稍后重试", show_alert=True)
                return
            approvals = await count_approvals(session, session_id)
            await session.commit()
            banned, outcome_persisted = await _finalize_vote_enforcement(
                callback,
                settings,
                session,
                session_factory,
                record=record,
                session_id=session_id,
                approvals=approvals,
                recovery=recovery,
            )
            if not outcome_persisted:
                answer_text = (
                    "封禁已执行，状态正在自动同步"
                    if banned
                    else "封禁失败，状态正在自动同步"
                )
            elif banned:
                answer_text = "已直接封禁该用户"
            else:
                answer_text = "直接封禁失败，请手动处理"
            await callback.answer(answer_text, show_alert=True)
            log.info(
                "[%s]【结束】民主投票封禁（管理员直接封禁）| target=%s admin=%s banned=%s",
                group_id,
                target_id,
                voter_id,
                banned,
            )
            return

        if voter_id == target_id:
            await session.commit()
            await callback.answer("不能给自己投票", show_alert=True)
            return
        # Public supergroups deliver callbacks from non-members too; only
        # current members get a ballot. Fail closed on lookup errors.
        # Release the authorization/session snapshot first: Telegram membership
        # lookup may consume the full HTTP timeout.  Re-read the poll afterwards
        # because another process can expire or finish it while this call waits.
        await session.commit()
        try:
            member = await callback.bot.get_chat_member(group_id, voter_id)
            status = str(getattr(member, "status", "") or "")
        except Exception:
            status = ""
        if status in {"", "left", "kicked"}:
            await callback.answer("仅本群成员可以投票", show_alert=True)
            return
        record = await session.get(VoteBanSession, session_id, populate_existing=True)
        if record is None or record.status != "active":
            await session.commit()
            await callback.answer("该投票已由其他操作结束", show_alert=True)
            return
        # The deadline may have elapsed while Telegram resolved membership.
        if expire_overdue(record):
            if await claim_session_status(
                session, session_id, expected="active", new_status="expired"
            ):
                approvals = await count_approvals(session, session_id)
                await session.commit()
                cancel_vote_expiry(session_id)
                await finalize_vote_message(
                    callback.bot,
                    settings,
                    record,
                    outcome_line="投票超时，未达到封禁票数",
                    approvals=approvals,
                )
            else:
                await session.rollback()
            await callback.answer("该投票已超时结束", show_alert=True)
            return
        # End the poll refresh snapshot before the conditional ballot write.
        # The INSERT itself re-checks both active status and deadline, so an
        # administrator/expiry worker that won meanwhile remains authoritative.
        await session.commit()
        if not await record_vote(session, session_id, voter_id):
            await session.commit()
            latest = await session.get(
                VoteBanSession,
                session_id,
                populate_existing=True,
            )
            latest_status = str(latest.status or "") if latest is not None else ""
            if (
                latest is not None
                and latest_status == "active"
                and expire_overdue(latest)
            ):
                expired = await claim_session_status(
                    session,
                    session_id,
                    expected="active",
                    new_status="expired",
                )
                if expired:
                    approvals = await count_approvals(session, session_id)
                    await session.commit()
                    cancel_vote_expiry(session_id)
                    await finalize_vote_message(
                        callback.bot,
                        settings,
                        latest,
                        outcome_line="投票超时，未达到封禁票数",
                        approvals=approvals,
                    )
                    await callback.answer("该投票已超时结束", show_alert=True)
                    return

                await session.rollback()
                winner = await session.get(
                    VoteBanSession,
                    session_id,
                    populate_existing=True,
                )
                winner_status = (
                    str(winner.status or "") if winner is not None else ""
                )
                await session.commit()
                answer_text = (
                    "投票结果正在执行，请稍后"
                    if winner_status == "enforcing"
                    else "该投票已由其他操作结束"
                )
                await callback.answer(answer_text, show_alert=True)
                return
            await session.commit()
            if latest_status == "active":
                await callback.answer("你已投过票", show_alert=True)
            else:
                await callback.answer("该投票已由其他操作结束", show_alert=True)
            return
        # Publish this ballot before counting. In multi-process deployments a
        # concurrent voter can then observe it and exactly one side will claim
        # the threshold transition instead of both seeing a stale count.
        await session.commit()
        approvals = await count_approvals(session, session_id)
        threshold = int(record.threshold)
        vote_message_id = int(record.message_id)
        # Counting opens a read transaction. End it before either editing the
        # Telegram poll or trying to upgrade this snapshot into the enforcing
        # write; another voter may have committed while the count was read.
        await session.commit()

        if approvals < threshold:
            changed_status = await _refresh_closed_vote_message(
                callback,
                settings,
                session,
                session_id=session_id,
            )
            if changed_status is not None:
                answer_text = (
                    "投票结果正在执行，请稍后"
                    if changed_status == "enforcing"
                    else "该投票已由其他操作结束"
                )
                await callback.answer(answer_text, show_alert=True)
                return
            try:
                await edit_vote_message(
                    callback.bot,
                    session_id=session_id,
                    group_id=group_id,
                    message_id=vote_message_id,
                    text=build_vote_text(record, approvals=approvals),
                    reply_markup=build_vote_keyboard(
                        session_id, approvals, threshold
                    ),
                )
            except Exception:
                log.debug(
                    "vote message update failed | group=%s session=%s",
                    group_id,
                    session_id,
                    exc_info=True,
                )
            changed_status = await _refresh_closed_vote_message(
                callback,
                settings,
                session,
                session_id=session_id,
            )
            if changed_status is None:
                await callback.answer(f"已投票（{approvals}/{threshold}）")
            elif changed_status == "enforcing":
                await callback.answer("投票结果正在执行，请稍后", show_alert=True)
            else:
                await callback.answer("该投票已由其他操作结束", show_alert=True)
            return

        # Threshold reached: keep the target in an explicit enforcing state
        # while Telegram is called. "passed" is reserved for a confirmed ban.
        if not await claim_session_status(
            session, session_id, expected="active", new_status="enforcing"
        ):
            await session.rollback()
            await callback.answer("投票已由其他操作结束", show_alert=True)
            return
        recovery = await lease_join_verification_for_unban(
            session,
            group_id,
            target_id,
            manual_unban=False,
        )
        if recovery is None:
            await session.rollback()
            await callback.answer("无法建立封禁恢复工单，请稍后重试", show_alert=True)
            return
        await session.commit()
        banned, outcome_persisted = await _finalize_vote_enforcement(
            callback,
            settings,
            session,
            session_factory,
            record=record,
            session_id=session_id,
            approvals=approvals,
            recovery=recovery,
        )
        if not outcome_persisted:
            answer_text = "投票结果已执行，状态正在自动同步"
        else:
            answer_text = "投票通过，已封禁" if banned else "投票通过，但封禁失败"
        await callback.answer(answer_text, show_alert=True)
        log.info(
            "[%s]【结束】民主投票封禁 | target=%s approvals=%s/%s banned=%s",
            group_id,
            target_id,
            approvals,
            record.threshold,
            banned,
        )
    finally:
        moderation_lock.release()


async def _persist_group_activity_cas(
    session: AsyncSession,
    *,
    group_id: int,
    title: str,
    settings: Settings,
    activity_at: datetime | None = None,
    best_effort_on_lock: bool = True,
) -> dict[str, Any]:
    """Persist activity without replacing a concurrently updated JSON document."""

    # Authorization opened a read snapshot. End it before attempting a write;
    # a WAL snapshot cannot be upgraded after another writer commits.
    if session.in_transaction():
        await session.commit()
    for _attempt in range(5):
        row = await session.get(Group, group_id, populate_existing=True)
        if row is None:
            updated = record_group_activity({}, settings.bot, at=activity_at)
            session.add(Group(id=group_id, title=title or "", settings=updated))
            try:
                await session.commit()
                return updated
            except IntegrityError:
                await session.rollback()
                continue
            except OperationalError as exc:
                await session.rollback()
                if not is_database_locked_error(exc):
                    raise
                if not best_effort_on_lock:
                    raise
                log.warning("[%s] skipped activity insert due sqlite lock", group_id)
                return {}

        stored_settings = row.settings
        updated = record_group_activity(
            dict(stored_settings or {}),
            settings.bot,
            at=activity_at,
        )
        predicates = [Group.id == group_id]
        if stored_settings is None:
            predicates.append(Group.settings.is_(None))
        else:
            predicates.append(Group.settings == stored_settings)
        values: dict[str, Any] = {"settings": updated}
        if title:
            values["title"] = title
        try:
            result = await session.execute(
                update(Group)
                .where(*predicates)
                .values(**values)
                .execution_options(synchronize_session=False)
            )
            if int(result.rowcount or 0) == 1:
                await session.commit()
                return updated
            await session.rollback()
        except OperationalError as exc:
            await session.rollback()
            if not is_database_locked_error(exc):
                raise
            if not best_effort_on_lock:
                raise
            log.warning("[%s] skipped activity update due sqlite lock", group_id)
            return dict(stored_settings or {})

    log.warning("[%s] activity update lost repeated concurrent JSON races", group_id)
    await session.rollback()
    fresh = await session.get(Group, group_id, populate_existing=True)
    return dict(fresh.settings or {}) if fresh is not None else {}


@dataclass(slots=True)
class _PendingGroupActivityWrite:
    session_factory: async_sessionmaker[AsyncSession]
    title: str
    settings: Settings
    activity_at: datetime
    version: int = 1
    task: asyncio.Task[None] | None = None


_GROUP_ACTIVITY_DEBOUNCE_SECONDS = 1.0
_GROUP_ACTIVITY_PENDING: dict[int, _PendingGroupActivityWrite] = {}
_GROUP_ACTIVITY_WRITE_SEMAPHORE = asyncio.Semaphore(1)


async def _run_group_activity_writer(group_id: int) -> None:
    retry_delay = 0.5
    try:
        await asyncio.sleep(_GROUP_ACTIVITY_DEBOUNCE_SECONDS)
        while True:
            pending = _GROUP_ACTIVITY_PENDING.get(group_id)
            if pending is None:
                return
            version = pending.version
            try:
                async with _GROUP_ACTIVITY_WRITE_SEMAPHORE:
                    async with pending.session_factory() as write_session:
                        await _persist_group_activity_cas(
                            write_session,
                            group_id=group_id,
                            title=pending.title,
                            settings=pending.settings,
                            activity_at=pending.activity_at,
                            best_effort_on_lock=False,
                        )
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("[%s] deferred group activity flush failed", group_id)
                await asyncio.sleep(retry_delay)
                retry_delay = min(30.0, retry_delay * 2.0)
                continue

            current = _GROUP_ACTIVITY_PENDING.get(group_id)
            if current is pending and current.version == version:
                _GROUP_ACTIVITY_PENDING.pop(group_id, None)
                return
            retry_delay = 0.5
            await asyncio.sleep(_GROUP_ACTIVITY_DEBOUNCE_SECONDS)
    finally:
        current = _GROUP_ACTIVITY_PENDING.get(group_id)
        if current is not None and current.task is asyncio.current_task():
            current.task = None


def _schedule_group_activity_write(
    *,
    session_factory: async_sessionmaker[AsyncSession],
    group_id: int,
    title: str,
    settings: Settings,
    activity_at: datetime,
) -> None:
    pending = _GROUP_ACTIVITY_PENDING.get(group_id)
    if pending is None:
        pending = _PendingGroupActivityWrite(
            session_factory=session_factory,
            title=title,
            settings=settings,
            activity_at=activity_at,
        )
        _GROUP_ACTIVITY_PENDING[group_id] = pending
    else:
        pending.session_factory = session_factory
        pending.title = title or pending.title
        pending.settings = settings
        pending.activity_at = activity_at
        pending.version += 1

    if pending.task is None or pending.task.done():
        pending.task = asyncio.create_task(
            _run_group_activity_writer(group_id),
            name=f"group-activity:{group_id}",
            context=Context(),
        )


async def _record_group_activity_cas(
    session: AsyncSession,
    *,
    group_id: int,
    title: str,
    settings: Settings,
    session_factory: async_sessionmaker[AsyncSession] | None = None,
) -> dict[str, Any]:
    """Return fresh settings and coalesce activity writes off the hot path."""

    if session_factory is None:
        # Compatibility for isolated callers/tests. Production supplies the
        # application factory and never waits for this low-priority write.
        return await _persist_group_activity_cas(
            session,
            group_id=group_id,
            title=title,
            settings=settings,
        )

    if session.in_transaction():
        await session.commit()
    row = await session.get(Group, group_id, populate_existing=True)
    stored_settings = dict(row.settings or {}) if row is not None else {}
    if session.in_transaction():
        await session.commit()

    activity_at = datetime.now().astimezone()
    preview = record_group_activity(stored_settings, settings.bot, at=activity_at)
    _schedule_group_activity_write(
        session_factory=session_factory,
        group_id=group_id,
        title=title,
        settings=settings,
        activity_at=activity_at,
    )
    return preview


async def _best_effort_commit(
    session: AsyncSession,
    *,
    group_id: int,
    context: str,
) -> None:
    if not session.in_transaction():
        return
    try:
        await session.commit()
    except OperationalError as exc:
        if not is_database_locked_error(exc):
            raise
        log.warning("[%s] skipped noncritical db commit | context=%s", group_id, context)


def _extract_image_file_info(message: Message) -> tuple[str, str, int] | None:
    """Return (file_id, mime, declared_size) for safe image-like messages."""
    if message.photo:
        idx = max(0, len(message.photo) - 2)
        photo = message.photo[idx]
        return photo.file_id, "image/jpeg", int(getattr(photo, "file_size", 0) or 0)

    if message.document and (message.document.mime_type or "").startswith("image/"):
        mime = (message.document.mime_type or "image/jpeg").split(";", 1)[0].strip().lower()
        if mime not in _VISION_IMAGE_MIME_TYPES:
            return None
        return message.document.file_id, mime, int(getattr(message.document, "file_size", 0) or 0)

    if message.animation:
        mime = message.animation.mime_type or "image/gif"
        if mime.lower().startswith("video/"):
            return None
        normalized_mime = mime.split(";", 1)[0].strip().lower()
        if normalized_mime not in _VISION_IMAGE_MIME_TYPES:
            return None
        return (
            message.animation.file_id,
            normalized_mime,
            int(getattr(message.animation, "file_size", 0) or 0),
        )

    if message.sticker:
        is_animated = bool(getattr(message.sticker, "is_animated", False))
        is_video = bool(getattr(message.sticker, "is_video", False))
        if not is_animated and not is_video:
            return (
                message.sticker.file_id,
                "image/webp",
                int(getattr(message.sticker, "file_size", 0) or 0),
            )

        thumb = getattr(message.sticker, "thumbnail", None)
        if thumb and getattr(thumb, "file_id", None):
            return thumb.file_id, "image/jpeg", int(getattr(thumb, "file_size", 0) or 0)

    return None


async def _build_telegram_image_data_uri(message: Message) -> str:
    info = _extract_image_file_info(message)
    if not info:
        return ""

    file_id, mime, declared_size = info
    if declared_size > _MAX_VISION_IMAGE_BYTES:
        log.warning("【视觉】跳过超限图片 | 声明大小=%dB | 类型=%s", declared_size, mime)
        return ""

    try:
        async with asyncio.timeout(_VISION_DOWNLOAD_TIMEOUT_SEC):
            tg_file = await message.bot.get_file(file_id)
            remote_size = int(getattr(tg_file, "file_size", 0) or 0)
            if remote_size > _MAX_VISION_IMAGE_BYTES:
                log.warning("【视觉】跳过超限图片 | 远端大小=%dB | 类型=%s", remote_size, mime)
                return ""
            if not tg_file.file_path:
                return ""

            buf = _LimitedBytesIO(_MAX_VISION_IMAGE_BYTES)
            await message.bot.download_file(tg_file.file_path, destination=buf)
    except TimeoutError:
        log.warning("【视觉】图片下载超时")
        return ""
    except Exception as exc:
        log.warning("【视觉】图片下载失败，已降级为纯文本 | error=%s", exc)
        return ""

    raw = buf.getbuffer()
    if not raw:
        return ""

    log.info("【视觉】图片下载完成 | 大小=%dB | 类型=%s", len(raw), mime)
    b64 = base64.b64encode(raw).decode("ascii")
    return f"data:{mime};base64,{b64}"


async def _append_image_context(
    message: Message, llm: LLMService, text: str, msg_type: str
) -> tuple[str, str]:
    """Append image understanding text for moderation/decision/reply and return vision text."""
    if msg_type not in {
        "photo",
        "photo_caption",
        "document",
        "document_caption",
        "animation",
        "animation_caption",
        "sticker",
    }:
        return text, ""

    vision_prompt = (
        "Please describe key information in this image, prioritizing visible text (OCR) and main objects. "
        "Respond briefly in Chinese within 30 words. "
        "If no useful content can be identified, reply exactly: NO_VALID_IMAGE_CONTENT."
    )

    data_uri = await _build_telegram_image_data_uri(message)
    vision_text = ""
    if data_uri:
        try:
            vision_text = (
                await _await_hard_deadline(
                    llm.vision_describe(data_uri, vision_prompt),
                    timeout_seconds=20.0,
                )
            ).strip()
        except asyncio.TimeoutError:
            log.warning("【视觉】识别超时，已降级为纯文本")
            vision_text = ""
        except Exception as exc:
            log.warning("【视觉】识别失败，已降级为纯文本 | error=%s", exc)
            vision_text = ""

    if vision_text == "NO_VALID_IMAGE_CONTENT":
        vision_text = ""

    if not vision_text:
        log.info("【视觉】识别为空")
        return text, ""

    log.info("【视觉】识别结果 | %s", vision_text[:80])
    return f"{text}\n[image-vision]\n{vision_text}", vision_text


async def _build_reply_context_for_llm(message: Message, llm: LLMService) -> str:
    """Build richer reply context, including best-effort vision for replied media."""
    base_context = extract_reply_context(message)
    lines = [line for line in (base_context.splitlines() if base_context else []) if line.strip()]

    reply = getattr(message, "reply_to_message", None)
    if not reply:
        return "\n".join(lines)

    reply_text, reply_type = extract_message_text(reply)
    enriched_reply_text, vision_text = await _append_image_context(reply, llm, reply_text, reply_type)
    if vision_text:
        compact = re.sub(r"\s+", " ", (enriched_reply_text or "").strip())
        if compact:
            lines.append(f"[reply_to_enriched:{reply_type}] {_truncate_text(compact, 320)}")

    return "\n".join(lines)


@dataclass(slots=True)
class _PendingReplyItem:
    message: Message
    group_id: int
    user_id: int
    input_text: str
    msg_type: str
    sender_username: str
    sender_is_owner: bool
    sender_is_tg_admin: bool
    user_tag: str
    explicit_mention: bool
    mentioned: bool
    is_reply: bool
    reply_to_bot: bool
    reply_to_other: bool
    mention_other: bool
    memory_entry: str = ""
    update_completion: UpdateCompletionReceipt | None = None


@dataclass(slots=True)
class _PendingReplyBatch:
    items: list[_PendingReplyItem] = field(default_factory=list)
    task: asyncio.Task[None] | None = None
    settings: Settings | None = None
    flush_at: float = 0.0
    processing: bool = False
    wake_event: asyncio.Event = field(default_factory=asyncio.Event)


@dataclass(slots=True)
class _ReplyDeliveryPlan:
    text: str
    delivery_mode: str
    reply_to_message_id: int | None = None


_PENDING_REPLY_LOCK = asyncio.Lock()
_PENDING_REPLY_BATCHES: dict[tuple[int, int], _PendingReplyBatch] = {}
_PENDING_REPLY_EXECUTION_CAPACITY = 4
_PENDING_REPLY_EXECUTION_SEMAPHORE = asyncio.Semaphore(
    _PENDING_REPLY_EXECUTION_CAPACITY
)
_PENDING_REPLY_DEFAULT_TIMEOUT_SECONDS = 45.0
_PENDING_REPLY_SHUTDOWN_TIMEOUT_SECONDS = 30.0
_PENDING_REPLY_MAX_SENDERS = 64
_PENDING_REPLY_MAX_ITEMS_PER_SENDER = 20
_PENDING_REPLY_ORPHAN_TASKS: set[asyncio.Future[Any]] = set()
_PENDING_REPLY_ORPHAN_STARTED: dict[asyncio.Future[Any], float] = {}
_PENDING_REPLY_ORPHAN_MAX_AGE_SECONDS = 120.0


def _observe_pending_reply_orphan(task: asyncio.Future[Any]) -> None:
    _PENDING_REPLY_ORPHAN_TASKS.discard(task)
    _PENDING_REPLY_ORPHAN_STARTED.pop(task, None)
    try:
        task.result()
    except (asyncio.CancelledError, Exception):
        pass


def _track_pending_reply_orphan(task: asyncio.Future[Any]) -> None:
    if task.done():
        _observe_pending_reply_orphan(task)
        return
    _PENDING_REPLY_ORPHAN_TASKS.add(task)
    _PENDING_REPLY_ORPHAN_STARTED.setdefault(task, time.monotonic())
    task.add_done_callback(_observe_pending_reply_orphan)


def pending_reply_resource_health_snapshot() -> dict[str, Any]:
    now = time.monotonic()
    active_orphans = [
        task for task in _PENDING_REPLY_ORPHAN_TASKS if not task.done()
    ]
    oldest_age = max(
        (
            now - _PENDING_REPLY_ORPHAN_STARTED.get(task, now)
            for task in active_orphans
        ),
        default=0.0,
    )
    waiters = getattr(_PENDING_REPLY_EXECUTION_SEMAPHORE, "_waiters", None)
    batches = tuple(_PENDING_REPLY_BATCHES.values())
    orphan_count = len(active_orphans)
    fatal = bool(
        orphan_count >= _PENDING_REPLY_EXECUTION_CAPACITY
        or oldest_age >= _PENDING_REPLY_ORPHAN_MAX_AGE_SECONDS
    )
    return {
        "ok": not fatal,
        "fatal": fatal,
        "capacity": _PENDING_REPLY_EXECUTION_CAPACITY,
        "available_permits": int(
            getattr(_PENDING_REPLY_EXECUTION_SEMAPHORE, "_value", 0)
        ),
        "semaphore_waiters": len(waiters or ()),
        "orphan_count": orphan_count,
        "oldest_orphan_seconds": round(oldest_age, 3),
        "batch_count": len(batches),
        "processing_batches": sum(1 for batch in batches if batch.processing),
        "queued_items": sum(len(batch.items) for batch in batches),
    }


register_resource_health_provider(
    "pending_replies",
    pending_reply_resource_health_snapshot,
)


class _PendingReplyDeadlineExceeded(asyncio.TimeoutError):
    def __init__(self, task: asyncio.Future[Any]) -> None:
        super().__init__("pending reply deadline exceeded")
        self.task = task


class _PendingReplyQueueFull(RuntimeError):
    pass


def _pending_batch_key(group_id: int, user_id: int) -> tuple[int, int]:
    return group_id, user_id


def _build_merged_user_text(items: list[_PendingReplyItem]) -> str:
    texts = [(item.input_text or "").strip() for item in items if (item.input_text or "").strip()]
    if not texts:
        return ""
    if len(texts) == 1:
        return texts[0]
    return "\n".join(texts)


def _build_recent_bot_messages_context(
    history: list[dict[str, Any]] | None,
    *,
    recent_tail_items: int = 12,
    max_items: int = 3,
) -> str:
    if not history:
        return ""

    recent_tail = [
        item
        for item in history
        if str(item.get("role", "")).strip().lower() != "system"
    ][-max(1, recent_tail_items) :]
    recent_bot_messages = [
        item
        for item in recent_tail
        if str(item.get("role", "")).strip().lower() == "assistant"
    ]
    if not recent_bot_messages:
        return ""

    lines = [
        "[RECENT_BOT_MESSAGES]",
        "Use these recent bot messages to avoid replying too frequently unless the latest merged user intent clearly asks for the bot.",
        f"recent_tail_size={len(recent_tail)}",
        f"recent_bot_message_count={len(recent_bot_messages)}",
    ]
    for item in recent_bot_messages[-max(1, max_items) :]:
        lines.append(format_history_message_line(item, max_body_chars=160))
    return "\n".join(lines)


def _build_merged_context(
    items: list[_PendingReplyItem],
    *,
    recent_history: list[dict[str, Any]] | None = None,
) -> str:
    if not items:
        return ""

    lines = [f"count={len(items)}", "以下是同一用户在当前抖动窗口内连续发送的消息，按时间顺序排列："]
    for idx, item in enumerate(items, start=1):
        meta = (
            f"type={item.msg_type} "
            f"explicit_mention_bot={'yes' if item.explicit_mention else 'no'} "
            f"mention_bot={'yes' if item.mentioned else 'no'} "
            f"reply={'yes' if item.is_reply else 'no'} "
            f"reply_bot={'yes' if item.reply_to_bot else 'no'} "
            f"reply_other={'yes' if item.reply_to_other else 'no'} "
            f"mention_other={'yes' if item.mention_other else 'no'}"
        )
        text = _truncate_text((item.input_text or "").replace("\n", " ").strip(), 280)
        lines.append(f"[{idx}] {meta}")
        lines.append(text or "(empty)")

    recent_bot_context = _build_recent_bot_messages_context(recent_history)
    if recent_bot_context:
        lines.append(recent_bot_context)
    return "\n".join(lines)


def _build_recent_group_message_window(
    items: list[_PendingReplyItem],
    *,
    recent_history: list[dict[str, Any]] | None = None,
) -> str:
    if not items:
        return ""

    recent_group_messages = [
        item
        for item in (recent_history or [])
        if str(item.get("role", "")).strip().lower() != "system"
    ][-6:]
    lines = [
        f"current_sender_batch_count={len(items)}",
        "[RECENT_GROUP_MESSAGES]",
        "These are the most recent group messages before the current merged batch. Use them to judge topic continuity and whether the bot already spoke recently.",
    ]
    if recent_group_messages:
        for item in recent_group_messages:
            lines.append(format_history_message_line(item, max_body_chars=160))
    else:
        lines.append("(none)")

    recent_bot_context = _build_recent_bot_messages_context(recent_history)
    if recent_bot_context:
        lines.append(recent_bot_context)
    return "\n".join(lines)


_build_merged_context = _build_recent_group_message_window


def _message_sender_label(message: Message | None) -> str:
    if message is None:
        return "unknown"

    sender_chat = getattr(message, "sender_chat", None)
    if sender_chat is not None:
        return (
            (getattr(sender_chat, "title", None) or "").strip()
            or (getattr(sender_chat, "username", None) or "").strip()
            or f"chat:{int(getattr(sender_chat, 'id', 0) or 0)}"
        )

    user = getattr(message, "from_user", None)
    if user is not None:
        return (
            (getattr(user, "full_name", None) or "").strip()
            or (getattr(user, "username", None) or "").strip()
            or str(int(getattr(user, "id", 0) or 0))
        )

    return "unknown"


def _append_reply_target_candidate(
    lines: list[str],
    alias_map: dict[str, int],
    *,
    alias: str,
    target_message: Message | None,
    relation: str,
) -> None:
    if not alias or target_message is None:
        return

    message_id = int(getattr(target_message, "message_id", 0) or 0)
    if message_id <= 0:
        return

    key = alias.strip().lower()
    if key in alias_map:
        return

    sender = _truncate_text(_message_sender_label(target_message), 48)
    preview_text, preview_type = extract_message_text(target_message)
    preview = _truncate_text((preview_text or "").replace("\n", " ").strip(), 80)
    alias_map[key] = message_id
    lines.append(
        f"- alias={alias} | message_id={message_id} | sender={sender or 'unknown'} | "
        f"type={preview_type} | relation={relation} | preview={preview or '(empty)'}"
    )


def _build_reply_targets_context(items: list[_PendingReplyItem]) -> tuple[str, dict[str, int]]:
    if not items:
        return "", {}

    latest = items[-1]
    lines = [
        "[REPLY_TARGET_CANDIDATES]",
        "Use these aliases in JSON field reply_to when a specific outgoing message should reply to a specific Telegram message.",
        'If you want the normal default anchor, use "reply_to":"auto".',
        "default_reply_alias: latest_input",
    ]
    alias_map: dict[str, int] = {}

    _append_reply_target_candidate(
        lines,
        alias_map,
        alias="latest_input",
        target_message=latest.message,
        relation="latest current-sender input message",
    )
    _append_reply_target_candidate(
        lines,
        alias_map,
        alias="current_input",
        target_message=latest.message,
        relation="latest current-sender input message",
    )
    _append_reply_target_candidate(
        lines,
        alias_map,
        alias="first_input",
        target_message=items[0].message,
        relation="first current-sender input message in this batch",
    )

    for idx, item in enumerate(items, start=1):
        _append_reply_target_candidate(
            lines,
            alias_map,
            alias=f"input_{idx}",
            target_message=item.message,
            relation=f"batch input #{idx}",
        )
        reply_target = getattr(item.message, "reply_to_message", None)
        if reply_target is not None:
            _append_reply_target_candidate(
                lines,
                alias_map,
                alias=f"input_{idx}_reply_target",
                target_message=reply_target,
                relation=f"message that input #{idx} replies to",
            )

    latest_reply_target = getattr(latest.message, "reply_to_message", None)
    if latest_reply_target is not None:
        _append_reply_target_candidate(
            lines,
            alias_map,
            alias="latest_reply_target",
            target_message=latest_reply_target,
            relation="message that the latest input replies to",
        )
        _append_reply_target_candidate(
            lines,
            alias_map,
            alias="reply_target",
            target_message=latest_reply_target,
            relation="message that the latest input replies to",
        )

    return "\n".join(lines), alias_map


def _resolve_reply_target_message_id(
    value: Any,
    *,
    alias_map: dict[str, int],
) -> int | None:
    default_reply_id = alias_map.get("latest_input") or alias_map.get("current_input")
    if value is None:
        return default_reply_id
    if isinstance(value, int):
        return int(value) if int(value) > 0 else None

    text = str(value or "").strip()
    if not text:
        return default_reply_id

    normalized = text.lower()
    if normalized in {"auto", "default", "latest", "latest_input", "current", "current_input"}:
        return default_reply_id
    if normalized in {"first", "first_input"}:
        return alias_map.get("first_input") or default_reply_id
    if normalized in {"latest_reply_target", "reply_target", "replied_message"}:
        return alias_map.get("latest_reply_target") or alias_map.get("reply_target")
    if normalized in {"none", "message", "standalone", "no_reply"}:
        return None
    if normalized in alias_map:
        return alias_map[normalized]

    for prefix in ("message_id:", "msg:", "id:"):
        if normalized.startswith(prefix):
            normalized = normalized[len(prefix) :].strip()
            break

    if normalized.isdigit():
        parsed = int(normalized)
        return parsed if parsed > 0 else None
    return default_reply_id


def _normalize_multi_message_delivery_plans(
    delivery_plans: list[_ReplyDeliveryPlan],
) -> list[_ReplyDeliveryPlan]:
    if len(delivery_plans) <= 1:
        return list(delivery_plans)

    normalized: list[_ReplyDeliveryPlan] = []
    current_reply_run_target: tuple[str, int | None] | None = None
    reply_used_for_current_target = False

    for plan in delivery_plans:
        mode = (plan.delivery_mode or "reply").strip().lower()
        if mode != "reply":
            normalized.append(plan)
            current_reply_run_target = None
            reply_used_for_current_target = False
            continue

        target_key = ("reply", plan.reply_to_message_id)
        if target_key != current_reply_run_target:
            current_reply_run_target = target_key
            reply_used_for_current_target = False

        if reply_used_for_current_target:
            normalized.append(
                _ReplyDeliveryPlan(
                    text=plan.text,
                    delivery_mode="message",
                    reply_to_message_id=None,
                )
            )
            continue

        normalized.append(plan)
        reply_used_for_current_target = True

    return normalized


# Kept as a module-level name so tests can patch bot.handlers.group._is_user_admin_cached.
_is_user_admin_cached = is_user_admin_cached


async def _revalidate_pending_sender_admin(item: _PendingReplyItem) -> bool:
    """Resolve admin authority again when a queued reply is about to run.

    A normal user's enqueue-time flag is only a snapshot.  Anonymous messages
    sent as the group itself have no user membership to refresh, so retain the
    group-identity semantics used by the inbound handler.
    """
    message = item.message
    if _uses_sender_chat_identity(message):
        sender_chat = getattr(message, "sender_chat", None)
        return bool(sender_chat and int(getattr(sender_chat, "id", 0) or 0) == item.group_id)

    try:
        return bool(await _is_user_admin_cached(message))
    except Exception:
        # Authorization checks must fail closed even if the shared lookup
        # implementation or Telegram API raises unexpectedly.
        log.warning(
            "[%s] pending admin revalidation failed | user=%s",
            item.group_id,
            item.user_id,
            exc_info=True,
        )
        return False


_MEMORY_COMPACT_TASKS: dict[int, asyncio.Task[Any]] = {}
_MEMORY_COMPACT_RERUN: set[int] = set()


def _schedule_memory_compaction(memory: Any, group_id: int) -> None:
    """Run compaction off the hot path; it only matters before the NEXT prompt build."""

    existing = _MEMORY_COMPACT_TASKS.get(group_id)
    if existing is not None and not existing.done():
        _MEMORY_COMPACT_RERUN.add(group_id)
        return

    async def _run() -> None:
        try:
            while True:
                _MEMORY_COMPACT_RERUN.discard(group_id)
                try:
                    await memory.compact_if_needed(group_id)
                except Exception:
                    log.exception("[%s] background memory compaction failed", group_id)
                if group_id not in _MEMORY_COMPACT_RERUN:
                    return
        finally:
            current = asyncio.current_task()
            if _MEMORY_COMPACT_TASKS.get(group_id) is current:
                _MEMORY_COMPACT_TASKS.pop(group_id, None)
            _MEMORY_COMPACT_RERUN.discard(group_id)

    try:
        task = asyncio.create_task(
            _run(),
            name=f"memory-compact:{group_id}",
            context=Context(),
        )
    except RuntimeError:
        return
    # Keep a strong reference and only allow one compactor per group.
    _MEMORY_COMPACT_TASKS[group_id] = task


def _is_strong_pending_reply_signal(item: _PendingReplyItem) -> bool:
    return bool(item.mentioned or item.reply_to_bot or item.sender_is_owner)


def _pending_reply_has_question_signal(text: str) -> bool:
    compact = re.sub(r"\s+", "", text or "")
    if not compact:
        return False
    return bool(_PENDING_REPLY_QUESTION_RE.search(compact))


def _next_pending_reply_flush_at(
    *,
    item: _PendingReplyItem,
    batch_size: int,
    settings: Settings,
    now: float,
    current_flush_at: float = 0.0,
) -> float:
    base_delay = max(0.0, float(settings.bot.inbound_debounce_seconds or 0.0))
    if base_delay <= 0.0:
        return now

    if _is_strong_pending_reply_signal(item):
        delay_seconds = min(base_delay, 0.5)
    elif batch_size >= 3:
        delay_seconds = min(base_delay, 0.9)
    elif batch_size == 2:
        delay_seconds = min(base_delay, 1.1)
    elif item.is_reply or _pending_reply_has_question_signal(item.input_text):
        delay_seconds = min(base_delay, 1.4)
    else:
        delay_seconds = min(base_delay, 1.8)

    target_flush_at = now + delay_seconds
    if current_flush_at > 0.0:
        target_flush_at = min(target_flush_at, current_flush_at)
    return target_flush_at


def _exclude_batch_messages(
    history: list[dict[str, Any]] | None,
    batch_memory_entries: list[str],
) -> list[dict[str, Any]]:
    if not history:
        return []

    pending = Counter(entry for entry in batch_memory_entries if entry)
    if not pending:
        return list(history)

    kept_reversed: list[dict[str, Any]] = []
    for item in reversed(history):
        role = str(item.get("role", "")).strip().lower()
        content = str(item.get("content", ""))
        if role == "user" and pending.get(content, 0) > 0:
            pending[content] -= 1
            continue
        kept_reversed.append(item)
    kept_reversed.reverse()
    return kept_reversed


async def _deliver_reply_plans(
    *,
    message: Message,
    delivery_plans: list[_ReplyDeliveryPlan],
    settings: Settings,
    tts_mode: str,
    tts_service: Any,
    user_id: int,
    group_id: int,
    tts_already_sent: bool,
    force_text: bool = False,
    on_delivery: Callable[[], None] | None = None,
    on_ambiguous: Callable[[], None] | None = None,
    progress_overlay: ReplyMessageOverlay | None = None,
    progress_overlay_factory: (
        Callable[[], Awaitable[ReplyMessageOverlay | None]] | None
    ) = None,
) -> tuple[bool, bool, list[str]]:
    """Deliver every plan, falling back to text per failed voice item."""

    if not delivery_plans:
        return False, tts_already_sent, []

    sent_ok = False
    tts_sent_ok = tts_already_sent
    sent_messages: list[str] = []
    overlay_claimed = False

    async def _claim_progress_overlay(
        *,
        allowed: bool = True,
    ) -> ReplyMessageOverlay | None:
        nonlocal overlay_claimed, progress_overlay
        if not allowed:
            overlay_claimed = True
            return None
        if overlay_claimed:
            return None
        overlay_claimed = True
        if progress_overlay is None and progress_overlay_factory is not None:
            progress_overlay = await progress_overlay_factory()
        return progress_overlay

    if (
        not force_text
        and is_tts_always_enabled(tts_mode)
        and bool(getattr(tts_service, "available", False))
    ):
        visible_delivery_seen = bool(tts_already_sent)
        for plan in delivery_plans:
            plan_receipt = [False]

            def _confirm_plan_delivery(receipt: list[bool] = plan_receipt) -> None:
                receipt[0] = True
                confirm_telegram_delivery(on_delivery)

            detailed_sender = getattr(tts_service, "send_message_tts_result", None)
            if callable(detailed_sender):
                delivery = await detailed_sender(
                    message,
                    plan.text,
                    delivery_mode=plan.delivery_mode,
                    reply_to_message_id=plan.reply_to_message_id,
                    auto_delete_seconds=configured_auto_delete_seconds(settings, "media"),
                    uid=str(user_id or group_id),
                    on_delivery=_confirm_plan_delivery,
                )
            else:
                legacy_sender = tts_service.send_message_tts
                legacy_kwargs = {
                    "delivery_mode": plan.delivery_mode,
                    "reply_to_message_id": plan.reply_to_message_id,
                    "auto_delete_seconds": configured_auto_delete_seconds(
                        settings,
                        "media",
                    ),
                    "uid": str(user_id or group_id),
                }
                if tts_sender_accepts_delivery_callback(legacy_sender):
                    complete = await legacy_sender(
                        message,
                        plan.text,
                        **legacy_kwargs,
                        on_delivery=_confirm_plan_delivery,
                    )
                else:
                    complete = await legacy_sender(
                        message,
                        plan.text,
                        **legacy_kwargs,
                    )
                delivery = TTSDeliveryResult(
                    requested_segments=(plan.text,),
                    sent_segment_count=1 if complete else 0,
                    error="" if complete else "legacy_send_failed",
                )
            if delivery.any_sent and not plan_receipt[0]:
                _confirm_plan_delivery()

            if delivery.complete:
                sent_messages.append(plan.text)
                sent_ok = True
                tts_sent_ok = True
                visible_delivery_seen = True
                continue
            if delivery.any_sent:
                visible_delivery_seen = True
                remaining_sent = False
                if delivery.remaining_text:
                    fallback_overlay = await _claim_progress_overlay(
                        allowed=False,
                    )
                    remaining_sent = await send_reply(
                        message,
                        delivery.remaining_text,
                        delivery_mode=plan.delivery_mode,
                        reply_to_message_id=plan.reply_to_message_id,
                        rich=bool(getattr(settings.bot, "enable_rich_messages", False)),
                        stream=False,
                        auto_delete_seconds=configured_auto_delete_seconds(
                            settings,
                            "reply",
                        ),
                        disable_link_preview=bool(
                            getattr(settings.bot, "disable_link_preview", True)
                        ),
                        on_delivery=_confirm_plan_delivery,
                        on_ambiguous=on_ambiguous,
                        overlay=fallback_overlay,
                    )
                sent_messages.append(
                    plan.text
                    if remaining_sent
                    else delivery.delivered_text
                )
                sent_ok = True
                tts_sent_ok = True
                log.warning(
                    "[%s] always-tts item partially delivered | sent=%d total=%d "
                    "text_fallback=%s",
                    group_id,
                    delivery.sent_segment_count,
                    len(delivery.requested_segments),
                    remaining_sent,
                )
                continue
            log.warning("[%s] always-tts item failed; using text fallback", group_id)
            fallback_overlay = await _claim_progress_overlay(
                allowed=not visible_delivery_seen,
            )
            text_ok = await send_reply(
                message,
                plan.text,
                delivery_mode=plan.delivery_mode,
                reply_to_message_id=plan.reply_to_message_id,
                rich=bool(getattr(settings.bot, "enable_rich_messages", False)),
                stream=False,
                auto_delete_seconds=configured_auto_delete_seconds(settings, "reply"),
                disable_link_preview=bool(
                    getattr(settings.bot, "disable_link_preview", True)
                ),
                on_delivery=_confirm_plan_delivery,
                on_ambiguous=on_ambiguous,
                overlay=fallback_overlay,
            )
            if text_ok:
                sent_messages.append(plan.text)
                sent_ok = True
                visible_delivery_seen = True
            elif plan_receipt[0]:
                sent_ok = True
                visible_delivery_seen = True
        return sent_ok, tts_sent_ok, sent_messages

    # A TTS tool may already have delivered the answer.  Security/quota
    # refusals explicitly set force_text so they remain visible regardless.
    if tts_already_sent and not force_text:
        return True, True, sent_messages

    for plan in delivery_plans:
        plan_receipt = [False]

        def _confirm_plan_delivery(receipt: list[bool] = plan_receipt) -> None:
            receipt[0] = True
            confirm_telegram_delivery(on_delivery)

        current_overlay = await _claim_progress_overlay(
            allowed=not tts_already_sent,
        )
        text_ok = await send_reply(
            message,
            plan.text,
            delivery_mode=plan.delivery_mode,
            reply_to_message_id=plan.reply_to_message_id,
            rich=bool(getattr(settings.bot, "enable_rich_messages", False)),
            stream=bool(settings.bot.enable_streaming and len(delivery_plans) == 1),
            stream_chunk_size=settings.bot.stream_chunk_size,
            stream_interval=settings.bot.stream_edit_interval_sec,
            auto_delete_seconds=configured_auto_delete_seconds(settings, "reply"),
            disable_link_preview=bool(
                getattr(settings.bot, "disable_link_preview", True)
            ),
            on_delivery=_confirm_plan_delivery,
            on_ambiguous=on_ambiguous,
            overlay=current_overlay,
        )
        if text_ok:
            sent_messages.append(plan.text)
            sent_ok = True
        elif plan_receipt[0]:
            sent_ok = True
    return sent_ok, tts_sent_ok, sent_messages


def _effective_batch_flags(
    items: list[_PendingReplyItem],
) -> tuple[bool, bool, bool, bool, bool, str]:
    latest = items[-1]
    mentioned = any(item.mentioned for item in items)
    reply_to_bot = any(item.reply_to_bot for item in items)
    is_reply = latest.is_reply or reply_to_bot
    reply_to_other = latest.reply_to_other and not (mentioned or reply_to_bot)
    mention_other = latest.mention_other and not (mentioned or reply_to_bot)
    return mentioned, is_reply, reply_to_bot, reply_to_other, mention_other, latest.msg_type


async def _resolve_pending_reply_action(
    *,
    decision_svc: DecisionService,
    group_settings: dict | None,
    explicit_mention: bool,
    input_text: str,
    is_mentioned: bool,
    is_reply: bool,
    is_reply_to_bot: bool,
    is_reply_to_other: bool,
    mentions_other_user: bool,
    is_owner: bool,
    is_tg_admin: bool,
    user_tag: str,
    msg_type: str,
    history: list[dict[str, Any]] | None,
    merged_count: int,
    merged_context: str,
) -> tuple[str, bool]:
    if explicit_mention or is_reply_to_bot:
        return "casual", True

    if is_at_reply_enabled(group_settings):
        return "skip", True

    action = await decision_svc.decide(
        input_text,
        is_mentioned=is_mentioned,
        is_reply=is_reply,
        is_reply_to_bot=is_reply_to_bot,
        is_reply_to_other=is_reply_to_other,
        mentions_other_user=mentions_other_user,
        is_owner=is_owner,
        is_tg_admin=is_tg_admin,
        user_tag=user_tag,
        msg_type=msg_type,
        history=history,
        merged_count=merged_count,
        merged_context=merged_context,
    )
    return action, False


@dataclass(slots=True)
class _PendingReplyDeliveryReceipt:
    delivered: bool = False
    ambiguous: bool = False

    def confirm(self) -> None:
        self.delivered = True

    def mark_ambiguous(self) -> None:
        self.ambiguous = True

    @property
    def consumed(self) -> bool:
        """Whether replay could duplicate an externally attempted effect."""

        return self.delivered or self.ambiguous


def _log_pending_reply_action(
    *,
    group_id: int,
    action: str,
    action_forced: bool,
    explicit_mention: bool,
    reply_to_bot: bool,
    elapsed_ms: int,
) -> None:
    if action_forced and action == "skip":
        # In @-reply mode this is the expected path for almost every ordinary
        # group message. Keep it available for diagnosis without flooding the
        # normal service log.
        log.debug(
            "[%s] pending batch skipped by at-reply mode | "
            "explicit_mention=%s reply_to_bot=%s elapsed=%dms",
            group_id,
            explicit_mention,
            reply_to_bot,
            elapsed_ms,
        )
    elif action_forced:
        log.info(
            "[%s] pending batch action forced | action=%s explicit_mention=%s "
            "reply_to_bot=%s elapsed=%dms",
            group_id,
            action,
            explicit_mention,
            reply_to_bot,
            elapsed_ms,
        )
    else:
        log.info(
            "[%s] pending batch decision done | action=%s elapsed=%dms",
            group_id,
            action,
            elapsed_ms,
        )


async def _process_pending_reply_batch(
    items: list[_PendingReplyItem],
    settings: Settings,
    *,
    delivery_receipt: _PendingReplyDeliveryReceipt | None = None,
) -> bool:
    if not items:
        return True

    latest = items[-1]
    group_id = latest.group_id
    user_id = latest.user_id
    flow_started = time.perf_counter()
    merged_count = len(items)
    merged_input_text = _build_merged_user_text(items)
    merged_context = _build_recent_group_message_window(items)
    reply_targets_context, reply_target_aliases = _build_reply_targets_context(items)
    memory_entries = [item.memory_entry for item in items if item.memory_entry]
    memory = memory_holder.get()
    session_factory = memory.session_factory
    delivery_confirmed = bool(delivery_receipt and delivery_receipt.delivered)
    delivery_ambiguous = bool(delivery_receipt and delivery_receipt.ambiguous)

    def _confirm_delivery() -> None:
        nonlocal delivery_confirmed
        delivery_confirmed = True
        if delivery_receipt is not None:
            delivery_receipt.confirm()

    def _mark_delivery_ambiguous() -> None:
        nonlocal delivery_ambiguous
        delivery_ambiguous = True
        if delivery_receipt is not None:
            delivery_receipt.mark_ambiguous()

    llm = LLMService(
        settings.bot.main_model,
        settings.bot.decision_model,
        settings.bot.compress_model,
        moderation=settings.bot.moderation_model,
        vision=settings.bot.vision_model,
        embed=settings.bot.embed_model,
        max_context_tokens=settings.bot.max_context_tokens,
    )
    decision_svc = DecisionService(llm, context_items=settings.bot.decision_context_items)
    reply_mode_svc = ReplyModeService(llm)
    sticker_pool = [
        x.strip()
        for x in (settings.skill_sticker_file_ids or "").split(",")
        if x and x.strip()
    ]
    skill = SkillService(llm, settings=settings, default_sticker_file_ids=sticker_pool)

    mentioned, is_reply, reply_to_bot, reply_to_other, mention_other, msg_type = _effective_batch_flags(items)
    explicit_mention = any(item.explicit_mention for item in items)
    latest_is_direct_request = any(
        item.explicit_mention
        or item.reply_to_bot
        or is_explicit_vote_ban_request(item.input_text)
        for item in items
    )
    progress: ReplyProgressTracker | None = None
    progress_overlay: ReplyMessageOverlay | None = None
    progress_handoff_attempted = False

    async def _handoff_progress_overlay() -> ReplyMessageOverlay | None:
        nonlocal progress_handoff_attempted, progress_overlay
        if progress_handoff_attempted:
            return progress_overlay
        progress_handoff_attempted = True
        if progress is not None:
            progress_overlay = await progress.handoff(
                "已整理并发送回答"
            )
        return progress_overlay

    log.info(
        "[%s] pending batch flush started | user=%s messages=%d debounce=%.2fs",
        group_id,
        user_id,
        merged_count,
        float(settings.bot.inbound_debounce_seconds or 0.0),
    )

    async with session_factory() as session:
        try:
            group_row = await session.get(Group, group_id)
            group_settings = (group_row.settings if group_row and group_row.settings else {})
            if bool(group_settings.get("mute_all_replies", False)):
                log.info("[%s] pending batch skipped | reason=mute_all_replies user=%s", group_id, user_id)
                return True
            tts_mode = normalize_tts_mode(group_settings)
            allow_api_model_query = api_model_query_tool_enabled(group_settings)
            style_state = get_style_state(group_settings)
            style_profile_context = build_style_profile_context(
                str(style_state.get("profile_text") or ""),
                target_name=str(style_state.get("target_user_name") or ""),
            )

            mute_stmt = select(ReplyMute.id).where(
                ReplyMute.group_id == group_id,
                ReplyMute.user_id == user_id,
            )
            mute_result = await session.execute(mute_stmt)
            if mute_result.scalar_one_or_none() is not None:
                log.info("[%s] pending batch skipped | reason=user_muted user=%s", group_id, user_id)
                return True

            # The remaining path may spend tens of seconds in model, tool and
            # Telegram calls.  Release the connection now; AsyncSession can be
            # reused later if a short DB operation is actually needed.
            await session.close()

            decision_history = _exclude_batch_messages(memory.get_history(group_id), memory_entries)
            merged_context = _build_recent_group_message_window(items, recent_history=decision_history)
            decision_started = time.perf_counter()
            # The enqueue-time snapshot is sufficient for reply-routing
            # semantics, but it must never authorize a later skill execution.
            action, action_forced = await _resolve_pending_reply_action(
                decision_svc=decision_svc,
                group_settings=group_settings,
                explicit_mention=explicit_mention,
                input_text=merged_input_text,
                is_mentioned=mentioned,
                is_reply=is_reply,
                is_reply_to_bot=reply_to_bot,
                is_reply_to_other=reply_to_other,
                mentions_other_user=mention_other,
                is_owner=latest.sender_is_owner,
                is_tg_admin=latest.sender_is_tg_admin,
                user_tag=latest.user_tag,
                msg_type=msg_type,
                history=decision_history,
                merged_count=merged_count,
                merged_context=merged_context,
            )
            _log_pending_reply_action(
                group_id=group_id,
                action=action,
                action_forced=action_forced,
                explicit_mention=explicit_mention,
                reply_to_bot=reply_to_bot,
                elapsed_ms=int((time.perf_counter() - decision_started) * 1000),
            )

            reply = ""
            reply_specs: list[ReplyMessageSpec] = []
            reply_source = "none"
            sent_ok = False
            sent_reply_messages: list[str] = []
            delivery_plans: list[_ReplyDeliveryPlan] = []
            skill_handled = False
            skill_must_deliver_text = False
            sticker_sent_ok = False
            tts_sent_ok = False
            embedded_reply_sent_ok = False
            sticker_file = ""
            delivery_mode = "reply"
            tts_text = ""
            embedded_reply_text = ""
            explicit_no_reply = False
            force_reply = bool(action_forced and action == "casual")
            tts_service = skill.tts_service or DoubaoTTSService(settings)

            if action != "skip":
                progress = ReplyProgressTracker(
                    latest.message,
                    enabled=latest_is_direct_request,
                    reveal_after=3.0,
                    edit_interval=max(
                        0.8,
                        float(settings.bot.stream_edit_interval_sec or 0.0),
                    ),
                    # Progress is transient UI, not another permanent group
                    # message. Keep its retention independent from replies.
                    auto_delete_seconds=30,
                    disable_link_preview=bool(
                        getattr(settings.bot, "disable_link_preview", True)
                    ),
                )
                await progress.start()
                history = await memory.get_history_for_llm(
                    group_id,
                    prompt_payload_builder=lambda candidate_history: skill.build_answer_prompt_payload(
                        merged_input_text,
                        history=_exclude_batch_messages(candidate_history, memory_entries),
                        sender_user_id=user_id,
                        sender_username=latest.sender_username,
                        sender_is_owner=latest.sender_is_owner,
                        # This payload is only used for token budgeting. Keep it
                        # conservative until the execution-time lookup below.
                        sender_is_tg_admin=False,
                        intent_type=action,
                        allow_tts=is_tts_tool_enabled(tts_mode),
                        allow_api_model_query=allow_api_model_query,
                        tts_mode=tts_mode,
                        merged_count=merged_count,
                        merged_context=merged_context,
                        reply_targets_context=reply_targets_context,
                        is_mentioned=mentioned,
                        is_reply_to_bot=reply_to_bot,
                        style_profile_context=style_profile_context,
                    ),
                )
                history = _exclude_batch_messages(history, memory_entries)
                log.info(
                    "[%s] pending batch reply generation started | action=%s history=%d",
                    group_id,
                    action,
                    len(history),
                )

                async with typing_action(latest.message, enabled=settings.bot.enable_typing):
                    raw_reply = ""
                    sender_is_tg_admin = await _revalidate_pending_sender_admin(latest)
                    if latest.sender_is_tg_admin and not sender_is_tg_admin:
                        log.info(
                            "[%s] pending admin authority revoked before skill execution | user=%s",
                            group_id,
                            user_id,
                        )
                    skill_result = await skill.answer_with_skill(
                        merged_input_text,
                        session=None,
                        history=history,
                        sender_user_id=user_id,
                        sender_username=latest.sender_username,
                        sender_is_owner=latest.sender_is_owner,
                        sender_is_tg_admin=sender_is_tg_admin,
                        message=latest.message,
                        session_factory=session_factory,
                        intent_type=action,
                        allow_tts=is_tts_tool_enabled(tts_mode),
                        allow_api_model_query=allow_api_model_query,
                        tts_mode=tts_mode,
                        merged_count=merged_count,
                        merged_context=merged_context,
                        reply_targets_context=reply_targets_context,
                        is_mentioned=mentioned,
                        is_reply_to_bot=reply_to_bot,
                        is_direct_request=latest_is_direct_request,
                        style_profile_context=style_profile_context,
                        delivery_callback=_confirm_delivery,
                        progress_callback=progress.report,
                    )
                    skill_handled = bool(skill_result.handled)
                    skill_must_deliver_text = bool(
                        getattr(skill_result, "must_deliver_text", False)
                    )
                    sticker_sent_ok = bool(skill_result.sticker_sent)
                    tts_sent_ok = bool(skill_result.tts_sent)
                    embedded_reply_sent_ok = bool(
                        getattr(skill_result, "embedded_reply_sent", False)
                    )
                    skill_delivery_confirmed = bool(
                        getattr(skill_result, "delivery_confirmed", False)
                    )
                    sticker_file = skill_result.sticker_file_id or ""
                    tts_text = skill_result.tts_text or ""
                    embedded_reply_text = str(
                        getattr(skill_result, "embedded_reply_text", "") or ""
                    ).strip()
                    skill_delivery_sent = bool(
                        sticker_sent_ok
                        or tts_sent_ok
                        or embedded_reply_sent_ok
                        or skill_delivery_confirmed
                    )
                    if skill_delivery_sent:
                        skill_handled = True
                        sent_ok = True
                        _confirm_delivery()
                    if skill_result.text:
                        raw_reply = skill_result.text
                        reply_source = "skill"
                        log.info("[%s] pending batch reply via skill | %s", group_id, raw_reply[:80])
                    elif skill_handled:
                        reply_source = "skill"
                        log.info(
                            "[%s] pending batch handled by skill | sticker_sent=%s "
                            "tts_sent=%s embedded_sent=%s file=%s",
                            group_id,
                            sticker_sent_ok,
                            tts_sent_ok,
                            embedded_reply_sent_ok,
                            sticker_file[:32] if sticker_file else "-",
                        )
                    if (
                        is_tts_always_enabled(tts_mode)
                        and tts_sent_ok
                        and raw_reply
                        and not skill_must_deliver_text
                    ):
                        log.info("[%s] pending batch suppressing text because TTS already sent", group_id)
                        raw_reply = ""
                        reply_source = "skill"

                    if not raw_reply and not skill_handled:
                        await progress.composing()
                        casual = CasualService(
                            llm,
                            settings=settings,
                            skill_names=skill.available_skill_names(
                                allow_tts=is_tts_tool_enabled(tts_mode),
                                allow_api_model_query=allow_api_model_query,
                            ),
                        )
                        raw_reply = await casual.reply(
                            merged_input_text,
                            history=history,
                            sender_user_id=user_id,
                            sender_username=latest.sender_username,
                            sender_is_owner=latest.sender_is_owner,
                            sender_is_tg_admin=sender_is_tg_admin,
                            intent_type=action,
                            merged_count=merged_count,
                            merged_context=merged_context,
                            reply_targets_context=reply_targets_context,
                            is_mentioned=mentioned,
                            is_reply_to_bot=reply_to_bot,
                            style_profile_context=style_profile_context,
                        )
                        if raw_reply:
                            reply_source = "casual"
                        log.info(
                            "[%s] pending batch reply via casual | intent=%s | %s",
                            group_id,
                            action,
                            raw_reply[:80] if raw_reply else "(empty)",
                        )

                    if raw_reply and reply_source == "skill":
                        await progress.composing()

                    current_task = asyncio.current_task()
                    if current_task is not None and current_task.cancelling():
                        raise asyncio.CancelledError

                    if raw_reply:
                        parsed_reply = parse_reply_output(raw_reply)
                        explicit_no_reply = parsed_reply.explicit_no_reply
                        if parsed_reply.used_json:
                            log.info(
                                "[%s] pending batch structured reply parsed | source=%s messages=%d explicit_no_reply=%s",
                                group_id,
                                reply_source,
                                len(parsed_reply.messages),
                                explicit_no_reply,
                            )
                        if explicit_no_reply:
                            log.info(
                                "[%s] pending batch reply explicitly skipped by model | source=%s reason=%s",
                                group_id,
                                reply_source,
                                parsed_reply.reason or "model_declined_reply",
                            )
                        else:
                            cleaned_specs: list[ReplyMessageSpec] = []
                            for candidate_spec in parsed_reply.message_specs:
                                cleaned_reply = sanitize_outgoing_text(candidate_spec.text)
                                if cleaned_reply != candidate_spec.text:
                                    log.warning("[%s] pending batch reply sanitized", group_id)
                                normalized_reply = _normalize_owner_address(
                                    cleaned_reply,
                                    latest.sender_is_owner,
                                )
                                if normalized_reply != cleaned_reply:
                                    log.info("[%s] pending batch owner-address normalized", group_id)
                                if normalized_reply:
                                    cleaned_specs.append(
                                        ReplyMessageSpec(
                                            text=normalized_reply,
                                            delivery_mode=candidate_spec.delivery_mode,
                                            reply_to=candidate_spec.reply_to,
                                        )
                                    )
                            reply_specs = cleaned_specs
                            reply = "\n\n".join(spec.text for spec in reply_specs).strip()

                if explicit_no_reply:
                    reply = ""
                    reply_specs = []
                    delivery_plans = []
                    reply_source = "none"
                    action = "skip"
                elif reply_specs or not skill_handled:
                    silence_reply, silence_reason = _should_silence_generated_reply(reply)
                    if silence_reply and force_reply:
                        log.warning(
                            "[%s] pending batch forced casual produced no usable reply, using fallback | reason=%s",
                            group_id,
                            silence_reason,
                        )
                        reply = "我在，直接说就好~"
                        reply_specs = [ReplyMessageSpec(text=reply)]
                        reply_source = "fallback"
                        silence_reply = False
                    if silence_reply:
                        preview = _truncate_text(reply, 80) if reply else "-"
                        log.info(
                            "[%s] pending batch reply suppressed | reason=%s source=%s intent=%s preview=%s",
                            group_id,
                            silence_reason,
                            reply_source,
                            action,
                            preview,
                            )
                        reply = ""
                        reply_specs = []
                        delivery_plans = []
                        reply_source = "none"
                        action = "skip"

            if action != "skip" and reply_specs:
                auto_reply_specs = [
                    reply_spec
                    for reply_spec in reply_specs
                    if reply_spec.delivery_mode == "auto"
                ]
                # Obvious case: the user addressed the bot directly, so replies
                # anchor to their message — no reply-mode LLM round needed.
                obvious_reply_mode = reply_to_bot or (mentioned and not reply_to_other)
                if auto_reply_specs and obvious_reply_mode:
                    resolved_auto_modes = ["reply"] * len(auto_reply_specs)
                elif auto_reply_specs:
                    resolved_auto_modes = await reply_mode_svc.decide_many(
                        user_text=merged_input_text,
                        assistant_replies=[reply_spec.text for reply_spec in auto_reply_specs],
                        msg_type=msg_type,
                        is_mentioned=mentioned,
                        is_reply_to_bot=reply_to_bot,
                        is_reply_to_other=reply_to_other,
                        merged_count=merged_count,
                        merged_context=merged_context,
                    )
                else:
                    resolved_auto_modes = []
                auto_mode_iter = iter(resolved_auto_modes)

                for reply_spec in reply_specs:
                    resolved_mode = reply_spec.delivery_mode
                    if resolved_mode == "auto":
                        resolved_mode = next(
                            auto_mode_iter,
                            "reply"
                            if reply_to_bot or (mentioned and not reply_to_other)
                            else "message",
                        )
                    reply_target_id = (
                        _resolve_reply_target_message_id(
                            reply_spec.reply_to,
                            alias_map=reply_target_aliases,
                        )
                        if resolved_mode == "reply"
                        else None
                    )
                    delivery_plans.append(
                        _ReplyDeliveryPlan(
                            text=reply_spec.text,
                            delivery_mode=resolved_mode,
                            reply_to_message_id=reply_target_id,
                        )
                    )

                delivery_plans = _normalize_multi_message_delivery_plans(delivery_plans)
                resolved_modes = [plan.delivery_mode for plan in delivery_plans]

                unique_modes = sorted({mode for mode in resolved_modes if mode})
                if not unique_modes:
                    delivery_mode = "reply"
                elif len(unique_modes) == 1:
                    delivery_mode = unique_modes[0]
                else:
                    delivery_mode = "mixed"
                log.info(
                    "[%s] pending batch reply plans ready | count=%d modes=%s",
                    group_id,
                    len(delivery_plans),
                    ",".join(unique_modes) or "(none)",
                )

            current_task = asyncio.current_task()
            if current_task is not None and current_task.cancelling():
                raise asyncio.CancelledError

            await _best_effort_commit(
                session,
                group_id=group_id,
                context="pending_reply_pre_delivery",
            )

            if action != "skip" and delivery_plans:
                text_delivery_expected = bool(
                    not tts_sent_ok
                    and (
                        skill_must_deliver_text
                        or not (
                            is_tts_always_enabled(tts_mode)
                            and bool(getattr(tts_service, "available", False))
                        )
                    )
                )
                if progress is not None and text_delivery_expected:
                    progress_overlay = await _handoff_progress_overlay()
                delivered, tts_sent_ok, sent_reply_messages = await _deliver_reply_plans(
                    message=latest.message,
                    delivery_plans=delivery_plans,
                    settings=settings,
                    tts_mode=tts_mode,
                    tts_service=tts_service,
                    user_id=user_id,
                    group_id=group_id,
                    tts_already_sent=tts_sent_ok,
                    force_text=skill_must_deliver_text,
                    on_delivery=_confirm_delivery,
                    on_ambiguous=_mark_delivery_ambiguous,
                    progress_overlay=progress_overlay,
                    progress_overlay_factory=_handoff_progress_overlay,
                )
                sent_ok = sent_ok or delivered
                if delivered and not delivery_ambiguous:
                    _confirm_delivery()

            if action == "skip":
                if progress is not None:
                    await progress.dismiss()
                log.info(
                    "[%s] pending batch finished | action=skip mention=%s mention_other=%s reply=%s reply_bot=%s reply_other=%s skill_handled=%s sticker_sent=%s tts_sent=%s elapsed=%dms",
                    group_id,
                    mentioned,
                    mention_other,
                    is_reply,
                    reply_to_bot,
                    reply_to_other,
                    skill_handled,
                    sticker_sent_ok,
                    tts_sent_ok,
                    int((time.perf_counter() - flow_started) * 1000),
                )
                return True

            stored_reply_messages = list(sent_reply_messages)
            if not stored_reply_messages and tts_text and tts_sent_ok:
                stored_reply_messages = [tts_text]
            if (
                not stored_reply_messages
                and embedded_reply_text
                and embedded_reply_sent_ok
            ):
                stored_reply_messages = [embedded_reply_text]
            if stored_reply_messages:
                current_task = asyncio.current_task()
                if current_task is not None and current_task.cancelling():
                    raise asyncio.CancelledError
                for stored_reply in stored_reply_messages:
                    await memory.add_message(
                        group_id,
                        "assistant",
                        stored_reply,
                        message_type="assistant_reply",
                        defer_persistence=True,
                        completions=_unique_pending_reply_completions(items),
                    )
                _schedule_memory_compaction(memory, group_id)
            log.info(
                "[%s] pending batch finished | action=%s source=%s generated=%s "
                "sent=%s mode=%s skill_handled=%s sticker_sent=%s tts_sent=%s "
                "embedded_sent=%s file=%s count=%d len=%d elapsed=%dms",
                group_id,
                action,
                reply_source,
                bool(reply_specs),
                sent_ok,
                delivery_mode,
                skill_handled,
                sticker_sent_ok,
                tts_sent_ok,
                embedded_reply_sent_ok,
                sticker_file[:32] if sticker_file else "-",
                len(reply_specs),
                len(reply or ""),
                int((time.perf_counter() - flow_started) * 1000),
            )
            succeeded = bool(
                sent_ok
                or sticker_sent_ok
                or tts_sent_ok
                or delivery_ambiguous
                or not latest_is_direct_request
            )
            if progress is not None:
                overlay_owns_message = bool(
                    progress_overlay is not None
                    and progress_overlay.outcome
                    in {"attempting", "attached", "ambiguous"}
                )
                if not overlay_owns_message:
                    await progress.dismiss()
            return succeeded
        except asyncio.CancelledError:
            overlay_owns_message = bool(
                progress_overlay is not None
                and progress_overlay.outcome
                in {"attempting", "attached", "ambiguous"}
            )
            if overlay_owns_message:
                pass
            elif delivery_confirmed:
                if progress is not None:
                    await progress.dismiss()
            elif progress is not None and progress.visible:
                terminal_delivered = await progress.fail(
                    "处理已中止；若涉及发送或修改，结果可能已生效，"
                    "请先检查群内状态，确认未执行后再重试"
                )
                if terminal_delivered:
                    _confirm_delivery()
                else:
                    await progress.dismiss()
            elif progress is not None:
                await progress.dismiss()
            raise
        except Exception as exc:
            try:
                await session.rollback()
            except Exception:
                log.exception(
                    "[%s] pending batch rollback failed after processing error",
                    group_id,
                )
            if delivery_confirmed:
                if progress is not None and not (
                    progress_overlay is not None
                    and progress_overlay.outcome
                    in {"attempting", "attached", "ambiguous"}
                ):
                    await progress.dismiss()
                log.error(
                    "[%s] pending batch post-delivery processing failed; "
                    "suppressing duplicate failure reply",
                    group_id,
                    exc_info=(type(exc), exc, exc.__traceback__),
                )
                return True
            if progress is not None and not (
                progress_overlay is not None
                and progress_overlay.outcome
                in {"attempting", "attached", "ambiguous"}
            ):
                await progress.dismiss()
            raise
        finally:
            if progress is not None:
                await progress.close()


async def _await_hard_deadline(awaitable: Any, *, timeout_seconds: float) -> Any:
    """Return at a wall-clock deadline even if a child delays cancellation."""

    task = asyncio.ensure_future(awaitable)

    try:
        done, _ = await asyncio.wait(
            {task},
            timeout=max(0.01, float(timeout_seconds)),
        )
    except asyncio.CancelledError:
        task.cancel()
        # Shutdown cancellation can be ignored just like a wall-clock timeout.
        # Keep the real child visible to the common drain until it actually
        # exits; otherwise it can retain the reply semaphore or shared Bot/DB
        # resources after the worker that owned it has already disappeared.
        _track_pending_reply_orphan(task)
        raise
    if task in done:
        return task.result()
    task.cancel()
    _track_pending_reply_orphan(task)
    raise _PendingReplyDeadlineExceeded(task)


def _pending_reply_timeout_seconds(settings: Settings) -> float:
    configured = getattr(settings.bot, "reply_batch_timeout_seconds", None)
    if configured is None:
        return _PENDING_REPLY_DEFAULT_TIMEOUT_SECONDS
    return min(120.0, max(5.0, float(configured)))


async def _notify_pending_reply_failure(
    items: list[_PendingReplyItem],
    *,
    timed_out: bool,
) -> bool:
    if not items:
        return False
    direct_item = next(
        (
            item
            for item in reversed(items)
            if _is_strong_pending_reply_signal(item)
            or is_explicit_vote_ban_request(item.input_text)
        ),
        None,
    )
    if direct_item is None:
        # A non-directed ambient message has no promised visible reply. Its
        # failure is terminal and should not cause Telegram to replay the whole
        # moderation/memory pipeline.
        return True
    outcome = "超时" if timed_out else "失败"
    text = (
        f"这次请求处理{outcome}了。若请求涉及发送或修改，结果可能已生效；"
        "请先检查群内状态，确认未执行后再重试。"
    )
    try:
        sent = await _await_hard_deadline(
            send_reply(
                direct_item.message,
                text,
                delivery_mode="reply",
                reply_to_message_id=(
                    int(getattr(direct_item.message, "message_id", 0) or 0) or None
                ),
                stream=False,
            ),
            timeout_seconds=8.0,
        )
        return bool(sent)
    except Exception:
        log.exception(
            "[%s] pending failure notification failed | user=%s",
            direct_item.group_id,
            direct_item.user_id,
        )
        return False


@dataclass(slots=True)
class _PendingReplyOutcome:
    succeeded: bool
    orphan: asyncio.Task[Any] | None = None
    timed_out: bool = False


def _finish_pending_reply_items(
    items: list[_PendingReplyItem],
    *,
    succeeded: bool,
) -> None:
    for item in items:
        receipt = item.update_completion
        if receipt is not None:
            receipt.finish(succeeded)


def _unique_pending_reply_completions(
    items: list[_PendingReplyItem],
) -> tuple[UpdateCompletionReceipt, ...]:
    completions: list[UpdateCompletionReceipt] = []
    seen: set[int] = set()
    for item in items:
        completion = item.update_completion
        if completion is None or id(completion) in seen:
            continue
        seen.add(id(completion))
        completions.append(completion)
    return tuple(completions)


async def _process_pending_reply_batch_guarded(
    key: tuple[int, int],
    items: list[_PendingReplyItem],
    settings: Settings,
    *,
    delivery_receipt: _PendingReplyDeliveryReceipt | None = None,
) -> _PendingReplyOutcome:
    timeout_seconds = _pending_reply_timeout_seconds(settings)
    if delivery_receipt is None:
        delivery_receipt = _PendingReplyDeliveryReceipt()

    async def _run() -> bool:
        async with _PENDING_REPLY_EXECUTION_SEMAPHORE:
            return await _process_pending_reply_batch(
                items,
                settings,
                delivery_receipt=delivery_receipt,
            )

    try:
        succeeded = bool(
            await _await_hard_deadline(_run(), timeout_seconds=timeout_seconds)
        ) or delivery_receipt.consumed
        if not succeeded:
            succeeded = await _notify_pending_reply_failure(items, timed_out=False)
        return _PendingReplyOutcome(succeeded=succeeded)
    except _PendingReplyDeadlineExceeded as exc:
        log.error(
            "pending batch timed out | key=%s messages=%d timeout=%.1fs",
            key,
            len(items),
            timeout_seconds,
        )
        if delivery_receipt.consumed:
            log.warning(
                "pending batch exceeded deadline after delivery or an ambiguous "
                "external attempt; "
                "failure notification suppressed | key=%s messages=%d",
                key,
                len(items),
            )
            return _PendingReplyOutcome(
                succeeded=True,
                orphan=exc.task if not exc.task.done() else None,
            )
        orphan = exc.task if not exc.task.done() else None
        if orphan is not None:
            # Do not publish a timeout while the cancelled child can still
            # complete a Telegram send. The per-sender worker waits for the
            # real child and decides only after its delivery receipt is final.
            return _PendingReplyOutcome(
                succeeded=False,
                orphan=orphan,
                timed_out=True,
            )
        notified = await _notify_pending_reply_failure(items, timed_out=True)
        return _PendingReplyOutcome(
            succeeded=notified,
            timed_out=True,
        )
    except asyncio.CancelledError:
        raise
    except Exception:
        log.exception("pending batch processing failed | key=%s messages=%d", key, len(items))
        if delivery_receipt.consumed:
            log.warning(
                "pending batch failed after delivery or an ambiguous external attempt; "
                "failure notification suppressed | key=%s messages=%d",
                key,
                len(items),
            )
            return _PendingReplyOutcome(succeeded=True)
        notified = await _notify_pending_reply_failure(items, timed_out=False)
        return _PendingReplyOutcome(succeeded=notified)


async def _pending_reply_worker(key: tuple[int, int]) -> None:
    """Process one sender's batches sequentially until its queue is empty."""

    current_task = asyncio.current_task()
    active_items: list[_PendingReplyItem] = []
    active_delivery_receipt: _PendingReplyDeliveryReceipt | None = None
    try:
        while True:
            while True:
                async with _PENDING_REPLY_LOCK:
                    state = _PENDING_REPLY_BATCHES.get(key)
                    if state is None or state.task is not current_task:
                        return
                    if not state.items:
                        _PENDING_REPLY_BATCHES.pop(key, None)
                        return
                    delay_seconds = max(0.0, state.flush_at - time.monotonic())
                    wake_event = state.wake_event
                    wake_event.clear()
                if delay_seconds <= 0.0:
                    break
                try:
                    await asyncio.wait_for(wake_event.wait(), timeout=delay_seconds)
                except asyncio.TimeoutError:
                    break

            async with _PENDING_REPLY_LOCK:
                state = _PENDING_REPLY_BATCHES.get(key)
                if state is None or state.task is not current_task:
                    return
                items = list(state.items)
                state.items.clear()
                active_items = items
                settings = state.settings
                state.processing = True
                state.flush_at = 0.0

            if settings is not None and items:
                active_delivery_receipt = _PendingReplyDeliveryReceipt()
                outcome = await _process_pending_reply_batch_guarded(
                    key,
                    items,
                    settings,
                    delivery_receipt=active_delivery_receipt,
                )
                final_succeeded = bool(
                    outcome.succeeded or active_delivery_receipt.consumed
                )
                orphan = outcome.orphan
                if orphan is not None:
                    # Preserve per-sender single-flight even when a provider
                    # ignores cancellation. New messages remain queued for this
                    # key until the old task really exits, while other senders
                    # continue through independent workers.
                    try:
                        final_succeeded = bool(await asyncio.shield(orphan)) or final_succeeded
                    except asyncio.CancelledError:
                        worker_task = asyncio.current_task()
                        if worker_task is not None and worker_task.cancelling():
                            orphan.cancel()
                            raise
                        # The timed-out child acknowledged the earlier cancel;
                        # this is completion, not cancellation of the worker.
                    except Exception:
                        pass
                final_succeeded = bool(
                    final_succeeded or active_delivery_receipt.consumed
                )
                if outcome.timed_out and not final_succeeded:
                    final_succeeded = await _notify_pending_reply_failure(
                        items,
                        timed_out=True,
                    )
                # The durable receipt belongs to the real execution owner, not
                # merely its timeout wrapper. Releasing it before a
                # cancellation-resistant child exits permits replay and a
                # duplicate reply while the original task is still live.
                _finish_pending_reply_items(items, succeeded=final_succeeded)
            elif items:
                _finish_pending_reply_items(items, succeeded=False)
            active_items = []
            active_delivery_receipt = None

            async with _PENDING_REPLY_LOCK:
                state = _PENDING_REPLY_BATCHES.get(key)
                if state is None or state.task is not current_task:
                    return
                state.processing = False
                if not state.items:
                    _PENDING_REPLY_BATCHES.pop(key, None)
                    return
                if state.flush_at <= 0.0:
                    state.flush_at = time.monotonic()
    except asyncio.CancelledError:
        if active_items:
            _finish_pending_reply_items(
                active_items,
                succeeded=bool(
                    active_delivery_receipt
                    and active_delivery_receipt.consumed
                ),
            )
            active_items = []
            active_delivery_receipt = None
        raise
    finally:
        if active_items:
            _finish_pending_reply_items(active_items, succeeded=False)
        async with _PENDING_REPLY_LOCK:
            state = _PENDING_REPLY_BATCHES.get(key)
            if state is not None and state.task is current_task:
                if state.items:
                    _finish_pending_reply_items(state.items, succeeded=False)
                state.processing = False
                state.task = None


async def _enqueue_pending_reply(item: _PendingReplyItem, settings: Settings) -> tuple[int, float]:
    key = _pending_batch_key(item.group_id, item.user_id)
    now = time.monotonic()

    async with _PENDING_REPLY_LOCK:
        state = _PENDING_REPLY_BATCHES.get(key)
        if state is None:
            if len(_PENDING_REPLY_BATCHES) >= _PENDING_REPLY_MAX_SENDERS:
                raise _PendingReplyQueueFull("global pending sender limit reached")
            state = _PendingReplyBatch()
            _PENDING_REPLY_BATCHES[key] = state
        if len(state.items) >= _PENDING_REPLY_MAX_ITEMS_PER_SENDER:
            raise _PendingReplyQueueFull("per-sender pending item limit reached")
        state.settings = settings
        state.items.append(item)
        queued_count = len(state.items)
        state.flush_at = _next_pending_reply_flush_at(
            item=item,
            batch_size=queued_count,
            settings=settings,
            now=now,
            current_flush_at=state.flush_at,
        )
        delay_seconds = max(0.0, state.flush_at - now)
        state.wake_event.set()

        if state.task is None or state.task.done():
            state.task = asyncio.create_task(
                _pending_reply_worker(key),
                name=f"pending-reply:{item.group_id}:{item.user_id}",
                context=Context(),
            )

    return queued_count, delay_seconds


async def flush_pending_inbound_batches() -> None:
    async with _PENDING_REPLY_LOCK:
        now = time.monotonic()
        tasks: list[asyncio.Task[None]] = []
        shutdown_timeout = _PENDING_REPLY_SHUTDOWN_TIMEOUT_SECONDS
        for key, state in list(_PENDING_REPLY_BATCHES.items()):
            state.flush_at = now
            state.wake_event.set()
            if state.task is None or state.task.done():
                state.task = asyncio.create_task(
                    _pending_reply_worker(key),
                    name=f"pending-reply:{key[0]}:{key[1]}",
                    context=Context(),
                )
            tasks.append(state.task)
            if state.settings is not None:
                shutdown_timeout = max(
                    shutdown_timeout,
                    _pending_reply_timeout_seconds(state.settings) + 5.0,
                )

    if tasks:
        done, pending = await asyncio.wait(
            set(tasks),
            timeout=min(125.0, shutdown_timeout),
        )
        for task in done:
            try:
                task.result()
            except (asyncio.CancelledError, Exception):
                log.exception("pending batch shutdown task failed")
        if pending:
            log.error("pending batch shutdown deadline reached | active=%d", len(pending))
            for task in pending:
                task.cancel()
            await asyncio.wait(pending, timeout=1.0)

    async with _PENDING_REPLY_LOCK:
        for state in _PENDING_REPLY_BATCHES.values():
            if state.items:
                _finish_pending_reply_items(state.items, succeeded=False)
        _PENDING_REPLY_BATCHES.clear()

    compact_tasks = {task for task in _MEMORY_COMPACT_TASKS.values() if not task.done()}
    if compact_tasks:
        _, pending_compactors = await asyncio.wait(compact_tasks, timeout=5.0)
        for task in pending_compactors:
            task.cancel()
        if pending_compactors:
            await asyncio.wait(pending_compactors, timeout=1.0)

    activity_tasks = {
        state.task
        for state in _GROUP_ACTIVITY_PENDING.values()
        if state.task is not None and not state.task.done()
    }
    if activity_tasks:
        _, pending_activity = await asyncio.wait(activity_tasks, timeout=3.0)
        for task in pending_activity:
            task.cancel()
        if pending_activity:
            await asyncio.wait(pending_activity, timeout=1.0)
    _GROUP_ACTIVITY_PENDING.clear()

    orphan_tasks = {task for task in _PENDING_REPLY_ORPHAN_TASKS if not task.done()}
    for task in orphan_tasks:
        task.cancel()
    if orphan_tasks:
        await asyncio.wait(orphan_tasks, timeout=1.0)


@router.message(
    F.text
    | F.caption
    | F.sticker
    | F.voice
    | F.photo
    | F.video
    | F.animation
    | F.document
    | F.audio
    | F.video_note
    | F.contact
)
async def on_group_message(
    message: Message,
    session: AsyncSession,
    settings: Settings,
    session_factory: async_sessionmaker[AsyncSession] | None = None,
) -> None:
    if not is_group(message):
        return
    if not await ensure_group_authorized(message, session, settings):
        return

    group_id = message.chat.id
    user = message.from_user
    sender_identity = _resolve_sender_identity(message)
    user_id = sender_identity.actor_id
    # Signal the background proactive task before any DB/API await. The
    # persisted timestamp is committed immediately below; the in-process
    # revision closes the shorter pre-commit race during topic generation.
    note_group_activity(group_id)
    activity_kwargs: dict[str, Any] = {
        "group_id": group_id,
        "title": message.chat.title or "",
        "settings": settings,
    }
    if session_factory is not None:
        activity_kwargs["session_factory"] = session_factory
    group_settings = await _record_group_activity_cas(session, **activity_kwargs)

    text, msg_type = extract_message_text(message)
    if not text:
        return
    input_text = text
    flow_started = time.perf_counter()
    bot_me = await message.bot.me()

    warn_target = _build_warn_target(
        user=user,
        actor_id=user_id,
        display_name=sender_identity.display_name,
        sender_username=sender_identity.username,
        sender_is_chat=sender_identity.is_chat,
    )

    # Other bots' messages never enter the reply pipeline. Guest-mode ad bots
    # abuse this blind spot, so when bot screening is enabled their messages
    # are moderated until the bot earns the per-group whitelist.
    raw_bot_sender = bool(message.from_user and message.from_user.is_bot and not _uses_sender_chat_identity(message))
    if raw_bot_sender:
        bot_sender_id = int(message.from_user.id)
        if (
            not getattr(settings.moderation, "enabled", False)
            or not getattr(settings.moderation, "bot_screening_enabled", False)
            or bot_sender_id == int(getattr(bot_me, "id", 0) or 0)
        ):
            return
        if await is_bot_whitelisted(session, group_id, bot_sender_id):
            log.info(
                "[%s] bot screening skipped | reason=whitelisted bot=%s",
                group_id,
                bot_sender_id,
            )
            return
        llm = LLMService(
            settings.bot.main_model,
            settings.bot.decision_model,
            settings.bot.compress_model,
            moderation=settings.bot.moderation_model,
            vision=settings.bot.vision_model,
            embed=settings.bot.embed_model,
            max_context_tokens=settings.bot.max_context_tokens,
        )
        input_text, bot_vision_text = await _append_image_context(
            message, llm, input_text, msg_type
        )
        # Placeholder-only media ("[voice]", "[video]", failed-vision images)
        # carry nothing to judge; a trivially-clean verdict must not credit a
        # pass toward the permanent whitelist, so skip them entirely.
        has_screenable_content = (
            msg_type == "text"
            or "caption" in msg_type
            or msg_type in {"contact", "document"}
            or bool(bot_vision_text)
        )
        if not has_screenable_content:
            log.info(
                "[%s] bot screening skipped | reason=no_screenable_content bot=%s type=%s",
                group_id,
                bot_sender_id,
                msg_type,
            )
            return
        await _screen_bot_sender_message(
            moderation=ModerationService(settings.moderation, llm),
            session=session,
            settings=settings,
            message=message,
            group_id=group_id,
            bot_id=bot_sender_id,
            input_text=input_text,
            warn_target=warn_target,
            flow_started=flow_started,
        )
        return

    # Placeholder-only videos still bypass the text pipeline. Captions carry
    # user-controlled text and must pass moderation before we stop processing.
    if msg_type in {"video", "video_note"}:
        log.info("[%s]【流程】媒体旁路 | 类型=%s", group_id, msg_type)
        return
    media_moderation_only = msg_type == "video_caption"

    mute_all_replies = bool(group_settings.get("mute_all_replies", False))

    sender_username = sender_identity.username
    display_name = sender_identity.display_name
    sender_is_owner = bool(user and not sender_identity.is_chat and is_super_admin_user_id(user.id, settings))
    sender_chat = getattr(message, "sender_chat", None)
    sender_is_group_identity = bool(
        sender_identity.is_chat and sender_chat and getattr(sender_chat, "id", None) == group_id
    )
    sender_is_tg_admin = sender_is_group_identity or await _is_user_admin_cached(message)
    owner_flag = "yes" if sender_is_owner else "no"
    tg_admin_flag = "yes" if sender_is_tg_admin else "no"
    trusted_source = "tg_admin" if sender_is_tg_admin else "none"
    sender_username_tag = f"@{sender_username}" if sender_username else "(none)"
    # Keep id/admin flags at the front so trust metadata survives truncation/compression.
    user_tag = (
        f"id:{user_id} username:{sender_username_tag} "
        f"is_owner:{owner_flag} is_tg_admin:{tg_admin_flag} trusted_source:{trusted_source} "
        f"name:{display_name}"
    )

    llm = LLMService(
        settings.bot.main_model,
        settings.bot.decision_model,
        settings.bot.compress_model,
        moderation=settings.bot.moderation_model,
        vision=settings.bot.vision_model,
        embed=settings.bot.embed_model,
        max_context_tokens=settings.bot.max_context_tokens,
    )

    input_text, vision_text = await _append_image_context(message, llm, input_text, msg_type)
    reply_context = await _build_reply_context_for_llm(message, llm)
    if reply_context:
        input_text = f"{input_text}\n{reply_context}"
    if msg_type == "sticker":
        try:
            learned = await sticker_library.learn_from_message(
                session,
                group_id,
                message,
                vision_description=vision_text,
            )
            if learned:
                log.info(
                    "[%s] sticker learned: file_id=%s desc=%s seen=%s",
                    group_id,
                    str(learned.get("file_id", ""))[:32],
                    str(learned.get("description", ""))[:80],
                    learned.get("seen_count", 1),
                )
        except Exception:
            await session.rollback()
            log.exception("[%s] sticker learning failed", group_id)


    log.info("[%s]【流程】审核 | 开始", group_id)
    await _best_effort_commit(
        session,
        group_id=group_id,
        context="sticker_learning",
    )
    if not await _fresh_group_authorized_for_moderation(session, group_id):
        return
    moderation_started = time.perf_counter()
    if settings.moderation.enabled:
        mod = ModerationService(settings.moderation, llm)
        auto_exempt_moderation = sender_is_owner or sender_is_tg_admin
        if sender_is_owner:
            auto_exempt_reason = "owner_auto_exempt"
        elif sender_is_tg_admin:
            auto_exempt_reason = "tg_admin_auto_exempt"
        else:
            auto_exempt_reason = ""
        manual_exempt = False
        if auto_exempt_moderation:
            log.info(
                "[%s] moderation skipped | reason=%s user=%s",
                group_id,
                auto_exempt_reason,
                user_id,
            )
        else:
            manual_exempt = await mod.is_user_exempt(session, group_id, user_id)
            if manual_exempt:
                log.info("[%s] moderation skipped | reason=manual_exempt user=%s", group_id, user_id)

        # Profile (name/username/bio) screening runs only on join and during
        # patrol; ordinary messages are moderated on their content alone.

        if not auto_exempt_moderation and not manual_exempt:
            verdict = await mod.evaluate(session, group_id, input_text)
            violated = verdict.violated
            reason = verdict.reason
            rule = verdict.rule
            log.info(
                "[%s]【流程】审核 | 完成 | 违规=%s | 置信度=%.2f | 原因=%s | 耗时=%dms",
                group_id,
                violated,
                verdict.confidence,
                reason,
                int((time.perf_counter() - moderation_started) * 1000),
            )
            if not await _fresh_group_authorized_for_moderation(session, group_id):
                return
            if violated:
                action = str(rule.action if rule else "warn").strip().lower()
                if action not in {"warn", "delete", "ban"}:
                    action = "warn"

                message_deleted = False
                high_confidence = mod.is_high_confidence(verdict)
                if not high_confidence and not sender_identity.is_chat:
                    if moderation_challenge_ready(settings):
                        async with _moderation_user_lock(group_id, user_id):
                            if not await _claim_current_moderation_verdict(
                                session,
                                group_id=group_id,
                                user_id=user_id,
                                verdict=verdict,
                            ):
                                return
                            challenge_violation = await mod.record_violation(
                                session,
                                group_id,
                                user_id,
                                input_text,
                                "challenge",
                                rule,
                                source_message_id=_source_message_id(message),
                            )
                            await session.flush()
                            await session.commit()
                            try:
                                await message.delete()
                                message_deleted = True
                            except Exception:
                                log.warning(
                                    "[%s] low-confidence message delete failed | user=%s",
                                    group_id,
                                    user_id,
                                )
                            await _refresh_violation_notice_state(
                                session,
                                challenge_violation,
                            )
                            challenged = (
                                getattr(challenge_violation, "notice_sent_at", None)
                                is not None
                            )
                            if not challenged:
                                challenged = await begin_moderation_challenge(
                                    bot=message.bot,
                                    session=session,
                                    settings=settings,
                                    group_id=group_id,
                                    user_id=user_id,
                                    display_name=display_name,
                                    bot_username=getattr(bot_me, "username", "") or "",
                                    reason=reason,
                                    session_factory=session_factory,
                                )
                                if challenged:
                                    challenge_violation.notice_sent_at = (
                                        now_shanghai_naive()
                                    )
                                    await session.commit()
                                else:
                                    # The challenge was not created, so this
                                    # provisional event must not monopolize the
                                    # source-message idempotency key.  The
                                    # deterministic fallback below will create
                                    # the real warn/delete/ban event instead.
                                    delete_row = getattr(session, "delete", None)
                                    if callable(delete_row):
                                        await delete_row(challenge_violation)
                                        await session.commit()
                        if challenged:
                            log.info(
                                "[%s]【结束】审核质询 | user=%s | confidence=%.2f | 已删=%s | 总耗时=%dms",
                                group_id,
                                user_id,
                                verdict.confidence,
                                message_deleted,
                                int((time.perf_counter() - flow_started) * 1000),
                            )
                            return
                    if not verdict.conclusive:
                        log.warning(
                            "[%s] inconclusive moderation confidence; no direct action | user=%s",
                            group_id,
                            user_id,
                        )
                        return
                    log.warning(
                        "[%s] low-confidence challenge unavailable; falling back to rule action | "
                        "user=%s confidence=%.2f action=%s",
                        group_id,
                        user_id,
                        verdict.confidence,
                        action,
                    )

                if not high_confidence and sender_identity.is_chat:
                    if not verdict.conclusive:
                        log.warning(
                            "[%s] inconclusive sender-chat moderation; no direct action | "
                            "sender_chat=%s",
                            group_id,
                            user_id,
                        )
                        return
                    log.warning(
                        "[%s] sender-chat cannot complete a challenge; applying rule action | "
                        "sender_chat=%s confidence=%.2f action=%s",
                        group_id,
                        user_id,
                        verdict.confidence,
                        action,
                    )

                if action == "ban" and sender_identity.is_chat:
                    # Channel identities are chat IDs, not users. Passing one
                    # to ban_chat_member always fails; use the dedicated Bot
                    # API method and avoid user-warning/callback workflows.
                    async with _moderation_user_lock(group_id, user_id):
                        if not await _claim_current_moderation_verdict(
                            session,
                            group_id=group_id,
                            user_id=user_id,
                            verdict=verdict,
                        ):
                            return
                        source_message_id = _source_message_id(message)
                        violation = await mod.record_violation(
                            session,
                            group_id,
                            user_id,
                            input_text,
                            "ban_applied",
                            rule,
                            source_message_id=source_message_id,
                        )
                        flush = getattr(session, "flush", None)
                        if callable(flush):
                            await flush()
                        await session.commit()
                        prior_ban_result = _violation_nullable_bool(
                            violation,
                            "ban_enforced",
                        )
                        if prior_ban_result is not True:
                            attempted_ban = False
                            ban_retryable = True
                            ban_sender_chat = getattr(
                                message.chat,
                                "ban_sender_chat",
                                None,
                            )
                            if callable(ban_sender_chat):
                                try:
                                    ban_result = await ban_sender_chat(user_id)
                                    attempted_ban = ban_result is not False
                                except Exception as exc:
                                    ban_retryable = not (
                                        telegram_ban_failure_is_deterministic(exc)
                                    )
                                    log.exception(
                                        "[%s] sender-chat ban failed | sender_chat=%s",
                                        group_id,
                                        user_id,
                                    )
                            try:
                                if not message_deleted:
                                    await message.delete()
                            except Exception:
                                log.warning(
                                    "[%s] sender-chat moderation delete failed | sender_chat=%s",
                                    group_id,
                                    user_id,
                                )
                            ban_enforced, _result_changed = (
                                await _persist_violation_ban_result(
                                    session,
                                    violation,
                                    enforced=attempted_ban,
                                )
                            )
                            violation.action_taken = (
                                "ban_applied" if ban_enforced else "delete"
                            )
                            await session.commit()
                        else:
                            ban_enforced = bool(prior_ban_result)
                            ban_retryable = False
                        notice = _build_moderation_notice(
                            warn_target=warn_target,
                            reason=reason,
                            rule=rule,
                            hit_action="ban" if ban_enforced else "delete",
                            should_ban=ban_enforced,
                        )
                        await _send_moderation_notice_once_locked(
                            session=session,
                            violation=violation,
                            message=message,
                            notice=notice,
                            auto_delete_seconds=configured_auto_delete_seconds(
                                settings,
                                "moderation",
                            ),
                        )
                        if not ban_enforced and ban_retryable:
                            request_current_update_retry()
                    return

                if action == "warn":
                    async with _moderation_user_lock(group_id, user_id):
                        if not await _claim_current_moderation_verdict(
                            session,
                            group_id=group_id,
                            user_id=user_id,
                            verdict=verdict,
                        ):
                            return
                        violation = await mod.record_violation(
                            session,
                            group_id,
                            user_id,
                            input_text,
                            "warn",
                            rule,
                            source_message_id=_source_message_id(message),
                        )
                        await session.flush()
                        violation_id = int(violation.id)
                        await session.commit()
                        notice = _build_moderation_notice(
                            warn_target=warn_target,
                            reason=reason,
                            rule=rule,
                            hit_action=action,
                        )
                        await _send_moderation_notice_once_locked(
                            session=session,
                            violation=violation,
                            message=message,
                            notice=notice,
                            auto_delete_seconds=configured_auto_delete_seconds(
                                settings,
                                "moderation",
                            ),
                            reply_markup=(
                                None
                                if sender_identity.is_chat
                                else _build_moderation_action_keyboard(violation_id)
                            ),
                        )
                    log.info(
                        "[%s]【结束】审核拦截 | 动作=warn | 已回复=是 | 总耗时=%dms",
                        group_id,
                        int((time.perf_counter() - flow_started) * 1000),
                    )
                    return

                if action == "delete":
                    async with _moderation_user_lock(group_id, user_id):
                        if not await _claim_current_moderation_verdict(
                            session,
                            group_id=group_id,
                            user_id=user_id,
                            verdict=verdict,
                        ):
                            return
                        violation = await mod.record_violation(
                            session,
                            group_id,
                            user_id,
                            input_text,
                            "delete",
                            rule,
                            source_message_id=_source_message_id(message),
                        )
                        await session.flush()
                        await session.commit()
                        try:
                            if not message_deleted:
                                await message.delete()
                        except Exception:
                            pass
                        notice = _build_moderation_notice(
                            warn_target=warn_target,
                            reason=reason,
                            rule=rule,
                            hit_action=action,
                        )
                        await _send_moderation_notice_once_locked(
                            session=session,
                            violation=violation,
                            message=message,
                            notice=notice,
                            auto_delete_seconds=configured_auto_delete_seconds(
                                settings,
                                "moderation",
                            ),
                        )
                    log.info(
                        "[%s]【结束】审核拦截 | 动作=delete | 已回复=是 | 总耗时=%dms",
                        group_id,
                        int((time.perf_counter() - flow_started) * 1000),
                    )
                    return

                warn_threshold = max(1, settings.moderation.warn_threshold)
                counted_outcome = await _apply_counted_moderation_ban(
                        moderation=mod,
                        session=session,
                        message=message,
                        group_id=group_id,
                        user_id=user_id,
                        input_text=input_text,
                        rule=rule,
                        message_deleted=message_deleted,
                        verdict=verdict,
                    )
                if counted_outcome is None:
                    return
                (
                    count,
                    violation,
                    ban_enforced,
                    ban_failure_note,
                    ban_retryable,
                ) = counted_outcome
                violation_id = int(violation.id)
                notice = _build_moderation_notice(
                    warn_target=warn_target,
                    reason=reason,
                    rule=rule,
                    hit_action=action,
                    count=count,
                    threshold=warn_threshold,
                    should_ban=ban_enforced,
                    failure_note=ban_failure_note,
                )
                async with _moderation_user_lock(group_id, user_id):
                    await _send_moderation_notice_once_locked(
                        session=session,
                        violation=violation,
                        message=message,
                        notice=notice,
                        auto_delete_seconds=configured_auto_delete_seconds(
                            settings,
                            "moderation",
                        ),
                        reply_markup=_build_moderation_action_keyboard(violation_id),
                    )
                if ban_retryable:
                    request_current_update_retry()
                log.info(
                    "[%s]【结束】审核拦截 | 动作=ban | 封禁=%s | 警告=%s/%s | 已回复=是 | 总耗时=%dms",
                    group_id,
                    ban_enforced,
                    count,
                    warn_threshold,
                    int((time.perf_counter() - flow_started) * 1000),
                )
                return
    else:
        log.info("[%s]【流程】审核 | 关闭", group_id)

    if media_moderation_only:
        log.info("[%s]【结束】视频 caption 已完成审核", group_id)
        return

    # "@admin" summons run after moderation (violating text never pings the
    # admins) but before the reply-mute gates: muting AI replies must not
    # disable the reporting channel.
    if (
        (msg_type == "text" or "caption" in msg_type)
        and not sender_identity.is_chat
        and is_call_admin_trigger(text if msg_type == "text" else message.caption or "")
    ):
        called = await handle_call_admin(
            message,
            session,
            settings,
            group_settings=group_settings,
            caller_id=user_id,
            caller_name=display_name,
        )
        if called:
            # The summon replaces the AI reply, but the report still belongs
            # to group history like any other user message.
            await memory_holder.get().add_message(
                group_id,
                "user",
                f"[{user_tag}] {input_text}",
                user_id=user_id,
                sender_name=display_name,
                message_type=msg_type,
                message_id=str(message.message_id),
                created_at=message.date,
                defer_persistence=True,
            )
            log.info(
                "[%s]【结束】呼叫管理员 | user=%s | 总耗时=%dms",
                group_id,
                user_id,
                int((time.perf_counter() - flow_started) * 1000),
            )
            return

    if mute_all_replies:
        log.info(
            "[%s]【结束】回复静默 | 范围=all | 仅审核不回复 | 耗时=%dms",
            group_id,
            int((time.perf_counter() - flow_started) * 1000),
        )
        return

    mute_stmt = select(ReplyMute.id).where(
        ReplyMute.group_id == group_id,
        ReplyMute.user_id == user_id,
    )
    mute_result = await session.execute(mute_stmt)
    is_muted_user = mute_result.scalar_one_or_none() is not None
    if is_muted_user:
        log.info(
            "[%s]【结束】回复静默 | 用户=%s | 仅审核不回复 | 耗时=%dms",
            group_id,
            user_id,
            int((time.perf_counter() - flow_started) * 1000),
        )
        return

    # Keyword auto replies run after moderation and the mute gates, so a
    # violating message never triggers a canned answer and muted scopes stay
    # silent. Only real user text (incl. captions) is matched — synthesized
    # placeholders like "[image]" must not trip contains-rules. A hit
    # replaces the AI reply; memory indexing still happens.
    keyword_rule = None
    if msg_type == "text" or "caption" in msg_type:
        keyword_subject = text if msg_type == "text" else message.caption or ""
        try:
            keyword_rule = await find_keyword_reply(session, group_id, keyword_subject)
        except Exception:
            await session.rollback()
            log.exception("[%s] keyword reply lookup failed", group_id)

    memory = memory_holder.get()

    await _best_effort_commit(
        session,
        group_id=group_id,
        context="pre_memory_index",
    )

    should_index_user_memory = msg_type != "contact"
    memory_entry = ""
    if should_index_user_memory:
        memory_entry = f"[{user_tag}] {input_text}"
        await memory.add_message(
            group_id,
            "user",
            memory_entry,
            user_id=user_id,
            sender_name=display_name,
            message_type=msg_type,
            message_id=str(message.message_id),
            created_at=message.date,
            defer_persistence=True,
        )
        _schedule_memory_compaction(memory, group_id)
    else:
        log.info("[%s] memory indexing skipped | reason=contact_message", group_id)

    if msg_type == "text" and not sender_identity.is_chat:
        try:
            style_service = SpeechStyleService(llm)
            collected = await style_service.collect_sample(
                session,
                group_id=group_id,
                user_id=user_id,
                text=text,
            )
            if collected:
                await _best_effort_commit(
                    session,
                    group_id=group_id,
                    context="speech_style_sample",
                )
        except Exception:
            await session.rollback()
            log.exception("[%s] speech style collection failed", group_id)

    if keyword_rule is not None:
        sent_ok = await send_keyword_reply(message, keyword_rule, settings)
        log.info(
            "[%s]【结束】关键词回复 | 规则=%s 关键词=%s 已发送=%s | 总耗时=%dms",
            group_id,
            keyword_rule.id,
            str(keyword_rule.keyword or "")[:40],
            sent_ok,
            int((time.perf_counter() - flow_started) * 1000),
        )
        return

    explicit_mention = has_explicit_bot_mention(message, bot_me.username or "", bot_me.id)
    mentioned = is_bot_mentioned(message, bot_me.username or "", bot_me.id)
    is_reply = is_reply_message(message)
    reply_to_bot = is_reply_to_bot(message, bot_me.username or "", bot_me.id)
    reply_to_other = is_reply and not reply_to_bot
    mention_other = mentions_other_user(message, bot_me.username or "", bot_me.id)

    pending_item = _PendingReplyItem(
        message=message,
        group_id=group_id,
        user_id=user_id,
        input_text=input_text,
        msg_type=msg_type,
        sender_username=sender_username,
        sender_is_owner=sender_is_owner,
        sender_is_tg_admin=sender_is_tg_admin,
        user_tag=user_tag,
        explicit_mention=explicit_mention,
        mentioned=mentioned,
        is_reply=is_reply,
        reply_to_bot=reply_to_bot,
        reply_to_other=reply_to_other,
        mention_other=mention_other,
        memory_entry=memory_entry,
        update_completion=current_update_completion(),
    )
    if pending_item.update_completion is not None:
        # Register ownership before publishing the item. A zero-delay worker
        # can otherwise finish between enqueue and ``defer()``, completing the
        # durable receipt before this detached reply is visible to it.
        pending_item.update_completion.defer()
    try:
        queued_count, queued_delay = await _enqueue_pending_reply(
            pending_item,
            settings,
        )
    except _PendingReplyQueueFull:
        direct_overload = _is_strong_pending_reply_signal(pending_item)
        overload_handled = not direct_overload
        log.warning(
            "[%s] pending batch rejected by backpressure | user=%s direct=%s",
            group_id,
            user_id,
            direct_overload,
        )
        if direct_overload:
            try:
                overload_handled = bool(await _await_hard_deadline(
                    send_reply(
                        message,
                        "当前请求较多，请稍后再试。",
                        delivery_mode="reply",
                        reply_to_message_id=int(message.message_id or 0) or None,
                        stream=False,
                        disable_link_preview=bool(
                            getattr(settings.bot, "disable_link_preview", True)
                        ),
                    ),
                    timeout_seconds=8.0,
                ))
            except Exception:
                log.exception(
                    "[%s] pending overload notification failed | user=%s",
                    group_id,
                    user_id,
                )
        if pending_item.update_completion is not None:
            pending_item.update_completion.finish(overload_handled)
        if not overload_handled:
            # No reply work was accepted and even the visible overload fallback
            # failed. Let Telegram retry instead of acknowledging silent loss.
            raise
        return
    log.info(
        "[%s] pending batch queued | user=%s size=%d delay=%.2fs mention=%s mention_other=%s reply=%s reply_bot=%s reply_other=%s type=%s elapsed=%dms",
        group_id,
        user_id,
        queued_count,
        queued_delay,
        mentioned,
        mention_other,
        is_reply,
        reply_to_bot,
        reply_to_other,
        msg_type,
        int((time.perf_counter() - flow_started) * 1000),
    )
    return
