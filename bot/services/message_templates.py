"""Trusted, admin-authored message templates and inline buttons.

Welcome messages, keyword replies, and scheduled announcements share the
same deliberately small Markdown dialect and button schema.  The renderer
escapes raw HTML first, so a malformed template can never inject Telegram
HTML; only the Markdown constructs converted below become entities.
"""
from __future__ import annotations

import html
import re
from collections import defaultdict
from collections.abc import Awaitable, Callable, Mapping
from typing import Any
from urllib.parse import urlencode, urlparse

from aiogram.exceptions import TelegramBadRequest
from aiogram.types import (
    CopyTextButton,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)

from bot.utils.telegram import DELETE_BUTTON_CALLBACK_DATA

TEMPLATE_BUTTON_ACTIONS = frozenset({"url", "copy", "share", "dismiss"})
MAX_TEMPLATE_BUTTONS = 12
MAX_TEMPLATE_BUTTON_TEXT = 64
MAX_TEMPLATE_BUTTON_VALUE = 2048
MAX_TEMPLATE_COPY_VALUE = 256
MAX_TEMPLATE_BUTTON_ROWS = 8

_TOKEN_RE = re.compile(r"\x00tpl(\d+)\x00")
_FENCED_CODE_RE = re.compile(r"```(?:[A-Za-z0-9_+.-]+)?\n?([\s\S]*?)```")
_INLINE_CODE_RE = re.compile(r"`([^`\n]+)`")
_MARKDOWN_LINK_RE = re.compile(r"\[([^\]\n]+)\]\(([^)\s]+)\)")


def _safe_link(value: object) -> str:
    raw = str(value or "").strip()
    if not raw or len(raw) > MAX_TEMPLATE_BUTTON_VALUE:
        return ""
    parsed = urlparse(raw)
    if parsed.scheme.lower() in {"http", "https"} and parsed.netloc:
        return raw
    if parsed.scheme.lower() == "tg" and (parsed.netloc or parsed.path):
        return raw
    return ""


def normalize_template_buttons(value: object) -> list[dict[str, Any]]:
    """Validate and normalize the public template-button JSON shape.

    Each button is ``{text, action, value, row}``.  ``row`` is zero-based;
    omitted rows are assigned in source order, producing one button per row.
    The UI can give several buttons the same row number for a horizontal row.
    """
    if value in (None, ""):
        return []
    if not isinstance(value, list):
        raise ValueError("内联按钮必须是数组。")
    if len(value) > MAX_TEMPLATE_BUTTONS:
        raise ValueError(f"内联按钮最多 {MAX_TEMPLATE_BUTTONS} 个。")

    normalized: list[dict[str, Any]] = []
    for index, raw in enumerate(value):
        if not isinstance(raw, Mapping):
            raise ValueError(f"第 {index + 1} 个内联按钮格式无效。")
        unknown = set(raw) - {"text", "action", "value", "row"}
        if unknown:
            raise ValueError(
                f"第 {index + 1} 个内联按钮包含不支持的字段："
                f"{', '.join(sorted(str(item) for item in unknown))}。"
            )
        text = str(raw.get("text") or "").strip()
        if not text:
            raise ValueError(f"第 {index + 1} 个内联按钮缺少按钮名称。")
        if len(text) > MAX_TEMPLATE_BUTTON_TEXT:
            raise ValueError(
                f"第 {index + 1} 个内联按钮名称不能超过 "
                f"{MAX_TEMPLATE_BUTTON_TEXT} 个字符。"
            )
        action = str(raw.get("action") or "url").strip().lower()
        if action not in TEMPLATE_BUTTON_ACTIONS:
            raise ValueError(
                f"第 {index + 1} 个内联按钮操作无效；"
                "可用 url、copy、share、dismiss。"
            )
        value_text = str(raw.get("value") or "").strip()
        if len(value_text) > MAX_TEMPLATE_BUTTON_VALUE:
            raise ValueError(
                f"第 {index + 1} 个内联按钮内容不能超过 "
                f"{MAX_TEMPLATE_BUTTON_VALUE} 个字符。"
            )
        if action == "url":
            safe_url = _safe_link(value_text)
            if not safe_url:
                raise ValueError(
                    f"第 {index + 1} 个按钮需要有效的 http(s):// 或 tg:// 链接。"
                )
            value_text = safe_url
        elif action in {"copy", "share"} and not value_text:
            raise ValueError(f"第 {index + 1} 个按钮需要填写操作内容。")
        elif action in {"copy", "share"} and len(value_text) > MAX_TEMPLATE_COPY_VALUE:
            raise ValueError(
                f"第 {index + 1} 个按钮的复制/分享内容不能超过 "
                f"{MAX_TEMPLATE_COPY_VALUE} 个字符。"
            )
        elif action == "dismiss":
            value_text = ""

        raw_row = raw.get("row", index)
        if isinstance(raw_row, bool):
            raise ValueError(f"第 {index + 1} 个按钮的行号无效。")
        try:
            row = int(raw_row)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"第 {index + 1} 个按钮的行号无效。") from exc
        if row < 0 or row >= MAX_TEMPLATE_BUTTON_ROWS:
            raise ValueError(
                f"第 {index + 1} 个按钮行号需在 0 到 "
                f"{MAX_TEMPLATE_BUTTON_ROWS - 1} 之间。"
            )
        normalized.append(
            {"text": text, "action": action, "value": value_text, "row": row}
        )
    row_counts: dict[int, int] = defaultdict(int)
    for item in normalized:
        row_counts[int(item["row"])] += 1
        if row_counts[int(item["row"])] > 8:
            raise ValueError("同一行最多放置 8 个内联按钮。")
    return normalized


