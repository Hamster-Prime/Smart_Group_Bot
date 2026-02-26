from __future__ import annotations

import logging
import re

from bot.services.llm import LLMService
from bot.utils.prompts import DECISION_SYSTEM
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

    @staticmethod
    def _format_titles(knowledge_titles: list[str] | None) -> str:
        if not knowledge_titles:
            return "（空）"
        return "\n".join(f"- {clean_text(title, max_len=120)}" for title in knowledge_titles)

    @staticmethod
    def _contains_bot_trigger(text: str) -> bool:
        lower = text.lower()
        triggers = (
            "@",
            "机器人",
            "bot",
            "助手",
            "sanite",
            "小助理",
            "你能",
            "你会",
            "帮我",
            "请你",
        )
        return any(t in lower for t in triggers)

    @staticmethod
    def _looks_like_question(text: str) -> bool:
        if "?" in text or "？" in text:
            return True
        hints = (
            "怎么",
            "如何",
            "为什么",
            "请问",
            "是什么",
            "多少",
            "哪个",
            "哪一个",
            "哪里",
            "吗",
            "呢",
            "么",
        )
        return any(k in text for k in hints)

    async def _llm_decide(
        self,
        normalized: str,
        is_mentioned: bool,
        user_tag: str,
        msg_type: str,
        knowledge_titles: list[str] | None,
        knowledge_index: str,
    ) -> str:
        sender = f"[发送者]\n{clean_text(user_tag, max_len=120)}\n" if user_tag else ""
        mention_tag = "[是否@机器人]\n是" if is_mentioned else "[是否@机器人]\n否"
        titles_block = self._format_titles(knowledge_titles)

        context = (
            f"{sender}"
            f"{mention_tag}\n"
            f"[消息类型]\n{clean_text(msg_type, max_len=40)}\n"
            f"[知识库标题]\n{wrap_untrusted('知识库标题', titles_block, max_len=1200)}\n"
            f"[知识库摘要]\n{wrap_untrusted('知识库摘要', knowledge_index, max_len=2500)}\n"
            f"[消息正文]\n{wrap_untrusted('消息正文', normalized, max_len=1000)}"
        )

        result = await self.llm.decision(build_defended_system(DECISION_SYSTEM), context)
        result = result.strip().lower()
        log.info(
            "决策结果(LLM): mentioned=%s type=%s kb_titles=%d %s '%s' -> %s",
            is_mentioned,
            msg_type,
            len(knowledge_titles or []),
            clean_text(user_tag, max_len=80),
            normalized[:80],
            result,
        )
        return result

    async def decide(
        self,
        text: str,
        is_mentioned: bool = False,
        user_tag: str = "",
        msg_type: str = "text",
        knowledge_titles: list[str] | None = None,
        knowledge_index: str = "（空）",
    ) -> str:
        """返回: skip / knowledge / casual"""
        normalized = clean_text(re.sub(r"\s+", " ", text).strip(), max_len=1200)

        if contains_prompt_injection(normalized):
            log.warning("决策输入检测到疑似提示词注入")

        # 非@场景：非问题 + 无触发词 => skip，避免群聊噪声全回复
        if not is_mentioned and not self._looks_like_question(normalized) and not self._contains_bot_trigger(normalized):
            log.info("决策前置: 非问题且无触发词，直接 skip")
            return "skip"

        result = await self._llm_decide(
            normalized,
            is_mentioned,
            user_tag,
            msg_type,
            knowledge_titles,
            knowledge_index,
        )

        if is_mentioned:
            if result in ("knowledge", "casual"):
                return result
            return "casual"

        if result in ("skip", "knowledge", "casual"):
            return result

        if self._looks_like_question(normalized):
            return "casual"
        return "skip"