from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

from bot.utils.security import clean_multiline_text

REPLY_OUTPUT_SCHEMA = "smart-group-bot.reply.v2"
REPLY_SPLIT_MARKER = "[[SPLIT]]"

REPLY_OUTPUT_PROTOCOL = (
    "[REPLY_OUTPUT_PROTOCOL]\n"
    "The runtime explicitly supports 0, 1, or many outgoing messages in one turn.\n"
    "For the normal case, output one natural Markdown message directly.\n"
    "Blank lines, paragraphs, lists, blockquotes, and fenced code blocks all remain inside that one message.\n"
    "Never use blank lines as message separators.\n"
    f"To split plain text into separate outgoing messages, put {REPLY_SPLIT_MARKER} alone on its own line between them.\n"
    f"A line containing only {REPLY_SPLIT_MARKER} is removed and never shown to users.\n"
    f"{REPLY_SPLIT_MARKER} written inline inside a sentence or inside a fenced code block is ordinary visible text.\n"
    "Plain-text example for two messages:\n"
    "first message\n"
    f"{REPLY_SPLIT_MARKER}\n"
    "second message\n"
    "To send zero messages, or to control delivery_mode/reply_to per message, output the strict JSON protocol.\n"
    f'The protocol JSON MUST contain exactly this schema identifier: "schema":"{REPLY_OUTPUT_SCHEMA}".\n'
    "The protocol JSON must be the entire answer and must not be wrapped in a Markdown code fence.\n"
    "A JSON object without the exact schema identifier is ordinary visible content, not a command.\n"
    "messages may contain plain strings or message objects.\n"
    "Use message objects only when per-message delivery control is needed.\n"
    "Example with JSON strings:\n"
    f'{{"schema":"{REPLY_OUTPUT_SCHEMA}","messages":["first message","second message"]}}\n'
    "Example with message objects:\n"
    f'{{"schema":"{REPLY_OUTPUT_SCHEMA}","messages":[{{"text":"first message","delivery_mode":"message"}},{{"text":"second message","delivery_mode":"reply","reply_to":"latest_input"}}]}}\n'
    "Example for explicit silence:\n"
    f'{{"schema":"{REPLY_OUTPUT_SCHEMA}","should_reply":false,"reason":"not_addressed_to_bot"}}\n'
    "When to keep one message (DEFAULT - most of the time):\n"
    "- one response is enough, even if it has multiple paragraphs or code blocks\n"
    "- you are replying to a single topic\n"
    "- the response is an explanation or one complete thought\n"
    "When to split into multiple messages (RARE - only when truly needed):\n"
    "- you need to address two completely different people or topics\n"
    "- different outgoing messages need different delivery modes or reply targets\n"
    f"- for a plain-text split use {REPLY_SPLIT_MARKER}; for per-message delivery control use JSON\n"
    "Do NOT split just because a response is long or has blank lines.\n"
    "Do NOT split just to create a chat-bubble effect.\n"
    "When to use delivery_mode=message:\n"
    "- user explicitly asks for standalone messages\n"
    "- user says do not use reply format / direct-send / separate-send\n"
    "When to use delivery_mode=reply:\n"
    "- the message clearly answers or continues a specific message thread\n"
    "- you want to address a specific person or anchor message\n"
    "Rules:\n"
    "1. Protocol JSON must be the entire answer when you use it.\n"
    "2. messages are sent in order.\n"
    "3. Keep each message concise and natural.\n"
    "4. If the bot is not the real target, prefer should_reply=false.\n"
    "5. delivery_mode supports auto / reply / message.\n"
    "6. If delivery_mode is auto, the system chooses reply vs message with the normal logic.\n"
    "7. reply_to is optional. Use auto if the system should choose the default target.\n"
    "8. If reply_to is needed, use an alias from [REPLY_TARGET_CANDIDATES].\n"
    "9. Either send Markdown directly or output the strict JSON object; do not describe the protocol.\n"
)

