from __future__ import annotations

import html
import logging

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from bot.config import Settings
from bot.db.models import Group, ModerationExemption, ModerationRule, ReplyMute, UserWarning
from bot.services.at_reply import build_at_reply_status_text, set_at_reply_enabled
from bot.services.authz import (
    authorize_group,
    authorize_group_admin,
    deauthorize_group,
    deauthorize_group_admin,
    ensure_group_admin_permission,
    ensure_group_authorized,
    ensure_super_admin,
    is_group_admin_authorized,
    is_super_admin_user_id,
    list_authorized_groups,
    list_group_admins,
)
from bot.services.join_screening import (
    add_global_ban,
    list_global_bans,
    remove_global_ban,
)
from bot.services.join_verification import (
    delete_join_verifications_for_user,
    restore_member_permissions,
)
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
from bot.services.skills import SkillService
from bot.utils.telegram import answer_with_auto_delete, is_group

router = Router()
log = logging.getLogger(__name__)

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
    "封禁：加入封禁名单并立即在本群封禁；此后其任何消息都会被自动删除。"
)
_UNBAN_USAGE = (
    "<b>命令用法</b>\n"
    "1. 回复目标用户消息后发送 /unban\n"
    "2. /unban &lt;用户ID&gt;\n\n"
    "解封：移出封禁名单、清空违规次数，并永久豁免资料审查（消息仍会被正常审核）。"
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


async def _authorized_group_ids(session: AsyncSession) -> list[int]:
    rows = await list_authorized_groups(session)
    return [int(row.group_id) for row in rows]


async def _clear_user_warning(
    session: AsyncSession,
    group_id: int,
    user_id: int,
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
    await session.delete(warning)
    return previous_count, was_banned


@router.message(Command("ban"))
async def cmd_ban(message: Message, session: AsyncSession, settings: Settings) -> None:
    if not await ensure_group_authorized(message, session, settings):
        return
    if not await ensure_super_admin(message, settings):
        return

    args = (message.text or "").partition(" ")[2].strip()
    target_id, reason = _resolve_ban_target(message, args)
    if target_id is None:
        await _answer(message, settings, _BAN_USAGE)
        return
    if is_super_admin_user_id(target_id, settings):
        await _answer(message, settings, "不能封禁最高管理员。")
        return

    operator = message.from_user
    created = await add_global_ban(
        session,
        target_id,
        reason=reason or "手动封禁",
        source="manual",
        created_by=operator.id if operator else 0,
    )
    await delete_join_verifications_for_user(session, target_id)
    group_ids = await _authorized_group_ids(session)
    # Publish the registry entry and challenge cancellation before Telegram
    # calls so an in-flight CF callback cannot restore a manually banned user.
    await session.commit()

    banned_groups = 0
    for group_id in group_ids:
        try:
            await message.bot.ban_chat_member(group_id, target_id)
            banned_groups += 1
        except Exception as exc:
            log.info("ban failed | group=%s user=%s error=%s", group_id, target_id, exc)

    status = "已加入封禁名单" if created else "已在封禁名单（信息已更新）"
    lines = [
        "<b>封禁结果</b>",
        f"<b>用户ID</b>: <code>{target_id}</code>",
        f"<b>状态</b>: {status}",
    ]
    if reason:
        lines.append(f"<b>原因</b>: {html.escape(reason)}")
    lines.append(f"<b>群内封禁</b>: {banned_groups}/{len(group_ids)} 个授权群成功")
    lines.append("该用户此后发言或再入群都会被自动封禁。")
    await _answer(message, settings, "\n".join(lines))


@router.message(Command("unban"))
async def cmd_unban(message: Message, session: AsyncSession, settings: Settings) -> None:
    if not await ensure_group_authorized(message, session, settings):
        return
    if not await ensure_super_admin(message, settings):
        return

    args = (message.text or "").partition(" ")[2].strip()
    target_id, _ = _resolve_ban_target(message, args)
    if target_id is None:
        await _answer(message, settings, _UNBAN_USAGE)
        return

    operator = message.from_user
    removed = await remove_global_ban(
        session,
        target_id,
        operator_id=operator.id if operator else 0,
    )
    await delete_join_verifications_for_user(session, target_id)
    group_ids = await _authorized_group_ids(session)
    cleared_total = 0
    for group_id in group_ids:
        cleared_count, _ = await _clear_user_warning(session, group_id, target_id)
        cleared_total += cleared_count
    await session.commit()

    unbanned_groups = 0
    restored_groups = 0
    for group_id in group_ids:
        try:
            await message.bot.unban_chat_member(group_id, target_id, only_if_banned=True)
            unbanned_groups += 1
            if await restore_member_permissions(message.bot, group_id, target_id):
                restored_groups += 1
        except Exception as exc:
            log.info("unban failed | group=%s user=%s error=%s", group_id, target_id, exc)

    status = "已移出封禁名单" if removed else "原本不在封禁名单"
    lines = [
        "<b>解封结果</b>",
        f"<b>用户ID</b>: <code>{target_id}</code>",
        f"<b>状态</b>: {status}",
        f"<b>群内解封</b>: {unbanned_groups}/{len(group_ids)} 个授权群成功",
        f"<b>发言权限恢复</b>: {restored_groups} 个群成功",
        (
            f"<b>违规次数</b>: 已清零（累计 {cleared_total} 次）"
            if cleared_total
            else "<b>违规次数</b>: 原本无记录"
        ),
        "<b>资料审查</b>: 已永久豁免（消息仍会被正常审核）",
    ]
    await _answer(message, settings, "\n".join(lines))


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
        await _answer(message, settings, "\n".join(lines))
        return

    if arg in {"off", "disable", "stop"}:
        group_row.settings = set_style_target(group_row.settings, user_id=0, user_name="")
        await _clear_style_samples(session, message.chat.id)
        await _answer(message, settings, "<b>说话风格学习</b>\n已停止学习并清除画像。")
        return

    reply = message.reply_to_message
    target = reply.from_user if reply else None
    if target is None or arg:
        await _answer(message, settings, _MIMIC_USAGE)
        return
    if target.is_bot:
        await _answer(message, settings, "不能学习 bot 的说话风格。")
        return

    group_row.settings = set_style_target(
        group_row.settings,
        user_id=target.id,
        user_name=target.full_name or target.username or str(target.id),
    )
    await _clear_style_samples(session, message.chat.id)
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
    if not rows:
        await _answer(message, settings, "<b>封禁名单</b>\n当前为空。")
        return

    lines = ["<b>封禁名单</b>（最近 30 条）"]
    for row in rows:
        reason = _truncate_text(row.reason or "-", 40)
        source = "资料审查" if row.source == "join_screening" else "手动"
        lines.append(
            f"• <code>{row.user_id}</code> | {source} | {html.escape(reason)}"
        )
    lines.append("\n解封请使用 /unban &lt;用户ID&gt;")
    await _answer(message, settings, "\n".join(lines))


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

    user_id = int(getattr(message.from_user, "id", 0) or 0)
    sender_username = (getattr(message.from_user, "username", "") or "").strip()
    skill = _build_skill_service(settings)
    result = await skill.run_skill(
        "rule_manage",
        {"request_text": args},
        session=session,
        sender_user_id=user_id,
        sender_username=sender_username,
        sender_is_owner=bool(user_id and is_super_admin_user_id(user_id, settings)),
        sender_is_tg_admin=True,
        message=message,
        chat_id=message.chat.id,
        current_user_text=args,
    )
    await _answer(message, settings, result.summary)


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
    await session.commit()

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
    msg = callback.message
    if not msg or not msg.chat:
        await callback.answer("消息已失效", show_alert=True)
        return
    if not await ensure_group_authorized(msg, session, settings):
        await callback.answer("当前群组未授权", show_alert=True)
        return
    if not callback.data:
        await callback.answer()
        return
    if session is None:
        await callback.answer("会话未就绪，请重新 /adminlist", show_alert=True)
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
        lines.append("该用户此前已达封禁状态；本命令不会解封，请使用 /unban。")
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
