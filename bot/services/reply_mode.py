from __future__ import annotations

import json
import logging
import re
from typing import TYPE_CHECKING

from bot.utils.prompts import get_prompt
from bot.utils.runtime_context import build_current_time_context
from bot.utils.security import build_defended_system, clean_text, wrap_untrusted

if TYPE_CHECKING:
    from bot.services.llm import LLMService

log = logging.getLogger(__name__)
_VALID_REPLY_MODES = {"reply", "message"}
_JSON_OBJECT_RE = re.compile(r"\{[\s\S]*\}")
_JSON_ARRAY_RE = re.compile(r"\[[\s\S]*\]")


def _fallback_mode(*, is_mentioned: bool, is_reply_to_bot: bool, is_reply_to_other: bool) -> str:
    if is_reply_to_bot:
        return "reply"
    if is_mentioned and not is_reply_to_other:
        return "reply"
    return "message"


def _extract_mode_from_line(raw: str) -> str:
    for line in (raw or "").splitlines():
        normalized = line.strip().lower().strip("`*_#-:> ")
        if not normalized:
            continue
        if normalized in _VALID_REPLY_MODES:
            return normalized
        match = re.match(r"^(reply|message)\b", normalized)
        if match:
            return match.group(1)
    return ""


def _load_json_fragment(raw: str) -> object | None:
    payload = (raw or "").strip()
    if payload.startswith("```"):
        payload = re.sub(r"^```(?:json|javascript|js|text|txt)?", "", payload, flags=re.IGNORECASE).strip()
        payload = re.sub(r"```$", "", payload).strip()

    candidates: list[str] = []
    if payload.startswith("{") and payload.endswith("}"):
        candidates.append(payload)
    elif payload.startswith("[") and payload.endswith("]"):
        candidates.append(payload)
    else:
        object_match = _JSON_OBJECT_RE.search(payload)
        if object_match:
            candidates.append(object_match.group(0))
        array_match = _JSON_ARRAY_RE.search(payload)
        if array_match:
            candidates.append(array_match.group(0))

    for candidate in candidates:
        try:
            return json.loads(candidate)
        except json.JSONDecodeError:
            continue
    return None


def _extract_modes_from_payload(raw: str, *, expected_count: int) -> list[str]:
    if expected_count <= 0:
        return []

    data = _load_json_fragment(raw)
    candidates: list[object] = []
    if isinstance(data, dict):
        for key in ("modes", "reply_modes", "delivery_modes"):
            value = data.get(key)
            if isinstance(value, list):
                candidates.append(value)
                break
    elif isinstance(data, list):
        candidates.append(data)

    for candidate in candidates:
        modes = [str(item or "").strip().lower() for item in candidate]
        if len(modes) == expected_count and all(mode in _VALID_REPLY_MODES for mode in modes):
            return modes

    line_modes: list[str] = []
    for line in (raw or "").splitlines():
        mode = _extract_mode_from_line(line)
        if mode:
            line_modes.append(mode)
    if len(line_modes) == expected_count:
        return line_modes

    token_modes = re.findall(r"\b(reply|message)\b", (raw or "").lower())
    if len(token_modes) == expected_count:
        return token_modes

    return []


