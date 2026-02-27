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

    @staticmethod
    def _build_sender_context(sender_user_id: int, sender_username: str) -> str:
        uname = (sender_username or "").strip().lstrip("@")
        shown = f"@{uname}" if uname else "(none)"
        return (
            "[CURRENT_SENDER]\n"
            f"user_id: {sender_user_id}\n"
            f"username: {shown}\n"
            "Use this sender identity for this turn."
        )

    async def reply(
        self,
        text: str,
        history: list[dict[str, str]] | None = None,
        *,
        sender_user_id: int = 0,
        sender_username: str = "",
    ) -> str:
        q = clean_text(text, max_len=1000)
        if contains_prompt_injection(q):
            log.warning("casual input may contain prompt injection")

        messages: list[dict[str, str]] = [
            {"role": "system", "content": build_defended_system(CASUAL_SYSTEM)},
            {
                "role": "system",
                "content": self._build_sender_context(sender_user_id, sender_username),
            },
        ]
        if history:
            messages.extend(sanitize_history_for_llm(history, max_items=len(history)))
        messages.append({"role": "user", "content": wrap_untrusted("user_message", q, max_len=1000)})

        log.info("casual request: history=%d", len(history) if history else 0)
        result = await self.llm.chat(messages)
        log.info("casual reply len=%d", len(result or ""))
        return result

