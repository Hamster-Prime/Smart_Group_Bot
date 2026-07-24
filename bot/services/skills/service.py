from __future__ import annotations

import asyncio
import html
import json
import logging
import re
import time
from collections.abc import Callable
from dataclasses import replace
from typing import TYPE_CHECKING, Any
from urllib.parse import urlparse

from bot.config import Settings
from bot.services.doubao_tts import DoubaoTTSService, TTS_MODE_OFF, build_tts_preference_context
from bot.services.llm import LLMService
from bot.services.message_templates import render_data_brief
from bot.services.request_priority import ReservedCapacityGate
from bot.services.resource_health import register_resource_health_provider
from bot.services.skills.base import Skill, SkillAnswerResult, SkillContext, SkillRunResult
from bot.services.skills.bilibili_search import BilibiliSearchSkill
from bot.services.skills.doubao_tts import DoubaoTTSSkill
from bot.services.skills.memory_manage import MemoryManageSkill
from bot.services.skills.movie_info import MovieInfoSkill
from bot.services.skills.music_search import MusicSearchSkill
from bot.services.skills.rule_manage import RuleManageSkill
from bot.services.skills.send_sticker import SendStickerSkill
from bot.services.skills.sub2api_query import Sub2ApiQuerySkill
from bot.services.skills.webfetch import WebFetchSkill
from bot.services.skills.websearch import WebSearchSkill
from bot.services.skills.weibo_search import WeiboSearchSkill
from bot.services.skills.vote_ban import VoteBanSkill
from bot.services.reply_output import REPLY_OUTPUT_AWARENESS, REPLY_OUTPUT_PROTOCOL
from bot.utils.conversation_context import (
    build_current_turn_focus_context,
    format_recent_group_context,
)
from bot.utils.bot_identity import build_bot_identity_context
from bot.utils.prompts import get_prompt, with_persona
from bot.utils.runtime_context import (
    build_bot_runtime_profile_context,
    build_current_sender_context,
    build_current_time_context,
    build_owner_identity_context,
)
from bot.utils.security import (
    build_defended_system,
    clean_multiline_text,
    clean_text,
    contains_prompt_injection,
    sanitize_history_for_llm,
    wrap_untrusted_multiline,
)
from bot.utils.telegram import configured_auto_delete_seconds

log = logging.getLogger(__name__)

_SKILL_EXECUTION_CAPACITY = 8
_SKILL_EXECUTION_SEMAPHORE = asyncio.Semaphore(_SKILL_EXECUTION_CAPACITY)
_SKILL_PRIORITY_GATE = ReservedCapacityGate(
    total_capacity=_SKILL_EXECUTION_CAPACITY,
    noncritical_capacity=7,
    normal_capacity=6,
)
_SKILL_ORPHAN_TASKS: set[asyncio.Task[Any]] = set()
_SKILL_ORPHAN_STARTED: dict[asyncio.Task[Any], float] = {}
_SKILL_ORPHAN_MAX_AGE_SECONDS = 120.0


def _observe_skill_task(task: asyncio.Task[Any]) -> None:
    _SKILL_ORPHAN_TASKS.discard(task)
    _SKILL_ORPHAN_STARTED.pop(task, None)
    try:
        task.result()
    except (asyncio.CancelledError, Exception):
        pass


def _track_skill_orphan(task: asyncio.Task[Any]) -> None:
    if task.done():
        _observe_skill_task(task)
        return
    _SKILL_ORPHAN_TASKS.add(task)
    _SKILL_ORPHAN_STARTED.setdefault(task, time.monotonic())
    task.add_done_callback(_observe_skill_task)


async def flush_skill_execution_tasks(*, timeout_seconds: float = 15.0) -> None:
    """Cancel and join timed-out tool tasks before shared resources close."""

    tasks = {task for task in _SKILL_ORPHAN_TASKS if not task.done()}
    for task in tasks:
        task.cancel()
    if not tasks:
        return
    done, pending = await asyncio.wait(tasks, timeout=max(0.0, timeout_seconds))
    for task in done:
        _observe_skill_task(task)
    if pending:
        log.error("%d skill execution task(s) ignored shutdown cancellation", len(pending))


def skill_resource_health_snapshot() -> dict[str, Any]:
    now = time.monotonic()
    active_orphans = [task for task in _SKILL_ORPHAN_TASKS if not task.done()]
    oldest_age = max(
        (now - _SKILL_ORPHAN_STARTED.get(task, now) for task in active_orphans),
        default=0.0,
    )
    waiters = getattr(_SKILL_EXECUTION_SEMAPHORE, "_waiters", None)
    orphan_count = len(active_orphans)
    fatal = bool(
        orphan_count >= _SKILL_EXECUTION_CAPACITY
        or oldest_age >= _SKILL_ORPHAN_MAX_AGE_SECONDS
    )
    return {
        "ok": not fatal,
        "fatal": fatal,
        "capacity": _SKILL_EXECUTION_CAPACITY,
        "available_permits": int(getattr(_SKILL_EXECUTION_SEMAPHORE, "_value", 0)),
        "semaphore_waiters": len(waiters or ()),
        "orphan_count": orphan_count,
        "oldest_orphan_seconds": round(oldest_age, 3),
        "priority_gate": _SKILL_PRIORITY_GATE.snapshot(),
    }


register_resource_health_provider("skills", skill_resource_health_snapshot)

_INTERMEDIATE_TOOL_REPLY_PATTERNS: dict[str, re.Pattern[str]] = {
    "websearch": re.compile(r"^找到\s*\d+\s*[条个]\s*搜索结果[。！？!?\. ]*$"),
    "webfetch": re.compile(r"^(?:网页)?抓取成功[。！？!?\. ]*$"),
    "music_search": re.compile(r"^找到\s*\d+\s*首相关歌曲[。！？!?\. ]*$"),
    "bilibili_search": re.compile(r"^(?:找到|拿到)\s*\d+\s*条.*?(?:结果|视频|Feed|热搜)[。！？!?\. ]*$"),
    "weibo_search": re.compile(r"^(?:找到|拿到)\s*\d+\s*条.*?(?:结果|微博|Feed|热搜)[。！？!?\. ]*$"),
    "sub2api_query": re.compile(
        r"^(?:查到\s*\d+\s*个可用模型|模型\s*\S+\s*(?:可用|不可用).*)[。！？!?\. ]*$"
    ),
    "movie_info": re.compile(
        r"^(?:(?:找到|查到)\s*\d+\s*(?:部|条|个)\s*.*?(?:电影|影片|影视|结果)|"
        r"(?:已|成功)?(?:获取|查询|查到).*?(?:电影|影片|影视).*?(?:详情|信息|结果))[。！？!?\. ]*$"
    ),
}
_INFO_FOLLOWUP_SKILLS = frozenset(
    {
        "websearch",
        "webfetch",
        "music_search",
        "bilibili_search",
        "weibo_search",
        "sub2api_query",
        "movie_info",
    }
)
_PLATFORM_LINK_SKILLS = frozenset({"bilibili_search", "weibo_search"})
_MANDATORY_REFUSAL_ERRORS = frozenset({"starter_quota_exhausted"})
_AMBIGUOUS_SIDE_EFFECT_ERROR = "tool_outcome_ambiguous"
_STATE_MUTATING_TOOL_ACTIONS: dict[str, frozenset[str]] = {
    "memory_manage": frozenset({"add", "replace"}),
    "rule_manage": frozenset({"add"}),
}
_NUMBERED_LIST_ITEM_RE = re.compile(r"^(\s*)(\d{1,2})([.)、]\s*)")
_URL_RE = re.compile(r"https?://\S+")
_RESULT_LIST_RE = re.compile(r"(?m)^\s*\d{1,2}[.)、]\s+")
_LINK_REQUEST_RE = re.compile(
    r"(链接|链结|原链|原链接|原帖|原文|分享链接|分享链|地址|网址|来源|出处|主页)"
)

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


