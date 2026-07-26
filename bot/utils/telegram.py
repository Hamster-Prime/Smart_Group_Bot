"""Telegram helper utilities."""

from __future__ import annotations

import asyncio
import html
import logging
import re
import time
import weakref
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass
from datetime import timedelta
from typing import TYPE_CHECKING, Literal

from aiogram import Bot
from aiogram.enums import ChatAction, ChatMemberStatus
from aiogram.exceptions import TelegramBadRequest, TelegramNetworkError, TelegramRetryAfter
from aiogram.types import InputRichMessage, Message, ReplyParameters

from bot.config import Settings
from bot.services.authz import is_super_admin_user_id
from bot.services.request_priority import (
    ExecutionPriority,
    current_execution_priority,
)
from bot.services.resource_health import register_resource_health_provider
from bot.utils.timezone import now_shanghai_naive

if TYPE_CHECKING:
    from bot.services.telegram_cleanup import TelegramCleanupScheduler

log = logging.getLogger(__name__)


@dataclass(slots=True)
class ReplyMessageOverlay:
    """A progress message that may become the first final reply message."""

    message: Message
    status_html: str
    reply_to_message_id: int | None
    sent_as_reply: bool
    outcome: Literal[
        "pending",
        "attempting",
        "attached",
        "definite_failure",
        "ambiguous",
    ] = "pending"


_TELEGRAM_BACKGROUND_TASKS: set[asyncio.Task[object]] = set()
_TELEGRAM_BACKGROUND_STARTED: dict[asyncio.Task[object], float] = {}
_TELEGRAM_CLEANUP_SCHEDULER: TelegramCleanupScheduler | None = None
_UNINITIALIZED_AUTO_DELETE_WARNED = False


def confirm_telegram_delivery(callback: Callable[[], None] | None) -> None:
    """Publish Telegram acceptance without making bookkeeping part of send success."""

    if callback is None:
        return
    try:
        callback()
    except Exception:
        # Telegram has already accepted the message. A receipt failure must
        # never make a caller resend the externally visible side effect.
        log.exception("Telegram delivery callback failed")


def _observe_telegram_background_task(task: asyncio.Task[object]) -> None:
    _TELEGRAM_BACKGROUND_TASKS.discard(task)
    _TELEGRAM_BACKGROUND_STARTED.pop(task, None)
    if task.cancelled():
        return
    try:
        task.exception()
    except (asyncio.CancelledError, Exception):
        pass


def _track_telegram_background_task(task: asyncio.Task[object]) -> None:
    """Keep a detached Telegram operation observable until it really exits."""

    _TELEGRAM_BACKGROUND_TASKS.add(task)
    _TELEGRAM_BACKGROUND_STARTED.setdefault(task, time.monotonic())
    task.add_done_callback(_observe_telegram_background_task)


def _schedule_telegram_background_task(
    awaitable: Awaitable[object],
    *,
    name: str,
) -> bool:
    priority = current_execution_priority()
    limit = (
        _TELEGRAM_BACKGROUND_FATAL_LIMIT
        if priority <= ExecutionPriority.HIGH
        else _TELEGRAM_BACKGROUND_SOFT_LIMIT
    )
    if len(_TELEGRAM_BACKGROUND_TASKS) >= limit:
        close = getattr(awaitable, "close", None)
        if callable(close):
            close()
        log.warning(
            "Telegram background task rejected by capacity limit | "
            "name=%s active=%d limit=%d priority=%s",
            name,
            len(_TELEGRAM_BACKGROUND_TASKS),
            limit,
            priority.name.lower(),
        )
        return False
    task = asyncio.create_task(awaitable, name=name)
    _track_telegram_background_task(task)
    return True


async def flush_telegram_background_tasks(*, timeout_seconds: float = 2.0) -> None:
    """Cancel short Telegram helper tasks before closing the Bot session.

    Timer-based deletion is owned by the durable cleanup scheduler and is
    intentionally not part of this set.
    """

    tasks = {task for task in _TELEGRAM_BACKGROUND_TASKS if not task.done()}
    for task in tasks:
        task.cancel()
    if not tasks:
        return
    done, pending = await asyncio.wait(
        tasks,
        timeout=max(0.0, float(timeout_seconds)),
    )
    for task in done:
        _observe_telegram_background_task(task)
    if pending:
        log.error(
            "%d Telegram background tasks ignored shutdown cancellation",
            len(pending),
        )


def telegram_background_health_snapshot() -> dict[str, object]:
    now = time.monotonic()
    active = [task for task in _TELEGRAM_BACKGROUND_TASKS if not task.done()]
    oldest_age = max(
        (now - _TELEGRAM_BACKGROUND_STARTED.get(task, now) for task in active),
        default=0.0,
    )
    count = len(active)
    fatal = bool(
        count >= _TELEGRAM_BACKGROUND_FATAL_LIMIT
        or oldest_age >= _TELEGRAM_BACKGROUND_MAX_AGE_SECONDS
    )
    return {
        "ok": count < _TELEGRAM_BACKGROUND_SOFT_LIMIT and not fatal,
        "fatal": fatal,
        "task_count": count,
        "oldest_task_seconds": round(oldest_age, 3),
        "chat_send_semaphores": len(_SEND_SEMAPHORES),
    }


register_resource_health_provider(
    "telegram_background",
    telegram_background_health_snapshot,
)


def configure_telegram_cleanup_scheduler(
    scheduler: TelegramCleanupScheduler | None,
) -> None:
    """Install/remove the process-wide durable deletion scheduler."""

    global _TELEGRAM_CLEANUP_SCHEDULER, _UNINITIALIZED_AUTO_DELETE_WARNED
    _TELEGRAM_CLEANUP_SCHEDULER = scheduler
    if scheduler is not None:
        _UNINITIALIZED_AUTO_DELETE_WARNED = False

TG_MESSAGE_LIMIT = 4096
TG_STREAM_SAFE_LIMIT = 3800
# Bot API 10.1+ rich messages accept up to 32768 UTF-8 chars of Rich Markdown.
TG_RICH_MESSAGE_LIMIT = 32000
# Sentinel parse_mode routing _safe_send through sendRichMessage.
RICH_MARKDOWN_MODE = "__rich_markdown__"

# Structures only a Rich Message can render. Bold/italic/code/quotes/links
# render fine through the normal HTML path, so plain chat stays a plain
# message and sendRichMessage is reserved for genuinely structured replies.
_RICH_ONLY_PATTERNS = (
    re.compile(r"(?m)^\s*\|.+\|\s*$\n^\s*\|[\s:|-]+\|\s*$"),  # GFM table
    re.compile(r"(?m)^#{1,6}[ \t]+\S"),                        # heading
    re.compile(r"(?m)^\s*(?:-{3,}|\*{3,})\s*$"),               # divider
    re.compile(r"(?m)^\s*(?:[-*+]|\d+\.)[ \t]+\[[ xX]\]"),     # task list
    re.compile(r"(?m)^\s*(?:[-*+]|\d+\.)[ \t]+\S(?:.*\n\s*(?:[-*+]|\d+\.)[ \t])"),  # multi-item list
    re.compile(r"==[^=\n]+=="),                                # marked text
    re.compile(r"\$\$[\s\S]+?\$\$|(?<!\$)\$[^$\n]+\$(?!\$)"),  # math
    re.compile(r"(?is)<(?:details|sub|sup|aside|tg-collage|tg-slideshow|tg-map)\b"),
    re.compile(r"(?m)\[\^[^\]\s]+\]"),                         # footnote
)


_CODE_FENCE_LINE_RE = re.compile(r"^\s*(?:```|~~~)")
_HEADING_LINE_RE = re.compile(r"^#{1,6}[ \t]+\S")
_LIST_ITEM_LINE_RE = re.compile(r"^\s*(?:[-*+]|\d+\.)[ \t]+\S")
_TABLE_LINE_RE = re.compile(r"^\s*\|.*\|\s*$")
_QUOTE_LINE_RE = re.compile(r"^\s*&?>")
_DIVIDER_LINE_RE = re.compile(r"^\s*(?:-{3,}|\*{3,})\s*$")


def _block_kind(line: str) -> str:
    if not line.strip():
        return "blank"
    if _HEADING_LINE_RE.match(line):
        return "heading"
    if _TABLE_LINE_RE.match(line):
        return "table"
    if _DIVIDER_LINE_RE.match(line):
        return "divider"
    if _LIST_ITEM_LINE_RE.match(line):
        return "list"
    if _QUOTE_LINE_RE.match(line):
        return "quote"
    return "text"


