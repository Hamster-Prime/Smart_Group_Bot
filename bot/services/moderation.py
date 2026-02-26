from __future__ import annotations

import json
import logging
import re

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from bot.config import ModerationConfig
from bot.db.models import ModerationRule, UserWarning, Violation
from bot.services.llm import LLMService
from bot.utils.prompts import MODERATION_SYSTEM
from bot.utils.security import build_defended_system, clean_text, wrap_untrusted

log = logging.getLogger(__name__)


def _strip_markdown_fence(text: str) -> str:
    payload = (text or "").strip()
    if payload.startswith("```"):
        payload = re.sub(r"^```(?:json)?", "", payload, flags=re.IGNORECASE).strip()
        payload = re.sub(r"```$", "", payload).strip()
    return payload


def _extract_balanced_object(text: str) -> str | None:
    """Extract first balanced JSON object from text, ignoring braces in strings."""
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
    # Support escaped quote content inside JSON strings.
    pattern = rf'"{re.escape(key)}"\s*:\s*"((?:[^"\\]|\\.)*)"'
    m = re.search(pattern, payload, flags=re.IGNORECASE | re.DOTALL)
    if not m:
        return ""
    return _decode_json_fragment(m.group(1)).strip()


def _salvage_moderation_fields(payload: str) -> dict | None:
    data: dict = {}

    bool_match = re.search(
        r'"(?:violated|violation)"\s*:\s*(true|false)\b(?!\s*/)',
        payload,
        flags=re.IGNORECASE,
    )
    if bool_match:
        data["violated"] = bool_match.group(1).lower() == "true"

    rid_match = re.search(
        r'"rule_id"\s*:\s*(null|-?\d+)',
        payload,
        flags=re.IGNORECASE,
    )
    if rid_match:
        rid_raw = rid_match.group(1).lower()
        data["rule_id"] = None if rid_raw == "null" else int(rid_raw)

    reason = _extract_field_by_regex(payload, "reason")
    if reason:
        data["reason"] = reason

    rule = _extract_field_by_regex(payload, "rule")
    if rule:
        data["rule"] = rule

    # Moderation decision must at least contain violated boolean.
    if "violated" not in data:
        return None
    return data


def _parse_moderation_json(raw: str) -> dict | None:
    payload = _strip_markdown_fence(raw)
    candidate = _extract_balanced_object(payload)
    if candidate:
        payload = candidate
    elif "{" in payload:
        # LLM can return truncated object; keep from first "{" for regex salvage.
        payload = payload[payload.find("{") :]

    try:
        data = json.loads(payload)
    except json.JSONDecodeError:
        return _salvage_moderation_fields(payload)

    if not isinstance(data, dict):
        return None
    return data


class ModerationService:
    def __init__(self, config: ModerationConfig, llm: LLMService) -> None:
        self.config = config
        self.llm = llm

    async def check_rules(
        self, session: AsyncSession, group_id: int, text: str
    ) -> tuple[bool, str, ModerationRule | None]:
        """使用 LLM 基于群规则判定，返回(是否违规, 原因, 命中规则)。"""
        stmt = select(ModerationRule).where(
            ModerationRule.group_id == group_id,
            ModerationRule.enabled == True,
        ).order_by(ModerationRule.id)
        result = await session.execute(stmt)
        rules = list(result.scalars().all())

        if not rules:
            log.info("审核通过 (无启用规则)")
            return False, "", None

        rules_payload = [
            {
                "id": r.id,
                "rule_type": r.rule_type,
                "rule": r.pattern,
                "action": r.action,
            }
            for r in rules
        ]
        rules_json = json.dumps(rules_payload, ensure_ascii=False, indent=2)

        system_prompt = build_defended_system(MODERATION_SYSTEM.format(rules_json=rules_json))
        user_input = wrap_untrusted("待审核消息", clean_text(text, max_len=1200), max_len=1200)
        llm_raw = await self.llm.moderation(system_prompt, user_input)
        data = _parse_moderation_json(llm_raw)

        if not data:
            log.warning("审核模型输出不可解析，按不违规处理: %s", (llm_raw or "")[:200])
            return False, "", None

        violated = bool(data.get("violated", data.get("violation", False)))
        reason = clean_text(str(data.get("reason", "")).strip(), max_len=120)

        hit_rule: ModerationRule | None = None
        rid = data.get("rule_id")
        if rid is not None:
            try:
                rid_int = int(rid)
                hit_rule = next((r for r in rules if r.id == rid_int), None)
            except (TypeError, ValueError):
                hit_rule = None

        if violated and not hit_rule:
            rule_text = str(data.get("rule", "")).strip()
            if rule_text:
                hit_rule = next((r for r in rules if (r.pattern or "").strip() == rule_text), None)

        if violated:
            if not reason:
                reason = "命中群规（AI判定）"
            log.info(
                "审核命中: group=%s rule_id=%s reason=%s",
                group_id,
                hit_rule.id if hit_rule else None,
                reason,
            )
            return True, reason, hit_rule

        log.info("审核通过 (检查了 %d 条规则, AI判定)", len(rules))
        return False, "", None

    async def record_violation(
        self,
        session: AsyncSession,
        group_id: int,
        user_id: int,
        text: str,
        action: str,
        rule: ModerationRule | None = None,
    ) -> Violation:
        v = Violation(
            group_id=group_id,
            user_id=user_id,
            rule_id=rule.id if rule else None,
            message_text=text[:500],
            action_taken=action,
        )
        session.add(v)
        await session.flush()
        return v

    async def add_warning(
        self, session: AsyncSession, group_id: int, user_id: int
    ) -> tuple[int, bool]:
        """增加警告次数，返回(当前次数, 是否应封禁)。"""
        stmt = select(UserWarning).where(
            UserWarning.group_id == group_id,
            UserWarning.user_id == user_id,
        )
        result = await session.execute(stmt)
        warn = result.scalar_one_or_none()

        if warn is None:
            warn = UserWarning(group_id=group_id, user_id=user_id, count=1)
            session.add(warn)
        else:
            warn.count += 1

        should_ban = warn.count >= self.config.warn_threshold
        if should_ban:
            warn.is_banned = True

        await session.flush()
        return warn.count, should_ban
