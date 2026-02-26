"""Telegram helper utilities."""

from __future__ import annotations

import re

from aiogram.enums import ChatMemberStatus
from aiogram.types import Message

from bot.config import Settings
from bot.services.authz import is_super_admin_user_id


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