def build_template_keyboard(value: object) -> InlineKeyboardMarkup | None:
    """Build a Telegram keyboard, ignoring corrupt legacy data safely."""
    try:
        buttons = normalize_template_buttons(value)
    except ValueError:
        return None
    if not buttons:
        return None

    rows: dict[int, list[InlineKeyboardButton]] = defaultdict(list)
    for item in buttons:
        action = item["action"]
        kwargs: dict[str, Any] = {"text": item["text"]}
        if action == "url":
            kwargs["url"] = item["value"]
        elif action == "copy":
            kwargs["copy_text"] = CopyTextButton(text=item["value"])
        elif action == "share":
            # Telegram's public share deep link works even when this bot does
            # not have inline mode enabled, unlike switch_inline_query.
            kwargs["url"] = "https://t.me/share/url?" + urlencode(
                {"text": item["value"]}
            )
        else:
            # Reuse the existing admin-authorized delete callback.  This
            # makes "dismiss" useful without allowing ordinary members to
            # remove announcements or moderation-adjacent messages.
            kwargs["callback_data"] = DELETE_BUTTON_CALLBACK_DATA
        rows[int(item["row"])].append(InlineKeyboardButton(**kwargs))
    return InlineKeyboardMarkup(
        inline_keyboard=[rows[row] for row in sorted(rows) if rows[row]]
    )


def _formatted_text_rejected(exc: TelegramBadRequest) -> bool:
    detail = str(exc).lower()
    return "can't parse entities" in detail or "message is too long" in detail


async def send_template_with_fallback(
    send: Callable[..., Awaitable[Any]],
    *,
    formatted_text: str,
    plain_text: str,
    reply_markup: InlineKeyboardMarkup | None,
) -> Any:
    """Send a template while keeping its body if formatting/buttons fail.

    Telegram can reject a keyboard even after local validation (for example a
    newly unsupported URL/button type). In that case the announcement body is
    more important than the optional controls, so retry without the keyboard.
    Entity and length failures retain the existing plain-text fallback.
    """
    try:
        return await send(
            formatted_text,
            parse_mode="HTML",
            reply_markup=reply_markup,
        )
    except TelegramBadRequest as exc:
        if _formatted_text_rejected(exc):
            try:
                return await send(
                    plain_text,
                    parse_mode=None,
                    reply_markup=reply_markup,
                )
            except TelegramBadRequest:
                if reply_markup is None:
                    raise
                return await send(plain_text, parse_mode=None, reply_markup=None)
        if reply_markup is None:
            raise
        try:
            return await send(
                formatted_text,
                parse_mode="HTML",
                reply_markup=None,
            )
        except TelegramBadRequest as retry_exc:
            if not _formatted_text_rejected(retry_exc):
                raise
            return await send(plain_text, parse_mode=None, reply_markup=None)


