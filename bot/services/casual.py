from __future__ import annotations

import logging

from bot.services.llm import LLMService
from bot.utils.prompts import CASUAL_SYSTEM
from bot.utils.security import (
    build_defended_system,
    clean_text,
    contains_prompt_injection,
    sanitize_history_for_llm,
    wrap_untrusted,
)

log = logging.getLogger(__name__)


class CasualService:
    def __init__(self, llm: LLMService) -> None:
        self.llm = llm

    async def reply(self, text: str, history: list[dict[str, str]] | None = None) -> str:
        q = clean_text(text, max_len=1000)
        if contains_prompt_injection(q):
            log.warning("闲聊输入检测到疑似提示词注入")

        messages: list[dict[str, str]] = [{"role": "system", "content": build_defended_system(CASUAL_SYSTEM)}]
        if history:
            messages.extend(sanitize_history_for_llm(history, max_items=len(history)))
        messages.append({"role": "user", "content": wrap_untrusted("用户消息", q, max_len=1000)})

        log.info("闲聊请求: '%s', history=%d条", q[:50], len(history) if history else 0)
        result = await self.llm.chat(messages)
        log.info("闲聊回复: %s", result[:80] if result else "(空)")
        return result
