from __future__ import annotations

import logging

from sqlalchemy.ext.asyncio import AsyncSession

from bot.services.knowledge import KnowledgeService
from bot.services.llm import LLMService
from bot.utils.prompts import RAG_SYSTEM
from bot.utils.security import (
    build_defended_system,
    clean_text,
    contains_prompt_injection,
    sanitize_history_for_llm,
    wrap_untrusted,
)

log = logging.getLogger(__name__)


class RAGService:
    def __init__(self, llm: LLMService, knowledge: KnowledgeService) -> None:
        self.llm = llm
        self.knowledge = knowledge

    async def answer(
        self,
        session: AsyncSession,
        group_id: int,
        question: str,
        history: list[dict[str, str]] | None = None,
    ) -> str:
        q = clean_text(question, max_len=1000)
        if contains_prompt_injection(q):
            log.warning("RAG问题检测到疑似提示词注入")

        log.info("RAG搜索: group=%s, query='%s'", group_id, q[:50])
        results = await self.knowledge.search(session, group_id, q)
        if not results:
            log.info("RAG搜索: 无结果")
            return ""

        log.info("RAG搜索: 找到 %d 条结果", len(results))
        context_parts: list[str] = []
        for item in results:
            title = clean_text(str(item.get("metadata", {}).get("title", "")), max_len=120)
            doc = clean_text(str(item.get("document", "")), max_len=1800)
            context_parts.append(
                f"[标题] {title}\n{wrap_untrusted('知识片段', doc, max_len=1800)}"
            )
        context = "\n\n---\n".join(context_parts)

        system_prompt = build_defended_system(RAG_SYSTEM.format(context=context))
        messages: list[dict[str, str]] = [{"role": "system", "content": system_prompt}]
        if history:
            messages.extend(sanitize_history_for_llm(history))
        messages.append({"role": "user", "content": wrap_untrusted("用户问题", q, max_len=1000)})

        result = await self.llm.chat(messages)
        log.info("RAG回复: %s", result[:80] if result else "(空)")
        return result