def _stash(tokens: list[str], rendered: str) -> str:
    index = len(tokens)
    tokens.append(rendered)
    return f"\x00tpl{index}\x00"


def render_markdown_html(
    text: object,
    *,
    replacements: Mapping[str, str] | None = None,
) -> str:
    """Render a safe subset of familiar Markdown into Telegram HTML.

    Supported constructs: headings, bold, italic, strike-through, inline and
    fenced code, block quotes, and Markdown links.  Newlines are preserved.
    ``replacements`` values are already-safe HTML snippets used by welcome
    placeholders such as ``{mention}``.
    """
    source = str(text or "").replace("\r\n", "\n").replace("\r", "\n")
    replacement_tokens: dict[str, str] = {}
    for index, (needle, rendered) in enumerate((replacements or {}).items()):
        token = f"TPLREPLACEMENT{index}TOKEN"
        source = source.replace(str(needle), token)
        replacement_tokens[token] = str(rendered)

    tokens: list[str] = []
    source = _FENCED_CODE_RE.sub(
        lambda match: _stash(tokens, f"<pre><code>{html.escape(match.group(1))}</code></pre>"),
        source,
    )
    source = _INLINE_CODE_RE.sub(
        lambda match: _stash(tokens, f"<code>{html.escape(match.group(1))}</code>"),
        source,
    )

    def _link(match: re.Match[str]) -> str:
        url = _safe_link(match.group(2))
        if not url:
            return match.group(0)
        return _stash(
            tokens,
            f'<a href="{html.escape(url, quote=True)}">{html.escape(match.group(1))}</a>',
        )

    source = _MARKDOWN_LINK_RE.sub(_link, source)
    rendered = html.escape(source)
    rendered = re.sub(
        r"(?m)^#{1,6}[ \t]+(.+)$",
        r"<b>\1</b>",
        rendered,
    )
    rendered = re.sub(r"\*\*([^*\n]+?)\*\*", r"<b>\1</b>", rendered)
    rendered = re.sub(r"__([^_\n]+?)__", r"<b>\1</b>", rendered)
    rendered = re.sub(r"~~([^~\n]+?)~~", r"<s>\1</s>", rendered)
    rendered = re.sub(r"(?<!\*)\*([^*\n]+?)\*(?!\*)", r"<i>\1</i>", rendered)
    rendered = re.sub(r"(?<!\w)_([^_\n]+?)_(?!\w)", r"<i>\1</i>", rendered)
    rendered = re.sub(r"(?m)^&gt;[ \t]?(.*)$", r"<blockquote>\1</blockquote>", rendered)

    rendered = _TOKEN_RE.sub(
        lambda match: tokens[int(match.group(1))],
        rendered,
    )
    for token, replacement in replacement_tokens.items():
        rendered = rendered.replace(token, replacement)
    return rendered.strip()


def render_plain_template(
    text: object,
    *,
    replacements: Mapping[str, str] | None = None,
) -> str:
    """Plain-text fallback retaining line breaks and placeholder values."""
    rendered = str(text or "").replace("\r\n", "\n").replace("\r", "\n")
    for needle, replacement in (replacements or {}).items():
        rendered = rendered.replace(str(needle), str(replacement))
    return rendered.strip()


# Shared field syntax for the four system-message layouts below. Field rows use
# a full-width ideographic space between label and value so quoted metadata is
# easy to scan without depending on fragile manual column alignment.
CARD_FIELD_SPACE = "　"


