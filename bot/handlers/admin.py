from __future__ import annotations

import html
import json
import logging
import re

from aiogram import Router
from aiogram.filters import Command
from aiogram.types import Message
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from bot.config import Settings
from bot.db.models import ModerationExemption, ModerationRule, UserWarning
from bot.services.authz import (
    authorize_group,
    authorize_group_admin,
    deauthorize_group_admin,
    deauthorize_group,
    ensure_group_admin_permission,
    ensure_group_authorized,
    ensure_super_admin,
    list_group_admins,
    list_authorized_groups,
)
from bot.services.llm import LLMService
from bot.utils.prompts import RULE_MANAGE_SYSTEM
from bot.utils.telegram import is_group

router = Router()
log = logging.getLogger(__name__)

# 将“禁止骂人”等自然语言规则归一为语义审核模式（llm）
_SEMANTIC_ABUSE_HINTS = {"骂人", "辱骂", "脏话", "人身攻击", "侮辱", "喷人"}
_ABUSE_LLM_PATTERN = "禁止辱骂、脏话、人身攻击（含谐音、缩写、变体、阴阳怪气）"


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

    if not isinstance(data, dict):
        return None
    return data


def _resolve_target_group_id(message: Message, args: str) -> int | None:
    arg = (args or "").strip()
    if arg:
        try:
            return int(arg)
        except ValueError:
            return None
    if is_group(message):
        return message.chat.id
    return None


def _resolve_admin_binding(message: Message, args: str) -> tuple[int | None, int | None]:
    arg = (args or "").strip()
    parts = arg.split() if arg else []

    if len(parts) >= 2:
        try:
            return int(parts[0]), int(parts[1])
        except ValueError:
            return None, None

    if len(parts) == 1 and is_group(message):
        try:
            return message.chat.id, int(parts[0])
        except ValueError:
            return None, None

    if is_group(message):
        reply = message.reply_to_message
        if reply and reply.from_user:
            return message.chat.id, reply.from_user.id

    return None, None


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


def _safe_user_label(user_id: int, full_name: str | None = None) -> str:
    safe_name = html.escape((full_name or "").strip())
    if safe_name:
        return f"{safe_name}（{user_id}）"
    return str(user_id)


async def _reply_rules(message: Message, session: AsyncSession) -> None:
    stmt = (
        select(ModerationRule)
        .where(ModerationRule.group_id == message.chat.id)
        .order_by(ModerationRule.id)
    )
    result = await session.execute(stmt)
    rules = result.scalars().all()

    if not rules:
        await message.answer("<b>群审核规则</b>\n当前没有规则，可使用 /addrule 添加。")
        return

    lines = [f"<b>群审核规则</b>\n共 {len(rules)} 条"]
    for r in rules:
        status = "启用" if r.enabled else "关闭"
        pattern_preview = html.escape(_truncate_text(r.pattern or "", 120))
        lines.append(
            "\n".join(
                [
                    f"<b>#{r.id}</b> [{_rule_type_label(r.rule_type)}] {_action_label(r.action)} | {status}",
                    f"规则: {pattern_preview}",
                ]
            )
        )
    await message.answer("\n\n".join(lines))


@router.message(Command("authgroup"))
async def cmd_authgroup(message: Message, session: AsyncSession, settings: Settings) -> None:
    if not await ensure_super_admin(message, settings):
        return

    args = (message.text or "").partition(" ")[2].strip()
    group_id = _resolve_target_group_id(message, args)
    if group_id is None:
        await message.answer(
            "<b>命令用法</b>\n"
            "/authgroup &lt;群ID&gt;\n"
            "或在群内直接发送 /authgroup"
        )
        return

    created = await authorize_group(session, group_id, message.from_user.id if message.from_user else 0)
    if created:
        await message.answer(
            "<b>群组授权结果</b>\n"
            f"<b>群ID</b>: {group_id}\n"
            "<b>状态</b>: 授权成功"
        )
    else:
        await message.answer(
            "<b>群组授权结果</b>\n"
            f"<b>群ID</b>: {group_id}\n"
            "<b>状态</b>: 已处于授权状态"
        )


