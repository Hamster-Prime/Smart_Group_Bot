from __future__ import annotations

import asyncio
import html
import logging
import re
import time
import weakref
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Literal

from aiogram import F, Router
from aiogram.exceptions import (
    TelegramBadRequest,
    TelegramForbiddenError,
    TelegramNetworkError,
    TelegramRetryAfter,
)
from aiogram.filters import Command
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from sqlalchemy import delete, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from bot.config import Settings
from bot.db.models import (
    Group,
    ModerationExemption,
    ModerationRule,
    ReplyMute,
    UserWarning,
)
from bot.services.at_reply import build_at_reply_status_text, set_at_reply_enabled
from bot.services.bot_screening import remove_bot_whitelist
from bot.services.ban_audit import record_ban_event
from bot.services.authz import (
    authorize_group,
    authorize_group_admin,
    deauthorize_group,
    deauthorize_group_admin,
    ensure_group_admin_permission,
    ensure_group_authorized,
    ensure_super_admin,
    is_group_admin_authorized,
    is_group_authorized,
    is_super_admin_user_id,
    list_authorized_groups,
    list_group_admins,
)
from bot.services.join_screening import (
    add_global_ban,
    is_globally_banned,
    list_global_bans,
    remove_global_ban,
)
from bot.services.join_verification import (
    activate_manual_unban_recoveries,
    activate_manual_unban_recovery,
    UnbanRecovery,
    ban_member,
    close_private_challenge_messages,
    complete_leased_join_verification,
    delete_verification_prompts,
    get_join_verification,
    lease_join_verification_for_unban,
    lease_join_verifications_for_user_unban,
    reconcile_moderation_ban_after_lost_lease,
    release_moderation_restriction_after_exemption,
    restore_member_permissions,
    unban_member,
    verification_release_blocked_by_ban,
    verification_restriction_required,
)
from bot.services.privileged_tasks import submit_privileged_task
from bot.services.request_priority import privileged_request_scope
from bot.services.update_delivery import (
    clear_privileged_operator_group,
    mark_privileged_operator,
    unmark_privileged_operator,
)
from bot.services.callback_auth import is_group_admin_or_higher
from bot.services.speech_style import get_style_state, set_style_target
from bot.services.doubao_tts import (
    DoubaoTTSService,
    TTS_MODE_ALWAYS,
    TTS_MODE_OFF,
    TTS_MODE_ON,
    build_tts_status_text,
    set_tts_mode,
)
from bot.services.llm import LLMService
from bot.services.proactive import (
    get_cooldown_status_text,
    set_cooldown_task_enabled,
)
from bot.services.raid_guard import (
    RAID_GUARD_DISABLE_CALLBACK_DATA,
    get_raid_guard_service,
    normalize_manual_lockdown_minutes,
)
from bot.services.group_settings import acquire_group_settings_write_intent
from bot.services.message_templates import (
    render_action_notice,
    render_data_brief,
    render_expandable_blockquote,
    render_progress_notice,
)
from bot.services.skills import SkillService
from bot.utils.telegram import (
    answer_with_auto_delete,
    configured_auto_delete_seconds,
    is_group,
    preserve_delete_button,
)

router = Router()
log = logging.getLogger(__name__)

_MUTE_ALL_REPLIES_KEY = "mute_all_replies"
_LIST_PAGE_SIZE = 5
_LEGACY_ACTION_RESPONSE_RE = re.compile(
    r"^\s*<b>(?P<title>[^<>\n]+)</b>(?:\n+)?(?P<body>[\s\S]*?)\s*$"
)
_TaskResultStatus = Literal["completed", "failed", "rejected", "duplicate"]
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
    "0. /tts（查看当前状态）\n"
    "1. /tts enable\n"
    "2. /tts disable\n"
    "3. /tts always\n\n"
    "请最高管理员在目标群内使用。"
)
_AT_REPLY_USAGE = (
    "<b>命令用法</b>\n"
    "0. /atreply（查看当前状态）\n"
    "1. /atreply enable\n"
    "2. /atreply disable\n\n"
    "请最高管理员在目标群内使用。"
)
_BAN_USAGE = (
    "<b>命令用法</b>\n"
    "1. 回复目标用户消息后发送 /ban [原因]\n"
    "2. /ban &lt;用户ID&gt; [原因]\n\n"
    "群管理员默认只在当前群封禁；最高管理员会收到「仅本群 / 全局」选择按钮。\n"
    "回复使用时，封禁成功后会同时删除被回复消息。"
)
_SPAM_USAGE = (
    "<b>命令用法</b>\n"
    "1. 回复垃圾消息后发送 /spam [原因]\n"
    "2. /spam &lt;用户ID&gt; [原因]\n\n"
    "此命令会封禁目标，并将其加入全局封禁名单。"
)
_UNBAN_USAGE = (
    "<b>命令用法</b>\n"
    "1. 回复目标用户消息后发送 /unban\n"
    "2. /unban &lt;用户ID&gt;\n\n"
    "群管理员默认只解除当前群封禁；最高管理员会收到「仅本群 / 全局」选择按钮。"
)
_CLEAR_WARNINGS_USAGE = (
    "<b>命令用法</b>\n"
    "1. 回复目标用户消息后发送 /clearwarnings\n"
    "2. /clearwarnings &lt;用户ID&gt;\n\n"
    "此命令只清空累计违规次数，不执行解封。"
)
_MIMIC_USAGE = (
    "<b>命令用法</b>\n"
    "1. 回复目标用户消息后发送 /mimic（开始学习 TA 的说话风格）\n"
    "2. /mimic status（查看当前状态）\n"
    "3. /mimic off（停止学习并清除画像）\n\n"
    "bot 会持续收集目标用户的发言，每约 50 条蒸馏刷新一次说话风格画像并应用到回复，总量上限 1000 条。"
)


def _render_action_response(text: str) -> str:
    """Render remaining synchronous command replies in the shared layout.

    Most legacy administration results already expose a bold heading followed
    by trusted HTML details.  Translating that shape here keeps ordinary
    successes, denials, and usage messages visually consistent while letting
    data briefs and task-progress messages opt out by carrying a blockquote.
    """
    rendered = str(text or "").strip()
    if not rendered or "<blockquote" in rendered.lower():
        return rendered
    match = _LEGACY_ACTION_RESPONSE_RE.match(rendered)
    if match:
        title = html.unescape(match.group("title").strip()) or "操作结果"
        return render_action_notice(title, action=match.group("body").strip())
    return render_action_notice("操作结果", action=rendered)


def _render_task_result(
    text: str,
    *,
    status: _TaskResultStatus,
    default_title: str = "后台任务",
) -> str:
    """Convert an explicitly classified privileged-job result into a terminal flow."""
    rendered = str(text or "").strip()
    match = (
        None
        if "<blockquote" in rendered.lower()
        else _LEGACY_ACTION_RESPONSE_RE.match(rendered)
    )
    if match:
        title = html.unescape(match.group("title").strip()) or default_title
        body = match.group("body").strip()
    else:
        title = default_title
        body = rendered

    state_fields: dict[str, object | None]
    if status == "rejected":
        state_fields = {
            "completed": None,
            "current": "未能进入后台队列",
            "next_step": "请按下方说明重试",
        }
    elif status == "duplicate":
        state_fields = {
            "completed": "已受理请求",
            "current": "已有相同任务正在执行",
            "next_step": "完成后会更新原任务消息",
        }
    elif status == "failed":
        state_fields = {
            "completed": "已受理请求",
            "current": "后台处理未完成",
            "next_step": "请按下方说明重试",
        }
    elif status == "completed":
        state_fields = {
            "completed": ["已受理请求", "已结束后台处理"],
            "current": "处理结果已返回",
            "next_step": "请查看下方处理结果",
        }
    else:
        raise ValueError(f"unsupported privileged task result status: {status}")

    structured_body = bool(body and "<blockquote" in body.lower())
    notice = render_progress_notice(
        title,
        completed=state_fields["completed"],
        current=state_fields["current"],
        next_step=state_fields["next_step"],
        action=None if structured_body else body,
    )
    return f"{notice}\n\n{body}" if structured_body else notice


def _render_ban_result(
    title: str,
    *,
    errors: list[str] | tuple[str, ...] = (),
) -> str:
    """Render concise ban/unban outcomes with optional collapsed errors."""

    parts = [f"<b>{html.escape(str(title))}</b>"]
    error_lines = [
        html.escape(str(error).strip())
        for error in errors
        if str(error).strip()
    ]
    if error_lines:
        parts.append(
            render_expandable_blockquote(
                ["<b>错误详情</b>", *error_lines]
            )
        )
    return "\n\n".join(parts)


async def _answer(
    message: Message,
    settings: Settings,
    text: str,
    **kwargs: object,
) -> None:
    await answer_with_auto_delete(
        message,
        _render_action_response(text),
        auto_delete_seconds=configured_auto_delete_seconds(settings, "management"),
        **kwargs,
    )


async def _commit_settings(session: AsyncSession) -> None:
    """Make runtime setting changes visible before awaiting Telegram replies."""
    commit = getattr(session, "commit", None)
    if commit is not None:
        await commit()
        return
    # Lightweight unit-test doubles may only expose flush; production always
    # receives an AsyncSession and therefore takes the commit path above.
    flush = getattr(session, "flush", None)
    if flush is not None:
        await flush()


def _build_skill_service(settings: Settings) -> SkillService:
    llm = LLMService(
        settings.bot.main_model,
        settings.bot.decision_model,
        settings.bot.compress_model,
        moderation=settings.bot.moderation_model,
        vision=settings.bot.vision_model,
        embed=settings.bot.embed_model,
        max_context_tokens=settings.bot.max_context_tokens,
    )
    sticker_pool = [
        x.strip()
        for x in (settings.skill_sticker_file_ids or "").split(",")
        if x and x.strip()
    ]
    return SkillService(llm, settings=settings, default_sticker_file_ids=sticker_pool)


async def _ensure_group_row(session: AsyncSession, group_id: int, title: str) -> Group:
    # Authorization checks opened a read snapshot. End it before upgrading to
    # a write transaction, otherwise SQLite WAL can raise BUSY_SNAPSHOT when a
    # concurrent writer committed between the two statements.
    if session.in_transaction():
        await session.commit()
    await acquire_group_settings_write_intent(session, group_id)
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
        "challenge": "真人质询",
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

    lines: list[str] = []
    for idx, row in enumerate(rows[start:end], start=start + 1):
        reachability = "可用" if bool(getattr(row, "bot_present", True)) else "Bot 不可达/暂停"
        lines.extend(
            [
                f"<b>{idx}.</b> <code>{row.group_id}</code>",
                f"授权人　<code>{row.authorized_by or 0}</code>　状态　{reachability}",
                "",
            ]
        )

    keyboard_rows: list[list[InlineKeyboardButton]] = []
    nav_row: list[InlineKeyboardButton] = []
    if page > 0:
        nav_row.append(
            InlineKeyboardButton(
                text="上一页",
                callback_data=f"atl:{page - 1}",
            )
        )
    if page < total_pages - 1:
        nav_row.append(
            InlineKeyboardButton(
                text="下一页",
                callback_data=f"atl:{page + 1}",
            )
        )
    if nav_row:
        keyboard_rows.append(nav_row)
    keyboard = InlineKeyboardMarkup(inline_keyboard=keyboard_rows) if keyboard_rows else None
    return (
        render_data_brief(
            "已授权群组",
            metadata={
                "总数": f"<code>{total}</code> 个",
                "页码": f"<code>{page + 1} / {total_pages}</code>",
            },
            items=lines,
        ),
        keyboard,
    )


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

    lines: list[str] = []
    for idx, row in enumerate(rows[start:end], start=start + 1):
        lines.extend(
            [
                f"<b>{idx}.</b> <code>{row.user_id}</code>",
                f"角色　{html.escape(row.role)}",
                "",
            ]
        )

    keyboard_rows: list[list[InlineKeyboardButton]] = []
    nav_row: list[InlineKeyboardButton] = []
    if page > 0:
        nav_row.append(
            InlineKeyboardButton(
                text="上一页",
                callback_data=f"adl:{group_id}:{page - 1}",
            )
        )
    if page < total_pages - 1:
        nav_row.append(
            InlineKeyboardButton(
                text="下一页",
                callback_data=f"adl:{group_id}:{page + 1}",
            )
        )
    if nav_row:
        keyboard_rows.append(nav_row)
    keyboard = InlineKeyboardMarkup(inline_keyboard=keyboard_rows) if keyboard_rows else None
    return (
        render_data_brief(
            "群管理授权列表",
            metadata={
                "群组": f"<code>{group_id}</code>",
                "总数": f"<code>{total}</code> 人",
                "页码": f"<code>{page + 1} / {total_pages}</code>",
            },
            items=lines,
        ),
        keyboard,
    )


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

    lines: list[str] = []
    for idx, row in enumerate(rows[start:end], start=start + 1):
        status = "已封禁" if row.is_banned else "警告中"
        lines.extend(
            [
                f"<b>{idx}.</b> <code>{row.user_id}</code>",
                f"警告　<code>{row.count} / {threshold}</code>　状态　{status}",
                "",
            ]
        )

    keyboard_rows: list[list[InlineKeyboardButton]] = []
    nav_row: list[InlineKeyboardButton] = []
    if page > 0:
        nav_row.append(
            InlineKeyboardButton(
                text="上一页",
                callback_data=f"wpl:{page - 1}",
            )
        )
    if page < total_pages - 1:
        nav_row.append(
            InlineKeyboardButton(
                text="下一页",
                callback_data=f"wpl:{page + 1}",
            )
        )
    if nav_row:
        keyboard_rows.append(nav_row)
    keyboard = InlineKeyboardMarkup(inline_keyboard=keyboard_rows) if keyboard_rows else None
    return (
        render_data_brief(
            "当前群组警告名单",
            metadata={
                "总数": f"<code>{total}</code> 人",
                "已封禁": f"<code>{banned_count}</code> 人",
                "警告中": f"<code>{total - banned_count}</code> 人",
                "页码": f"<code>{page + 1} / {total_pages}</code>",
            },
            items=lines,
        ),
        keyboard,
    )


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

    lines: list[str] = []
    keyboard_rows: list[list[InlineKeyboardButton]] = []
    for idx, rule in enumerate(rules[start:end], start=start + 1):
        status = "启用" if rule.enabled else "关闭"
        pattern_preview = html.escape(_truncate_text(rule.pattern or "", 120))
        lines.extend(
            [
                f"<b>{idx}.</b> <code>#{rule.id}</code>　{_rule_type_label(rule.rule_type)} · {_action_label(rule.action)} · {status}",
                pattern_preview,
                "",
            ]
        )
        keyboard_rows.append(
            [
                InlineKeyboardButton(
                    text=f"删除规则 #{rule.id}",
                    callback_data=f"rud:{rule.id}:{page}",
                )
            ]
        )

    nav_row: list[InlineKeyboardButton] = []
    if page > 0:
        nav_row.append(
            InlineKeyboardButton(
                text="上一页",
                callback_data=f"rul:{page - 1}",
            )
        )
    if page < total_pages - 1:
        nav_row.append(
            InlineKeyboardButton(
                text="下一页",
                callback_data=f"rul:{page + 1}",
            )
        )
    if nav_row:
        keyboard_rows.append(nav_row)

    return (
        render_data_brief(
            "群审核规则",
            metadata={
                "总数": f"<code>{total}</code> 条",
                "页码": f"<code>{page + 1} / {total_pages}</code>",
            },
            items=lines,
            footer="使用下方按钮删除对应规则。",
        ),
        InlineKeyboardMarkup(inline_keyboard=keyboard_rows),
    )


