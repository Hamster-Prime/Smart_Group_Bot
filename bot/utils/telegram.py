"""Telegram helper utilities."""

from __future__ import annotations

import asyncio
import html
import logging
import re
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress

from aiogram import Bot
from aiogram.enums import ChatAction, ChatMemberStatus
from aiogram.exceptions import TelegramBadRequest, TelegramNetworkError, TelegramRetryAfter
from aiogram.types import Message

from bot.config import Settings
from bot.services.authz import is_super_admin_user_id

log = logging.getLogger(__name__)

TG_MESSAGE_LIMIT = 4096
TG_STREAM_SAFE_LIMIT = 3800
CHAT_SEND_PARALLEL = 3
TG_TLS_RECORD_RETRY_DELAY = 0.35
_SEND_SEMAPHORES: dict[int, asyncio.Semaphore] = {}
_MENTION_USERNAME_RE = re.compile(r"(?<![A-Za-z0-9_/])@([A-Za-z][A-Za-z0-9_]{4,31})")
_TG_USER_LINK_HTML_RE = re.compile(
    r"""<a\s+href\s*=\s*(?:(['"])tg://user\?id=\d+\1|tg://user\?id=\d+)\s*>(.*?)</a>""",
    flags=re.IGNORECASE | re.DOTALL,
)
_TG_USER_LINK_MD_RE = re.compile(
    r"""\[([^\]]+)\]\(\s*tg://user\?id=\d+\s*\)""",
    flags=re.IGNORECASE,
)
_CODE_SPAN_RE = re.compile(
    r"```[\s\S]*?```|`[^`\n]*`|<code>[\s\S]*?</code>|<pre>[\s\S]*?</pre>",
    flags=re.IGNORECASE,
)
_LLM_INTERNAL_BLOCK_RE = re.compile(
    r"(?is)<\s*(?:think|analysis|reasoning|scratchpad)[\w:-]*[^>]*>[\s\S]*?</\s*(?:think|analysis|reasoning|scratchpad)[\w:-]*\s*>"
)
_LLM_INTERNAL_TAG_RE = re.compile(
    r"(?is)</?\s*(?:think|analysis|reasoning|scratchpad)[\w:-]*[^>]*>"
)
_HISTORY_WRAPPER_TAG_RE = re.compile(
    r"(?im)^\s*</?(?:untrusted|trusted):history_message(?:\(trusted_tg_admin_source\))?>\s*$"
)
_REPLY_TARGET_MISSING_MARKERS = (
    "reply message not found",
    "message to reply not found",
    "message to be replied not found",
    "replied message not found",
)


def sanitize_outgoing_mentions(text: str) -> str:
    """Prevent outgoing mentions while keeping a monospace @name look."""
    if not text:
        return text

    # Keep surrounding text untouched: only strip tg://user links to visible labels.
    cleaned = _TG_USER_LINK_HTML_RE.sub(lambda match: (match.group(2) or ""), text)
    cleaned = _TG_USER_LINK_MD_RE.sub(lambda match: (match.group(1) or ""), cleaned)

    def _break_mention_token(username: str) -> str:
        return f"@\u200b{username}"

    def _replace_mentions(segment: str) -> str:
        return _MENTION_USERNAME_RE.sub(
            lambda match: f"<code>{_break_mention_token(match.group(1))}</code>",
            segment,
        )

    def _replace_mentions_in_code(segment: str) -> str:
        return _MENTION_USERNAME_RE.sub(
            lambda match: _break_mention_token(match.group(1)),
            segment,
        )

    # Do not touch existing code spans; otherwise markdown/html formatting can break.
    result_parts: list[str] = []
    cursor = 0
    for code_match in _CODE_SPAN_RE.finditer(cleaned):
        start, end = code_match.span()
        if start > cursor:
            result_parts.append(_replace_mentions(cleaned[cursor:start]))
        result_parts.append(_replace_mentions_in_code(cleaned[start:end]))
        cursor = end
    if cursor < len(cleaned):
        result_parts.append(_replace_mentions(cleaned[cursor:]))
    return "".join(result_parts)


