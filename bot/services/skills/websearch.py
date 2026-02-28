from __future__ import annotations

import asyncio
import logging

from bot.services.skills.base import SkillContext, SkillRunResult
from bot.utils.security import clean_text

log = logging.getLogger(__name__)


class WebSearchSkill:
    name = "websearch"
    description = "联网搜索公开网页信息，返回标题、链接和摘要。"
    parameters_schema = {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "搜索关键词"},
            "max_results": {"type": "integer", "description": "返回数量(1-10)", "default": 5},
        },
        "required": ["query"],
        "additionalProperties": False,
    }

    async def run(self, arguments: dict, context: SkillContext) -> SkillRunResult:
        _ = context  # Unused for this skill.
        q = clean_text(str(arguments.get("query", "")), max_len=300)
        try:
            max_results = int(arguments.get("max_results", 5))
        except Exception:
            max_results = 5
        max_results = max(1, min(max_results, 10))

        if not q:
            return SkillRunResult(ok=False, skill=self.name, summary="搜索词为空", error="empty_query")

        def _search() -> list[dict]:
            from ddgs import DDGS

            with DDGS() as ddgs:
                return list(ddgs.text(q, max_results=max_results))

        try:
            rows = await asyncio.to_thread(_search)
        except ModuleNotFoundError:
            log.exception("websearch failed: ddgs not installed")
            return SkillRunResult(ok=False, skill=self.name, summary="未安装 ddgs 依赖", error="ddgs_not_installed")
        except Exception as e:
            log.exception("websearch failed")
            return SkillRunResult(ok=False, skill=self.name, summary="网页搜索失败", error=str(e))

        results: list[dict] = []
        for row in rows[:max_results]:
            title = clean_text(str(row.get("title", "")), max_len=200)
            href = str(row.get("href") or row.get("url") or "").strip()
            body = clean_text(str(row.get("body", "")), max_len=300)
            if href:
                results.append({"title": title, "url": href, "snippet": body})

        if not results:
            return SkillRunResult(ok=False, skill=self.name, summary="没有找到搜索结果", error="empty_result")

        return SkillRunResult(
            ok=True,
            skill=self.name,
            summary=f"找到 {len(results)} 条搜索结果",
            payload={"query": q, "results": results},
        )
