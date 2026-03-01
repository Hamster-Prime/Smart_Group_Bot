from __future__ import annotations

import logging
import re

from bot.services.llm import LLMService
from bot.utils.prompts import DECISION_SYSTEM
from bot.utils.runtime_context import build_current_time_context
from bot.utils.security import (
    build_defended_system,
    clean_text,
    contains_prompt_injection,
    wrap_untrusted,
)

log = logging.getLogger(__name__)


class DecisionService:
    def __init__(self, llm: LLMService) -> None:
        self.llm = llm

    async def _llm_decide(
        self,
        normalized: str,
        is_mentioned: bool,
        is_reply: bool,
        is_reply_to_bot: bool,
        is_reply_to_other: bool,
        mentions_other_user: bool,
        is_owner: bool,
        user_tag: str,
        msg_type: str,
    ) -> str:
        sender = f"[发送者]\n{clean_text(user_tag, max_len=120)}\n" if user_tag else ""
        mention_tag = "[是否@机器人]\n是" if is_mentioned else "[是否@机器人]\n否"
        reply_tag = "[是否回复消息]\n是" if is_reply else "[是否回复消息]\n否"
        reply_bot_tag = "[是否回复机器人]\n是" if is_reply_to_bot else "[是否回复机器人]\n否"
        reply_other_tag = "[是否回复其他用户]\n是" if is_reply_to_other else "[是否回复其他用户]\n否"
        mention_other_tag = "[是否@其他用户]\n是" if mentions_other_user else "[是否@其他用户]\n否"
        owner_tag = "[当前发送者是否主人]\n是" if is_owner else "[当前发送者是否主人]\n否"

        context = (
            f"{build_current_time_context()}\n"
            f"{sender}"
            f"{mention_tag}\n"
            f"{reply_tag}\n"
            f"{reply_bot_tag}\n"
            f"{reply_other_tag}\n"
            f"{mention_other_tag}\n"
            f"{owner_tag}\n"
            f"[消息类型]\n{clean_text(msg_type, max_len=40)}\n"
            f"[消息正文]\n{wrap_untrusted('消息正文', normalized, max_len=1000)}"
        )

        result = await self.llm.decision(build_defended_system(DECISION_SYSTEM), context)
        result = result.strip().lower()
        log.info(
            "decision llm returned=%s mention=%s msg_type=%s",
            result,
            is_mentioned,
            msg_type,
        )
        return result

    async def decide(
        self,
        text: str,
        is_mentioned: bool = False,
        is_reply: bool = False,
        is_reply_to_bot: bool = False,
        is_reply_to_other: bool = False,
        mentions_other_user: bool = False,
        is_owner: bool = False,
        user_tag: str = "",
        msg_type: str = "text",
    ) -> str:
        """Return one of: skip / casual."""
        normalized = clean_text(re.sub(r"\s+", " ", text).strip(), max_len=1200)

        if contains_prompt_injection(normalized):
            log.warning("decision input may contain prompt injection")

        result = await self._llm_decide(
            normalized,
            is_mentioned,
            is_reply,
            is_reply_to_bot,
            is_reply_to_other,
            mentions_other_user,
            is_owner,
            user_tag,
            msg_type,
        )

        if result == "knowledge":
            log.info("decision normalized legacy output: knowledge -> casual")
            result = "casual"

        if is_mentioned:
            return "casual"

        if result in ("skip", "casual"):
            return result

        log.info("decision fallback=skip reason=invalid_output")
        return "skip"
