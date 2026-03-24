from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession

from bot.services.llm import LLMService
from bot.services.sticker_library import sticker_library
from bot.utils.prompts import STICKER_DECISION_SYSTEM
from bot.utils.security import build_defended_system, clean_text, wrap_untrusted

log = logging.getLogger(__name__)
_REPLIES_PER_STICKER = 5
_RATIO_BASELINE: dict[int, tuple[int, int]] = {}


def _strip_markdown_fence(text: str) -> str:
    payload = (text or "").strip()
    if payload.startswith("```"):
        payload = re.sub(r"^```(?:json)?", "", payload, flags=re.IGNORECASE).strip()
        payload = re.sub(r"```$", "", payload).strip()
    return payload


def _extract_balanced_object(text: str) -> str | None:
    start = text.find("{")
    if start < 0:
        return None

    depth = 0
    in_string = False
    escaped = False
    for idx in range(start, len(text)):
        ch = text[idx]
        if escaped:
            escaped = False
            continue
        if ch == "\\":
            escaped = True
            continue
        if ch == '"':
            in_string = not in_string
            continue
        if in_string:
            continue
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[start : idx + 1]
    return None


def _decode_json_fragment(value: str) -> str:
    try:
        return json.loads(f'"{value}"')
    except Exception:
        return value


def _extract_field_by_regex(payload: str, key: str) -> str:
    pattern = rf'"{re.escape(key)}"\s*:\s*"((?:[^"\\]|\\.)*)"'
    m = re.search(pattern, payload, flags=re.IGNORECASE | re.DOTALL)
    if not m:
        return ""
    return _decode_json_fragment(m.group(1)).strip()


def _salvage_decision_fields(payload: str) -> dict | None:
    data: dict = {}
    send_match = re.search(r'"send"\s*:\s*(true|false)\b', payload, flags=re.IGNORECASE)
    if send_match:
        data["send"] = send_match.group(1).lower() == "true"

    sticker_file_id = _extract_field_by_regex(payload, "sticker_file_id")
    if sticker_file_id:
        data["sticker_file_id"] = sticker_file_id

    query = _extract_field_by_regex(payload, "query")
    if query:
        data["query"] = query

    reason = _extract_field_by_regex(payload, "reason")
    if reason:
        data["reason"] = reason

    if "send" not in data:
        return None
    return data


def _parse_sticker_decision_json(raw: str) -> dict | None:
    payload = _strip_markdown_fence(raw)
    candidate = _extract_balanced_object(payload)
    if candidate:
        payload = candidate
    elif "{" in payload:
        payload = payload[payload.find("{") :]

    try:
        data = json.loads(payload)
    except json.JSONDecodeError:
        return _salvage_decision_fields(payload)

    if not isinstance(data, dict):
        return None
    return data


def _parse_send_flag(value: object) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        v = value.strip().lower()
        if v in {"true", "1", "yes", "y", "是"}:
            return True
        if v in {"false", "0", "no", "n", "否", ""}:
            return False
    return False


@dataclass(slots=True)
class StickerDecisionResult:
    send: bool = False
    sticker_file_id: str = ""
    query: str = ""
    reason: str = ""
    raw: str = ""


