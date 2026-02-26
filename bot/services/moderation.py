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


def _parse_moderation_json(raw: str) -> dict | None:
    payload = (raw or "").strip()

    if payload.startswith("```"):
        payload = re.sub(r"^```(?:json)?", "", payload).strip()
        payload = re.sub(r"```$", "", payload).strip()

    if not payload.startswith("{"):
        m = re.search(r"\{[\s\S]*\}", payload)
        if m:
            payload = m.group(0)

    try:
        data = json.loads(payload)
    except json.JSONDecodeError:
        return None

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
        llm_raw = await self.llm.generate(system_prompt, user_input)
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