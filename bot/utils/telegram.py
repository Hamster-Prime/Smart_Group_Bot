"""Telegram helper utilities."""

from __future__ import annotations

import asyncio
import logging
import re
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress

from aiogram.enums import ChatAction, ChatMemberStatus
from aiogram.types import Message

from bot.config import Settings
from bot.services.authz import is_super_admin_user_id

log = logging.getLogger(__name__)


def get_display_name(msg: Message) -> str:
    if msg.from_user:
        return msg.from_user.username or msg.from_user.full_name
    return "unknown"


def is_group(msg: Message) -> bool:
    return msg.chat.type in ("group", "supergroup")


def is_bot_mentioned(msg: Message, bot_username: str) -> bool:
    """Check if the bot is @mentioned or replied to."""
    if msg.reply_to_message and msg.reply_to_message.from_user:
        if msg.reply_to_message.from_user.is_bot:
            return True
    text = msg.text or msg.caption or ""
    return f"@{bot_username}" in text


async def is_user_admin(message: Message) -> bool:
    """Check if sender is group admin/creator.

    In private chats, returns True.
    """
    if not message.from_user or not message.chat:
        return False

    if message.chat.type == "private":
        return True

    try:
        member = await message.chat.get_member(message.from_user.id)
    except Exception:
        return False

    return member.status in (ChatMemberStatus.CREATOR, ChatMemberStatus.ADMINISTRATOR)


async def ensure_admin(message: Message, settings: Settings | None = None) -> bool:
    """Return True if sender is admin (or super admin), otherwise reply and return False."""
    if settings and message.from_user and is_super_admin_user_id(message.from_user.id, settings):
        return True

    ok = await is_user_admin(message)
    if ok:
        return True

    await message.answer("仅群管理员可使用该命令。")
    return False


def md_to_html(text: str) -> str:
    """Convert common Markdown to Telegram HTML."""
    text = re.sub(r"```\w*\n(.*?)```", r"<pre>\1</pre>", text, flags=re.DOTALL)
    text = re.sub(r"`([^`]+)`", r"<code>\1</code>", text)
    text = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", text)
    text = re.sub(r"__(.+?)__", r"<b>\1</b>", text)
    text = re.sub(r"\*(.+?)\*", r"<i>\1</i>", text)
    text = re.sub(r"(?<!\w)_(.+?)_(?!\w)", r"<i>\1</i>", text)
    text = re.sub(r"~~(.+?)~~", r"<s>\1</s>", text)
    text = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", r'<a href="\2">\1</a>', text)
    text = re.sub(r"^#{1,6}\s+(.+)$", r"<b>\1</b>", text, flags=re.MULTILINE)
    return text


def _stream_chunks(text: str, chunk_size: int = 96) -> list[str]:
    """Split text to natural chunks for streaming edits."""
    if not text:
        return []

    source = text.strip()
    if len(source) <= chunk_size:
        return [source]

    seps = ("\n", "。", "！", "？", ".", "!", "?", "，", ",", "；", ";", " ")
    chunks: list[str] = []
    cursor = 0

    while cursor < len(source):
        end = min(cursor + chunk_size, len(source))
        if end >= len(source):
            chunks.append(source[cursor:])
            break

        split_at = -1
        for sep in seps:
            idx = source.rfind(sep, cursor, end)
            if idx > split_at:
                split_at = idx

        if split_at <= cursor:
            split_at = end
        else:
            split_at += 1

        chunk = source[cursor:split_at]
        if chunk:
            chunks.append(chunk)
        cursor = split_at

    return chunks


@asynccontextmanager
async def typing_action(
    message: Message, *, enabled: bool, interval: float = 4.5
) -> AsyncIterator[None]:
    """Continuously send typing chat-action while the context is active."""
    if not enabled:
        yield
        return

    stop = asyncio.Event()

    async def _worker() -> None:
        while not stop.is_set():
            try:
                await message.bot.send_chat_action(
                    chat_id=message.chat.id,
                    action=ChatAction.TYPING,
                )
            except Exception:
                break

            try:
                await asyncio.wait_for(stop.wait(), timeout=interval)
            except asyncio.TimeoutError:
                continue

    task = asyncio.create_task(_worker(), name=f"typing:{message.chat.id}")
    try:
        yield
    finally:
        stop.set()
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task