def card_field(label: str, value: object) -> str:
    """Render one ``<b>label</b>　value`` row for a notice-card body.

    ``label`` is a trusted, caller-authored constant.  ``value`` must already
    be HTML-safe — escaped, or built from trusted fragments (mentions, ``<code>``
    handles) by the caller — because it is emitted verbatim.
    """
    return f"<b>{label}</b>{CARD_FIELD_SPACE}{value}"


def render_notice_card(title: str, body: object) -> str:
    """Render the original one-quote card retained for compatibility.

    ``title`` is plain text and is escaped here.  ``body`` is trusted HTML —
    either a single pre-built string or an iterable of pre-built lines joined
    with newlines (typically :func:`card_field` rows).
    """
    if not isinstance(body, str):
        body = "\n".join(str(line) for line in body)
    return f"<b>{html.escape(str(title))}</b>\n<blockquote>{body}</blockquote>"


def _coerce_html_body(body: object | None) -> str:
    """Join pre-rendered HTML lines while preserving intentional blank rows."""
    if body is None:
        return ""
    if isinstance(body, str):
        return body.strip()
    try:
        return "\n".join(str(line) for line in body).strip()
    except TypeError:
        return str(body).strip()


def truncate_telegram_text(
    value: object,
    max_units: int,
    *,
    suffix: str = "...",
) -> str:
    """Truncate text using Telegram's UTF-16 message-length accounting."""
    source = "" if value is None else str(value)
    limit = max(0, int(max_units))

    def units(text: str) -> int:
        return len(text.encode("utf-16-le")) // 2

    def take(text: str, budget: int) -> str:
        used = 0
        chars: list[str] = []
        for char in text:
            width = 2 if ord(char) > 0xFFFF else 1
            if used + width > budget:
                break
            chars.append(char)
            used += width
        return "".join(chars)

    if units(source) <= limit:
        return source
    ending = str(suffix or "")
    ending_units = units(ending)
    if ending_units >= limit:
        return take(ending, limit)
    return take(source, limit - ending_units).rstrip() + ending


def render_expandable_blockquote(body: object | None) -> str:
    """Render optional Telegram expandable-quote details.

    The input follows the same convention as :func:`render_notice_card`:
    it is already-safe Telegram HTML.  Returning an empty string for an empty
    body lets callers append optional details without producing an invalid
    empty quote entity.
    """
    rendered = _coerce_html_body(body)
    if not rendered:
        return ""
    return f"<blockquote expandable>{rendered}</blockquote>"


def render_summary_notice(
    title: str,
    summary: object | None = None,
    *,
    details: object | None = None,
) -> str:
    """Render a concise notice with optional expandable supporting detail.

    This is intended for moderation, voting, raid, and escalation notices:
    the group-visible outcome remains immediately readable while evidence,
    rationale, and follow-up policy can be collapsed below it. ``summary``
    and ``details`` are pre-rendered safe Telegram HTML.
    """
    parts = [f"<b>{html.escape(str(title))}</b>"]
    summary_text = _coerce_html_body(summary)
    if summary_text:
        parts.append(f"<blockquote>{summary_text}</blockquote>")
    detail_text = render_expandable_blockquote(details)
    if detail_text:
        parts.append(detail_text)
    return "\n\n".join(parts)