def sanitize_outgoing_text(text: str) -> str:
    """Remove model-internal markup that can break Telegram entity parsing."""
    if not text:
        return ""

    cleaned = text.replace("\r\n", "\n").replace("\r", "\n")
    leak_positions = [
        pos
        for marker in ("<untrusted:history_message", "<trusted:history_message", "[HISTORY_MESSAGE]")
        if (pos := cleaned.find(marker)) != -1
    ]
    if leak_positions:
        cleaned = cleaned[: min(leak_positions)].rstrip()
    cleaned = _HISTORY_WRAPPER_TAG_RE.sub("", cleaned)
    cleaned = _LLM_INTERNAL_BLOCK_RE.sub(" ", cleaned)
    cleaned = _LLM_INTERNAL_TAG_RE.sub(" ", cleaned)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    cleaned = re.sub(r"[ \t]{2,}", " ", cleaned)
    return cleaned.strip()


AUTO_DELETE_CATEGORIES = frozenset(
    {
        "reply",
        "management",
        "moderation",
        "media",
        "proactive",
        "keyword",
        "scheduled",
        "welcome",
    }
)


def configured_auto_delete_seconds(settings: Settings, category: str) -> int:
    """Resolve the seconds-based retention policy for one message class.

    A per-category seconds override wins; otherwise the category inherits the
    global auto_delete_seconds value.
    """
    normalized = str(category or "").strip().lower()
    if normalized not in AUTO_DELETE_CATEGORIES:
        return 0
    enabled = {
        str(item or "").strip().lower()
        for item in getattr(settings.bot, "auto_delete_categories", [])
    }
    if normalized not in enabled:
        return 0
    per_category = getattr(settings.bot, "auto_delete_category_seconds", None) or {}
    try:
        seconds = int(per_category.get(normalized) or 0)
    except (TypeError, ValueError, AttributeError):
        seconds = 0
    if seconds <= 0:
        seconds = int(getattr(settings.bot, "auto_delete_seconds", 0) or 0)
    if seconds <= 0:
        # Keep integrations that still populate the deprecated alias working
        # while all persisted/runtime configuration uses seconds.
        seconds = int(getattr(settings.bot, "auto_delete_minutes", 0) or 0) * 60
    return max(0, seconds)


def schedule_message_auto_delete(sent: Message | None, auto_delete_seconds: int) -> None:
    """Best-effort delayed delete for outgoing bot messages."""
    delay_seconds = int(auto_delete_seconds or 0)
    if not sent or delay_seconds <= 0:
        return

    chat_id = sent.chat.id
    message_id = sent.message_id

    async def _delete_later() -> None:
        await asyncio.sleep(delay_seconds)
        try:
            await sent.delete()
        except Exception:
            log.debug(
                "auto delete skipped chat_id=%s message_id=%s",
                chat_id,
                message_id,
            )

    try:
        asyncio.create_task(
            _delete_later(),
            name=f"auto-delete:{chat_id}:{message_id}",
        )
    except RuntimeError:
        log.debug(
            "auto delete scheduling failed chat_id=%s message_id=%s",
            chat_id,
            message_id,
        )


def is_reply_target_missing_error(detail: str) -> bool:
    normalized = str(detail or "").lower()
    return any(marker in normalized for marker in _REPLY_TARGET_MISSING_MARKERS)


async def answer_with_auto_delete(
    message: Message,
    text: str,
    *,
    auto_delete_seconds: int = 0,
    retry_tls_record_error: bool = False,
    **kwargs: object,
) -> Message:
    payload = sanitize_outgoing_text(text or "")
    safe_text = sanitize_outgoing_mentions(payload)

    async def _answer_with_network_retry(body: str, options: dict[str, object]) -> Message:
        try:
            return await message.answer(body, **options)
        except TelegramNetworkError as exc:
            detail = str(exc).lower()
            is_tls_record_error = (
                "decryption_failed_or_bad_record_mac" in detail
                or "bad record mac" in detail
            )
            if not retry_tls_record_error or not is_tls_record_error:
                raise
            log.warning(
                "telegram TLS record error chat_id=%s; retrying sendMessage once in %.2fs: %s",
                getattr(getattr(message, "chat", None), "id", "unknown"),
                TG_TLS_RECORD_RETRY_DELAY,
                exc,
            )
            await asyncio.sleep(TG_TLS_RECORD_RETRY_DELAY)
            return await message.answer(body, **options)

    try:
        sent = await _answer_with_network_retry(safe_text, dict(kwargs))
    except TelegramBadRequest as exc:
        detail = str(exc).lower()
        if "can't parse entities" not in detail:
            raise
        log.warning("answer entity parse failed, retrying as plain text")
        fallback_kwargs = dict(kwargs)
        fallback_kwargs["parse_mode"] = None
        plain_text = html.escape(payload)
        sent = await _answer_with_network_retry(plain_text, fallback_kwargs)
    schedule_message_auto_delete(sent, auto_delete_seconds)
    return sent


