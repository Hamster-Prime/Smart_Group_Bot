from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

from bot.utils.security import clean_multiline_text

REPLY_OUTPUT_PROTOCOL = (
    "[REPLY_OUTPUT_PROTOCOL]\n"
    "The runtime explicitly supports 0, 1, or many outgoing messages in one turn.\n"
    "You are allowed to choose the number of outgoing messages yourself.\n"
    "If one natural message is enough, plain text is fine.\n"
    "In plain text output, every blank-line-separated block is treated as a separate outgoing message.\n"
    "Inside one visible message, normal line breaks are allowed.\n"
    "If you want one visible message, do NOT use blank lines. Use single newlines only.\n"
    "If you want multiple plain-text messages with the default behavior, separate them with blank lines.\n"
    "Do NOT use blank lines inside one message just for spacing or to imitate multiple chat bubbles.\n"
    "If you need per-message control for delivery_mode or reply_to, you MUST output JSON.\n"
    "If you want different delivery modes or different reply targets for different messages, you MUST use message objects.\n"
    "messages may be plain strings or message objects.\n"
    "Use plain strings only when every outgoing message can use the default behavior.\n"
    "Use message objects when you need per-message control.\n"
    "Plain-text example for two default-behavior messages:\n"
    "first message\n"
    "\n"
    "second message\n"
    "Example with JSON strings:\n"
    '{"messages":["first message","second message"]}\n'
    "Example with message objects:\n"
    '{"messages":[{"text":"first message","delivery_mode":"message"},{"text":"second message","delivery_mode":"reply","reply_to":"latest_input"}]}\n'
    "Example for explicit silence:\n"
    '{"should_reply":false,"reason":"not_addressed_to_bot"}\n'
    "When to keep one message (DEFAULT - most of the time):\n"
    "- one response is enough, even if it has multiple lines\n"
    "- you are replying to a single topic\n"
    "- the response is a paragraph, an explanation, or a complete thought\n"
    "When to split into multiple messages (RARE - only when truly needed):\n"
    "- you need to address two completely different people or topics\n"
    "- different outgoing messages should use different delivery_mode or different reply_to targets\n"
    "- there are 2 genuinely independent beats that feel unnatural joined together\n"
    "Do NOT split just because a response is long or has line breaks.\n"
    "Do NOT split just to create a 'chat bubble' effect - one message is almost always better.\n"
    "When to use delivery_mode=message:\n"
    "- user explicitly asks for standalone messages\n"
    '- user says do not use reply format / direct-send / separate-send\n'
    "When to use delivery_mode=reply:\n"
    "- the message clearly answers or continues a specific message thread\n"
    "- you want to address a specific person or anchor message\n"
    "Rules:\n"
    "1. JSON must be the entire answer when you use it.\n"
    "2. messages are sent in order.\n"
    "3. Keep each message concise and natural.\n"
    "4. If the bot is not the real target, prefer should_reply=false.\n"
    "5. delivery_mode supports auto / reply / message.\n"
    '6. If delivery_mode is auto, the system will decide reply vs message with the normal logic.\n'
    '7. reply_to is optional. Use "auto" if the system should choose the default reply target.\n'
    "8. If reply_to is needed, use an alias from [REPLY_TARGET_CANDIDATES].\n"
    "9. Do not describe the protocol in your visible message. Either send plain text or output the JSON directly.\n"
)

REPLY_OUTPUT_AWARENESS = (
    "[REPLY_OUTPUT_AWARENESS]\n"
    "You do have the ability to choose how many outgoing messages to send in this turn.\n"
    "Do not assume you are limited to a single message.\n"
    "Do not assume every outgoing message must use the same delivery mode or the same reply target.\n"
    "You may choose:\n"
    "- no message\n"
    "- one message (this is the default and most common choice)\n"
    "- multiple messages (only when genuinely needed, e.g. addressing different people)\n"
    "You may also choose per outgoing message:\n"
    "- delivery_mode=message\n"
    "- delivery_mode=reply\n"
    "- reply_to=auto or a concrete alias from [REPLY_TARGET_CANDIDATES]\n"
    "Practical decision guide:\n"
    "1. Default to ONE message. Most turns only need one message, even if the content has multiple sentences or line breaks.\n"
    "2. If you stay in one message, keep the layout compact: use normal line breaks when needed, but do not leave blank lines.\n"
    "3. In plain text, a blank line is a message separator. Each blank-line-separated paragraph becomes a separate outgoing message.\n"
    "4. If you want one visible message, use single newlines only and do not leave blank lines.\n"
    "5. Do NOT use blank lines to fake spacing, multiple bubbles, or separate beats inside one message.\n"
    "6. Only split into multiple messages when they address genuinely different people, topics, or need different delivery modes.\n"
    "7. If the user explicitly asks for direct standalone messages, prefer delivery_mode=message for those items.\n"
    "8. If different outgoing messages should answer different people or different anchors, use message objects and set reply_to per item.\n"
    "9. If you need per-message control, do not use plain string arrays. Use message objects.\n"
    "10. After tool use, these same output rules still apply to your final answer.\n"
)

