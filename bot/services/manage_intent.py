from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING

from bot.utils.prompts import MANAGE_INTENT_SYSTEM
from bot.utils.runtime_context import build_current_time_context
from bot.utils.security import build_defended_system, clean_text, wrap_untrusted

if TYPE_CHECKING:
    from bot.services.llm import LLMService

log = logging.getLogger(__name__)

_POLITE_PREFIX = r"(?:(?:请|请你|麻烦|麻烦你|帮我|帮忙|bot|@bot)[,，:：\s]*)*"
_MEMORY_REF = r"(?:这个|这条|这句|这段|这一条|上一条|上面那条|上面这句|前面那条|前面这句|刚才那条|刚刚那条)"

_MEMORY_NON_COMMAND_PATTERNS = (
    re.compile(r"^\s*(?:大家|请大家|所有人|你们)\s*记住"),
    re.compile(r"(?:记不记得|还记得|记住了吗|你记住了吗)"),
    re.compile(r"(?:永久记忆是什么|什么是永久记忆|永久记忆是什么意思|永久记忆怎么用)"),
    re.compile(r"(?:怎么|如何|怎样|为啥|为什么).*(?:写入|写进|加入|删除|清空|查看|修改|更新).*(?:永久记忆|记忆)"),
)

_MEMORY_ADD_PATTERNS = (
    re.compile(
        rf"^\s*{_POLITE_PREFIX}(?:记住|记一下)\s*(?:[:：,，]\s*)?.+\s*$",
        re.IGNORECASE,
    ),
    re.compile(
        rf"^\s*{_POLITE_PREFIX}(?:把|将).+(?:写入|记到|加入|存到)(?:永久记忆|记忆)\s*$",
        re.IGNORECASE,
    ),
    re.compile(
        rf"^\s*{_POLITE_PREFIX}(?:添加|新增)(?:一条)?永久记忆\s*(?:[:：,，]\s*)?.+\s*$",
        re.IGNORECASE,
    ),
    re.compile(
        rf"^\s*{_POLITE_PREFIX}.*?{_MEMORY_REF}.{0,12}(?:写入|写进|记到|记进|加入|加到|存到|存入|保存|收录|录入)(?:永久记忆|记忆).*$",
        re.IGNORECASE,
    ),
    re.compile(
        rf"^\s*{_POLITE_PREFIX}.*?(?:写入|写进|记到|记进|加入|加到|存到|存入|保存|收录|录入)(?:永久记忆|记忆).*$",
        re.IGNORECASE,
    ),
)

_MEMORY_REPLACE_PATTERNS = (
    re.compile(
        rf"^\s*{_POLITE_PREFIX}(?:把|将).+(?:永久记忆|记忆).+(?:改成|改为|更新为|替换为).+\s*$",
        re.IGNORECASE,
    ),
    re.compile(
        rf"^\s*{_POLITE_PREFIX}(?:把|将)[\"“].+[\"”](?:改成|改为|更新为|替换为)[\"“].+[\"”]\s*$",
        re.IGNORECASE,
    ),
    re.compile(
        rf"^\s*{_POLITE_PREFIX}.*?(?:永久记忆|记忆).*(?:改成|改为|更新为|替换为).+\s*$",
        re.IGNORECASE,
    ),
)

_MEMORY_DELETE_PATTERNS = (
    re.compile(
        rf"^\s*{_POLITE_PREFIX}(?:删除|删掉|移除)(?:一下)?(?:永久记忆|记忆)\s*(?:#?\d+|.+)\s*$",
        re.IGNORECASE,
    ),
    re.compile(
        rf"^\s*{_POLITE_PREFIX}(?:忘掉|删掉)(?:刚才那条|这条)(?:永久记忆|记忆).*$",
        re.IGNORECASE,
    ),
    re.compile(
        rf"^\s*{_POLITE_PREFIX}.*?(?:永久记忆|记忆).*(?:删除|删掉|移除|去掉).*$",
        re.IGNORECASE,
    ),
)

_MEMORY_CLEAR_PATTERNS = (
    re.compile(
        rf"^\s*{_POLITE_PREFIX}(?:清空|清掉|删光|删除全部|全部删除)(?:所有)?(?:永久记忆|记忆)\s*$",
        re.IGNORECASE,
    ),
)

