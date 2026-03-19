from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from datetime import datetime

from bot.services.llm import LLMService
from bot.utils.prompts import TASK_INTENT_SYSTEM
from bot.utils.runtime_context import build_current_time_context
from bot.utils.security import build_defended_system, clean_text, wrap_untrusted

log = logging.getLogger(__name__)

_TASK_REQUEST_RE = re.compile(
    r"(提醒我|提醒下我|记得提醒我|记得叫我|到时候提醒我|到时提醒我|到点提醒我|回头提醒我|帮我|给我|麻烦你|麻烦帮我)",
    re.IGNORECASE,
)
_TASK_COMMAND_RE = re.compile(
    r"(查找|查询|搜索|搜一下|找一下|概述|总结|整理|汇总|收集|统计|生成|提醒|通知|告诉我|发我|整理下|看一下)",
    re.IGNORECASE,
)
_SCHEDULE_KEYWORD_RE = re.compile(
    r"(定时|到时候|到时|到了|到点)",
    re.IGNORECASE,
)
_TIME_HINT_RE = re.compile(
    r"(今天|今晚|明天|后天|等会|待会|稍后|分钟后|小时后|早上|上午|中午|下午|傍晚|晚上|凌晨|明晚|周[一二三四五六日天]|星期[一二三四五六日天]|\d+\s*[点时号分])",
    re.IGNORECASE,
)


@dataclass(slots=True)
class TaskIntent:
    intent: str = "chat"
    task_action: str = "unknown"
    task_type: str = "unknown"
    due_at: datetime | None = None
    task_content: str = ""
    ack_text: str = ""


class TaskIntentService:
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
            match = re.search(r"\{[\s\S]*\}", text)
            if match:
                text = match.group(0)
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
            if content:
                lines.append(f"[{role}] {content}")
        return "\n".join(lines) if lines else "(empty)"

    @staticmethod
    def _looks_like_task_candidate(text: str) -> bool:
        normalized = clean_text(text, max_len=1200)
        if not normalized:
            return False
        if not _TIME_HINT_RE.search(normalized):
            return False
        if _TASK_REQUEST_RE.search(normalized):
            return True
        if _SCHEDULE_KEYWORD_RE.search(normalized) and _TASK_COMMAND_RE.search(normalized):
            return True
        if _TASK_COMMAND_RE.search(normalized):
            return True
        return False

    def looks_like_task_candidate(self, text: str) -> bool:
        return self._looks_like_task_candidate(text)

    @staticmethod
    def _parse_due_at(value: str) -> datetime | None:
        text = clean_text(value, max_len=32)
        if not text:
            return None
        try:
            naive = datetime.strptime(text, "%Y-%m-%d %H:%M:%S")
        except ValueError:
            return None
        local_tz = datetime.now().astimezone().tzinfo
        return naive.replace(tzinfo=local_tz)

    async def detect(
        self,
        text: str,
        *,
        history: list[dict[str, str]] | None = None,
    ) -> TaskIntent:
        user_text = clean_text(text, max_len=1200)
        if not self._looks_like_task_candidate(user_text):
            return TaskIntent()

        prompt = (
            f"{build_current_time_context()}\n\n"
            "[RECENT_CONTEXT]\n"
            f"{wrap_untrusted('recent_context', self._build_recent_context(history), max_len=1200)}\n\n"
            "[CURRENT_MESSAGE]\n"
            f"{wrap_untrusted('current_message', user_text, max_len=1200)}"
        )
        raw = await self.llm.decision(build_defended_system(TASK_INTENT_SYSTEM), prompt)
        data = self._extract_json(raw)
        if not data:
            log.info("task intent parse failed, fallback chat")
            return TaskIntent()

        intent = clean_text(str(data.get("intent", "chat")).lower(), max_len=24)
        action = clean_text(str(data.get("task_action", "unknown")).lower(), max_len=24)
        task_type = clean_text(str(data.get("task_type", "unknown")).lower(), max_len=24)
        due_at = self._parse_due_at(str(data.get("due_at", "")))
        task_content = clean_text(str(data.get("task_content", "")), max_len=300)
        ack_text = clean_text(str(data.get("ack_text", "")), max_len=120)

        if intent != "task_manage" or action != "add" or task_type not in {"reminder", "agent_task"}:
            return TaskIntent()
        if due_at is None or not task_content:
            return TaskIntent()
        if not ack_text:
            ack_text = "好，到时间我会处理。"

        return TaskIntent(
            intent="task_manage",
            task_action="add",
            task_type=task_type,
            due_at=due_at,
            task_content=task_content,
            ack_text=ack_text,
        )