async def send_reply(
    message: Message,
    text: str,
    *,
    stream: bool = False,
    stream_chunk_size: int = 36,
    stream_interval: float = 1.0,
) -> bool:
    """Send reply in normal mode or as stream-like incremental edits."""

    async def _safe_reply(
        body: str,
        *,
        parse_mode: str | None,
        retries: int = 1,
        retry_delay: float = 0.8,
    ) -> Message | None:
        for attempt in range(retries + 1):
            try:
                return await message.reply(body, parse_mode=parse_mode)
            except Exception:
                if attempt >= retries:
                    log.exception(
                        "reply failed chat_id=%s retries=%d",
                        message.chat.id,
                        retries,
                    )
                    return None
                await asyncio.sleep(retry_delay * (attempt + 1))
        return None

    async def _safe_edit(
        sent: Message,
        body: str,
        *,
        parse_mode: str | None,
        retries: int = 0,
    ) -> bool:
        for attempt in range(retries + 1):
            try:
                await sent.edit_text(body, parse_mode=parse_mode)
                return True
            except Exception:
                if attempt >= retries:
                    return False
                await asyncio.sleep(0.3 * (attempt + 1))
        return False

    payload = (text or "").strip()
    if not payload:
        return False

    if not stream:
        html = md_to_html(payload)
        sent = await _safe_reply(html, parse_mode="HTML")
        if sent:
            return True
        plain = await _safe_reply(payload, parse_mode=None)
        return bool(plain)

    chunks = _stream_chunks(payload, chunk_size=stream_chunk_size)
    if len(chunks) <= 1 and len(payload) >= 18:
        mid = max(1, len(payload) // 2)
        chunks = [payload[:mid], payload[mid:]]

    if len(chunks) <= 1:
        sent = await _safe_reply(payload, parse_mode=None)
        return bool(sent)

    sent = await _safe_reply(chunks[0], parse_mode=None)
    if not sent:
        return False
    merged = chunks[0]
    last_edit_ts = time.monotonic()

    for chunk in chunks[1:]:
        merged += chunk
        elapsed = time.monotonic() - last_edit_ts
        if elapsed < stream_interval:
            await asyncio.sleep(stream_interval - elapsed)
        edited = await _safe_edit(sent, merged, parse_mode=None)
        if edited:
            last_edit_ts = time.monotonic()

    final_html = md_to_html(payload)
    if final_html == payload:
        return True
    await _safe_edit(sent, final_html, parse_mode="HTML")
    return True


def extract_message_text(message: Message) -> tuple[str, str]:
    """Extract text content and type description from any message type.

    Returns (text_for_ai, type_label).
    """
    if message.text:
        return message.text, "text"
    if message.photo:
        if message.caption:
            return f"[image]\n{message.caption}", "photo_caption"
        return "[image]", "photo"
    if message.video:
        if message.caption:
            return f"[video]\n{message.caption}", "video_caption"
        return "[video]", "video"
    if message.animation:
        if message.caption:
            return f"[gif]\n{message.caption}", "animation_caption"
        return "[gif]", "animation"
    if message.document:
        name = message.document.file_name or "file"
        if message.caption:
            return f"[document: {name}]\n{message.caption}", "document_caption"
        return f"[document: {name}]", "document"
    if message.audio:
        if message.caption:
            return f"[audio]\n{message.caption}", "audio_caption"
        return "[audio]", "audio"
    if message.caption:
        return message.caption, "caption"
    if message.sticker:
        emoji = message.sticker.emoji or ""
        return f"[sticker {emoji}]", "sticker"
    if message.voice:
        return "[voice]", "voice"
    if message.video_note:
        return "[video_note]", "video_note"
    if message.contact:
        return "[contact]", "contact"
    if message.location:
        return "[location]", "location"
    return "", "unknown"

