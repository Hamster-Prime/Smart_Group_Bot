from __future__ import annotations

import base64
import io
import logging

from aiogram import F, Router
from aiogram.types import Message
from sqlalchemy.ext.asyncio import AsyncSession

from bot.config import Settings
from bot.db.models import Group, MessageLog
from bot.services import memory_holder
from bot.services.authz import ensure_group_authorized
from bot.services.casual import CasualService
from bot.services.decision import DecisionService
from bot.services.knowledge import KnowledgeService
from bot.services.llm import LLMService
from bot.services.moderation import ModerationService
from bot.services.rag import RAGService
from bot.services.skills import SkillService
from bot.utils.telegram import (
    extract_message_text,
    is_bot_mentioned,
    is_group,
    send_reply,
    typing_action,
)

router = Router()
log = logging.getLogger(__name__)


def _build_kb_index(entries: list) -> str:
    if not entries:
        return "（空）"

    lines: list[str] = []
    for e in entries[:20]:
        title = (e.title or "").strip()
        content = (e.content or "").replace("\n", " ").strip()
        if len(content) > 120:
            content = content[:120] + "..."
        lines.append(f"- {title}: {content}")
    return "\n".join(lines)


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

    log.info("[vision] 图片已下载 bytes=%d mime=%s", len(raw), mime)
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


async def _append_image_context(message: Message, llm: LLMService, text: str, msg_type: str) -> str:
    """Append image understanding text for moderation/decision/RAG."""
    if msg_type not in {
        "photo",
        "photo_caption",
        "document",
        "document_caption",
        "animation",
        "animation_caption",
        "sticker",
    }:
        return text

    vision_prompt = (
        "请识别这张图片里的关键信息，优先提取可见文字(OCR)和主要物体；"
        "用中文简洁回答，不超过80字；若无法识别请回答：未识别到有效图片内容。"
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
        log.info("[vision] 未获取到图片识别结果")
        return text

    log.info("[vision] 图片识别结果: %s", vision_text[:120])
    return f"{text}\n[图片识别]\n{vision_text}"


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

    group_id = message.chat.id
    user = message.from_user
    user_id = user.id if user else 0

    group = await session.get(Group, group_id)
    if not group:
        group = Group(id=group_id, title=message.chat.title or "")
        session.add(group)

    if msg_type in {"video", "video_caption", "video_note"}:
        log.info("[%s] 视频类媒体直接放行: msg_type=%s", group_id, msg_type)
        session.add(
            MessageLog(
                group_id=group_id,
                user_id=user_id,
                username=(user.username if user else None),
                text=text[:1000],
            )
        )
        return

    if user:
        display_name = user.full_name or ""
        uname = f"@{user.username}" if user.username else ""
        user_tag = f"{display_name}({uname} id:{user_id})" if uname else f"{display_name}(id:{user_id})"
    else:
        user_tag = f"unknown(id:{user_id})"

    llm = LLMService(
        settings.bot.main_model,
        settings.bot.decision_model,
        settings.bot.compress_model,
        moderation=settings.bot.moderation_model,
        embed=settings.bot.embed_model,
    )
    kb = KnowledgeService(settings.knowledge, llm)
    skill = SkillService(llm)

    text = await _append_image_context(message, llm, text, msg_type)

    session.add(
        MessageLog(
            group_id=group_id,
            user_id=user_id,
            username=(user.username if user else None),
            text=text[:1000],
        )
    )

    log.info("[%s] 步骤1: 审核检查", group_id)
    if settings.moderation.enabled:
        mod = ModerationService(settings.moderation, llm)
        violated, reason, rule = await mod.check_rules(session, group_id, text)
        log.info("[%s] 审核结果: violated=%s reason=%s", group_id, violated, reason)
        if violated:
            action = rule.action if rule else "warn"
            await mod.record_violation(session, group_id, user_id, text, action, rule)
            count, should_ban = await mod.add_warning(session, group_id, user_id)

            try:
                await message.delete()
            except Exception:
                pass

            if should_ban:
                try:
                    await message.chat.ban(user_id)
                except Exception:
                    pass
                await message.answer(f"用户已封禁（累计 {count} 次警告）。原因：{reason}")
            else:
                await message.answer(f"⚠️ 警告（第{count}次）：{reason}")
            return

    memory = memory_holder.get()
    memory.add_message(group_id, "user", f"[{user_tag}] {text}")

    bot_me = await message.bot.me()
    mentioned = is_bot_mentioned(message, bot_me.username or "")

    entries = await kb.list_entries(session, group_id)
    kb_titles = [e.title for e in entries if e.title]
    kb_index = _build_kb_index(entries)

    log.info(
        "[%s] 步骤2: 决策 mentioned=%s msg_type=%s kb_entries=%d",
        group_id,
        mentioned,
        msg_type,
        len(entries),
    )

    decision_svc = DecisionService(llm)
    action = "skip"
    reply = ""
    sent_ok = False
    async with typing_action(message, enabled=settings.bot.enable_typing):
        action = await decision_svc.decide(
            text,
            is_mentioned=mentioned,
            user_tag=user_tag,
            msg_type=msg_type,
            knowledge_titles=kb_titles,
            knowledge_index=kb_index,
        )
        log.info("[%s] 决策结果: action=%s", group_id, action)

        if action != "skip":
            history = memory.get_history(group_id)
            log.info("[%s] 步骤3: 生成回复 action=%s history_len=%d", group_id, action, len(history))

            if action == "knowledge":
                rag = RAGService(llm, kb)
                reply = await rag.answer(session, group_id, text, history=history)
                log.info("[%s] RAG回复: %s", group_id, reply[:120] if reply else "(空)")

                if reply and any(x in reply for x in ("参考资料中没有", "我不知道", "没有相关信息")):
                    log.info("[%s] RAG未命中有效答案，尝试技能", group_id)
                    reply = ""

            if not reply:
                skill_reply = await skill.answer_with_skill(text, history=history)
                if skill_reply:
                    reply = skill_reply
                    log.info("[%s] 技能回复: %s", group_id, reply[:120])

            if not reply:
                casual = CasualService(llm)
                reply = await casual.reply(text, history=history)
                log.info("[%s] 闲聊回复: %s", group_id, reply[:120] if reply else "(空)")

            if reply:
                sent_ok = await send_reply(
                    message,
                    reply,
                    stream=settings.bot.enable_streaming,
                    stream_chunk_size=settings.bot.stream_chunk_size,
                    stream_interval=settings.bot.stream_edit_interval_sec,
                )

    if action == "skip":
        log.info("[%s] 跳过，不回复", group_id)
        await memory.maybe_compress(group_id)
        return

    if reply and sent_ok:
        memory.add_message(group_id, "assistant", reply)

    await memory.maybe_compress(group_id)