async def _callback_user_can_manage_rules(
    callback: CallbackQuery,
    session: AsyncSession,
    settings: Settings,
) -> bool:
    msg = callback.message
    if not msg or not msg.chat or msg.chat.type not in ("group", "supergroup"):
        await callback.answer("消息已失效", show_alert=True)
        return False
    authorized = await is_group_authorized(session, int(msg.chat.id))
    await session.commit()
    if not authorized:
        await callback.answer("当前群组未授权", show_alert=True)
        return False

    user = callback.from_user
    if user and is_super_admin_user_id(user.id, settings):
        return True
    if not user:
        await callback.answer("无法识别操作者", show_alert=True)
        return False
    locally_authorized = await is_group_admin_authorized(
        session,
        msg.chat.id,
        user.id,
    )
    # The denial path answers through Telegram, and the success path immediately
    # moves on to more callback work. End this authorization snapshot in both
    # cases so it never remains checked out across a Bot API request.
    await session.commit()
    if locally_authorized:
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
    rules = list(result.scalars().all())
    # Do not retain the list query's connection while Telegram sends the page.
    await session.commit()

    if not rules:
        await _answer(
            message,
            settings,
            render_data_brief(
                "群审核规则",
                empty="当前没有规则。",
                footer="可使用 <code>/addrule</code> 添加规则。",
            ),
        )
        return

    text, keyboard = _build_rule_list_page(rules, page=0)
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
    await session.commit()
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
    await session.commit()
    clear_privileged_operator_group(group_id)
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

    rows = await list_authorized_groups(session, include_inactive=True)
    await session.commit()
    if not rows:
        await _answer(
            message,
            settings,
            render_data_brief("已授权群组", empty="当前没有已授权群组。"),
        )
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
    if not await ensure_group_authorized(message, session, settings):
        return
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

    if not await is_group_authorized(session, group_id):
        await session.commit()
        await _answer(
            message,
            settings,
            "<b>群管理授权失败</b>\n"
            f"<b>群ID</b>: {group_id}\n"
            "<b>原因</b>: 目标群组尚未授权",
        )
        return

    created = await authorize_group_admin(session, group_id, user_id, role="admin")
    if not created and not await is_group_admin_authorized(session, group_id, user_id):
        await session.commit()
        await _answer(
            message,
            settings,
            "<b>群管理授权失败</b>\n"
            f"<b>群ID</b>: {group_id}\n"
            "<b>原因</b>: 目标群组当前未授权",
        )
        return
    try:
        await session.commit()
    except IntegrityError:
        await session.rollback()
        await _answer(
            message,
            settings,
            "<b>群管理授权失败</b>\n"
            f"<b>群ID</b>: {group_id}\n"
            "<b>原因</b>: 群授权状态刚刚发生变化，请重试",
        )
        return
    mark_privileged_operator(user_id, group_id=group_id)
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
    if not await ensure_group_authorized(message, session, settings):
        return
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
    await session.commit()
    unmark_privileged_operator(user_id, group_id=group_id)
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
    if not await ensure_group_authorized(message, session, settings):
        return
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
    await session.commit()
    if not rows:
        await _answer(
            message,
            settings,
            render_data_brief(
                "群管理授权列表",
                metadata={"群组": f"<code>{group_id}</code>"},
                empty="当前没有已授权群管理。",
            ),
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


def _resolve_ban_target(message: Message, args: str) -> tuple[int | None, str]:
    """Returns (user_id, reason). Target from reply or first numeric arg."""
    arg = (args or "").strip()
    reply = message.reply_to_message
    if reply and reply.from_user:
        return reply.from_user.id, arg

    if not arg:
        return None, ""
    first, _, rest = arg.partition(" ")
    try:
        return int(first), rest.strip()
    except ValueError:
        return None, ""


def _replied_message_id(message: Message) -> int:
    reply = getattr(message, "reply_to_message", None)
    return max(0, int(getattr(reply, "message_id", 0) or 0))


async def _delete_ban_target_message(
    bot: object,
    *,
    group_id: int,
    message_id: int,
) -> bool:
    """Best-effort removal of the message that supplied a ban target."""

    message_id = int(message_id or 0)
    if message_id <= 0:
        return False
    try:
        async with asyncio.timeout(5.0):
            deleted = await bot.delete_message(
                chat_id=int(group_id),
                message_id=message_id,
            )
        return deleted is not False
    except asyncio.CancelledError:
        raise
    except Exception:
        log.debug(
            "ban target message delete failed | group=%s message=%s",
            group_id,
            message_id,
            exc_info=True,
        )
        return False


async def _authorized_group_ids(
    session: AsyncSession,
    *,
    include_inactive: bool = False,
) -> list[int]:
    rows = await list_authorized_groups(
        session,
        include_inactive=include_inactive,
    )
    return [int(row.group_id) for row in rows]


async def _clear_user_warning(
    session: AsyncSession,
    group_id: int,
    user_id: int,
    *,
    preserve_ban: bool = True,
) -> tuple[int, bool]:
    stmt = select(UserWarning).where(
        UserWarning.group_id == group_id,
        UserWarning.user_id == user_id,
    )
    result = await session.execute(stmt)
    warning = result.scalar_one_or_none()
    if warning is None:
        return 0, False

    previous_count = max(0, int(warning.count or 0))
    was_banned = bool(warning.is_banned)
    if was_banned and preserve_ban:
        # Preserve the local ban policy; /clearwarnings is explicitly not an
        # unban operation.  Dropping the row used to make a later Telegram-side
        # unban silently bypass the bot's local state.
        warning.count = 0
    else:
        await session.delete(warning)
    return previous_count, was_banned


_BAN_SCOPE_CALLBACK_PREFIX = "bsc"
_BAN_SCOPE_TTL_SECONDS = 15 * 60


@dataclass(slots=True)
class _BanScopeRequest:
    action: str
    target_id: int
    reason: str
    created_at: float
    target_message_id: int = 0
    claimed_scope: str = ""


_BAN_SCOPE_REQUESTS: dict[tuple[int, int], _BanScopeRequest] = {}
_BAN_SCOPE_LOCK = asyncio.Lock()
_BAN_SCOPE_MAX_REQUESTS = 512
_MANUAL_BAN_LOCKS: weakref.WeakValueDictionary[
    tuple[int, int], asyncio.Lock
] = weakref.WeakValueDictionary()
_PRIVILEGED_GROUP_CONCURRENCY = 4
_PRIVILEGED_GROUP_DEADLINE_SECONDS = 45.0
_PRIVILEGED_JOB_DEADLINE_SECONDS = 300.0


@dataclass(frozen=True, slots=True)
class _GroupActionOutcome:
    group_id: int
    succeeded: bool
    category: str = ""


def _telegram_failure_category(exc: BaseException) -> str:
    if isinstance(exc, TelegramForbiddenError):
        return "forbidden"
    if isinstance(exc, TelegramBadRequest):
        return "bad_request"
    if isinstance(exc, TelegramRetryAfter):
        return "rate_limited"
    if isinstance(exc, (TelegramNetworkError, asyncio.TimeoutError)):
        return "transient"
    return "unexpected"


async def _run_group_actions_bounded(
    group_ids: list[int],
    operation: Callable[[int], Awaitable[bool]],
    *,
    concurrency: int = _PRIVILEGED_GROUP_CONCURRENCY,
    deadline_seconds: float = _PRIVILEGED_GROUP_DEADLINE_SECONDS,
) -> list[_GroupActionOutcome]:
    """Run idempotent Telegram group operations with bounded concurrency."""

    semaphore = asyncio.Semaphore(max(1, int(concurrency)))

    async def run_one(group_id: int) -> _GroupActionOutcome:
        async with semaphore:
            try:
                async with asyncio.timeout(max(0.1, float(deadline_seconds))):
                    succeeded = bool(await operation(int(group_id)))
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                category = _telegram_failure_category(exc)
                log.info(
                    "privileged group operation failed | group=%s category=%s error=%s",
                    group_id,
                    category,
                    exc,
                )
                return _GroupActionOutcome(int(group_id), False, category)
            return _GroupActionOutcome(
                int(group_id),
                succeeded,
                "" if succeeded else "unconfirmed",
            )

    return list(await asyncio.gather(*(run_one(group_id) for group_id in group_ids)))


def _failure_summary(outcomes: list[_GroupActionOutcome]) -> str:
    counts: dict[str, int] = {}
    for outcome in outcomes:
        if outcome.succeeded:
            continue
        category = outcome.category or "unknown"
        counts[category] = counts.get(category, 0) + 1
    if not counts:
        return ""
    labels = {
        "forbidden": "权限不足",
        "bad_request": "群状态不允许",
        "rate_limited": "限流",
        "transient": "网络超时",
        "unconfirmed": "结果未确认",
        "unexpected": "其他错误",
        "unknown": "未知错误",
    }
    return "、".join(
        f"{labels.get(category, category)} {count}"
        for category, count in sorted(counts.items())
    )


async def _publish_privileged_text(message: Message, text: str) -> None:
    """Edit a task message in place, with a best-effort reply fallback."""
    try:
        await message.edit_text(text, parse_mode="HTML", reply_markup=None)
        return
    except Exception:
        log.debug("privileged result edit failed", exc_info=True)
    try:
        await message.answer(text, parse_mode="HTML")
    except Exception:
        log.exception("privileged result delivery failed")


async def _publish_privileged_result(
    message: Message,
    text: str,
    *,
    status: _TaskResultStatus,
    default_title: str = "后台任务",
    compact: bool = False,
) -> None:
    rendered = (
        str(text or "").strip()
        if compact
        else _render_task_result(text, status=status, default_title=default_title)
    )
    if not rendered:
        rendered = f"<b>{html.escape(default_title)}</b>"
    await _publish_privileged_text(
        message,
        rendered,
    )


async def _publish_privileged_progress(
    message: Message,
    *,
    title: str,
    current: str,
    next_step: str,
    completed: object | None = "已受理请求",
    context: object | None = None,
    action: object | None = None,
    details: object | None = None,
) -> None:
    """Keep long-running administration work in its flow-oriented state."""
    await _publish_privileged_text(
        message,
        render_progress_notice(
            title,
            completed=completed,
            current=current,
            next_step=next_step,
            context=context,
            action=action,
            details=details,
        ),
    )


async def _ack_callback_fast(
    callback: CallbackQuery,
    text: str,
    *,
    show_alert: bool = False,
) -> None:
    """Bound callback acknowledgement so a bad HTTP session cannot pin a worker."""

    async def _answer() -> None:
        # CallbackQuery.answer() returns a TelegramMethod (awaitable, not a
        # coroutine); create_task requires a genuine coroutine.
        await callback.answer(text, show_alert=show_alert)

    with privileged_request_scope():
        task = asyncio.create_task(
            _answer(),
            name="privileged-callback-ack",
        )
    done, _pending = await asyncio.wait({task}, timeout=2.0)
    if task in done:
        try:
            await task
        except Exception:
            log.debug("privileged callback acknowledgement failed", exc_info=True)
        return
    task.cancel()

    def consume(done: asyncio.Task[object]) -> None:
        if done.cancelled():
            return
        try:
            done.exception()
        except (asyncio.CancelledError, Exception):
            pass

    task.add_done_callback(consume)


async def _commit_if_supported(session: object) -> None:
    commit = getattr(session, "commit", None)
    if callable(commit):
        await commit()


async def _mark_group_banned_after_telegram(
    session: AsyncSession,
    *,
    group_id: int,
    target_id: int,
) -> None:
    """Set the local policy without overwriting a concurrent warning count."""
    updated = await session.execute(
        update(UserWarning)
        .where(
            UserWarning.group_id == int(group_id),
            UserWarning.user_id == int(target_id),
        )
        .values(is_banned=True)
    )
    if int(updated.rowcount or 0) == 1:
        return
    try:
        async with session.begin_nested():
            session.add(
                UserWarning(
                    group_id=int(group_id),
                    user_id=int(target_id),
                    count=0,
                    is_banned=True,
                )
            )
            await session.flush()
        return
    except IntegrityError:
        # Another moderation path inserted the row after our UPDATE. Change
        # only the ban flag; its newly written warning count remains intact.
        updated = await session.execute(
            update(UserWarning)
            .where(
                UserWarning.group_id == int(group_id),
                UserWarning.user_id == int(target_id),
            )
            .values(is_banned=True)
        )
        if int(updated.rowcount or 0) != 1:
            raise


async def _ensure_ban_command_admin(
    message: Message,
    session: AsyncSession,
    settings: Settings,
) -> bool:
    if not is_group(message):
        await _answer(
            message,
            settings,
            _render_ban_result(
                "封禁管理未执行",
                errors=["该命令仅可在群内使用。"],
            ),
        )
        return False
    user = message.from_user
    if user is None:
        await _answer(
            message,
            settings,
            _render_ban_result(
                "封禁管理未执行",
                errors=["无法识别操作者。"],
            ),
        )
        return False
    try:
        with privileged_request_scope():
            async with asyncio.timeout(4.0):
                telegram_admin = await is_group_admin_or_higher(
                    bot=message.bot,
                    session=session,
                    settings=settings,
                    group_id=int(message.chat.id),
                    user_id=int(user.id),
                )
    except Exception:
        telegram_admin = False
        log.warning(
            "ban command Telegram admin lookup failed | group=%s user=%s",
            int(message.chat.id),
            int(user.id),
            exc_info=True,
        )
    if telegram_admin:
        return True
    await _answer(
        message,
        settings,
        _render_ban_result(
            "封禁管理未执行",
            errors=["仅本群管理员可使用该命令。"],
        ),
    )
    return False


async def _queued_operator_can_manage_members(
    *,
    bot: Any,
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    group_id: int,
    user_id: int,
) -> bool:
    """Revalidate queued authority immediately before a privileged side effect."""

    with privileged_request_scope():
        async with session_factory() as auth_session:
            if not await is_group_authorized(auth_session, int(group_id)):
                await auth_session.commit()
                return False
            try:
                async with asyncio.timeout(4.0):
                    return await is_group_admin_or_higher(
                        bot=bot,
                        session=auth_session,
                        settings=settings,
                        group_id=int(group_id),
                        user_id=int(user_id),
                    )
            except Exception:
                await auth_session.rollback()
                log.warning(
                    "queued privileged operator revalidation failed | group=%s user=%s",
                    group_id,
                    user_id,
                    exc_info=True,
                )
                return False


async def _target_is_group_admin(message: Message, target_id: int) -> bool | None:
    try:
        async with asyncio.timeout(10.0):
            member = await message.bot.get_chat_member(
                int(message.chat.id),
                int(target_id),
            )
    except Exception:
        log.exception(
            "cannot verify target Telegram admin status | group=%s user=%s",
            int(message.chat.id),
            int(target_id),
        )
        return None
    status = str(getattr(member, "status", "") or "").lower()
    return status in {"administrator", "creator", "chatmemberstatus.administrator", "chatmemberstatus.creator"}


async def _ban_target_rejection(
    message: Message,
    settings: Settings,
    target_id: int,
) -> str:
    if is_super_admin_user_id(target_id, settings):
        return "不能封禁最高管理员。"
    target_admin = await _target_is_group_admin(message, target_id)
    if target_admin is None:
        return "暂时无法确认目标的管理员身份；为避免误封，请稍后重试。"
    if target_admin:
        return "不能直接封禁本群管理员，请先在 Telegram 中撤销其管理员身份。"
    return ""


async def _perform_group_ban(
    message: Message,
    session: AsyncSession,
    settings: Settings,
    *,
    target_id: int,
    reason: str,
    operator_id: int = 0,
    operator_display: str = "",
    target_message_id: int = 0,
) -> str:
    # Local and global policy changes for the same user must serialize. Group-
    # scoped keys allowed a local ban and global unban to race in different
    # privileged lanes and overwrite each other's recovery generation.
    key = (0, int(target_id))
    lock = _MANUAL_BAN_LOCKS.setdefault(key, asyncio.Lock())
    async with lock:
        return await _perform_group_ban_locked(
            message,
            session,
            settings,
            target_id=target_id,
            reason=reason,
            operator_id=operator_id,
            operator_display=operator_display,
            target_message_id=target_message_id,
        )


async def _perform_group_ban_locked(
    message: Message,
    session: AsyncSession,
    settings: Settings,
    *,
    target_id: int,
    reason: str,
    operator_id: int = 0,
    operator_display: str = "",
    target_message_id: int = 0,
) -> str:
    group_id = int(message.chat.id)
    replied_message_id = int(target_message_id or _replied_message_id(message))
    actor_id = int(operator_id or getattr(message.from_user, "id", 0) or 0)
    actor_display = operator_display or str(
        getattr(message.from_user, "full_name", "") or ""
    )
    rejection = await _ban_target_rejection(message, settings, target_id)
    if rejection:
        return _render_ban_result(
            "本群封禁未完成",
            errors=[rejection],
        )

    prompts: set[tuple[int, int]] = set()
    private_prompts: set[tuple[int, int]] = set()
    verification = await get_join_verification(session, group_id, target_id)
    if verification is not None and int(verification.prompt_message_id or 0) > 0:
        prompts.add((group_id, int(verification.prompt_message_id)))
    if verification is not None and int(
        getattr(verification, "private_message_id", 0) or 0
    ) > 0:
        private_prompts.add((target_id, int(verification.private_message_id)))
    # Crash-compensation journal: until the local ban policy and terminal
    # delete commit together below, a SIGKILL after Telegram applies the ban
    # must recover by unbanning rather than strand an untracked permanent ban.
    recovery = await lease_join_verification_for_unban(
        session,
        group_id,
        target_id,
        manual_unban=False,
    )
    if recovery is None:
        await session.rollback()
        return _render_ban_result(
            "本群封禁未完成",
            errors=["无法建立封禁恢复工单，本次未调用 Telegram；请立即重试。"],
        )
    # Do not hold a SQLite read snapshot across the Telegram API call. The
    # post-enforcement transaction must observe challenges/warnings written by
    # other moderation entry points while the request was in flight.
    await session.commit()

    try:
        result = await ban_member(message.bot, group_id, target_id)
        if not result:
            raise RuntimeError("Telegram returned false")
    except Exception as exc:
        log.info("group ban failed | group=%s user=%s error=%s", group_id, target_id, exc)
        await session.rollback()
        try:
            await record_ban_event(
                session,
                group_id=group_id,
                target_user_id=target_id,
                action="ban",
                source="manual_command",
                outcome="failed",
                reason=reason,
                actor_user_id=actor_id,
                actor_display=actor_display,
                details={"error": str(exc)[:300]},
            )
            await session.commit()
        except Exception:
            await session.rollback()
            log.exception(
                "group ban failure audit write failed | group=%s user=%s",
                group_id,
                target_id,
            )
        return _render_ban_result(
            "本群封禁未完成",
            errors=[
                "Telegram 群内封禁结果未确认，已保留崩溃恢复工单；"
                "请检查 bot 的封禁用户权限后重试。"
            ],
        )

    target_user = getattr(getattr(message, "reply_to_message", None), "from_user", None)
    persisted = False
    for attempt in range(1, 4):
        try:
            claimed = await complete_leased_join_verification(
                session,
                verification_id=int(recovery.verification_id),
                lease_until=recovery.lease_until,
                status="unbanning",
            )
            if not claimed:
                await session.rollback()
                break
            await _mark_group_banned_after_telegram(
                session,
                group_id=group_id,
                target_id=target_id,
            )
            await record_ban_event(
                session,
                group_id=group_id,
                target_user_id=target_id,
                target_display=str(getattr(target_user, "full_name", "") or ""),
                target_username=str(getattr(target_user, "username", "") or ""),
                action="ban",
                source="manual_command",
                outcome="succeeded",
                reason=reason or "管理员手动本群封禁",
                actor_user_id=actor_id,
                actor_display=actor_display,
            )
            await session.commit()
            persisted = True
            break
        except Exception:
            await session.rollback()
            log.exception(
                "group ban state persistence failed | group=%s user=%s attempt=%s/3",
                group_id,
                target_id,
                attempt,
            )
    if not persisted:
        # A newer exact generation (usually /unban) won while Telegram ban was
        # in flight, or persistence repeatedly failed. Re-read the durable
        # policy and make the remote state match it; never recreate the old
        # local warning or delete the newer recovery journal.
        async def preserve_latest_ban() -> bool:
            await session.rollback()
            blocked = await verification_release_blocked_by_ban(
                session,
                group_id=group_id,
                user_id=target_id,
            )
            await session.commit()
            return bool(blocked)

        async def preserve_latest_restriction() -> bool:
            await session.rollback()
            required = await verification_restriction_required(
                session,
                group_id=group_id,
                user_id=target_id,
            )
            await session.commit()
            return bool(required)

        try:
            await reconcile_moderation_ban_after_lost_lease(
                message.bot,
                group_id,
                target_id,
                preserve_latest_ban,
                restriction_required=preserve_latest_restriction,
            )
        except Exception:
            log.exception(
                "group ban superseded-state reconciliation failed | group=%s user=%s",
                group_id,
                target_id,
            )
    await delete_verification_prompts(message.bot, prompts)
    await close_private_challenge_messages(message.bot, private_prompts)
    if not persisted:
        return _render_ban_result(
            "本群封禁未完成",
            errors=[
                "封禁执行期间权限策略已被更新或数据库写入失败；"
                "已按最新持久化策略重新校准 Telegram 状态。"
            ],
        )

    await _delete_ban_target_message(
        message.bot,
        group_id=group_id,
        message_id=replied_message_id,
    )

    return _render_ban_result("本群封禁完成")


async def _perform_group_unban(
    message: Message,
    session: AsyncSession,
    *,
    target_id: int,
    operator_id: int = 0,
    operator_display: str = "",
) -> str:
    key = (0, int(target_id))
    lock = _MANUAL_BAN_LOCKS.setdefault(key, asyncio.Lock())
    async with lock:
        return await _perform_group_unban_locked(
            message,
            session,
            target_id=target_id,
            operator_id=operator_id,
            operator_display=operator_display,
        )


async def _perform_group_unban_locked(
    message: Message,
    session: AsyncSession,
    *,
    target_id: int,
    operator_id: int = 0,
    operator_display: str = "",
) -> str:
    group_id = int(message.chat.id)
    actor_id = int(operator_id or getattr(message.from_user, "id", 0) or 0)
    actor_display = operator_display or str(
        getattr(message.from_user, "full_name", "") or ""
    )
    if await is_globally_banned(session, target_id):
        return _render_ban_result(
            "本群解封未完成",
            errors=[
                "该用户仍在全局封禁名单中，不能只解除本群封禁；"
                "请由最高管理员重新发送 /unban 并选择「全局解封」。"
            ],
        )
    row = await session.scalar(
        select(UserWarning).where(
            UserWarning.group_id == group_id,
            UserWarning.user_id == target_id,
        )
    )
    warning_snapshot = (
        (
            int(row.id),
            max(0, int(row.count or 0)),
            bool(row.is_banned),
        )
        if row is not None
        else None
    )
    prompts: set[tuple[int, int]] = set()
    private_prompts: set[tuple[int, int]] = set()
    verification = await get_join_verification(session, group_id, target_id)
    if verification is not None and int(verification.prompt_message_id or 0) > 0:
        prompts.add((group_id, int(verification.prompt_message_id)))
    if verification is not None and int(
        getattr(verification, "private_message_id", 0) or 0
    ) > 0:
        private_prompts.add((target_id, int(verification.private_message_id)))
    # Cancel the current verification/enforcement generation durably before
    # Telegram is touched.  The delayed ``unbanning`` journal survives SIGKILL
    # and outlives any stale worker already inside its bounded ban call.
    recovery = await lease_join_verification_for_unban(
        session,
        group_id,
        target_id,
    )
    # Release the read snapshot before awaiting Telegram. Any policy change
    # during the network call must be visible to the CAS below.
    await session.commit()
    activate_manual_unban_recovery(recovery)
    try:
        result = await unban_member(message.bot, group_id, target_id)
        if not result:
            raise RuntimeError("Telegram returned false")
    except Exception as exc:
        log.info("group unban failed | group=%s user=%s error=%s", group_id, target_id, exc)
        return _render_ban_result(
            "本群解封未完成",
            errors=["Telegram 群内解封失败，请检查 bot 的封禁用户权限后重试。"],
        )

    # A global ban added while Telegram was in flight wins. Re-enforce it
    # immediately instead of leaving Telegram and the policy registry split.
    if await is_globally_banned(session, target_id):
        await session.rollback()
        try:
            await ban_member(message.bot, group_id, target_id)
        except Exception:
            log.exception(
                "group unban global-race reban failed | group=%s user=%s",
                group_id,
                target_id,
            )
        return _render_ban_result(
            "本群解封未完成",
            errors=["解封期间该用户被加入全局封禁名单，已保留封禁状态。"],
        )

    claimed = False
    if warning_snapshot is None:
        current = await session.scalar(
            select(UserWarning).where(
                UserWarning.group_id == group_id,
                UserWarning.user_id == target_id,
            )
        )
        claimed = current is None
    else:
        warning_id, previous_count, was_banned = warning_snapshot
        result = await session.execute(
            delete(UserWarning).where(
                UserWarning.id == warning_id,
                UserWarning.group_id == group_id,
                UserWarning.user_id == target_id,
                UserWarning.count == previous_count,
                UserWarning.is_banned.is_(was_banned),
            )
        )
        claimed = int(result.rowcount or 0) == 1
    if not claimed:
        await session.rollback()
        current = await session.scalar(
            select(UserWarning).where(
                UserWarning.group_id == group_id,
                UserWarning.user_id == target_id,
            )
        )
        if current is not None and bool(current.is_banned):
            await session.rollback()
            try:
                await ban_member(message.bot, group_id, target_id)
            except Exception:
                log.exception(
                    "group unban concurrent-state reban failed | group=%s user=%s",
                    group_id,
                    target_id,
                )
            return _render_ban_result(
                "本群解封未完成",
                errors=["解封期间封禁状态已被其他管理操作更新，已保留最新封禁状态。"],
            )
        await session.rollback()
        return _render_ban_result(
            "本群解封未完成",
            errors=["群内解封已执行，但警告记录同时被其他操作更新，因此保留了最新记录。"],
        )

    current_verification = await get_join_verification(session, group_id, target_id)
    if (
        current_verification is not None
        and int(current_verification.prompt_message_id or 0) > 0
    ):
        prompts.add((group_id, int(current_verification.prompt_message_id)))
    if current_verification is not None and int(
        getattr(current_verification, "private_message_id", 0) or 0
    ) > 0:
        private_prompts.add(
            (target_id, int(current_verification.private_message_id))
        )
    try:
        await session.commit()
    except Exception:
        await session.rollback()
        try:
            await ban_member(message.bot, group_id, target_id)
        except Exception:
            log.exception(
                "group unban database-failure reban failed | group=%s user=%s",
                group_id,
                target_id,
            )
        return _render_ban_result(
            "本群解封未完成",
            errors=["Telegram 已解封，但数据库更新失败；已尝试恢复封禁，请稍后重试。"],
        )

    await delete_verification_prompts(message.bot, prompts)
    await close_private_challenge_messages(message.bot, private_prompts)
    restored = await restore_member_permissions(message.bot, group_id, target_id)
    target_user = getattr(getattr(message, "reply_to_message", None), "from_user", None)
    try:
        await record_ban_event(
            session,
            group_id=group_id,
            target_user_id=target_id,
            target_display=str(getattr(target_user, "full_name", "") or ""),
            target_username=str(getattr(target_user, "username", "") or ""),
            action="unban",
            source="manual_command",
            outcome="succeeded",
            reason="管理员解除本群封禁",
            actor_user_id=actor_id,
            actor_display=actor_display,
            details={"permissions_restored": bool(restored)},
        )
        await session.commit()
    except Exception:
        await session.rollback()
        log.exception(
            "group unban audit write failed | group=%s user=%s",
            group_id,
            target_id,
        )
    if restored and isinstance(recovery, UnbanRecovery):
        try:
            completed = await complete_leased_join_verification(
                session,
                verification_id=int(recovery.verification_id),
                lease_until=recovery.lease_until,
                status="unbanning",
            )
            if completed:
                await session.commit()
            else:
                await session.rollback()
                log.info(
                    "group unban recovery journal completion lost race | "
                    "group=%s user=%s",
                    group_id,
                    target_id,
                )
        except Exception:
            await session.rollback()
            log.exception(
                "group unban recovery journal completion failed | group=%s user=%s",
                group_id,
                target_id,
            )
    errors = (
        ["已解除封禁，但 Telegram 未确认权限恢复；用户重新入群后将按群默认权限生效。"]
        if not restored
        else []
    )
    return _render_ban_result("本群解封完成", errors=errors)


async def _perform_global_ban(
    message: Message,
    session_factory: async_sessionmaker[AsyncSession],
    *,
    target_id: int,
    reason: str,
    operator_id: int,
    source: str = "manual",
    origin_group_id: int = 0,
    target_message_id: int = 0,
) -> str:
    lock = _MANUAL_BAN_LOCKS.setdefault((0, int(target_id)), asyncio.Lock())
    async with lock:
        return await _perform_global_ban_locked(
            message,
            session_factory,
            target_id=target_id,
            reason=reason,
            operator_id=operator_id,
            source=source,
            origin_group_id=origin_group_id,
            target_message_id=target_message_id,
        )


async def _perform_global_ban_locked(
    message: Message,
    session_factory: async_sessionmaker[AsyncSession],
    *,
    target_id: int,
    reason: str,
    operator_id: int,
    source: str = "manual",
    origin_group_id: int = 0,
    target_message_id: int = 0,
) -> str:
    source_group_id = int(
        origin_group_id or getattr(getattr(message, "chat", None), "id", 0) or 0
    )
    replied_message_id = int(target_message_id or _replied_message_id(message))
    async with session_factory() as session:
        # Persist one idempotent reconciliation journal per authorized group in
        # the same transaction as the global policy.  The existing unbanning
        # terminal state is intentionally reused: its recovery routine reads
        # the latest durable policy and therefore enforces the global ban when
        # present, or restores access if a concurrent global unban wins.
        journal_group_ids = await _authorized_group_ids(
            session,
            include_inactive=True,
        )
        group_ids = await _authorized_group_ids(session)
        active_group_set = set(group_ids)
        recoveries = await lease_join_verifications_for_user_unban(
            session,
            target_id,
            group_ids=journal_group_ids,
            manual_unban=False,
        )
        default_reason = (
            "管理员标记为垃圾用户" if source == "spam_command" else "手动封禁"
        )
        await add_global_ban(
            session,
            target_id,
            reason=reason or default_reason,
            source=source,
            created_by=operator_id,
        )
        prompts = tuple(
            (item.group_id, item.prompt_message_id)
            for item in recoveries
            if item.prompt_message_id > 0
            and int(item.group_id) in active_group_set
        )
        private_prompts = tuple(
            (item.user_id, item.private_message_id)
            for item in recoveries
            if item.private_message_id > 0
        )
        await session.commit()
    await _publish_privileged_progress(
        message,
        title="全局封禁 · 执行中",
        current=f"处理 <code>{len(group_ids)}</code> 个授权群",
        next_step="写入汇总结果并更新本消息",
        action="全局策略已持久化，正在并发执行群内封禁。",
        context=f"<b>目标</b>　<code>{target_id}</code>",
    )

    recovery_by_group = {int(item.group_id): item for item in recoveries}

    async def complete_recovery(group_id: int) -> bool:
        recovery = recovery_by_group.get(int(group_id))
        if recovery is None:
            return True
        async with session_factory() as completion_session:
            completed = await complete_leased_join_verification(
                completion_session,
                verification_id=int(recovery.verification_id),
                lease_until=recovery.lease_until,
                status="unbanning",
            )
            if completed:
                await completion_session.commit()
                return True
            await completion_session.rollback()
            return False

    async def ban_one(group_id: int) -> bool:
        async with session_factory() as policy_session:
            if not await is_globally_banned(policy_session, target_id):
                if await unban_member(message.bot, group_id, target_id):
                    await restore_member_permissions(message.bot, group_id, target_id)
                    await complete_recovery(group_id)
                return False
        enforced = await ban_member(message.bot, group_id, target_id)
        if not enforced:
            return False
        async with session_factory() as policy_session:
            still_banned = await is_globally_banned(policy_session, target_id)
        if still_banned:
            await complete_recovery(group_id)
            return True
        # A concurrent global unban won while Telegram was in flight. Reconcile
        # the remote side immediately so the last durable policy wins.
        if await unban_member(message.bot, group_id, target_id):
            await restore_member_permissions(message.bot, group_id, target_id)
            await complete_recovery(group_id)
        return False

    outcomes = await _run_group_actions_bounded(
        group_ids,
        ban_one,
        deadline_seconds=45.0,
    )
    await delete_verification_prompts(message.bot, prompts)
    await close_private_challenge_messages(message.bot, private_prompts)
    origin_banned = any(
        outcome.succeeded and int(outcome.group_id) == source_group_id
        for outcome in outcomes
    )
    if origin_banned:
        await _delete_ban_target_message(
            message.bot,
            group_id=source_group_id,
            message_id=replied_message_id,
        )
    errors: list[str] = []
    deferred_groups = max(0, len(journal_group_ids) - len(group_ids))
    if deferred_groups:
        errors.append(
            f"{deferred_groups} 个离线群尚未处理，Bot 重回群后将由持久工单继续补偿。"
        )
    failure_summary = _failure_summary(outcomes)
    if failure_summary:
        errors.append(
            f"群内封禁未完成：{failure_summary}。"
            "失败群仍受全局策略拦截，可重新提交同一命令补偿。"
        )
    return _render_ban_result("全局封禁完成", errors=errors)


async def _perform_global_unban(
    message: Message,
    session_factory: async_sessionmaker[AsyncSession],
    *,
    target_id: int,
    operator_id: int,
) -> str:
    lock = _MANUAL_BAN_LOCKS.setdefault((0, int(target_id)), asyncio.Lock())
    async with lock:
        return await _perform_global_unban_locked(
            message,
            session_factory,
            target_id=target_id,
            operator_id=operator_id,
        )


async def _perform_global_unban_locked(
    message: Message,
    session_factory: async_sessionmaker[AsyncSession],
    *,
    target_id: int,
    operator_id: int,
) -> str:
    async with session_factory() as session:
        journal_group_ids = await _authorized_group_ids(
            session,
            include_inactive=True,
        )
        group_ids = await _authorized_group_ids(session)
        active_group_set = set(group_ids)
        recoveries = await lease_join_verifications_for_user_unban(
            session,
            target_id,
            group_ids=journal_group_ids,
        )
        await remove_global_ban(
            session,
            target_id,
            operator_id=operator_id,
        )
        prompts = tuple(
            (item.group_id, item.prompt_message_id)
            for item in recoveries
            if item.prompt_message_id > 0
            and int(item.group_id) in active_group_set
        )
        private_prompts = tuple(
            (item.user_id, item.private_message_id)
            for item in recoveries
            if item.private_message_id > 0
        )
        for group_id in journal_group_ids:
            await _clear_user_warning(
                session,
                group_id,
                target_id,
                preserve_ban=False,
            )
        await session.commit()
    activate_manual_unban_recoveries(recoveries)
    await _publish_privileged_progress(
        message,
        title="全局解封 · 执行中",
        current=f"处理 <code>{len(group_ids)}</code> 个授权群",
        next_step="恢复权限并更新本消息",
        action="恢复工单已持久化，正在并发执行群内解封。",
        context=f"<b>目标</b>　<code>{target_id}</code>",
    )
    recovery_by_group = {int(item.group_id): item for item in recoveries}
    restored_group_ids: set[int] = set()

    async def unban_one(group_id: int) -> bool:
        async def preserve_ban() -> bool:
            async with session_factory() as policy_session:
                blocked = await verification_release_blocked_by_ban(
                    policy_session,
                    group_id=group_id,
                    user_id=target_id,
                )
                return blocked

        result = await unban_member(
            message.bot,
            group_id,
            target_id,
            preserve_ban=preserve_ban,
        )
        if not result:
            return False
        restored = await restore_member_permissions(message.bot, group_id, target_id)
        if not restored:
            # Keep the durable unbanning journal so the sweeper retries the
            # permission restoration after the current lease expires.
            return True
        restored_group_ids.add(int(group_id))
        recovery = recovery_by_group.get(int(group_id))
        if recovery is not None:
            async with session_factory() as completion_session:
                completed = await complete_leased_join_verification(
                    completion_session,
                    verification_id=int(recovery.verification_id),
                    lease_until=recovery.lease_until,
                    status="unbanning",
                )
                if completed:
                    await completion_session.commit()
                else:
                    await completion_session.rollback()
        return True

    outcomes = await _run_group_actions_bounded(group_ids, unban_one)
    await delete_verification_prompts(message.bot, prompts)
    await close_private_challenge_messages(message.bot, private_prompts)
    unbanned_groups = sum(outcome.succeeded for outcome in outcomes)
    restored_groups = len(restored_group_ids)

    errors: list[str] = []
    unrestored_groups = max(0, unbanned_groups - restored_groups)
    if unrestored_groups:
        errors.append(
            f"{unrestored_groups} 个群未确认发言权限恢复，恢复工单将继续重试。"
        )
    deferred_groups = max(0, len(journal_group_ids) - len(group_ids))
    if deferred_groups:
        errors.append(
            f"{deferred_groups} 个离线群尚未处理，Bot 重回群后将由持久工单继续解封。"
        )
    failure_summary = _failure_summary(outcomes)
    if failure_summary:
        errors.append(
            f"群内解封未完成：{failure_summary}。"
            "未完成群保留恢复工单，可重新提交同一全局解封。"
        )
    return _render_ban_result("全局解封完成", errors=errors)


def _scope_keyboard(
    action: str,
    target_id: int,
    target_message_id: int = 0,
) -> InlineKeyboardMarkup:
    verb = "封禁" if action == "ban" else "解封"
    short = "b" if action == "ban" else "u"
    message_suffix = (
        f":{int(target_message_id)}" if int(target_message_id or 0) > 0 else ""
    )
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=f"仅本群{verb}",
                    callback_data=(
                        f"{_BAN_SCOPE_CALLBACK_PREFIX}:{short}:l:{target_id}"
                        f"{message_suffix}"
                    ),
                ),
                InlineKeyboardButton(
                    text=f"全局{verb}",
                    callback_data=(
                        f"{_BAN_SCOPE_CALLBACK_PREFIX}:{short}:g:{target_id}"
                        f"{message_suffix}"
                    ),
                ),
            ]
        ]
    )