_MEMORY_LIST_PATTERNS = (
    re.compile(
        rf"^\s*{_POLITE_PREFIX}(?:查看|列出|显示|看看)(?:一下)?永久记忆(?:列表)?\s*$",
        re.IGNORECASE,
    ),
    re.compile(
        rf"^\s*{_POLITE_PREFIX}永久记忆列表\s*$",
        re.IGNORECASE,
    ),
)

_RULE_ADD_PATTERNS = (
    re.compile(
        rf"^\s*{_POLITE_PREFIX}(?:(?:新增|添加|增加)(?:一条)?|加一条)(?:一下)?(?:群规|规则|审核规则)\s*(?:[:：,，]\s*)?.+\s*$",
        re.IGNORECASE,
    ),
)

_RULE_DELETE_PATTERNS = (
    re.compile(
        rf"^\s*{_POLITE_PREFIX}(?:删除|删掉|移除)(?:一下)?(?:第\s*#?\d+\s*条)?(?:群规|规则|审核规则).*\s*$",
        re.IGNORECASE,
    ),
)

_RULE_LIST_PATTERNS = (
    re.compile(
        rf"^\s*{_POLITE_PREFIX}(?:查看|列出|显示|看看)(?:一下)?(?:群规|规则|审核规则)(?:列表)?\s*$",
        re.IGNORECASE,
    ),
    re.compile(
        rf"^\s*{_POLITE_PREFIX}(?:群规列表|规则列表|审核规则列表)\s*$",
        re.IGNORECASE,
    ),
)

