from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

from bot.utils.security import clean_multiline_text

REPLY_OUTPUT_PROTOCOL = (
    "[REPLY_OUTPUT_PROTOCOL]\n"
    "You may answer in plain text or JSON.\n"
    "If one natural message is enough, plain text is fine.\n"
    "If you want to send multiple messages, output only JSON.\n"
    "messages can be plain strings or message objects.\n"
    "Example with strings:\n"
    '{"messages":["first message","second message"]}\n'
    "Example with message objects:\n"
    '{"messages":[{"text":"first message","delivery_mode":"message"},{"text":"second message","delivery_mode":"reply","reply_to":"latest_input"}]}\n'
    "If you intentionally decide not to reply, output only JSON like:\n"
    '{"should_reply":false,"reason":"not_addressed_to_bot"}\n'
    "Rules:\n"
    "1. JSON must be the entire answer when you use it.\n"
    "2. messages are sent in order.\n"
    "3. Keep each message concise and natural.\n"
    "4. If the bot is not the real target, prefer should_reply=false.\n"
    "5. delivery_mode supports auto / reply / message.\n"
    '6. If delivery_mode is auto, the system will decide reply vs message with the normal logic.\n'
    '7. reply_to is optional. Use "auto" if the system should choose the default reply target.\n'
    "8. If reply_to is needed, use an alias from [REPLY_TARGET_CANDIDATES].\n"
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