REPLY_OUTPUT_AWARENESS = (
    "[REPLY_OUTPUT_AWARENESS]\n"
    "You can choose zero, one, or multiple outgoing messages for this turn.\n"
    "Default to ONE message, even when it contains multiple paragraphs or code blocks.\n"
    "Use normal Markdown layout inside that message, including blank lines when useful.\n"
    "Blank lines never create additional outgoing messages.\n"
    f"In plain text, only a line containing just {REPLY_SPLIT_MARKER} starts a new outgoing message.\n"
    f"The {REPLY_SPLIT_MARKER} line is stripped before sending, so users never see it.\n"
    "The strict schema-tagged JSON protocol is for zero messages or per-message delivery control.\n"
    "Only split when messages address genuinely different people/topics or need different delivery modes.\n"
    "Use message objects for delivery_mode=message/reply or a concrete reply_to alias.\n"
    "Never wrap protocol JSON in a code fence; fenced JSON is visible Markdown content.\n"
    "After tool use, these same output rules still apply to the final answer.\n"
)

_REPLY_JSON_KEYS = {
    "action",
    "disposition",
    "message",
    "messages",
    "reply",
    "response_type",
    "should_reply",
    "silent",
    "skip_reply",
    "text",
}
_NO_REPLY_ACTIONS = {"silent", "skip", "no_reply", "noreply", "no-response", "no_response"}
_VALID_DELIVERY_MODES = {"auto", "reply", "message"}
_CONTROL_CHAR_RE = re.compile(r"[\x00-\x08\x0B-\x1F\x7F]")
# The marker only counts when it owns the whole line, so inline mentions of it
# inside prose stay visible content.
_SPLIT_MARKER_LINE_RE = re.compile(r"^\s*\[\[SPLIT\]\]\s*$", re.IGNORECASE)
_CODE_FENCE_RE = re.compile(r"^\s*(?:```|~~~)")


@dataclass(slots=True)
class ReplyMessageSpec:
    text: str
    delivery_mode: str = "auto"
    reply_to: str = "auto"


@dataclass(slots=True)
class ParsedReplyOutput:
    message_specs: list[ReplyMessageSpec] = field(default_factory=list)
    explicit_no_reply: bool = False
    reason: str = ""
    used_json: bool = False

    @property
    def messages(self) -> list[str]:
        return [item.text for item in self.message_specs if item.text]

    @property
    def joined_text(self) -> str:
        return "\n\n".join(self.messages).strip()


def _extract_json_object(text: str) -> dict[str, Any] | None:
    """Load an object only when the complete output is JSON.

    In particular, do not search a Markdown answer for an embedded object and do
    not unwrap fenced examples. Both behaviours can turn visible code into a bot
    control command.
    """

    payload = (text or "").strip()
    if not payload.startswith("{") or not payload.endswith("}"):
        return None
    try:
        data = json.loads(payload)
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


def _is_reply_json(data: dict[str, Any]) -> bool:
    return data.get("schema") == REPLY_OUTPUT_SCHEMA and any(
        key in data for key in _REPLY_JSON_KEYS
    )


def _coerce_bool(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"true", "yes", "1"}:
            return True
        if normalized in {"false", "no", "0"}:
            return False
    return None


def _normalize_message_text(
    value: Any,
    *,
    max_len: int | None,
    drop_split_markers: bool = False,
) -> str:
    """Normalize transport-only characters without reformatting Markdown."""

    text = str(value or "").replace("\r\n", "\n").replace("\r", "\n")
    text = _CONTROL_CHAR_RE.sub(" ", text)
    if drop_split_markers:
        # JSON already carries one text per message, so a marker line here is
        # stray output rather than a split request.
        text = "\n".join(
            line for line in text.split("\n") if not _SPLIT_MARKER_LINE_RE.match(line)
        )
    text = text.strip()
    if max_len is not None and max_len > 0 and len(text) > max_len:
        return text[:max_len].rstrip() + " ..."
    return text


def _normalize_delivery_mode(value: Any) -> str:
    normalized = str(value or "").strip().lower() or "auto"
    if normalized in _VALID_DELIVERY_MODES:
        return normalized
    return "auto"


def _normalize_reply_to(value: Any) -> str:
    normalized = clean_multiline_text(str(value or ""), max_len=80).strip()
    return normalized or "auto"