def normalize_block_layout(text: str) -> str:
    """Insert breathing room between different Markdown block structures.

    Model output frequently glues a heading straight onto prose, or a table
    onto the sentence above it, which renders as one cramped wall of text.
    This inserts a single blank line at block-kind boundaries (outside fenced
    code) while never touching the interior of a list, table, or quote run.
    """

    source = str(text or "").replace("\r\n", "\n").replace("\r", "\n")
    lines = source.split("\n")
    out: list[str] = []
    in_fence = False
    prev_kind = "blank"
    for line in lines:
        if _CODE_FENCE_LINE_RE.match(line):
            if not in_fence and out and prev_kind not in ("blank",):
                out.append("")
            in_fence = not in_fence
            out.append(line)
            if not in_fence:
                prev_kind = "fence_end"
            continue
        if in_fence:
            out.append(line)
            continue
        kind = _block_kind(line)
        if (
            out
            and kind != "blank"
            and prev_kind not in ("blank",)
            and (
                prev_kind == "fence_end"
                or kind == "heading"
                or prev_kind == "heading"
                or (kind != prev_kind and "text" in (kind, prev_kind))
                or (kind == "divider" or prev_kind == "divider")
            )
        ):
            out.append("")
        out.append(line)
        prev_kind = kind
    normalized = "\n".join(out)
    return re.sub(r"\n{3,}", "\n\n", normalized)


def _needs_rich_markdown(text: str) -> bool:
    """True when the payload uses structures the HTML path cannot render."""

    source = str(text or "")
    ranges = _markdown_code_ranges(source)

    def _outside_code(pos: int) -> bool:
        return not any(start <= pos < end for start, end in ranges)

    for pattern in _RICH_ONLY_PATTERNS:
        for match in pattern.finditer(source):
            if _outside_code(match.start()):
                return True
    return False
CHAT_SEND_PARALLEL = 3
TG_TLS_RECORD_RETRY_DELAY = 0.35
_TYPING_SEND_TIMEOUT_SECONDS = 3.0
_TELEGRAM_CANCEL_GRACE_SECONDS = 0.25
_TYPING_WORKER_CANCEL_GRACE_SECONDS = 1.0
_TELEGRAM_BACKGROUND_SOFT_LIMIT = 16
_TELEGRAM_BACKGROUND_FATAL_LIMIT = 32
_TELEGRAM_BACKGROUND_MAX_AGE_SECONDS = 60.0
_SEND_TOTAL_DEADLINE_SECONDS = 60.0
_STREAM_MAX_INCREMENTAL_EDITS = 12
_STREAM_MAX_PACING_SECONDS = 8.0
_SEND_SEMAPHORES: weakref.WeakValueDictionary[int, asyncio.Semaphore] = (
    weakref.WeakValueDictionary()
)
_MENTION_USERNAME_RE = re.compile(r"(?<![A-Za-z0-9_/])@([A-Za-z][A-Za-z0-9_]{4,31})")
_TG_USER_LINK_HTML_RE = re.compile(
    r"""<a\s+href\s*=\s*(?:(['"])tg://user\?id=\d+\1|tg://user\?id=\d+)\s*>(.*?)</a>""",
    flags=re.IGNORECASE | re.DOTALL,
)
_TG_USER_LINK_MD_RE = re.compile(
    r"""\[([^\]]+)\]\(\s*tg://user\?id=\d+\s*\)""",
    flags=re.IGNORECASE,
)
_TG_HTML_TAG_RE = re.compile(
    r"</?(?:b|strong|i|em|u|ins|s|strike|del|span|tg-spoiler|tg-emoji|a|code|pre|blockquote)"
    r"(?=[\s>/])[^>]*>",
    flags=re.IGNORECASE,
)
_CODE_SPAN_RE = re.compile(
    r"```[\s\S]*?```|`[^`\n]*`|<code>[\s\S]*?</code>|<pre>[\s\S]*?</pre>",
    flags=re.IGNORECASE,
)
_LLM_INTERNAL_TAG = r"(?:think|analysis|reasoning|scratchpad)"
_LLM_INTERNAL_BLOCK_RE = re.compile(
    rf"(?is)<\s*(?P<llm_internal_tag>{_LLM_INTERNAL_TAG})(?=\s|/?>)[^>]*>"
    rf"[\s\S]*?</\s*(?P=llm_internal_tag)\s*>"
)
_LLM_INTERNAL_TAG_RE = re.compile(
    rf"(?is)</?\s*{_LLM_INTERNAL_TAG}(?=\s|/?>)[^>]*>"
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


def _bounded_retry_after_seconds(exc: TelegramRetryAfter) -> float | None:
    requested = max(0.5, float(getattr(exc, "retry_after", 1.0))) + 0.2
    priority = current_execution_priority()
    if priority <= ExecutionPriority.CRITICAL:
        maximum = 2.0
    elif priority <= ExecutionPriority.HIGH:
        maximum = 5.0
    else:
        maximum = 15.0
    return requested if requested <= maximum else None


def _send_total_deadline_seconds() -> float:
    priority = current_execution_priority()
    if priority <= ExecutionPriority.CRITICAL:
        return 12.0
    if priority <= ExecutionPriority.HIGH:
        return 30.0
    return _SEND_TOTAL_DEADLINE_SECONDS


def sanitize_outgoing_mentions(text: str, *, monospace: bool = True) -> str:
    """Prevent outgoing mentions without rewriting code samples.

    ``monospace=False`` is used for Markdown input that will be rendered later;
    injecting raw ``<code>`` tags there would make the whole answer look like
    trusted HTML and can break fenced code blocks.
    """
    if not text:
        return text

    # Keep surrounding text untouched: only strip tg://user links to visible
    # labels in prose. Literal link examples inside code must remain copyable.
    cleaned = _transform_markdown_prose(
        text,
        lambda prose: _TG_USER_LINK_MD_RE.sub(
            lambda match: (match.group(1) or ""),
            _TG_USER_LINK_HTML_RE.sub(
                lambda match: (match.group(2) or ""),
                prose,
            ),
        ),
    )

    def _break_mention_token(username: str) -> str:
        return f"@\u200b{username}"

    def _replace_mentions(segment: str) -> str:
        return _MENTION_USERNAME_RE.sub(
            lambda match: (
                f"<code>{_break_mention_token(match.group(1))}</code>"
                if monospace
                else _break_mention_token(match.group(1))
            ),
            segment,
        )

    def _replace_mentions_in_code(segment: str) -> str:
        return _MENTION_USERNAME_RE.sub(
            lambda match: _break_mention_token(match.group(1)),
            segment,
        )

    # Do not rewrite HTML tag attributes. Search/tool results can contain an
    # ``@name`` inside a URL query; inserting ``<code>`` into that href would
    # make the whole Telegram entity invalid. Visible text inside an existing
    # HTML entity still gets a zero-width break, without nesting another entity.
    protected: list[tuple[int, int, int, str]] = []
    protected.extend(
        (block.start, block.end, 0, cleaned[block.start:block.end])
        for block in _find_fenced_code_blocks(cleaned)
    )
    protected.extend(
        (match.start(), match.end(), 0, match.group(0))
        for match in _CODE_SPAN_RE.finditer(cleaned)
    )
    protected.extend(
        (match.start(), match.end(), 1, match.group(0))
        for match in _TG_HTML_TAG_RE.finditer(cleaned)
    )
    protected.sort(key=lambda item: (item[0], item[2], -(item[1] - item[0])))

    result_parts: list[str] = []
    cursor = 0
    open_tags: list[str] = []
    for start, end, kind, token in protected:
        # A complete code/pre span takes precedence over its individual tags.
        if start < cursor:
            continue
        if start > cursor:
            visible = cleaned[cursor:start]
            result_parts.append(
                _replace_mentions_in_code(visible)
                if open_tags
                else _replace_mentions(visible)
            )

        if kind == 0:
            # Telegram does not activate mentions inside code/pre entities.
            # Keeping the bytes intact also keeps copied configs and scripts
            # usable (notably @names in YAML, shell and examples).
            result_parts.append(token)
        else:
            result_parts.append(token)
            tag_match = re.match(r"<\s*(/?)\s*([A-Za-z-]+)", token)
            if tag_match:
                closing = bool(tag_match.group(1))
                tag_name = tag_match.group(2).lower()
                if closing:
                    for index in range(len(open_tags) - 1, -1, -1):
                        if open_tags[index] == tag_name:
                            del open_tags[index:]
                            break
                elif not token.rstrip().endswith("/>"):
                    open_tags.append(tag_name)
        cursor = end
    if cursor < len(cleaned):
        tail = cleaned[cursor:]
        result_parts.append(
            _replace_mentions_in_code(tail) if open_tags else _replace_mentions(tail)
        )
    return "".join(result_parts)


def _telegram_html_text_units(text: str) -> int:
    """Count parsed Telegram HTML text in UTF-16 code units."""
    visible = html.unescape(_TG_HTML_TAG_RE.sub("", str(text or "")))
    return len(visible.encode("utf-16-le")) // 2


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
    cleaned = _transform_markdown_prose(
        cleaned,
        lambda prose: _LLM_INTERNAL_TAG_RE.sub(
            " ",
            _LLM_INTERNAL_BLOCK_RE.sub(
                " ",
                _HISTORY_WRAPPER_TAG_RE.sub("", prose),
            ),
        ),
    )
    cleaned = _normalize_markdown_whitespace(cleaned)
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
        "call_admin",
        "vote",
    }
)