_TASK_REQUEST_RE = re.compile(
    r"(提醒我|提醒下我|记得提醒我|记得叫我|到时候提醒我|到时提醒我|到点提醒我|回头提醒我|帮我|给我|麻烦你|麻烦帮我)",
    re.IGNORECASE,
)
_TASK_COMMAND_RE = re.compile(
    r"(查找|查询|搜索|搜一下|找一下|概述|总结|整理|汇总|收集|统计|生成|提醒|通知|告诉我|发我|整理下|看一下)",
    re.IGNORECASE,
)
_TASK_DELETE_RE = re.compile(
    r"(取消|删掉|删除|去掉|撤销|不用了|不要了|算了|取消掉|删了)",
    re.IGNORECASE,
)
_TASK_REFERENCE_RE = re.compile(
    r"(提醒|定时任务|定时|任务|安排)",
    re.IGNORECASE,
)
_TASK_ID_RE = re.compile(
    r"(?:#|任务\s*#?|提醒\s*#?)(\d{1,9})",
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
_STRONG_TIME_HINT_RE = re.compile(
    r"(今晚|等会|待会|稍后|分钟后|小时后|早上|上午|中午|下午|傍晚|晚上|凌晨|明晚|\d+\s*[点时号分])",
    re.IGNORECASE,
)


@dataclass(slots=True)
class GroupIntent:
    intent: str = "chat"  # chat | memory_manage | rule_manage
    memory_action: str = "unknown"  # add | delete | replace | clear | list | unknown
    memory_content: str = ""
    memory_target: str = ""
    rule_action: str = "unknown"  # add | delete | list | unknown
    rule_id: int = 0
    rule_type: str = "unknown"  # keyword | regex | llm | unknown
    rule_pattern: str = ""
    rule_hit_action: str = "unknown"  # warn | delete | ban | unknown
    rule_instruction: str = ""


@dataclass(slots=True)
class TaskIntent:
    intent: str = "chat"
    task_action: str = "unknown"
    task_type: str = "unknown"
    due_at: datetime | None = None
    task_id: int = 0
    task_content: str = ""
    ack_text: str = ""


class ManagementIntentService:
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

    async def _analyze(
        self,
        text: str,
        *,
        history: list[dict[str, str]] | None = None,
    ) -> dict | None:
        prompt = (
            f"{build_current_time_context()}\n\n"
            "[RECENT_CONTEXT]\n"
            f"{wrap_untrusted('recent_context', self._build_recent_context(history), max_len=1200)}\n\n"
            "[CURRENT_MESSAGE]\n"
            f"{wrap_untrusted('current_message', clean_text(text, max_len=1200), max_len=1200)}"
        )
        raw = await self.llm.decision(build_defended_system(MANAGE_INTENT_SYSTEM), prompt)
        return self._extract_json(raw)

    @staticmethod
    def _matches_any(patterns: tuple[re.Pattern[str], ...], text: str) -> bool:
        return any(pattern.search(text) for pattern in patterns)

    @classmethod
    def _is_explicit_memory_command(cls, text: str, action: str | None = None) -> bool:
        if not text:
            return False
        if cls._matches_any(_MEMORY_NON_COMMAND_PATTERNS, text):
            return False

        action_patterns = {
            "add": _MEMORY_ADD_PATTERNS,
            "replace": _MEMORY_REPLACE_PATTERNS,
            "delete": _MEMORY_DELETE_PATTERNS,
            "clear": _MEMORY_CLEAR_PATTERNS,
            "list": _MEMORY_LIST_PATTERNS,
        }
        if action:
            patterns = action_patterns.get(action)
            return cls._matches_any(patterns, text) if patterns else False

        return any(cls._matches_any(patterns, text) for patterns in action_patterns.values())

    @classmethod
    def _is_explicit_rule_command(cls, text: str) -> bool:
        if not text:
            return False
        return any(
            cls._matches_any(patterns, text)
            for patterns in (_RULE_ADD_PATTERNS, _RULE_DELETE_PATTERNS, _RULE_LIST_PATTERNS)
        )

    @classmethod
    def _looks_like_management_candidate(cls, text: str) -> bool:
        stripped = (text or "").strip()
        if not stripped:
            return False
        return cls._is_explicit_memory_command(stripped) or cls._is_explicit_rule_command(stripped)

    @staticmethod
    def _looks_like_task_candidate(text: str) -> bool:
        normalized = clean_text(text, max_len=1200)
        if not normalized:
            return False
        if _TASK_DELETE_RE.search(normalized):
            return any(
                pattern.search(normalized)
                for pattern in (_TASK_REFERENCE_RE, _TASK_ID_RE, _TIME_HINT_RE, _TASK_COMMAND_RE)
            )
        if not _TIME_HINT_RE.search(normalized):
            return False
        if _TASK_REQUEST_RE.search(normalized):
            return True
        if _SCHEDULE_KEYWORD_RE.search(normalized) and _TASK_COMMAND_RE.search(normalized):
            return True
        if _STRONG_TIME_HINT_RE.search(normalized) and _TASK_COMMAND_RE.search(normalized):
            return True
        return False

    def looks_like_management_candidate(self, text: str) -> bool:
        return self._looks_like_management_candidate(clean_text(text, max_len=1200))

    def looks_like_task_candidate(self, text: str) -> bool:
        return self._looks_like_task_candidate(text)

    @staticmethod
    def _parse_positive_id(value: object) -> int:
        if value is None:
            return 0
        text = clean_text(str(value), max_len=32)
        if not text:
            return 0
        match = re.search(r"\d{1,9}", text)
        if not match:
            return 0
        try:
            return max(0, int(match.group(0)))
        except ValueError:
            return 0

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

    @staticmethod
    def _extract_task_id(text: str) -> int:
        normalized = clean_text(text, max_len=1200)
        if not normalized:
            return 0
        match = _TASK_ID_RE.search(normalized)
        if not match:
            return 0
        try:
            return max(0, int(match.group(1)))
        except ValueError:
            return 0

    async def detect_group_intent(
        self,
        text: str,
        *,
        history: list[dict[str, str]] | None = None,
        surface_text: str | None = None,
        force_llm: bool = False,
    ) -> GroupIntent:
        user_text = clean_text(text, max_len=1200)
        surface = clean_text(surface_text or text, max_len=400)
        if not force_llm and not self._looks_like_management_candidate(surface):
            return GroupIntent()

        data = await self._analyze(user_text, history=history)
        if not data:
            log.info("manage intent parse failed for group action, fallback chat")
            return GroupIntent()

        intent = clean_text(str(data.get("intent", "chat")).lower(), max_len=24)
        if intent not in {"chat", "memory_manage", "rule_manage"}:
            intent = "chat"

        memory_action = clean_text(str(data.get("memory_action", "unknown")).lower(), max_len=24)
        if memory_action not in {"add", "delete", "replace", "clear", "list", "unknown"}:
            memory_action = "unknown"

        rule_action = clean_text(str(data.get("rule_action", "unknown")).lower(), max_len=24)
        if rule_action not in {"add", "delete", "list", "unknown"}:
            rule_action = "unknown"

        rule_type = clean_text(str(data.get("rule_type", "unknown")).lower(), max_len=16)
        if rule_type not in {"keyword", "regex", "llm", "unknown"}:
            rule_type = "unknown"

        rule_hit_action = clean_text(str(data.get("rule_hit_action", "unknown")).lower(), max_len=16)
        if rule_hit_action not in {"warn", "delete", "ban", "unknown"}:
            rule_hit_action = "unknown"

        intent_result = GroupIntent(
            intent=intent,
            memory_action=memory_action,
            memory_content=clean_text(str(data.get("memory_content", "")), max_len=1200),
            memory_target=clean_text(str(data.get("memory_target", "")), max_len=300),
            rule_action=rule_action,
            rule_id=self._parse_positive_id(data.get("rule_id")),
            rule_type=rule_type,
            rule_pattern=clean_text(str(data.get("rule_pattern", "")), max_len=1000),
            rule_hit_action=rule_hit_action,
            rule_instruction=clean_text(str(data.get("rule_instruction", "")), max_len=1200),
        )
        if intent_result.intent == "rule_manage" and not intent_result.rule_instruction:
            intent_result.rule_instruction = user_text

        if intent_result.intent == "memory_manage" and not force_llm:
            if memory_action == "unknown" or not self._is_explicit_memory_command(surface, memory_action):
                log.info("manage intent downgraded to chat | reason=memory_not_explicit")
                return GroupIntent()

        if intent_result.intent == "rule_manage" and not force_llm and not self._is_explicit_rule_command(surface):
            log.info("manage intent downgraded to chat | reason=rule_not_explicit")
            return GroupIntent()

        return intent_result

    async def detect_task_intent(
        self,
        text: str,
        *,
        history: list[dict[str, str]] | None = None,
        force_llm: bool = False,
    ) -> TaskIntent:
        user_text = clean_text(text, max_len=1200)
        if not force_llm and not self._looks_like_task_candidate(user_text):
            return TaskIntent()

        data = await self._analyze(user_text, history=history)
        if not data:
            log.info("manage intent parse failed for task action, fallback chat")
            return TaskIntent()

        intent = clean_text(str(data.get("intent", "chat")).lower(), max_len=24)
        action = clean_text(str(data.get("task_action", "unknown")).lower(), max_len=24)
        task_type = clean_text(str(data.get("task_type", "unknown")).lower(), max_len=24)
        due_at = self._parse_due_at(str(data.get("due_at", "")))
        task_id = self._parse_positive_id(data.get("task_id")) or self._extract_task_id(user_text)
        task_content = clean_text(str(data.get("task_content", "")), max_len=300)
        ack_text = clean_text(str(data.get("ack_text", "")), max_len=120)

        if intent != "task_manage":
            return TaskIntent()

        if action == "add":
            if task_type not in {"reminder", "agent_task"}:
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
                task_id=0,
                task_content=task_content,
                ack_text=ack_text,
            )

        if action != "delete":
            return TaskIntent()

        if task_type not in {"reminder", "agent_task"}:
            task_type = "unknown"
        if not ack_text:
            ack_text = "好，我来取消这个定时任务。"
        return TaskIntent(
            intent="task_manage",
            task_action="delete",
            task_type=task_type,
            due_at=due_at,
            task_id=task_id,
            task_content=task_content,
            ack_text=ack_text,
        )


class GroupIntentService(ManagementIntentService):
    async def detect(
        self,
        text: str,
        *,
        history: list[dict[str, str]] | None = None,
        surface_text: str | None = None,
        force_llm: bool = False,
    ) -> GroupIntent:
        return await self.detect_group_intent(
            text,
            history=history,
            surface_text=surface_text,
            force_llm=force_llm,
        )


class TaskIntentService(ManagementIntentService):
    async def detect(
        self,
        text: str,
        *,
        history: list[dict[str, str]] | None = None,
        force_llm: bool = False,
    ) -> TaskIntent:
        return await self.detect_task_intent(
            text,
            history=history,
            force_llm=force_llm,
        )
