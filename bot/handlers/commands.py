from __future__ import annotations

import json
import logging
import re

from aiogram import Router
from aiogram.filters import Command
from aiogram.types import Message
from sqlalchemy.ext.asyncio import AsyncSession

from bot.config import Settings
from bot.services.authz import ensure_group_authorized
from bot.services.knowledge import KnowledgeService
from bot.services.llm import LLMService
from bot.utils.prompts import KB_MANAGE_SYSTEM
from bot.utils.telegram import ensure_admin

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


@router.message(Command("start"))
async def cmd_start(message: Message, session: AsyncSession, settings: Settings) -> None:
    if not await ensure_group_authorized(message, session, settings):
        return
    await message.answer(
        "你好，我是智能群管机器人。\n\n"
        "功能:\n"
        "- 知识库问答\n"
        "- 内容审核\n"
        "- 智能闲聊\n\n"
        "发送 /help 查看命令。"
    )


@router.message(Command("help"))
async def cmd_help(message: Message, session: AsyncSession, settings: Settings) -> None:
    if not await ensure_group_authorized(message, session, settings):
        return
    await message.answer(
        "<b>命令列表</b>\n\n"
        "/start - 开始使用\n"
        "/help - 帮助信息\n"
        "/kb &lt;自然语言指令&gt; - 管理知识库（管理员）\n"
        "/kb list - 列出知识条目（管理员）\n\n"
        "<b>管理员命令</b>\n"
        "/addrule &lt;自然语言指令&gt; - 管理审核规则\n"
        "/rules - 查看审核规则\n"
        "/warnings &lt;用户ID&gt; - 查看警告记录\n\n"
        "/aiexempt - 回复目标用户消息，开启 AI 审查豁免\n"
        "/unaiexempt - 回复目标用户消息，取消 AI 审查豁免\n\n"
        "<b>最高管理员命令</b>\n"
        "/authgroup [群ID] - 授权群组\n"
        "/unauthgroup [群ID] - 取消授权群组\n"
        "/authlist - 查看已授权群组"
    )


@router.message(Command("kb"))
async def cmd_kb(
    message: Message, session: AsyncSession, settings: Settings
) -> None:
    if not await ensure_group_authorized(message, session, settings):
        return
    if not await ensure_admin(message, settings):
        return

    args = (message.text or "").partition(" ")[2].strip()
    if not args:
        await message.answer(
            "用法: /kb &lt;自然语言指令&gt;\n"
            "示例: /kb 添加 标题:问候语 内容:你好世界"
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
            await message.answer("知识库为空。")
            return
        lines = [f"- <b>{e.title}</b>: {e.content[:60]}..." for e in entries]
        await message.answer("\n".join(lines))
        return

    result = await llm.generate(KB_MANAGE_SYSTEM, args)
    data = _parse_json_payload(result)
    if not data:
        await message.answer("无法理解指令，请重试。")
        return

    action = str(data.get("action", "unknown")).strip().lower()

    if action == "add":
        title = str(data.get("title", "未命名")).strip() or "未命名"
        content = str(data.get("content", "")).strip()
        await kb.add(session, group_id, title, content)
        await message.answer(f"已添加知识条目: <b>{title}</b>")

    elif action == "delete":
        title = str(data.get("title", "")).strip()
        if not title:
            await message.answer("请提供要删除的标题。")
            return
        ok = await kb.remove(session, group_id, title)
        if ok:
            await message.answer(f"已删除: <b>{title}</b>")
        else:
            await message.answer(f"未找到: {title}")

    elif action == "search":
        query = str(data.get("query", args)).strip() or args
        results = await kb.search(session, group_id, query)
        if not results:
            await message.answer("未找到相关内容。")
        else:
            lines = [f"- {r['metadata'].get('title', '?')}" for r in results[:5]]
            await message.answer("搜索结果:\n" + "\n".join(lines))

    elif action == "list":
        entries = await kb.list_entries(session, group_id)
        if not entries:
            await message.answer("知识库为空。")
            return
        lines = [f"- <b>{e.title}</b>: {e.content[:60]}..." for e in entries]
        await message.answer("\n".join(lines))

    else:
        await message.answer("无法理解指令，请重试。")

