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
    "MAX_TEMPLATE_BUTTONS",
    "TEMPLATE_BUTTON_ACTIONS",
    "build_template_keyboard",
    "buttons_to_lines",
    "normalize_template_buttons",
    "render_markdown_html",
    "render_plain_template",
    "send_template_with_fallback",
]