def _scope_request_from_durable_callback(
    callback: CallbackQuery,
    *,
    action: str,
    target_id: int,
    target_message_id: int = 0,
) -> _BanScopeRequest | None:
    """Rebuild a lost in-memory token from an authentic, recent bot message."""

    message = callback.message
    sender = getattr(message, "from_user", None)
    bot_id = int(getattr(callback.bot, "id", 0) or 0)
    if sender is None or not bool(getattr(sender, "is_bot", False)):
        return None
    if bot_id and int(getattr(sender, "id", 0) or 0) != bot_id:
        return None
    message_date = getattr(message, "date", None)
    timestamp = getattr(message_date, "timestamp", None)
    if not callable(timestamp):
        return None
    try:
        age = max(0.0, time.time() - float(timestamp()))
    except (TypeError, ValueError, OSError):
        return None
    if age > _BAN_SCOPE_TTL_SECONDS:
        return None
    reason = ""
    text = str(getattr(message, "text", "") or "")
    match = re.search(
        r"(?:^|\n)(?:<b>)?原因(?:</b>)?(?:\s*[:：]\s*|　+)([^\n]+)",
        text,
    )
    if match:
        reason = match.group(1).strip()[:1000]
    return _BanScopeRequest(
        action=action,
        target_id=int(target_id),
        reason=reason,
        created_at=time.monotonic(),
        target_message_id=int(target_message_id or 0),
    )