# Sentinel returned by configured_auto_delete_seconds for button-mode
# categories: schedule_message_auto_delete attaches an inline delete button
# instead of a timer, so every existing send site honors the mode without
# threading extra flags. Config validation forbids negative seconds, so the
# sentinel can never collide with a real retention value.
AUTO_DELETE_BUTTON_SENTINEL = -1

DELETE_BUTTON_CALLBACK_PREFIX = "adel"
DELETE_BUTTON_CALLBACK_DATA = f"{DELETE_BUTTON_CALLBACK_PREFIX}:1"


def configured_auto_delete_mode(settings: Settings, category: str) -> str:
    """Return "timer", "button", or "off" for one message class.

    Timer and button are mutually exclusive per category: button mode replaces
    the delayed delete with an inline delete button on the sent message.
    """
    normalized = str(category or "").strip().lower()
    if normalized not in AUTO_DELETE_CATEGORIES:
        return "off"
    enabled = {
        str(item or "").strip().lower()
        for item in getattr(settings.bot, "auto_delete_categories", [])
    }
    if normalized not in enabled:
        return "off"
    modes = getattr(settings.bot, "auto_delete_category_mode", None) or {}
    try:
        mode = str(modes.get(normalized) or "").strip().lower()
    except AttributeError:
        mode = ""
    return "button" if mode == "button" else "timer"


def build_delete_button_markup(base: object | None = None) -> object:
    """Delete-button keyboard, appended below an existing inline keyboard."""
    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

    delete_row = [
        InlineKeyboardButton(
            text="删除消息",
            callback_data=DELETE_BUTTON_CALLBACK_DATA,
        )
    ]
    existing = list(getattr(base, "inline_keyboard", None) or [])
    if any(
        getattr(button, "callback_data", None) == DELETE_BUTTON_CALLBACK_DATA
        for row in existing
        for button in row
    ):
        from aiogram.types import InlineKeyboardMarkup

        return InlineKeyboardMarkup(inline_keyboard=existing)
    return InlineKeyboardMarkup(inline_keyboard=[*existing, delete_row])


def preserve_delete_button(message: object, keyboard: object | None) -> object | None:
    """Carry an appended delete row across in-place keyboard rebuilds.

    Pagination edits replace reply_markup wholesale; without this the
    button-mode cleanup row would vanish on the first page flip.
    """
    existing = getattr(getattr(message, "reply_markup", None), "inline_keyboard", None) or []
    has_delete_row = any(
        getattr(button, "callback_data", None) == DELETE_BUTTON_CALLBACK_DATA
        for row in existing
        for button in row
    )
    if not has_delete_row:
        return keyboard
    return build_delete_button_markup(keyboard)


def attach_delete_button(sent: Message | None) -> None:
    """Best-effort delete-button attach for an already-sent message.

    Runs as a fire-and-forget edit so send paths stay non-blocking; an
    existing inline keyboard is preserved with the delete row appended.
    """
    if sent is None or isinstance(sent, bool):
        return
    existing = getattr(sent, "reply_markup", None)

    async def _attach() -> None:
        try:
            await sent.edit_reply_markup(
                reply_markup=build_delete_button_markup(existing)
            )
        except Exception:
            log.debug(
                "delete button attach skipped chat_id=%s message_id=%s",
                getattr(getattr(sent, "chat", None), "id", "?"),
                getattr(sent, "message_id", "?"),
            )

    try:
        _schedule_telegram_background_task(
            _attach(),
            name=(
                "delete-button:"
                f"{getattr(getattr(sent, 'chat', None), 'id', 0)}:"
                f"{getattr(sent, 'message_id', 0)}"
            ),
        )
    except RuntimeError:
        log.debug("delete button scheduling failed")


def configured_auto_delete_seconds(settings: Settings, category: str) -> int:
    """Resolve the seconds-based retention policy for one message class.

    A per-category seconds override wins; otherwise the category inherits the
    global auto_delete_seconds value. Button-mode categories return
    AUTO_DELETE_BUTTON_SENTINEL so schedule_message_auto_delete attaches the
    inline delete button instead of a timer.
    """
    normalized = str(category or "").strip().lower()
    mode = configured_auto_delete_mode(settings, normalized)
    if mode == "off":
        return 0
    if mode == "button":
        return AUTO_DELETE_BUTTON_SENTINEL
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
    """Compatibility-only best-effort cleanup scheduling.

    AUTO_DELETE_BUTTON_SENTINEL attaches the inline delete button instead of
    scheduling a timer. Production asynchronous send paths must await
    :func:`schedule_message_auto_delete_durable` for timer mode.
    """
    delay_seconds = int(auto_delete_seconds or 0)
    if delay_seconds == AUTO_DELETE_BUTTON_SENTINEL:
        attach_delete_button(sent)
        return
    if not sent or delay_seconds <= 0:
        return

    chat_id = int(sent.chat.id)
    message_id = int(sent.message_id)
    scheduler = _TELEGRAM_CLEANUP_SCHEDULER
    if scheduler is None:
        # Imports and isolated utility tests legitimately run without the main
        # process lifecycle.  A safe no-op is preferable to recreating the old
        # unbounded per-message sleep tasks.  Production installs the scheduler
        # before update delivery starts.
        global _UNINITIALIZED_AUTO_DELETE_WARNED
        if not _UNINITIALIZED_AUTO_DELETE_WARNED:
            _UNINITIALIZED_AUTO_DELETE_WARNED = True
            log.warning(
                "auto-delete requested before durable scheduler initialization; "
                "timer jobs are disabled in this process"
            )
        return

    accepted = scheduler.enqueue(
        chat_id=chat_id,
        message_id=message_id,
        due_at=now_shanghai_naive() + timedelta(seconds=delay_seconds),
    )
    if not accepted:
        log.error(
            "auto delete persistence rejected | chat_id=%s message_id=%s",
            chat_id,
            message_id,
        )


async def schedule_message_auto_delete_durable(
    sent: Message | None,
    auto_delete_seconds: int,
) -> bool:
    """Persist timer cleanup before the enclosing send path returns.

    Button mode has no delayed timer and keeps its existing best-effort markup
    edit. A ``False`` timer result means the scheduler is unhealthy; callers
    must not resend the already-delivered Telegram message.
    """

    delay_seconds = int(auto_delete_seconds or 0)
    if delay_seconds == AUTO_DELETE_BUTTON_SENTINEL:
        attach_delete_button(sent)
        return True
    if not sent or delay_seconds <= 0:
        return True

    chat_id = int(sent.chat.id)
    message_id = int(sent.message_id)
    scheduler = _TELEGRAM_CLEANUP_SCHEDULER
    if scheduler is None:
        global _UNINITIALIZED_AUTO_DELETE_WARNED
        if not _UNINITIALIZED_AUTO_DELETE_WARNED:
            _UNINITIALIZED_AUTO_DELETE_WARNED = True
            log.critical(
                "durable auto-delete requested before scheduler initialization"
            )
        return False

    accepted = await scheduler.enqueue_durable(
        chat_id=chat_id,
        message_id=message_id,
        due_at=now_shanghai_naive() + timedelta(seconds=delay_seconds),
    )
    if not accepted:
        log.error(
            "durable auto delete persistence rejected | chat_id=%s message_id=%s",
            chat_id,
            message_id,
        )
    return accepted


async def _schedule_delivered_message_cleanup(
    sent: Message | None,
    auto_delete_seconds: int,
) -> None:
    """Keep post-delivery cleanup failures outside transport retry decisions."""

    try:
        await schedule_message_auto_delete_durable(sent, auto_delete_seconds)
    except Exception:
        log.exception("Telegram post-delivery cleanup scheduling failed")