def _extract_message_specs(
    value: Any,
    *,
    max_messages: int,
    max_len: int | None,
) -> list[ReplyMessageSpec]:
    if isinstance(value, str):
        text = _normalize_message_text(value, max_len=max_len, drop_split_markers=True)
        return [ReplyMessageSpec(text=text)] if text else []

    if not isinstance(value, list):
        return []

    out: list[ReplyMessageSpec] = []
    for item in value[:max_messages]:
        text = ""
        delivery_mode = "auto"
        reply_to = "auto"
        if isinstance(item, str):
            text = item
        elif isinstance(item, dict):
            for key in ("text", "content", "message", "reply"):
                candidate = item.get(key)
                if isinstance(candidate, str) and candidate.strip():
                    text = candidate
                    break
            delivery_mode = _normalize_delivery_mode(
                item.get("delivery_mode", item.get("mode", "auto"))
            )
            reply_to = _normalize_reply_to(
                item.get("reply_to", item.get("reply_target", item.get("target", "auto")))
            )
        else:
            text = str(item or "")

        normalized = _normalize_message_text(text, max_len=max_len, drop_split_markers=True)
        if normalized:
            out.append(
                ReplyMessageSpec(
                    text=normalized,
                    delivery_mode=delivery_mode,
                    reply_to=reply_to,
                )
            )
    return out


def _split_plain_text_messages(
    text: str,
    *,
    max_messages: int,
    max_len: int | None,
) -> list[str]:
    """Split plain text on standalone [[SPLIT]] marker lines.

    Markers inside fenced code blocks are left alone so a snippet that shows the
    marker stays intact. Without any marker this returns a single part, which is
    the safe default when the model does not ask for a split.
    """

    parts: list[str] = []
    buffer: list[str] = []
    in_fence = False
    for line in (text or "").replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        if _CODE_FENCE_RE.match(line):
            in_fence = not in_fence
        elif not in_fence and _SPLIT_MARKER_LINE_RE.match(line):
            parts.append("\n".join(buffer))
            buffer = []
            continue
        buffer.append(line)
    parts.append("\n".join(buffer))

    normalized = [_normalize_message_text(part, max_len=max_len) for part in parts]
    normalized = [part for part in normalized if part]
    if len(normalized) > max_messages > 0:
        head = normalized[: max_messages - 1]
        tail = _normalize_message_text(
            "\n\n".join(normalized[max_messages - 1 :]),
            max_len=max_len,
        )
        normalized = head + ([tail] if tail else [])
    return normalized


def parse_reply_output(
    raw: str,
    *,
    max_messages: int = 8,
    max_message_chars: int | None = None,
) -> ParsedReplyOutput:
    text = (raw or "").strip()
    if not text:
        return ParsedReplyOutput()

    data = _extract_json_object(text)
    if not data or not _is_reply_json(data):
        parts = _split_plain_text_messages(
            text,
            max_messages=max_messages,
            max_len=max_message_chars,
        )
        return ParsedReplyOutput(
            message_specs=[ReplyMessageSpec(text=part) for part in parts]
        )

    should_reply = _coerce_bool(data.get("should_reply"))
    silent = _coerce_bool(data.get("silent"))
    skip_reply = _coerce_bool(data.get("skip_reply"))
    action = str(data.get("action") or data.get("response_type") or "").strip().lower()
    disposition = str(data.get("disposition") or "").strip().lower()
    reason = _normalize_message_text(data.get("reason", ""), max_len=160)

    if (
        disposition in {"silent", "skip", "no_reply"}
        or should_reply is False
        or silent is True
        or skip_reply is True
        or action in _NO_REPLY_ACTIONS
    ):
        return ParsedReplyOutput(
            explicit_no_reply=True,
            reason=reason or action or "model_declined_reply",
            used_json=True,
        )

    message_specs = _extract_message_specs(
        data.get("messages"),
        max_messages=max_messages,
        max_len=max_message_chars,
    )
    if not message_specs:
        for key in ("message", "reply", "text"):
            candidate = data.get(key)
            if isinstance(candidate, dict):
                candidate = [candidate]
            message_specs = _extract_message_specs(
                candidate,
                max_messages=1,
                max_len=max_message_chars,
            )
            if message_specs:
                break

    if message_specs:
        return ParsedReplyOutput(message_specs=message_specs[:max_messages], used_json=True)

    if should_reply is False:
        return ParsedReplyOutput(
            explicit_no_reply=True,
            reason=reason or "model_declined_reply",
            used_json=True,
        )

    return ParsedReplyOutput(used_json=True)