@router.message(Command("unauthgroup"))
async def cmd_unauthgroup(message: Message, session: AsyncSession, settings: Settings) -> None:
    if not await ensure_super_admin(message, settings):
        return

    args = (message.text or "").partition(" ")[2].strip()
    group_id = _resolve_target_group_id(message, args)
    if group_id is None:
        await message.answer(
            "<b>命令用法</b>\n"
            "/unauthgroup &lt;群ID&gt;\n"
            "或在群内直接发送 /unauthgroup"
        )
        return

    removed = await deauthorize_group(session, group_id)
    if removed:
        await message.answer(
            "<b>群组授权结果</b>\n"
            f"<b>群ID</b>: {group_id}\n"
            "<b>状态</b>: 已取消授权"
        )
    else:
        await message.answer(
            "<b>群组授权结果</b>\n"
            f"<b>群ID</b>: {group_id}\n"
            "<b>状态</b>: 当前未授权"
        )


@router.message(Command("authlist"))
async def cmd_authlist(message: Message, session: AsyncSession, settings: Settings) -> None:
    if not await ensure_super_admin(message, settings):
        return

    rows = await list_authorized_groups(session)
    if not rows:
        await message.answer("<b>已授权群组</b>\n当前为空。")
        return

    lines = [f"<b>已授权群组</b>\n共 {len(rows)} 个"]
    for idx, r in enumerate(rows[:100], start=1):
        lines.append(f"{idx}. <b>群ID</b>: {r.group_id} | 授权人: {r.authorized_by or 0}")
    await message.answer("\n".join(lines))


@router.message(Command("authadmin"))
async def cmd_authadmin(message: Message, session: AsyncSession, settings: Settings) -> None:
    if not await ensure_super_admin(message, settings):
        return

    args = (message.text or "").partition(" ")[2].strip()
    group_id, user_id = _resolve_admin_binding(message, args)
    if group_id is None or user_id is None:
        await message.answer(
            "<b>命令用法</b>\n"
            "1. 群内回复目标用户消息后发送 /authadmin\n"
            "2. /authadmin &lt;群ID&gt; &lt;用户ID&gt;\n"
            "3. 群内 /authadmin &lt;用户ID&gt;"
        )
        return

    created = await authorize_group_admin(session, group_id, user_id, role="admin")
    if created:
        await message.answer(
            "<b>群管理授权结果</b>\n"
            f"<b>群ID</b>: {group_id}\n"
            f"<b>用户ID</b>: {user_id}\n"
            "<b>状态</b>: 授权成功"
        )
    else:
        await message.answer(
            "<b>群管理授权结果</b>\n"
            f"<b>群ID</b>: {group_id}\n"
            f"<b>用户ID</b>: {user_id}\n"
            "<b>状态</b>: 已有群管理权限"
        )


@router.message(Command("unauthadmin"))
async def cmd_unauthadmin(message: Message, session: AsyncSession, settings: Settings) -> None:
    if not await ensure_super_admin(message, settings):
        return

    args = (message.text or "").partition(" ")[2].strip()
    group_id, user_id = _resolve_admin_binding(message, args)
    if group_id is None or user_id is None:
        await message.answer(
            "<b>命令用法</b>\n"
            "1. 群内回复目标用户消息后发送 /unauthadmin\n"
            "2. /unauthadmin &lt;群ID&gt; &lt;用户ID&gt;\n"
            "3. 群内 /unauthadmin &lt;用户ID&gt;"
        )
        return

    removed = await deauthorize_group_admin(session, group_id, user_id)
    if removed:
        await message.answer(
            "<b>群管理授权结果</b>\n"
            f"<b>群ID</b>: {group_id}\n"
            f"<b>用户ID</b>: {user_id}\n"
            "<b>状态</b>: 已取消群管理权限"
        )
    else:
        await message.answer(
            "<b>群管理授权结果</b>\n"
            f"<b>群ID</b>: {group_id}\n"
            f"<b>用户ID</b>: {user_id}\n"
            "<b>状态</b>: 当前未拥有群管理权限"
        )


@router.message(Command("adminlist"))
async def cmd_adminlist(message: Message, session: AsyncSession, settings: Settings) -> None:
    if not await ensure_super_admin(message, settings):
        return

    args = (message.text or "").partition(" ")[2].strip()
    group_id = _resolve_target_group_id(message, args)
    if group_id is None:
        await message.answer(
            "<b>命令用法</b>\n"
            "/adminlist &lt;群ID&gt;\n"
            "或在群内直接发送 /adminlist"
        )
        return

    rows = await list_group_admins(session, group_id)
    if not rows:
        await message.answer(
            "<b>群管理授权列表</b>\n"
            f"<b>群ID</b>: {group_id}\n"
            "<b>结果</b>: 当前无已授权群管理"
        )
        return

    lines = [f"<b>群管理授权列表</b>\n群ID: {group_id} | 共 {len(rows)} 人"]
    for idx, row in enumerate(rows[:200], start=1):
        lines.append(f"{idx}. <b>用户ID</b>: {row.user_id} | 角色: {html.escape(row.role)}")
    await message.answer("\n".join(lines))


