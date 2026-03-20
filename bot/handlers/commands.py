from __future__ import annotations

import html
import logging

import aiohttp
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
)
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from bot.config import Settings
from bot.db.models import Group
from bot.services.authz import (
    ensure_group_authorized,
    ensure_super_admin,
    is_group_admin_authorized,
    is_super_admin_user_id,
)
from bot.services.av_search import (
    AVDetail,
    AVQuerySession,
    AVQuerySessionStore,
    AVSearchItem,
    AVSearchService,
    is_av_code_query,
)
from bot.services.scheduled_tasks import (
    cancel_scheduled_task,
    list_group_scheduled_tasks,
    render_task_list_text,
)
from bot.utils.telegram import answer_with_auto_delete, typing_action

router = Router()
log = logging.getLogger(__name__)
_AV_SEARCH_PAGE_SIZE = 6
_AV_SEED_PAGE_SIZE = 1
_AV_SESSION_STORE = AVQuerySessionStore(ttl_seconds=15 * 60, max_sessions=256)
_AV_GROUP_ENABLE_KEY = "av_enabled"


async def _answer(
    message: Message,
    settings: Settings,
    text: str,
    *,
    auto_delete_minutes: int | None = None,
    **kwargs: object,
) -> None:
    await answer_with_auto_delete(
        message,
        text,
        auto_delete_minutes=settings.bot.auto_delete_minutes if auto_delete_minutes is None else auto_delete_minutes,
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


def _av_session_owner_ok(session: AVQuerySession, user_id: int) -> bool:
    if session.owner_user_id <= 0:
        return True
    return session.owner_user_id == user_id


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

    timeout = aiohttp.ClientTimeout(total=15)
    try:
        async with aiohttp.ClientSession(timeout=timeout, headers=headers) as client:
            async with client.get(url, allow_redirects=True) as resp:
                if resp.status >= 400:
                    return None
                ctype = (resp.headers.get("content-type") or "").lower()
                if "image" not in ctype:
                    return None
                raw = await resp.read()
                if not raw:
                    return None
                # Keep payload reasonable for Telegram photo upload.
                if len(raw) > 15 * 1024 * 1024:
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
    await _answer(message, settings, 
        "<b>命令总览</b>\n\n"
        "<b>基础命令</b>\n"
        "/start：开始使用\n"
        "/help：查看帮助\n"
        "/av &lt;番号/演员/关键词&gt;：搜索 JAVBUS + MADOUQU + DMM + FC2\n\n"
        "/tasks：查看当前群待执行定时任务\n"
        "/canceltask &lt;任务ID&gt;：取消自己创建的定时任务\n\n"
        "<b>自然语言管理（群管理员）</b>\n"
        "直接发送：记住xxx / 把xxx写入永久记忆 / 删除永久记忆#12 / 永久记忆列表\n"
        "直接发送：新增群规xxx / 删除第N条群规 / 群规列表\n\n"
        "<b>自然语言定时任务（所有群成员）</b>\n"
        "直接发送：记得今晚9点提醒我吃饭\n\n"
        "直接发送：3点帮我查询今天的科技新闻并概述\n\n"
        "直接发送：取消今晚9点提醒我吃饭 / 把刚才那个提醒删了\n\n"
        "<b>群审核管理（需已授权）</b>\n"
        "/addrule &lt;自然语言指令&gt;\n"
        "/rules 审核规则列表\n"
        "/warnings（群内查看警告/封禁名单）\n"
        "/aiexempt（回复目标用户消息）\n"
        "/unaiexempt（回复目标用户消息）\n"
        "/mute（回复目标用户消息，忽略其后续消息回复）\n"
        "/mute all（本群仅做审核，不再回复）\n\n"
        "/unmute（回复目标用户消息，恢复其消息回复）\n"
        "/unmute all（恢复本群正常回复）\n\n"
        "/proactive on|off|status（主动话题定时任务开关/状态）\n\n"
        "<b>最高管理员命令</b>\n"
        "/authgroup 授权群组\n"
        "/unauthgroup 撤销授权群组\n"
        "/authlist 授权群组列表\n"
        "/authadmin 授权群管理\n"
        "/unauthadmin 撤销授权群管理\n"
        "/adminlist 群管理列表\n"
        "/tts（查看当前群 TTS 状态）\n"
        "/tts enable|disable|always（在当前群控制 TTS skill / 始终语音输出）\n"
        "/av enable（在当前群启用 AV 查询）\n"
        "/av disable（在当前群停用 AV 查询）"
    )


@router.message(Command("tasks"))
async def cmd_tasks(message: Message, session: AsyncSession, settings: Settings) -> None:
    if not await ensure_group_authorized(message, session, settings):
        return
    if not message.chat or message.chat.type not in ("group", "supergroup"):
        await _answer(message, settings, "<b>定时任务列表</b>\n请在群内使用 /tasks。", auto_delete_minutes=0)
        return

    rows = await list_group_scheduled_tasks(session, group_id=message.chat.id, limit=20)
    await _answer(message, settings, render_task_list_text(rows), auto_delete_minutes=0)


@router.message(Command("canceltask"))
async def cmd_canceltask(message: Message, session: AsyncSession, settings: Settings) -> None:
    if not await ensure_group_authorized(message, session, settings):
        return
    if not message.chat or message.chat.type not in ("group", "supergroup"):
        await _answer(
            message,
            settings,
            "<b>取消定时任务</b>\n请在群内使用 /canceltask &lt;任务ID&gt;。",
            auto_delete_minutes=0,
        )
        return

    args = (message.text or "").partition(" ")[2].strip()
    try:
        task_id = int(args)
    except (TypeError, ValueError):
        await _answer(
            message,
            settings,
            "<b>取消定时任务</b>\n用法：/canceltask &lt;任务ID&gt;",
            auto_delete_minutes=0,
        )
        return

    user_id = message.from_user.id if message.from_user else 0
    allow_any = False
    if user_id and is_super_admin_user_id(user_id, settings):
        allow_any = True
    elif user_id:
        allow_any = await is_group_admin_authorized(session, message.chat.id, user_id)

    row = await cancel_scheduled_task(
        session,
        group_id=message.chat.id,
        task_id=task_id,
        operator_user_id=user_id,
        allow_any=allow_any,
    )
    if row is None:
        await _answer(
            message,
            settings,
            "<b>取消定时任务</b>\n未找到可取消的任务，或你没有权限取消该任务。",
            auto_delete_minutes=0,
        )
        return

    await _answer(
        message,
        settings,
        "<b>定时任务已取消</b>\n"
        f"<b>ID</b>: #{row.id}\n"
        f"<b>内容</b>: {html.escape(row.content or '-')}",
        auto_delete_minutes=0,
    )


@router.message(Command("av"))
async def cmd_av(message: Message, session: AsyncSession, settings: Settings) -> None:
    if not await ensure_group_authorized(message, session, settings):
        return

    args = (message.text or "").partition(" ")[2].strip()
    if not args:
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
            "<b>最高管理员命令（群内）</b>\n"
            "4. /av enable（启用本群 AV 查询）\n"
            "5. /av disable（停用本群 AV 查询）",
            auto_delete_minutes=0,
        )
        return

    args_norm = args.strip().lower()
    if args_norm in {"enable", "disable"}:
        if not message.chat or message.chat.type not in ("group", "supergroup"):
            await _answer(
                message,
                settings,
                "<b>AV 开关</b>\n请在目标群内发送：/av enable 或 /av disable",
                auto_delete_minutes=0,
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
            auto_delete_minutes=0,
        )
        return

    if message.chat and message.chat.type in ("group", "supergroup"):
        group_row = await _ensure_group_row(session, message.chat.id, message.chat.title or "")
        if not _is_group_av_enabled(group_row.settings):
            await _answer(
                message,
                settings,
                "<b>AV 查询</b>\n当前群组未启用该功能，请最高管理员发送 /av enable。",
                auto_delete_minutes=0,
            )
            return

    svc = AVSearchService(settings)
    if not svc.enabled:
        await _answer(message, settings, "<b>AV 查询</b>\n当前已禁用。", auto_delete_minutes=0)
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
                        auto_delete_minutes=0,
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
            auto_delete_minutes=0,
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
    if not await ensure_group_authorized(msg, session, settings):
        await callback.answer()
        return
    if msg.chat and msg.chat.type in ("group", "supergroup"):
        group_row = await _ensure_group_row(session, msg.chat.id, msg.chat.title or "")
        if not _is_group_av_enabled(group_row.settings):
            await callback.answer("当前群组未启用 AV 查询", show_alert=True)
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
        await msg.edit_text(text, reply_markup=keyboard, disable_web_page_preview=True)
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
    if not await ensure_group_authorized(msg, session, settings):
        await callback.answer()
        return
    if msg.chat and msg.chat.type in ("group", "supergroup"):
        group_row = await _ensure_group_row(session, msg.chat.id, msg.chat.title or "")
        if not _is_group_av_enabled(group_row.settings):
            await callback.answer("当前群组未启用 AV 查询", show_alert=True)
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
    if not await ensure_group_authorized(msg, session, settings):
        await callback.answer()
        return
    if msg.chat and msg.chat.type in ("group", "supergroup"):
        group_row = await _ensure_group_row(session, msg.chat.id, msg.chat.title or "")
        if not _is_group_av_enabled(group_row.settings):
            await callback.answer("当前群组未启用 AV 查询", show_alert=True)
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


