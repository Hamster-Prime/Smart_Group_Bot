from __future__ import annotations

import json
import logging
import math
import re
from dataclasses import dataclass

from sqlalchemy import case, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from bot.config import ModerationConfig
from bot.db.models import ModerationExemption, ModerationRule, UserWarning, Violation
from bot.services.llm import LLMService
from bot.utils.prompts import get_prompt
from bot.utils.security import build_defended_system, clean_text, wrap_untrusted

log = logging.getLogger(__name__)


@dataclass(slots=True)
class ModerationVerdict:
    """LLM verdict for one message.

    confidence is only meaningful when violated=True. Missing or invalid
    confidence is treated as inconclusive low confidence, never upgraded into
    a direct punishment.
    """

    violated: bool
    reason: str
    rule: ModerationRule | None
    conclusive: bool
    confidence: float = 0.0


def _parse_confidence(value: object) -> tuple[float, bool]:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0.0, False
    try:
        confidence = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0.0, False
    if not math.isfinite(confidence) or not 0.0 <= confidence <= 1.0:
        return 0.0, False
    return confidence, True


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

    conf_match = re.search(
        r'"confidence"\s*:\s*(-?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?)\s*(?=[,}]|$)',
        payload,
        flags=re.IGNORECASE,
    )
    if conf_match:
        data["confidence"] = float(conf_match.group(1))

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

    async def is_user_exempt(self, session: AsyncSession, group_id: int, user_id: int) -> bool:
        stmt = select(ModerationExemption.id).where(
            ModerationExemption.group_id == group_id,
            ModerationExemption.user_id == user_id,
        )
        with session.no_autoflush:
            result = await session.execute(stmt)
        return result.scalar_one_or_none() is not None

    async def check_rules(
        self, session: AsyncSession, group_id: int, text: str
    ) -> tuple[bool, str, ModerationRule | None]:
        """使用 LLM 基于群规则判定，返回(是否违规, 原因, 命中规则)。"""
        verdict = await self.evaluate(session, group_id, text)
        return verdict.violated, verdict.reason, verdict.rule

    async def check_rules_verbose(
        self, session: AsyncSession, group_id: int, text: str
    ) -> tuple[bool, str, ModerationRule | None, bool]:
        """同 check_rules，但额外返回判定是否可信（conclusive）。"""
        verdict = await self.evaluate(session, group_id, text)
        return verdict.violated, verdict.reason, verdict.rule, verdict.conclusive

    async def evaluate(
        self, session: AsyncSession, group_id: int, text: str
    ) -> ModerationVerdict:
        """使用 LLM 基于群规则判定，返回带置信度的完整判定。

        审核模型输出不可解析时按不违规处理，但 conclusive=False，
        调用方不应据此写入"已审查通过"类缓存。"""
        stmt = select(ModerationRule).where(
            ModerationRule.group_id == group_id,
            ModerationRule.enabled == True,
        ).order_by(ModerationRule.id)
        with session.no_autoflush:
            result = await session.execute(stmt)
        rules = list(result.scalars().all())

        if not rules:
            log.info("审核通过 (无启用规则)")
            return ModerationVerdict(violated=False, reason="", rule=None, conclusive=True)

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

        system_prompt = build_defended_system(
            get_prompt("moderation").format(rules_json=rules_json)
        )
        user_input = wrap_untrusted("待审核消息", clean_text(text, max_len=1200), max_len=1200)
        llm_raw = await self.llm.moderation(system_prompt, user_input)
        data = _parse_moderation_json(llm_raw)

        if not data:
            raw_text = llm_raw or ""
            escaped = raw_text.replace("\r", "\\r").replace("\n", "\\n")
            preview_limit = 500
            preview_truncated = len(escaped) > preview_limit
            preview = escaped[:preview_limit]
            log.warning(
                "审核模型输出不可解析，按不违规处理: response_len=%d preview_truncated=%s preview=%s",
                len(raw_text),
                preview_truncated,
                preview,
            )
            return ModerationVerdict(violated=False, reason="", rule=None, conclusive=False)

        violated_value = data.get("violated", data.get("violation"))
        if not isinstance(violated_value, bool):
            log.warning("审核模型 violated 字段无效，按不违规处理")
            return ModerationVerdict(
                violated=False,
                reason="",
                rule=None,
                conclusive=False,
            )
        violated = violated_value
        reason = clean_text(str(data.get("reason", "")).strip(), max_len=120)
        confidence, confidence_valid = _parse_confidence(data.get("confidence"))

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
                "审核命中: group=%s rule_id=%s confidence=%.2f reason=%s",
                group_id,
                hit_rule.id if hit_rule else None,
                confidence,
                reason,
            )
            return ModerationVerdict(
                violated=True,
                reason=reason,
                rule=hit_rule,
                conclusive=confidence_valid,
                confidence=confidence,
            )

        log.info("审核通过 (检查了 %d 条规则, AI判定)", len(rules))
        return ModerationVerdict(violated=False, reason="", rule=None, conclusive=True)

    def is_high_confidence(self, verdict: ModerationVerdict) -> bool:
        return bool(
            verdict.conclusive
            and verdict.confidence >= self.config.high_confidence_threshold
        )

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
        return v

    async def add_warning(
        self, session: AsyncSession, group_id: int, user_id: int
    ) -> tuple[int, bool]:
        """增加警告次数，返回(当前次数, 是否应封禁)。"""
        threshold = max(1, int(self.config.warn_threshold))

        async def increment_existing() -> tuple[int, bool] | None:
            next_count = UserWarning.count + 1
            result = await session.execute(
                update(UserWarning)
                .where(
                    UserWarning.group_id == group_id,
                    UserWarning.user_id == user_id,
                )
                .values(
                    count=next_count,
                    is_banned=case(
                        (next_count >= threshold, True),
                        else_=UserWarning.is_banned,
                    ),
                )
                .returning(UserWarning.count, UserWarning.is_banned)
            )
            row = result.first()
            if row is None:
                return None
            return int(row[0]), bool(row[1])

        incremented = await increment_existing()
        if incremented is not None:
            return incremented

        should_ban = threshold <= 1
        try:
            async with session.begin_nested():
                warning = UserWarning(
                    group_id=group_id,
                    user_id=user_id,
                    count=1,
                    is_banned=should_ban,
                )
                session.add(warning)
                await session.flush()
            return 1, should_ban
        except IntegrityError:
            incremented = await increment_existing()
            if incremented is None:
                raise
            return incremented
