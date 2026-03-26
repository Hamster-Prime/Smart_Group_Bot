from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass

from bot.services.llm import LLMService
from bot.utils.prompts import GROUP_INTENT_SYSTEM
from bot.utils.security import build_defended_system, clean_text, wrap_untrusted

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

        return any(
            cls._matches_any(patterns, text)
            for patterns in action_patterns.values()
        )

    @classmethod
    def _is_explicit_rule_command(cls, text: str) -> bool:
        if not text:
            return False
        return any(
            cls._matches_any(patterns, text)
            for patterns in (
                _RULE_ADD_PATTERNS,
                _RULE_DELETE_PATTERNS,
                _RULE_LIST_PATTERNS,
            )
        )

    @classmethod
    def _looks_like_management_candidate(cls, text: str) -> bool:
        stripped = (text or "").strip()
        if not stripped:
            return False
        return cls._is_explicit_memory_command(stripped) or cls._is_explicit_rule_command(
            stripped
        )

    def looks_like_management_candidate(self, text: str) -> bool:
        return self._looks_like_management_candidate(clean_text(text, max_len=1200))

    async def detect(
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

        if intent == "memory_manage" and not force_llm:
            if memory_action == "unknown" or not self._is_explicit_memory_command(surface, memory_action):
                log.info("group intent downgraded to chat | reason=memory_not_explicit")
                return GroupIntent()

        if intent == "rule_manage" and not force_llm and not self._is_explicit_rule_command(surface):
            log.info("group intent downgraded to chat | reason=rule_not_explicit")
            return GroupIntent()

        return GroupIntent(
            intent=intent,
            memory_action=memory_action,
            memory_content=memory_content,
            memory_target=memory_target,
            rule_instruction=rule_instruction,
        )
