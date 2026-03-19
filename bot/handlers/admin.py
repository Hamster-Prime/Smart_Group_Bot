from __future__ import annotations

import html
import json
import logging
import re

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from bot.config import Settings
from bot.db.models import Group, ModerationExemption, ModerationRule, ReplyMute, UserWarning
from bot.services.authz import (
    authorize_group,
    authorize_group_admin,
    deauthorize_group_admin,
    deauthorize_group,
    ensure_group_admin_permission,
    ensure_group_authorized,
    ensure_super_admin,
    is_group_admin_authorized,
    is_super_admin_user_id,
    list_group_admins,
    list_authorized_groups,
)
from bot.services.doubao_tts import (
    DoubaoTTSService,
    TTS_MODE_ALWAYS,
    TTS_MODE_OFF,
    TTS_MODE_ON,
    build_tts_status_text,
    set_tts_mode,
)
from bot.services.llm import LLMService
from bot.services.scheduled_tasks import (
    get_cooldown_status_text,
    set_cooldown_task_enabled,
)
from bot.utils.prompts import RULE_MANAGE_SYSTEM
from bot.utils.telegram import answer_with_auto_delete, is_group

router = Router()
log = logging.getLogger(__name__)

# 将“禁止骂人”等自然语言规则归一为语义审核模式（llm）
_SEMANTIC_ABUSE_HINTS = {"骂人", "辱骂", "脏话", "人身攻击", "侮辱", "喷人"}
_ABUSE_LLM_PATTERN = "禁止辱骂、脏话、人身攻击（含谐音、缩写、变体、阴阳怪气）"
_MUTE_ALL_REPLIES_KEY = "mute_all_replies"
_LIST_PAGE_SIZE = 5
_MUTE_USAGE = (
    "<b>命令用法</b>\n"
    "1. 回复目标用户消息后发送 /mute\n"
    "2. 发送 /mute all（本群仅做审核，不再回复）"
)
_UNMUTE_USAGE = (
    "<b>命令用法</b>\n"
    "1. 回复目标用户消息后发送 /unmute\n"
    "2. 发送 /unmute all（恢复本群正常回复）"
)
_PROACTIVE_USAGE = (
    "<b>命令用法</b>\n"
    "1. /proactive on\n"
    "2. /proactive off\n"
    "3. /proactive status"
)
_TTS_USAGE = (
    "<b>命令用法</b>\n"
    "1. /tts on\n"
    "2. /tts off\n"
    "3. /tts always\n"
    "4. /tts status"
)


async def _answer(
    message: Message,
    settings: Settings,
    text: str,
    **kwargs: object,
) -> None:
    await answer_with_auto_delete(
        message,
        text,
        auto_delete_minutes=settings.bot.auto_delete_minutes,
        **kwargs,
    )


async def _ensure_group_row(session: AsyncSession, group_id: int, title: str) -> Group:
    row = await session.get(Group, group_id)
    if row:
        if title and row.title != title:
            row.title = title
        if row.settings is None:
            row.settings = {}
        return row

    try:
        async with session.begin_nested():
            row = Group(id=group_id, title=title or "", settings={})
            session.add(row)
            await session.flush()
            return row
    except IntegrityError:
        row = await session.get(Group, group_id)
        if row:
            if title and row.title != title:
                row.title = title
            if row.settings is None:
                row.settings = {}
            return row

    row = Group(id=group_id, title=title or "", settings={})
    session.add(row)
    return row


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


