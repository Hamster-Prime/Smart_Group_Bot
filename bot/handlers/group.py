from __future__ import annotations

import asyncio
import base64
import html
import io
import json
import logging
import re
import time
from collections import Counter
from dataclasses import dataclass, field
from typing import Any

from aiogram import F, Router
from aiogram.types import Message
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.ext.asyncio import AsyncSession

from bot.config import Settings
from bot.db.models import Group, ModerationRule, ReplyMute
from bot.db.sqlite_session import is_database_locked_error
from bot.services import memory_holder
from bot.services.at_reply import is_at_reply_enabled
from bot.services.authz import ensure_group_authorized, is_group_admin_authorized, is_super_admin_user_id
from bot.services.casual import CasualService
from bot.services.chat_bridge import (
    ChatBridgeService,
    compose_chat_bridge_message,
    extract_chat_bridge_target_username,
    get_chat_bridge_state,
    is_bot_style_name,
    normalize_chat_bridge_username,
    parse_incoming_chat_bridge_message,
    set_chat_bridge_target,
)
from bot.services.decision import DecisionService
from bot.services.doubao_tts import DoubaoTTSService, is_tts_always_enabled, is_tts_tool_enabled, normalize_tts_mode
from bot.services.manage_intent import GroupIntent
from bot.services.llm import LLMService
from bot.services.moderation import ModerationService
from bot.services.reply_mode import ReplyModeService
from bot.services.reply_output import ReplyMessageSpec, parse_reply_output
from bot.services.scheduled_tasks import (
    cancel_scheduled_task,
    create_scheduled_task,
    format_due_at_local,
    format_task_summary,
    match_scheduled_task_for_cancel,
    record_group_activity,
)
from bot.services.skills import SkillService
from bot.services.sticker_library import sticker_library
from bot.utils.telegram import (
    answer_with_auto_delete,
    extract_reply_context,
    extract_message_text,
    has_explicit_bot_mention,
    is_bot_mentioned,
    is_user_admin,
    is_reply_to_bot,
    is_group,
    is_reply_message,
    mentions_other_user,
    sanitize_outgoing_text,
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

    if user and getattr(user, "username", None):
        return f"@{user.username}"

    label = (getattr(user, "full_name", None) or str(actor_id)).strip() if user else str(actor_id)
    return f'<a href="tg://user?id={actor_id}">{html.escape(label or str(actor_id))}</a>'


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


def _management_reply_auto_delete_minutes(intent: GroupIntent, settings: Settings) -> int:
    if intent.intent == "memory_manage" and (intent.memory_action or "").strip().lower() == "add":
        return 0
    return settings.bot.auto_delete_minutes


def _build_moderation_notice(
    warn_target: str,
    reason: str,
    rule: ModerationRule | None,
    *,
    hit_action: str,
    count: int | None = None,
    threshold: int | None = None,
    should_ban: bool = False,
) -> str:
    rule_type_labels = {
        "keyword": "关键词",
        "regex": "正则",
        "llm": "语义",
    }
    reason_text = html.escape((reason or "命中群审核规则（AI判定）").strip())
    action_norm = (hit_action or "").strip().lower()

    title = "AI审查警告"
    action_result = "已发出警告"
    show_warning_count = False
    if action_norm == "delete":
        title = "AI审查删除"
        action_result = "已删除违规消息"
    elif action_norm == "ban":
        title = "AI审查自动封禁" if should_ban else "AI审查警告"
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

    lines = [
        f"<b>{title}</b>",
        "————————",
        f"<b>用户</b>: {warn_target}",
    ]
    if show_warning_count and count is not None and threshold is not None:
        lines.append(f"<b>警告次数</b>: {count}/{threshold}")
    lines.extend(
        [
            f"<b>原因</b>: {reason_text}",
            f"<b>依据规则</b>: {rule_ref}",
            f"<b>处理结果</b>: {action_result}",
        ]
    )
    return "\n".join(lines)


async def _ensure_group_row(session: AsyncSession, group_id: int, title: str) -> Group:
    """Ensure group metadata row exists, tolerating concurrent inserts."""
    existing = await session.get(Group, group_id)
    if existing:
        if title and existing.title != title:
            existing.title = title
        if existing.settings is None:
            existing.settings = {}
        return existing

    # Avoid failing the whole update when two messages from the same new group race.
    try:
        async with session.begin_nested():
            created = Group(id=group_id, title=title or "", settings={})
            session.add(created)
            await session.flush()
            return created
    except IntegrityError:
        log.debug("group row already inserted concurrently: group_id=%s", group_id)
        existing = await session.get(Group, group_id)
        if existing:
            if title and existing.title != title:
                existing.title = title
            if existing.settings is None:
                existing.settings = {}
            return existing

    log.warning("group row unavailable after conflict fallback: group_id=%s", group_id)
    return Group(id=group_id, title=title or "", settings={})


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


def _extract_image_file_info(message: Message) -> tuple[str, str] | None:
    """Return (file_id, mime) for image-like messages."""
    if message.photo:
        idx = max(0, len(message.photo) - 2)
        return message.photo[idx].file_id, "image/jpeg"

    if message.document and (message.document.mime_type or "").startswith("image/"):
        return message.document.file_id, (message.document.mime_type or "image/jpeg")

    if message.animation:
        mime = message.animation.mime_type or "image/gif"
        if mime.lower().startswith("video/"):
            return None
        return message.animation.file_id, mime

    if message.sticker:
        is_animated = bool(getattr(message.sticker, "is_animated", False))
        is_video = bool(getattr(message.sticker, "is_video", False))
        if not is_animated and not is_video:
            return message.sticker.file_id, "image/webp"

        thumb = getattr(message.sticker, "thumbnail", None)
        if thumb and getattr(thumb, "file_id", None):
            return thumb.file_id, "image/jpeg"

    return None


async def _build_telegram_image_data_uri(message: Message) -> str:
    info = _extract_image_file_info(message)
    if not info:
        return ""

    file_id, mime = info
    tg_file = await message.bot.get_file(file_id)
    if not tg_file.file_path:
        return ""

    buf = io.BytesIO()
    await message.bot.download_file(tg_file.file_path, destination=buf)
    raw = buf.getvalue()
    if not raw:
        return ""

    log.info("【视觉】图片下载完成 | 大小=%dB | 类型=%s", len(raw), mime)
    b64 = base64.b64encode(raw).decode("ascii")
    return f"data:{mime};base64,{b64}"


async def _build_telegram_image_url(message: Message) -> str:
    """Fallback URL mode in case data-uri vision fails."""
    info = _extract_image_file_info(message)
    if not info:
        return ""

    file_id, _ = info
    tg_file = await message.bot.get_file(file_id)
    if not tg_file.file_path:
        return ""

    token = message.bot.token
    return f"https://api.telegram.org/file/bot{token}/{tg_file.file_path}"


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
        vision_text = (await llm.vision_describe(data_uri, vision_prompt)).strip()

    if not vision_text:
        image_url = await _build_telegram_image_url(message)
        if image_url:
            vision_text = (await llm.vision_describe(image_url, vision_prompt)).strip()

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


@dataclass(slots=True)
class _PendingReplyBatch:
    items: list[_PendingReplyItem] = field(default_factory=list)
    version: int = 0
    task: asyncio.Task[None] | None = None
    settings: Settings | None = None
    flush_at: float = 0.0


@dataclass(slots=True)
class _ReplyDeliveryPlan:
    text: str
    delivery_mode: str
    reply_to_message_id: int | None = None


_PENDING_REPLY_LOCK = asyncio.Lock()
_PENDING_REPLY_BATCHES: dict[tuple[int, int], _PendingReplyBatch] = {}


def _pending_batch_key(group_id: int, user_id: int) -> tuple[int, int]:
    return group_id, user_id


def _build_merged_user_text(items: list[_PendingReplyItem]) -> str:
    texts = [(item.input_text or "").strip() for item in items if (item.input_text or "").strip()]
    if not texts:
        return ""
    if len(texts) == 1:
        return texts[0]
    return "\n".join(texts)


def _build_merged_context(items: list[_PendingReplyItem]) -> str:
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
    return "\n".join(lines)


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
        delay_seconds = min(base_delay, 0.8)
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


def _build_chat_bridge_memory_entry(
    *,
    sender_id: int,
    sender_username: str,
    display_name: str,
    text: str,
) -> str:
    sender_username_tag = f"@{sender_username}" if sender_username else "(none)"
    return (
        f"id:{sender_id} username:{sender_username_tag} "
        f"is_owner:no is_tg_admin:no trusted_source:none "
        f"name:{display_name} {text}"
    )


def _chat_bridge_fallback_body(mode: str) -> str:
    if mode == "start":
        return "最近群里这个话题你怎么看？"
    return "你刚才那句展开说说？"


async def _generate_chat_bridge_body(
    *,
    settings: Settings,
    group_id: int,
    peer_username: str,
    mode: str,
    current_message: str,
    message: Message,
) -> str:
    memory = memory_holder.get()
    llm = LLMService(
        settings.bot.chat_bridge_model,
        settings.bot.decision_model,
        settings.bot.compress_model,
        moderation=settings.bot.moderation_model,
        vision=settings.bot.vision_model,
        embed=settings.bot.embed_model,
        max_context_tokens=settings.bot.max_context_tokens,
    )
    bridge = ChatBridgeService(llm, settings=settings)
    history = await memory.get_history_for_llm(
        group_id,
        prompt_payload_builder=lambda candidate_history: bridge.build_prompt_payload(
            mode=mode,
            peer_username=peer_username,
            current_message=current_message,
            history=candidate_history,
        ),
    )

    async with typing_action(message, enabled=settings.bot.enable_typing):
        body = await bridge.reply(
            mode=mode,
            peer_username=peer_username,
            current_message=current_message,
            history=history,
        )

    cleaned = sanitize_outgoing_text(body)
    if cleaned:
        return cleaned
    return _chat_bridge_fallback_body(mode)


async def _send_chat_bridge_turn(
    *,
    settings: Settings,
    message: Message,
    group_id: int,
    peer_username: str,
    mode: str,
    current_message: str,
) -> bool:
    reply_body = await _generate_chat_bridge_body(
        settings=settings,
        group_id=group_id,
        peer_username=peer_username,
        mode=mode,
        current_message=current_message,
        message=message,
    )
    outgoing = compose_chat_bridge_message(peer_username, reply_body)
    if not outgoing:
        outgoing = compose_chat_bridge_message(peer_username, _chat_bridge_fallback_body(mode))
    if not outgoing:
        return False

    sent_ok = await send_reply(
        message,
        outgoing,
        delivery_mode="message",
        stream=settings.bot.enable_streaming,
        stream_chunk_size=settings.bot.stream_chunk_size,
        stream_interval=settings.bot.stream_edit_interval_sec,
        auto_delete_minutes=0,
    )
    if not sent_ok:
        return False

    memory = memory_holder.get()
    await memory.add_message(
        group_id,
        "assistant",
        outgoing,
        sender_name="bot",
        message_type="assistant_reply",
    )
    await memory.compact_if_needed(group_id)
    return True


async def _maybe_handle_chat_bridge_target_reply(
    *,
    message: Message,
    session: AsyncSession,
    settings: Settings,
    group_row: Group,
    sender_identity: _SenderIdentity,
    input_text: str,
    my_username: str,
) -> bool:
    state = get_chat_bridge_state(group_row.settings)
    if not state.waiting_for_target:
        return False
    if sender_identity.is_chat:
        return False

    user = message.from_user
    if user is None or user.is_bot or not is_super_admin_user_id(user.id, settings):
        return False
    if state.pending_admin_id and user.id != state.pending_admin_id:
        return False

    reply = getattr(message, "reply_to_message", None)
    prompt_message_id = int(getattr(reply, "message_id", 0) or 0)
    if state.prompt_message_id and prompt_message_id != state.prompt_message_id:
        return False

    target_username = extract_chat_bridge_target_username(input_text)
    if not target_username:
        await answer_with_auto_delete(
            message,
            "<b>/chat 对话设置</b>\n请回复目标 bot 用户名，格式例如：@examplebot",
            auto_delete_minutes=0,
        )
        return True

    if target_username == normalize_chat_bridge_username(my_username):
        await answer_with_auto_delete(
            message,
            "<b>/chat 对话设置</b>\n目标 bot 不能是当前 bot 自己，请换一个用户名。",
            auto_delete_minutes=0,
        )
        return True

    if not is_bot_style_name(target_username):
        await answer_with_auto_delete(
            message,
            "<b>/chat 对话设置</b>\n目标用户名需要明显是 bot 账号，并以 bot 结尾，例如：@examplebot",
            auto_delete_minutes=0,
        )
        return True

    group_row.settings = set_chat_bridge_target(group_row.settings, target_username)
    await _best_effort_commit(
        session,
        group_id=message.chat.id,
        context="chat_bridge_target_set",
    )

    sent_ok = await _send_chat_bridge_turn(
        settings=settings,
        message=message,
        group_id=message.chat.id,
        peer_username=target_username,
        mode="start",
        current_message="",
    )
    if not sent_ok:
        await answer_with_auto_delete(
            message,
            "<b>/chat 对话设置</b>\n已记录目标 bot，但首条 /chat 消息发送失败，请稍后重试。",
            auto_delete_minutes=0,
        )
    return True


async def _maybe_handle_chat_bridge_turn(
    *,
    message: Message,
    settings: Settings,
    sender_identity: _SenderIdentity,
    input_text: str,
    group_row: Group,
    my_username: str,
) -> bool:
    state = get_chat_bridge_state(group_row.settings)
    incoming = parse_incoming_chat_bridge_message(input_text)
    if incoming is None or not state.active:
        return False

    my_username_norm = normalize_chat_bridge_username(my_username)
    if not my_username_norm or incoming.target_username != my_username_norm:
        return False

    user = message.from_user
    sender_username = normalize_chat_bridge_username(sender_identity.username)
    if user is None or not user.is_bot or not sender_username:
        return False
    if sender_username != state.target_username:
        log.info(
            "[%s] chat bridge ignored | sender=@%s active_target=@%s",
            message.chat.id,
            sender_username or "(none)",
            state.target_username or "(none)",
        )
        return False
    if not is_bot_style_name(sender_username, sender_identity.display_name, getattr(user, "full_name", "")):
        return False

    memory = memory_holder.get()
    await memory.add_message(
        message.chat.id,
        "user",
        _build_chat_bridge_memory_entry(
            sender_id=sender_identity.actor_id,
            sender_username=sender_username,
            display_name=sender_identity.display_name,
            text=input_text,
        ),
        user_id=sender_identity.actor_id,
        sender_name=sender_identity.display_name,
        message_type="chat_bridge_inbound",
        message_id=str(message.message_id),
        created_at=message.date,
    )
    await memory.compact_if_needed(message.chat.id)

    sent_ok = await _send_chat_bridge_turn(
        settings=settings,
        message=message,
        group_id=message.chat.id,
        peer_username=sender_username,
        mode="reply",
        current_message=incoming.body,
    )
    if not sent_ok:
        log.warning("[%s] chat bridge reply send failed | peer=@%s", message.chat.id, sender_username)
    return True


async def _process_pending_reply_batch(items: list[_PendingReplyItem], settings: Settings) -> None:
    if not items:
        return

    latest = items[-1]
    group_id = latest.group_id
    user_id = latest.user_id
    flow_started = time.perf_counter()
    merged_count = len(items)
    merged_input_text = _build_merged_user_text(items)
    merged_context = _build_merged_context(items)
    reply_targets_context, reply_target_aliases = _build_reply_targets_context(items)
    memory_entries = [item.memory_entry for item in items if item.memory_entry]
    memory = memory_holder.get()
    session_factory = memory.session_factory

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
                return
            tts_mode = normalize_tts_mode(group_settings)

            mute_stmt = select(ReplyMute.id).where(
                ReplyMute.group_id == group_id,
                ReplyMute.user_id == user_id,
            )
            mute_result = await session.execute(mute_stmt)
            if mute_result.scalar_one_or_none() is not None:
                log.info("[%s] pending batch skipped | reason=user_muted user=%s", group_id, user_id)
                return

            decision_history = _exclude_batch_messages(memory.get_history(group_id), memory_entries)
            decision_started = time.perf_counter()
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
            if action_forced:
                log.info(
                    "[%s] pending batch action forced | action=%s explicit_mention=%s reply_to_bot=%s elapsed=%dms",
                    group_id,
                    action,
                    explicit_mention,
                    reply_to_bot,
                    int((time.perf_counter() - decision_started) * 1000),
                )
            else:
                log.info(
                    "[%s] pending batch decision done | action=%s elapsed=%dms",
                    group_id,
                    action,
                    int((time.perf_counter() - decision_started) * 1000),
                )

            reply = ""
            reply_specs: list[ReplyMessageSpec] = []
            reply_source = "none"
            sent_ok = False
            sent_reply_messages: list[str] = []
            delivery_plans: list[_ReplyDeliveryPlan] = []
            skill_handled = False
            sticker_sent_ok = False
            tts_sent_ok = False
            sticker_file = ""
            delivery_mode = "reply"
            tts_text = ""
            explicit_no_reply = False
            force_reply = bool(action_forced and action == "casual")
            tts_service = skill.tts_service or DoubaoTTSService(settings)

            if action != "skip":
                history = await memory.get_history_for_llm(
                    group_id,
                    prompt_payload_builder=lambda candidate_history: skill.build_answer_prompt_payload(
                        merged_input_text,
                        history=_exclude_batch_messages(candidate_history, memory_entries),
                        sender_user_id=user_id,
                        sender_username=latest.sender_username,
                        sender_is_owner=latest.sender_is_owner,
                        sender_is_tg_admin=latest.sender_is_tg_admin,
                        intent_type=action,
                        allow_tts=is_tts_tool_enabled(tts_mode),
                        merged_count=merged_count,
                        merged_context=merged_context,
                        reply_targets_context=reply_targets_context,
                        is_mentioned=mentioned,
                        is_reply_to_bot=reply_to_bot,
                    ),
                )
                history = _exclude_batch_messages(history, memory_entries)
                skill_prompt_payload = skill.build_answer_prompt_payload(
                    merged_input_text,
                    history=history,
                    sender_user_id=user_id,
                    sender_username=latest.sender_username,
                    sender_is_owner=latest.sender_is_owner,
                    sender_is_tg_admin=latest.sender_is_tg_admin,
                    intent_type=action,
                    allow_tts=is_tts_tool_enabled(tts_mode),
                    merged_count=merged_count,
                    merged_context=merged_context,
                    reply_targets_context=reply_targets_context,
                    is_mentioned=mentioned,
                    is_reply_to_bot=reply_to_bot,
                )
                log.info(
                    "[%s] pending batch reply generation started | action=%s history=%d prompt_tokens=%s",
                    group_id,
                    action,
                    len(history),
                    llm.prompt_usage_text(
                        skill_prompt_payload["messages"],
                        tools=skill_prompt_payload["tools"],
                    ),
                )

                async with typing_action(latest.message, enabled=settings.bot.enable_typing):
                    raw_reply = ""
                    skill_result = await skill.answer_with_skill(
                        merged_input_text,
                        session=session,
                        history=history,
                        sender_user_id=user_id,
                        sender_username=latest.sender_username,
                        sender_is_owner=latest.sender_is_owner,
                        sender_is_tg_admin=latest.sender_is_tg_admin,
                        message=latest.message,
                        intent_type=action,
                        allow_tts=is_tts_tool_enabled(tts_mode),
                        merged_count=merged_count,
                        merged_context=merged_context,
                        reply_targets_context=reply_targets_context,
                        is_mentioned=mentioned,
                        is_reply_to_bot=reply_to_bot,
                    )
                    skill_handled = bool(skill_result.handled)
                    sticker_sent_ok = bool(skill_result.sticker_sent)
                    tts_sent_ok = bool(skill_result.tts_sent)
                    sticker_file = skill_result.sticker_file_id or ""
                    tts_text = skill_result.tts_text or ""
                    if tts_sent_ok:
                        sent_ok = True
                    if skill_result.text:
                        raw_reply = skill_result.text
                        reply_source = "skill"
                        log.info("[%s] pending batch reply via skill | %s", group_id, raw_reply[:80])
                    elif skill_handled:
                        reply_source = "skill"
                        log.info(
                            "[%s] pending batch handled by skill | sticker_sent=%s file=%s",
                            group_id,
                            sticker_sent_ok,
                            sticker_file[:32] if sticker_file else "-",
                        )
                    if is_tts_always_enabled(tts_mode) and tts_sent_ok and raw_reply:
                        log.info("[%s] pending batch suppressing text because TTS already sent", group_id)
                        raw_reply = ""
                        reply_source = "skill"

                    if not raw_reply and not skill_handled:
                        casual = CasualService(
                            llm,
                            settings=settings,
                            skill_names=skill.available_skill_names(
                                allow_tts=is_tts_tool_enabled(tts_mode)
                            ),
                        )
                        raw_reply = await casual.reply(
                            merged_input_text,
                            history=history,
                            sender_user_id=user_id,
                            sender_username=latest.sender_username,
                            sender_is_owner=latest.sender_is_owner,
                            sender_is_tg_admin=latest.sender_is_tg_admin,
                            intent_type=action,
                            merged_count=merged_count,
                            merged_context=merged_context,
                            reply_targets_context=reply_targets_context,
                            is_mentioned=mentioned,
                            is_reply_to_bot=reply_to_bot,
                        )
                        if raw_reply:
                            reply_source = "casual"
                        log.info(
                            "[%s] pending batch reply via casual | intent=%s | %s",
                            group_id,
                            action,
                            raw_reply[:80] if raw_reply else "(empty)",
                        )

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
                    if False and silence_reply and action == "casual" and silence_reason == "silent_marker":
                        should_really_silence = True
                        if action == "casual" and silence_reason == "silent_marker":
                            log.warning("[%s] pending batch casual returned silent marker, using fallback", group_id)
                        reply = "嗯哼，我在听~"
                        should_really_silence = False
                        reply_source = "fallback"
                        if should_really_silence:
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
                            reply_source = "none"
                            action = "skip"

            if action != "skip" and reply_specs:
                for reply_spec in reply_specs:
                    resolved_mode = reply_spec.delivery_mode
                    if resolved_mode == "auto":
                        resolved_mode = await reply_mode_svc.decide(
                            user_text=merged_input_text,
                            assistant_reply=reply_spec.text,
                            msg_type=msg_type,
                            is_mentioned=mentioned,
                            is_reply_to_bot=reply_to_bot,
                            is_reply_to_other=reply_to_other,
                            merged_count=merged_count,
                            merged_context=merged_context,
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

            await _best_effort_commit(
                session,
                group_id=group_id,
                context="pending_reply_pre_delivery",
            )

            if action != "skip" and delivery_plans:
                if is_tts_always_enabled(tts_mode) and tts_service.available:
                    tts_results: list[bool] = []
                    for plan in delivery_plans:
                        voice_ok = await tts_service.send_message_tts(
                            latest.message,
                            plan.text,
                            delivery_mode=plan.delivery_mode,
                            reply_to_message_id=plan.reply_to_message_id,
                            auto_delete_minutes=0,
                            uid=str(user_id or group_id),
                        )
                        tts_results.append(voice_ok)
                    sent_reply_messages = [
                        plan.text
                        for plan, ok in zip(delivery_plans, tts_results)
                        if ok
                    ]
                    if any(tts_results):
                        tts_sent_ok = True
                    sent_ok = sent_ok or any(tts_results)
                    if not all(tts_results):
                        log.warning("[%s] always-tts send failed; suppressing text fallback", group_id)
                elif delivery_plans and not tts_sent_ok:
                    text_results: list[bool] = []
                    for plan in delivery_plans:
                        text_ok = await send_reply(
                            latest.message,
                            plan.text,
                            delivery_mode=plan.delivery_mode,
                            reply_to_message_id=plan.reply_to_message_id,
                            stream=bool(settings.bot.enable_streaming and len(delivery_plans) == 1),
                            stream_chunk_size=settings.bot.stream_chunk_size,
                            stream_interval=settings.bot.stream_edit_interval_sec,
                            auto_delete_minutes=0,
                        )
                        text_results.append(text_ok)
                    sent_reply_messages = [
                        plan.text
                        for plan, ok in zip(delivery_plans, text_results)
                        if ok
                    ]
                    sent_ok = sent_ok or any(text_results)

            if action == "skip":
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
                return

            stored_reply_messages = list(sent_reply_messages)
            if not stored_reply_messages and tts_text and tts_sent_ok:
                stored_reply_messages = [tts_text]
            if stored_reply_messages:
                for stored_reply in stored_reply_messages:
                    await memory.add_message(
                        group_id,
                        "assistant",
                        stored_reply,
                        message_type="assistant_reply",
                    )
                await memory.compact_if_needed(group_id)
            log.info(
                "[%s] pending batch finished | action=%s source=%s generated=%s sent=%s mode=%s skill_handled=%s sticker_sent=%s tts_sent=%s file=%s count=%d len=%d elapsed=%dms",
                group_id,
                action,
                reply_source,
                bool(reply_specs),
                sent_ok,
                delivery_mode,
                skill_handled,
                sticker_sent_ok,
                tts_sent_ok,
                sticker_file[:32] if sticker_file else "-",
                len(reply_specs),
                len(reply or ""),
                int((time.perf_counter() - flow_started) * 1000),
            )
        except Exception:
            await session.rollback()
            raise


async def _flush_pending_reply_batch(
    key: tuple[int, int],
    *,
    expected_version: int | None = None,
) -> None:
    current_task = asyncio.current_task()
    async with _PENDING_REPLY_LOCK:
        state = _PENDING_REPLY_BATCHES.get(key)
        if state is None:
            return
        if expected_version is not None and state.version != expected_version:
            return
        _PENDING_REPLY_BATCHES.pop(key, None)

    if state.task and state.task is not current_task and not state.task.done():
        state.task.cancel()
        await asyncio.gather(state.task, return_exceptions=True)

    if state.settings is None:
        return
    await _process_pending_reply_batch(list(state.items), state.settings)


async def _wait_and_flush_pending_reply_batch(
    key: tuple[int, int],
    *,
    expected_version: int,
    delay_seconds: float,
) -> None:
    try:
        await asyncio.sleep(max(0.0, delay_seconds))
        await _flush_pending_reply_batch(key, expected_version=expected_version)
    except asyncio.CancelledError:
        raise
    except Exception:
        log.exception("pending batch flush failed | key=%s", key)


async def _enqueue_pending_reply(item: _PendingReplyItem, settings: Settings) -> tuple[int, float]:
    key = _pending_batch_key(item.group_id, item.user_id)
    now = time.monotonic()

    async with _PENDING_REPLY_LOCK:
        state = _PENDING_REPLY_BATCHES.get(key)
        if state is None:
            state = _PendingReplyBatch()
            _PENDING_REPLY_BATCHES[key] = state
        state.settings = settings
        state.items.append(item)
        state.version += 1
        queued_count = len(state.items)
        state.flush_at = _next_pending_reply_flush_at(
            item=item,
            batch_size=queued_count,
            settings=settings,
            now=now,
            current_flush_at=state.flush_at,
        )
        delay_seconds = max(0.0, state.flush_at - now)

        if state.task and not state.task.done():
            state.task.cancel()

        state.task = asyncio.create_task(
            _wait_and_flush_pending_reply_batch(
                key,
                expected_version=state.version,
                delay_seconds=delay_seconds,
            ),
            name=f"pending-reply:{item.group_id}:{item.user_id}",
        )

    return queued_count, delay_seconds


async def flush_pending_inbound_batches() -> None:
    current_task = asyncio.current_task()
    async with _PENDING_REPLY_LOCK:
        pending = list(_PENDING_REPLY_BATCHES.items())
        _PENDING_REPLY_BATCHES.clear()

    tasks_to_cancel: list[asyncio.Task[None]] = []
    for _, state in pending:
        if state.task and state.task is not current_task and not state.task.done():
            state.task.cancel()
            tasks_to_cancel.append(state.task)
    if tasks_to_cancel:
        await asyncio.gather(*tasks_to_cancel, return_exceptions=True)

    for key, state in pending:
        if not state.items or state.settings is None:
            continue
        try:
            await _process_pending_reply_batch(list(state.items), state.settings)
        except Exception:
            log.exception("pending batch flush_all failed | key=%s", key)


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
    message: Message, session: AsyncSession, settings: Settings
) -> None:
    if not is_group(message):
        return
    if not await ensure_group_authorized(message, session, settings):
        return

    group_id = message.chat.id
    user = message.from_user
    sender_identity = _resolve_sender_identity(message)
    user_id = sender_identity.actor_id
    group_row = await _ensure_group_row(session, group_id, message.chat.title or "")
    group_row.settings = record_group_activity(group_row.settings, settings.bot)

    text, msg_type = extract_message_text(message)
    if not text:
        return
    if msg_type in {"video", "video_caption", "video_note"}:
        log.info("[%s]銆愭祦绋嬨€戝獟浣撴梺璺?| 绫诲瀷=%s", group_id, msg_type)
        return
    input_text = text
    flow_started = time.perf_counter()
    group_settings = group_row.settings or {}
    bot_me = await message.bot.me()
    my_username = normalize_chat_bridge_username(bot_me.username or "")

    if await _maybe_handle_chat_bridge_target_reply(
        message=message,
        session=session,
        settings=settings,
        group_row=group_row,
        sender_identity=sender_identity,
        input_text=input_text,
        my_username=my_username,
    ):
        return

    raw_bot_sender = bool(message.from_user and message.from_user.is_bot and not _uses_sender_chat_identity(message))
    if raw_bot_sender:
        if await _maybe_handle_chat_bridge_turn(
            message=message,
            settings=settings,
            sender_identity=sender_identity,
            input_text=input_text,
            group_row=group_row,
            my_username=my_username,
        ):
            await _best_effort_commit(
                session,
                group_id=group_id,
                context="chat_bridge_turn",
            )
            return
        return

    warn_target = _build_warn_target(
        user=user,
        actor_id=user_id,
        display_name=sender_identity.display_name,
        sender_username=sender_identity.username,
        sender_is_chat=sender_identity.is_chat,
    )

    mute_all_replies = bool(group_settings.get("mute_all_replies", False))

    if msg_type in {"video", "video_caption", "video_note"}:
        log.info("[%s]【流程】媒体旁路 | 类型=%s", group_id, msg_type)
        return

    sender_username = sender_identity.username
    display_name = sender_identity.display_name
    sender_is_owner = bool(user and not sender_identity.is_chat and is_super_admin_user_id(user.id, settings))
    sender_chat = getattr(message, "sender_chat", None)
    sender_is_group_identity = bool(
        sender_identity.is_chat and sender_chat and getattr(sender_chat, "id", None) == group_id
    )
    sender_is_tg_admin = sender_is_group_identity or await is_user_admin(message)
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
    sticker_pool = [
        x.strip()
        for x in (settings.skill_sticker_file_ids or "").split(",")
        if x and x.strip()
    ]
    skill = SkillService(llm, settings=settings, default_sticker_file_ids=sticker_pool)

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
        context="group_activity_and_sticker_learning",
    )
    moderation_started = time.perf_counter()
    if settings.moderation.enabled:
        mod = ModerationService(settings.moderation, llm)
        auto_exempt_moderation = sender_is_tg_admin or sender_identity.is_chat
        auto_exempt_reason = "tg_admin_auto_exempt" if sender_is_tg_admin else "sender_chat_auto_exempt"
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

        if not auto_exempt_moderation and not manual_exempt:
            violated, reason, rule = await mod.check_rules(session, group_id, input_text)
            log.info(
                "[%s]【流程】审核 | 完成 | 违规=%s | 原因=%s | 耗时=%dms",
                group_id,
                violated,
                reason,
                int((time.perf_counter() - moderation_started) * 1000),
            )
            if violated:
                action = str(rule.action if rule else "warn").strip().lower()
                if action not in {"warn", "delete", "ban"}:
                    action = "warn"
                await mod.record_violation(session, group_id, user_id, input_text, action, rule)
                if action == "warn":
                    notice = _build_moderation_notice(
                        warn_target=warn_target,
                        reason=reason,
                        rule=rule,
                        hit_action=action,
                    )
                    await answer_with_auto_delete(
                        message,
                        notice,
                        auto_delete_minutes=settings.bot.auto_delete_minutes,
                    )
                    log.info(
                        "[%s]【结束】审核拦截 | 动作=warn | 已回复=是 | 总耗时=%dms",
                        group_id,
                        int((time.perf_counter() - flow_started) * 1000),
                    )
                    return

                if action == "delete":
                    try:
                        await message.delete()
                    except Exception:
                        pass
                    notice = _build_moderation_notice(
                        warn_target=warn_target,
                        reason=reason,
                        rule=rule,
                        hit_action=action,
                    )
                    await answer_with_auto_delete(
                        message,
                        notice,
                        auto_delete_minutes=settings.bot.auto_delete_minutes,
                    )
                    log.info(
                        "[%s]【结束】审核拦截 | 动作=delete | 已回复=是 | 总耗时=%dms",
                        group_id,
                        int((time.perf_counter() - flow_started) * 1000),
                    )
                    return

                count, should_ban = await mod.add_warning(session, group_id, user_id)
                warn_threshold = max(1, settings.moderation.warn_threshold)
                notice = _build_moderation_notice(
                    warn_target=warn_target,
                    reason=reason,
                    rule=rule,
                    hit_action=action,
                    count=count,
                    threshold=warn_threshold,
                    should_ban=should_ban,
                )

                try:
                    await message.delete()
                except Exception:
                    pass

                if should_ban:
                    try:
                        await message.chat.ban(user_id)
                    except Exception:
                        pass
                await answer_with_auto_delete(
                    message,
                    notice,
                    auto_delete_minutes=settings.bot.auto_delete_minutes,
                )
                log.info(
                    "[%s]【结束】审核拦截 | 动作=ban | 封禁=%s | 警告=%s/%s | 已回复=是 | 总耗时=%dms",
                    group_id,
                    should_ban,
                    count,
                    warn_threshold,
                    int((time.perf_counter() - flow_started) * 1000),
                )
                return
    else:
        log.info("[%s]【流程】审核 | 关闭", group_id)

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
        )
        await memory.compact_if_needed(group_id)
    else:
        log.info("[%s] memory indexing skipped | reason=contact_message", group_id)

    explicit_mention = has_explicit_bot_mention(message, bot_me.username or "", bot_me.id)
    mentioned = is_bot_mentioned(message, bot_me.username or "", bot_me.id)
    is_reply = is_reply_message(message)
    reply_to_bot = is_reply_to_bot(message, bot_me.username or "", bot_me.id)
    reply_to_other = is_reply and not reply_to_bot
    mention_other = mentions_other_user(message, bot_me.username or "", bot_me.id)

    queued_count, queued_delay = await _enqueue_pending_reply(
        _PendingReplyItem(
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
        ),
        settings,
    )
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
