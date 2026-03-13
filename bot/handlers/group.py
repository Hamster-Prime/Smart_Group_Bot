from __future__ import annotations

import base64
import html
import io
import json
import logging
import re
import time
from typing import Any

from aiogram import F, Router
from aiogram.types import Message
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from bot.config import Settings
from bot.db.models import Group, ModerationRule, ReplyMute
from bot.services import memory_holder
from bot.services.authz import ensure_group_authorized, is_super_admin_user_id
from bot.services.casual import CasualService
from bot.services.decision import DecisionService
from bot.services.group_intent import GroupIntent, GroupIntentService
from bot.services.llm import LLMService
from bot.services.moderation import ModerationService
from bot.services.skills import SkillService
from bot.services.sticker_decision import StickerDecisionService
from bot.services.sticker_library import sticker_library
from bot.utils.prompts import RULE_MANAGE_SYSTEM
from bot.utils.telegram import (
    answer_with_auto_delete,
    extract_reply_context,
    extract_message_text,
    is_bot_mentioned,
    is_user_admin,
    is_reply_to_bot,
    is_group,
    is_reply_message,
    mentions_other_user,
    reply_sticker_with_auto_delete,
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


def _parse_json_payload(raw: str) -> dict | None:
    payload = (raw or "").strip()
    if payload.startswith("```"):
        payload = re.sub(r"^```(?:json)?", "", payload).strip()
        payload = re.sub(r"```$", "", payload).strip()
    if not payload.startswith("{"):
        match = re.search(r"\{[\s\S]*\}", payload)
        if match:
            payload = match.group(0)
    try:
        data = json.loads(payload)
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


def _rule_type_label(rule_type: str) -> str:
    return {
        "keyword": "关键词",
        "regex": "正则",
        "llm": "语义",
    }.get((rule_type or "").lower(), rule_type or "未知")


def _action_label(action: str) -> str:
    return {
        "warn": "警告",
        "delete": "删消息",
        "ban": "封禁",
    }.get((action or "").lower(), action or "未知")


async def _render_rules_text(session: AsyncSession, group_id: int) -> str:
    stmt = (
        select(ModerationRule)
        .where(ModerationRule.group_id == group_id)
        .order_by(ModerationRule.id.asc())
    )
    rows = list((await session.execute(stmt)).scalars().all())
    if not rows:
        return "<b>群审核规则</b>\n当前没有规则。"

    lines = [
        "<b>群审核规则</b>",
        f"共 {len(rows)} 条",
        "",
    ]
    for idx, rule in enumerate(rows[:30], start=1):
        status = "启用" if rule.enabled else "关闭"
        pattern = html.escape(_truncate_text(rule.pattern or "", 120))
        lines.append(
            f"{idx}. <b>#{rule.id}</b> [{_rule_type_label(rule.rule_type)}] {_action_label(rule.action)} | {status}"
        )
        lines.append(f"规则: {pattern}")
        lines.append("")
    if len(rows) > 30:
        lines.append(f"... 其余 {len(rows) - 30} 条未展示")
    return "\n".join(lines).rstrip()


async def _apply_natural_rule_instruction(
    *,
    session: AsyncSession,
    llm: LLMService,
    group_id: int,
    instruction: str,
) -> str:
    parsed = _parse_json_payload(await llm.generate(RULE_MANAGE_SYSTEM, instruction))
    if not parsed:
        return "<b>规则解析失败</b>\n无法理解你的规则意图，请换个说法。"

    action = str(parsed.get("action", "unknown")).strip().lower()
    if action == "list":
        return await _render_rules_text(session, group_id)

    if action == "add":
        rule_type = str(parsed.get("rule_type", "keyword")).strip().lower()
        pattern = str(parsed.get("pattern", "")).strip()
        hit_action = str(parsed.get("hit_action", "warn")).strip().lower()
        if rule_type not in {"keyword", "regex", "llm"}:
            return "<b>规则添加失败</b>\nrule_type 必须是 keyword、regex 或 llm。"
        if hit_action not in {"warn", "delete", "ban"}:
            hit_action = "warn"
        if not pattern:
            return "<b>规则添加失败</b>\npattern 不能为空。"

        if rule_type == "keyword" and pattern.lower() in _SEMANTIC_ABUSE_HINTS:
            rule_type = "llm"
            pattern = _ABUSE_LLM_PATTERN

        rule = ModerationRule(
            group_id=group_id,
            rule_type=rule_type,
            pattern=pattern,
            action=hit_action,
        )
        session.add(rule)
        await session.flush()
        return (
            "<b>规则添加成功</b>\n"
            f"<b>规则编号</b>: #{rule.id}\n"
            f"<b>规则类型</b>: {_rule_type_label(rule_type)}\n"
            f"<b>命中动作</b>: {_action_label(hit_action)}\n"
            f"<b>规则内容</b>: {html.escape(_truncate_text(pattern, 160))}"
        )

    if action == "delete":
        target: ModerationRule | None = None
        if parsed.get("rule_id") is not None:
            try:
                rid = int(parsed["rule_id"])
            except (TypeError, ValueError):
                return "<b>规则删除失败</b>\nrule_id 必须是整数。"
            candidate = await session.get(ModerationRule, rid)
            if candidate and candidate.group_id == group_id:
                target = candidate
        else:
            pattern = str(parsed.get("pattern", "")).strip()
            rule_type = str(parsed.get("rule_type", "")).strip().lower()
            if not pattern:
                return "<b>规则删除失败</b>\n删除时请提供 rule_id 或 pattern。"
            stmt = select(ModerationRule).where(
                ModerationRule.group_id == group_id,
                ModerationRule.pattern == pattern,
            )
            if rule_type:
                stmt = stmt.where(ModerationRule.rule_type == rule_type)
            stmt = stmt.order_by(ModerationRule.id.desc())
            target = (await session.execute(stmt)).scalars().first()

        if not target:
            return "<b>规则删除失败</b>\n未找到对应规则。"

        rid = target.id
        content = html.escape(_truncate_text(target.pattern or "", 160))
        rtype = _rule_type_label(target.rule_type or "")
        await session.delete(target)
        return (
            "<b>规则删除成功</b>\n"
            f"<b>规则编号</b>: #{rid}\n"
            f"<b>规则类型</b>: {rtype}\n"
            f"<b>规则内容</b>: {content}"
        )

    return "<b>规则解析失败</b>\n无法理解你的规则意图，请换个说法。"


def _format_permanent_memory_list(items: list[Any]) -> str:
    if not items:
        return "<b>永久记忆</b>\n当前为空。"
    lines = [
        "<b>永久记忆</b>",
        f"共 {len(items)} 条",
        "",
    ]
    for item in items[:40]:
        text = html.escape(_truncate_text(str(getattr(item, "content", "") or ""), 140))
        lines.append(f"#{getattr(item, 'id', 0)} {text}")
    if len(items) > 40:
        lines.append(f"... 其余 {len(items) - 40} 条未展示")
    return "\n".join(lines).rstrip()


async def _handle_management_intent(
    *,
    intent: GroupIntent,
    input_text: str,
    session: AsyncSession,
    llm: LLMService,
    memory: Any,
    group_id: int,
    user_id: int,
) -> tuple[bool, str]:
    if intent.intent == "memory_manage":
        action = (intent.memory_action or "unknown").strip().lower()
        if action == "add":
            content = (intent.memory_content or "").strip()
            row, created = await memory.add_permanent_memory(
                group_id,
                content,
                created_by=user_id,
            )
            if not row:
                return True, "<b>永久记忆写入失败</b>\n内容为空或无效。"
            if created:
                return (
                    True,
                    "<b>永久记忆已写入</b>\n"
                    f"<b>ID</b>: #{row.id}\n"
                    f"<b>内容</b>: {html.escape(_truncate_text(row.content, 180))}",
                )
            return (
                True,
                "<b>永久记忆已存在</b>\n"
                f"<b>ID</b>: #{row.id}\n"
                f"<b>内容</b>: {html.escape(_truncate_text(row.content, 180))}",
            )

        if action == "replace":
            target = (intent.memory_target or "").strip()
            new_content = (intent.memory_content or "").strip()
            if not target or not new_content:
                return True, "<b>永久记忆修改失败</b>\n请明确“要改哪条”以及“改成什么”。"
            deleted, created_row, created_new = await memory.replace_permanent_memory(
                group_id,
                target=target,
                new_content=new_content,
                created_by=user_id,
            )
            if not created_row:
                return True, "<b>永久记忆修改失败</b>\n新内容为空或无效。"
            deleted_count = len(deleted)
            action_text = "已更新" if deleted_count > 0 else "未命中旧记忆，已新增"
            status_text = "新增" if created_new else "复用"
            return (
                True,
                "<b>永久记忆修改结果</b>\n"
                f"<b>状态</b>: {action_text}\n"
                f"<b>删除数量</b>: {deleted_count}\n"
                f"<b>写入方式</b>: {status_text}\n"
                f"<b>新ID</b>: #{created_row.id}\n"
                f"<b>内容</b>: {html.escape(_truncate_text(created_row.content, 180))}",
            )

        if action == "delete":
            target = (intent.memory_target or "").strip()
            if not target:
                return True, "<b>永久记忆删除失败</b>\n请提供删除目标（关键词或 #ID）。"
            deleted = await memory.delete_permanent_memory(group_id, target)
            if not deleted:
                return True, "<b>永久记忆删除失败</b>\n未找到匹配条目。"
            lines = [
                "<b>永久记忆删除成功</b>",
                f"<b>删除数量</b>: {len(deleted)}",
                "",
            ]
            for item in deleted[:10]:
                lines.append(
                    f"#{item.id} {html.escape(_truncate_text(item.content or '', 120))}"
                )
            if len(deleted) > 10:
                lines.append(f"... 其余 {len(deleted) - 10} 条未展示")
            return True, "\n".join(lines).rstrip()

        if action == "clear":
            count = await memory.clear_permanent_memory(group_id)
            return True, f"<b>永久记忆已清空</b>\n共删除 {count} 条。"

        if action == "list":
            items = await memory.list_permanent_memories(group_id)
            return True, _format_permanent_memory_list(items)

        return True, "<b>未识别的永久记忆指令</b>\n请换个说法，例如：记住xxx / 删除永久记忆#12 / 永久记忆列表。"

    if intent.intent == "rule_manage":
        instruction = (intent.rule_instruction or input_text).strip()
        if not instruction:
            instruction = input_text
        return True, await _apply_natural_rule_instruction(
            session=session,
            llm=llm,
            group_id=group_id,
            instruction=instruction,
        )

    return False, ""


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


def _extract_image_file_info(message: Message) -> tuple[str, str] | None:
    """Return (file_id, mime) for image-like messages."""
    if message.photo:
        idx = max(0, len(message.photo) - 2)
        return message.photo[idx].file_id, "image/jpeg"

    if message.document and (message.document.mime_type or "").startswith("image/"):
        return message.document.file_id, (message.document.mime_type or "image/jpeg")

    if message.animation:
        mime = message.animation.mime_type or "image/gif"
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
    if message.from_user and message.from_user.is_bot:
        return
    if not await ensure_group_authorized(message, session, settings):
        return

    text, msg_type = extract_message_text(message)
    if not text:
        return
    input_text = text
    flow_started = time.perf_counter()

    group_id = message.chat.id
    user = message.from_user
    user_id = user.id if user else 0
    warn_target = (
        f"@{user.username}"
        if user and user.username
        else f'<a href="tg://user?id={user_id}">{html.escape((user.full_name if user else str(user_id)) or str(user_id))}</a>'
    )

    group_row = await _ensure_group_row(session, group_id, message.chat.title or "")
    group_settings = group_row.settings or {}
    mute_all_replies = bool(group_settings.get("mute_all_replies", False))

    if msg_type in {"video", "video_caption", "video_note"}:
        log.info("[%s]【流程】媒体旁路 | 类型=%s", group_id, msg_type)
        return

    sender_username = (user.username or "").strip() if user else ""
    sender_is_owner = bool(user and is_super_admin_user_id(user.id, settings))
    sender_is_tg_admin = await is_user_admin(message)
    owner_flag = "yes" if sender_is_owner else "no"
    tg_admin_flag = "yes" if sender_is_tg_admin else "no"
    trusted_source = "tg_admin" if sender_is_tg_admin else "none"
    sender_username_tag = f"@{sender_username}" if sender_username else "(none)"
    if user:
        display_name = (user.full_name or "").strip() or "unknown"
        # Keep id/admin flags at the front so trust metadata survives truncation/compression.
        user_tag = (
            f"id:{user_id} username:{sender_username_tag} "
            f"is_owner:{owner_flag} is_tg_admin:{tg_admin_flag} trusted_source:{trusted_source} "
            f"name:{display_name}"
        )
    else:
        user_tag = (
            "id:0 username:(none) "
            "is_owner:no is_tg_admin:no trusted_source:none "
            "name:unknown"
        )

    llm = LLMService(
        settings.bot.main_model,
        settings.bot.decision_model,
        settings.bot.compress_model,
        moderation=settings.bot.moderation_model,
        embed=settings.bot.embed_model,
    )
    intent_svc = GroupIntentService(llm, context_items=min(6, settings.bot.decision_context_items))
    sticker_pool = [
        x.strip()
        for x in (settings.skill_sticker_file_ids or "").split(",")
        if x and x.strip()
    ]
    skill = SkillService(llm, default_sticker_file_ids=sticker_pool)
    sticker_decider = StickerDecisionService(llm)

    input_text, vision_text = await _append_image_context(message, llm, input_text, msg_type)
    reply_context = extract_reply_context(message)
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
            log.exception("[%s] sticker learning failed", group_id)


    log.info("[%s]【流程】审核 | 开始", group_id)
    moderation_started = time.perf_counter()
    if settings.moderation.enabled:
        mod = ModerationService(settings.moderation, llm)
        auto_exempt_tg_admin = sender_is_tg_admin
        manual_exempt = False
        if auto_exempt_tg_admin:
            log.info(
                "[%s] moderation skipped | reason=tg_admin_auto_exempt user=%s",
                group_id,
                user_id,
            )
        else:
            manual_exempt = await mod.is_user_exempt(session, group_id, user_id)
            if manual_exempt:
                log.info("[%s] moderation skipped | reason=manual_exempt user=%s", group_id, user_id)

        if not auto_exempt_tg_admin and not manual_exempt:
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
    intent = await intent_svc.detect(input_text, history=memory.get_history(group_id))
    if intent.intent in {"memory_manage", "rule_manage"}:
        if not sender_is_tg_admin:
            await answer_with_auto_delete(
                message,
                "<b>权限不足</b>\n仅群管理员可通过自然语言修改永久记忆或群规。",
                auto_delete_minutes=settings.bot.auto_delete_minutes,
            )
            return

        handled, manage_reply = await _handle_management_intent(
            intent=intent,
            input_text=input_text,
            session=session,
            llm=llm,
            memory=memory,
            group_id=group_id,
            user_id=user_id,
        )
        if handled:
            memory.add_message(
                group_id,
                "user",
                f"[{user_tag}] {input_text}",
                user_id=user_id,
                message_type=msg_type,
                message_id=str(message.message_id),
            )
            await answer_with_auto_delete(
                message,
                manage_reply,
                auto_delete_minutes=settings.bot.auto_delete_minutes,
            )
            memory.add_message(group_id, "assistant", manage_reply, message_type="assistant_reply")
            log.info("[%s] management intent handled | intent=%s", group_id, intent.intent)
            return

    should_index_user_memory = msg_type != "contact"
    if should_index_user_memory:
        memory.add_message(
            group_id,
            "user",
            f"[{user_tag}] {input_text}",
            user_id=user_id,
            message_type=msg_type,
            message_id=str(message.message_id),
        )
        history_for_decision = memory.get_history(group_id)
        decision_history = history_for_decision[:-1]
    else:
        # Contact cards are useful for moderation, but noisy for long-term memory indexing.
        history_for_decision = memory.get_history(group_id)
        decision_history = history_for_decision
        log.info("[%s] memory indexing skipped | reason=contact_message", group_id)

    assistant_reply_count = sum(1 for item in history_for_decision if item.get("role") == "assistant")

    bot_me = await message.bot.me()
    mentioned = is_bot_mentioned(message, bot_me.username or "", bot_me.id)
    is_reply = is_reply_message(message)
    reply_to_bot = is_reply_to_bot(message, bot_me.username or "", bot_me.id)
    reply_to_other = is_reply and not reply_to_bot
    mention_other = mentions_other_user(message, bot_me.username or "", bot_me.id)

    log.info(
        "[%s]【决策】输入 | @机=%s @他=%s 回=%s 回机=%s 回他=%s 类型=%s",
        group_id,
        mentioned,
        mention_other,
        is_reply,
        reply_to_bot,
        reply_to_other,
        msg_type,
    )

    decision_svc = DecisionService(llm, context_items=settings.bot.decision_context_items)
    action = "skip"
    reply = ""
    sent_ok = False
    reply_source = "none"
    sticker_decision_send = False
    sticker_decision_reason = ""
    sticker_decision_file = ""
    sticker_sent_ok = False
    decision_started = time.perf_counter()
    action = await decision_svc.decide(
        input_text,
        is_mentioned=mentioned,
        is_reply=is_reply,
        is_reply_to_bot=reply_to_bot,
        is_reply_to_other=reply_to_other,
        mentions_other_user=mention_other,
        is_owner=sender_is_owner,
        is_tg_admin=sender_is_tg_admin,
        user_tag=user_tag,
        msg_type=msg_type,
        history=decision_history,
    )
    log.info(
        "[%s]【决策】完成 | 动作=%s | 耗时=%dms",
        group_id,
        action,
        int((time.perf_counter() - decision_started) * 1000),
    )

    if action != "skip":
        history = await memory.get_history_for_llm(group_id, query=input_text)
        log.info("[%s] flow reply generation started | action=%s | history=%d", group_id, action, len(history))

        async with typing_action(message, enabled=settings.bot.enable_typing):
            skill_reply = await skill.answer_with_skill(
                input_text,
                session=session,
                history=history,
                sender_user_id=user_id,
                sender_username=sender_username,
                sender_is_owner=sender_is_owner,
                sender_is_tg_admin=sender_is_tg_admin,
                message=message,
                intent_type=action,
            )
            if skill_reply:
                reply = skill_reply
                log.info("[%s] reply via skill | %s", group_id, reply[:80])
                reply_source = "skill"

            if not reply:
                casual = CasualService(llm)
                reply = await casual.reply(
                    input_text,
                    history=history,
                    sender_user_id=user_id,
                    sender_username=sender_username,
                    sender_is_owner=sender_is_owner,
                    sender_is_tg_admin=sender_is_tg_admin,
                    intent_type=action,
                )
                log.info(
                    "[%s] reply via casual | intent=%s | %s",
                    group_id,
                    action,
                    reply[:80] if reply else "(empty)",
                )
                if reply:
                    reply_source = "casual"

            if reply:
                cleaned_reply = sanitize_outgoing_text(reply)
                if cleaned_reply != reply:
                    log.warning("[%s] reply sanitized: removed internal markup", group_id)
                reply = cleaned_reply
                normalized_reply = _normalize_owner_address(reply, sender_is_owner)
                if normalized_reply != reply:
                    log.info("[%s] reply owner-address normalized for non-owner sender", group_id)
                reply = normalized_reply

        silence_reply, silence_reason = _should_silence_generated_reply(reply)
        if silence_reply:
            # 根据决策类型和静默原因决定是否真的要静默
            should_really_silence = True
            if action == "casual" and silence_reason == "silent_marker":
                # 闲聊场景返回了 NO_TRUSTED_ANSWER，这不合理
                # 说明提示词可能没生效，使用默认友好回复
                log.warning("[%s] casual chat returned NO_TRUSTED_ANSWER, using fallback reply", group_id)
                reply = "嗯嗯，我在听~"
                should_really_silence = False
                reply_source = "fallback"
            if should_really_silence:
                preview = _truncate_text(reply, 80) if reply else "-"
                log.info(
                    "[%s] reply suppressed -> silent | reason=%s source=%s intent=%s preview=%s",
                    group_id,
                    silence_reason,
                    reply_source,
                    action,
                    preview,
                )
                reply = ""
                reply_source = "none"
                action = "skip"

    sticker_decision_started = time.perf_counter()
    sticker_decision = await sticker_decider.decide(
        session=session,
        group_id=group_id,
        action=action,
        msg_type=msg_type,
        is_mentioned=mentioned,
        is_reply_to_bot=reply_to_bot,
        user_text=input_text,
        assistant_reply=reply,
        reply_source=reply_source,
        assistant_reply_count=assistant_reply_count,
        default_sticker_file_ids=sticker_pool,
    )
    sticker_decision_send = bool(sticker_decision.send)
    sticker_decision_reason = sticker_decision.reason or ""
    sticker_decision_file = sticker_decision.sticker_file_id or ""
    log.info(
        "[%s] sticker decision done | send=%s file=%s reason=%s elapsed=%dms",
        group_id,
        sticker_decision_send,
        sticker_decision_file[:32] if sticker_decision_file else "-",
        sticker_decision_reason or "-",
        int((time.perf_counter() - sticker_decision_started) * 1000),
    )

    if action != "skip" and reply and sticker_decision_send and sticker_decision_file:
        try:
            await reply_sticker_with_auto_delete(
                message,
                sticker=sticker_decision_file,
                auto_delete_minutes=0,
            )
            await sticker_library.mark_sent(session, group_id, sticker_decision_file)
            sticker_sent_ok = True
            log.info(
                "[%s] sticker sent via decision | file=%s reason=%s",
                group_id,
                sticker_decision_file[:32],
                sticker_decision_reason or "-",
            )
        except Exception:
            log.exception("[%s] sticker send failed via decision", group_id)

    if action != "skip" and reply:
        sent_ok = await send_reply(
            message,
            reply,
            stream=settings.bot.enable_streaming,
            stream_chunk_size=settings.bot.stream_chunk_size,
            stream_interval=settings.bot.stream_edit_interval_sec,
            auto_delete_minutes=0,
        )

    if action == "skip":
        log.info(
            "[%s]【结束】跳过 | @机=%s @他=%s 回=%s 回机=%s 回他=%s 贴纸决策=%s | 耗时=%dms",
            group_id,
            mentioned,
            mention_other,
            is_reply,
            reply_to_bot,
            reply_to_other,
            sticker_decision_send,
            int((time.perf_counter() - flow_started) * 1000),
        )
        return

    if reply and sent_ok:
        memory.add_message(group_id, "assistant", reply, message_type="assistant_reply")

    log.info(
        "[%s]【结束】完成 | 动作=%s 来源=%s 已生成=%s 发送=%s 贴纸决策=%s 贴纸发送=%s 长度=%d 耗时=%dms",
        group_id,
        action,
        reply_source,
        bool(reply),
        sent_ok,
        sticker_decision_send,
        sticker_sent_ok,
        len(reply or ""),
        int((time.perf_counter() - flow_started) * 1000),
    )