def _parse_int(value: str, *, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _build_auth_group_list_page(
    rows: list,
    *,
    page: int,
) -> tuple[str, InlineKeyboardMarkup | None]:
    total = len(rows)
    total_pages = max(1, (total + _LIST_PAGE_SIZE - 1) // _LIST_PAGE_SIZE)
    page = min(max(page, 0), total_pages - 1)
    start = page * _LIST_PAGE_SIZE
    end = min(start + _LIST_PAGE_SIZE, total)

    lines = [
        "<b>已授权群组</b>",
        f"共 {total} 个 | 页码: {page + 1}/{total_pages}",
        "",
    ]
    for idx, row in enumerate(rows[start:end], start=start + 1):
        lines.append(f"{idx}. <b>群ID</b>: {row.group_id} | 授权人: {row.authorized_by or 0}")

    keyboard_rows: list[list[InlineKeyboardButton]] = []
    nav_row: list[InlineKeyboardButton] = []
    if page > 0:
        nav_row.append(
            InlineKeyboardButton(
                text="⬅️ 上一页",
                callback_data=f"atl:{page - 1}",
            )
        )
    if page < total_pages - 1:
        nav_row.append(
            InlineKeyboardButton(
                text="下一页 ➡️",
                callback_data=f"atl:{page + 1}",
            )
        )
    if nav_row:
        keyboard_rows.append(nav_row)
    keyboard = InlineKeyboardMarkup(inline_keyboard=keyboard_rows) if keyboard_rows else None
    return "\n".join(lines), keyboard


def _build_admin_list_page(
    rows: list,
    *,
    group_id: int,
    page: int,
) -> tuple[str, InlineKeyboardMarkup | None]:
    total = len(rows)
    total_pages = max(1, (total + _LIST_PAGE_SIZE - 1) // _LIST_PAGE_SIZE)
    page = min(max(page, 0), total_pages - 1)
    start = page * _LIST_PAGE_SIZE
    end = min(start + _LIST_PAGE_SIZE, total)

    lines = [
        "<b>群管理授权列表</b>",
        f"群ID: {group_id} | 共 {total} 人 | 页码: {page + 1}/{total_pages}",
        "",
    ]
    for idx, row in enumerate(rows[start:end], start=start + 1):
        lines.append(f"{idx}. <b>用户ID</b>: {row.user_id} | 角色: {html.escape(row.role)}")

    keyboard_rows: list[list[InlineKeyboardButton]] = []
    nav_row: list[InlineKeyboardButton] = []
    if page > 0:
        nav_row.append(
            InlineKeyboardButton(
                text="⬅️ 上一页",
                callback_data=f"adl:{group_id}:{page - 1}",
            )
        )
    if page < total_pages - 1:
        nav_row.append(
            InlineKeyboardButton(
                text="下一页 ➡️",
                callback_data=f"adl:{group_id}:{page + 1}",
            )
        )
    if nav_row:
        keyboard_rows.append(nav_row)
    keyboard = InlineKeyboardMarkup(inline_keyboard=keyboard_rows) if keyboard_rows else None
    return "\n".join(lines), keyboard


def _build_warning_list_page(
    rows: list[UserWarning],
    *,
    threshold: int,
    page: int,
) -> tuple[str, InlineKeyboardMarkup | None]:
    total = len(rows)
    total_pages = max(1, (total + _LIST_PAGE_SIZE - 1) // _LIST_PAGE_SIZE)
    page = min(max(page, 0), total_pages - 1)
    start = page * _LIST_PAGE_SIZE
    end = min(start + _LIST_PAGE_SIZE, total)
    banned_count = sum(1 for row in rows if row.is_banned)

    lines = [
        "<b>当前群组警告/封禁名单</b>",
        f"<b>总人数</b>: {total} | 页码: {page + 1}/{total_pages}",
        f"<b>已封禁</b>: {banned_count}",
        f"<b>警告中</b>: {total - banned_count}",
        "",
    ]
    for idx, row in enumerate(rows[start:end], start=start + 1):
        status = "已封禁" if row.is_banned else "警告中"
        lines.append(f"{idx}. 用户ID: {row.user_id} | 次数: {row.count}/{threshold} | 状态: {status}")

    keyboard_rows: list[list[InlineKeyboardButton]] = []
    nav_row: list[InlineKeyboardButton] = []
    if page > 0:
        nav_row.append(
            InlineKeyboardButton(
                text="⬅️ 上一页",
                callback_data=f"wpl:{page - 1}",
            )
        )
    if page < total_pages - 1:
        nav_row.append(
            InlineKeyboardButton(
                text="下一页 ➡️",
                callback_data=f"wpl:{page + 1}",
            )
        )
    if nav_row:
        keyboard_rows.append(nav_row)
    keyboard = InlineKeyboardMarkup(inline_keyboard=keyboard_rows) if keyboard_rows else None
    return "\n".join(lines), keyboard


def _build_rule_list_page(
    rules: list[ModerationRule],
    *,
    page: int,
) -> tuple[str, InlineKeyboardMarkup]:
    total = len(rules)
    total_pages = max(1, (total + _LIST_PAGE_SIZE - 1) // _LIST_PAGE_SIZE)
    page = min(max(page, 0), total_pages - 1)
    start = page * _LIST_PAGE_SIZE
    end = min(start + _LIST_PAGE_SIZE, total)

    lines = [
        "<b>群审核规则</b>",
        f"共 {total} 条 | 页码: {page + 1}/{total_pages}",
        "",
        "点击下方按钮可删除对应规则：",
        "",
    ]
    keyboard_rows: list[list[InlineKeyboardButton]] = []
    for idx, rule in enumerate(rules[start:end], start=start + 1):
        status = "启用" if rule.enabled else "关闭"
        pattern_preview = html.escape(_truncate_text(rule.pattern or "", 120))
        lines.append(
            f"{idx}. <b>#{rule.id}</b> [{_rule_type_label(rule.rule_type)}] {_action_label(rule.action)} | {status}"
        )
        lines.append(f"规则: {pattern_preview}")
        lines.append("")
        keyboard_rows.append(
            [
                InlineKeyboardButton(
                    text=f"🗑 删除规则 #{rule.id}",
                    callback_data=f"rud:{rule.id}:{page}",
                )
            ]
        )

    nav_row: list[InlineKeyboardButton] = []
    if page > 0:
        nav_row.append(
            InlineKeyboardButton(
                text="⬅️ 上一页",
                callback_data=f"rul:{page - 1}",
            )
        )
    if page < total_pages - 1:
        nav_row.append(
            InlineKeyboardButton(
                text="下一页 ➡️",
                callback_data=f"rul:{page + 1}",
            )
        )
    if nav_row:
        keyboard_rows.append(nav_row)

    return "\n".join(lines).rstrip(), InlineKeyboardMarkup(inline_keyboard=keyboard_rows)


async def _callback_user_can_manage_rules(
    callback: CallbackQuery,
    session: AsyncSession,
    settings: Settings,
) -> bool:
    msg = callback.message
    if not msg or not msg.chat or msg.chat.type not in ("group", "supergroup"):
        await callback.answer("消息已失效", show_alert=True)
        return False
    if not await ensure_group_authorized(msg, session, settings):
        await callback.answer("当前群组未授权", show_alert=True)
        return False

    user = callback.from_user
    if user and is_super_admin_user_id(user.id, settings):
        return True
    if not user:
        await callback.answer("无法识别操作者", show_alert=True)
        return False
    if await is_group_admin_authorized(session, msg.chat.id, user.id):
        return True

    await callback.answer("仅群管理可操作该列表", show_alert=True)
    return False


async def _callback_user_is_super_admin(
    callback: CallbackQuery,
    settings: Settings,
) -> bool:
    user = callback.from_user
    if user and is_super_admin_user_id(user.id, settings):
        return True
    await callback.answer("仅最高管理员可操作该列表", show_alert=True)
    return False


async def _reply_rules(message: Message, session: AsyncSession, settings: Settings) -> None:
    stmt = (
        select(ModerationRule)
        .where(ModerationRule.group_id == message.chat.id)
        .order_by(ModerationRule.id)
    )
    result = await session.execute(stmt)
    rules = result.scalars().all()

    if not rules:
        await _answer(message, settings, "<b>群审核规则</b>\n当前没有规则，可使用 /addrule 添加。")
        return

    text, keyboard = _build_rule_list_page(list(rules), page=0)
    await _answer(
        message,
        settings,
        text,
        reply_markup=keyboard,
        disable_web_page_preview=True,
    )


@router.message(Command("authgroup"))
async def cmd_authgroup(message: Message, session: AsyncSession, settings: Settings) -> None:
    if not await ensure_super_admin(message, settings):
        return

    args = (message.text or "").partition(" ")[2].strip()
    group_id = _resolve_target_group_id(message, args)
    if group_id is None:
        await _answer(message, settings, 
            "<b>命令用法</b>\n"
            "/authgroup &lt;群ID&gt;\n"
            "或在群内直接发送 /authgroup"
        )
        return

    created = await authorize_group(session, group_id, message.from_user.id if message.from_user else 0)
    if created:
        await _answer(message, settings, 
            "<b>群组授权结果</b>\n"
            f"<b>群ID</b>: {group_id}\n"
            "<b>状态</b>: 授权成功"
        )
    else:
        await _answer(message, settings, 
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
        await _answer(message, settings, 
            "<b>命令用法</b>\n"
            "/unauthgroup &lt;群ID&gt;\n"
            "或在群内直接发送 /unauthgroup"
        )
        return

    removed = await deauthorize_group(session, group_id)
    if removed:
        await _answer(message, settings, 
            "<b>群组授权结果</b>\n"
            f"<b>群ID</b>: {group_id}\n"
            "<b>状态</b>: 已取消授权"
        )
    else:
        await _answer(message, settings, 
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
        await _answer(message, settings, "<b>已授权群组</b>\n当前为空。")
        return

    text, keyboard = _build_auth_group_list_page(rows, page=0)
    await _answer(
        message,
        settings,
        text,
        reply_markup=keyboard,
        disable_web_page_preview=True,
    )


@router.message(Command("authadmin"))
async def cmd_authadmin(message: Message, session: AsyncSession, settings: Settings) -> None:
    if not await ensure_super_admin(message, settings):
        return

    args = (message.text or "").partition(" ")[2].strip()
    group_id, user_id = _resolve_admin_binding(message, args)
    if group_id is None or user_id is None:
        await _answer(message, settings, 
            "<b>命令用法</b>\n"
            "1. 群内回复目标用户消息后发送 /authadmin\n"
            "2. /authadmin &lt;群ID&gt; &lt;用户ID&gt;\n"
            "3. 群内 /authadmin &lt;用户ID&gt;"
        )
        return

    created = await authorize_group_admin(session, group_id, user_id, role="admin")
    if created:
        await _answer(message, settings, 
            "<b>群管理授权结果</b>\n"
            f"<b>群ID</b>: {group_id}\n"
            f"<b>用户ID</b>: {user_id}\n"
            "<b>状态</b>: 授权成功"
        )
    else:
        await _answer(message, settings, 
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
        await _answer(message, settings, 
            "<b>命令用法</b>\n"
            "1. 群内回复目标用户消息后发送 /unauthadmin\n"
            "2. /unauthadmin &lt;群ID&gt; &lt;用户ID&gt;\n"
            "3. 群内 /unauthadmin &lt;用户ID&gt;"
        )
        return

    removed = await deauthorize_group_admin(session, group_id, user_id)
    if removed:
        await _answer(message, settings, 
            "<b>群管理授权结果</b>\n"
            f"<b>群ID</b>: {group_id}\n"
            f"<b>用户ID</b>: {user_id}\n"
            "<b>状态</b>: 已取消群管理权限"
        )
    else:
        await _answer(message, settings, 
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
        await _answer(message, settings, 
            "<b>命令用法</b>\n"
            "/adminlist &lt;群ID&gt;\n"
            "或在群内直接发送 /adminlist"
        )
        return

    rows = await list_group_admins(session, group_id)
    if not rows:
        await _answer(message, settings, 
            "<b>群管理授权列表</b>\n"
            f"<b>群ID</b>: {group_id}\n"
            "<b>结果</b>: 当前无已授权群管理"
        )
        return

    text, keyboard = _build_admin_list_page(rows, group_id=group_id, page=0)
    await _answer(
        message,
        settings,
        text,
        reply_markup=keyboard,
        disable_web_page_preview=True,
    )


@router.message(Command("addrule"))
async def cmd_addrule(message: Message, session: AsyncSession, settings: Settings) -> None:
    if not await ensure_group_authorized(message, session, settings):
        return
    if not await ensure_group_admin_permission(message, session, settings):
        return

    args = (message.text or "").partition(" ")[2].strip()
    if not args:
        await _answer(message, settings, 
            "<b>规则管理指令格式</b>\n"
            "用法: /addrule &lt;自然语言&gt;\n"
            "示例: /addrule 增加群规 禁止骂人"
        )
        return

    if args.lower() == "list":
        await _reply_rules(message, session, settings)
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
        await _answer(message, settings, "<b>规则解析失败</b>\n无法理解你的规则意图，请换个说法。")
        return

    action = str(data.get("action", "unknown")).strip().lower()

    if action == "add":
        rule_type = str(data.get("rule_type", "keyword")).strip().lower()
        pattern = str(data.get("pattern", "")).strip()
        hit_action = str(data.get("hit_action", "warn")).strip().lower()

        if rule_type not in ("keyword", "regex", "llm"):
            await _answer(message, settings, 
                "<b>规则添加失败</b>\n"
                "rule_type 必须是 keyword、regex 或 llm"
            )
            return
        if not pattern:
            await _answer(message, settings, "<b>规则添加失败</b>\npattern 不能为空")
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
        await _answer(message, settings, 
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
                await _answer(message, settings, "<b>规则删除失败</b>\nrule_id 必须是整数")
                return
            candidate = await session.get(ModerationRule, rid)
            if candidate and candidate.group_id == message.chat.id:
                rule = candidate
        else:
            pattern = str(data.get("pattern", "")).strip()
            rule_type = str(data.get("rule_type", "")).strip().lower()
            if not pattern:
                await _answer(message, settings, "<b>规则删除失败</b>\n删除时请提供 rule_id 或 pattern")
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
            await _answer(message, settings, "<b>规则删除失败</b>\n未找到对应规则")
            return

        rid = rule.id
        await session.delete(rule)
        await _answer(message, settings, 
            "<b>规则删除成功</b>\n"
            f"<b>规则编号</b>: #{rid}\n"
            f"<b>规则类型</b>: {_rule_type_label(rule.rule_type)}\n"
            f"<b>规则内容</b>: {html.escape(_truncate_text(rule.pattern or '', 160))}"
        )
        return

    if action == "list":
        await _reply_rules(message, session, settings)
        return

    await _answer(message, settings, "<b>规则解析失败</b>\n无法理解你的规则意图，请换个说法。")


@router.message(Command("rules"))
async def cmd_rules(message: Message, session: AsyncSession, settings: Settings) -> None:
    if not await ensure_group_authorized(message, session, settings):
        return
    if not await ensure_group_admin_permission(message, session, settings):
        return
    await _reply_rules(message, session, settings)


@router.callback_query(F.data.startswith("rul:"))
async def on_rule_list_paging(
    callback: CallbackQuery,
    settings: Settings,
    session: AsyncSession | None = None,
) -> None:
    if not callback.data:
        await callback.answer()
        return
    if session is None:
        await callback.answer("会话未就绪，请重新 /rules", show_alert=True)
        return
    if not await _callback_user_can_manage_rules(callback, session, settings):
        return

    msg = callback.message
    if not msg or not msg.chat:
        await callback.answer("消息已失效", show_alert=True)
        return

    parts = callback.data.split(":")
    if len(parts) != 2:
        await callback.answer("参数错误", show_alert=True)
        return
    page = _parse_int(parts[1], default=0)

    stmt = (
        select(ModerationRule)
        .where(ModerationRule.group_id == msg.chat.id)
        .order_by(ModerationRule.id)
    )
    result = await session.execute(stmt)
    rules = list(result.scalars().all())
    if not rules:
        await msg.edit_text("<b>群审核规则</b>\n当前没有规则，可使用 /addrule 添加。", reply_markup=None)
        await callback.answer("列表已空")
        return

    text, keyboard = _build_rule_list_page(rules, page=page)
    try:
        await msg.edit_text(text, reply_markup=keyboard, disable_web_page_preview=True)
    except TelegramBadRequest as exc:
        if "message is not modified" not in str(exc).lower():
            await callback.answer("列表刷新失败，请重试 /rules", show_alert=True)
            return
    except Exception:
        await callback.answer("列表刷新失败，请重试 /rules", show_alert=True)
        return
    await callback.answer()


@router.callback_query(F.data.startswith("rud:"))
async def on_rule_delete(
    callback: CallbackQuery,
    settings: Settings,
    session: AsyncSession | None = None,
) -> None:
    if not callback.data:
        await callback.answer()
        return
    if session is None:
        await callback.answer("会话未就绪，请重新 /rules", show_alert=True)
        return
    if not await _callback_user_can_manage_rules(callback, session, settings):
        return

    msg = callback.message
    if not msg or not msg.chat:
        await callback.answer("消息已失效", show_alert=True)
        return

    parts = callback.data.split(":")
    if len(parts) != 3:
        await callback.answer("参数错误", show_alert=True)
        return

    rid = _parse_int(parts[1], default=0)
    page_hint = _parse_int(parts[2], default=0)
    if rid <= 0:
        await callback.answer("参数错误", show_alert=True)
        return

    rule = await session.get(ModerationRule, rid)
    if not rule or rule.group_id != msg.chat.id:
        await callback.answer("规则不存在或已删除", show_alert=True)
        return

    pattern_label = _truncate_text(rule.pattern or "", 24) or f"#{rule.id}"
    await session.delete(rule)
    await session.flush()

    stmt = (
        select(ModerationRule)
        .where(ModerationRule.group_id == msg.chat.id)
        .order_by(ModerationRule.id)
    )
    result = await session.execute(stmt)
    rules = list(result.scalars().all())
    if not rules:
        await msg.edit_text("<b>群审核规则</b>\n当前没有规则，可使用 /addrule 添加。", reply_markup=None)
        await callback.answer(f"已删除: {pattern_label}")
        return

    total_pages = max(1, (len(rules) + _LIST_PAGE_SIZE - 1) // _LIST_PAGE_SIZE)
    page = min(max(page_hint, 0), total_pages - 1)
    text, keyboard = _build_rule_list_page(rules, page=page)
    try:
        await msg.edit_text(text, reply_markup=keyboard, disable_web_page_preview=True)
    except Exception:
        await callback.answer("删除成功，但列表刷新失败，请重试 /rules", show_alert=True)
        return
    await callback.answer(f"已删除: {pattern_label}")


@router.callback_query(F.data.startswith("atl:"))
async def on_authlist_paging(
    callback: CallbackQuery,
    settings: Settings,
    session: AsyncSession | None = None,
) -> None:
    if not callback.data:
        await callback.answer()
        return
    if session is None:
        await callback.answer("会话未就绪，请重新 /authlist", show_alert=True)
        return
    if not await _callback_user_is_super_admin(callback, settings):
        return

    msg = callback.message
    if not msg:
        await callback.answer("消息已失效", show_alert=True)
        return

    parts = callback.data.split(":")
    if len(parts) != 2:
        await callback.answer("参数错误", show_alert=True)
        return
    page = _parse_int(parts[1], default=0)

    rows = await list_authorized_groups(session)
    if not rows:
        await msg.edit_text("<b>已授权群组</b>\n当前为空。", reply_markup=None)
        await callback.answer("列表已空")
        return

    text, keyboard = _build_auth_group_list_page(rows, page=page)
    try:
        await msg.edit_text(text, reply_markup=keyboard, disable_web_page_preview=True)
    except TelegramBadRequest as exc:
        if "message is not modified" not in str(exc).lower():
            await callback.answer("列表刷新失败，请重试 /authlist", show_alert=True)
            return
    except Exception:
        await callback.answer("列表刷新失败，请重试 /authlist", show_alert=True)
        return
    await callback.answer()


@router.callback_query(F.data.startswith("adl:"))
async def on_adminlist_paging(
    callback: CallbackQuery,
    settings: Settings,
    session: AsyncSession | None = None,
) -> None:
    if not callback.data:
        await callback.answer()
        return
    if session is None:
        await callback.answer("会话未就绪，请重新 /adminlist", show_alert=True)
        return
    if not await _callback_user_is_super_admin(callback, settings):
        return

    msg = callback.message
    if not msg:
        await callback.answer("消息已失效", show_alert=True)
        return

    parts = callback.data.split(":")
    if len(parts) != 3:
        await callback.answer("参数错误", show_alert=True)
        return
    group_id = _parse_int(parts[1], default=0)
    page = _parse_int(parts[2], default=0)
    if group_id == 0:
        await callback.answer("参数错误", show_alert=True)
        return

    rows = await list_group_admins(session, group_id)
    if not rows:
        await msg.edit_text(
            "<b>群管理授权列表</b>\n"
            f"<b>群ID</b>: {group_id}\n"
            "<b>结果</b>: 当前无已授权群管理",
            reply_markup=None,
        )
        await callback.answer("列表已空")
        return

    text, keyboard = _build_admin_list_page(rows, group_id=group_id, page=page)
    try:
        await msg.edit_text(text, reply_markup=keyboard, disable_web_page_preview=True)
    except TelegramBadRequest as exc:
        if "message is not modified" not in str(exc).lower():
            await callback.answer("列表刷新失败，请重试 /adminlist", show_alert=True)
            return
    except Exception:
        await callback.answer("列表刷新失败，请重试 /adminlist", show_alert=True)
        return
    await callback.answer()


@router.callback_query(F.data.startswith("wpl:"))
async def on_warnings_paging(
    callback: CallbackQuery,
    settings: Settings,
    session: AsyncSession | None = None,
) -> None:
    if not callback.data:
        await callback.answer()
        return
    if session is None:
        await callback.answer("会话未就绪，请重新 /warnings", show_alert=True)
        return
    if not await _callback_user_can_manage_rules(callback, session, settings):
        return

    msg = callback.message
    if not msg or not msg.chat:
        await callback.answer("消息已失效", show_alert=True)
        return

    parts = callback.data.split(":")
    if len(parts) != 2:
        await callback.answer("参数错误", show_alert=True)
        return
    page = _parse_int(parts[1], default=0)

    stmt = (
        select(UserWarning)
        .where(
            UserWarning.group_id == msg.chat.id,
            UserWarning.count > 0,
        )
        .order_by(UserWarning.is_banned.desc(), UserWarning.count.desc(), UserWarning.user_id.asc())
    )
    result = await session.execute(stmt)
    rows = list(result.scalars().all())
    if not rows:
        await msg.edit_text(
            "<b>当前群组警告/封禁名单</b>\n"
            "<b>结果</b>: 暂无被警告或封禁用户",
            reply_markup=None,
        )
        await callback.answer("列表已空")
        return

    threshold = max(1, settings.moderation.warn_threshold)
    text, keyboard = _build_warning_list_page(rows, threshold=threshold, page=page)
    try:
        await msg.edit_text(text, reply_markup=keyboard, disable_web_page_preview=True)
    except TelegramBadRequest as exc:
        if "message is not modified" not in str(exc).lower():
            await callback.answer("列表刷新失败，请重试 /warnings", show_alert=True)
            return
    except Exception:
        await callback.answer("列表刷新失败，请重试 /warnings", show_alert=True)
        return
    await callback.answer()


@router.message(Command("mute"))
async def cmd_mute(message: Message, session: AsyncSession, settings: Settings) -> None:
    if not await ensure_group_authorized(message, session, settings):
        return
    if not await ensure_group_admin_permission(message, session, settings):
        return

    args = (message.text or "").partition(" ")[2].strip().lower()
    group_id = message.chat.id

    if args == "all":
        group_row = await _ensure_group_row(session, group_id, message.chat.title or "")
        settings_data = dict(group_row.settings or {})
        already_muted = bool(settings_data.get(_MUTE_ALL_REPLIES_KEY, False))
        if already_muted:
            await _answer(
                message,
                settings,
                "<b>回复静默设置</b>\n"
                "<b>范围</b>: 全群\n"
                "<b>状态</b>: 已是静默状态（仅做审核，不再回复）",
            )
            return

        settings_data[_MUTE_ALL_REPLIES_KEY] = True
        group_row.settings = settings_data
        await _answer(
            message,
            settings,
            "<b>回复静默设置</b>\n"
            "<b>范围</b>: 全群\n"
            "<b>状态</b>: 已开启（仅做审核，不再回复）",
        )
        return

    if args:
        await _answer(message, settings, _MUTE_USAGE)
        return

    reply = message.reply_to_message
    target = reply.from_user if reply else None
    if not target:
        await _answer(message, settings, _MUTE_USAGE)
        return

    if target.is_bot:
        await _answer(
            message,
            settings,
            "<b>回复静默设置</b>\n"
            "<b>结果</b>: 机器人账号无需设置静默",
        )
        return

    stmt = select(ReplyMute).where(
        ReplyMute.group_id == group_id,
        ReplyMute.user_id == target.id,
    )
    result = await session.execute(stmt)
    existing = result.scalar_one_or_none()
    if existing:
        await _answer(
            message,
            settings,
            "<b>回复静默设置</b>\n"
            f"<b>用户</b>: {_safe_user_label(target.id, target.full_name)}\n"
            "<b>状态</b>: 已在静默名单",
        )
        return

    session.add(
        ReplyMute(
            group_id=group_id,
            user_id=target.id,
            created_by=(message.from_user.id if message.from_user else 0),
        )
    )
    await _answer(
        message,
        settings,
        "<b>回复静默设置</b>\n"
        f"<b>用户</b>: {_safe_user_label(target.id, target.full_name)}\n"
        "<b>状态</b>: 已加入静默名单（仍参与审核）",
    )


@router.message(Command("unmute"))
async def cmd_unmute(message: Message, session: AsyncSession, settings: Settings) -> None:
    if not await ensure_group_authorized(message, session, settings):
        return
    if not await ensure_group_admin_permission(message, session, settings):
        return

    args = (message.text or "").partition(" ")[2].strip().lower()
    group_id = message.chat.id

    if args == "all":
        group_row = await _ensure_group_row(session, group_id, message.chat.title or "")
        settings_data = dict(group_row.settings or {})
        already_enabled = not bool(settings_data.get(_MUTE_ALL_REPLIES_KEY, False))
        if already_enabled:
            await _answer(
                message,
                settings,
                "<b>回复静默设置</b>\n"
                "<b>范围</b>: 全群\n"
                "<b>状态</b>: 已是正常回复状态",
            )
            return

        settings_data.pop(_MUTE_ALL_REPLIES_KEY, None)
        group_row.settings = settings_data
        await _answer(
            message,
            settings,
            "<b>回复静默设置</b>\n"
            "<b>范围</b>: 全群\n"
            "<b>状态</b>: 已关闭静默（恢复正常回复）",
        )
        return

    if args:
        await _answer(message, settings, _UNMUTE_USAGE)
        return

    reply = message.reply_to_message
    target = reply.from_user if reply else None
    if not target:
        await _answer(message, settings, _UNMUTE_USAGE)
        return

    if target.is_bot:
        await _answer(
            message,
            settings,
            "<b>回复静默设置</b>\n"
            "<b>结果</b>: 机器人账号无需解除静默",
        )
        return

    stmt = select(ReplyMute).where(
        ReplyMute.group_id == group_id,
        ReplyMute.user_id == target.id,
    )
    result = await session.execute(stmt)
    existing = result.scalar_one_or_none()
    if not existing:
        await _answer(
            message,
            settings,
            "<b>回复静默设置</b>\n"
            f"<b>用户</b>: {_safe_user_label(target.id, target.full_name)}\n"
            "<b>状态</b>: 当前不在静默名单",
        )
        return

    await session.delete(existing)
    await _answer(
        message,
        settings,
        "<b>回复静默设置</b>\n"
        f"<b>用户</b>: {_safe_user_label(target.id, target.full_name)}\n"
        "<b>状态</b>: 已移出静默名单（恢复回复）",
    )


@router.message(Command("proactive"))
async def cmd_proactive(message: Message, session: AsyncSession, settings: Settings) -> None:
    if not await ensure_group_authorized(message, session, settings):
        return
    if not await ensure_group_admin_permission(message, session, settings):
        return

    args = (message.text or "").partition(" ")[2].strip().lower()
    if not message.chat or message.chat.type not in ("group", "supergroup"):
        await _answer(message, settings, "<b>主动话题定时任务</b>\n请在目标群内使用该命令。")
        return

    group_row = await _ensure_group_row(session, message.chat.id, message.chat.title or "")
    group_settings = dict(group_row.settings or {})

    if args in {"", "status"}:
        await _answer(
            message,
            settings,
            get_cooldown_status_text(
                group_settings,
                default_enabled=settings.bot.proactive_default_enabled,
            ),
        )
        return

    if args in {"on", "enable"}:
        group_row.settings = set_cooldown_task_enabled(
            group_settings,
            enabled=True,
            config=settings.bot,
        )
        await _answer(
            message,
            settings,
            get_cooldown_status_text(
                group_row.settings,
                default_enabled=settings.bot.proactive_default_enabled,
            ),
        )
        return

    if args in {"off", "disable"}:
        group_row.settings = set_cooldown_task_enabled(
            group_settings,
            enabled=False,
            config=settings.bot,
        )
        await _answer(
            message,
            settings,
            get_cooldown_status_text(
                group_row.settings,
                default_enabled=settings.bot.proactive_default_enabled,
            ),
        )
        return

    await _answer(message, settings, _PROACTIVE_USAGE)


@router.message(Command("tts"))
async def cmd_tts(message: Message, session: AsyncSession, settings: Settings) -> None:
    if not await ensure_group_authorized(message, session, settings):
        return
    if not await ensure_group_admin_permission(message, session, settings):
        return

    args = (message.text or "").partition(" ")[2].strip().lower()
    if not message.chat or message.chat.type not in ("group", "supergroup"):
        await _answer(message, settings, "<b>TTS 设置</b>\n请在目标群内使用该命令。")
        return

    group_row = await _ensure_group_row(session, message.chat.id, message.chat.title or "")
    group_settings = dict(group_row.settings or {})
    tts_service = DoubaoTTSService(settings)

    if args in {"", "status"}:
        await _answer(
            message,
            settings,
            build_tts_status_text(
                group_id=message.chat.id,
                group_settings=group_settings,
                service_ready=tts_service.available,
            ),
        )
        return

    mode_map = {
        "on": TTS_MODE_ON,
        "enable": TTS_MODE_ON,
        "off": TTS_MODE_OFF,
        "disable": TTS_MODE_OFF,
        "always": TTS_MODE_ALWAYS,
    }
    target_mode = mode_map.get(args)
    if target_mode is None:
        await _answer(message, settings, _TTS_USAGE)
        return

    if target_mode in {TTS_MODE_ON, TTS_MODE_ALWAYS} and not tts_service.available:
        await _answer(
            message,
            settings,
            "<b>TTS 设置</b>\nDoubao TTS 尚未配置完成，请先补齐 .env 中的 DOUBAO_TTS_* 配置。",
        )
        return

    group_row.settings = set_tts_mode(group_settings, target_mode)
    await session.flush()
    await _answer(
        message,
        settings,
        build_tts_status_text(
            group_id=message.chat.id,
            group_settings=group_row.settings,
            service_ready=tts_service.available,
        ),
    )


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
        await _answer(message, settings, 
            "<b>当前群组警告/封禁名单</b>\n"
            "<b>结果</b>: 暂无被警告或封禁用户"
        )
        return

    threshold = max(1, settings.moderation.warn_threshold)
    text, keyboard = _build_warning_list_page(list(rows), threshold=threshold, page=0)
    await _answer(
        message,
        settings,
        text,
        reply_markup=keyboard,
        disable_web_page_preview=True,
    )


@router.message(Command("aiexempt"))
async def cmd_aiexempt(message: Message, session: AsyncSession, settings: Settings) -> None:
    if not await ensure_group_authorized(message, session, settings):
        return
    if not await ensure_group_admin_permission(message, session, settings):
        return

    reply = message.reply_to_message
    target = reply.from_user if reply else None
    if not target:
        await _answer(message, settings, 
            "<b>命令用法</b>\n"
            "请先回复目标用户的一条消息，再执行 /aiexempt"
        )
        return
    if target.is_bot:
        await _answer(message, settings, "<b>AI 审查豁免</b>\n机器人账号无需设置豁免。")
        return

    stmt = select(ModerationExemption).where(
        ModerationExemption.group_id == message.chat.id,
        ModerationExemption.user_id == target.id,
    )
    result = await session.execute(stmt)
    existing = result.scalar_one_or_none()
    if existing:
        await _answer(message, settings, 
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
    await _answer(message, settings, 
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
        await _answer(message, settings, 
            "<b>命令用法</b>\n"
            "请先回复目标用户的一条消息，再执行 /unaiexempt"
        )
        return
    if target.is_bot:
        await _answer(message, settings, "<b>AI 审查豁免</b>\n机器人账号不在豁免名单中。")
        return

    stmt = select(ModerationExemption).where(
        ModerationExemption.group_id == message.chat.id,
        ModerationExemption.user_id == target.id,
    )
    result = await session.execute(stmt)
    existing = result.scalar_one_or_none()
    if not existing:
        await _answer(message, settings, 
            "<b>AI 审查豁免</b>\n"
            f"<b>用户</b>: {_safe_user_label(target.id, target.full_name)}\n"
            "<b>状态</b>: 当前不在豁免名单"
        )
        return

    await session.delete(existing)
    await _answer(message, settings, 
        "<b>AI 审查豁免</b>\n"
        f"<b>用户</b>: {_safe_user_label(target.id, target.full_name)}\n"
        "<b>状态</b>: 已取消豁免"
    )