_REPLY_JSON_KEYS = {
    "action",
    "message",
    "messages",
    "reason",
    "reply",
    "response_type",
    "should_reply",
    "silent",
    "skip_reply",
    "text",
}
_NO_REPLY_ACTIONS = {"silent", "skip", "no_reply", "noreply", "no-response", "no_response"}
_VALID_DELIVERY_MODES = {"auto", "reply", "message"}
_STRUCTURED_PLAIN_TEXT_RE = re.compile(
    r"(?m)^\s*(?:[-*+]\s+|\d+\.\s+|[（(]?\d+[)）]\s+|#{1,6}\s+|>|```|[一二三四五六七八九十]+[、.])"
)
_BLANK_LINE_SPLIT_RE = re.compile(r"\n\s*\n+")
_PLAIN_TEXT_MULTI_MESSAGE_MAX_PART_CHARS = 90
_PLAIN_TEXT_MULTI_MESSAGE_MAX_TOTAL_CHARS = 240


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


def _strip_code_fence(text: str) -> str:
    payload = (text or "").strip()
    if payload.startswith("```"):
        payload = re.sub(r"^```(?:json|javascript|js|text|txt)?", "", payload, flags=re.IGNORECASE).strip()
        payload = re.sub(r"```$", "", payload).strip()
    return payload


def _extract_json_object(text: str) -> dict[str, Any] | None:
    payload = _strip_code_fence(text)
    candidates: list[str] = []
    if payload.startswith("{") and payload.endswith("}"):
        candidates.append(payload)
    else:
        match = re.search(r"\{[\s\S]*\}", payload)
        if match:
            candidates.append(match.group(0))

    for candidate in candidates:
        try:
            data = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(data, dict):
            return data
    return None


def _is_reply_json(data: dict[str, Any]) -> bool:
    return any(key in data for key in _REPLY_JSON_KEYS)


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


def _normalize_message_text(value: Any, *, max_len: int) -> str:
    text = clean_multiline_text(str(value or ""), max_len=max_len).strip()
    return text


def _normalize_delivery_mode(value: Any) -> str:
    normalized = str(value or "").strip().lower() or "auto"
    if normalized in _VALID_DELIVERY_MODES:
        return normalized
    return "auto"


def _normalize_reply_to(value: Any) -> str:
    normalized = clean_multiline_text(str(value or ""), max_len=80).strip()
    return normalized or "auto"


def _looks_like_structured_plain_text(text: str) -> bool:
    payload = (text or "").strip()
    if not payload:
        return False
    return bool(_STRUCTURED_PLAIN_TEXT_RE.search(payload))


def _extract_plain_text_message_specs(
    value: str,
    *,
    max_messages: int,
    max_len: int,
) -> list[ReplyMessageSpec]:
    if max_messages <= 1:
        return []

    normalized = clean_multiline_text(
        value,
        max_len=max(max_len, 1) * max(max_messages, 1),
    ).strip()
    if not normalized or "\n\n" not in normalized:
        return []

    parts = [
        _normalize_message_text(part, max_len=max_len)
        for part in _BLANK_LINE_SPLIT_RE.split(normalized)
    ]
    parts = [part for part in parts if part]
    if len(parts) <= 1:
        return []

    if len(parts) > max_messages:
        head = parts[: max_messages - 1]
        tail = "\n\n".join(parts[max_messages - 1 :]).strip()
        parts = head + ([tail] if tail else [])

    return [ReplyMessageSpec(text=part) for part in parts]


def _extract_message_specs(value: Any, *, max_messages: int, max_len: int) -> list[ReplyMessageSpec]:
    if isinstance(value, str):
        text = _normalize_message_text(value, max_len=max_len)
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

        normalized = _normalize_message_text(text, max_len=max_len)
        if normalized:
            out.append(
                ReplyMessageSpec(
                    text=normalized,
                    delivery_mode=delivery_mode,
                    reply_to=reply_to,
                )
            )
    return out


def parse_reply_output(
    raw: str,
    *,
    max_messages: int = 8,
    max_message_chars: int = 1200,
) -> ParsedReplyOutput:
    text = (raw or "").strip()
    if not text:
        return ParsedReplyOutput()

    data = _extract_json_object(text)
    if not data or not _is_reply_json(data):
        plain_text_specs = _extract_plain_text_message_specs(
            text,
            max_messages=max_messages,
            max_len=max_message_chars,
        )
        if plain_text_specs:
            return ParsedReplyOutput(message_specs=plain_text_specs)
        normalized = _normalize_message_text(text, max_len=max_message_chars)
        return ParsedReplyOutput(
            message_specs=[ReplyMessageSpec(text=normalized)] if normalized else []
        )

    should_reply = _coerce_bool(data.get("should_reply"))
    silent = _coerce_bool(data.get("silent"))
    skip_reply = _coerce_bool(data.get("skip_reply"))
    action = str(data.get("action") or data.get("response_type") or "").strip().lower()
    reason = _normalize_message_text(data.get("reason", ""), max_len=160)

    if should_reply is False or silent is True or skip_reply is True or action in _NO_REPLY_ACTIONS:
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
