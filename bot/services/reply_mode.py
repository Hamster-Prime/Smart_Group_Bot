from __future__ import annotations

import logging

from bot.services.llm import LLMService
from bot.utils.prompts import REPLY_MODE_SYSTEM
from bot.utils.runtime_context import build_current_time_context
from bot.utils.security import build_defended_system, clean_text, wrap_untrusted

log = logging.getLogger(__name__)


class ReplyModeService:
    def __init__(self, llm: LLMService) -> None:
        self.llm = llm

    async def decide(
        self,
        *,
        user_text: str,
        assistant_reply: str,
        msg_type: str,
        is_mentioned: bool,
        is_reply_to_bot: bool,
        is_reply_to_other: bool,
        merged_count: int = 1,
        merged_context: str = "",
    ) -> str:
        mention_tag = "yes" if is_mentioned else "no"
        reply_bot_tag = "yes" if is_reply_to_bot else "no"
        reply_other_tag = "yes" if is_reply_to_other else "no"
        merged_tag = "yes" if merged_count > 1 else "no"

        context = (
            f"{build_current_time_context()}\n"
            f"[IS_MERGED_MESSAGE]\n{merged_tag}\n"
            f"[MERGED_MESSAGE_COUNT]\n{max(1, int(merged_count or 1))}\n"
            f"[IS_MENTIONED]\n{mention_tag}\n"
            f"[IS_REPLY_TO_BOT]\n{reply_bot_tag}\n"
            f"[IS_REPLY_TO_OTHER]\n{reply_other_tag}\n"
            f"[MESSAGE_TYPE]\n{clean_text(msg_type, max_len=40)}\n"
        )
        if merged_count > 1 and merged_context.strip():
            context += (
                "[MERGED_MESSAGE_CONTEXT]\n"
                f"{wrap_untrusted('merged_message_context', clean_text(merged_context, max_len=1600), max_len=1600)}\n"
            )
        context += (
            f"[CURRENT_MESSAGE]\n{wrap_untrusted('current_message', clean_text(user_text, max_len=1200), max_len=1200)}\n"
            f"[ASSISTANT_DRAFT_REPLY]\n"
            f"{wrap_untrusted('assistant_draft_reply', clean_text(assistant_reply, max_len=1200), max_len=1200)}"
        )

        raw = await self.llm.decision(build_defended_system(REPLY_MODE_SYSTEM), context)
        result = (raw or "").strip().lower()
        if result in {"reply", "message"}:
            log.info("reply mode decided=%s merged=%s", result, merged_count > 1)
            return result

        fallback = "reply"
        if is_reply_to_other and not is_reply_to_bot:
            fallback = "message"
        log.info("reply mode fallback=%s raw=%s", fallback, result or "(empty)")
        return fallback
