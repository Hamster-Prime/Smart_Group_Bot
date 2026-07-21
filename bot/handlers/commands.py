from __future__ import annotations

import html
import logging
import re

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import (
    BufferedInputFile,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputMediaPhoto,
    Message,
    WebAppInfo,
)
from sqlalchemy.exc import IntegrityError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from bot.config import Settings
from bot.db.models import Admin, AuthorizedGroup, Group
from bot.services.authz import (
    ensure_group_admin_permission,
    ensure_group_authorized,
    ensure_super_admin,
    is_group_admin_authorized,
    is_group_authorized,
    is_super_admin_user_id,
)
from bot.services import memory_holder
from bot.services.av_search import (
    AVDetail,
    AVQuerySession,
    AVQuerySessionStore,
    AVSearchItem,
    AVSearchService,
    is_av_code_query,
)
from bot.services.join_verification import (
    maybe_send_private_verification,
    parse_private_verify_group_id,
)
from bot.services.llm import LLMService
from bot.services.group_settings import acquire_group_settings_write_intent
from bot.services.skills import SkillService
from bot.services.skills.platform_common import fetch_bytes
from bot.utils.command_catalog import build_help_text
from bot.utils.telegram import (
    answer_with_auto_delete,
    configured_auto_delete_seconds,
    is_group,
    preserve_delete_button,
    typing_action,
)

router = Router()
log = logging.getLogger(__name__)
_AV_SEARCH_PAGE_SIZE = 6
_AV_SEED_PAGE_SIZE = 1
_LIST_PAGE_SIZE = 5
_AV_SESSION_STORE = AVQuerySessionStore(ttl_seconds=15 * 60, max_sessions=256)
_AV_GROUP_ENABLE_KEY = "av_enabled"


async def _answer(
    message: Message,
    settings: Settings,
    text: str,
    *,
    auto_delete_seconds: int | None = None,
    **kwargs: object,
) -> None:
    await answer_with_auto_delete(
        message,
        text,
        auto_delete_seconds=(
            configured_auto_delete_seconds(settings, "management")
            if auto_delete_seconds is None
            else auto_delete_seconds
        ),
        **kwargs,
    )


async def _ensure_group_row(session: AsyncSession, group_id: int, title: str) -> Group:
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


def _is_group_av_enabled(group_settings: dict | None) -> bool:
    settings_dict = group_settings if isinstance(group_settings, dict) else {}
    value = settings_dict.get(_AV_GROUP_ENABLE_KEY)
    if value is None:
        return False
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"1", "true", "yes", "on", "enabled"}:
            return True
        if normalized in {"0", "false", "no", "off", "disabled"}:
            return False
    return bool(value)


def _truncate_text(text: str, max_len: int) -> str:
    cleaned = (text or "").replace("\n", " ").strip()
    if len(cleaned) <= max_len:
        return cleaned
    return cleaned[:max_len] + "..."


def _source_name(source: str) -> str:
    val = (source or "").strip().lower()
    if val == "javbus":
        return "JAVBUS"
    if val == "madouqu":
        return "MADOUQU"
    if val == "dmm":
        return "DMM"
    if val == "fc2":
        return "FC2"
    return val.upper() or "UNKNOWN"