@router.message(Command("addrule"))
async def cmd_addrule(message: Message, session: AsyncSession, settings: Settings) -> None:
    if not await ensure_group_authorized(message, session, settings):
        return
    if not await ensure_group_admin_permission(message, session, settings):
        return

    args = (message.text or "").partition(" ")[2].strip()
    if not args:
        await message.answer(
            "<b>规则管理指令格式</b>\n"
            "用法: /addrule &lt;自然语言&gt;\n"
            "示例: /addrule 增加群规 禁止骂人"
        )
        return

    if args.lower() == "list":
        await _reply_rules(message, session)
        return

    llm = LLMService(
        settings.bot.main_model,
        settings.bot.decision_model,
        moderation=settings.bot.moderation_model,
        embed=settings.bot.embed_model,
    )
    result = await llm.generate(RULE_MANAGE_SYSTEM, args)
    data = _parse_json_payload(result)
    if not data:
        await message.answer("<b>规则解析失败</b>\n无法理解你的规则意图，请换个说法。")
        return

    action = str(data.get("action", "unknown")).strip().lower()

    if action == "add":
        rule_type = str(data.get("rule_type", "keyword")).strip().lower()
        pattern = str(data.get("pattern", "")).strip()
        hit_action = str(data.get("hit_action", "warn")).strip().lower()

        if rule_type not in ("keyword", "regex", "llm"):
            await message.answer(
                "<b>规则添加失败</b>\n"
                "rule_type 必须是 keyword、regex 或 llm"
            )
            return
        if not pattern:
            await message.answer("<b>规则添加失败</b>\npattern 不能为空")
            return
        if hit_action not in ("warn", "delete", "ban"):
            hit_action = "warn"

        # 对典型“骂人/辱骂/脏话”语义规则，默认走 llm 语义判定，避免只靠固定词表。
        if rule_type == "keyword" and pattern.lower() in _SEMANTIC_ABUSE_HINTS:
            rule_type = "llm"
            pattern = _ABUSE_LLM_PATTERN

        rule = ModerationRule(
            group_id=message.chat.id,
            rule_type=rule_type,
            pattern=pattern,
            action=hit_action,
        )
        session.add(rule)
        await session.flush()
        await message.answer(
            "<b>规则添加成功</b>\n"
            f"<b>规则编号</b>: #{rule.id}\n"
            f"<b>规则类型</b>: {_rule_type_label(rule_type)}\n"
            f"<b>命中动作</b>: {_action_label(hit_action)}\n"
            f"<b>规则内容</b>: {html.escape(_truncate_text(pattern, 160))}"
        )
        return

    if action == "delete":
        rule: ModerationRule | None = None

        if data.get("rule_id") is not None:
            try:
                rid = int(data["rule_id"])
            except (TypeError, ValueError):
                await message.answer("<b>规则删除失败</b>\nrule_id 必须是整数")
                return
            candidate = await session.get(ModerationRule, rid)
            if candidate and candidate.group_id == message.chat.id:
                rule = candidate
        else:
            pattern = str(data.get("pattern", "")).strip()
            rule_type = str(data.get("rule_type", "")).strip().lower()
            if not pattern:
                await message.answer("<b>规则删除失败</b>\n删除时请提供 rule_id 或 pattern")
                return

            stmt = select(ModerationRule).where(
                ModerationRule.group_id == message.chat.id,
                ModerationRule.pattern == pattern,
            )
            if rule_type:
                stmt = stmt.where(ModerationRule.rule_type == rule_type)
            stmt = stmt.order_by(ModerationRule.id.desc())
            result = await session.execute(stmt)
            rule = result.scalars().first()

        if not rule:
            await message.answer("<b>规则删除失败</b>\n未找到对应规则")
            return

        rid = rule.id
        await session.delete(rule)
        await message.answer(
            "<b>规则删除成功</b>\n"
            f"<b>规则编号</b>: #{rid}\n"
            f"<b>规则类型</b>: {_rule_type_label(rule.rule_type)}\n"
            f"<b>规则内容</b>: {html.escape(_truncate_text(rule.pattern or '', 160))}"
        )
        return

    if action == "list":
        await _reply_rules(message, session)
        return

    await message.answer("<b>规则解析失败</b>\n无法理解你的规则意图，请换个说法。")