def is_reply_target_missing_error(detail: str) -> bool:
    normalized = str(detail or "").lower()
    return any(marker in normalized for marker in _REPLY_TARGET_MISSING_MARKERS)


async def answer_with_auto_delete(
    message: Message,
    text: str,
    *,
    auto_delete_seconds: int = 0,
    disable_link_preview: bool | None = None,
    retry_tls_record_error: bool = False,
    plain_text_fallback: str | None = None,
    sanitize_mentions: bool = True,
    drop_invalid_reply_markup: bool = False,
    **kwargs: object,
) -> Message:
    payload = sanitize_outgoing_text(text or "")
    safe_text = sanitize_outgoing_mentions(payload) if sanitize_mentions else payload

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

    # Trusted admin-authored templates pass their original Markdown/plain
    # source here. Without it, retain the legacy escaped fallback used by
    # model-generated HTML output.
    plain_text = (
        sanitize_outgoing_text(plain_text_fallback)
        if plain_text_fallback is not None
        else html.escape(payload)
    )
    send_kwargs = dict(kwargs)
    if (
        disable_link_preview is not None
        and "disable_web_page_preview" not in send_kwargs
        and "link_preview_options" not in send_kwargs
    ):
        send_kwargs["disable_web_page_preview"] = bool(disable_link_preview)
    try:
        sent = await _answer_with_network_retry(safe_text, send_kwargs)
    except TelegramBadRequest as exc:
        detail = str(exc).lower()
        formatted_rejected = (
            "can't parse entities" in detail or "message is too long" in detail
        )
        has_markup = send_kwargs.get("reply_markup") is not None
        if formatted_rejected:
            log.warning("answer formatted send failed, retrying as plain text: %s", exc)
            fallback_kwargs = dict(send_kwargs)
            fallback_kwargs["parse_mode"] = None
            try:
                sent = await _answer_with_network_retry(plain_text, fallback_kwargs)
            except TelegramBadRequest:
                if not (drop_invalid_reply_markup and has_markup):
                    raise
                fallback_kwargs.pop("reply_markup", None)
                sent = await _answer_with_network_retry(plain_text, fallback_kwargs)
        elif drop_invalid_reply_markup and has_markup:
            log.warning("answer keyboard send failed, retrying without markup: %s", exc)
            no_markup_kwargs = dict(send_kwargs)
            no_markup_kwargs.pop("reply_markup", None)
            try:
                sent = await _answer_with_network_retry(safe_text, no_markup_kwargs)
            except TelegramBadRequest as retry_exc:
                retry_detail = str(retry_exc).lower()
                if (
                    "can't parse entities" not in retry_detail
                    and "message is too long" not in retry_detail
                ):
                    raise
                no_markup_kwargs["parse_mode"] = None
                sent = await _answer_with_network_retry(
                    plain_text,
                    no_markup_kwargs,
                )
        else:
            raise
    await schedule_message_auto_delete_durable(sent, auto_delete_seconds)
    return sent


async def reply_sticker_with_auto_delete(
    message: Message,
    *,
    sticker: str,
    auto_delete_seconds: int = 0,
    **kwargs: object,
) -> Message:
    sent = await message.reply_sticker(sticker=sticker, **kwargs)
    await schedule_message_auto_delete_durable(sent, auto_delete_seconds)
    return sent


async def send_sticker_with_auto_delete(
    message: Message,
    *,
    sticker: str,
    delivery_mode: str = "reply",
    auto_delete_seconds: int = 0,
    on_delivery: Callable[[], None] | None = None,
    **kwargs: object,
) -> Message:
    if (delivery_mode or "").strip().lower() == "message":
        sent = await message.answer_sticker(sticker=sticker, **kwargs)
    else:
        sent = await message.reply_sticker(sticker=sticker, **kwargs)
    confirm_telegram_delivery(on_delivery)
    await _schedule_delivered_message_cleanup(sent, auto_delete_seconds)
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

    # Deferred to avoid the template module's import of this utility module.
    from bot.services.message_templates import render_action_notice

    sent = await message.answer(
        render_action_notice(
            "权限不足",
            action="仅群管理员可使用该命令。",
        ),
        parse_mode="HTML",
    )
    if settings:
        await schedule_message_auto_delete_durable(
            sent,
            configured_auto_delete_seconds(settings, "management"),
        )
    return False


_MARKDOWN_FENCE_LINE_RE = re.compile(
    r"^(?P<indent>[ \t]{0,3})(?P<fence>`{3,}|~{3,})(?P<rest>[^\n]*)$"
)
_MARKDOWN_INLINE_CODE_RE = re.compile(r"(?<!`)`([^`\n]+)`(?!`)")
_MARKDOWN_LINK_RE = re.compile(r"\[([^\]\n]+)\]\(([^)\s]+)\)")
_MARKDOWN_TOKEN_RE = re.compile(r"\x00tgmd(\d+)\x00")


@dataclass(frozen=True, slots=True)
class _FencedCodeBlock:
    start: int
    end: int
    opening: str
    content: str
    closing: str
    fence: str
    closed: bool


class _FencedMarkdownPart(str):
    """A split Markdown wrapper whose delivered code content stays exact.

    Markdown requires a line break before a closing fence.  That structural
    newline must not become part of a long single-line code sample merely
    because Telegram forced it into multiple messages, so split parts retain
    the exact content separately from their valid Markdown debug form.
    """

    def __new__(
        cls,
        value: str,
        *,
        opening: str,
        content: str,
    ) -> "_FencedMarkdownPart":
        instance = super().__new__(cls, value)
        instance.opening = opening
        instance.content = content
        return instance


def _utf16_units(text: str) -> int:
    return len(str(text or "").encode("utf-16-le")) // 2


def _line_body(line: str) -> str:
    if line.endswith("\r\n"):
        return line[:-2]
    if line.endswith(("\r", "\n")):
        return line[:-1]
    return line


def _find_fenced_code_blocks(text: str) -> list[_FencedCodeBlock]:
    """Locate complete or unfinished line-oriented Markdown code fences."""
    source = str(text or "")
    lines = source.splitlines(keepends=True)
    blocks: list[_FencedCodeBlock] = []
    offsets: list[int] = []
    cursor = 0
    for line in lines:
        offsets.append(cursor)
        cursor += len(line)

    index = 0
    while index < len(lines):
        opening_body = _line_body(lines[index])
        opening_match = _MARKDOWN_FENCE_LINE_RE.match(opening_body)
        if not opening_match:
            index += 1
            continue

        fence = opening_match.group("fence")
        content_start = offsets[index] + len(lines[index])
        closing_index: int | None = None
        for candidate in range(index + 1, len(lines)):
            closing_body = _line_body(lines[candidate])
            closing_match = _MARKDOWN_FENCE_LINE_RE.match(closing_body)
            if not closing_match:
                continue
            closing_fence = closing_match.group("fence")
            if (
                closing_fence[0] == fence[0]
                and len(closing_fence) >= len(fence)
                and not closing_match.group("rest").strip()
            ):
                closing_index = candidate
                break

        start = offsets[index]
        if closing_index is None:
            blocks.append(
                _FencedCodeBlock(
                    start=start,
                    end=len(source),
                    opening=opening_body,
                    content=source[content_start:],
                    closing=f"{opening_match.group('indent')}{fence}",
                    fence=fence,
                    closed=False,
                )
            )
            break

        closing_body = _line_body(lines[closing_index])
        closing_start = offsets[closing_index]
        closing_end = closing_start + len(closing_body)
        blocks.append(
            _FencedCodeBlock(
                start=start,
                end=closing_end,
                opening=opening_body,
                content=source[content_start:closing_start],
                closing=closing_body,
                fence=fence,
                closed=True,
            )
        )
        index = closing_index + 1

    return blocks


def _markdown_code_ranges(text: str) -> list[tuple[int, int]]:
    """Return non-overlapping fenced and inline Markdown code ranges."""

    source = str(text or "")
    ranges = [(block.start, block.end) for block in _find_fenced_code_blocks(source)]
    ranges.extend(
        (match.start(), match.end())
        for match in _MARKDOWN_INLINE_CODE_RE.finditer(source)
    )
    ranges.sort(key=lambda item: (item[0], -(item[1] - item[0])))
    merged: list[tuple[int, int]] = []
    for start, end in ranges:
        if merged and start < merged[-1][1]:
            continue
        merged.append((start, end))
    return merged