async def _ask_super_admin_scope(
    message: Message,
    *,
    action: str,
    target_id: int,
    reason: str = "",
) -> None:
    verb = "封禁" if action == "ban" else "解封"
    target_message_id = _replied_message_id(message) if action == "ban" else 0
    details = ["「仅本群」只修改当前群；「全局」会作用于全部授权群和全局名单。"]
    if reason:
        details.insert(0, f"<b>原因</b>　{html.escape(reason)}")
    text = render_action_notice(
        f"选择{verb}范围",
        context=f"目标　<code>{target_id}</code>",
        action="请使用下方按钮选择处理范围。",
        details=details,
    )
    with privileged_request_scope():
        sent = await message.answer(
            text,
            parse_mode="HTML",
            reply_markup=_scope_keyboard(action, target_id, target_message_id),
        )
    now = time.monotonic()
    for key, request in list(_BAN_SCOPE_REQUESTS.items()):
        if now - request.created_at > _BAN_SCOPE_TTL_SECONDS:
            _BAN_SCOPE_REQUESTS.pop(key, None)
    overflow = len(_BAN_SCOPE_REQUESTS) - _BAN_SCOPE_MAX_REQUESTS + 1
    if overflow > 0:
        oldest = sorted(
            _BAN_SCOPE_REQUESTS.items(),
            key=lambda item: item[1].created_at,
        )[:overflow]
        for old_key, _request in oldest:
            _BAN_SCOPE_REQUESTS.pop(old_key, None)
    _BAN_SCOPE_REQUESTS[(int(message.chat.id), int(sent.message_id))] = _BanScopeRequest(
        action=action,
        target_id=int(target_id),
        reason=str(reason or ""),
        created_at=now,
        target_message_id=target_message_id,
    )