def _parse_int(value: str, *, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


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


def _build_memory_list_page(items: list[object], *, page: int) -> tuple[str, InlineKeyboardMarkup | None]:
    total = len(items)
    total_pages = max(1, (total + _LIST_PAGE_SIZE - 1) // _LIST_PAGE_SIZE)
    page = min(max(page, 0), total_pages - 1)
    start = page * _LIST_PAGE_SIZE
    end = min(start + _LIST_PAGE_SIZE, total)

    if not items:
        return "<b>永久记忆</b>\n当前为空。", None

    lines = [
        "<b>永久记忆</b>",
        f"共 {total} 条 | 页码: {page + 1}/{total_pages}",
        "",
        "点击下方按钮可删除对应记忆：",
        "",
    ]
    keyboard_rows: list[list[InlineKeyboardButton]] = []
    for idx, item in enumerate(items[start:end], start=start + 1):
        memory_id = int(getattr(item, "id", 0) or 0)
        preview = html.escape(_truncate_text(str(getattr(item, "content", "") or ""), 120))
        lines.append(f"{idx}. <b>#{memory_id}</b> {preview}")
        lines.append("")
        keyboard_rows.append(
            [
                InlineKeyboardButton(
                    text=f"🗑 删除记忆 #{memory_id}",
                    callback_data=f"lmd:{memory_id}:{page}",
                )
            ]
        )

    nav_row: list[InlineKeyboardButton] = []
    if page > 0:
        nav_row.append(InlineKeyboardButton(text="⬅️ 上一页", callback_data=f"lml:{page - 1}"))
    if page < total_pages - 1:
        nav_row.append(InlineKeyboardButton(text="下一页 ➡️", callback_data=f"lml:{page + 1}"))
    if nav_row:
        keyboard_rows.append(nav_row)
    return "\n".join(lines).rstrip(), InlineKeyboardMarkup(inline_keyboard=keyboard_rows)


async def _callback_user_can_manage_memories(
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
    await session.commit()
    if locally_authorized:
        return True

    await callback.answer("仅群管理员可操作该列表", show_alert=True)
    return False


async def _ensure_callback_group_authorized(
    callback: CallbackQuery,
    message: Message,
    session: AsyncSession,
) -> bool:
    chat = getattr(message, "chat", None)
    if chat is None or getattr(chat, "type", "") not in {"group", "supergroup"}:
        return True
    authorized = await is_group_authorized(session, int(chat.id))
    await session.commit()
    if authorized:
        return True
    await callback.answer("当前群组未授权", show_alert=True)
    return False


def _av_session_owner_ok(session: AVQuerySession, user_id: int) -> bool:
    if session.owner_user_id <= 0:
        return True
    return session.owner_user_id == user_id


async def _ensure_av_callback_scope(
    callback: CallbackQuery,
    message: Message,
    session: AsyncSession,
    settings: Settings,
) -> bool:
    if message.chat and message.chat.type in ("group", "supergroup"):
        group_row = await _ensure_group_row(
            session,
            message.chat.id,
            message.chat.title or "",
        )
        group_av_enabled = _is_group_av_enabled(group_row.settings)
        await session.commit()
        if not group_av_enabled:
            await callback.answer("当前群组未启用 AV 查询", show_alert=True)
            return False
        return True

    await session.commit()
    user = callback.from_user
    if user is None or not is_super_admin_user_id(user.id, settings):
        await callback.answer("私聊仅最高管理员可使用 AV 查询", show_alert=True)
        return False
    return True


def _build_av_search_page(
    session: AVQuerySession,
    *,
    page: int,
) -> tuple[str, InlineKeyboardMarkup]:
    total = len(session.results)
    total_pages = max(1, (total + _AV_SEARCH_PAGE_SIZE - 1) // _AV_SEARCH_PAGE_SIZE)
    page = min(max(page, 0), total_pages - 1)
    start = page * _AV_SEARCH_PAGE_SIZE
    end = min(start + _AV_SEARCH_PAGE_SIZE, total)

    lines = [
        "<b>AV 搜索结果</b>",
        f"关键词: {html.escape(_truncate_text(session.query, 80))}",
        f"结果: {total} 条",
        f"页码: {page + 1}/{total_pages}",
        "",
        "点击下方按钮查看详情：",
        "",
    ]
    for idx, item in enumerate(session.results[start:end], start=start + 1):
        code = html.escape(item.code or "-")
        title = html.escape(_truncate_text(item.title or "-", 56))
        source = html.escape(_source_name(item.source))
        date = html.escape(item.date or "-")
        lines.append(f"<b>#{idx}</b> [{source}] {code}")
        lines.append(f"{title}")
        lines.append(f"日期: {date}")
        lines.append("")

    keyboard_rows: list[list[InlineKeyboardButton]] = []
    for idx, item in enumerate(session.results[start:end], start=start):
        source_key = (item.source or "").lower()
        if source_key == "javbus":
            source_short = "J"
        elif source_key == "madouqu":
            source_short = "M"
        elif source_key == "dmm":
            source_short = "D"
        elif source_key == "fc2":
            source_short = "F"
        else:
            source_short = "?"
        label = item.code or _truncate_text(item.title, 20)
        btn_text = _truncate_text(f"{idx + 1}. [{source_short}] {label}", 60)
        keyboard_rows.append(
            [
                InlineKeyboardButton(
                    text=btn_text,
                    callback_data=f"avd:{session.token}:{idx}",
                )
            ]
        )

    nav_row: list[InlineKeyboardButton] = []
    if page > 0:
        nav_row.append(
            InlineKeyboardButton(
                text="⬅️ 上一页",
                callback_data=f"avs:{session.token}:{page - 1}",
            )
        )
    if page < total_pages - 1:
        nav_row.append(
            InlineKeyboardButton(
                text="下一页 ➡️",
                callback_data=f"avs:{session.token}:{page + 1}",
            )
        )
    if nav_row:
        keyboard_rows.append(nav_row)

    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=keyboard_rows)


def _build_av_detail_caption(detail: AVDetail) -> str:
    lines = [
        "<b>影片详情</b>",
        f"<b>来源</b>: {html.escape(_source_name(detail.source))}",
        f"<b>番号</b>: {html.escape(detail.code or '未知')}",
        f"<b>标题</b>: {html.escape(_truncate_text(detail.title or '-', 70))}",
    ]
    if detail.date:
        lines.append(f"<b>发行日期</b>: {html.escape(detail.date)}")
    if detail.runtime:
        lines.append(f"<b>时长</b>: {html.escape(detail.runtime)}")
    if detail.score:
        lines.append(f"<b>评分</b>: {html.escape(detail.score)}")
    if detail.studio:
        lines.append(f"<b>制作商</b>: {html.escape(_truncate_text(detail.studio, 60))}")
    if detail.publisher:
        lines.append(f"<b>发行商</b>: {html.escape(_truncate_text(detail.publisher, 60))}")
    if detail.series:
        lines.append(f"<b>系列</b>: {html.escape(_truncate_text(detail.series, 60))}")
    if detail.actors:
        lines.append(f"<b>演员</b>: {html.escape(_truncate_text(' / '.join(detail.actors), 80))}")
    if detail.genres:
        lines.append(f"<b>类型</b>: {html.escape(_truncate_text(' / '.join(detail.genres), 80))}")
    if detail.seeds:
        lines.append(f"<b>种子</b>: {len(detail.seeds)} 条（可翻页）")
    else:
        lines.append("<b>种子</b>: 无")
    if detail.url:
        lines.append(f"<b>详情页</b>: {html.escape(_truncate_text(detail.url, 100))}")
    return "\n".join(lines)


def _build_av_detail_text(detail: AVDetail) -> str:
    lines = [
        "<b>影片详情</b>",
        f"<b>来源</b>: {html.escape(_source_name(detail.source))}",
        f"<b>番号</b>: {html.escape(detail.code or '未知')}",
        f"<b>标题</b>: {html.escape(detail.title or '-')}",
    ]

    if detail.date:
        lines.append(f"<b>发行日期</b>: {html.escape(detail.date)}")
    if detail.runtime:
        lines.append(f"<b>时长</b>: {html.escape(detail.runtime)}")
    if detail.score:
        lines.append(f"<b>评分</b>: {html.escape(detail.score)}")
    if detail.director:
        lines.append(f"<b>导演</b>: {html.escape(detail.director)}")
    if detail.studio:
        lines.append(f"<b>制作商</b>: {html.escape(detail.studio)}")
    if detail.publisher:
        lines.append(f"<b>发行商</b>: {html.escape(detail.publisher)}")
    if detail.series:
        lines.append(f"<b>系列</b>: {html.escape(detail.series)}")
    if detail.actors:
        lines.append(f"<b>演员</b>: {html.escape(' / '.join(detail.actors))}")
    if detail.genres:
        lines.append(f"<b>类型</b>: {html.escape(' / '.join(detail.genres))}")
    if detail.summary:
        lines.append(f"<b>简介</b>: {html.escape(_truncate_text(detail.summary, 360))}")

    if detail.seeds:
        lines.append(f"<b>种子</b>: {len(detail.seeds)} 条（点下方按钮浏览）")
    else:
        lines.append("<b>种子</b>: 无")
    lines.append(f"<b>详情页</b>: {html.escape(detail.url)}")
    return "\n".join(lines)


def _build_av_detail_keyboard(
    *,
    session: AVQuerySession,
    result_idx: int,
    detail: AVDetail,
) -> InlineKeyboardMarkup | None:
    rows: list[list[InlineKeyboardButton]] = []
    if detail.seeds:
        rows.append(
            [
                InlineKeyboardButton(
                    text=f"🧲 浏览种子 ({len(detail.seeds)})",
                    callback_data=f"avm:{session.token}:{result_idx}:0",
                )
            ]
        )
    if detail.url.startswith("http"):
        rows.append([InlineKeyboardButton(text="🌐 打开详情页", url=detail.url)])
    if not rows:
        return None
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _build_av_seed_page(
    *,
    session: AVQuerySession,
    result_idx: int,
    detail: AVDetail,
    page: int,
) -> tuple[str, InlineKeyboardMarkup | None]:
    if not detail.seeds:
        return "<b>种子列表</b>\n无", None

    total = len(detail.seeds)
    total_pages = max(1, (total + _AV_SEED_PAGE_SIZE - 1) // _AV_SEED_PAGE_SIZE)
    page = min(max(page, 0), total_pages - 1)
    start = page * _AV_SEED_PAGE_SIZE
    end = min(start + _AV_SEED_PAGE_SIZE, total)

    seed = detail.seeds[start] if start < len(detail.seeds) else None
    lines = [
        "<b>种子列表</b>",
        f"<b>番号</b>: {html.escape(detail.code or '未知')}",
        f"<b>来源</b>: {html.escape(_source_name(detail.source))}",
        f"<b>页码</b>: {page + 1}/{total_pages}",
        "",
    ]
    if seed:
        title = html.escape(_truncate_text(seed.title or "Magnet", 120))
        size = html.escape(seed.size or "-")
        date = html.escape(seed.date or "-")
        lines.append(f"{start + 1}. <b>{title}</b>")
        lines.append(f"大小: {size} | 日期: {date}")
        lines.append(f"<code>{html.escape(_truncate_text(seed.magnet or '-', 260))}</code>")
    else:
        lines.append("无")

    rows: list[list[InlineKeyboardButton]] = []
    nav_row: list[InlineKeyboardButton] = []
    if page > 0:
        nav_row.append(
            InlineKeyboardButton(
                text="⬅️ 上一页",
                callback_data=f"avm:{session.token}:{result_idx}:{page - 1}",
            )
        )
    if page < total_pages - 1:
        nav_row.append(
            InlineKeyboardButton(
                text="下一页 ➡️",
                callback_data=f"avm:{session.token}:{result_idx}:{page + 1}",
            )
        )
    if nav_row:
        rows.append(nav_row)

    rows.append(
        [
            InlineKeyboardButton(
                text="⬅️ 返回详情",
                callback_data=f"avd:{session.token}:{result_idx}",
            )
        ]
    )

    if detail.url.startswith("http"):
        rows.append([InlineKeyboardButton(text="🌐 打开详情页", url=detail.url)])

    keyboard = InlineKeyboardMarkup(inline_keyboard=rows) if rows else None
    return "\n".join(lines).rstrip(), keyboard


async def _download_cover_input_file(cover_url: str, referer: str = "") -> BufferedInputFile | None:
    url = (cover_url or "").strip()
    if not url.startswith("http"):
        return None

    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ),
        "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
    }
    if referer:
        headers["Referer"] = referer

    try:
        status, raw, _final_url, ctype = await fetch_bytes(
            url,
            headers=headers,
            timeout_sec=15.0,
            allowed_content_types=("image/",),
            max_response_bytes=15 * 1024 * 1024,
        )
        if status >= 400 or not raw:
            return None
        ext = ".jpg"
        if "png" in ctype:
            ext = ".png"
        elif "webp" in ctype:
            ext = ".webp"
        return BufferedInputFile(raw, filename=f"av_cover{ext}")
    except Exception:
        log.exception("failed to download cover: %s", url)
        return None


async def _edit_message_as_photo(
    *,
    message: Message,
    cover_url: str,
    caption: str,
    keyboard: InlineKeyboardMarkup | None,
    referer: str = "",
) -> bool:
    if not cover_url:
        return False

    try:
        media = InputMediaPhoto(media=cover_url, caption=caption, parse_mode="HTML")
        await message.edit_media(media=media, reply_markup=keyboard)
        return True
    except TelegramBadRequest as exc:
        # Some sources block Telegram fetch; retry by uploading bytes.
        log.warning("edit_media by url failed: %s", exc)
    except Exception:
        log.exception("edit_media by url failed")

    file_obj = await _download_cover_input_file(cover_url, referer=referer)
    if not file_obj:
        return False

    try:
        media = InputMediaPhoto(media=file_obj, caption=caption, parse_mode="HTML")
        await message.edit_media(media=media, reply_markup=keyboard)
        return True
    except Exception:
        log.exception("edit_media by uploaded file failed")
        return False


async def _edit_av_detail_in_place(
    *,
    message: Message,
    detail: AVDetail,
    keyboard: InlineKeyboardMarkup | None,
) -> bool:
    caption = _build_av_detail_caption(detail)
    detail_text = _build_av_detail_text(detail)

    # If current message is photo, edit caption first to keep same media message.
    if getattr(message, "photo", None):
        try:
            await message.edit_caption(caption=caption, reply_markup=keyboard)
            return True
        except Exception:
            log.debug("edit_caption for detail failed, will try media refresh")

    # Try switching message to photo+caption (URL first, then uploaded bytes).
    media_ok = await _edit_message_as_photo(
        message=message,
        cover_url=detail.cover_url,
        caption=caption,
        keyboard=keyboard,
        referer=detail.url,
    )
    if media_ok:
        return True

    # Photo message cannot be edited via edit_text.
    if getattr(message, "photo", None):
        return False

    # Fallback to text edit for non-media messages.
    try:
        await message.edit_text(detail_text, reply_markup=keyboard, disable_web_page_preview=True)
        return True
    except Exception:
        log.exception("failed to edit message as text detail")
        return False


async def _edit_av_seed_in_place(
    *,
    message: Message,
    detail: AVDetail,
    text: str,
    keyboard: InlineKeyboardMarkup | None,
) -> bool:
    # Keep seed browsing in the same message.
    if getattr(message, "photo", None):
        try:
            await message.edit_caption(caption=text, reply_markup=keyboard)
            return True
        except Exception:
            log.debug("edit_caption for seed failed, will try media refresh")
        media_ok = await _edit_message_as_photo(
            message=message,
            cover_url=detail.cover_url,
            caption=text,
            keyboard=keyboard,
            referer=detail.url,
        )
        if media_ok:
            return True
        return False

    try:
        await message.edit_text(text, reply_markup=keyboard, disable_web_page_preview=True)
        return True
    except Exception:
        log.exception("failed to edit seed page in place")
        return False


async def _send_av_detail(
    *,
    message: Message,
    session: AVQuerySession,
    result_idx: int,
    detail: AVDetail,
    in_place: bool = False,
) -> bool:
    caption = _build_av_detail_caption(detail)
    detail_text = _build_av_detail_text(detail)
    keyboard = _build_av_detail_keyboard(session=session, result_idx=result_idx, detail=detail)

    if in_place:
        return await _edit_av_detail_in_place(message=message, detail=detail, keyboard=keyboard)

    sent_photo: Message | None = None
    if detail.cover_url:
        try:
            sent_photo = await message.answer_photo(
                photo=detail.cover_url,
                caption=caption,
                reply_markup=keyboard,
            )
            return True
        except TelegramBadRequest as exc:
            # Some source URLs are blocked for Telegram fetch or return non-image content.
            log.warning("send_photo by url failed: %s", exc)
        except Exception:
            log.exception("failed to send av cover photo: %s", detail.cover_url)

    if detail.cover_url and not sent_photo:
        file_obj = await _download_cover_input_file(detail.cover_url, referer=detail.url)
        if file_obj:
            try:
                sent_photo = await message.answer_photo(
                    photo=file_obj,
                    caption=caption,
                    reply_markup=keyboard,
                )
                return True
            except Exception:
                log.exception("failed to send av cover photo by upload")

    if not sent_photo:
        sent = await message.answer(
            detail_text,
            reply_markup=keyboard,
            disable_web_page_preview=True,
        )
        return True
    return False



@router.message(Command("start"))
async def cmd_start(message: Message, session: AsyncSession, settings: Settings) -> None:
    if not await ensure_group_authorized(message, session, settings):
        return
    # New members arrive here via the group prompt's deep link; hand out the
    # exact group's one-time challenge instead of the generic welcome.
    if message.chat and message.chat.type == "private":
        command_parts = str(message.text or "").split(maxsplit=1)
        payload = command_parts[1] if len(command_parts) == 2 else ""
        if await maybe_send_private_verification(
            message,
            session,
            settings,
            group_id=parse_private_verify_group_id(payload),
        ):
            return
    await _answer(message, settings,
        "<b>智能群管机器人</b>\n"
        "欢迎使用。\n\n"
        "<b>核心功能</b>\n"
        "永久记忆（管理员自然语言维护）\n"
        "内容审核\n"
        "智能闲聊\n\n"
        "<b>快速开始</b>\n"
        "发送 /help 查看完整命令。"
    )


@router.message(Command("help"))
async def cmd_help(message: Message, session: AsyncSession, settings: Settings) -> None:
    if not await ensure_group_authorized(message, session, settings):
        return
    await _answer(message, settings, build_help_text())


_VOTEBAN_USAGE = (
    "<b>命令用法</b>\n"
    "回复目标用户的消息后发送 /voteban [举报理由]\n\n"
    "对被回复用户发起民主投票封禁；达到本群设定票数后立即封禁。"
)


@router.message(Command("voteban"))
async def cmd_voteban(
    message: Message,
    session: AsyncSession,
    settings: Settings,
    session_factory: object | None = None,
) -> None:
    """Open a vote through the same quota-enforcing service used by the AI skill."""
    from bot.services.vote_ban import start_vote_ban

    if not is_group(message):
        await _answer(message, settings, "该命令仅可在群内使用。")
        return
    if not await ensure_group_authorized(message, session, settings):
        return
    if getattr(message, "reply_to_message", None) is None:
        await _answer(message, settings, _VOTEBAN_USAGE)
        return
    reason = str(message.text or "").partition(" ")[2].strip()
    result = await start_vote_ban(
        message,
        session,
        settings,
        reason_override=reason,
        trigger_source="command",
        session_factory=session_factory,
    )
    if not result.ok:
        await _answer(message, settings, result.summary)


@router.message(Command("settings"))
async def cmd_settings(
    message: Message,
    settings: Settings,
    session: AsyncSession | None = None,
) -> None:
    user_id = int(getattr(message.from_user, "id", 0) or 0)
    allowed = bool(user_id and is_super_admin_user_id(user_id, settings))
    if not allowed and user_id and session is not None:
        allowed = bool(await session.scalar(
            select(Admin.id)
            .join(AuthorizedGroup, AuthorizedGroup.group_id == Admin.group_id)
            .where(
                Admin.user_id == user_id,
                AuthorizedGroup.bot_present.is_(True),
            )
            .limit(1)
        ))
    if not allowed:
        await _answer(message, settings, "你没有可管理的已授权群组。")
        return
    if not message.chat or message.chat.type != "private":
        await _answer(
            message,
            settings,
            "<b>设置中心</b>\n请私聊机器人后使用 /settings。",
            retry_tls_record_error=True,
        )
        return
    base_url = settings.miniapp_public_base_url.strip().rstrip("/")
    if not base_url:
        await _answer(
            message,
            settings,
            "<b>设置中心不可用</b>\n请先配置 MINIAPP_PUBLIC_BASE_URL 并重启。",
            auto_delete_seconds=0,
            retry_tls_record_error=True,
        )
        return
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="打开设置中心",
                    web_app=WebAppInfo(url=f"{base_url}/settings"),
                )
            ]
        ]
    )
    await _answer(
        message,
        settings,
        "<b>Bot 设置中心</b>",
        auto_delete_seconds=0,
        reply_markup=keyboard,
        retry_tls_record_error=True,
    )