async def reply_sticker_with_auto_delete(
    message: Message,
    *,
    sticker: str,
    auto_delete_seconds: int = 0,
    **kwargs: object,
) -> Message:
    sent = await message.reply_sticker(sticker=sticker, **kwargs)
    schedule_message_auto_delete(sent, auto_delete_seconds)
    return sent


async def send_sticker_with_auto_delete(
    message: Message,
    *,
    sticker: str,
    delivery_mode: str = "reply",
    auto_delete_seconds: int = 0,
    **kwargs: object,
) -> Message:
    if (delivery_mode or "").strip().lower() == "message":
        sent = await message.answer_sticker(sticker=sticker, **kwargs)
    else:
        sent = await message.reply_sticker(sticker=sticker, **kwargs)
    schedule_message_auto_delete(sent, auto_delete_seconds)
    return sent


def get_display_name(msg: Message) -> str:
    if msg.from_user:
        return msg.from_user.username or msg.from_user.full_name
    return "unknown"


def is_group(msg: Message) -> bool:
    return msg.chat.type in ("group", "supergroup")


def _reply_origin_is_bot(msg: Message, bot_username: str, bot_user_id: int | None) -> bool:
    """Best-effort check whether message is replying to this bot."""
    username_norm = (bot_username or "").lstrip("@").lower()

    reply = getattr(msg, "reply_to_message", None)
    if reply:
        from_user = getattr(reply, "from_user", None)
        if from_user:
            if bot_user_id is not None:
                return from_user.id == bot_user_id
            if getattr(from_user, "is_bot", False):
                return True

        sender_chat = getattr(reply, "sender_chat", None)
        sender_username = (getattr(sender_chat, "username", None) or "").lower()
        if username_norm and sender_username == username_norm:
            return True

    external = getattr(msg, "external_reply", None)
    origin = getattr(external, "origin", None) if external else None
    if origin:
        sender_user = getattr(origin, "sender_user", None)
        if sender_user:
            if bot_user_id is not None:
                return sender_user.id == bot_user_id
            if getattr(sender_user, "is_bot", False):
                return True

        origin_chat = getattr(origin, "chat", None)
        origin_username = (getattr(origin_chat, "username", None) or "").lower()
        if username_norm and origin_username == username_norm:
            return True

    return False


def is_reply_message(msg: Message) -> bool:
    return bool(getattr(msg, "reply_to_message", None) or getattr(msg, "external_reply", None) or getattr(msg, "quote", None))


def is_reply_to_bot(msg: Message, bot_username: str, bot_user_id: int | None = None) -> bool:
    """Check whether this message is replying to the bot itself."""
    return _reply_origin_is_bot(msg, bot_username, bot_user_id)


def has_explicit_bot_mention(msg: Message, bot_username: str, bot_user_id: int | None = None) -> bool:
    """Check whether the message explicitly @mentions this bot."""
    text = msg.text or msg.caption or ""
    username = (bot_username or "").lstrip("@").lower()
    entities = (msg.entities or []) + (msg.caption_entities or [])

    for entity in entities:
        et = getattr(entity, "type", None)
        if et == "mention":
            offset = int(getattr(entity, "offset", 0) or 0)
            length = int(getattr(entity, "length", 0) or 0)
            mention = text[offset : offset + length].strip().lstrip("@").lower()
            if mention and mention == username:
                return True
        elif et == "text_mention":
            user = getattr(entity, "user", None)
            uid = getattr(user, "id", None) if user else None
            if bot_user_id is not None and uid == bot_user_id:
                return True

    if not username:
        return False
    return f"@{username}" in text.lower()