async def _run_result_job(
    *,
    progress_message: Message,
    operation: Callable[[], Awaitable[str]],
    task_title: str = "后台任务",
    compact: bool = False,
) -> None:
    try:
        result_text = await operation()
    except asyncio.CancelledError:
        raise
    except Exception:
        log.exception("privileged administration job failed")
        failure_text = (
            _render_ban_result(
                f"{task_title}未完成",
                errors=["后台任务发生异常，幂等状态已保留，请稍后重试。"],
            )
            if compact
            else "<b>操作未完成</b>\n后台任务发生异常，幂等状态已保留，请稍后重试。"
        )
        await _publish_privileged_result(
            progress_message,
            failure_text,
            status="failed",
            default_title=task_title,
            compact=compact,
        )
        raise
    await _publish_privileged_result(
        progress_message,
        result_text,
        status="completed",
        default_title=task_title,
        compact=compact,
    )


@router.message(Command("ban"))
async def cmd_ban(
    message: Message,
    session: AsyncSession,
    settings: Settings,
    session_factory: async_sessionmaker[AsyncSession] | None = None,
) -> None:
    if not await ensure_group_authorized(message, session, settings):
        return
    if not await _ensure_ban_command_admin(message, session, settings):
        return
    await _commit_if_supported(session)
    args = (message.text or "").partition(" ")[2].strip()
    target_id, reason = _resolve_ban_target(message, args)
    if target_id is None or target_id <= 0:
        await _answer(message, settings, _BAN_USAGE)
        return
    if is_super_admin_user_id(target_id, settings):
        await _answer(
            message,
            settings,
            _render_ban_result(
                "本群封禁未执行",
                errors=["不能封禁最高管理员。"],
            ),
        )
        return
    operator = message.from_user
    if operator and is_super_admin_user_id(int(operator.id), settings):
        await session.commit()
        await _ask_super_admin_scope(
            message,
            action="ban",
            target_id=target_id,
            reason=reason,
        )
        return
    if session_factory is not None:
        await session.commit()
        with privileged_request_scope():
            progress = await message.answer(
                render_progress_notice(
                    "本群封禁 · 执行中",
                    completed="已受理请求",
                    current="执行 Telegram 封禁",
                    next_step="写入结果并更新本消息",
                    context=f"<b>目标</b>　<code>{target_id}</code>",
                ),
                parse_mode="HTML",
            )

        operator_id = int(operator.id) if operator is not None else 0

        async def operation() -> str:
            if not await _queued_operator_can_manage_members(
                bot=message.bot,
                session_factory=session_factory,
                settings=settings,
                group_id=int(message.chat.id),
                user_id=operator_id,
            ):
                return _render_ban_result(
                    "本群封禁已取消",
                    errors=["操作者权限或群授权已发生变化。"],
                )
            async with session_factory() as work_session:
                return await _perform_group_ban(
                    message,
                    work_session,
                    settings,
                    target_id=target_id,
                    reason=reason,
                )

        submission = submit_privileged_task(
            key=f"ban:local:{int(message.chat.id)}:{target_id}",
            label=f"local ban {target_id} in {int(message.chat.id)}",
            operation=lambda: _run_result_job(
                progress_message=progress,
                operation=operation,
                task_title="本群封禁",
                compact=True,
            ),
            lane="critical",
            priority=0,
            timeout_seconds=90.0,
        )
        if not submission.accepted:
            await _publish_privileged_result(
                progress,
                _render_ban_result(
                    "本群封禁未入队",
                    errors=["权限任务队列正忙，请立即重试。"],
                ),
                status="rejected",
                compact=True,
            )
        elif not submission.created:
            await _publish_privileged_result(
                progress,
                _render_ban_result("本群封禁正在执行"),
                status="duplicate",
                compact=True,
            )
        return
    text = await _perform_group_ban(
        message,
        session,
        settings,
        target_id=target_id,
        reason=reason,
    )
    await _answer(message, settings, text)


@router.message(Command("spam"))
async def cmd_spam(
    message: Message,
    session: AsyncSession,
    settings: Settings,
    session_factory: async_sessionmaker[AsyncSession] | None = None,
) -> None:
    """Ban a spammer and add the account to the global ban registry."""

    if not await ensure_group_authorized(message, session, settings):
        return
    if not await _ensure_ban_command_admin(message, session, settings):
        return
    await _commit_if_supported(session)

    args = (message.text or "").partition(" ")[2].strip()
    target_id, reason = _resolve_ban_target(message, args)
    if target_id is None or target_id <= 0:
        await _answer(message, settings, _SPAM_USAGE)
        return
    rejection = await _ban_target_rejection(message, settings, target_id)
    if rejection:
        await _answer(
            message,
            settings,
            _render_ban_result(
                "垃圾用户封禁未执行",
                errors=[rejection],
            ),
        )
        return

    operator = message.from_user
    operator_id = int(getattr(operator, "id", 0) or 0)
    group_id = int(message.chat.id)
    target_message_id = _replied_message_id(message)
    effective_reason = reason or "管理员标记为垃圾用户"

    effective_factory = session_factory
    if effective_factory is None:
        bind = getattr(session, "bind", None)
        if bind is not None:
            effective_factory = async_sessionmaker(
                bind=bind,
                class_=type(session),
                expire_on_commit=False,
                autoflush=False,
            )
    if effective_factory is None:
        await _answer(
            message,
            settings,
            _render_ban_result(
                "垃圾用户封禁未执行",
                errors=["数据库会话未就绪，请稍后重试。"],
            ),
        )
        return

    await session.commit()
    with privileged_request_scope():
        progress = await message.answer(
            render_progress_notice(
                "垃圾用户全局封禁 · 执行中",
                completed="已受理请求",
                current="写入全局封禁策略并执行 Telegram 封禁",
                next_step="汇总各授权群结果并更新本消息",
                context=f"<b>目标</b>　<code>{target_id}</code>",
            ),
            parse_mode="HTML",
        )

    async def operation() -> str:
        if not await _queued_operator_can_manage_members(
            bot=message.bot,
            session_factory=effective_factory,
            settings=settings,
            group_id=group_id,
            user_id=operator_id,
        ):
            return _render_ban_result(
                "垃圾用户封禁已取消",
                errors=["操作者权限或群授权已发生变化。"],
            )
        latest_rejection = await _ban_target_rejection(message, settings, target_id)
        if latest_rejection:
            return _render_ban_result(
                "垃圾用户封禁已取消",
                errors=[latest_rejection],
            )
        return await _perform_global_ban(
            progress,
            effective_factory,
            target_id=target_id,
            reason=effective_reason,
            operator_id=operator_id,
            source="spam_command",
            origin_group_id=group_id,
            target_message_id=target_message_id,
        )

    if session_factory is not None:
        submission = submit_privileged_task(
            key=f"ban:g:0:{target_id}",
            label=f"global spam ban {target_id}",
            operation=lambda: _run_result_job(
                progress_message=progress,
                operation=operation,
                task_title="垃圾用户全局封禁",
                compact=True,
            ),
            lane="critical_bulk",
            priority=10,
            timeout_seconds=_PRIVILEGED_JOB_DEADLINE_SECONDS,
        )
        if not submission.accepted:
            await _publish_privileged_result(
                progress,
                _render_ban_result(
                    "垃圾用户封禁未入队",
                    errors=["权限任务队列正忙，请立即重试。"],
                ),
                status="rejected",
                compact=True,
            )
        elif not submission.created:
            await _publish_privileged_result(
                progress,
                _render_ban_result("垃圾用户封禁正在执行"),
                status="duplicate",
                compact=True,
            )
        return

    result_text = await operation()
    await _publish_privileged_result(
        progress,
        result_text,
        status="completed",
        default_title="垃圾用户全局封禁",
        compact=True,
    )


@router.message(Command("unban"))
async def cmd_unban(
    message: Message,
    session: AsyncSession,
    settings: Settings,
    session_factory: async_sessionmaker[AsyncSession] | None = None,
) -> None:
    if not await ensure_group_authorized(message, session, settings):
        return
    if not await _ensure_ban_command_admin(message, session, settings):
        return
    await _commit_if_supported(session)
    args = (message.text or "").partition(" ")[2].strip()
    target_id, _ = _resolve_ban_target(message, args)
    if target_id is None or target_id <= 0:
        await _answer(message, settings, _UNBAN_USAGE)
        return
    operator = message.from_user
    if operator and is_super_admin_user_id(int(operator.id), settings):
        await session.commit()
        await _ask_super_admin_scope(
            message,
            action="unban",
            target_id=target_id,
        )
        return
    if session_factory is not None:
        await session.commit()
        with privileged_request_scope():
            progress = await message.answer(
                render_progress_notice(
                    "本群解封 · 执行中",
                    completed="已受理请求",
                    current="解除 Telegram 封禁",
                    next_step="恢复权限并更新本消息",
                    context=f"<b>目标</b>　<code>{target_id}</code>",
                ),
                parse_mode="HTML",
            )

        operator_id = int(operator.id) if operator is not None else 0

        async def operation() -> str:
            if not await _queued_operator_can_manage_members(
                bot=message.bot,
                session_factory=session_factory,
                settings=settings,
                group_id=int(message.chat.id),
                user_id=operator_id,
            ):
                return _render_ban_result(
                    "本群解封已取消",
                    errors=["操作者权限或群授权已发生变化。"],
                )
            async with session_factory() as work_session:
                return await _perform_group_unban(
                    message,
                    work_session,
                    target_id=target_id,
                )

        submission = submit_privileged_task(
            key=f"unban:local:{int(message.chat.id)}:{target_id}",
            label=f"local unban {target_id} in {int(message.chat.id)}",
            operation=lambda: _run_result_job(
                progress_message=progress,
                operation=operation,
                task_title="本群解封",
                compact=True,
            ),
            lane="critical",
            priority=0,
            timeout_seconds=90.0,
        )
        if not submission.accepted:
            await _publish_privileged_result(
                progress,
                _render_ban_result(
                    "本群解封未入队",
                    errors=["权限任务队列正忙，请立即重试。"],
                ),
                status="rejected",
                compact=True,
            )
        elif not submission.created:
            await _publish_privileged_result(
                progress,
                _render_ban_result("本群解封正在执行"),
                status="duplicate",
                compact=True,
            )
        return
    text = await _perform_group_unban(
        message,
        session,
        target_id=target_id,
    )
    await _answer(message, settings, text)


