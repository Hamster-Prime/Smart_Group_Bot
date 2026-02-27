from __future__ import annotations

import html
import json
import logging
import re

from aiogram import Router
from aiogram.filters import Command
from aiogram.types import Message
from sqlalchemy.ext.asyncio import AsyncSession

from bot.config import Settings
from bot.services.authz import ensure_group_admin_permission, ensure_group_authorized
from bot.services.knowledge import KnowledgeService
from bot.services.llm import LLMService
from bot.utils.prompts import KB_MANAGE_SYSTEM

router = Router()
log = logging.getLogger(__name__)


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


def _truncate_text(text: str, max_len: int) -> str:
    cleaned = (text or "").replace("\n", " ").strip()
    if len(cleaned) <= max_len:
        return cleaned
    return cleaned[:max_len] + "..."


def _format_kb_entries(entries: list) -> str:
    lines = [f"<b>知识库条目</b>\n共 {len(entries)} 条"]
    for idx, entry in enumerate(entries[:30], start=1):
        title = html.escape((entry.title or "未命名").strip())
        summary = html.escape(_truncate_text(entry.content or "", 90) or "(空)")
        lines.append(f"{idx}. <b>{title}</b>\n摘要: {summary}")
    return "\n\n".join(lines)


def _format_kb_search_results(query: str, results: list[dict]) -> str:
    lines = [
        "<b>知识库搜索结果</b>",
        f"关键词: {html.escape(_truncate_text(query, 60))}",
        f"命中: {len(results[:5])} 条",
    ]
    for idx, item in enumerate(results[:5], start=1):
        meta = item.get("metadata", {}) if isinstance(item, dict) else {}
        title = html.escape(str(meta.get("title") or "未命名"))
        score = item.get("score") if isinstance(item, dict) else None
        score_text = f"{float(score):.2f}" if isinstance(score, (float, int)) else "-"
        lines.append(f"{idx}. <b>{title}</b>（相关度: {score_text}）")
    return "\n".join(lines)


@router.message(Command("start"))
async def cmd_start(message: Message, session: AsyncSession, settings: Settings) -> None:
    if not await ensure_group_authorized(message, session, settings):
        return
    await message.answer(
        "<b>智能群管机器人</b>\n"
        "欢迎使用。\n\n"
        "<b>核心功能</b>\n"
        "- 知识库问答\n"
        "- 内容审核\n"
        "- 智能闲聊\n\n"
        "<b>快速开始</b>\n"
        "发送 /help 查看完整命令。"
    )


@router.message(Command("help"))
async def cmd_help(message: Message, session: AsyncSession, settings: Settings) -> None:
    if not await ensure_group_authorized(message, session, settings):
        return
    await message.answer(
        "<b>命令总览</b>\n\n"
        "<b>基础命令</b>\n"
        "- /start：开始使用\n"
        "- /help：查看帮助\n\n"
        "<b>知识库管理（已授权群管理）</b>\n"
        "- /kb &lt;自然语言指令&gt;\n"
        "- /kb list\n\n"
        "<b>群审核管理（需已授权）</b>\n"
        "- /addrule &lt;自然语言指令&gt;\n"
        "- /rules\n"
        "- /warnings &lt;用户ID&gt;\n"
        "- /aiexempt（回复目标用户消息）\n"
        "- /unaiexempt（回复目标用户消息）\n\n"
        "<b>最高管理员命令</b>\n"
        "- /authgroup [群ID]\n"
        "- /unauthgroup [群ID]\n"
        "- /authlist\n"
        "- /authadmin [群ID] [用户ID]\n"
        "- /unauthadmin [群ID] [用户ID]\n"
        "- /adminlist [群ID]"
    )


@router.message(Command("kb"))
async def cmd_kb(
    message: Message, session: AsyncSession, settings: Settings
) -> None:
    if not await ensure_group_authorized(message, session, settings):
        return
    if not await ensure_group_admin_permission(message, session, settings):
        return

    args = (message.text or "").partition(" ")[2].strip()
    if not args:
        await message.answer(
            "<b>知识库指令格式</b>\n"
            "- 用法: /kb &lt;自然语言指令&gt;\n"
            "- 示例: /kb 添加 标题:问候语 内容:你好世界"
        )
        return

    group_id = message.chat.id
    llm = LLMService(
        settings.bot.main_model,
        settings.bot.decision_model,
        moderation=settings.bot.moderation_model,
        embed=settings.bot.embed_model,
    )
    kb = KnowledgeService(settings.knowledge, llm)

    if args.lower() == "list":
        entries = await kb.list_entries(session, group_id)
        if not entries:
            await message.answer("<b>知识库条目</b>\n当前为空。")
            return
        await message.answer(_format_kb_entries(entries))
        return

    result = await llm.generate(KB_MANAGE_SYSTEM, args)
    data = _parse_json_payload(result)
    if not data:
        await message.answer("<b>知识库操作失败</b>\n无法理解指令，请重试。")
        return

    action = str(data.get("action", "unknown")).strip().lower()

    if action == "add":
        title = str(data.get("title", "未命名")).strip() or "未命名"
        content = str(data.get("content", "")).strip()
        await kb.add(session, group_id, title, content)
        await message.answer(
            "<b>知识库新增成功</b>\n"
            f"- <b>标题</b>: {html.escape(title)}\n"
            f"- <b>内容预览</b>: {html.escape(_truncate_text(content, 90) or '(空)')}"
        )

    elif action == "delete":
        title = str(data.get("title", "")).strip()
        if not title:
            await message.answer("<b>知识库删除失败</b>\n请提供要删除的标题。")
            return
        ok = await kb.remove(session, group_id, title)
        if ok:
            await message.answer(
                "<b>知识库删除成功</b>\n"
                f"- <b>标题</b>: {html.escape(title)}"
            )
        else:
            await message.answer(
                "<b>知识库删除失败</b>\n"
                f"- 未找到标题: {html.escape(title)}"
            )

    elif action == "search":
        query = str(data.get("query", args)).strip() or args
        results = await kb.search(session, group_id, query)
        if not results:
            await message.answer(
                "<b>知识库搜索结果</b>\n"
                f"关键词: {html.escape(_truncate_text(query, 60))}\n"
                "命中: 0 条"
            )
        else:
            await message.answer(_format_kb_search_results(query, results))

    elif action == "list":
        entries = await kb.list_entries(session, group_id)
        if not entries:
            await message.answer("<b>知识库条目</b>\n当前为空。")
            return
        await message.answer(_format_kb_entries(entries))

    else:
        await message.answer("<b>知识库操作失败</b>\n无法理解指令，请重试。")

