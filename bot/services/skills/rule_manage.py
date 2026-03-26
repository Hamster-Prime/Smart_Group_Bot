from __future__ import annotations

import html
import json
import re

from sqlalchemy import select

from bot.db.models import ModerationRule
from bot.services.skills.base import SkillContext, SkillRunResult
from bot.utils.prompts import RULE_MANAGE_SYSTEM
from bot.utils.security import clean_text

_SEMANTIC_ABUSE_HINTS = {"骂人", "辱骂", "脏话", "人身攻击", "侮辱", "喷人"}
_ABUSE_LLM_PATTERN = "禁止辱骂、脏话、人身攻击（含谐音、缩写、变体、阴阳怪气）"


def _truncate_text(text: str, max_len: int) -> str:
    cleaned = (text or "").replace("\n", " ").strip()
    if len(cleaned) <= max_len:
        return cleaned
    return cleaned[:max_len] + "..."


def _rule_type_label(rule_type: str) -> str:
    return {
        "keyword": "关键词",
        "regex": "正则",
        "llm": "语义",
    }.get((rule_type or "").lower(), rule_type or "未知")


def _action_label(action: str) -> str:
    return {
        "warn": "警告",
        "delete": "删消息",
        "ban": "封禁",
    }.get((action or "").lower(), action or "未知")


def _parse_json_payload(raw: str) -> dict | None:
    payload = (raw or "").strip()
    if payload.startswith("```"):
        payload = re.sub(r"^```(?:json)?", "", payload).strip()
        payload = re.sub(r"```$", "", payload).strip()
    if not payload.startswith("{"):
        match = re.search(r"\{[\s\S]*\}", payload)
        if match:
            payload = match.group(0)
    try:
        data = json.loads(payload)
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


def _format_rule_list(rules: list[ModerationRule]) -> str:
    if not rules:
        return "<b>群审核规则</b>\n当前没有规则。\n\n删除请使用 /rules 打开列表后点按钮。"

    lines = [
        "<b>群审核规则</b>",
        f"共 {len(rules)} 条",
        "",
    ]
    for idx, rule in enumerate(rules[:20], start=1):
        status = "启用" if rule.enabled else "关闭"
        pattern = html.escape(_truncate_text(rule.pattern or "", 120))
        lines.append(
            f"{idx}. <b>#{rule.id}</b> [{_rule_type_label(rule.rule_type)}] {_action_label(rule.action)} | {status}"
        )
        lines.append(f"规则: {pattern}")
        lines.append("")
    if len(rules) > 20:
        lines.append(f"... 其余 {len(rules) - 20} 条请使用 /rules 查看")
    lines.extend(["删除请使用 /rules 打开列表后点按钮。"])
    return "\n".join(lines).rstrip()


class RuleManageSkill:
    name = "rule_manage"
    description = (
        "View or add moderation rules for admin requests. "
        "Never use this tool to delete rules; rule deletion must go through /rules."
    )
    parameters_schema = {
        "type": "object",
        "properties": {
            "request_text": {
                "type": "string",
                "description": "The user's rule-management request. Omit it to use the current user message.",
            }
        },
        "additionalProperties": False,
    }

    async def run(self, arguments: dict, context: SkillContext) -> SkillRunResult:
        if context.session is None or context.llm is None:
            return SkillRunResult(
                ok=False,
                skill=self.name,
                summary="当前无法处理群规请求，请稍后重试。",
                error="missing_context",
            )

        group_id = int(context.chat_id or 0)
        if group_id == 0:
            return SkillRunResult(
                ok=False,
                skill=self.name,
                summary="当前会话不支持群规管理。",
                error="missing_chat_context",
            )

        if not (context.sender_is_owner or context.sender_is_tg_admin):
            return SkillRunResult(
                ok=True,
                skill=self.name,
                summary="仅群管理员可管理群规；删除请使用 /rules。",
                payload={"denied": True},
            )

        request_text = clean_text(
            str(arguments.get("request_text", "") or context.current_user_text),
            max_len=1200,
        )
        if not request_text:
            return SkillRunResult(
                ok=True,
                skill=self.name,
                summary="请直接说要新增或查看什么群规；删除请使用 /rules。",
            )

        parsed = _parse_json_payload(await context.llm.generate(RULE_MANAGE_SYSTEM, request_text))
        if not parsed:
            return SkillRunResult(
                ok=True,
                skill=self.name,
                summary="我没看懂这条群规指令。你可以说“新增群规 禁止发广告”，删除请使用 /rules。",
            )

        action = clean_text(str(parsed.get("action", "unknown")).lower(), max_len=24)
        if action == "list":
            stmt = (
                select(ModerationRule)
                .where(ModerationRule.group_id == group_id)
                .order_by(ModerationRule.id.asc())
            )
            result = await context.session.execute(stmt)
            rules = list(result.scalars().all())
            return SkillRunResult(
                ok=True,
                skill=self.name,
                summary=_format_rule_list(rules),
                payload={"action": "list", "count": len(rules)},
            )

        if action == "delete":
            return SkillRunResult(
                ok=True,
                skill=self.name,
                summary="群规删除请使用 /rules 打开列表后点按钮，我这边不直接删除。",
                payload={"redirect_command": "/rules", "action": "delete"},
            )

        if action != "add":
            return SkillRunResult(
                ok=True,
                skill=self.name,
                summary="这条群规请求暂不支持；删除请使用 /rules。",
            )

        rule_type = clean_text(str(parsed.get("rule_type", "keyword")).lower(), max_len=16)
        pattern = clean_text(str(parsed.get("pattern", "")), max_len=1000)
        hit_action = clean_text(str(parsed.get("hit_action", "warn")).lower(), max_len=16)
        if rule_type not in {"keyword", "regex", "llm"}:
            return SkillRunResult(
                ok=True,
                skill=self.name,
                summary="群规添加失败：规则类型必须是 keyword、regex 或 llm。",
            )
        if hit_action not in {"warn", "delete", "ban"}:
            hit_action = "warn"
        if not pattern:
            return SkillRunResult(
                ok=True,
                skill=self.name,
                summary="群规添加失败：规则内容不能为空。",
            )

        if rule_type == "keyword" and pattern.lower() in _SEMANTIC_ABUSE_HINTS:
            rule_type = "llm"
            pattern = _ABUSE_LLM_PATTERN

        rule = ModerationRule(
            group_id=group_id,
            rule_type=rule_type,
            pattern=pattern,
            action=hit_action,
        )
        context.session.add(rule)
        await context.session.commit()
        await context.session.refresh(rule)
        return SkillRunResult(
            ok=True,
            skill=self.name,
            summary=(
                "<b>规则添加成功</b>\n"
                "<b>规则编号</b>: #{rule_id}\n"
                "<b>规则类型</b>: {rule_type}\n"
                "<b>命中动作</b>: {hit_action}\n"
                "<b>规则内容</b>: {pattern}\n\n"
                "如需删除，请使用 /rules。"
            ).format(
                rule_id=rule.id,
                rule_type=_rule_type_label(rule_type),
                hit_action=_action_label(hit_action),
                pattern=html.escape(_truncate_text(pattern, 160)),
            ),
            payload={"action": "add", "rule_id": rule.id},
        )