@router.message(Command("rules"))
async def cmd_rules(message: Message, session: AsyncSession, settings: Settings) -> None:
    if not await ensure_group_authorized(message, session, settings):
        return
    if not await ensure_group_admin_permission(message, session, settings):
        return
    await _reply_rules(message, session)


@router.message(Command("warnings"))
async def cmd_warnings(message: Message, session: AsyncSession, settings: Settings) -> None:
    if not await ensure_group_authorized(message, session, settings):
        return
    if not await ensure_group_admin_permission(message, session, settings):
        return

    stmt = (
        select(UserWarning)
        .where(
            UserWarning.group_id == message.chat.id,
            UserWarning.count > 0,
        )
        .order_by(UserWarning.is_banned.desc(), UserWarning.count.desc(), UserWarning.user_id.asc())
    )
    result = await session.execute(stmt)
    rows = result.scalars().all()

    if not rows:
        await message.answer(
            "<b>当前群组警告/封禁名单</b>\n"
            "<b>结果</b>: 暂无被警告或封禁用户"
        )
        return

    threshold = max(1, settings.moderation.warn_threshold)
    banned_count = sum(1 for row in rows if row.is_banned)
    lines = [
        "<b>当前群组警告/封禁名单</b>",
        f"<b>总人数</b>: {len(rows)}",
        f"<b>已封禁</b>: {banned_count}",
        f"<b>警告中</b>: {len(rows) - banned_count}",
        "",
    ]
    for idx, row in enumerate(rows[:100], start=1):
        status = "已封禁" if row.is_banned else "警告中"
        lines.append(f"{idx}. 用户ID: {row.user_id} | 次数: {row.count}/{threshold} | 状态: {status}")

    await message.answer("\n".join(lines))


@router.message(Command("aiexempt"))
async def cmd_aiexempt(message: Message, session: AsyncSession, settings: Settings) -> None:
    if not await ensure_group_authorized(message, session, settings):
        return
    if not await ensure_group_admin_permission(message, session, settings):
        return

    reply = message.reply_to_message
    target = reply.from_user if reply else None
    if not target:
        await message.answer(
            "<b>命令用法</b>\n"
            "请先回复目标用户的一条消息，再执行 /aiexempt"
        )
        return
    if target.is_bot:
        await message.answer("<b>AI 审查豁免</b>\n机器人账号无需设置豁免。")
        return

    stmt = select(ModerationExemption).where(
        ModerationExemption.group_id == message.chat.id,
        ModerationExemption.user_id == target.id,
    )
    result = await session.execute(stmt)
    existing = result.scalar_one_or_none()
    if existing:
        await message.answer(
            "<b>AI 审查豁免</b>\n"
            f"<b>用户</b>: {_safe_user_label(target.id, target.full_name)}\n"
            "<b>状态</b>: 已在豁免名单中"
        )
        return

    session.add(
        ModerationExemption(
            group_id=message.chat.id,
            user_id=target.id,
            created_by=(message.from_user.id if message.from_user else 0),
        )
    )
    await message.answer(
        "<b>AI 审查豁免</b>\n"
        f"<b>用户</b>: {_safe_user_label(target.id, target.full_name)}\n"
        "<b>状态</b>: 已开启豁免"
    )


@router.message(Command("unaiexempt"))
async def cmd_unaiexempt(message: Message, session: AsyncSession, settings: Settings) -> None:
    if not await ensure_group_authorized(message, session, settings):
        return
    if not await ensure_group_admin_permission(message, session, settings):
        return

    reply = message.reply_to_message
    target = reply.from_user if reply else None
    if not target:
        await message.answer(
            "<b>命令用法</b>\n"
            "请先回复目标用户的一条消息，再执行 /unaiexempt"
        )
        return
    if target.is_bot:
        await message.answer("<b>AI 审查豁免</b>\n机器人账号不在豁免名单中。")
        return

    stmt = select(ModerationExemption).where(
        ModerationExemption.group_id == message.chat.id,
        ModerationExemption.user_id == target.id,
    )
    result = await session.execute(stmt)
    existing = result.scalar_one_or_none()
    if not existing:
        await message.answer(
            "<b>AI 审查豁免</b>\n"
            f"<b>用户</b>: {_safe_user_label(target.id, target.full_name)}\n"
            "<b>状态</b>: 当前不在豁免名单"
        )
        return

    await session.delete(existing)
    await message.answer(
        "<b>AI 审查豁免</b>\n"
        f"<b>用户</b>: {_safe_user_label(target.id, target.full_name)}\n"
        "<b>状态</b>: 已取消豁免"
    )