def is_bot_mentioned(msg: Message, bot_username: str, bot_user_id: int | None = None) -> bool:
    """Check if the bot is @mentioned or directly replied to."""
    if _reply_origin_is_bot(msg, bot_username, bot_user_id):
        return True

    return has_explicit_bot_mention(msg, bot_username, bot_user_id)


def mentions_other_user(msg: Message, bot_username: str, bot_user_id: int | None = None) -> bool:
    """Check whether message mentions any user except this bot."""
    bot_name = (bot_username or "").lstrip("@").lower()
    text = msg.text or msg.caption or ""
    entities = (msg.entities or []) + (msg.caption_entities or [])

    for entity in entities:
        et = getattr(entity, "type", None)
        if et == "mention":
            offset = int(getattr(entity, "offset", 0) or 0)
            length = int(getattr(entity, "length", 0) or 0)
            mention = text[offset : offset + length].strip().lstrip("@").lower()
            if mention and mention != bot_name:
                return True
        elif et == "text_mention":
            user = getattr(entity, "user", None)
            uid = getattr(user, "id", None) if user else None
            if uid is not None and (bot_user_id is None or uid != bot_user_id):
                return True

    return False


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

    sent = await message.answer("仅群管理员可使用该命令。")
    if settings:
        schedule_message_auto_delete(
            sent,
            configured_auto_delete_seconds(settings, "management"),
        )
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
    """Split text into natural chunks for streaming edits."""
    if not text:
        return []

    source = text.strip()
    if len(source) <= chunk_size:
        return [source]

    seps = ("\n", "。", "！", "？", ".", "!", "?", "，", ",", ";", " ")
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


def _split_for_telegram(text: str, limit: int) -> list[str]:
    """Split long text so each part stays under Telegram length limits."""
    source = (text or "").strip()
    if not source:
        return []
    if len(source) <= limit:
        return [source]

    parts: list[str] = []
    cursor = 0
    while cursor < len(source):
        end = min(cursor + limit, len(source))
        if end >= len(source):
            tail = source[cursor:].strip()
            if tail:
                parts.append(tail)
            break

        split_at = source.rfind("\n", cursor, end)
        if split_at <= cursor:
            split_at = source.rfind(" ", cursor, end)
        if split_at <= cursor:
            split_at = end
        else:
            split_at += 1

        part = source[cursor:split_at].strip()
        if part:
            parts.append(part)
        cursor = split_at

    return parts


def _compact_ws(value: str) -> str:
    return re.sub(r"\s+", " ", (value or "")).strip()


def _truncate_for_context(value: str, max_len: int) -> str:
    if len(value) <= max_len:
        return value
    return value[:max_len] + "..."


def _format_user_identity(prefix: str, user: object | None) -> str:
    if not user:
        return ""

    uid = getattr(user, "id", None)
    username = _compact_ws(getattr(user, "username", None) or "")
    display_name = _compact_ws(getattr(user, "full_name", None) or "")

    parts: list[str] = []
    if uid is not None:
        parts.append(f"id:{uid}")
    if username:
        parts.append(f"username:@{username}")
    if display_name:
        parts.append(f"name:{display_name}")

    if not parts:
        return ""
    return f"[{prefix}] {' '.join(parts)}"


def _format_chat_identity(prefix: str, chat: object | None) -> str:
    if not chat:
        return ""

    cid = getattr(chat, "id", None)
    username = _compact_ws(getattr(chat, "username", None) or "")
    title = _compact_ws(getattr(chat, "title", None) or "")

    parts: list[str] = []
    if cid is not None:
        parts.append(f"id:{cid}")
    if username:
        parts.append(f"username:@{username}")
    if title:
        parts.append(f"title:{title}")

    if not parts:
        return ""
    return f"[{prefix}] {' '.join(parts)}"