@router.callback_query(F.data.startswith(f"{_BAN_SCOPE_CALLBACK_PREFIX}:"))
async def on_ban_scope_choice(
    callback: CallbackQuery,
    session: AsyncSession,
    settings: Settings,
    session_factory: async_sessionmaker[AsyncSession] | None = None,
) -> None:
    operator = callback.from_user
    message = callback.message
    chat = getattr(message, "chat", None)
    if operator is None or not is_super_admin_user_id(int(operator.id), settings):
        await callback.answer("仅最高管理员可选择全局操作范围", show_alert=True)
        return
    if message is None or chat is None or getattr(chat, "type", "") not in {"group", "supergroup"}:
        await callback.answer("操作消息已失效", show_alert=True)
        return
    parts = str(callback.data or "").split(":")
    if len(parts) not in {4, 5} or parts[0] != _BAN_SCOPE_CALLBACK_PREFIX:
        await callback.answer("操作参数无效", show_alert=True)
        return
    action = "ban" if parts[1] == "b" else "unban" if parts[1] == "u" else ""
    scope = parts[2]
    try:
        target_id = int(parts[3])
    except (TypeError, ValueError):
        target_id = 0
    try:
        target_message_id = int(parts[4]) if len(parts) == 5 else 0
    except (TypeError, ValueError):
        target_message_id = -1
    if (
        not action
        or scope not in {"l", "g"}
        or target_id <= 0
        or target_message_id < 0
    ):
        await callback.answer("操作参数无效", show_alert=True)
        return
    if action == "ban" and is_super_admin_user_id(target_id, settings):
        await callback.answer("不能封禁最高管理员", show_alert=True)
        return

    key = (int(chat.id), int(message.message_id))
    invalid_request = False
    conflicting_scope = False
    async with _BAN_SCOPE_LOCK:
        request = _BAN_SCOPE_REQUESTS.get(key)
        if request is None:
            request = _scope_request_from_durable_callback(
                callback,
                action=action,
                target_id=target_id,
                target_message_id=target_message_id,
            )
            if request is not None:
                _BAN_SCOPE_REQUESTS[key] = request
        if (
            request is None
            or request.action != action
            or request.target_id != target_id
            or int(request.target_message_id or 0) != target_message_id
            or time.monotonic() - request.created_at > _BAN_SCOPE_TTL_SECONDS
        ):
            invalid_request = True
        elif request.claimed_scope and request.claimed_scope != scope:
            conflicting_scope = True
        else:
            request.claimed_scope = scope
    if invalid_request:
        try:
            await message.edit_reply_markup(reply_markup=None)
        except Exception:
            pass
        await callback.answer("该范围选择已失效，请重新发送命令", show_alert=True)
        return
    if conflicting_scope:
        await callback.answer("另一范围的任务已受理，不能重复选择", show_alert=True)
        return
    assert request is not None
    reason = request.reason
    verb = "封禁" if action == "ban" else "解封"
    scope_label = "全局" if scope == "g" else "本群"
    task_title = f"{scope_label}{verb}"
    task_details = [
        f"<b>目标</b>　<code>{target_id}</code>",
        f"<b>范围</b>　{scope_label}",
    ]
    if reason:
        task_details.append(f"<b>原因</b>　{html.escape(reason)}")
    await session.commit()
    if session_factory is not None:
        ack_gate = asyncio.Event()

        async def operation() -> str:
            await ack_gate.wait()
            if not is_super_admin_user_id(int(operator.id), settings):
                return _render_ban_result(
                    "权限任务已取消",
                    errors=["执行前复验发现最高管理员身份已发生变化。"],
                )
            if action == "ban" and is_super_admin_user_id(target_id, settings):
                return _render_ban_result(
                    "权限任务已取消",
                    errors=["目标用户已成为最高管理员。"],
                )
            if scope == "l" and not await _queued_operator_can_manage_members(
                bot=callback.bot,
                session_factory=session_factory,
                settings=settings,
                group_id=int(chat.id),
                user_id=int(operator.id),
            ):
                return _render_ban_result(
                    "权限任务已取消",
                    errors=["群授权或操作者权限已发生变化。"],
                )
            if action == "ban" and scope == "l":
                async with session_factory() as work_session:
                    return await _perform_group_ban(
                        message,
                        work_session,
                        settings,
                        target_id=target_id,
                        reason=reason,
                        operator_id=int(operator.id),
                        operator_display=str(getattr(operator, "full_name", "") or ""),
                        target_message_id=target_message_id,
                    )
            if action == "ban":
                return await _perform_global_ban(
                    message,
                    session_factory,
                    target_id=target_id,
                    reason=reason,
                    operator_id=int(operator.id),
                    origin_group_id=int(chat.id),
                    target_message_id=target_message_id,
                )
            if scope == "l":
                async with session_factory() as work_session:
                    return await _perform_group_unban(
                        message,
                        work_session,
                        target_id=target_id,
                        operator_id=int(operator.id),
                        operator_display=str(getattr(operator, "full_name", "") or ""),
                    )
            return await _perform_global_unban(
                message,
                session_factory,
                target_id=target_id,
                operator_id=int(operator.id),
            )

        async def run_claimed_job() -> None:
            try:
                await _run_result_job(
                    progress_message=message,
                    operation=operation,
                    task_title=task_title,
                    compact=True,
                )
            except BaseException:
                async with _BAN_SCOPE_LOCK:
                    current = _BAN_SCOPE_REQUESTS.get(key)
                    if current is request and current.claimed_scope == scope:
                        current.claimed_scope = ""
                raise
            else:
                async with _BAN_SCOPE_LOCK:
                    if _BAN_SCOPE_REQUESTS.get(key) is request:
                        _BAN_SCOPE_REQUESTS.pop(key, None)

        submission = submit_privileged_task(
            key=f"{action}:{scope}:{int(chat.id) if scope == 'l' else 0}:{target_id}",
            label=f"{scope} {action} {target_id}",
            operation=run_claimed_job,
            lane="critical_bulk" if scope == "g" else "critical",
            priority=10 if scope == "g" else 0,
            timeout_seconds=_PRIVILEGED_JOB_DEADLINE_SECONDS,
        )
        if not submission.accepted:
            async with _BAN_SCOPE_LOCK:
                current = _BAN_SCOPE_REQUESTS.get(key)
                if current is request and current.claimed_scope == scope:
                    current.claimed_scope = ""
            await _ack_callback_fast(
                callback,
                "权限任务队列正忙，请再次点击",
                show_alert=True,
            )
            return
        try:
            await _ack_callback_fast(
                callback,
                "任务已受理，正在后台执行" if submission.created else "相同任务正在执行",
            )
            if submission.created:
                await _publish_privileged_progress(
                    message,
                    title=f"{task_title} · 执行中",
                    current=f"执行 {scope_label}{verb}",
                    next_step="写入结果并更新本消息",
                    context=task_details,
                )
        finally:
            ack_gate.set()
        return

    await _ack_callback_fast(callback, "正在执行…")
    await _publish_privileged_progress(
        message,
        title=f"{task_title} · 执行中",
        current=f"执行 {scope_label}{verb}",
        next_step="写入结果并更新本消息",
        context=task_details,
    )
    try:
        if action == "ban" and scope == "l":
            result_text = await _perform_group_ban(
                message,
                session,
                settings,
                target_id=target_id,
                reason=reason,
                operator_id=int(operator.id),
                operator_display=str(getattr(operator, "full_name", "") or ""),
                target_message_id=target_message_id,
            )
        elif action == "ban":
            result_text = await _perform_global_ban(
                message,
                async_sessionmaker(
                    bind=session.bind,
                    class_=type(session),
                    expire_on_commit=False,
                    autoflush=False,
                ),
                target_id=target_id,
                reason=reason,
                operator_id=int(operator.id),
                origin_group_id=int(chat.id),
                target_message_id=target_message_id,
            )
        elif scope == "l":
            result_text = await _perform_group_unban(
                message,
                session,
                target_id=target_id,
                operator_id=int(operator.id),
                operator_display=str(getattr(operator, "full_name", "") or ""),
            )
        else:
            result_text = await _perform_global_unban(
                message,
                async_sessionmaker(
                    bind=session.bind,
                    class_=type(session),
                    expire_on_commit=False,
                    autoflush=False,
                ),
                target_id=target_id,
                operator_id=int(operator.id),
            )
    except BaseException:
        async with _BAN_SCOPE_LOCK:
            current = _BAN_SCOPE_REQUESTS.get(key)
            if current is request and current.claimed_scope == scope:
                current.claimed_scope = ""
        raise
    else:
        async with _BAN_SCOPE_LOCK:
            if _BAN_SCOPE_REQUESTS.get(key) is request:
                _BAN_SCOPE_REQUESTS.pop(key, None)
    await _publish_privileged_result(
        message,
        result_text,
        status="completed",
        default_title=task_title,
        compact=True,
    )


_RAID_GUARD_USAGE = (
    "<b>命令用法</b>\n"
    "/raidguard on：手动开启，直到管理员关闭\n"
    "/raidguard on &lt;分钟&gt;：按分钟限时开启\n"
    "/raidguard &lt;分钟&gt;：限时开启的简写\n"
    "/raidguard off：立即解除\n"
    "/raidguard status：查看当前状态"
)


@router.message(Command("raidguard", "raid"))
async def cmd_raidguard(
    message: Message,
    session: AsyncSession,
    settings: Settings,
    session_factory: async_sessionmaker[AsyncSession] | None = None,
) -> None:
    if not await ensure_group_authorized(message, session, settings):
        return
    if not await _ensure_ban_command_admin(message, session, settings):
        return
    await _commit_if_supported(session)
    service = get_raid_guard_service()
    if service is None:
        await _answer(message, settings, "爆破防护服务尚未启动，请稍后重试。")
        return
    raw = (message.text or "").partition(" ")[2].strip()
    parts = raw.split()
    action = parts[0].lower() if parts else "status"
    group_id = int(message.chat.id)

    if action in {"status", "state"}:
        status = service.lockdown_status(group_id)
        if not status["active"]:
            await _answer(message, settings, "<b>爆破防护状态</b>\n当前未处于锁定防护状态。")
            return
        until = status.get("until")
        source = "管理员手动开启" if status.get("source") == "manual" else "自动检测触发"
        deadline = "管理员手动关闭前" if until is None else until.strftime("%Y-%m-%d %H:%M:%S")
        await _answer(
            message,
            settings,
            "<b>爆破防护状态</b>\n"
            f"<b>状态</b>: 已开启（{source}）\n"
            f"<b>解除时间</b>: {deadline}",
        )
        return

    if action in {"off", "disable", "stop"}:
        if session_factory is not None:
            await session.commit()
            with privileged_request_scope():
                progress = await message.answer(
                    render_progress_notice(
                        "爆破防护 · 执行中",
                        completed="已受理请求",
                        current="解除手动锁定",
                        next_step="写入结果并更新本消息",
                        context=f"<b>群组</b>　<code>{group_id}</code>",
                    ),
                    parse_mode="HTML",
                )

            async def disable_operation() -> str:
                if not await _queued_operator_can_manage_members(
                    bot=message.bot,
                    session_factory=session_factory,
                    settings=settings,
                    group_id=group_id,
                    user_id=int(message.from_user.id) if message.from_user else 0,
                ):
                    return "爆破防护操作已取消：操作者权限或群授权已发生变化。"
                disabled = await service.disable_manual_lockdown(group_id)
                return "爆破防护已解除。" if disabled else "当前没有正在运行的爆破锁定。"

            submission = submit_privileged_task(
                key=f"raidguard:disable:{group_id}",
                label=f"disable raid guard in {group_id}",
                operation=lambda: _run_result_job(
                    progress_message=progress,
                    operation=disable_operation,
                    task_title="爆破防护",
                ),
                lane="critical",
                priority=0,
                timeout_seconds=60.0,
            )
            if not submission.accepted:
                await _publish_privileged_result(
                    progress,
                    "<b>爆破防护操作未入队</b>\n权限任务队列正忙，请立即重试。",
                    status="rejected",
                )
            elif not submission.created:
                await _publish_privileged_result(
                    progress,
                    "<b>爆破防护操作正在执行</b>\n相同任务已在队列中。",
                    status="duplicate",
                )
            return
        try:
            disabled = await service.disable_manual_lockdown(group_id)
        except Exception as exc:
            log.exception("manual raid disable failed | group=%s", group_id)
            detail = str(exc) if isinstance(exc, RuntimeError) else "手动爆破防护状态保存失败，请稍后重试"
            await _answer(message, settings, html.escape(detail))
            return
        await _answer(
            message,
            settings,
            "爆破防护已解除。" if disabled else "当前没有正在运行的爆破锁定。",
        )
        return

    duration: object | None
    if action in {"on", "enable", "start"}:
        if len(parts) > 2:
            await _answer(message, settings, _RAID_GUARD_USAGE)
            return
        duration = parts[1] if len(parts) == 2 else None
    elif len(parts) == 1:
        duration = parts[0]
    else:
        await _answer(message, settings, _RAID_GUARD_USAGE)
        return
    try:
        minutes = normalize_manual_lockdown_minutes(duration)
    except ValueError as exc:
        await _answer(message, settings, f"{html.escape(str(exc))}\n\n{_RAID_GUARD_USAGE}")
        return
    if session_factory is not None:
        await session.commit()
        with privileged_request_scope():
            progress = await message.answer(
                render_progress_notice(
                    "爆破防护 · 执行中",
                    completed="已受理请求",
                    current="开启手动锁定",
                    next_step="写入结果并更新本消息",
                    context=f"<b>群组</b>　<code>{group_id}</code>",
                ),
                parse_mode="HTML",
            )

        async def enable_operation() -> str:
            if not await _queued_operator_can_manage_members(
                bot=message.bot,
                session_factory=session_factory,
                settings=settings,
                group_id=group_id,
                user_id=int(message.from_user.id) if message.from_user else 0,
            ):
                return "爆破防护操作已取消：操作者权限或群授权已发生变化。"
            await service.enable_manual_lockdown(
                group_id,
                duration_minutes=minutes,
            )
            return (
                f"爆破防护已手动开启 {minutes} 分钟。"
                if minutes is not None
                else "爆破防护已手动开启，将持续到管理员发送 /raidguard off。"
            )

        submission = submit_privileged_task(
            key=f"raidguard:enable:{group_id}",
            label=f"enable raid guard in {group_id}",
            operation=lambda: _run_result_job(
                progress_message=progress,
                operation=enable_operation,
                task_title="爆破防护",
            ),
            lane="critical",
            priority=0,
            timeout_seconds=60.0,
        )
        if not submission.accepted:
            await _publish_privileged_result(
                progress,
                "<b>爆破防护操作未入队</b>\n权限任务队列正忙，请立即重试。",
                status="rejected",
            )
        elif not submission.created:
            await _publish_privileged_result(
                progress,
                "<b>爆破防护操作正在执行</b>\n相同任务已在队列中。",
                status="duplicate",
            )
        return
    try:
        await service.enable_manual_lockdown(
            group_id,
            duration_minutes=minutes,
        )
    except Exception as exc:
        log.exception("manual raid enable failed | group=%s", group_id)
        detail = str(exc) if isinstance(exc, RuntimeError) else "手动爆破防护状态保存失败，请稍后重试"
        await _answer(message, settings, html.escape(detail))
        return
    # The service has already posted the persistent state-change notice. The
    # command acknowledgement follows the normal management retention policy.
    await _answer(
        message,
        settings,
        (
            f"爆破防护已手动开启 {minutes} 分钟。"
            if minutes is not None
            else "爆破防护已手动开启，将持续到管理员发送 /raidguard off。"
        ),
    )