def _transform_markdown_prose(
    text: str,
    transform: Callable[[str], str],
) -> str:
    """Apply a sanitizer only outside Markdown code spans and fences."""

    source = str(text or "")
    ranges = _markdown_code_ranges(source)
    if not ranges:
        return transform(source)

    rendered: list[str] = []
    cursor = 0
    for start, end in ranges:
        rendered.append(transform(source[cursor:start]))
        rendered.append(source[start:end])
        cursor = end
    rendered.append(transform(source[cursor:]))
    return "".join(rendered)


def _contains_telegram_html_outside_markdown_code(text: str) -> bool:
    """Detect trusted Telegram HTML without mistaking code examples for it."""

    source = str(text or "")
    ranges = _markdown_code_ranges(source)
    range_index = 0
    for match in _TG_HTML_TAG_RE.finditer(source):
        while range_index < len(ranges) and ranges[range_index][1] <= match.start():
            range_index += 1
        if (
            range_index >= len(ranges)
            or not ranges[range_index][0] <= match.start() < ranges[range_index][1]
        ):
            return True
    return False


def _normalize_markdown_whitespace(text: str) -> str:
    """Compact prose whitespace without modifying code contents."""
    source = str(text or "")
    protected = [
        (block.start, block.end)
        for block in _find_fenced_code_blocks(source)
    ]
    protected.extend(
        (match.start(), match.end())
        for match in _CODE_SPAN_RE.finditer(source)
    )
    protected.sort(key=lambda item: (item[0], -(item[1] - item[0])))
    if not protected:
        source = re.sub(r"\n{3,}", "\n\n", source)
        return re.sub(r"[ \t]{2,}", " ", source)

    rendered: list[str] = []
    cursor = 0
    for start, end in protected:
        if start < cursor:
            continue
        prose = source[cursor:start]
        prose = re.sub(r"\n{3,}", "\n\n", prose)
        prose = re.sub(r"[ \t]{2,}", " ", prose)
        rendered.append(prose)
        rendered.append(source[start:end])
        cursor = end
    prose = source[cursor:]
    prose = re.sub(r"\n{3,}", "\n\n", prose)
    prose = re.sub(r"[ \t]{2,}", " ", prose)
    rendered.append(prose)
    return "".join(rendered)


def _safe_markdown_link(value: str) -> str:
    candidate = html.unescape(str(value or "").strip())
    if re.match(r"(?i)^(?:https?://|tg://|mailto:)", candidate):
        return candidate
    return ""


_BLOCKQUOTE_LINE_RE = re.compile(r"^&gt;(!?)[ \t]?(.*)$")


def _render_blockquotes(text: str) -> str:
    """Merge consecutive `>` lines into one blockquote; `>!` marks expandable."""

    out: list[str] = []
    quote_lines: list[str] = []
    quote_expandable = False

    def _flush() -> None:
        nonlocal quote_lines, quote_expandable
        if quote_lines:
            attr = " expandable" if quote_expandable else ""
            out.append(
                f"<blockquote{attr}>" + "\n".join(quote_lines) + "</blockquote>"
            )
            quote_lines = []
            quote_expandable = False

    for line in text.split("\n"):
        match = _BLOCKQUOTE_LINE_RE.match(line)
        if match:
            if match.group(1):
                quote_expandable = True
            quote_lines.append(match.group(2))
        else:
            _flush()
            out.append(line)
    _flush()
    return "\n".join(out)


def _render_inline_markdown(text: str) -> str:
    tokens: list[str] = []

    def _stash(rendered: str) -> str:
        index = len(tokens)
        tokens.append(rendered)
        return f"\x00tgmd{index}\x00"

    source = _MARKDOWN_INLINE_CODE_RE.sub(
        lambda match: _stash(f"<code>{html.escape(match.group(1))}</code>"),
        text,
    )

    def _link(match: re.Match[str]) -> str:
        target = _safe_markdown_link(match.group(2))
        if not target:
            return match.group(0)
        return _stash(
            f'<a href="{html.escape(target, quote=True)}">'
            f"{html.escape(match.group(1))}</a>"
        )

    source = _MARKDOWN_LINK_RE.sub(_link, source)
    rendered = html.escape(source)
    rendered = re.sub(r"(?m)^#{1,6}[ \t]+(.+)$", r"<b>\1</b>", rendered)
    rendered = re.sub(r"\*\*([^*\n]+?)\*\*", r"<b>\1</b>", rendered)
    rendered = re.sub(r"__([^_\n]+?)__", r"<b>\1</b>", rendered)
    rendered = re.sub(r"~~([^~\n]+?)~~", r"<s>\1</s>", rendered)
    rendered = re.sub(r"\|\|([^|\n]+?)\|\|", r"<tg-spoiler>\1</tg-spoiler>", rendered)
    rendered = re.sub(r"(?<!\*)\*([^*\n]+?)\*(?!\*)", r"<i>\1</i>", rendered)
    rendered = re.sub(r"(?<!\w)_([^_\n]+?)_(?!\w)", r"<i>\1</i>", rendered)
    rendered = _render_blockquotes(rendered)
    return _MARKDOWN_TOKEN_RE.sub(
        lambda match: tokens[int(match.group(1))],
        rendered,
    )


def _render_fenced_code_html(opening: str, content: str) -> str:
    opening_match = _MARKDOWN_FENCE_LINE_RE.match(opening)
    info = opening_match.group("rest").strip() if opening_match else ""
    language = info.split(maxsplit=1)[0] if info else ""
    language_attr = ""
    if re.fullmatch(r"[A-Za-z0-9_+.-]{1,32}", language):
        language_attr = f' class="language-{html.escape(language, quote=True)}"'
    return f"<pre><code{language_attr}>{html.escape(content)}</code></pre>"


def md_to_html(text: str) -> str:
    """Render model Markdown as escaped, Telegram-safe HTML."""
    if isinstance(text, _FencedMarkdownPart):
        return _render_fenced_code_html(text.opening, text.content)
    source = str(text or "").replace("\r\n", "\n").replace("\r", "\n")
    blocks = _find_fenced_code_blocks(source)
    if not blocks:
        return _render_inline_markdown(source)

    rendered: list[str] = []
    cursor = 0
    for block in blocks:
        rendered.append(_render_inline_markdown(source[cursor:block.start]))
        rendered.append(_render_fenced_code_html(block.opening, block.content))
        cursor = block.end
    rendered.append(_render_inline_markdown(source[cursor:]))
    return "".join(rendered)


def _telegram_html_to_plain(text: str) -> str:
    """Return a readable fallback for trusted Telegram HTML templates."""
    return html.unescape(_TG_HTML_TAG_RE.sub("", str(text or ""))).strip()


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


def _take_utf16_chunk(text: str, limit: int, *, spaces: bool = True) -> int:
    """Return a safe Python index whose prefix fits a UTF-16 unit budget."""
    if limit <= 0:
        return 0
    units = 0
    hard_end = 0
    newline_end = 0
    whitespace_end = 0
    for index, char in enumerate(text):
        char_units = 2 if ord(char) > 0xFFFF else 1
        if units + char_units > limit:
            break
        units += char_units
        hard_end = index + 1
        if char == "\n":
            newline_end = hard_end
        elif spaces and char.isspace():
            whitespace_end = hard_end
    if hard_end >= len(text):
        return hard_end
    return newline_end or whitespace_end or hard_end


def _split_plain_utf16(text: str, limit: int, *, spaces: bool = True) -> list[str]:
    chunks: list[str] = []
    remaining = text
    while remaining:
        split_at = _take_utf16_chunk(remaining, limit, spaces=spaces)
        if split_at <= 0:
            # A valid Telegram budget always fits at least one Unicode scalar,
            # but keep this helper total for defensive/unit-test callers.
            split_at = 1
        chunks.append(remaining[:split_at])
        remaining = remaining[split_at:]
    return chunks


def _split_fenced_code_block(
    block: _FencedCodeBlock,
    limit: int,
) -> list[str]:
    opening = block.opening
    closing = block.closing
    wrapper_units = _utf16_units(f"{opening}\n\n{closing}")
    if wrapper_units >= limit:
        opening = block.fence
        closing = block.fence
        wrapper_units = _utf16_units(f"{opening}\n\n{closing}")
    if wrapper_units >= limit:
        return _split_plain_utf16(
            f"{block.opening}\n{block.content}{block.closing}",
            limit,
            spaces=False,
        )

    content_limit = limit - wrapper_units
    content_parts = _split_plain_utf16(
        block.content,
        content_limit,
        spaces=False,
    ) or [""]
    parts: list[str] = []
    for content in content_parts:
        separator = "" if content.endswith("\n") else "\n"
        part = f"{opening}\n{content}{separator}{closing}"
        # ``wrapper_units`` reserves the optional separator above, so this is
        # an invariant unless a caller supplied a nonsensically tiny budget.
        if _utf16_units(part) <= limit:
            parts.append(
                _FencedMarkdownPart(
                    part,
                    opening=opening,
                    content=content,
                )
            )
        else:
            parts.extend(_split_plain_utf16(part, limit, spaces=False))
    return parts


