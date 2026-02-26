from __future__ import annotations

import json
import logging
import re
from typing import Any

from bot.services.llm import LLMService
from bot.services.skills.base import SkillRunResult
from bot.services.skills.webfetch import WebFetchSkill
from bot.services.skills.websearch import WebSearchSkill
from bot.utils.prompts import SKILL_ANSWER_SYSTEM, SKILL_SYSTEM
from bot.utils.security import (
    build_defended_system,
    clean_text,
    contains_prompt_injection,
    sanitize_history_for_llm,
    wrap_untrusted,
)

log = logging.getLogger(__name__)

_URL_RE = re.compile(r"https?://[^\s]+", re.IGNORECASE)


def _parse_json_payload(raw: str) -> dict[str, Any] | None:
    payload = (raw or "").strip()
    if payload.startswith("```"):
        payload = re.sub(r"^```(?:json)?", "", payload).strip()
        payload = re.sub(r"```$", "", payload).strip()
    if not payload.startswith("{"):
        m = re.search(r"\{[\s\S]*\}", payload)
        if m:
            payload = m.group(0)
    try:
        data = json.loads(payload)
    except Exception:
        return None
    return data if isinstance(data, dict) else None


class SkillService:
    def __init__(self, llm: LLMService) -> None:
        self.llm = llm
        self.websearch = WebSearchSkill()
        self.webfetch = WebFetchSkill()

    @staticmethod
    def _extract_first_url(text: str) -> str:
        m = _URL_RE.search(text or "")
        return m.group(0) if m else ""

    @staticmethod
    def _heuristic_plan(text: str) -> dict[str, Any]:
        t = clean_text(text, max_len=800)
        url = SkillService._extract_first_url(t)
        if url and any(k in t.lower() for k in ("总结", "看看", "网页", "链接", "内容", "解析", "fetch")):
            return {"use_skill": True, "skill": "webfetch", "url": url, "query": "", "reason": "用户请求解析链接"}
        if url:
            return {"use_skill": True, "skill": "webfetch", "url": url, "query": "", "reason": "检测到URL"}
        if any(k in t.lower() for k in ("搜索", "查一下", "最新", "新闻", "官网", "资料", "websearch", "ddgs")):
            return {"use_skill": True, "skill": "websearch", "url": "", "query": t, "reason": "用户请求联网搜索"}
        return {"use_skill": False, "skill": "none", "url": "", "query": "", "reason": "无需技能"}

    async def _plan(self, text: str) -> dict[str, Any]:
        user_text = clean_text(text, max_len=1200)
        has_url = bool(self._extract_first_url(user_text))

        if contains_prompt_injection(user_text):
            log.warning("技能规划检测到疑似提示词注入，启用保守规划")
            if has_url:
                return {"use_skill": True, "skill": "webfetch", "url": self._extract_first_url(user_text), "query": "", "reason": "注入风险下只执行显式URL抓取"}
            return {"use_skill": False, "skill": "none", "url": "", "query": "", "reason": "注入风险，拒绝隐式技能调用"}

        planner_input = (
            "[用户消息]\n"
            f"{wrap_untrusted('用户消息', user_text, max_len=1200)}\n\n"
            "[可用技能]\n"
            "- websearch: 基于DDGS做网页搜索，适合“查资料/最新信息/官网链接”\n"
            "- webfetch: 抓取指定URL正文，适合“看这个链接内容/总结网页”\n\n"
            "[输出要求]\n"
            "严格输出JSON: {\"use_skill\":true|false,\"skill\":\"websearch|webfetch|none\",\"query\":\"\",\"url\":\"\",\"reason\":\"\"}"
        )
        raw = await self.llm.decision(build_defended_system(SKILL_SYSTEM), planner_input)
        data = _parse_json_payload(raw)
        if not data:
            return self._heuristic_plan(user_text)

        use_skill = bool(data.get("use_skill", False))
        skill = str(data.get("skill", "none")).strip().lower()
        query = clean_text(str(data.get("query", "")), max_len=300)
        url = str(data.get("url", "")).strip()
        reason = clean_text(str(data.get("reason", "")), max_len=120)

        if not use_skill or skill not in {"websearch", "webfetch"}:
            return {"use_skill": False, "skill": "none", "url": "", "query": "", "reason": reason or "规划为不使用技能"}

        if skill == "webfetch" and not url:
            url = self._extract_first_url(user_text)
        if skill == "websearch" and not query:
            query = user_text

        if skill == "webfetch" and not url:
            return self._heuristic_plan(user_text)
        if skill == "websearch" and not query:
            return self._heuristic_plan(user_text)

        return {"use_skill": True, "skill": skill, "url": url, "query": query, "reason": reason or "模型规划"}

    async def _run_plan(self, plan: dict[str, Any]) -> SkillRunResult:
        skill = plan.get("skill")
        if skill == "websearch":
            return await self.websearch.run(str(plan.get("query", "")))
        if skill == "webfetch":
            return await self.webfetch.run(str(plan.get("url", "")))
        return SkillRunResult(ok=False, skill="none", summary="未执行技能", error="none")

    async def answer_with_skill(
        self,
        text: str,
        *,
        history: list[dict[str, str]] | None = None,
    ) -> str:
        plan = await self._plan(text)
        if not plan.get("use_skill"):
            return ""

        result = await self._run_plan(plan)
        if not result.ok:
            log.warning("技能执行失败: skill=%s error=%s", result.skill, result.error)
            return ""

        payload_text = json.dumps(result.payload, ensure_ascii=False)
        payload_text = clean_text(payload_text, max_len=6000)

        messages: list[dict[str, str]] = [{"role": "system", "content": build_defended_system(SKILL_ANSWER_SYSTEM)}]
        if history:
            messages.extend(sanitize_history_for_llm(history, max_items=len(history)))
        messages.append(
            {
                "role": "user",
                "content": (
                    f"[用户问题]\n{wrap_untrusted('用户问题', text, max_len=800)}\n"
                    f"[技能名称]\n{result.skill}\n"
                    f"[技能执行摘要]\n{result.summary}\n"
                    f"[技能输出]\n{wrap_untrusted('技能输出', payload_text, max_len=6000)}"
                ),
            }
        )
        reply = (await self.llm.chat(messages)).strip()
        if reply:
            log.info("技能回复生成成功: skill=%s", result.skill)
            return reply
        return result.summary
