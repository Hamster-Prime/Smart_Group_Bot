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
    def __init__(self, llm: LLMService, *, context_items: int = 5) -> None:
        self.llm = llm
        self.context_items = max(0, int(context_items))

    @staticmethod
    def _format_recent_context(
        history: list[dict[str, str]] | None,
        *,
        max_items: int = 5,
    ) -> str:
        if max_items <= 0:
            return "[最近上下文]\n(无)"
        if not history:
            return "[最近上下文]\n(无)"

        role_map = {
            "user": "user",
            "assistant": "assistant",
            "system": "system",
        }
        lines: list[str] = []
        for item in history[-max_items:]:
            role = str(item.get("role", "user")).strip().lower()
            role_label = role_map.get(role, "other")
            content = clean_text(str(item.get("content", "")), max_len=240)
            if not content:
                continue
            lines.append(f"[{role_label}] {content}")

        if not lines:
            return "[最近上下文]\n(无)"

        merged = "\n".join(lines)
        return f"[最近上下文]\n{wrap_untrusted('最近上下文', merged, max_len=1800)}"

    async def _llm_decide(
        self,
        normalized: str,
        is_mentioned: bool,
        is_reply: bool,
        is_reply_to_bot: bool,
        is_reply_to_other: bool,
        mentions_other_user: bool,
        is_owner: bool,
        is_tg_admin: bool,
        user_tag: str,
        msg_type: str,
        history: list[dict[str, str]] | None,
    ) -> str:
        sender = f"[发送者]\n{clean_text(user_tag, max_len=120)}\n" if user_tag else ""
        mention_tag = "[是否@机器人]\n是" if is_mentioned else "[是否@机器人]\n否"
        reply_tag = "[是否回复消息]\n是" if is_reply else "[是否回复消息]\n否"
        reply_bot_tag = "[是否回复机器人]\n是" if is_reply_to_bot else "[是否回复机器人]\n否"
        reply_other_tag = "[是否回复其他用户]\n是" if is_reply_to_other else "[是否回复其他用户]\n否"
        mention_other_tag = "[是否@其他用户]\n是" if mentions_other_user else "[是否@其他用户]\n否"
        owner_tag = "[当前发送者是否主人]\n是" if is_owner else "[当前发送者是否主人]\n否"
        tg_admin_tag = "[当前发送者是否TG群管理员]\n是" if is_tg_admin else "[当前发送者是否TG群管理员]\n否"
        recent_context_tag = self._format_recent_context(history, max_items=self.context_items)

        context = (
            f"{build_current_time_context()}\n"
            f"{sender}"
            f"{mention_tag}\n"
            f"{reply_tag}\n"
            f"{reply_bot_tag}\n"
            f"{reply_other_tag}\n"
            f"{mention_other_tag}\n"
            f"{owner_tag}\n"
            f"{tg_admin_tag}\n"
            f"{recent_context_tag}\n"
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
        is_tg_admin: bool = False,
        user_tag: str = "",
        msg_type: str = "text",
        history: list[dict[str, str]] | None = None,
    ) -> str:
        """Return one of: skip / question / casual."""
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
            is_tg_admin,
            user_tag,
            msg_type,
            history,
        )

        # 兼容旧版输出
        if result == "knowledge":
            log.info("decision normalized legacy output: knowledge -> question")
            result = "question"

        # 被@时，如果模型返回了有效的 question/casual，保持原样
        # 否则默认为 casual（保持向后兼容）
        if is_mentioned:
            if result not in ("question", "casual"):
                log.info("decision @mentioned fallback to casual")
                return "casual"
            return result

        # 验证输出
        if result in ("skip", "question", "casual"):
            return result

        log.info("decision fallback=skip reason=invalid_output actual=%s", result)
        return "skip"