def render_progress_notice(
    title: str,
    *,
    completed: object | None = None,
    current: object | None = None,
    next_step: object | None = None,
    action: object | None = None,
    details: object | None = None,
) -> str:
    """Render a flow-oriented notice for verification and background tasks.

    ``completed`` accepts one line or an iterable of lines and is rendered
    with strikethrough. ``current`` and ``next_step`` become stable field
    rows in the progress quote. ``action`` stays outside the quote so the
    member's next action remains prominent. All non-title arguments are
    pre-rendered safe Telegram HTML, allowing links and code entities.
    """
    progress_lines: list[str] = []
    completed_text = _coerce_html_body(completed)
    if completed_text:
        progress_lines.extend(
            f"<s>{line}</s>"
            for line in completed_text.split("\n")
            if line.strip()
        )
    current_text = _coerce_html_body(current)
    if current_text:
        progress_lines.append(card_field("当前", current_text))
    next_text = _coerce_html_body(next_step)
    if next_text:
        progress_lines.append(card_field("下一步", next_text))

    parts = [f"<b>{html.escape(str(title))}</b>"]
    if progress_lines:
        parts.append(f"<blockquote>{'\n'.join(progress_lines)}</blockquote>")
    action_text = _coerce_html_body(action)
    if action_text:
        parts.append(action_text)
    detail_text = render_expandable_blockquote(details)
    if detail_text:
        parts.append(detail_text)
    return "\n\n".join(parts)


def render_action_notice(
    title: str,
    *,
    context: object | None = None,
    action: object | None = None,
    details: object | None = None,
) -> str:
    """Render an action-first command response with optional detail.

    ``context`` is a short, unquoted line such as a target mention and
    deadline. ``action`` is placed in a quote for the result or required next
    action. Use :func:`render_summary_notice` when the quote itself is the
    primary content rather than a command response. All non-title arguments
    are pre-rendered safe Telegram HTML.
    """
    parts = [f"<b>{html.escape(str(title))}</b>"]
    context_text = _coerce_html_body(context)
    if context_text:
        parts.append(context_text)
    action_text = _coerce_html_body(action)
    if action_text:
        parts.append(f"<blockquote>{action_text}</blockquote>")
    detail_text = render_expandable_blockquote(details)
    if detail_text:
        parts.append(detail_text)
    return "\n\n".join(parts)


def render_data_brief(
    title: str,
    *,
    metadata: object | None = None,
    items: object | None = None,
    empty: object | None = None,
    footer: object | None = None,
) -> str:
    """Render a compact, scan-friendly paginated/search result view.

    Metadata is shown as the project's standard quoted field rows, while
    ``items`` remains ordinary text for readable lists on narrow Telegram
    clients. Values must be pre-rendered safe Telegram HTML; callers normally
    use ``<code>`` for identifiers, counts, and page positions.
    """
    parts = [f"<b>{html.escape(str(title))}</b>"]
    metadata_lines: list[str] = []
    if isinstance(metadata, Mapping):
        metadata_lines = [
            card_field(str(label), value) for label, value in metadata.items()
        ]
    elif metadata is not None:
        metadata_text = _coerce_html_body(metadata)
        if metadata_text:
            metadata_lines = [metadata_text]
    if metadata_lines:
        parts.append(f"<blockquote>{'\n'.join(metadata_lines)}</blockquote>")

    item_text = _coerce_html_body(items)
    if item_text:
        parts.append(item_text)
    else:
        empty_text = _coerce_html_body(empty)
        if empty_text:
            parts.append(f"<blockquote>{empty_text}</blockquote>")
    footer_text = _coerce_html_body(footer)
    if footer_text:
        parts.append(footer_text)
    return "\n\n".join(parts)


def buttons_to_lines(value: object) -> str:
    """Human-readable/debug representation used by tests and migrations."""
    try:
        buttons = normalize_template_buttons(value)
    except ValueError:
        return ""
    return "\n".join(
        " | ".join(
            [item["text"], item["action"], item["value"], str(item["row"] + 1)]
        ).rstrip(" |")
        for item in buttons
    )


__all__ = [
    "CARD_FIELD_SPACE",
    "MAX_TEMPLATE_BUTTONS",
    "TEMPLATE_BUTTON_ACTIONS",
    "build_template_keyboard",
    "buttons_to_lines",
    "card_field",
    "normalize_template_buttons",
    "render_action_notice",
    "render_data_brief",
    "render_expandable_blockquote",
    "render_markdown_html",
    "render_notice_card",
    "render_plain_template",
    "render_progress_notice",
    "render_summary_notice",
    "send_template_with_fallback",
    "truncate_telegram_text",
]