@router.callback_query(F.data == RAID_GUARD_DISABLE_CALLBACK_DATA)
async def on_raid_guard_disable_callback(
    callback: CallbackQuery,
    session: AsyncSession,
    settings: Settings,
    session_factory: async_sessionmaker[AsyncSession] | None = None,
) -> None:
    """Release the active raid lockdown from its current status message."""
    message = callback.message
    chat = getattr(message, "chat", None)
    operator = callback.from_user
    if message is None or chat is None or chat.type not in ("group", "supergroup"):
        await callback.answer("爆破防护状态消息已失效", show_alert=True)
        return
    if operator is None:
        await callback.answer("无法识别操作者", show_alert=True)
        return
    group_id = int(chat.id)
    message_id = int(message.message_id)

    authorized = await is_group_authorized(session, group_id)
    await _commit_if_supported(session)
    if not authorized:
        await callback.answer("当前群组未授权", show_alert=True)
        return
    try:
        with privileged_request_scope():
            async with asyncio.timeout(4.0):
                can_manage = await is_group_admin_or_higher(
                    bot=callback.bot,
                    session=session,
                    settings=settings,
                    group_id=group_id,
                    user_id=int(operator.id),
                )
    except Exception:
        can_manage = False
        log.warning(
            "raid guard callback admin lookup failed | group=%s user=%s",
            group_id,
            operator.id,
            exc_info=True,
        )
    if not can_manage:
        await callback.answer("仅本群管理员可解除爆破防护", show_alert=True)
        return

    service = get_raid_guard_service()
    if service is None:
        await callback.answer("爆破防护服务尚未启动，请稍后重试", show_alert=True)
        return
    if not service.lockdown_status_message_matches(group_id, message_id):
        await callback.answer("防护状态已更新，请使用最新消息操作", show_alert=True)
        return

    if session_factory is None:
        try:
            disabled = await service.disable_manual_lockdown(group_id)
        except Exception:
            log.exception("raid guard callback disable failed | group=%s", group_id)
            await callback.answer("解除爆破防护失败，请稍后重试", show_alert=True)
            return
        await callback.answer(
            "爆破防护已解除" if disabled else "当前没有正在运行的爆破锁定"
        )
        return

    callback_acknowledged = asyncio.Event()

    async def disable_operation() -> None:
        await callback_acknowledged.wait()
        if not await _queued_operator_can_manage_members(
            bot=callback.bot,
            session_factory=session_factory,
            settings=settings,
            group_id=group_id,
            user_id=int(operator.id),
        ):
            log.warning(
                "raid guard callback cancelled after authorization changed | "
                "group=%s operator=%s",
                group_id,
                operator.id,
            )
            return
        if not service.lockdown_status_message_matches(group_id, message_id):
            return
        try:
            await service.disable_manual_lockdown(group_id)
        except Exception:
            log.exception("raid guard callback disable failed | group=%s", group_id)

    submission = submit_privileged_task(
        key=f"raidguard:disable:{group_id}",
        label=f"disable raid guard in {group_id}",
        operation=disable_operation,
        lane="critical",
        priority=0,
        timeout_seconds=60.0,
    )
    try:
        if not submission.accepted:
            await callback.answer("权限任务队列正忙，请再次点击", show_alert=True)
        elif not submission.created:
            await callback.answer("爆破防护解除正在执行", show_alert=True)
        else:
            await callback.answer("正在解除爆破防护…")
    finally:
        callback_acknowledged.set()


async def _clear_style_samples(session: AsyncSession, group_id: int) -> None:
    from sqlalchemy import delete

    from bot.db.models import SpeechStyleSample

    await session.execute(
        delete(SpeechStyleSample).where(SpeechStyleSample.group_id == group_id)
    )


@router.message(Command("mimic"))
async def cmd_mimic(message: Message, session: AsyncSession, settings: Settings) -> None:
    if not await ensure_group_authorized(message, session, settings):
        return
    if not await ensure_group_admin_permission(message, session, settings):
        return

    arg = (message.text or "").partition(" ")[2].strip().lower()
    group_row = await _ensure_group_row(session, message.chat.id, message.chat.title or "")
    state = get_style_state(group_row.settings)

    if arg == "status":
        target_id = int(state.get("target_user_id") or 0)
        if not target_id:
            await _commit_settings(session)
            await _answer(message, settings, "<b>说话风格学习</b>\n当前未指定学习目标。")
            return
        profile = (state.get("profile_text") or "").strip()
        lines = [
            "<b>说话风格学习状态</b>",
            f"<b>目标</b>: {html.escape(str(state.get('target_user_name') or target_id))}"
            f"（<code>{target_id}</code>）",
            f"<b>已收集</b>: {int(state.get('sample_count') or 0)}/1000 条",
            f"<b>画像</b>: {'已生成（' + str(len(profile)) + ' 字）' if profile else '尚未生成（满 50 条后自动蒸馏）'}",
        ]
        await _commit_settings(session)
        await _answer(message, settings, "\n".join(lines))
        return

    if arg in {"off", "disable", "stop"}:
        group_row.settings = set_style_target(group_row.settings, user_id=0, user_name="")
        await _clear_style_samples(session, message.chat.id)
        await _commit_settings(session)
        await _answer(message, settings, "<b>说话风格学习</b>\n已停止学习并清除画像。")
        return

    reply = message.reply_to_message
    target = reply.from_user if reply else None
    if target is None or arg:
        await _commit_settings(session)
        await _answer(message, settings, _MIMIC_USAGE)
        return
    if target.is_bot:
        await _commit_settings(session)
        await _answer(message, settings, "不能学习 bot 的说话风格。")
        return

    group_row.settings = set_style_target(
        group_row.settings,
        user_id=target.id,
        user_name=target.full_name or target.username or str(target.id),
    )
    await _clear_style_samples(session, message.chat.id)
    await _commit_settings(session)
    shown = html.escape(target.full_name or target.username or str(target.id))
    await _answer(
        message,
        settings,
        "<b>说话风格学习</b>\n"
        f"已开始学习 <b>{shown}</b>（<code>{target.id}</code>）的说话风格。\n"
        "每约 50 条发言自动蒸馏刷新一次画像，总上限 1000 条。\n"
        "使用 /mimic status 查看进度，/mimic off 停止。",
    )


@router.message(Command("banlist"))
async def cmd_banlist(message: Message, session: AsyncSession, settings: Settings) -> None:
    if not await ensure_group_authorized(message, session, settings):
        return
    if not await ensure_super_admin(message, settings):
        return

    rows = await list_global_bans(session, limit=30)
    await session.commit()
    if not rows:
        await _answer(
            message,
            settings,
            render_data_brief("全局封禁名单", empty="当前没有全局封禁记录。"),
        )
        return

    lines: list[str] = []
    for index, row in enumerate(rows, start=1):
        reason = _truncate_text(row.reason or "-", 40)
        source = "资料审查" if row.source == "join_screening" else "手动"
        lines.extend(
            [
                f"<b>{index}.</b> <code>{row.user_id}</code>　{source}",
                html.escape(reason),
                "",
            ]
        )
    await _answer(
        message,
        settings,
        render_data_brief(
            "全局封禁名单",
            metadata={"范围": f"最近 <code>{len(rows)}</code> 条"},
            items=lines,
            footer="解封请使用 <code>/unban &lt;用户ID&gt;</code>。",
        ),
    )