class StickerDecisionService:
    def __init__(self, llm: LLMService) -> None:
        self.llm = llm

    @staticmethod
    def _format_candidates(candidates: list[dict]) -> str:
        if not candidates:
            return "[]"

        normalized: list[dict] = []
        for row in candidates[:30]:
            if not isinstance(row, dict):
                continue
            normalized.append(
                {
                    "file_id": clean_text(str(row.get("file_id", "")), max_len=255),
                    "emoji": clean_text(str(row.get("emoji", "")), max_len=16),
                    "set_name": clean_text(str(row.get("set_name", "")), max_len=80),
                    "description": clean_text(str(row.get("description", "")), max_len=120),
                    "seen_count": int(row.get("seen_count", 0) or 0),
                    "sent_count": int(row.get("sent_count", 0) or 0),
                    "source": clean_text(str(row.get("source", "")), max_len=32),
                }
            )
        return json.dumps(normalized, ensure_ascii=False)

    async def decide(
        self,
        *,
        session: AsyncSession,
        group_id: int,
        action: str,
        msg_type: str,
        is_mentioned: bool,
        is_reply_to_bot: bool,
        user_text: str,
        assistant_reply: str,
        reply_source: str,
        assistant_reply_count: int = 0,
        default_sticker_file_ids: list[str] | None = None,
    ) -> StickerDecisionResult:
        normalized_action = (action or "").strip().lower()
        can_have_reply = normalized_action != "skip" and bool((assistant_reply or "").strip())
        total_replies = max(0, int(assistant_reply_count or 0)) + (1 if can_have_reply else 0)
        total_sent = await sticker_library.total_sent_count(session, group_id)

        base_replies, base_sent = _RATIO_BASELINE.get(group_id, (total_replies, total_sent))
        if group_id not in _RATIO_BASELINE or total_replies < base_replies or total_sent < base_sent:
            base_replies, base_sent = total_replies, total_sent
            _RATIO_BASELINE[group_id] = (base_replies, base_sent)

        delta_replies = max(0, total_replies - base_replies)
        delta_sent = max(0, total_sent - base_sent)
        allowed_delta_sent = delta_replies // _REPLIES_PER_STICKER
        if delta_sent >= allowed_delta_sent:
            log.info(
                "[%s] sticker ratio limited | delta_reply=%d delta_sent=%d allowed=%d baseline=(%d,%d)",
                group_id,
                delta_replies,
                delta_sent,
                allowed_delta_sent,
                base_replies,
                base_sent,
            )
            return StickerDecisionResult(send=False, reason="reply_ratio_limited")

        candidates = await sticker_library.list_candidates(
            session,
            group_id,
            limit=30,
            fallback_pool=default_sticker_file_ids or [],
        )
        candidate_ids = {str(x.get("file_id", "")).strip() for x in candidates if str(x.get("file_id", "")).strip()}

        mention_tag = "yes" if is_mentioned else "no"
        reply_bot_tag = "yes" if is_reply_to_bot else "no"
        context = (
            f"[REPLY_ACTION]\n{clean_text(normalized_action, max_len=16)}\n"
            f"[MESSAGE_TYPE]\n{clean_text(msg_type, max_len=40)}\n"
            f"[IS_MENTIONED]\n{mention_tag}\n"
            f"[IS_REPLY_TO_BOT]\n{reply_bot_tag}\n"
            f"[REPLY_SOURCE]\n{clean_text(reply_source, max_len=32)}\n"
            f"[CURRENT_MESSAGE]\n{wrap_untrusted('current_message', clean_text(user_text, max_len=900), max_len=900)}\n"
            f"[ASSISTANT_DRAFT_REPLY]\n"
            f"{wrap_untrusted('assistant_draft_reply', clean_text(assistant_reply, max_len=900), max_len=900)}\n"
            f"[STICKER_CANDIDATES]\n"
            f"{wrap_untrusted('sticker_candidates', self._format_candidates(candidates), max_len=8000)}"
        )

        raw = await self.llm.generate(build_defended_system(STICKER_DECISION_SYSTEM), context)
        data = _parse_sticker_decision_json(raw)
        if not data:
            log.warning("sticker decision parse failed, fallback no-send")
            return StickerDecisionResult(send=False, reason="parse_failed", raw=raw or "")

        send = _parse_send_flag(data.get("send", False))
        if not send:
            return StickerDecisionResult(
                send=False,
                sticker_file_id="",
                query=clean_text(str(data.get("query", "")), max_len=80),
                reason=clean_text(str(data.get("reason", "")), max_len=60),
                raw=raw or "",
            )

        sticker_file_id = clean_text(str(data.get("sticker_file_id", "")), max_len=255)
        query = clean_text(str(data.get("query", "")), max_len=80)
        reason = clean_text(str(data.get("reason", "")), max_len=60)

        if sticker_file_id and sticker_file_id in candidate_ids:
            return StickerDecisionResult(
                send=True,
                sticker_file_id=sticker_file_id,
                query=query,
                reason=reason,
                raw=raw or "",
            )

        # Fallback: use model-provided query/reason for semantic pick when file_id is missing.
        pick_query = query or reason
        if not pick_query:
            return StickerDecisionResult(send=False, query=query, reason=reason or "missing_choice", raw=raw or "")

        picked = await sticker_library.pick_sticker(
            session,
            group_id,
            query=clean_text(pick_query, max_len=200),
            fallback_pool=default_sticker_file_ids or [],
        )
        if picked.file_id:
            return StickerDecisionResult(
                send=True,
                sticker_file_id=picked.file_id,
                query=query,
                reason=reason or picked.source,
                raw=raw or "",
            )

        return StickerDecisionResult(send=False, query=query, reason=reason or "no_sticker", raw=raw or "")