def _format_contact_text(message: Message) -> str:
    contact = getattr(message, "contact", None)
    if not contact:
        return "[contact]"

    first_name = _compact_ws(getattr(contact, "first_name", None) or "")
    last_name = _compact_ws(getattr(contact, "last_name", None) or "")
    phone = _compact_ws(getattr(contact, "phone_number", None) or "")
    vcard = _compact_ws(getattr(contact, "vcard", None) or "")
    contact_uid = getattr(contact, "user_id", None)

    name = _compact_ws(" ".join(part for part in (first_name, last_name) if part))

    lines = ["[contact]"]
    if name:
        lines.append(f"name: {name}")
    if phone:
        lines.append(f"phone: {phone}")
    if contact_uid is not None:
        lines.append(f"user_id: {contact_uid}")
    if vcard:
        lines.append(f"vcard: {_truncate_for_context(vcard, 200)}")

    return "\n".join(lines)


@asynccontextmanager
async def typing_action(
    message: Message, *, enabled: bool, interval: float = 4.0
) -> AsyncIterator[None]:
    """Continuously send typing action while the context is active."""
    if not enabled:
        yield
        return

    stop = asyncio.Event()
    chat_id = message.chat.id

    async def _send_typing_once() -> bool:
        try:
            await asyncio.wait_for(
                message.bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING),
                timeout=3.0,
            )
            return True
        except Exception:
            return False

    async def _worker() -> None:
        # Wait one interval before the next heartbeat, because we send one immediately.
        while not stop.is_set():
            try:
                await asyncio.wait_for(stop.wait(), timeout=interval)
                break
            except asyncio.TimeoutError:
                sent = await _send_typing_once()
                if not sent:
                    # Don't keep a broken typing loop alive forever.
                    break

    await _send_typing_once()
    task = asyncio.create_task(_worker(), name=f"typing:{chat_id}")
    try:
        yield
    finally:
        stop.set()
        if not task.done():
            task.cancel()
        with suppress(asyncio.CancelledError, asyncio.TimeoutError):
            await asyncio.wait_for(task, timeout=1.0)