@router.message(Command("lm"))
async def cmd_lm(
    message: Message,
    session: AsyncSession,
    settings: Settings,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    if not await ensure_group_authorized(message, session, settings):
        return
    if not await ensure_group_admin_permission(message, session, settings):
        return
    # Authorization queries have completed. Release their connection before
    # any memory lookup, LLM intent parsing, typing heartbeat or Telegram send.
    await session.commit()
    if not message.chat or message.chat.type not in ("group", "supergroup"):
        await _answer(message, settings, "<b>永久记忆</b>\n请在群内使用 /lm。", auto_delete_seconds=0)
        return

    args = (message.text or "").partition(" ")[2].strip()
    memory = memory_holder.get()
    if not args or args.lower() in {"list", "ls"}:
        items = await memory.list_permanent_memories(message.chat.id, limit=200)
        text, keyboard = _build_memory_list_page(items, page=0)
        await _answer(
            message,
            settings,
            text,
            auto_delete_seconds=0,
            reply_markup=keyboard,
            disable_web_page_preview=True,
        )
        return

    user_id = int(getattr(message.from_user, "id", 0) or 0)
    sender_username = (getattr(message.from_user, "username", "") or "").strip()
    sender_is_owner = bool(user_id and is_super_admin_user_id(user_id, settings))
    sender_is_tg_admin = True
    skill = _build_skill_service(settings)

    request_text = ""
    normalized = args.lower()
    if normalized.startswith("add "):
        content = args[4:].strip()
        if not content:
            await _answer(
                message,
                settings,
                "<b>/lm 用法</b>\n"
                "/lm：查看永久记忆\n"
                "/lm add &lt;内容&gt;\n"
                "/lm replace &lt;#ID或关键词&gt; =&gt; &lt;新内容&gt;",
            )
            return
        request_text = f"添加一条永久记忆：{content}"
    elif normalized.startswith("replace "):
        payload = args[8:].strip()
        parts = re.split(r"\s*(?:=>|->|→)\s*", payload, maxsplit=1)
        if len(parts) != 2 or not parts[0].strip() or not parts[1].strip():
            await _answer(
                message,
                settings,
                "<b>/lm replace 用法</b>\n"
                "/lm replace &lt;#ID或关键词&gt; =&gt; &lt;新内容&gt;",
            )
            return
        request_text = f"把永久记忆 {parts[0].strip()} 改成 {parts[1].strip()}"
    else:
        await _answer(
            message,
            settings,
            "<b>/lm 用法</b>\n"
            "/lm：查看永久记忆\n"
            "/lm add &lt;内容&gt;\n"
            "/lm replace &lt;#ID或关键词&gt; =&gt; &lt;新内容&gt;\n\n"
            "删除请直接使用 /lm 列表里的按钮。",
        )
        return

    async with typing_action(message, enabled=settings.bot.enable_typing):
        result = await skill.run_skill(
            "memory_manage",
            {"request_text": request_text},
            session=None,
            session_factory=session_factory,
            sender_user_id=user_id,
            sender_username=sender_username,
            sender_is_owner=sender_is_owner,
            sender_is_tg_admin=sender_is_tg_admin,
            message=message,
            chat_id=message.chat.id,
            current_user_text=request_text,
        )
    await _answer(message, settings, result.summary)


@router.message(Command("compact"))
async def cmd_compact(
    message: Message,
    session: AsyncSession,
    settings: Settings,
) -> None:
    if not await ensure_group_authorized(message, session, settings):
        return
    if not await ensure_group_admin_permission(message, session, settings):
        return
    # Authorization queries have completed. Release their connection before
    # the compression LLM call, typing heartbeat or Telegram send.
    await session.commit()
    if not message.chat or message.chat.type not in ("group", "supergroup"):
        await _answer(message, settings, "<b>上下文压缩</b>\n请在目标群内使用 /compact。")
        return

    memory = memory_holder.get()
    async with typing_action(message, enabled=settings.bot.enable_typing):
        result = await memory.compact_now(message.chat.id)

    status = str(result.get("status", ""))
    if status == "ok":
        await _answer(
            message,
            settings,
            "<b>上下文压缩完成</b>\n"
            f"已把 {int(result.get('compacted_messages', 0))} 条临时对话历史压缩进背景摘要。",
        )
    elif status == "empty":
        await _answer(message, settings, "<b>上下文压缩</b>\n当前群没有可压缩的临时对话历史。")
    elif status == "db_locked":
        await _answer(message, settings, "<b>上下文压缩失败</b>\n数据库暂时繁忙，历史已保留，请稍后重试。")
    else:
        await _answer(message, settings, "<b>上下文压缩失败</b>\n压缩模型未返回摘要，历史已保留，请稍后重试。")


@router.callback_query(F.data.startswith("lml:"))
async def on_memory_list_paging(
    callback: CallbackQuery,
    settings: Settings,
    session: AsyncSession | None = None,
) -> None:
    if not callback.data:
        await callback.answer()
        return
    if session is None:
        await callback.answer("会话未就绪，请重新 /lm", show_alert=True)
        return
    if not await _callback_user_can_manage_memories(callback, session, settings):
        return

    msg = callback.message
    if not msg or not msg.chat:
        await callback.answer("消息已失效", show_alert=True)
        return

    page = _parse_int(callback.data.split(":")[1], default=0)
    items = await memory_holder.get().list_permanent_memories(msg.chat.id, limit=200)
    text, keyboard = _build_memory_list_page(items, page=page)
    try:
        await msg.edit_text(text, reply_markup=preserve_delete_button(msg, keyboard), disable_web_page_preview=True)
    except TelegramBadRequest as exc:
        if "message is not modified" not in str(exc).lower():
            await callback.answer("列表刷新失败，请重试 /lm", show_alert=True)
            return
    except Exception:
        await callback.answer("列表刷新失败，请重试 /lm", show_alert=True)
        return
    await callback.answer()


@router.callback_query(F.data.startswith("lmd:"))
async def on_memory_delete(
    callback: CallbackQuery,
    settings: Settings,
    session: AsyncSession | None = None,
) -> None:
    if not callback.data:
        await callback.answer()
        return
    if session is None:
        await callback.answer("会话未就绪，请重新 /lm", show_alert=True)
        return
    if not await _callback_user_can_manage_memories(callback, session, settings):
        return

    msg = callback.message
    if not msg or not msg.chat:
        await callback.answer("消息已失效", show_alert=True)
        return

    parts = callback.data.split(":")
    if len(parts) != 3:
        await callback.answer("参数错误", show_alert=True)
        return
    memory_id = _parse_int(parts[1], default=0)
    page_hint = _parse_int(parts[2], default=0)
    if memory_id <= 0:
        await callback.answer("参数错误", show_alert=True)
        return

    deleted = await memory_holder.get().delete_permanent_memory(msg.chat.id, f"#{memory_id}")
    if not deleted:
        await callback.answer("记忆不存在或已删除", show_alert=True)
        return

    items = await memory_holder.get().list_permanent_memories(msg.chat.id, limit=200)
    if not items:
        await msg.edit_text("<b>永久记忆</b>\n当前为空。", reply_markup=None)
        await callback.answer(f"已删除记忆 #{memory_id}")
        return

    total_pages = max(1, (len(items) + _LIST_PAGE_SIZE - 1) // _LIST_PAGE_SIZE)
    page = min(max(page_hint, 0), total_pages - 1)
    text, keyboard = _build_memory_list_page(items, page=page)
    try:
        await msg.edit_text(text, reply_markup=preserve_delete_button(msg, keyboard), disable_web_page_preview=True)
    except Exception:
        await callback.answer("删除成功，但列表刷新失败，请重试 /lm", show_alert=True)
        return
    await callback.answer(f"已删除记忆 #{memory_id}")


@router.message(Command("av"))
async def cmd_av(message: Message, session: AsyncSession, settings: Settings) -> None:
    if not await ensure_group_authorized(message, session, settings):
        return

    is_group_chat = bool(
        message.chat and message.chat.type in ("group", "supergroup")
    )
    if not is_group_chat:
        # Private chats have no per-group feature flag or authorization scope.
        # Keep the diagnostic entrypoint available to the configured owner only
        # instead of turning it into a public, unmetered external-search proxy.
        await session.commit()
        user = message.from_user
        if user is None or not is_super_admin_user_id(user.id, settings):
            await _answer(
                message,
                settings,
                "<b>AV 查询</b>\n私聊仅最高管理员可使用；普通用户请在已启用该功能的授权群内查询。",
                auto_delete_seconds=0,
            )
            return

    args = (message.text or "").partition(" ")[2].strip()
    if not args:
        await session.commit()
        await _answer(
            message,
            settings,
            "<b>AV 查询用法</b>\n"
            "1. /av WANZ-530（按番号直查并展示详情+种子）\n"
            "2. /av 推川悠里（按演员名查询并弹出可选列表）\n"
            "3. /av 人妻 NTR（按关键词查询并弹出可选列表）\n\n"
            "支持来源：JAVBUS / MADOUQU / DMM / FC2\n"
            "支持 FC2 编号：/av FC2-PPV-4863846\n\n"
            "默认状态：<b>关闭</b>（每个群独立）\n"
            "需最高管理员在目标群发送 /av enable 后可使用\n\n"
            "私聊仅最高管理员可查询。\n\n"
            "<b>最高管理员命令（群内）</b>\n"
            "4. /av enable（启用本群 AV 查询）\n"
            "5. /av disable（停用本群 AV 查询）",
        )
        return

    args_norm = args.strip().lower()
    if args_norm in {"enable", "disable"}:
        if not message.chat or message.chat.type not in ("group", "supergroup"):
            await _answer(
                message,
                settings,
                "<b>AV 开关</b>\n请在目标群内发送：/av enable 或 /av disable",
                auto_delete_seconds=0,
            )
            return
        if not await ensure_super_admin(message, settings):
            return

        group_row = await _ensure_group_row(session, message.chat.id, message.chat.title or "")
        group_settings = dict(group_row.settings or {})
        target_enabled = args_norm == "enable"
        previous = _is_group_av_enabled(group_settings)
        group_settings[_AV_GROUP_ENABLE_KEY] = target_enabled
        group_row.settings = group_settings
        # Persist and release the SQLite write lock before Telegram I/O.
        await session.commit()

        if previous == target_enabled:
            status_line = "状态未变化"
        else:
            status_line = "已更新"
        state_text = "已启用" if target_enabled else "已停用"
        await _answer(
            message,
            settings,
            "<b>AV 开关</b>\n"
            f"<b>群ID</b>: {message.chat.id}\n"
            f"<b>结果</b>: {state_text}（{status_line}）",
        )
        return

    if is_group_chat:
        group_row = await _ensure_group_row(session, message.chat.id, message.chat.title or "")
        group_av_enabled = _is_group_av_enabled(group_row.settings)
        await session.commit()
        if not group_av_enabled:
            await _answer(
                message,
                settings,
                "<b>AV 查询</b>\n当前群组未启用该功能，请最高管理员发送 /av enable。",
            )
            return
    svc = AVSearchService(settings)
    if not svc.enabled:
        await _answer(message, settings, "<b>AV 查询</b>\n当前已禁用。")
        return

    owner_user_id = message.from_user.id if message.from_user else 0
    query = _truncate_text(args, 120)
    is_code_query = is_av_code_query(query)

    async with typing_action(message, enabled=settings.bot.enable_typing):
        if is_code_query:
            detail = await svc.lookup_by_code(query)
            if detail:
                item = AVSearchItem(
                    source=detail.source,
                    title=detail.title,
                    code=detail.code,
                    url=detail.url,
                    cover_url=detail.cover_url,
                    date=detail.date,
                    summary=detail.summary,
                )
                av_session = _AV_SESSION_STORE.create(
                    owner_user_id=owner_user_id,
                    query=query,
                    results=[item],
                )
                av_session.details[0] = detail
                sent_ok = await _send_av_detail(
                    message=message,
                    session=av_session,
                    result_idx=0,
                    detail=detail,
                )
                if not sent_ok:
                    await _answer(
                        message,
                        settings,
                        "<b>AV 查询</b>\n详情发送失败，请稍后重试。",
                    )
                return

        results = await svc.search(query)

    if not results:
        await _answer(
            message,
            settings,
            "<b>AV 查询结果</b>\n"
            f"关键词: {html.escape(query)}\n"
            "未找到匹配内容。",
        )
        return

    av_session = _AV_SESSION_STORE.create(
        owner_user_id=owner_user_id,
        query=query,
        results=results,
    )
    text, keyboard = _build_av_search_page(av_session, page=0)
    await message.answer(text, reply_markup=keyboard, disable_web_page_preview=True)


@router.callback_query(F.data.startswith("avs:"))
async def on_av_search_paging(
    callback: CallbackQuery,
    settings: Settings,
    session: AsyncSession | None = None,
) -> None:
    if not callback.data:
        await callback.answer()
        return
    if session is None:
        await callback.answer("会话未就绪，请重新 /av", show_alert=True)
        return
    msg = callback.message
    if not msg:
        await callback.answer("消息已失效", show_alert=True)
        return
    if not await _ensure_callback_group_authorized(callback, msg, session):
        return
    if not await _ensure_av_callback_scope(callback, msg, session, settings):
        return

    parts = callback.data.split(":")
    if len(parts) != 3:
        await callback.answer("参数错误", show_alert=True)
        return
    token = parts[1]
    page = _parse_int(parts[2], default=0)
    av_session = _AV_SESSION_STORE.get(token)
    if not av_session:
        await callback.answer("查询已过期，请重新 /av 搜索", show_alert=True)
        return

    user_id = callback.from_user.id if callback.from_user else 0
    if not _av_session_owner_ok(av_session, user_id):
        await callback.answer("仅发起查询的人可操作该列表", show_alert=True)
        return

    text, keyboard = _build_av_search_page(av_session, page=page)
    try:
        await msg.edit_text(text, reply_markup=preserve_delete_button(msg, keyboard), disable_web_page_preview=True)
    except Exception:
        await msg.answer(text, reply_markup=keyboard, disable_web_page_preview=True)
    await callback.answer()


@router.callback_query(F.data.startswith("avd:"))
async def on_av_detail_select(
    callback: CallbackQuery,
    settings: Settings,
    session: AsyncSession | None = None,
) -> None:
    if not callback.data:
        await callback.answer()
        return
    if session is None:
        await callback.answer("会话未就绪，请重新 /av", show_alert=True)
        return
    msg = callback.message
    if not msg:
        await callback.answer("消息已失效", show_alert=True)
        return
    if not await _ensure_callback_group_authorized(callback, msg, session):
        return
    if not await _ensure_av_callback_scope(callback, msg, session, settings):
        return

    parts = callback.data.split(":")
    if len(parts) != 3:
        await callback.answer("参数错误", show_alert=True)
        return

    token = parts[1]
    idx = _parse_int(parts[2], default=-1)
    av_session = _AV_SESSION_STORE.get(token)
    if not av_session:
        await callback.answer("查询已过期，请重新 /av 搜索", show_alert=True)
        return
    if idx < 0 or idx >= len(av_session.results):
        await callback.answer("目标不存在", show_alert=True)
        return

    user_id = callback.from_user.id if callback.from_user else 0
    if not _av_session_owner_ok(av_session, user_id):
        await callback.answer("仅发起查询的人可查看详情", show_alert=True)
        return

    detail = av_session.details.get(idx)
    if not detail:
        item = av_session.results[idx]
        svc = AVSearchService(settings)
        async with typing_action(msg, enabled=settings.bot.enable_typing):
            detail = await svc.fetch_detail(item)
        if not detail:
            await callback.answer("详情抓取失败，请稍后重试", show_alert=True)
            return
        av_session.details[idx] = detail

    ok = await _send_av_detail(
        message=msg,
        session=av_session,
        result_idx=idx,
        detail=detail,
        in_place=True,
    )
    if not ok:
        await callback.answer("详情更新失败，请重试", show_alert=True)
        return
    await callback.answer("已更新详情")


@router.callback_query(F.data.startswith("avm:"))
async def on_av_seed_paging(
    callback: CallbackQuery,
    settings: Settings,
    session: AsyncSession | None = None,
) -> None:
    if not callback.data:
        await callback.answer()
        return
    if session is None:
        await callback.answer("会话未就绪，请重新 /av", show_alert=True)
        return
    msg = callback.message
    if not msg:
        await callback.answer("消息已失效", show_alert=True)
        return
    if not await _ensure_callback_group_authorized(callback, msg, session):
        return
    if not await _ensure_av_callback_scope(callback, msg, session, settings):
        return

    parts = callback.data.split(":")
    if len(parts) != 4:
        await callback.answer("参数错误", show_alert=True)
        return

    token = parts[1]
    idx = _parse_int(parts[2], default=-1)
    page = _parse_int(parts[3], default=0)

    av_session = _AV_SESSION_STORE.get(token)
    if not av_session:
        await callback.answer("查询已过期，请重新 /av 搜索", show_alert=True)
        return
    if idx < 0 or idx >= len(av_session.results):
        await callback.answer("目标不存在", show_alert=True)
        return

    user_id = callback.from_user.id if callback.from_user else 0
    if not _av_session_owner_ok(av_session, user_id):
        await callback.answer("仅发起查询的人可浏览种子", show_alert=True)
        return

    detail = av_session.details.get(idx)
    if not detail:
        item = av_session.results[idx]
        svc = AVSearchService(settings)
        async with typing_action(msg, enabled=settings.bot.enable_typing):
            detail = await svc.fetch_detail(item)
        if not detail:
            await callback.answer("详情抓取失败，请稍后重试", show_alert=True)
            return
        av_session.details[idx] = detail

    if not detail.seeds:
        await callback.answer("无种子信息", show_alert=True)
        return

    text, keyboard = _build_av_seed_page(
        session=av_session,
        result_idx=idx,
        detail=detail,
        page=page,
    )
    ok = await _edit_av_seed_in_place(
        message=msg,
        detail=detail,
        text=text,
        keyboard=keyboard,
    )
    if not ok:
        await callback.answer("更新失败，请重新 /av", show_alert=True)
        return
    await callback.answer()
