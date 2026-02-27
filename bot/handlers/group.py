from __future__ import annotations

import base64
import html
import io
import logging

from aiogram import F, Router
from aiogram.types import Message
from sqlalchemy.exc import IntegrityError
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


async def _ensure_group_row(session: AsyncSession, group_id: int, title: str) -> None:
    """Ensure group metadata row exists, tolerating concurrent inserts."""
    existing = await session.get(Group, group_id)
    if existing:
        if title and existing.title != title:
            existing.title = title
        return

    # Avoid failing the whole update when two messages from the same new group race.
    try:
        async with session.begin_nested():
            session.add(Group(id=group_id, title=title or ""))
            await session.flush()
    except IntegrityError:
        log.debug("group row already inserted concurrently: group_id=%s", group_id)


def _build_kb_index(entries: list) -> str:
    if not entries:
        return "(empty)"

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

    log.info("[vision] image downloaded bytes=%d mime=%s", len(raw), mime)
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
        log.info("[vision] no image recognition result")
        return text

    log.info("[vision] image recognition result: %s", vision_text[:120])
    return f"{text}\n[image-vision]\n{vision_text}"


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
    warn_target = (
        f"@{user.username}"
        if user and user.username
        else f'<a href="tg://user?id={user_id}">{html.escape((user.full_name if user else str(user_id)) or str(user_id))}</a>'
    )

    def _add_message_log(payload: str) -> None:
        session.add(
            MessageLog(
                group_id=group_id,
                user_id=user_id,
                username=(user.username if user else None),
                text=payload[:1000],
            )
        )

    await _ensure_group_row(session, group_id, message.chat.title or "")

    if msg_type in {"video", "video_caption", "video_note"}:
        log.info("[%s] media bypass msg_type=%s", group_id, msg_type)
        _add_message_log(text)
        return

    sender_username = (user.username or "").strip() if user else ""
    sender_username_tag = f"@{sender_username}" if sender_username else "(none)"
    if user:
        display_name = (user.full_name or "").strip() or "unknown"
        # Keep id/username at the front so identity survives truncation/compression.
        user_tag = f"id:{user_id} username:{sender_username_tag} name:{display_name}"
    else:
        user_tag = f"id:{user_id} username:(none) name:unknown"

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


    log.info("[%s] step1 moderation check", group_id)
    if settings.moderation.enabled:
        mod = ModerationService(settings.moderation, llm)
        exempt = await mod.is_user_exempt(session, group_id, user_id)
        if exempt:
            log.info("[%s] moderation bypass: exempt user_id=%s", group_id, user_id)
        else:
            violated, reason, rule = await mod.check_rules(session, group_id, text)
            log.info("[%s] moderation result: violated=%s reason=%s", group_id, violated, reason)
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
                    await message.answer(f"{warn_target} User banned (warnings={count}). Reason: {reason}")
                else:
                    await message.answer(f"{warn_target} Warning #{count}: {reason}")
                _add_message_log(text)
                return

    memory = memory_holder.get()
    memory.add_message(group_id, "user", f"[{user_tag}] {text}")

    bot_me = await message.bot.me()
    mentioned = is_bot_mentioned(message, bot_me.username or "")

    entries = await kb.list_entries(session, group_id)
    kb_titles = [e.title for e in entries if e.title]
    kb_index = _build_kb_index(entries)

    log.info(
        "[%s] step2 decision mentioned=%s msg_type=%s kb_entries=%d",
        group_id,
        mentioned,
        msg_type,
        len(entries),
    )

    decision_svc = DecisionService(llm)
    action = "skip"
    reply = ""
    sent_ok = False
    action = await decision_svc.decide(
        text,
        is_mentioned=mentioned,
        user_tag=user_tag,
        msg_type=msg_type,
        knowledge_titles=kb_titles,
        knowledge_index=kb_index,
    )
    log.info("[%s] decision result: action=%s", group_id, action)

    if action != "skip":
        history = memory.get_history_for_llm(group_id)
        log.info("[%s] step3 generate reply action=%s history_len=%d", group_id, action, len(history))

        if action == "knowledge":
            rag = RAGService(llm, kb)
            reply = await rag.answer(
                session,
                group_id,
                text,
                history=history,
                sender_user_id=user_id,
                sender_username=sender_username,
            )
            log.info("[%s] RAG reply: %s", group_id, reply[:120] if reply else "(empty)")

            if reply and any(x in reply for x in ("NO_RELEVANT_INFO", "I_DONT_KNOW", "NO_INFORMATION")):
                log.info("[%s] RAG answer not useful, fallback to skill", group_id)
                reply = ""

        if not reply:
            skill_reply = await skill.answer_with_skill(
                text,
                history=history,
                sender_user_id=user_id,
                sender_username=sender_username,
            )
            if skill_reply:
                reply = skill_reply
                log.info("[%s] skill reply: %s", group_id, reply[:120])

        if not reply:
            casual = CasualService(llm)
            reply = await casual.reply(
                text,
                history=history,
                sender_user_id=user_id,
                sender_username=sender_username,
            )
            log.info("[%s] casual reply: %s", group_id, reply[:120] if reply else "(empty)")

        if reply:
            async with typing_action(message, enabled=settings.bot.enable_typing):
                sent_ok = await send_reply(
                    message,
                    reply,
                    stream=settings.bot.enable_streaming,
                    stream_chunk_size=settings.bot.stream_chunk_size,
                    stream_interval=settings.bot.stream_edit_interval_sec,
                )

    if action == "skip":
        log.info("[%s] skip reply", group_id)
        await memory.maybe_compress(group_id)
        _add_message_log(text)
        return

    if reply and sent_ok:
        memory.add_message(group_id, "assistant", reply)

    await memory.maybe_compress(group_id)
    _add_message_log(text)