def _plain_fallback_for_part(part: str) -> str:
    if isinstance(part, _FencedMarkdownPart):
        return part.content
    return str(part)


def _split_for_telegram(text: str, limit: int) -> list[str]:
    """Split Markdown on UTF-16 limits while keeping fenced code valid."""
    source = str(text or "").strip()
    if not source or limit <= 0:
        return []
    if _utf16_units(source) <= limit:
        return [source]

    blocks = _find_fenced_code_blocks(source)
    if not blocks:
        return _split_plain_utf16(source, limit)

    parts: list[str] = []
    pending = ""

    def _flush_pending() -> None:
        nonlocal pending
        if pending.strip():
            parts.append(pending)
        pending = ""

    def _append_plain(value: str) -> None:
        nonlocal pending
        remaining = value
        while remaining:
            capacity = limit - _utf16_units(pending)
            if capacity <= 0:
                _flush_pending()
                capacity = limit
            split_at = _take_utf16_chunk(remaining, capacity)
            if split_at <= 0:
                _flush_pending()
                continue
            pending += remaining[:split_at]
            remaining = remaining[split_at:]
            if remaining:
                _flush_pending()

    cursor = 0
    for block in blocks:
        _append_plain(source[cursor:block.start])
        raw_block = source[block.start:block.end]
        if _utf16_units(raw_block) <= limit:
            if pending and _utf16_units(pending + raw_block) > limit:
                _flush_pending()
            pending += raw_block
        else:
            _flush_pending()
            parts.extend(_split_fenced_code_block(block, limit))
        cursor = block.end
    _append_plain(source[cursor:])
    _flush_pending()
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
        if len(_TELEGRAM_BACKGROUND_TASKS) >= _TELEGRAM_BACKGROUND_SOFT_LIMIT:
            # Typing is cosmetic.  Never add more work while real Telegram
            # operations are already showing cancellation pressure.
            return False
        task = asyncio.create_task(
            message.bot.send_chat_action(
                chat_id=chat_id,
                action=ChatAction.TYPING,
            ),
            name=f"typing-send:{chat_id}",
        )
        try:
            done, _pending = await asyncio.wait(
                {task},
                timeout=_TYPING_SEND_TIMEOUT_SECONDS,
            )
            if task in done:
                await task
                return True
            task.cancel()
            done, _pending = await asyncio.wait(
                {task},
                timeout=_TELEGRAM_CANCEL_GRACE_SECONDS,
            )
            if task in done:
                _observe_telegram_background_task(task)
            else:
                _track_telegram_background_task(task)
            return False
        except asyncio.CancelledError:
            task.cancel()
            done, _pending = await asyncio.wait(
                {task},
                timeout=_TELEGRAM_CANCEL_GRACE_SECONDS,
            )
            if task in done:
                _observe_telegram_background_task(task)
            else:
                _track_telegram_background_task(task)
            raise
        except Exception:
            if not task.done():
                task.cancel()
                done, _pending = await asyncio.wait(
                    {task},
                    timeout=_TELEGRAM_CANCEL_GRACE_SECONDS,
                )
                if task in done:
                    _observe_telegram_background_task(task)
                else:
                    _track_telegram_background_task(task)
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
        done, _pending = await asyncio.wait(
            {task},
            timeout=_TYPING_WORKER_CANCEL_GRACE_SECONDS,
        )
        if task in done:
            with suppress(asyncio.CancelledError, Exception):
                await task
        else:
            _track_telegram_background_task(task)


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
    disable_link_preview: bool | None = None,
    on_delivery: Callable[[], None] | None = None,
    on_ambiguous: Callable[[], None] | None = None,
    overlay: ReplyMessageOverlay | None = None,
    overlay_remove_after: float = 2.0,
    rich: bool = False,
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
    link_preview_kwargs = (
        {"disable_web_page_preview": bool(disable_link_preview)}
        if disable_link_preview is not None
        else {}
    )
    overlay_compatible = bool(
        overlay is not None
        and overlay.sent_as_reply
        and send_as_reply
        and int(getattr(getattr(overlay.message, "chat", None), "id", 0) or 0)
        == int(getattr(message.chat, "id", 0) or 0)
        and (
            explicit_reply_target is None
            or explicit_reply_target == overlay.reply_to_message_id
        )
        and str(overlay.status_html or "").strip()
    )

    def _mark_overlay_ambiguous() -> None:
        if overlay is not None:
            overlay.outcome = "ambiguous"
        if on_ambiguous is None:
            return
        try:
            on_ambiguous()
        except Exception:
            # The Telegram request may already have taken effect. Receipt
            # bookkeeping must never turn that uncertainty into a resend.
            log.exception("Telegram ambiguous-delivery callback failed")

    async def _safe_send(
        body: str,
        *,
        parse_mode: str | None,
        retries: int = 1,
        retry_delay: float = 0.8,
        schedule_cleanup: bool = True,
    ) -> Message | None:
        attempt = 0
        current_reply_id = explicit_reply_target if send_as_reply else None
        while attempt <= retries:
            try:
                if parse_mode == RICH_MARKDOWN_MODE:
                    reply_params = None
                    if send_as_reply:
                        target_id = current_reply_id or (
                            None
                            if explicit_reply_target
                            else int(getattr(message, "message_id", 0) or 0)
                        )
                        if target_id:
                            reply_params = ReplyParameters(message_id=target_id)
                    # skip_entity_detection: outgoing text must never grow live
                    # @mentions the sanitizer did not approve.
                    sent = await message.bot.send_rich_message(
                        chat_id=message.chat.id,
                        rich_message=InputRichMessage(
                            markdown=body,
                            skip_entity_detection=True,
                        ),
                        reply_parameters=reply_params,
                    )
                elif send_as_reply:
                    if current_reply_id:
                        sent = await message.bot.send_message(
                            chat_id=message.chat.id,
                            text=body,
                            parse_mode=parse_mode,
                            reply_to_message_id=current_reply_id,
                            **link_preview_kwargs,
                        )
                    elif explicit_reply_target:
                        sent = await message.answer(
                            body,
                            parse_mode=parse_mode,
                            **link_preview_kwargs,
                        )
                    else:
                        sent = await message.reply(
                            body,
                            parse_mode=parse_mode,
                            **link_preview_kwargs,
                        )
                else:
                    sent = await message.answer(
                        body,
                        parse_mode=parse_mode,
                        **link_preview_kwargs,
                    )
                confirm_telegram_delivery(on_delivery)
                if schedule_cleanup:
                    await _schedule_delivered_message_cleanup(
                        sent,
                        auto_delete_seconds,
                    )
                return sent
            except TelegramRetryAfter as exc:
                wait_s = _bounded_retry_after_seconds(exc)
                if wait_s is None:
                    log.warning("telegram flood control exceeds send deadline; aborting")
                    return None
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
                await sent.edit_text(
                    body,
                    parse_mode=parse_mode,
                    **link_preview_kwargs,
                )
                return True
            except TelegramBadRequest as exc:
                detail = str(exc).lower()
                if "message is not modified" in detail:
                    return True
                return False
            except TelegramRetryAfter as exc:
                wait_s = _bounded_retry_after_seconds(exc)
                if wait_s is None:
                    return False
                log.warning("telegram flood control on edit, waiting %.2fs", wait_s)
                await asyncio.sleep(wait_s)
                attempt += 1
            except Exception:
                if attempt >= retries:
                    return False
                attempt += 1
                await asyncio.sleep(0.3 * attempt)
        return False

    async def _safe_overlay_edit(
        sent: Message,
        body: str,
        *,
        parse_mode: str | None,
        retries: int = 2,
    ) -> Literal["success", "definite_failure", "ambiguous"]:
        """Edit one known message without turning ambiguity into a resend."""

        attempt = 0
        saw_ambiguous_result = False
        while attempt <= retries:
            try:
                await sent.edit_text(
                    body,
                    parse_mode=parse_mode,
                    **link_preview_kwargs,
                )
                return "success"
            except TelegramBadRequest as exc:
                if "message is not modified" in str(exc).lower():
                    return "success"
                return (
                    "ambiguous" if saw_ambiguous_result else "definite_failure"
                )
            except TelegramRetryAfter as exc:
                wait_s = _bounded_retry_after_seconds(exc)
                if wait_s is None:
                    return (
                        "ambiguous" if saw_ambiguous_result else "definite_failure"
                    )
                await asyncio.sleep(wait_s)
                attempt += 1
            except Exception:
                saw_ambiguous_result = True
                if attempt >= retries:
                    return "ambiguous"
                attempt += 1
                await asyncio.sleep(0.3 * attempt)
        return "ambiguous" if saw_ambiguous_result else "definite_failure"

    async def _schedule_overlay_removal(
        sent: Message,
        *,
        final_body: str,
        parse_mode: str | None,
        plain_fallback: str | None = None,
        confirm_on_success: bool = False,
    ) -> bool:
        delay = max(0.0, float(overlay_remove_after))

        async def _converge_to_body() -> bool:
            result = await _safe_overlay_edit(
                sent,
                final_body,
                parse_mode=parse_mode,
                retries=4,
            )
            if (
                result == "definite_failure"
                and parse_mode is not None
                and plain_fallback is not None
            ):
                result = await _safe_overlay_edit(
                    sent,
                    plain_fallback,
                    parse_mode=None,
                    retries=2,
                )
            if result == "success":
                if confirm_on_success:
                    confirm_telegram_delivery(on_delivery)
                return True
            log.warning(
                "Telegram reply overlay did not converge to body | "
                "chat_id=%s message_id=%s outcome=%s",
                getattr(getattr(sent, "chat", None), "id", "?"),
                getattr(sent, "message_id", "?"),
                result,
            )
            return False

        async def _remove() -> None:
            try:
                if delay > 0:
                    await asyncio.sleep(delay)
                await _converge_to_body()
            finally:
                if auto_delete_seconds == AUTO_DELETE_BUTTON_SENTINEL:
                    await _schedule_delivered_message_cleanup(
                        sent,
                        auto_delete_seconds,
                    )

        scheduled = _schedule_telegram_background_task(
            _remove(),
            name=(
                "telegram-reply-overlay-remove:"
                f"{getattr(getattr(sent, 'chat', None), 'id', 0)}:"
                f"{getattr(sent, 'message_id', 0)}"
            ),
        )
        # Arm the status-strip task before awaiting durable cleanup so caller
        # cancellation cannot leave an attached overlay without a convergence
        # task. Button mode is attached only after the text edit completes.
        if auto_delete_seconds != AUTO_DELETE_BUTTON_SENTINEL:
            await _schedule_delivered_message_cleanup(sent, auto_delete_seconds)
        if scheduled:
            return True

        # Capacity pressure must not leave the progress quote permanently
        # attached. Converge immediately on the same message ID; never resend.
        converged = await _converge_to_body()
        if auto_delete_seconds == AUTO_DELETE_BUTTON_SENTINEL:
            await _schedule_delivered_message_cleanup(sent, auto_delete_seconds)
        return converged

    def _arm_overlay_reconciliation(
        sent: Message,
        *,
        final_body: str,
        parse_mode: str | None,
        plain_fallback: str | None,
    ) -> None:
        scheduled = _schedule_telegram_background_task(
            _schedule_overlay_removal(
                sent,
                final_body=final_body,
                parse_mode=parse_mode,
                plain_fallback=plain_fallback,
                confirm_on_success=True,
            ),
            name=(
                "telegram-reply-overlay-reconcile:"
                f"{getattr(getattr(sent, 'chat', None), 'id', 0)}:"
                f"{getattr(sent, 'message_id', 0)}"
            ),
        )
        if not scheduled:
            log.error(
                "Telegram reply overlay reconciliation could not be armed | "
                "chat_id=%s message_id=%s",
                getattr(getattr(sent, "chat", None), "id", "?"),
                getattr(sent, "message_id", "?"),
            )

    async def _attempt_overlay_edit(
        sent: Message,
        body: str,
        *,
        parse_mode: str | None,
        retries: int,
        reconcile_body: str,
        reconcile_parse_mode: str | None,
        reconcile_plain_fallback: str | None,
    ) -> Literal["success", "definite_failure", "ambiguous"]:
        try:
            return await _safe_overlay_edit(
                sent,
                body,
                parse_mode=parse_mode,
                retries=retries,
            )
        except asyncio.CancelledError:
            _mark_overlay_ambiguous()
            _arm_overlay_reconciliation(
                sent,
                final_body=reconcile_body,
                parse_mode=reconcile_parse_mode,
                plain_fallback=reconcile_plain_fallback,
            )
            raise

    async def _try_attach_overlay(
        *,
        html_body: str,
        plain_body: str,
    ) -> Message | None:
        if overlay is None:
            return None

        overlay.outcome = "attempting"
        status_html = str(overlay.status_html or "").strip()
        combined_html = f"{status_html}\n\n{html_body}"
        combined_result = await _attempt_overlay_edit(
            overlay.message,
            combined_html,
            parse_mode="HTML",
            retries=2,
            reconcile_body=html_body,
            reconcile_parse_mode="HTML",
            reconcile_plain_fallback=plain_body,
        )
        if combined_result == "success":
            overlay.outcome = "attached"
            confirm_telegram_delivery(on_delivery)
            await _schedule_overlay_removal(
                overlay.message,
                final_body=html_body,
                parse_mode="HTML",
                plain_fallback=plain_body,
            )
            return overlay.message
        if combined_result == "ambiguous":
            _mark_overlay_ambiguous()
            await _schedule_overlay_removal(
                overlay.message,
                final_body=html_body,
                parse_mode="HTML",
                plain_fallback=plain_body,
                confirm_on_success=True,
            )
            return None

        # A definite entity/edit rejection is safe to recover on the same
        # message ID. Prefer the canonical formatted body, then plain text.
        body_result = await _attempt_overlay_edit(
            overlay.message,
            html_body,
            parse_mode="HTML",
            retries=1,
            reconcile_body=html_body,
            reconcile_parse_mode="HTML",
            reconcile_plain_fallback=plain_body,
        )
        if body_result == "success":
            overlay.outcome = "attached"
            confirm_telegram_delivery(on_delivery)
            await _schedule_delivered_message_cleanup(
                overlay.message,
                auto_delete_seconds,
            )
            return overlay.message
        if body_result == "ambiguous":
            _mark_overlay_ambiguous()
            await _schedule_overlay_removal(
                overlay.message,
                final_body=html_body,
                parse_mode="HTML",
                plain_fallback=plain_body,
                confirm_on_success=True,
            )
            return None

        plain_result = await _attempt_overlay_edit(
            overlay.message,
            plain_body,
            parse_mode=None,
            retries=1,
            reconcile_body=plain_body,
            reconcile_parse_mode=None,
            reconcile_plain_fallback=None,
        )
        if plain_result == "success":
            overlay.outcome = "attached"
            confirm_telegram_delivery(on_delivery)
            await _schedule_delivered_message_cleanup(
                overlay.message,
                auto_delete_seconds,
            )
            return overlay.message
        if plain_result == "ambiguous":
            _mark_overlay_ambiguous()
            await _schedule_overlay_removal(
                overlay.message,
                final_body=plain_body,
                parse_mode=None,
                confirm_on_success=True,
            )
        else:
            overlay.outcome = "definite_failure"
        return None

    async def _finalize_stream_format(sent: Message, segment: str) -> bool:
        """Try to preserve markdown formatting after stream plain-text phase."""
        final_html = md_to_html(segment)
        # Streaming initially sends plain text so incremental edits cannot
        # expose half-built entities. A pre-rendered Telegram HTML notice does
        # not change in ``md_to_html``; it still needs one final HTML edit or
        # clients will display its tags literally.
        if final_html == segment and _TG_HTML_TAG_RE.search(segment) is None:
            return True

        html_ok = await _safe_edit(sent, final_html, parse_mode="HTML", retries=2)
        if html_ok:
            return True

        # Keep single-message behavior: final fallback edits same message as plain text.
        plain_ok = await _safe_edit(sent, segment, parse_mode=None, retries=1)
        if plain_ok:
            return True
        log.warning("stream format finalize failed to apply markdown/html")
        return False

    async def _send_stream_segment(segment: str) -> bool:
        # Bound edit amplification and pacing time.  The old 36-character
        # cadence could spend almost a minute editing a 2K reply and exceed the
        # reply-batch deadline before the final text landed.
        adaptive_chunk_size = max(
            int(stream_chunk_size),
            max(1, (len(segment) + _STREAM_MAX_INCREMENTAL_EDITS - 1)
                // _STREAM_MAX_INCREMENTAL_EDITS),
        )
        chunks = _stream_chunks(segment, chunk_size=adaptive_chunk_size)
        if len(chunks) <= 1 and len(segment) >= 18:
            mid = max(1, len(segment) // 2)
            chunks = [segment[:mid], segment[mid:]]

        # Cleanup is applied after the final edit: an inline delete button
        # attached at send time would be dropped by the streaming edits.
        if len(chunks) <= 1:
            sent = await _safe_send(
                segment, parse_mode=None, retries=3, schedule_cleanup=False
            )
            if not sent:
                return False
            ok = await _finalize_stream_format(sent, segment)
            await _schedule_delivered_message_cleanup(sent, auto_delete_seconds)
            return ok

        sent = await _safe_send(
            chunks[0], parse_mode=None, retries=3, schedule_cleanup=False
        )
        if not sent:
            return False

        merged = chunks[0]
        last_edit_ts = time.monotonic()
        effective_interval = min(
            max(0.0, float(stream_interval)),
            _STREAM_MAX_PACING_SECONDS / max(1, len(chunks) - 1),
        )
        for chunk in chunks[1:]:
            merged += chunk
            elapsed = time.monotonic() - last_edit_ts
            if elapsed < effective_interval:
                await asyncio.sleep(effective_interval - elapsed)
            edited = await _safe_edit(sent, merged, parse_mode=None, retries=3)
            if edited:
                last_edit_ts = time.monotonic()

        final_plain_ok = await _safe_edit(sent, segment, parse_mode=None, retries=3)
        if not final_plain_ok:
            # Do not send/delete as fallback to avoid duplicate notifications.
            await _schedule_delivered_message_cleanup(
                sent,
                auto_delete_seconds,
            )
            return False

        ok = await _finalize_stream_format(sent, segment)
        await _schedule_delivered_message_cleanup(sent, auto_delete_seconds)
        return ok

    payload = sanitize_outgoing_text((text or "").strip())
    pre_rendered_html = _contains_telegram_html_outside_markdown_code(payload)
    if not pre_rendered_html:
        # Trusted HTML templates control their own layout; model Markdown gets
        # blank lines between block structures so it stops rendering cramped.
        payload = normalize_block_layout(payload)
    payload = sanitize_outgoing_mentions(
        payload,
        monospace=pre_rendered_html,
    )
    if not payload:
        return False
    # Incrementally editing a half-open fence cannot produce a valid Telegram
    # code entity. Code answers are finalized in one edit; an adopted progress
    # overlay already carries the useful intermediate state.
    # Rich messages carry final Markdown in one request; incremental plain-text
    # edits would defeat the point, so rich delivery never streams. Ordinary
    # chat formatting stays on the normal message path; sendRichMessage is used
    # only when the payload contains rich-only structures it alone can render.
    effective_rich = bool(
        rich
        and _utf16_units(payload) <= TG_RICH_MESSAGE_LIMIT
        and _needs_rich_markdown(payload)
    )
    effective_stream = bool(
        stream
        and not effective_rich
        and not overlay_compatible
        and not _find_fenced_code_blocks(payload)
    )

    semaphore = _SEND_SEMAPHORES.setdefault(
        message.chat.id,
        asyncio.Semaphore(CHAT_SEND_PARALLEL),
    )

    async def _deliver_payload() -> bool:
        async with semaphore:
            if effective_rich:
                sent = await _safe_send(
                    payload,
                    parse_mode=RICH_MARKDOWN_MODE,
                    retries=1,
                )
                if sent:
                    return True
                log.warning(
                    "rich message send failed, falling back to HTML | chat_id=%s",
                    message.chat.id,
                )
            # Pre-rendered HTML must not be streamed as literal, half-open tags.
            # Send it once as HTML and retain the normal format fallbacks.
            if not effective_stream or pre_rendered_html:
                split_limit = TG_STREAM_SAFE_LIMIT if effective_stream else TG_MESSAGE_LIMIT
                parts = (
                    [payload]
                    if pre_rendered_html
                    and _telegram_html_text_units(payload) <= TG_MESSAGE_LIMIT
                    else _split_for_telegram(payload, limit=split_limit)
                )
                ok = True
                for index, part in enumerate(parts):
                    html_body = part if pre_rendered_html else md_to_html(part)
                    plain_body = (
                        _telegram_html_to_plain(part)
                        if pre_rendered_html
                        else _plain_fallback_for_part(part)
                    )
                    if (
                        index == 0
                        and len(parts) == 1
                        and overlay_compatible
                        and overlay is not None
                        and _telegram_html_text_units(
                            f"{overlay.status_html.strip()}\n\n{html_body}"
                        )
                        <= TG_MESSAGE_LIMIT
                    ):
                        sent = await _try_attach_overlay(
                            html_body=html_body,
                            plain_body=plain_body,
                        )
                        if sent is not None:
                            continue
                        if overlay.outcome == "ambiguous":
                            return False
                    sent = await _safe_send(html_body, parse_mode="HTML", retries=3)
                    if not sent:
                        sent = await _safe_send(plain_body, parse_mode=None, retries=2)
                    ok = ok and bool(sent)
                return ok

            parts = _split_for_telegram(payload, limit=TG_STREAM_SAFE_LIMIT)
            if not parts:
                return False

            all_ok = True
            for part in parts:
                if len(part) > TG_MESSAGE_LIMIT:
                    slices = [
                        part[i : i + TG_STREAM_SAFE_LIMIT]
                        for i in range(0, len(part), TG_STREAM_SAFE_LIMIT)
                    ]
                else:
                    slices = [part]
                for segment in slices:
                    seg_ok = await _send_stream_segment(segment)
                    all_ok = all_ok and seg_ok
            return all_ok

    try:
        async with asyncio.timeout(_send_total_deadline_seconds()):
            return await _deliver_payload()
    except TimeoutError:
        log.error("Telegram reply total deadline exceeded | chat_id=%s", message.chat.id)
        return False


async def send_reply_messages(
    message: Message,
    texts: list[str],
    *,
    delivery_mode: str = "reply",
    stream: bool = False,
    stream_chunk_size: int = 36,
    stream_interval: float = 1.0,
    auto_delete_seconds: int = 0,
    disable_link_preview: bool | None = None,
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
            disable_link_preview=disable_link_preview,
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
    disable_link_preview: bool | None = None,
    on_delivery: Callable[[], None] | None = None,
) -> bool:
    payload = sanitize_outgoing_text((text or "").strip())
    payload = sanitize_outgoing_mentions(payload, monospace=False)
    if not payload:
        return False

    fallback_prefix_html = ""
    if fallback_mention_user_id:
        shown = html.escape((fallback_mention_name or str(fallback_mention_user_id)).strip())
        fallback_prefix_html = f'<a href="tg://user?id={fallback_mention_user_id}">@{shown}</a> '

    link_preview_kwargs = (
        {"disable_web_page_preview": bool(disable_link_preview)}
        if disable_link_preview is not None
        else {}
    )

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
                    **link_preview_kwargs,
                )
                confirm_telegram_delivery(on_delivery)
                await _schedule_delivered_message_cleanup(
                    sent,
                    auto_delete_seconds,
                )
                return sent
            except TelegramRetryAfter as exc:
                wait_s = _bounded_retry_after_seconds(exc)
                if wait_s is None:
                    log.warning("telegram flood control exceeds scheduled send deadline; aborting")
                    return None
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

    semaphore = _SEND_SEMAPHORES.setdefault(
        chat_id,
        asyncio.Semaphore(CHAT_SEND_PARALLEL),
    )
    try:
        async with asyncio.timeout(_send_total_deadline_seconds()):
            async with semaphore:
                ok = True
                for part in _split_for_telegram(payload, limit=TG_MESSAGE_LIMIT):
                    html_body = md_to_html(part)
                    sent = await _safe_send(
                        html_body,
                        parse_mode="HTML",
                        reply_id=reply_to_message_id,
                        retries=3,
                    )
                    if not sent:
                        sent = await _safe_send(
                            _plain_fallback_for_part(part),
                            parse_mode=None,
                            reply_id=reply_to_message_id,
                            retries=2,
                        )
                    ok = ok and bool(sent)
                return ok
    except TimeoutError:
        log.error("Telegram scheduled send total deadline exceeded | chat_id=%s", chat_id)
        return False


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