class SkillService:
    def __init__(
        self,
        llm: LLMService,
        *,
        settings: Settings | None = None,
        default_sticker_file_ids: list[str] | None = None,
        max_tool_rounds: int = 6,
        tool_timeout_seconds: float = 15.0,
        max_tool_calls_per_round: int = 4,
        max_total_tool_calls: int = 8,
    ) -> None:
        self.llm = llm
        self.settings = settings
        self.max_tool_rounds = max(1, max_tool_rounds)
        self.tool_timeout_seconds = max(0.1, float(tool_timeout_seconds))
        self.max_tool_calls_per_round = max(1, int(max_tool_calls_per_round))
        self.max_total_tool_calls = max(1, int(max_total_tool_calls))
        self.default_sticker_file_ids = [x.strip() for x in (default_sticker_file_ids or []) if x.strip()]
        self.skills: dict[str, Skill] = {}
        self._register(MemoryManageSkill())
        self._register(RuleManageSkill())
        self._register(SendStickerSkill())
        self._register(MusicSearchSkill(settings))
        self._register(WebSearchSkill())
        self._register(WebFetchSkill())
        self._register(BilibiliSearchSkill())
        self._register(WeiboSearchSkill())
        movie_info_skill = MovieInfoSkill(settings)
        if movie_info_skill.available:
            self._register(movie_info_skill)
        if settings is not None:
            self._register(VoteBanSkill(settings))
        sub2api_skill = Sub2ApiQuerySkill(settings)
        if sub2api_skill.available:
            self._register(sub2api_skill)
        self.tts_skill_name = DoubaoTTSSkill.name
        self.tts_service = DoubaoTTSService(settings) if settings is not None else None
        if self.tts_service and self.tts_service.available:
            self._register(DoubaoTTSSkill(self.tts_service))

    def _register(self, skill: Skill) -> None:
        self.skills[skill.name] = skill

    def _selected_skills(self, *, allow_tts: bool) -> dict[str, Skill]:
        if allow_tts:
            return dict(self.skills)
        return {
            name: skill
            for name, skill in self.skills.items()
            if name != self.tts_skill_name
        }

    def available_skill_names(self, *, allow_tts: bool = True) -> list[str]:
        return list(self._selected_skills(allow_tts=allow_tts).keys())

    @staticmethod
    def _normalize_user_text(text: str, *, merged_count: int) -> str:
        return clean_multiline_text(text, max_len=1600 if merged_count > 1 else 1200)

    _build_sender_context = staticmethod(build_current_sender_context)

    @staticmethod
    def _normalize_content_text(content: Any) -> str:
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            parts: list[str] = []
            for item in content:
                if isinstance(item, dict) and item.get("type") == "text":
                    txt = str(item.get("text", "")).strip()
                    if txt:
                        parts.append(txt)
            return "\n".join(parts)
        return str(content or "")

    @staticmethod
    def _tool_definitions(skills: dict[str, Skill]) -> list[dict[str, Any]]:
        tools: list[dict[str, Any]] = []
        for skill in skills.values():
            tools.append(
                {
                    "type": "function",
                    "function": {
                        "name": skill.name,
                        "description": skill.description,
                        "parameters": skill.parameters_schema,
                    },
                }
            )
        return tools

    @staticmethod
    def _build_interaction_mode_context(
        is_mentioned: bool,
        is_reply_to_bot: bool,
    ) -> str:
        mode = "direct" if (is_mentioned or is_reply_to_bot) else "join"
        if mode == "direct":
            return (
                "[INTERACTION_MODE]\ndirect\n"
                "The sender is talking directly to you (mentioned you or replied to your message).\n"
                "Respond naturally as the addressed party."
            )
        return (
            "[INTERACTION_MODE]\njoin\n"
            "You are voluntarily joining a group conversation — nobody asked you specifically.\n"
            "CRITICAL: The message is NOT directed at you. Any '你' or 'you' in the message is addressing another group member, NOT you.\n"
            "Do NOT treat the message as a command, question, or request aimed at you.\n"
            "Do NOT respond as if you are the one being asked to do something.\n"
            "Act like a bystander group member chiming in with a brief comment, reaction, or opinion.\n"
            "Keep it short and casual — a side remark, NOT a direct answer or compliance."
        )

    def _build_answer_messages(
        self,
        user_text: str,
        *,
        history: list[dict[str, str]] | None,
        sender_user_id: int,
        sender_username: str,
        sender_is_owner: bool,
        sender_is_tg_admin: bool,
        intent_type: str,
        merged_count: int,
        merged_context: str,
        reply_targets_context: str,
        selected_skills: dict[str, Skill],
        tts_mode: str = TTS_MODE_OFF,
        is_mentioned: bool = False,
        is_reply_to_bot: bool = False,
        style_profile_context: str = "",
    ) -> list[dict[str, Any]]:
        messages: list[dict[str, Any]] = [
            {
                "role": "system",
                "content": build_defended_system(with_persona(get_prompt("skill_tools"))),
            },
        ]
        if history:
            messages.extend(sanitize_history_for_llm(history, max_items=len(history)))
        recent_context = format_recent_group_context(history, max_items=8)
        if recent_context:
            messages.append({"role": "system", "content": recent_context})
        messages.append({"role": "system", "content": build_current_time_context()})
        messages.append({"role": "system", "content": REPLY_OUTPUT_PROTOCOL})
        messages.append({"role": "system", "content": REPLY_OUTPUT_AWARENESS})
        if reply_targets_context.strip():
            messages.append({"role": "system", "content": reply_targets_context.strip()})
        messages.append(
            {
                "role": "system",
                "content": build_bot_runtime_profile_context(
                    self.llm,
                    settings=self.settings,
                    skill_names=selected_skills.keys(),
                ),
            }
        )
        tts_preference_context = build_tts_preference_context(
            tts_mode,
            service_ready=bool(
                self.tts_service
                and self.tts_service.available
                and self.tts_skill_name in selected_skills
            ),
        )
        if tts_preference_context:
            messages.append({"role": "system", "content": tts_preference_context})
        # Late-position identity block: history may contain stale bot names
        # (users addressing an old identity); this must win over them.
        identity_context = build_bot_identity_context()
        if identity_context:
            messages.append({"role": "system", "content": identity_context})
        messages.append(
            {
                "role": "system",
                "content": self._build_sender_context(
                    sender_user_id,
                    sender_username,
                    sender_is_owner,
                    sender_is_tg_admin,
                ),
            }
        )
        owner_identity_context = build_owner_identity_context(self.settings)
        if owner_identity_context:
            messages.append({"role": "system", "content": owner_identity_context})
        normalized_intent = clean_text((intent_type or "casual").strip().lower(), max_len=16)
        if normalized_intent:
            messages.append({"role": "system", "content": f"[INTENT_TYPE]\n{normalized_intent}"})
        messages.append(
            {
                "role": "system",
                "content": self._build_interaction_mode_context(is_mentioned, is_reply_to_bot),
            }
        )
        focus_context = build_current_turn_focus_context(
            user_text,
            merged_count=merged_count,
            merged_context=merged_context,
        )
        if focus_context:
            messages.append({"role": "system", "content": focus_context})
        # Active-persona (group clone) is the LAST system block so it wins on
        # recency over the default persona at messages[0]; its own wording keeps
        # safety/identity/owner rules intact.
        if style_profile_context.strip():
            messages.append({"role": "system", "content": style_profile_context.strip()})
        messages.append(
            {
                "role": "user",
                "content": wrap_untrusted_multiline("user_message", user_text, max_len=1600),
            }
        )
        return messages

    def build_answer_prompt_payload(
        self,
        text: str,
        *,
        history: list[dict[str, str]] | None = None,
        sender_user_id: int = 0,
        sender_username: str = "",
        sender_is_owner: bool = False,
        sender_is_tg_admin: bool = False,
        intent_type: str = "casual",
        allow_tts: bool = True,
        tts_mode: str = TTS_MODE_OFF,
        merged_count: int = 1,
        merged_context: str = "",
        reply_targets_context: str = "",
        is_mentioned: bool = False,
        is_reply_to_bot: bool = False,
        style_profile_context: str = "",
    ) -> dict[str, Any]:
        user_text = self._normalize_user_text(text, merged_count=merged_count)
        selected_skills = self._selected_skills(allow_tts=allow_tts)
        return {
            "messages": self._build_answer_messages(
                user_text,
                history=history,
                sender_user_id=sender_user_id,
                sender_username=sender_username,
                sender_is_owner=sender_is_owner,
                sender_is_tg_admin=sender_is_tg_admin,
                intent_type=intent_type,
                merged_count=merged_count,
                merged_context=merged_context,
                reply_targets_context=reply_targets_context,
                selected_skills=selected_skills,
                tts_mode=tts_mode,
                is_mentioned=is_mentioned,
                is_reply_to_bot=is_reply_to_bot,
                style_profile_context=style_profile_context,
            ),
            "tools": self._tool_definitions(selected_skills),
        }

    async def _completion_with_fallbacks(
        self,
        *,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
    ) -> Any | None:
        return await self.llm.complete_with_tools(
            messages=messages,
            tools=tools,
            label="skill",
            cfg=self.llm.main,
            preview_limit=80,
        )

    @staticmethod
    def _parse_tool_calls(message: Any) -> list[dict[str, str]]:
        raw_tool_calls = getattr(message, "tool_calls", None)
        if raw_tool_calls is None and isinstance(message, dict):
            raw_tool_calls = message.get("tool_calls")
        if not raw_tool_calls:
            return []

        parsed: list[dict[str, str]] = []
        for call in raw_tool_calls:
            if isinstance(call, dict):
                call_id = str(call.get("id", "")).strip()
                fn = call.get("function", {}) or {}
                name = str(fn.get("name", "")).strip()
                raw_args = fn.get("arguments", "")
            else:
                call_id = str(getattr(call, "id", "") or "").strip()
                fn = getattr(call, "function", None)
                name = str(getattr(fn, "name", "") if fn else "").strip()
                raw_args = getattr(fn, "arguments", "") if fn else ""

            if isinstance(raw_args, dict):
                arguments = json.dumps(raw_args, ensure_ascii=False)
            else:
                arguments = str(raw_args or "").strip()

            if call_id and name:
                parsed.append({"id": call_id, "name": name, "arguments": arguments or "{}"})
        return parsed

    @staticmethod
    def _parse_tool_arguments(raw: str) -> dict[str, Any]:
        text = (raw or "").strip() or "{}"
        try:
            data = json.loads(text)
        except Exception:
            return {}
        return data if isinstance(data, dict) else {}

    async def _run_tool(
        self,
        *,
        name: str,
        arguments: dict[str, Any],
        context: SkillContext,
        skills: dict[str, Skill] | None = None,
    ) -> SkillRunResult:
        skill_map = skills or self.skills
        skill = skill_map.get(name)
        if not skill:
            return SkillRunResult(ok=False, skill=name, summary="未知技能", error="unknown_skill")

        may_have_side_effect = self._tool_may_have_side_effect(name, arguments)

        # A timed-out coroutine may ignore cancellation. Give it a private
        # context so a late completion cannot race the next model round by
        # overwriting ``context.session`` or falsely marking media as delivered.
        tool_context = replace(
            context,
            history=list(context.history),
            default_sticker_file_ids=list(context.default_sticker_file_ids),
        )

        async def _invoke() -> SkillRunResult:
            async with _SKILL_PRIORITY_GATE.slot(timeout=self.tool_timeout_seconds):
                async with _SKILL_EXECUTION_SEMAPHORE:
                    current_task = asyncio.current_task()
                    if current_task is not None and current_task.cancelling():
                        raise asyncio.CancelledError
                    if tool_context.session is not None or tool_context.session_factory is None:
                        return await skill.run(arguments, tool_context)

                    # A reply batch must not retain one database connection throughout
                    # all model rounds.  Give a DB-backed tool a short-lived session
                    # only for the duration of that tool call.
                    async with tool_context.session_factory() as tool_session:
                        tool_context.session = tool_session
                        try:
                            result = await skill.run(arguments, tool_context)
                            current_task = asyncio.current_task()
                            if current_task is not None and current_task.cancelling():
                                raise asyncio.CancelledError
                            if result.ok:
                                await tool_session.commit()
                            else:
                                await tool_session.rollback()
                            return result
                        except BaseException:
                            await tool_session.rollback()
                            raise
                        finally:
                            tool_context.session = None

        task = asyncio.create_task(_invoke(), name=f"skill:{name}")
        try:
            done, _ = await asyncio.wait({task}, timeout=self.tool_timeout_seconds)
            if task not in done:
                task.cancel()
                _track_skill_orphan(task)
                log.warning(
                    "skill timed out | name=%s timeout=%.1fs",
                    name,
                    self.tool_timeout_seconds,
                )
                if may_have_side_effect:
                    return self._ambiguous_side_effect_result(name)
                return SkillRunResult(
                    ok=False,
                    skill=name,
                    summary="技能执行超时，请稍后重试。",
                    error="tool_timeout",
                )
            result = task.result()
            for attribute in (
                "handled",
                "sticker_sent",
                "sticker_file_id",
                "tts_sent",
                "tts_text",
                "embedded_reply_sent",
                "embedded_reply_text",
                "suppress_followup_text",
            ):
                setattr(context, attribute, getattr(tool_context, attribute))
            context.delivery_confirmed = bool(
                context.delivery_confirmed or tool_context.delivery_confirmed
            )
            return result
        except asyncio.CancelledError:
            task.cancel()
            _track_skill_orphan(task)
            raise
        except Exception:
            log.exception("skill execution failed | name=%s", name)
            if may_have_side_effect:
                return self._ambiguous_side_effect_result(name)
            return SkillRunResult(
                ok=False,
                skill=name,
                summary="技能执行失败，请稍后重试。",
                error="tool_failed",
            )

    @staticmethod
    def _tool_may_have_side_effect(name: str, arguments: dict[str, Any]) -> bool:
        if name in {
            "send_sticker",
            "doubao_tts",
            "vote_ban",
            "memory_manage",
            "rule_manage",
        }:
            return True
        if name == "music_search":
            action = clean_text(
                str(arguments.get("action") or "search"),
                max_len=24,
            ).lower()
            return action == "send_audio"
        return False

    @staticmethod
    def _ambiguous_side_effect_result(name: str) -> SkillRunResult:
        return SkillRunResult(
            ok=False,
            skill=name,
            summary=(
                "操作可能已经完成，但系统未能确认最终结果。为避免重复发送或重复写入，"
                "本轮不会自动重试；请先检查群内状态，确认未执行后再发起新请求。"
            ),
            error=_AMBIGUOUS_SIDE_EFFECT_ERROR,
        )

    @staticmethod
    def _tool_result_to_payload(result: SkillRunResult) -> dict[str, Any]:
        return {
            "ok": result.ok,
            "skill": result.skill,
            "summary": result.summary,
            "error": result.error,
            "payload": result.payload,
        }

    @staticmethod
    def _committed_state_mutation(result: SkillRunResult) -> bool:
        if not result.ok:
            return False
        allowed_actions = _STATE_MUTATING_TOOL_ACTIONS.get(result.skill)
        if not allowed_actions:
            return False
        action = clean_text(str(result.payload.get("action") or ""), max_len=24).lower()
        return action in allowed_actions

    @staticmethod
    def _latest_successful_tool_result(
        tool_results: list[dict[str, Any]],
        *,
        allowed_skills: set[str] | frozenset[str] | None = None,
    ) -> dict[str, Any] | None:
        for entry in reversed(tool_results):
            result = entry.get("result")
            if not isinstance(result, SkillRunResult) or not result.ok:
                continue
            if allowed_skills and result.skill not in allowed_skills:
                continue
            return entry
        return None

    @classmethod
    def _is_intermediate_tool_reply(
        cls,
        content: str,
        *,
        recent_tool_results: list[dict[str, Any]],
        last_success_summary: str,
    ) -> bool:
        latest = cls._latest_successful_tool_result(
            recent_tool_results,
            allowed_skills=_INFO_FOLLOWUP_SKILLS,
        )
        if not latest:
            return False

        normalized = clean_multiline_text(content, max_len=240).strip()
        if not normalized:
            return True

        if last_success_summary:
            normalized_summary = clean_multiline_text(last_success_summary, max_len=240).strip()
            if normalized_summary and normalized == normalized_summary:
                return True

        result = latest["result"]
        pattern = _INTERMEDIATE_TOOL_REPLY_PATTERNS.get(result.skill)
        return bool(pattern and pattern.fullmatch(normalized))

    @classmethod
    def _build_tool_followup_prompt(cls, recent_tool_results: list[dict[str, Any]]) -> str:
        latest = cls._latest_successful_tool_result(
            recent_tool_results,
            allowed_skills=_INFO_FOLLOWUP_SKILLS,
        )
        if not latest:
            return ""

        result = latest["result"]
        if result.skill == "websearch":
            return (
                "[TOOL_FOLLOWUP]\n"
                "You already have websearch results.\n"
                "Do not stop at only saying how many results were found.\n"
                "Read the titles, snippets, and URLs, then continue the task.\n"
                "If the current search results are enough, answer the user directly in Chinese.\n"
                "If you need more confidence or detail, call webfetch on the most relevant URL first, then answer.\n"
                "Never use the raw intermediate summary like '找到5条搜索结果' as the final reply."
            )
        if result.skill == "webfetch":
            return (
                "[TOOL_FOLLOWUP]\n"
                "You already have fetched page content.\n"
                "Do not stop at an intermediate status like '网页抓取成功'.\n"
                "Read the fetched content and answer the user's actual request in Chinese now."
            )
        if result.skill == "music_search":
            return (
                "[TOOL_FOLLOWUP]\n"
                "You already have music search results.\n"
                "Do not stop at only reporting the number of matches.\n"
                "Use the returned tracks to answer the user in Chinese now."
            )
        if result.skill == "sub2api_query":
            return (
                "[TOOL_FOLLOWUP]\n"
                "You already have Sub2API gateway query results (models list or liveness check).\n"
                "Do not stop at an intermediate status like only reporting the count or raw summary.\n"
                "Read the payload fields (models[].id, alive, latency_ms, error_detail) "
                "and answer the user's actual request in Chinese now.\n"
                "When listing models, show the model ids so the user can pick one to test."
            )
        if result.skill == "movie_info":
            return (
                "[TOOL_FOLLOWUP]\n"
                "You already have current movie information from the configured providers.\n"
                "Do not stop at the raw tool summary. Read entry/results and answer the user's actual request in Chinese now.\n"
                "Read ratings.tmdb and ratings.imdb independently. Clearly label every score as TMDB or IMDb; "
                "never merge them or present one provider's score as the other's.\n"
                "Read provider_errors and mention a partial provider failure when it affects the answer.\n"
                "For region-specific release questions, read entry.regional_release instead of inferring from the global date.\n"
                "Use fetched_at as the data freshness timestamp and do not imply the data is newer than it.\n"
                "When TMDB data is used, preserve the short source attribution from payload.attribution.\n"
                "When IMDb data is used, preserve payload.imdb_disclaimer when it is present.\n"
                "Do not call websearch again when the movie_info payload already answers the request."
            )
        if result.skill in {"bilibili_search", "weibo_search"}:
            return (
                "[TOOL_FOLLOWUP]\n"
                "You already have platform-specific search or content results.\n"
                "Do not stop at an intermediate status like only reporting the count.\n"
                "Read the returned titles, snippets, entry fields, and URLs, then answer the user's actual request in Chinese now.\n"
                "If the user wants the original link, share link, profile link, source, or address, return the existing url-like fields directly.\n"
                "Do not call websearch again when the platform skill payload already contains enough relevant links.\n"
                "If the user shared a link, summarize the content directly instead of repeating that the tool succeeded."
            )
        return ""

    @staticmethod
    def _render_result_list_fallback(
        payload: dict[str, Any],
        *,
        lead: str = "搜索结果",
    ) -> str:
        rows = payload.get("results")
        if not isinstance(rows, list):
            return ""

        items: list[str] = []
        for idx, row in enumerate(rows[:3], start=1):
            if not isinstance(row, dict):
                continue
            title = clean_multiline_text(str(row.get("title") or ""), max_len=120).strip()
            snippet = clean_multiline_text(str(row.get("snippet") or ""), max_len=140).strip()
            url = clean_text(str(row.get("url") or ""), max_len=220).strip()

            heading = html.escape(title or url or "未命名结果")
            block = f"<b>{idx}.</b> {heading}"
            if snippet:
                block = f"{block}\n{html.escape(snippet)}"
            if url:
                escaped_url = html.escape(url, quote=True)
                try:
                    parsed_url = urlparse(url)
                    linkable = (
                        parsed_url.scheme.lower() in {"http", "https"}
                        and bool(parsed_url.netloc)
                    )
                except ValueError:
                    linkable = False
                if linkable:
                    block = f'{block}\n<a href="{escaped_url}">{html.escape(url)}</a>'
                else:
                    block = f"{block}\n<code>{html.escape(url)}</code>"
            items.append(block)

        if not items:
            return ""

        metadata: dict[str, str] = {
            "结果": f"<code>{len(items)}</code> 条",
        }
        query = clean_multiline_text(str(payload.get("query") or ""), max_len=120).strip()
        if query:
            metadata = {
                "关键词": f"<code>{html.escape(query)}</code>",
                **metadata,
            }
        return render_data_brief(
            lead.rstrip("：:").strip() or "搜索结果",
            metadata=metadata,
            items="\n\n".join(items),
        )

    @staticmethod
    def _render_entry_fallback(payload: dict[str, Any]) -> str:
        entry = payload.get("entry")
        if not isinstance(entry, dict):
            return ""

        title = clean_multiline_text(str(entry.get("title") or ""), max_len=160).strip()
        content = clean_multiline_text(
            str(entry.get("content") or entry.get("desc") or entry.get("subtitle_excerpt") or ""),
            max_len=520,
        ).strip()
        author = clean_text(str(entry.get("author") or entry.get("owner") or ""), max_len=80).strip()
        url = clean_text(str(entry.get("url") or ""), max_len=220).strip()
        share_url = clean_text(str(entry.get("share_url") or ""), max_len=220).strip()
        redirected_url = clean_text(str(entry.get("redirected_url") or ""), max_len=220).strip()
        author_url = clean_text(str(entry.get("author_url") or ""), max_len=220).strip()
        download_url = clean_text(str(entry.get("download_url") or ""), max_len=220).strip()

        lines: list[str] = []
        if title:
            lines.append(f"我先拿到这条内容：{title}")
        if author:
            lines.append(f"作者：{author}")
        if content:
            lines.append(content)

        comments = entry.get("comments")
        if isinstance(comments, list) and comments:
            rendered_comments: list[str] = []
            for item in comments[:3]:
                if not isinstance(item, dict):
                    continue
                c_author = clean_text(str(item.get("author") or ""), max_len=40).strip()
                c_content = clean_multiline_text(str(item.get("content") or ""), max_len=120).strip()
                if c_content:
                    prefix = f"{c_author}：" if c_author else ""
                    rendered_comments.append(f"{prefix}{c_content}")
            if rendered_comments:
                lines.append("热门评论：\n" + "\n".join(rendered_comments))

        if url:
            lines.append(url)
        if share_url and share_url != url:
            lines.append(f"分享链接：{share_url}")
        if redirected_url and redirected_url not in {url, share_url}:
            lines.append(f"跳转后链接：{redirected_url}")
        if author_url:
            lines.append(f"作者主页：{author_url}")
        if download_url:
            lines.append(f"相关直链：{download_url}")
        return "\n\n".join(lines).strip()

    @staticmethod
    def _render_webfetch_fallback(payload: dict[str, Any]) -> str:
        title = clean_multiline_text(str(payload.get("title") or ""), max_len=160).strip()
        content = clean_multiline_text(str(payload.get("content") or ""), max_len=520).strip()
        final_url = clean_text(
            str(payload.get("final_url") or payload.get("url") or ""),
            max_len=220,
        ).strip()

        lines: list[str] = []
        if title:
            lines.append(f"我查到的页面是：{title}")
        if content:
            lines.append(content)
        if final_url:
            lines.append(final_url)
        return "\n\n".join(lines).strip()

    @staticmethod
    def _render_movie_info_fallback(payload: dict[str, Any]) -> str:
        def rating_text(item: dict[str, Any], provider: str, label: str) -> str:
            ratings = item.get("ratings")
            if not isinstance(ratings, dict):
                return ""
            rating = ratings.get(provider)
            if not isinstance(rating, dict) or rating.get("score") is None:
                return ""
            score = clean_text(str(rating.get("score")), max_len=16).strip()
            votes = rating.get("vote_count")
            votes_text = clean_text(str(votes), max_len=24).strip() if votes is not None else ""
            suffix = f"（{votes_text} 票）" if votes_text else ""
            return f"{label}：{score}/10{suffix}"

        def item_lines(item: dict[str, Any], *, include_details: bool) -> list[str]:
            title = clean_multiline_text(str(item.get("title") or ""), max_len=160).strip()
            year = clean_text(str(item.get("year") or ""), max_len=8).strip()
            heading = title or "未命名影片"
            if year:
                heading = f"{heading} ({year})"

            lines = [heading]
            if include_details:
                original_title = clean_multiline_text(
                    str(item.get("original_title") or ""),
                    max_len=160,
                ).strip()
                if original_title and original_title != title:
                    lines.append(f"原名：{original_title}")

                facts: list[str] = []
                release_date = clean_text(str(item.get("release_date") or ""), max_len=24).strip()
                status = clean_multiline_text(str(item.get("status") or ""), max_len=40).strip()
                runtime = item.get("runtime_minutes")
                if release_date:
                    facts.append(f"上映：{release_date}")
                if status:
                    facts.append(f"状态：{status}")
                if runtime is not None:
                    runtime_text = clean_text(str(runtime), max_len=12).strip()
                    if runtime_text:
                        facts.append(f"片长：{runtime_text} 分钟")
                if facts:
                    lines.append("；".join(facts))

                regional = item.get("regional_release")
                if isinstance(regional, dict) and regional.get("date"):
                    shown_region = clean_text(
                        str(regional.get("region") or ""), max_len=8
                    ).strip()
                    shown_date = clean_text(
                        str(regional.get("date") or ""), max_len=24
                    ).strip()
                    shown_status = clean_text(
                        str(regional.get("status") or ""), max_len=24
                    ).strip()
                    shown_certification = clean_text(
                        str(regional.get("certification") or ""), max_len=32
                    ).strip()
                    regional_parts = [shown_date]
                    if shown_status:
                        regional_parts.append(shown_status)
                    if shown_certification:
                        regional_parts.append(f"分级 {shown_certification}")
                    lines.append(
                        f"{shown_region or '指定地区'}上映：" + "；".join(regional_parts)
                    )

                genres = item.get("genres")
                if isinstance(genres, list):
                    genre_text = "、".join(
                        clean_text(str(value), max_len=32).strip()
                        for value in genres[:6]
                        if clean_text(str(value), max_len=32).strip()
                    )
                    if genre_text:
                        lines.append(f"类型：{genre_text}")

                overview = clean_multiline_text(
                    str(item.get("overview") or ""),
                    max_len=520,
                ).strip()
                if overview:
                    lines.append(overview)

            rating_parts = [
                value
                for value in (
                    rating_text(item, "tmdb", "TMDB 评分"),
                    rating_text(item, "imdb", "IMDb 评分"),
                )
                if value
            ]
            if rating_parts:
                lines.append("；".join(rating_parts))

            urls = item.get("urls")
            if isinstance(urls, dict):
                for provider, label in (("tmdb", "TMDB"), ("imdb", "IMDb")):
                    url = clean_text(str(urls.get(provider) or ""), max_len=220).strip()
                    if url:
                        lines.append(f"{label}：{url}")
            return lines

        entry = payload.get("entry")
        rows = payload.get("results")
        blocks: list[str] = []
        if isinstance(entry, dict):
            blocks.append("\n".join(item_lines(entry, include_details=True)))
        elif isinstance(rows, list):
            for index, row in enumerate(rows[:3], start=1):
                if not isinstance(row, dict):
                    continue
                rendered = item_lines(row, include_details=False)
                if rendered:
                    rendered[0] = f"{index}. {rendered[0]}"
                    blocks.append("\n".join(rendered))

        if not blocks:
            return ""

        provider_errors = payload.get("provider_errors")
        if isinstance(provider_errors, dict) and provider_errors:
            errors: list[str] = []
            for provider, raw_error in provider_errors.items():
                shown_provider = clean_text(str(provider), max_len=24).strip().upper()
                shown_error = clean_multiline_text(str(raw_error), max_len=120).strip()
                if shown_provider:
                    errors.append(
                        f"{shown_provider}（{shown_error}）" if shown_error else shown_provider
                    )
            if errors:
                blocks.append("部分来源未返回：" + "；".join(errors))

        fetched_at = clean_text(str(payload.get("fetched_at") or ""), max_len=48).strip()
        if fetched_at:
            blocks.append(f"查询时间：{fetched_at}")

        attribution = payload.get("attribution")
        if isinstance(attribution, str):
            shown_attribution = clean_multiline_text(attribution, max_len=240).strip()
            if shown_attribution:
                blocks.append(shown_attribution)
        elif isinstance(attribution, list):
            shown_attribution = "；".join(
                clean_multiline_text(str(value), max_len=120).strip()
                for value in attribution
                if clean_multiline_text(str(value), max_len=120).strip()
            )
            if shown_attribution:
                blocks.append(shown_attribution)
        elif isinstance(attribution, dict):
            shown_attribution = "；".join(
                clean_multiline_text(str(value), max_len=120).strip()
                for value in attribution.values()
                if clean_multiline_text(str(value), max_len=120).strip()
            )
            if shown_attribution:
                blocks.append(shown_attribution)

        imdb_disclaimer = clean_multiline_text(
            str(payload.get("imdb_disclaimer") or ""),
            max_len=500,
        ).strip()
        if imdb_disclaimer:
            blocks.append(imdb_disclaimer)

        return "\n\n".join(blocks).strip()

    @staticmethod
    def _payload_result_rows(payload: dict[str, Any]) -> list[dict[str, Any]]:
        rows = payload.get("results")
        return rows if isinstance(rows, list) else []

    @staticmethod
    def _payload_entry(payload: dict[str, Any]) -> dict[str, Any]:
        entry = payload.get("entry")
        return entry if isinstance(entry, dict) else {}

    @staticmethod
    def _collect_payload_urls(payload: dict[str, Any]) -> list[str]:
        urls: list[str] = []
        for row in SkillService._payload_result_rows(payload):
            if not isinstance(row, dict):
                continue
            url = clean_text(str(row.get("url") or ""), max_len=220).strip()
            if url:
                urls.append(url)

        entry = SkillService._payload_entry(payload)
        for key in ("url", "share_url", "redirected_url", "author_url", "download_url"):
            value = clean_text(str(entry.get(key) or ""), max_len=220).strip()
            if value:
                urls.append(value)

        deduped: list[str] = []
        seen: set[str] = set()
        for url in urls:
            if url in seen:
                continue
            seen.add(url)
            deduped.append(url)
        return deduped

    @staticmethod
    def _platform_label(skill_name: str, payload: dict[str, Any]) -> str:
        platform = clean_text(str(payload.get("platform") or ""), max_len=40).strip()
        label_map = {
            "bilibili": "B站",
            "weibo": "微博",
        }
        if platform in label_map:
            return label_map[platform]
        fallback_map = {
            "bilibili_search": "B站",
            "weibo_search": "微博",
        }
        return fallback_map.get(skill_name, "相关")

    @classmethod
    def _render_missing_result_links(
        cls,
        *,
        skill_name: str,
        payload: dict[str, Any],
        content: str,
    ) -> str:
        label = cls._platform_label(skill_name, payload)
        lines: list[str] = []

        rows = cls._payload_result_rows(payload)
        if rows:
            for idx, row in enumerate(rows[:10], start=1):
                if not isinstance(row, dict):
                    continue
                url = clean_text(str(row.get("url") or ""), max_len=220).strip()
                if not url or url in content:
                    continue
                title = clean_multiline_text(str(row.get("title") or url), max_len=100).strip()
                lines.append(f"{idx}. {title}\n{url}")
            if lines:
                return f"{label}相关链接：\n" + "\n".join(lines)

        entry = cls._payload_entry(payload)
        if entry:
            entry_lines: list[str] = []
            direct_url = clean_text(str(entry.get("url") or ""), max_len=220).strip()
            if direct_url and direct_url not in content:
                entry_lines.append(direct_url)

            named_fields = (
                ("分享链接", "share_url"),
                ("跳转后链接", "redirected_url"),
                ("作者主页", "author_url"),
                ("相关直链", "download_url"),
            )
            for shown, key in named_fields:
                value = clean_text(str(entry.get(key) or ""), max_len=220).strip()
                if value and value not in content and value != direct_url:
                    entry_lines.append(f"{shown}：{value}")

            if entry_lines:
                return f"{label}相关链接：\n" + "\n".join(entry_lines)

        return ""

    @classmethod
    def _embed_result_urls_into_numbered_list(
        cls,
        *,
        payload: dict[str, Any],
        content: str,
    ) -> str:
        rows = cls._payload_result_rows(payload)
        if not rows:
            return content

        lines = content.splitlines()
        if not lines:
            return content

        matches: list[tuple[int, int, str]] = []
        for idx, line in enumerate(lines):
            match = _NUMBERED_LIST_ITEM_RE.match(line)
            if not match:
                continue
            try:
                number = int(match.group(2))
            except ValueError:
                continue
            if number < 1:
                continue
            matches.append((number, idx, match.group(1)))

        if not matches:
            return content

        merged_lines: list[str] = []
        cursor = 0
        changed = False
        for match_idx, (number, start_idx, indent) in enumerate(matches):
            end_idx = matches[match_idx + 1][1] if match_idx + 1 < len(matches) else len(lines)
            block = lines[start_idx:end_idx]

            merged_lines.extend(lines[cursor:start_idx])
            row_index = number - 1
            if row_index >= len(rows):
                merged_lines.extend(block)
                cursor = end_idx
                continue
            row = rows[row_index]
            if not isinstance(row, dict):
                merged_lines.extend(block)
                cursor = end_idx
                continue
            url = clean_text(str(row.get("url") or ""), max_len=220).strip()
            if not url:
                merged_lines.extend(block)
                cursor = end_idx
                continue

            block_text = "\n".join(block)
            if url in block_text or _URL_RE.search(block_text):
                merged_lines.extend(block)
                cursor = end_idx
                continue

            insert_at = len(block)
            for inner_idx, line in enumerate(block[1:], start=1):
                if not line.strip():
                    insert_at = inner_idx
                    break

            merged_lines.extend(block[:insert_at])
            merged_lines.append(f"{indent}{url}")
            merged_lines.extend(block[insert_at:])
            cursor = end_idx
            changed = True

        merged_lines.extend(lines[cursor:])
        if not changed:
            return content
        return "\n".join(merged_lines).strip()

    @classmethod
    def _append_missing_platform_links(
        cls,
        *,
        content: str,
        recent_tool_results: list[dict[str, Any]],
        user_text: str = "",
    ) -> str:
        normalized = clean_multiline_text(content, max_len=4000).strip()
        if not normalized:
            return normalized
        normalized_user_text = clean_multiline_text(user_text, max_len=400).strip()
        should_append_by_request = bool(_LINK_REQUEST_RE.search(normalized_user_text))
        should_append_by_layout = bool(_RESULT_LIST_RE.search(normalized))
        if not should_append_by_request and not should_append_by_layout:
            return normalized

        appendices: list[str] = []
        seen_urls: set[str] = set()
        for entry in recent_tool_results:
            result = entry.get("result")
            if not isinstance(result, SkillRunResult) or not result.ok or result.skill not in _PLATFORM_LINK_SKILLS:
                continue
            payload = result.payload if isinstance(result.payload, dict) else {}
            payload_urls = [url for url in cls._collect_payload_urls(payload) if url not in seen_urls]
            if not payload_urls:
                continue
            embedded = cls._embed_result_urls_into_numbered_list(
                payload=payload,
                content=normalized,
            )
            if embedded != normalized:
                normalized = embedded
                seen_urls.update(url for url in payload_urls if url in normalized)
                continue
            appendix = cls._render_missing_result_links(
                skill_name=result.skill,
                payload=payload,
                content=normalized,
            )
            if not appendix:
                continue
            appendices.append(appendix)
            seen_urls.update(payload_urls)

        if not appendices:
            return normalized
        return normalized.rstrip() + "\n" + "\n".join(appendices)

    @classmethod
    def _build_tool_fallback_text(
        cls,
        *,
        recent_tool_results: list[dict[str, Any]],
        default_text: str,
    ) -> str:
        for entry in reversed(recent_tool_results):
            result = entry.get("result")
            if (
                isinstance(result, SkillRunResult)
                and not result.ok
                and result.error in _MANDATORY_REFUSAL_ERRORS
                and result.summary
            ):
                payload = result.payload if isinstance(result.payload, dict) else {}
                telegram_text = str(payload.get("telegram_text") or "").strip()
                if telegram_text:
                    return telegram_text
                return result.summary
        latest = cls._latest_successful_tool_result(
            recent_tool_results,
            allowed_skills=frozenset(
                {
                    "webfetch",
                    "websearch",
                    "bilibili_search",
                    "weibo_search",
                    "movie_info",
                }
            ),
        )
        if not latest:
            return default_text

        result = latest["result"]
        payload = result.payload if isinstance(result.payload, dict) else {}
        if result.skill == "movie_info":
            rendered = cls._render_movie_info_fallback(payload)
            if rendered:
                return rendered
            if result.summary:
                return result.summary
        if payload.get("entry"):
            rendered = cls._render_entry_fallback(payload)
            if rendered:
                return rendered
        if payload.get("results"):
            rendered = cls._render_result_list_fallback(payload)
            if rendered:
                return rendered
        if result.skill == "webfetch":
            rendered = cls._render_webfetch_fallback(payload)
            if rendered:
                return rendered
        if result.skill == "websearch":
            rendered = cls._render_result_list_fallback(payload)
            if rendered:
                return rendered
        return default_text

    async def run_skill(
        self,
        name: str,
        arguments: dict[str, Any] | None = None,
        *,
        session: AsyncSession | None = None,
        history: list[dict[str, str]] | None = None,
        sender_user_id: int = 0,
        sender_username: str = "",
        sender_is_owner: bool = False,
        sender_is_tg_admin: bool = False,
        message: Any | None = None,
        bot: Any | None = None,
        chat_id: int = 0,
        current_user_text: str = "",
        session_factory: Any | None = None,
        is_direct_request: bool = False,
        delivery_callback: Callable[[], None] | None = None,
    ) -> SkillRunResult:
        context = SkillContext(
            session=session,
            session_factory=session_factory,
            message=message,
            bot=bot,
            chat_id=chat_id,
            llm=self.llm,
            history=list(history or []),
            sender_user_id=sender_user_id,
            sender_username=sender_username,
            sender_is_owner=sender_is_owner,
            sender_is_tg_admin=sender_is_tg_admin,
            current_user_text=current_user_text,
            is_direct_request=is_direct_request,
            default_sticker_file_ids=self.default_sticker_file_ids,
            auto_delete_media_seconds=(
                configured_auto_delete_seconds(self.settings, "media")
                if self.settings is not None
                else 0
            ),
            auto_delete_reply_seconds=(
                configured_auto_delete_seconds(self.settings, "reply")
                if self.settings is not None
                else 0
            ),
        )

        def _confirm_delivery() -> None:
            context.delivery_confirmed = True
            if delivery_callback is not None:
                delivery_callback()

        context.delivery_callback = _confirm_delivery
        return await self._run_tool(name=name, arguments=arguments or {}, context=context)

    async def answer_with_skill(
        self,
        text: str,
        *,
        session: AsyncSession | None = None,
        history: list[dict[str, str]] | None = None,
        sender_user_id: int = 0,
        sender_username: str = "",
        sender_is_owner: bool = False,
        sender_is_tg_admin: bool = False,
        message: Any | None = None,
        session_factory: Any | None = None,
        intent_type: str = "casual",
        allow_tts: bool = True,
        tts_mode: str = TTS_MODE_OFF,
        merged_count: int = 1,
        merged_context: str = "",
        reply_targets_context: str = "",
        is_mentioned: bool = False,
        is_reply_to_bot: bool = False,
        is_direct_request: bool | None = None,
        style_profile_context: str = "",
        delivery_callback: Callable[[], None] | None = None,
    ) -> SkillAnswerResult:
        user_text = self._normalize_user_text(text, merged_count=merged_count)
        if contains_prompt_injection(user_text):
            log.warning("skill input may contain prompt injection")

        selected_skills = self._selected_skills(allow_tts=allow_tts)
        messages = self._build_answer_messages(
            user_text,
            history=history,
            sender_user_id=sender_user_id,
            sender_username=sender_username,
            sender_is_owner=sender_is_owner,
            sender_is_tg_admin=sender_is_tg_admin,
            intent_type=intent_type,
            merged_count=merged_count,
            merged_context=merged_context,
            reply_targets_context=reply_targets_context,
            selected_skills=selected_skills,
            tts_mode=tts_mode,
            is_mentioned=is_mentioned,
            is_reply_to_bot=is_reply_to_bot,
            style_profile_context=style_profile_context,
        )
        tools = self._tool_definitions(selected_skills)
        context = SkillContext(
            session=session,
            session_factory=session_factory,
            message=message,
            bot=getattr(message, "bot", None) if message is not None else None,
            chat_id=int(getattr(getattr(message, "chat", None), "id", 0) or 0) if message is not None else 0,
            llm=self.llm,
            history=list(history or []),
            sender_user_id=sender_user_id,
            sender_username=sender_username,
            sender_is_owner=sender_is_owner,
            sender_is_tg_admin=sender_is_tg_admin,
            current_user_text=user_text,
            is_direct_request=(
                bool(is_mentioned or is_reply_to_bot)
                if is_direct_request is None
                else bool(is_direct_request)
            ),
            default_sticker_file_ids=self.default_sticker_file_ids,
            auto_delete_media_seconds=(
                configured_auto_delete_seconds(self.settings, "media")
                if self.settings is not None
                else 0
            ),
            auto_delete_reply_seconds=(
                configured_auto_delete_seconds(self.settings, "reply")
                if self.settings is not None
                else 0
            ),
        )

        def _confirm_delivery() -> None:
            context.delivery_confirmed = True
            if delivery_callback is not None:
                delivery_callback()

        context.delivery_callback = _confirm_delivery
        last_success_summary = ""
        last_tool_summary = ""
        recent_tool_results: list[dict[str, Any]] = []
        followup_retry_used = False
        mandatory_refusal_summary = ""
        total_tool_calls = 0
        side_effect_committed = ""
        side_effect_notice_added = False

        def _build_answer_result(text: str = "") -> SkillAnswerResult:
            return SkillAnswerResult(
                text=text,
                handled=context.handled,
                must_deliver_text=bool(mandatory_refusal_summary and text),
                sticker_sent=context.sticker_sent,
                sticker_file_id=context.sticker_file_id,
                tts_sent=context.tts_sent,
                tts_text=context.tts_text,
                delivery_confirmed=context.delivery_confirmed,
                embedded_reply_sent=context.embedded_reply_sent,
                embedded_reply_text=context.embedded_reply_text,
            )

        def _action_reply_completed() -> bool:
            return bool(
                context.sticker_sent
                or context.tts_sent
                or context.embedded_reply_sent
                or context.delivery_confirmed
            )

        for step in range(1, self.max_tool_rounds + 1):
            current_task = asyncio.current_task()
            if current_task is not None and current_task.cancelling():
                raise asyncio.CancelledError
            resp = await self._completion_with_fallbacks(messages=messages, tools=tools)
            if not resp:
                return _build_answer_result(
                    self._build_tool_fallback_text(
                        recent_tool_results=recent_tool_results,
                        default_text=last_tool_summary or last_success_summary,
                    )
                )

            msg = resp.choices[0].message
            content = self._normalize_content_text(getattr(msg, "content", "")).strip()
            tool_calls = self._parse_tool_calls(msg)

            remaining_calls = self.max_total_tool_calls - total_tool_calls
            if remaining_calls <= 0:
                log.warning(
                    "skill tool loop stopped at total call limit | limit=%d",
                    self.max_total_tool_calls,
                )
                return _build_answer_result(
                    self._build_tool_fallback_text(
                        recent_tool_results=recent_tool_results,
                        default_text=last_tool_summary or last_success_summary,
                    )
                )
            allowed_calls = min(self.max_tool_calls_per_round, remaining_calls)
            if len(tool_calls) > allowed_calls:
                log.warning(
                    "skill tool calls truncated | requested=%d allowed=%d step=%d",
                    len(tool_calls),
                    allowed_calls,
                    step,
                )
                tool_calls = tool_calls[:allowed_calls]
            total_tool_calls += len(tool_calls)

            # The model has now received the structured tool error and the
            # mandatory-refusal system block.  Its prose is advisory only:
            # return the trusted refusal rendering deterministically so it cannot
            # claim success, omit the reason, or offer a bypass.
            if mandatory_refusal_summary:
                log.info("skill tool loop enforcing deterministic quota refusal")
                return _build_answer_result(
                    self._build_tool_fallback_text(
                        recent_tool_results=recent_tool_results,
                        default_text=mandatory_refusal_summary,
                    )
                )

            assistant_message: dict[str, Any] = {"role": "assistant", "content": content}
            if tool_calls:
                assistant_message["tool_calls"] = [
                    {
                        "id": tool_call["id"],
                        "type": "function",
                        "function": {
                            "name": tool_call["name"],
                            "arguments": tool_call["arguments"],
                        },
                    }
                    for tool_call in tool_calls
                ]
            messages.append(assistant_message)

            if not tool_calls:
                if _action_reply_completed():
                    log.info("skill tool loop finished: step=%d action_already_delivered", step)
                    return _build_answer_result()
                if content and not self._is_intermediate_tool_reply(
                    content,
                    recent_tool_results=recent_tool_results,
                    last_success_summary=last_success_summary,
                ):
                    log.info("skill tool loop finished: step=%d no_tool_call", step)
                    return _build_answer_result(
                        self._append_missing_platform_links(
                            content=content,
                            recent_tool_results=recent_tool_results,
                            user_text=user_text,
                        )
                    )
                if (
                    not followup_retry_used
                    and step < self.max_tool_rounds
                    and self._is_intermediate_tool_reply(
                        content,
                        recent_tool_results=recent_tool_results,
                        last_success_summary=last_success_summary,
                    )
                ):
                    followup_prompt = self._build_tool_followup_prompt(recent_tool_results)
                    if followup_prompt:
                        followup_retry_used = True
                        messages.append({"role": "system", "content": followup_prompt})
                        log.info(
                            "skill tool loop continuing after intermediate tool reply: step=%d",
                            step,
                        )
                        continue
                return _build_answer_result(
                    self._build_tool_fallback_text(
                        recent_tool_results=recent_tool_results,
                        default_text=content or last_tool_summary or last_success_summary,
                    )
                )

            log.info("skill tool loop: step=%d tool_calls=%d", step, len(tool_calls))
            for tool_index, tool_call in enumerate(tool_calls):
                current_task = asyncio.current_task()
                if current_task is not None and current_task.cancelling():
                    raise asyncio.CancelledError
                args = self._parse_tool_arguments(tool_call["arguments"])
                if mandatory_refusal_summary:
                    result = SkillRunResult(
                        ok=False,
                        skill=tool_call["name"],
                        summary=mandatory_refusal_summary,
                        error="skipped_due_to_mandatory_refusal",
                    )
                elif side_effect_committed:
                    result = SkillRunResult(
                        ok=False,
                        skill=tool_call["name"],
                        summary=(
                            f"已完成有副作用的工具 {side_effect_committed}；"
                            "为避免重复操作，本轮其余工具调用已跳过。"
                        ),
                        error="skipped_after_side_effect",
                    )
                else:
                    result = await self._run_tool(
                        name=tool_call["name"],
                        arguments=args,
                        context=context,
                        skills=selected_skills,
                    )
                if result.ok and result.summary:
                    last_success_summary = result.summary
                if result.summary and result.error != "skipped_after_side_effect":
                    last_tool_summary = result.summary
                recent_tool_results.append(
                    {
                        "name": tool_call["name"],
                        "arguments": dict(args),
                        "result": result,
                    }
                )
                recent_tool_results = recent_tool_results[-8:]
                payload = self._tool_result_to_payload(result)
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": tool_call["id"],
                        "name": tool_call["name"],
                        "content": json.dumps(payload, ensure_ascii=False),
                    }
                )
                if result.error in _MANDATORY_REFUSAL_ERRORS:
                    mandatory_refusal_summary = result.summary or (
                        "本统计周期内的民主投票发起额度已用完，请稍后再试。"
                    )
                    messages.append(
                        {
                            "role": "system",
                            "content": (
                                "[MANDATORY_TOOL_REFUSAL]\n"
                                f"error: {result.error}\n"
                                f"reason_to_user: {result.summary}\n"
                                "You must refuse this action using reason_to_user. "
                                "Do not retry the tool in this turn, do not suggest /voteban "
                                "or another path to bypass the quota, and do not claim the vote started."
                            ),
                        }
                    )

                # A delivered action (sticker, voice, media, vote prompt, ...)
                # is the terminal reply for this turn.  Do not execute any
                # remaining tool calls emitted in the same model response: they
                # may be duplicate non-idempotent actions and the model is not a
                # trusted transaction coordinator for Telegram side effects.
                if _action_reply_completed():
                    skipped = len(tool_calls) - tool_index - 1
                    log.info(
                        "skill tool loop finished after delivered action | "
                        "step=%d skill=%s skipped_calls=%d",
                        step,
                        tool_call["name"],
                        max(0, skipped),
                    )
                    return _build_answer_result()
                if result.error == _AMBIGUOUS_SIDE_EFFECT_ERROR:
                    log.warning(
                        "skill tool loop stopped at ambiguous side effect | skill=%s",
                        result.skill,
                    )
                    return _build_answer_result(result.summary)
                if self._committed_state_mutation(result):
                    side_effect_committed = result.skill

            if mandatory_refusal_summary:
                if step < self.max_tool_rounds:
                    continue
                return _build_answer_result(
                    self._build_tool_fallback_text(
                        recent_tool_results=recent_tool_results,
                        default_text=mandatory_refusal_summary,
                    )
                )
            if side_effect_committed and not side_effect_notice_added:
                side_effect_notice_added = True
                messages.append(
                    {
                        "role": "system",
                        "content": (
                            "[SIDE_EFFECT_COMMITTED]\n"
                            f"tool: {side_effect_committed}\n"
                            "Exactly one state-changing tool has succeeded in this turn. "
                            "Do not call any more tools. Briefly report the confirmed result "
                            "without claiming any skipped action succeeded."
                        ),
                    }
                )
            if _action_reply_completed():
                log.info("skill tool loop finished: step=%d action_handled_without_followup", step)
                return _build_answer_result()

        log.info("skill tool loop reached max steps: %d", self.max_tool_rounds)
        if _action_reply_completed():
            return _build_answer_result()
        return _build_answer_result(
            self._build_tool_fallback_text(
                recent_tool_results=recent_tool_results,
                default_text=last_tool_summary or last_success_summary,
            )
        )