@router.message(Command("addrule"))
async def cmd_addrule(
    message: Message,
    session: AsyncSession,
    settings: Settings,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    if not await ensure_group_authorized(message, session, settings):
        return
    if not await ensure_group_admin_permission(message, session, settings):
        return
    # Release the authorization snapshot before natural-language intent
    # detection. RuleManageSkill will open its own short write session only
    # after the LLM returns.
    await session.commit()

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

    user_id = int(getattr(message.from_user, "id", 0) or 0)
    sender_username = (getattr(message.from_user, "username", "") or "").strip()
    with privileged_request_scope(background=True):
        progress = await message.answer(
            render_progress_notice(
                "规则任务 · 执行中",
                completed="已受理请求",
                current="解析并更新群审核规则",
                next_step="写入结果并更新本消息",
                context=f"<b>请求</b>　{html.escape(_truncate_text(args, 160))}",
            ),
            parse_mode="HTML",
        )

    async def operation() -> str:
        if not await _queued_operator_can_manage_members(
            bot=message.bot,
            session_factory=session_factory,
            settings=settings,
            group_id=int(message.chat.id),
            user_id=user_id,
        ):
            return "<b>规则任务已取消</b>\n群授权或操作者权限已发生变化。"
        skill = _build_skill_service(settings)
        result = await skill.run_skill(
            "rule_manage",
            {"request_text": args},
            session=None,
            session_factory=session_factory,
            sender_user_id=user_id,
            sender_username=sender_username,
            sender_is_owner=bool(
                user_id and is_super_admin_user_id(user_id, settings)
            ),
            sender_is_tg_admin=True,
            message=message,
            chat_id=message.chat.id,
            current_user_text=args,
        )
        return result.summary

    submission = submit_privileged_task(
        key=f"rule-manage:{int(message.chat.id)}:{int(message.message_id)}",
        label=f"rule manage in {int(message.chat.id)}",
        operation=lambda: _run_result_job(
            progress_message=progress,
            operation=operation,
            task_title="规则任务",
        ),
        lane="policy",
        priority=100,
        timeout_seconds=180.0,
    )
    if not submission.accepted:
        await _publish_privileged_result(
            progress,
            "<b>规则任务未入队</b>\n规则处理队列正忙，请稍后重试。",
            status="rejected",
        )
    elif not submission.created:
        await _publish_privileged_result(
            progress,
            "<b>规则任务正在执行</b>\n相同请求未重复提交。",
            status="duplicate",
        )


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
    await session.commit()
    if not rules:
        await msg.edit_text(
            render_data_brief(
                "群审核规则",
                empty="当前没有规则。",
                footer="可使用 <code>/addrule</code> 添加规则。",
            ),
            reply_markup=None,
        )
        await callback.answer("列表已空")
        return

    text, keyboard = _build_rule_list_page(rules, page=page)
    try:
        await msg.edit_text(text, reply_markup=preserve_delete_button(msg, keyboard), disable_web_page_preview=True)
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
        await session.commit()
        await callback.answer("规则不存在或已删除", show_alert=True)
        return

    pattern_label = _truncate_text(rule.pattern or "", 24) or f"#{rule.id}"
    await session.delete(rule)
    await session.commit()

    stmt = (
        select(ModerationRule)
        .where(ModerationRule.group_id == msg.chat.id)
        .order_by(ModerationRule.id)
    )
    result = await session.execute(stmt)
    rules = list(result.scalars().all())
    await session.commit()
    if not rules:
        await msg.edit_text(
            render_data_brief(
                "群审核规则",
                empty="当前没有规则。",
                footer="可使用 <code>/addrule</code> 添加规则。",
            ),
            reply_markup=None,
        )
        await callback.answer(f"已删除: {pattern_label}")
        return

    total_pages = max(1, (len(rules) + _LIST_PAGE_SIZE - 1) // _LIST_PAGE_SIZE)
    page = min(max(page_hint, 0), total_pages - 1)
    text, keyboard = _build_rule_list_page(rules, page=page)
    try:
        await msg.edit_text(text, reply_markup=preserve_delete_button(msg, keyboard), disable_web_page_preview=True)
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

    rows = await list_authorized_groups(session, include_inactive=True)
    await session.commit()
    if not rows:
        await msg.edit_text(
            render_data_brief("已授权群组", empty="当前没有已授权群组。"),
            reply_markup=None,
        )
        await callback.answer("列表已空")
        return

    text, keyboard = _build_auth_group_list_page(rows, page=page)
    try:
        await msg.edit_text(text, reply_markup=preserve_delete_button(msg, keyboard), disable_web_page_preview=True)
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
    msg = callback.message
    if not msg or not msg.chat:
        await callback.answer("消息已失效", show_alert=True)
        return
    if not callback.data:
        await callback.answer()
        return
    if session is None:
        await callback.answer("会话未就绪，请重新 /adminlist", show_alert=True)
        return
    authorized = await is_group_authorized(session, int(msg.chat.id))
    await session.commit()
    if not authorized:
        await callback.answer("当前群组未授权", show_alert=True)
        return
    if not await _callback_user_is_super_admin(callback, settings):
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
    await session.commit()
    if not rows:
        await msg.edit_text(
            render_data_brief(
                "群管理授权列表",
                metadata={"群组": f"<code>{group_id}</code>"},
                empty="当前没有已授权群管理。",
            ),
            reply_markup=None,
        )
        await callback.answer("列表已空")
        return

    text, keyboard = _build_admin_list_page(rows, group_id=group_id, page=page)
    try:
        await msg.edit_text(text, reply_markup=preserve_delete_button(msg, keyboard), disable_web_page_preview=True)
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
            or_(UserWarning.count > 0, UserWarning.is_banned.is_(True)),
        )
        .order_by(UserWarning.is_banned.desc(), UserWarning.count.desc(), UserWarning.user_id.asc())
    )
    result = await session.execute(stmt)
    rows = list(result.scalars().all())
    await session.commit()
    if not rows:
        await msg.edit_text(
            render_data_brief(
                "当前群组警告名单",
                empty="暂无被警告或封禁用户。",
            ),
            reply_markup=None,
        )
        await callback.answer("列表已空")
        return

    threshold = max(1, settings.moderation.warn_threshold)
    text, keyboard = _build_warning_list_page(rows, threshold=threshold, page=page)
    try:
        await msg.edit_text(text, reply_markup=preserve_delete_button(msg, keyboard), disable_web_page_preview=True)
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
            await _commit_settings(session)
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
        await _commit_settings(session)
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
        await session.commit()
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
    await session.commit()
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
            await _commit_settings(session)
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
        await _commit_settings(session)
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
        await session.commit()
        await _answer(
            message,
            settings,
            "<b>回复静默设置</b>\n"
            f"<b>用户</b>: {_safe_user_label(target.id, target.full_name)}\n"
            "<b>状态</b>: 当前不在静默名单",
        )
        return

    await session.delete(existing)
    await session.commit()
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
        await _commit_settings(session)
        await _answer(
            message,
            settings,
            get_cooldown_status_text(
                group_settings,
                default_enabled=settings.bot.proactive_default_enabled,
                config=settings.bot,
            ),
        )
        return

    if args in {"on", "enable"}:
        group_row.settings = set_cooldown_task_enabled(
            group_settings,
            enabled=True,
            config=settings.bot,
        )
        await _commit_settings(session)
        await _answer(
            message,
            settings,
            get_cooldown_status_text(
                group_row.settings,
                default_enabled=settings.bot.proactive_default_enabled,
                config=settings.bot,
            ),
        )
        return

    if args in {"off", "disable"}:
        group_row.settings = set_cooldown_task_enabled(
            group_settings,
            enabled=False,
            config=settings.bot,
        )
        await _commit_settings(session)
        await _answer(
            message,
            settings,
            get_cooldown_status_text(
                group_row.settings,
                default_enabled=settings.bot.proactive_default_enabled,
                config=settings.bot,
            ),
        )
        return

    await _commit_settings(session)
    await _answer(message, settings, _PROACTIVE_USAGE)


@router.message(Command("tts"))
async def cmd_tts(message: Message, session: AsyncSession, settings: Settings) -> None:
    if not await ensure_group_authorized(message, session, settings):
        return
    if not await ensure_super_admin(message, settings):
        return

    args = (message.text or "").partition(" ")[2].strip().lower()
    if not message.chat or message.chat.type not in ("group", "supergroup"):
        await _answer(message, settings, "<b>TTS 设置</b>\n请在目标群内使用该命令。")
        return

    group_row = await _ensure_group_row(session, message.chat.id, message.chat.title or "")
    group_settings = dict(group_row.settings or {})
    tts_service = DoubaoTTSService(settings)

    if not args:
        await _commit_settings(session)
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
        "enable": TTS_MODE_ON,
        "disable": TTS_MODE_OFF,
        "always": TTS_MODE_ALWAYS,
    }
    target_mode = mode_map.get(args)
    if target_mode is None:
        await _commit_settings(session)
        await _answer(message, settings, _TTS_USAGE)
        return

    if target_mode in {TTS_MODE_ON, TTS_MODE_ALWAYS} and not tts_service.available:
        await _commit_settings(session)
        await _answer(
            message,
            settings,
            "<b>TTS 设置</b>\nDoubao TTS 尚未配置完成，请先补齐 .env 中的 DOUBAO_TTS_* 配置。",
        )
        return

    group_row.settings = set_tts_mode(group_settings, target_mode)
    await _commit_settings(session)
    await _answer(
        message,
        settings,
        build_tts_status_text(
            group_id=message.chat.id,
            group_settings=group_row.settings,
            service_ready=tts_service.available,
        ),
    )


@router.message(Command("atreply"))
async def cmd_atreply(message: Message, session: AsyncSession, settings: Settings) -> None:
    if not await ensure_group_authorized(message, session, settings):
        return
    if not await ensure_super_admin(message, settings):
        return

    args = (message.text or "").partition(" ")[2].strip().lower()
    if not message.chat or message.chat.type not in ("group", "supergroup"):
        await _answer(message, settings, "<b>仅@回复设置</b>\n请在目标群内使用该命令。")
        return

    group_row = await _ensure_group_row(session, message.chat.id, message.chat.title or "")
    group_settings = dict(group_row.settings or {})

    if not args:
        await _commit_settings(session)
        await _answer(
            message,
            settings,
            build_at_reply_status_text(
                group_id=message.chat.id,
                group_settings=group_settings,
            ),
        )
        return

    mode_map = {
        "enable": True,
        "disable": False,
    }
    target_enabled = mode_map.get(args)
    if target_enabled is None:
        await _commit_settings(session)
        await _answer(message, settings, _AT_REPLY_USAGE)
        return

    group_row.settings = set_at_reply_enabled(group_settings, target_enabled)
    await _commit_settings(session)
    await _answer(
        message,
        settings,
        build_at_reply_status_text(
            group_id=message.chat.id,
            group_settings=group_row.settings,
        ),
    )


@router.message(Command("clearwarnings", "clearwarning", "clearwarns", "clearwarn"))
async def cmd_clearwarnings(
    message: Message,
    session: AsyncSession,
    settings: Settings,
) -> None:
    if not await ensure_group_authorized(message, session, settings):
        return
    if not await ensure_group_admin_permission(message, session, settings):
        return

    args = (message.text or "").partition(" ")[2].strip()
    target_id, _ = _resolve_ban_target(message, args)
    if target_id is None:
        await _answer(message, settings, _CLEAR_WARNINGS_USAGE)
        return

    cleared_count, was_banned = await _clear_user_warning(
        session,
        message.chat.id,
        target_id,
    )
    await session.commit()
    status = (
        f"累计违规次数已清零（原为 {cleared_count} 次）"
        if cleared_count
        else "原本无违规次数"
    )
    lines = [
        "<b>违规次数清除结果</b>",
        f"<b>用户ID</b>: <code>{target_id}</code>",
        f"<b>状态</b>: {status}",
    ]
    if was_banned:
        lines.append("该用户的本群封禁状态已保留；如需解封请使用 /unban。")
    await _answer(message, settings, "\n".join(lines))


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
            or_(UserWarning.count > 0, UserWarning.is_banned.is_(True)),
        )
        .order_by(UserWarning.is_banned.desc(), UserWarning.count.desc(), UserWarning.user_id.asc())
    )
    result = await session.execute(stmt)
    rows = list(result.scalars().all())
    await session.commit()

    if not rows:
        await _answer(
            message,
            settings,
            render_data_brief(
                "当前群组警告名单",
                empty="暂无被警告或封禁用户。",
            ),
        )
        return

    threshold = max(1, settings.moderation.warn_threshold)
    text, keyboard = _build_warning_list_page(rows, threshold=threshold, page=0)
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

    stmt = select(ModerationExemption).where(
        ModerationExemption.group_id == message.chat.id,
        ModerationExemption.user_id == target.id,
    )
    result = await session.execute(stmt)
    existing = result.scalar_one_or_none()
    if existing is None:
        session.add(
            ModerationExemption(
                group_id=message.chat.id,
                user_id=target.id,
                created_by=(message.from_user.id if message.from_user else 0),
            )
        )
    recovery = await lease_join_verification_for_unban(
        session,
        int(message.chat.id),
        int(target.id),
        manual_unban=False,
    )
    if recovery is None:
        await session.rollback()
        await _answer(message, settings, "无法建立豁免恢复工单，请立即重试。")
        return
    await session.commit()
    activate_manual_unban_recovery(recovery)
    released = await release_moderation_restriction_after_exemption(
        message.bot,
        session,
        recovery,
    )
    state = "已在豁免名单中" if existing is not None else "已开启豁免"
    if not released:
        state += "；旧限制正在由恢复任务继续校准"
    await _answer(message, settings, 
        "<b>AI 审查豁免</b>\n"
        f"<b>用户</b>: {_safe_user_label(target.id, target.full_name)}\n"
        f"<b>状态</b>: {state}"
    )


@router.message(Command("unaiexempt"))
async def cmd_unaiexempt(message: Message, session: AsyncSession, settings: Settings) -> None:
    if not await ensure_group_authorized(message, session, settings):
        return
    if not await ensure_group_admin_permission(message, session, settings):
        return

    reply = message.reply_to_message
    target = reply.from_user if reply else None
    target_id: int | None = target.id if target else None
    target_name = target.full_name if target else None
    # ID form covers the spam-incident flow where the bot's messages were
    # already deleted and there is nothing left to reply to.
    target_is_bot = bool(target and target.is_bot)
    if target_id is None:
        args = (message.text or "").partition(" ")[2].strip()
        first = args.split()[0] if args else ""
        try:
            target_id = int(first)
        except ValueError:
            target_id = None
        target_is_bot = True  # ID form cannot verify; try both tables.
    if target_id is None:
        await _answer(message, settings,
            "<b>命令用法</b>\n"
            "回复目标用户的一条消息，或使用 /unaiexempt &lt;用户ID&gt;"
        )
        return

    # For bot senders this also revokes an earned screening whitelist, so the
    # bot's next messages go back through moderation.
    whitelist_revoked = False
    if target_is_bot:
        whitelist_revoked = await remove_bot_whitelist(
            session, message.chat.id, target_id
        )

    stmt = select(ModerationExemption).where(
        ModerationExemption.group_id == message.chat.id,
        ModerationExemption.user_id == target_id,
    )
    result = await session.execute(stmt)
    existing = result.scalar_one_or_none()
    if not existing and not whitelist_revoked:
        # A zero-row whitelist UPDATE still acquired the SQLite write lock.
        await session.commit()
        await _answer(message, settings,
            "<b>AI 审查豁免</b>\n"
            f"<b>用户</b>: {_safe_user_label(target_id, target_name)}\n"
            "<b>状态</b>: 当前不在豁免名单"
        )
        return

    if existing:
        await session.delete(existing)
    # The whitelist UPDATE holds the process-wide SQLite write lock until
    # commit; release it before the Telegram reply instead of stalling every
    # concurrent write for the duration of the network call.
    await session.commit()
    status_lines = []
    if existing:
        status_lines.append("已取消豁免")
    if whitelist_revoked:
        status_lines.append("已撤销 bot 审核白名单，恢复审核")
    await _answer(message, settings,
        "<b>AI 审查豁免</b>\n"
        f"<b>用户</b>: {_safe_user_label(target_id, target_name)}\n"
        f"<b>状态</b>: {'；'.join(status_lines)}"
    )
