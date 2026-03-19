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
        mention_tag = "是" if is_mentioned else "否"
        reply_bot_tag = "是" if is_reply_to_bot else "否"
        reply_other_tag = "是" if is_reply_to_other else "否"
        merged_tag = "是" if merged_count > 1 else "否"

        context = (
            f"{build_current_time_context()}\n"
            f"[是否合并消息]\n{merged_tag}\n"
            f"[合并消息数]\n{max(1, int(merged_count or 1))}\n"
            f"[是否@机器人]\n{mention_tag}\n"
            f"[是否回复机器人]\n{reply_bot_tag}\n"
            f"[是否回复其他用户]\n{reply_other_tag}\n"
            f"[消息类型]\n{clean_text(msg_type, max_len=40)}\n"
        )
        if merged_count > 1 and merged_context.strip():
            context += (
                "[合并消息明细]\n"
                f"{wrap_untrusted('合并消息明细', clean_text(merged_context, max_len=1600), max_len=1600)}\n"
            )
        context += (
            f"[用户消息]\n{wrap_untrusted('用户消息', clean_text(user_text, max_len=1200), max_len=1200)}\n"
            f"[机器人拟回复]\n{wrap_untrusted('机器人拟回复', clean_text(assistant_reply, max_len=1200), max_len=1200)}"
        )

        raw = await self.llm.decision(build_defended_system(REPLY_MODE_SYSTEM), context)
        result = (raw or "").strip().lower()
        if result in {"reply", "message"}:
            log.info("reply mode decided=%s merged=%s", result, merged_count > 1)
            return result

        fallback = "reply"
        if is_reply_to_other and not is_reply_to_bot:
            fallback = "message"
        elif merged_count > 1 and not is_mentioned and not is_reply_to_bot:
            fallback = "message"
        log.info("reply mode fallback=%s raw=%s", fallback, result or "(empty)")
        return fallback