async def send_reply(
    message: Message,
    text: str,
    *,
    delivery_mode: str = "reply",
    reply_to_message_id: int | None = None,
    stream: bool = False,
    stream_chunk_size: int = 36,
    stream_interval: float = 1.0,
    auto_delete_seconds: int = 0,
) -> bool:
    """Send reply in normal mode or stream-like incremental edits.

    Guarantees best effort to land full content:
    - long content is split under Telegram limits
    - stream mode force-syncs final full text
    - fallback keeps editing the same message (no delete-and-resend)
    """

    mode = (delivery_mode or "reply").strip().lower()
    send_as_reply = mode != "message"
    explicit_reply_target = int(reply_to_message_id or 0) or None

    async def _safe_send(
        body: str,
        *,
        parse_mode: str | None,
        retries: int = 1,
        retry_delay: float = 0.8,
    ) -> Message | None:
        attempt = 0
        current_reply_id = explicit_reply_target if send_as_reply else None
        while attempt <= retries:
            try:
                if send_as_reply:
                    if current_reply_id:
                        sent = await message.bot.send_message(
                            chat_id=message.chat.id,
                            text=body,
                            parse_mode=parse_mode,
                            reply_to_message_id=current_reply_id,
                        )
                    elif explicit_reply_target:
                        sent = await message.answer(body, parse_mode=parse_mode)
                    else:
                        sent = await message.reply(body, parse_mode=parse_mode)
                else:
                    sent = await message.answer(body, parse_mode=parse_mode)
                schedule_message_auto_delete(sent, auto_delete_seconds)
                return sent
            except TelegramRetryAfter as exc:
                wait_s = max(0.5, float(getattr(exc, "retry_after", 1.0))) + 0.2
                log.warning("telegram flood control on send, waiting %.2fs", wait_s)
                await asyncio.sleep(wait_s)
                attempt += 1
            except TelegramBadRequest as exc:
                detail = str(exc).lower()
                if "can't parse entities" in detail:
                    return None
                if send_as_reply and current_reply_id and is_reply_target_missing_error(detail):
                    current_reply_id = None
                    attempt += 1
                    continue
                if attempt >= retries:
                    log.exception(
                        "send bad request chat_id=%s retries=%d",
                        message.chat.id,
                        retries,
                    )
                    return None
                attempt += 1
                await asyncio.sleep(retry_delay * attempt)
            except Exception:
                if attempt >= retries:
                    log.exception(
                        "send failed chat_id=%s retries=%d",
                        message.chat.id,
                        retries,
                    )
                    return None
                attempt += 1
                await asyncio.sleep(retry_delay * attempt)
        return None

    async def _safe_edit(
        sent: Message,
        body: str,
        *,
        parse_mode: str | None,
        retries: int = 1,
    ) -> bool:
        attempt = 0
        while attempt <= retries:
            try:
                await sent.edit_text(body, parse_mode=parse_mode)
                return True
            except TelegramBadRequest as exc:
                detail = str(exc).lower()
                if "message is not modified" in detail:
                    return True
                return False
            except TelegramRetryAfter as exc:
                wait_s = max(0.5, float(getattr(exc, "retry_after", 1.0))) + 0.2
                log.warning("telegram flood control on edit, waiting %.2fs", wait_s)
                await asyncio.sleep(wait_s)
                attempt += 1
            except Exception:
                if attempt >= retries:
                    return False
                attempt += 1
                await asyncio.sleep(0.3 * attempt)
        return False

    async def _finalize_stream_format(sent: Message, segment: str) -> bool:
        """Try to preserve markdown formatting after stream plain-text phase."""
        final_html = md_to_html(segment)
        if final_html == segment:
            return True

        html_ok = await _safe_edit(sent, final_html, parse_mode="HTML", retries=2)
        if html_ok:
            return True

        md_ok = await _safe_edit(sent, segment, parse_mode="Markdown", retries=1)
        if md_ok:
            return True

        # Keep single-message behavior: final fallback edits same message as plain text.
        plain_ok = await _safe_edit(sent, segment, parse_mode=None, retries=1)
        if plain_ok:
            return True
        log.warning("stream format finalize failed to apply markdown/html")
        return False

    async def _send_stream_segment(segment: str) -> bool:
        chunks = _stream_chunks(segment, chunk_size=stream_chunk_size)
        if len(chunks) <= 1 and len(segment) >= 18:
            mid = max(1, len(segment) // 2)
            chunks = [segment[:mid], segment[mid:]]

        if len(chunks) <= 1:
            sent = await _safe_send(segment, parse_mode=None, retries=3)
            if not sent:
                return False
            return await _finalize_stream_format(sent, segment)

        sent = await _safe_send(chunks[0], parse_mode=None, retries=3)
        if not sent:
            return False

        merged = chunks[0]
        last_edit_ts = time.monotonic()
        for chunk in chunks[1:]:
            merged += chunk
            elapsed = time.monotonic() - last_edit_ts
            if elapsed < stream_interval:
                await asyncio.sleep(stream_interval - elapsed)
            edited = await _safe_edit(sent, merged, parse_mode=None, retries=3)
            if edited:
                last_edit_ts = time.monotonic()

        final_plain_ok = await _safe_edit(sent, segment, parse_mode=None, retries=3)
        if not final_plain_ok:
            # Do not send/delete as fallback to avoid duplicate notifications.
            markdown_ok = await _safe_edit(sent, segment, parse_mode="Markdown", retries=1)
            if not markdown_ok:
                return False

        return await _finalize_stream_format(sent, segment)

    payload = sanitize_outgoing_text((text or "").strip())
    payload = sanitize_outgoing_mentions(payload)
    if not payload:
        return False

    semaphore = _SEND_SEMAPHORES.setdefault(message.chat.id, asyncio.Semaphore(CHAT_SEND_PARALLEL))
    async with semaphore:
        if not stream:
            parts = _split_for_telegram(payload, limit=TG_STREAM_SAFE_LIMIT)
            ok = True
            for part in parts:
                html = md_to_html(part)
                sent = await _safe_send(html, parse_mode="HTML", retries=3)
                if not sent:
                    sent = await _safe_send(part, parse_mode="Markdown", retries=2)
                if not sent:
                    sent = await _safe_send(part, parse_mode=None, retries=2)
                ok = ok and bool(sent)
            return ok

        parts = _split_for_telegram(payload, limit=TG_STREAM_SAFE_LIMIT)
        if not parts:
            return False

        all_ok = True
        for part in parts:
            if len(part) > TG_MESSAGE_LIMIT:
                slices = [part[i : i + TG_STREAM_SAFE_LIMIT] for i in range(0, len(part), TG_STREAM_SAFE_LIMIT)]
            else:
                slices = [part]
            for segment in slices:
                seg_ok = await _send_stream_segment(segment)
                all_ok = all_ok and seg_ok
        return all_ok


async def send_reply_messages(
    message: Message,
    texts: list[str],
    *,
    delivery_mode: str = "reply",
    stream: bool = False,
    stream_chunk_size: int = 36,
    stream_interval: float = 1.0,
    auto_delete_seconds: int = 0,
) -> list[bool]:
    normalized = [str(item or "").strip() for item in texts if str(item or "").strip()]
    if not normalized:
        return []

    use_stream = bool(stream and len(normalized) == 1)
    results: list[bool] = []
    normalized_mode = (delivery_mode or "reply").strip().lower()
    for idx, item in enumerate(normalized):
        current_mode = normalized_mode
        if idx > 0 and normalized_mode == "reply":
            current_mode = "message"
        ok = await send_reply(
            message,
            item,
            delivery_mode=current_mode,
            reply_to_message_id=None,
            stream=use_stream,
            stream_chunk_size=stream_chunk_size,
            stream_interval=stream_interval,
            auto_delete_seconds=auto_delete_seconds,
        )
        results.append(ok)
    return results


async def send_chat_message(
    bot: Bot,
    chat_id: int,
    text: str,
    *,
    reply_to_message_id: int | None = None,
    fallback_mention_user_id: int = 0,
    fallback_mention_name: str = "",
    auto_delete_seconds: int = 0,
) -> bool:
    payload = sanitize_outgoing_text((text or "").strip())
    payload = sanitize_outgoing_mentions(payload)
    if not payload:
        return False

    fallback_prefix_html = ""
    if fallback_mention_user_id:
        shown = html.escape((fallback_mention_name or str(fallback_mention_user_id)).strip())
        fallback_prefix_html = f'<a href="tg://user?id={fallback_mention_user_id}">@{shown}</a> '

    async def _safe_send(
        body: str,
        *,
        parse_mode: str | None,
        reply_id: int | None,
        retries: int = 1,
        retry_delay: float = 0.8,
    ) -> Message | None:
        attempt = 0
        current_reply_id = reply_id
        current_body = body
        current_parse_mode = parse_mode
        while attempt <= retries:
            try:
                sent = await bot.send_message(
                    chat_id=chat_id,
                    text=current_body,
                    parse_mode=current_parse_mode,
                    reply_to_message_id=current_reply_id,
                )
                schedule_message_auto_delete(sent, auto_delete_seconds)
                return sent
            except TelegramRetryAfter as exc:
                wait_s = max(0.5, float(getattr(exc, "retry_after", 1.0))) + 0.2
                log.warning("telegram flood control on scheduled send, waiting %.2fs", wait_s)
                await asyncio.sleep(wait_s)
                attempt += 1
            except TelegramBadRequest as exc:
                detail = str(exc).lower()
                if "can't parse entities" in detail:
                    return None
                if current_reply_id and is_reply_target_missing_error(detail):
                    current_reply_id = None
                    if fallback_prefix_html and current_parse_mode == "HTML":
                        current_body = f"{fallback_prefix_html}{body}"
                    attempt += 1
                    continue
                if attempt >= retries:
                    log.exception("scheduled send bad request chat_id=%s retries=%d", chat_id, retries)
                    return None
                attempt += 1
                await asyncio.sleep(retry_delay * attempt)
            except Exception:
                if attempt >= retries:
                    log.exception("scheduled send failed chat_id=%s retries=%d", chat_id, retries)
                    return None
                attempt += 1
                await asyncio.sleep(retry_delay * attempt)
        return None

    semaphore = _SEND_SEMAPHORES.setdefault(chat_id, asyncio.Semaphore(CHAT_SEND_PARALLEL))
    async with semaphore:
        ok = True
        for part in _split_for_telegram(payload, limit=TG_STREAM_SAFE_LIMIT):
            html_body = md_to_html(part)
            sent = await _safe_send(html_body, parse_mode="HTML", reply_id=reply_to_message_id, retries=3)
            if not sent:
                sent = await _safe_send(
                    part,
                    parse_mode="Markdown",
                    reply_id=reply_to_message_id,
                    retries=2,
                )
            if not sent:
                sent = await _safe_send(part, parse_mode=None, reply_id=reply_to_message_id, retries=2)
            ok = ok and bool(sent)
        return ok


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
        mime = (message.animation.mime_type or "").lower()
        if mime.startswith("video/"):
            if message.caption:
                return f"[video]\n{message.caption}", "video_caption"
            return "[video]", "video"
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
    if message.contact:
        return _format_contact_text(message), "contact"
    if message.caption:
        return message.caption, "caption"
    if message.sticker:
        emoji = message.sticker.emoji or ""
        return f"[sticker {emoji}]", "sticker"
    if message.voice:
        return "[voice]", "voice"
    if message.video_note:
        return "[video_note]", "video_note"
    if message.location:
        return "[location]", "location"
    return "", "unknown"


def extract_reply_context(message: Message, max_len: int = 320) -> str:
    """Extract concise replied content for downstream LLM context."""
    lines: list[str] = []

    reply = getattr(message, "reply_to_message", None)
    if reply:
        reply_user_line = _format_user_identity("reply_to_user", getattr(reply, "from_user", None))
        if reply_user_line:
            lines.append(reply_user_line)

        reply_chat_line = _format_chat_identity("reply_to_chat", getattr(reply, "sender_chat", None))
        if reply_chat_line and reply_chat_line not in lines:
            lines.append(reply_chat_line)

        reply_text, reply_type = extract_message_text(reply)
        reply_text = _compact_ws(reply_text or "")
        if reply_text:
            reply_text = _truncate_for_context(reply_text, max_len)
            lines.append(f"[reply_to:{reply_type}] {reply_text}")

    external_reply = getattr(message, "external_reply", None)
    if external_reply:
        origin = getattr(external_reply, "origin", None)
        if origin:
            ext_user_line = _format_user_identity("external_reply_user", getattr(origin, "sender_user", None))
            if ext_user_line and ext_user_line not in lines:
                lines.append(ext_user_line)

            ext_chat_line = _format_chat_identity("external_reply_chat", getattr(origin, "chat", None))
            if ext_chat_line and ext_chat_line not in lines:
                lines.append(ext_chat_line)

        # External replies can omit full text/caption; keep best-effort signal.
        ext_text = _compact_ws(getattr(external_reply, "text", None) or getattr(external_reply, "caption", None) or "")
        if ext_text:
            ext_text = _truncate_for_context(ext_text, max_len)
            ext_line = f"[external_reply:text] {ext_text}"
        else:
            media_markers: tuple[tuple[str, str], ...] = (
                ("photo", "[image]"),
                ("video", "[video]"),
                ("animation", "[gif]"),
                ("document", "[document]"),
                ("audio", "[audio]"),
                ("voice", "[voice]"),
                ("sticker", "[sticker]"),
                ("location", "[location]"),
                ("contact", "[contact]"),
                ("poll", "[poll]"),
            )
            marker = ""
            for field_name, label in media_markers:
                if getattr(external_reply, field_name, None):
                    marker = label
                    break
            ext_line = f"[external_reply] {marker}".strip() if marker else ""

        if ext_line and ext_line not in lines:
            lines.append(ext_line)

    quote = getattr(message, "quote", None)
    quote_text = _compact_ws(getattr(quote, "text", None) or "")
    if quote_text:
        quote_text = _truncate_for_context(quote_text, max_len)
        quote_line = f"[reply_quote] {quote_text}"
        if quote_line not in lines:
            lines.append(quote_line)

    return "\n".join(lines)
