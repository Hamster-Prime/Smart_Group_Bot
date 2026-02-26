from __future__ import annotations

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


async def _reply_rules(message: Message, session: AsyncSession) -> None:
    stmt = (
        select(ModerationRule)
        .where(ModerationRule.group_id == message.chat.id)
        .order_by(ModerationRule.id)
    )
    result = await session.execute(stmt)
    rules = result.scalars().all()

    if not rules:
        await message.answer("暂无审核规则。")
        return

    lines = []
    for r in rules:
        status = "启用" if r.enabled else "关闭"
        lines.append(f"#{r.id} [{r.rule_type}] {r.pattern} -> {r.action} ({status})")
    await message.answer("\n".join(lines))


@router.message(Command("authgroup"))
async def cmd_authgroup(message: Message, session: AsyncSession, settings: Settings) -> None:
    if not await ensure_super_admin(message, settings):
        return

    args = (message.text or "").partition(" ")[2].strip()
    group_id = _resolve_target_group_id(message, args)
    if group_id is None:
        await message.answer("用法: /authgroup &lt;群ID&gt; 或在群内直接 /authgroup")
        return

    created = await authorize_group(session, group_id, message.from_user.id if message.from_user else 0)
    if created:
        await message.answer(f"授权成功: {group_id}")
    else:
        await message.answer(f"该群已授权: {group_id}")


@router.message(Command("unauthgroup"))
async def cmd_unauthgroup(message: Message, session: AsyncSession, settings: Settings) -> None:
    if not await ensure_super_admin(message, settings):
        return

    args = (message.text or "").partition(" ")[2].strip()
    group_id = _resolve_target_group_id(message, args)
    if group_id is None:
        await message.answer("用法: /unauthgroup &lt;群ID&gt; 或在群内直接 /unauthgroup")
        return

    removed = await deauthorize_group(session, group_id)
    if removed:
        await message.answer(f"已取消授权: {group_id}")
    else:
        await message.answer(f"该群未授权: {group_id}")


@router.message(Command("authlist"))
async def cmd_authlist(message: Message, session: AsyncSession, settings: Settings) -> None:
    if not await ensure_super_admin(message, settings):
        return

    rows = await list_authorized_groups(session)
    if not rows:
        await message.answer("暂无已授权群组。")
        return

    lines = ["已授权群组列表:"]
    for r in rows[:100]:
        lines.append(f"- {r.group_id} (by={r.authorized_by or 0})")
    await message.answer("\n".join(lines))


@router.message(Command("authadmin"))
async def cmd_authadmin(message: Message, session: AsyncSession, settings: Settings) -> None:
    if not await ensure_super_admin(message, settings):
        return

    args = (message.text or "").partition(" ")[2].strip()
    group_id, user_id = _resolve_admin_binding(message, args)
    if group_id is None or user_id is None:
        await message.answer(
            "用法:\n"
            "1) 群内回复目标用户消息后发送 /authadmin\n"
            "2) /authadmin &lt;群ID&gt; &lt;用户ID&gt;\n"
            "3) 群内 /authadmin &lt;用户ID&gt;"
        )
        return

    created = await authorize_group_admin(session, group_id, user_id, role="admin")
    if created:
        await message.answer(f"已授权群管理权限: group={group_id} user={user_id}")
    else:
        await message.answer(f"该用户已拥有群管理权限: group={group_id} user={user_id}")


@router.message(Command("unauthadmin"))
async def cmd_unauthadmin(message: Message, session: AsyncSession, settings: Settings) -> None:
    if not await ensure_super_admin(message, settings):
        return

    args = (message.text or "").partition(" ")[2].strip()
    group_id, user_id = _resolve_admin_binding(message, args)
    if group_id is None or user_id is None:
        await message.answer(
            "用法:\n"
            "1) 群内回复目标用户消息后发送 /unauthadmin\n"
            "2) /unauthadmin &lt;群ID&gt; &lt;用户ID&gt;\n"
            "3) 群内 /unauthadmin &lt;用户ID&gt;"
        )
        return

    removed = await deauthorize_group_admin(session, group_id, user_id)
    if removed:
        await message.answer(f"已取消群管理权限: group={group_id} user={user_id}")
    else:
        await message.answer(f"该用户未拥有群管理权限: group={group_id} user={user_id}")


