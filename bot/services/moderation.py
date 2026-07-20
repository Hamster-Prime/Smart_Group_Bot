from __future__ import annotations

import json
import hashlib
import logging
import math
import re
import time
from dataclasses import dataclass

import regex as safe_regex
from sqlalchemy import case, select, update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
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
    rules_fingerprint: str = ""


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
        """Evaluate deterministic rules locally and semantic rules with the LLM.

        审核模型输出不可解析时按不违规处理，但 conclusive=False，
        调用方不应据此写入"已审查通过"类缓存。"""
        stmt = select(ModerationRule).where(
            ModerationRule.group_id == group_id,
            ModerationRule.enabled == True,
        ).order_by(ModerationRule.id)
        with session.no_autoflush:
            result = await session.execute(stmt)
        loaded_rules = list(result.scalars().all())
        # Keep a detached immutable-in-practice snapshot.  Ending the read
        # transaction may expire ORM rows on session factories configured with
        # expire_on_commit=True; accessing those rows during/after a slow LLM
        # call would otherwise silently check a connection back out or raise
        # MissingGreenlet.
        rules = [
            ModerationRule(
                id=int(rule.id),
                group_id=int(rule.group_id),
                rule_type=str(rule.rule_type or ""),
                pattern=str(rule.pattern or ""),
                action=str(rule.action or "warn"),
                enabled=bool(rule.enabled),
            )
            for rule in loaded_rules
        ]
        rules_raw = "\x1e".join(
            "\x1f".join(
                str(value)
                for value in (rule.id, rule.rule_type, rule.pattern, rule.action)
            )
            for rule in rules
        )
        rules_fingerprint = hashlib.sha256(
            rules_raw.encode("utf-8")
        ).hexdigest()[:16]

        def make_verdict(**kwargs: object) -> ModerationVerdict:
            return ModerationVerdict(
                **kwargs,
                rules_fingerprint=rules_fingerprint,
            )

        # A SELECT starts a SQLite transaction and keeps a pooled connection
        # checked out.  End that read-only transaction before regex/LLM work so
        # a slow provider cannot starve the database pool.
        commit = getattr(session, "commit", None)
        if callable(commit):
            await commit()

        if not rules:
            log.info("审核通过 (无启用规则)")
            return make_verdict(violated=False, reason="", rule=None, conclusive=True)

        deterministic_inconclusive = False
        llm_rules: list[ModerationRule] = []
        normalized_text = text or ""
        folded_text = normalized_text.casefold()
        regex_deadline = time.perf_counter() + 0.1
        for rule in rules:
            rule_type = (rule.rule_type or "keyword").strip().lower()
            pattern = (rule.pattern or "").strip()
            if rule_type == "keyword":
                if not pattern:
                    deterministic_inconclusive = True
                    log.warning(
                        "empty keyword moderation rule ignored: group=%s rule=%s",
                        group_id,
                        rule.id,
                    )
                    continue
                if pattern.casefold() in folded_text:
                    log.info("审核命中本地关键词: group=%s rule_id=%s", group_id, rule.id)
                    return make_verdict(
                        violated=True,
                        reason="命中关键词规则",
                        rule=rule,
                        conclusive=True,
                        confidence=1.0,
                    )
                continue
            if rule_type == "regex":
                if not pattern:
                    deterministic_inconclusive = True
                    log.warning(
                        "empty regex moderation rule ignored: group=%s rule=%s",
                        group_id,
                        rule.id,
                    )
                    continue
                remaining_regex_budget = regex_deadline - time.perf_counter()
                if remaining_regex_budget <= 0:
                    deterministic_inconclusive = True
                    log.warning(
                        "regex moderation total budget exhausted: group=%s rule=%s",
                        group_id,
                        rule.id,
                    )
                    continue
                try:
                    matched = safe_regex.search(
                        pattern,
                        normalized_text,
                        flags=safe_regex.IGNORECASE,
                        timeout=min(0.02, remaining_regex_budget),
                    )
                except (safe_regex.error, TimeoutError) as exc:
                    deterministic_inconclusive = True
                    log.warning(
                        "regex moderation rule invalid or timed out: group=%s rule=%s error=%s",
                        group_id,
                        rule.id,
                        exc,
                    )
                    continue
                if matched is not None:
                    log.info("审核命中本地正则: group=%s rule_id=%s", group_id, rule.id)
                    return make_verdict(
                        violated=True,
                        reason="命中正则规则",
                        rule=rule,
                        conclusive=True,
                        confidence=1.0,
                    )
                continue
            # Unknown legacy rule types are treated as semantic rules rather
            # than silently ignored.
            llm_rules.append(rule)

        if not llm_rules:
            log.info("审核通过 (检查了 %d 条本地规则)", len(rules))
            return make_verdict(
                violated=False,
                reason="",
                rule=None,
                conclusive=not deterministic_inconclusive,
            )

        rules_payload = [
            {
                "id": r.id,
                "rule_type": r.rule_type,
                "rule": r.pattern,
                "action": r.action,
            }
            for r in llm_rules
        ]
        rules_json = json.dumps(rules_payload, ensure_ascii=False, indent=2)

        system_prompt = build_defended_system(
            get_prompt("moderation").format(rules_json=rules_json)
        )
        user_input = wrap_untrusted("待审核消息", clean_text(text, max_len=1200), max_len=1200)
        try:
            llm_raw = await self.llm.moderation(system_prompt, user_input)
        except Exception:
            log.exception("审核模型调用失败；本地规则已完成检查")
            return make_verdict(
                violated=False,
                reason="",
                rule=None,
                conclusive=False,
            )
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
            return make_verdict(violated=False, reason="", rule=None, conclusive=False)

        violated_value = data.get("violated", data.get("violation"))
        if not isinstance(violated_value, bool):
            log.warning("审核模型 violated 字段无效，按不违规处理")
            return make_verdict(
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
                hit_rule = next((r for r in llm_rules if r.id == rid_int), None)
            except (TypeError, ValueError):
                hit_rule = None

        if violated and not hit_rule:
            rule_text = str(data.get("rule", "")).strip()
            if rule_text:
                hit_rule = next(
                    (r for r in llm_rules if (r.pattern or "").strip() == rule_text),
                    None,
                )

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
            return make_verdict(
                violated=True,
                reason=reason,
                rule=hit_rule,
                conclusive=confidence_valid,
                confidence=confidence,
            )

        log.info("审核通过 (检查了 %d 条语义规则, AI判定)", len(llm_rules))
        return make_verdict(
            violated=False,
            reason="",
            rule=None,
            conclusive=not deterministic_inconclusive,
        )

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
        *,
        source_message_id: int | None = None,
    ) -> Violation:
        values = {
            "group_id": int(group_id),
            "user_id": int(user_id),
            "rule_id": int(rule.id) if rule is not None and rule.id is not None else None,
            "message_text": str(text or "")[:500],
            "action_taken": str(action or "warn")[:32],
        }
        normalized_source = int(source_message_id or 0)
        if normalized_source <= 0:
            v = Violation(**values)
            session.add(v)
            setattr(v, "_source_event_created", True)
            return v

        values["source_message_id"] = normalized_source
        inserted_id = (
            await session.execute(
                sqlite_insert(Violation)
                .values(**values)
                .on_conflict_do_nothing(
                    index_elements=[
                        Violation.group_id,
                        Violation.source_message_id,
                    ]
                )
                .returning(Violation.id)
            )
        ).scalar_one_or_none()
        created = inserted_id is not None
        v = await session.scalar(
            select(Violation).where(
                Violation.group_id == int(group_id),
                Violation.source_message_id == normalized_source,
            )
        )
        if v is None:
            raise RuntimeError("failed to persist or reload idempotent violation event")
        setattr(v, "_source_event_created", created)
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
