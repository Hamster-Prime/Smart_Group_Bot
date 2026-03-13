from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass

from bot.services.llm import LLMService
from bot.utils.prompts import GROUP_INTENT_SYSTEM
from bot.utils.security import build_defended_system, clean_text, wrap_untrusted

log = logging.getLogger(__name__)


@dataclass(slots=True)
class GroupIntent:
    intent: str = "chat"  # chat | memory_manage | rule_manage
    memory_action: str = "unknown"  # add | delete | replace | clear | list | unknown
    memory_content: str = ""
    memory_target: str = ""
    rule_instruction: str = ""


class GroupIntentService:
    def __init__(self, llm: LLMService, *, context_items: int = 4) -> None:
        self.llm = llm
        self.context_items = max(0, int(context_items))

    @staticmethod
    def _extract_json(payload: str) -> dict | None:
        text = (payload or "").strip()
        if text.startswith("```"):
            text = re.sub(r"^```(?:json)?", "", text).strip()
            text = re.sub(r"```$", "", text).strip()
        if not text.startswith("{"):
            m = re.search(r"\{[\s\S]*\}", text)
            if m:
                text = m.group(0)
        try:
            data = json.loads(text)
        except Exception:
            return None
        return data if isinstance(data, dict) else None

    def _build_recent_context(self, history: list[dict[str, str]] | None) -> str:
        if not history or self.context_items <= 0:
            return "(empty)"
        lines: list[str] = []
        for item in history[-self.context_items :]:
            role = clean_text(str(item.get("role", "user")), max_len=16)
            content = clean_text(str(item.get("content", "")), max_len=180)
            if not content:
                continue
            lines.append(f"[{role}] {content}")
        return "\n".join(lines) if lines else "(empty)"

    async def detect(
        self,
        text: str,
        *,
        history: list[dict[str, str]] | None = None,
    ) -> GroupIntent:
        user_text = clean_text(text, max_len=1200)
        prompt = (
            "[RECENT_CONTEXT]\n"
            f"{wrap_untrusted('recent_context', self._build_recent_context(history), max_len=1200)}\n\n"
            "[CURRENT_MESSAGE]\n"
            f"{wrap_untrusted('current_message', user_text, max_len=1200)}"
        )
        raw = await self.llm.decision(build_defended_system(GROUP_INTENT_SYSTEM), prompt)
        data = self._extract_json(raw)
        if not data:
            log.info("group intent parse failed, fallback chat")
            return GroupIntent()

        intent = clean_text(str(data.get("intent", "chat")).lower(), max_len=24)
        if intent not in {"chat", "memory_manage", "rule_manage"}:
            intent = "chat"

        memory_action = clean_text(str(data.get("memory_action", "unknown")).lower(), max_len=24)
        if memory_action not in {"add", "delete", "replace", "clear", "list", "unknown"}:
            memory_action = "unknown"

        memory_content = clean_text(str(data.get("memory_content", "")), max_len=1200)
        memory_target = clean_text(str(data.get("memory_target", "")), max_len=300)
        rule_instruction = clean_text(str(data.get("rule_instruction", "")), max_len=1200)

        return GroupIntent(
            intent=intent,
            memory_action=memory_action,
            memory_content=memory_content,
            memory_target=memory_target,
            rule_instruction=rule_instruction,
        )