@router.message(Command("adminlist"))
async def cmd_adminlist(message: Message, session: AsyncSession, settings: Settings) -> None:
    if not await ensure_super_admin(message, settings):
        return

    args = (message.text or "").partition(" ")[2].strip()
    group_id = _resolve_target_group_id(message, args)
    if group_id is None:
        await message.answer("用法: /adminlist &lt;群ID&gt; 或在群内直接 /adminlist")
        return

    rows = await list_group_admins(session, group_id)
    if not rows:
        await message.answer(f"群 {group_id} 暂无已授权群管理。")
        return

    lines = [f"群 {group_id} 已授权群管理列表:"]
    for row in rows[:200]:
        lines.append(f"- user={row.user_id} role={row.role}")
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
        await message.answer("无法理解你的规则意图，请换个说法。")
        return

    action = str(data.get("action", "unknown")).strip().lower()

    if action == "add":
        rule_type = str(data.get("rule_type", "keyword")).strip().lower()
        pattern = str(data.get("pattern", "")).strip()
        hit_action = str(data.get("hit_action", "warn")).strip().lower()

        if rule_type not in ("keyword", "regex", "llm"):
            await message.answer("rule_type 必须是 keyword、regex 或 llm。")
            return
        if not pattern:
            await message.answer("pattern 不能为空。")
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
        await message.answer(f"已添加规则 #{rule.id}: [{rule_type}] {pattern} -> {hit_action}")
        return

    if action == "delete":
        rule: ModerationRule | None = None

        if data.get("rule_id") is not None:
            try:
                rid = int(data["rule_id"])
            except (TypeError, ValueError):
                await message.answer("rule_id 必须是整数。")
                return
            candidate = await session.get(ModerationRule, rid)
            if candidate and candidate.group_id == message.chat.id:
                rule = candidate
        else:
            pattern = str(data.get("pattern", "")).strip()
            rule_type = str(data.get("rule_type", "")).strip().lower()
            if not pattern:
                await message.answer("删除时请提供 rule_id 或 pattern。")
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
            await message.answer("未找到对应规则。")
            return

        rid = rule.id
        await session.delete(rule)
        await message.answer(f"已删除规则 #{rid}。")
        return

    if action == "list":
        await _reply_rules(message, session)
        return

    await message.answer("无法理解你的规则意图，请换个说法。")


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

    parts = (message.text or "").split()
    if len(parts) < 2:
        await message.answer("用法: /warnings &lt;用户ID&gt;")
        return

    try:
        user_id = int(parts[1])
    except ValueError:
        await message.answer("用户ID 必须是数字。")
        return

    stmt = select(UserWarning).where(
        UserWarning.group_id == message.chat.id,
        UserWarning.user_id == user_id,
    )
    result = await session.execute(stmt)
    warn = result.scalar_one_or_none()

    if not warn:
        await message.answer(f"用户 {user_id} 无警告记录。")
    else:
        status = "已封禁" if warn.is_banned else "正常"
        await message.answer(f"用户 {user_id}: 警告 {warn.count} 次，状态: {status}")


@router.message(Command("aiexempt"))
async def cmd_aiexempt(message: Message, session: AsyncSession, settings: Settings) -> None:
    if not await ensure_group_authorized(message, session, settings):
        return
    if not await ensure_group_admin_permission(message, session, settings):
        return

    reply = message.reply_to_message
    target = reply.from_user if reply else None
    if not target:
        await message.answer("请回复目标用户的一条消息后再执行：/aiexempt")
        return
    if target.is_bot:
        await message.answer("机器人账号无需设置 AI 审查豁免。")
        return

    stmt = select(ModerationExemption).where(
        ModerationExemption.group_id == message.chat.id,
        ModerationExemption.user_id == target.id,
    )
    result = await session.execute(stmt)
    existing = result.scalar_one_or_none()
    if existing:
        await message.answer(f"用户 {target.full_name}（{target.id}）已在 AI 审查豁免名单中。")
        return

    session.add(
        ModerationExemption(
            group_id=message.chat.id,
            user_id=target.id,
            created_by=(message.from_user.id if message.from_user else 0),
        )
    )
    await message.answer(f"已为用户 {target.full_name}（{target.id}）开启 AI 审查豁免。")


@router.message(Command("unaiexempt"))
async def cmd_unaiexempt(message: Message, session: AsyncSession, settings: Settings) -> None:
    if not await ensure_group_authorized(message, session, settings):
        return
    if not await ensure_group_admin_permission(message, session, settings):
        return

    reply = message.reply_to_message
    target = reply.from_user if reply else None
    if not target:
        await message.answer("请回复目标用户的一条消息后再执行：/unaiexempt")
        return
    if target.is_bot:
        await message.answer("机器人账号不在 AI 审查豁免名单中。")
        return

    stmt = select(ModerationExemption).where(
        ModerationExemption.group_id == message.chat.id,
        ModerationExemption.user_id == target.id,
    )
    result = await session.execute(stmt)
    existing = result.scalar_one_or_none()
    if not existing:
        await message.answer(f"用户 {target.full_name}（{target.id}）当前不在 AI 审查豁免名单。")
        return

    await session.delete(existing)
    await message.answer(f"已取消用户 {target.full_name}（{target.id}）的 AI 审查豁免。")