class ReplyModeService:
    def __init__(self, llm: LLMService) -> None:
        self.llm = llm

    def _build_common_context(
        self,
        *,
        user_text: str,
        msg_type: str,
        is_mentioned: bool,
        is_reply_to_bot: bool,
        is_reply_to_other: bool,
        merged_count: int,
        merged_context: str,
    ) -> str:
        mention_tag = "yes" if is_mentioned else "no"
        reply_bot_tag = "yes" if is_reply_to_bot else "no"
        reply_other_tag = "yes" if is_reply_to_other else "no"
        merged_tag = "yes" if merged_count > 1 else "no"

        context = (
            f"{build_current_time_context()}\n"
            f"[IS_MERGED_MESSAGE]\n{merged_tag}\n"
            f"[MERGED_MESSAGE_COUNT]\n{max(1, int(merged_count or 1))}\n"
            f"[IS_MENTIONED]\n{mention_tag}\n"
            f"[IS_REPLY_TO_BOT]\n{reply_bot_tag}\n"
            f"[IS_REPLY_TO_OTHER]\n{reply_other_tag}\n"
            f"[MESSAGE_TYPE]\n{clean_text(msg_type, max_len=40)}\n"
        )
        if merged_count > 1 and merged_context.strip():
            context += (
                "[MERGED_MESSAGE_CONTEXT]\n"
                f"{wrap_untrusted('merged_message_context', clean_text(merged_context, max_len=1600), max_len=1600)}\n"
            )
        context += (
            f"[CURRENT_MESSAGE]\n{wrap_untrusted('current_message', clean_text(user_text, max_len=1200), max_len=1200)}\n"
        )
        return context

    async def decide(
        self,
        *,
        user_text: str,
        assistant_reply: str,
        msg_type: str,
        is_mentioned: bool,
        is_reply_to_bot: bool,
        is_reply_to_other: bool,
        merged_count: int = 1,
        merged_context: str = "",
    ) -> str:
        context = self._build_common_context(
            user_text=user_text,
            msg_type=msg_type,
            is_mentioned=is_mentioned,
            is_reply_to_bot=is_reply_to_bot,
            is_reply_to_other=is_reply_to_other,
            merged_count=merged_count,
            merged_context=merged_context,
        )
        context += (
            f"[ASSISTANT_DRAFT_REPLY]\n"
            f"{wrap_untrusted('assistant_draft_reply', clean_text(assistant_reply, max_len=1200), max_len=1200)}"
        )

        raw = await self.llm.decision(
            build_defended_system(get_prompt("reply_mode")), context
        )
        result = _extract_mode_from_line(raw or "")
        if result in _VALID_REPLY_MODES:
            log.info("reply mode decided=%s merged=%s", result, merged_count > 1)
            return result

        fallback = _fallback_mode(
            is_mentioned=is_mentioned,
            is_reply_to_bot=is_reply_to_bot,
            is_reply_to_other=is_reply_to_other,
        )
        log.info("reply mode fallback=%s raw=%s", fallback, result or "(empty)")
        return fallback

    async def decide_many(
        self,
        *,
        user_text: str,
        assistant_replies: list[str],
        msg_type: str,
        is_mentioned: bool,
        is_reply_to_bot: bool,
        is_reply_to_other: bool,
        merged_count: int = 1,
        merged_context: str = "",
    ) -> list[str]:
        replies: list[str] = []
        for item in assistant_replies:
            normalized = clean_text(item, max_len=1200)
            if normalized:
                replies.append(normalized)
        if not replies:
            return []
        if len(replies) == 1:
            return [
                await self.decide(
                    user_text=user_text,
                    assistant_reply=replies[0],
                    msg_type=msg_type,
                    is_mentioned=is_mentioned,
                    is_reply_to_bot=is_reply_to_bot,
                    is_reply_to_other=is_reply_to_other,
                    merged_count=merged_count,
                    merged_context=merged_context,
                )
            ]

        context = self._build_common_context(
            user_text=user_text,
            msg_type=msg_type,
            is_mentioned=is_mentioned,
            is_reply_to_bot=is_reply_to_bot,
            is_reply_to_other=is_reply_to_other,
            merged_count=merged_count,
            merged_context=merged_context,
        )
        draft_lines = ["[ASSISTANT_DRAFT_REPLIES]"]
        for idx, reply in enumerate(replies, start=1):
            draft_lines.append(f"[REPLY_{idx}]")
            draft_lines.append(wrap_untrusted(f"assistant_draft_reply_{idx}", reply, max_len=1200))
        context += "\n".join(draft_lines)

        raw = await self.llm.decision(
            build_defended_system(get_prompt("reply_mode")), context
        )
        parsed = _extract_modes_from_payload(raw or "", expected_count=len(replies))
        if parsed:
            log.info(
                "reply mode decided_batch count=%d merged=%s modes=%s",
                len(parsed),
                merged_count > 1,
                ",".join(parsed),
            )
            return parsed

        fallback = _fallback_mode(
            is_mentioned=is_mentioned,
            is_reply_to_bot=is_reply_to_bot,
            is_reply_to_other=is_reply_to_other,
        )
        modes = [fallback] * len(replies)
        log.info(
            "reply mode batch fallback=%s count=%d raw=%s",
            fallback,
            len(replies),
            (raw or "").strip()[:120] or "(empty)",
        )
        return modes
