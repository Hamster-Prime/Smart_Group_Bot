from __future__ import annotations

import logging
from typing import Any

from bot.services.skills.base import SkillContext, SkillRunResult
from bot.services.skills.platform_common import fetch_text, parse_html_summary, site_search
from bot.utils.security import clean_multiline_text, clean_text

log = logging.getLogger(__name__)

_XHS_HEADERS = {
    "Referer": "https://www.xiaohongshu.com/",
    "Accept": "text/html,application/xhtml+xml,application/json,text/plain;q=0.9,*/*;q=0.8",
}


class XiaohongshuSearchSkill:
    name = "xiaohongshu_search"
    description = (
        "小红书内容检索与读取：定向搜索笔记/账号，抓取公开链接内容，并可直接返回笔记或主页链接。"
        "当前只做搜索和内容读取，不做 Cookie 登录、点赞、评论或收藏。"
    )
    parameters_schema = {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["search_notes", "search_profiles", "fetch_url"],
                "description": "要执行的动作",
            },
            "keyword": {"type": "string", "description": "搜索词"},
            "url": {"type": "string", "description": "小红书链接"},
            "max_results": {"type": "integer", "description": "返回数量 1-10", "default": 5},
        },
        "required": ["action"],
        "additionalProperties": False,
    }

    @staticmethod
    def _safe_count(value: Any, *, default: int = 5, upper: int = 10) -> int:
        try:
            count = int(value)
        except Exception:
            count = default
        return max(1, min(upper, count))

    @staticmethod
    def _is_note_url(url: str) -> bool:
        lowered = clean_text(url, max_len=300).lower()
        return any(token in lowered for token in ("/explore/", "/discovery/item/", "/note/"))

    @staticmethod
    def _is_profile_url(url: str) -> bool:
        lowered = clean_text(url, max_len=300).lower()
        return any(token in lowered for token in ("/user/profile/", "/user/"))

    @staticmethod
    def _filter_results(results: list[dict[str, str]], *, mode: str) -> list[dict[str, str]]:
        filtered: list[dict[str, str]] = []
        for row in results:
            url = clean_text(str(row.get("url") or ""), max_len=300)
            if mode == "notes" and not XiaohongshuSearchSkill._is_note_url(url):
                continue
            if mode == "profiles" and not XiaohongshuSearchSkill._is_profile_url(url):
                continue
            filtered.append(row)
        return filtered

    async def _search(self, keyword: str, *, max_results: int, mode: str) -> SkillRunResult:
        suffix = "笔记" if mode == "notes" else "博主"
        results, effective_query = await site_search(
            keyword,
            query_candidates=[
                f"{keyword} 小红书 site:xiaohongshu.com",
                f"{keyword} xiaohongshu site:xiaohongshu.com",
                f"\"{keyword}\" site:xiaohongshu.com",
            ],
            max_results=max_results * 2,
            allowed_hosts=("xiaohongshu.com", "xhslink.com"),
        )
        filtered = self._filter_results(results, mode=mode)
        if not filtered:
            filtered = results
        filtered = filtered[:max_results]
        if not filtered:
            return SkillRunResult(ok=False, skill=self.name, summary=f"没有找到相关小红书{suffix}", error="empty_result")
        return SkillRunResult(
            ok=True,
            skill=self.name,
            summary=f"找到 {len(filtered)} 条小红书{suffix}结果",
            payload={
                "platform": "xiaohongshu",
                "action": f"search_{mode}",
                "keyword": keyword,
                "effective_query": effective_query,
                "results": filtered,
            },
        )

    async def _fetch_url(self, url: str) -> SkillRunResult:
        status, raw_html, final_url, _ = await fetch_text(url, headers=_XHS_HEADERS, timeout_sec=18.0)
        if status >= 400:
            return SkillRunResult(ok=False, skill=self.name, summary=f"小红书链接抓取失败: HTTP {status}", error=f"http_{status}")
        summary = parse_html_summary(raw_html, max_content_len=1200)
        entry = {
            "title": summary["title"] or "小红书页面",
            "url": final_url,
            "author": summary["author"],
            "content": summary["description"] or summary["content"],
        }
        if not clean_multiline_text(str(entry["content"]), max_len=200):
            return SkillRunResult(ok=False, skill=self.name, summary="小红书页面内容为空", error="empty_content")
        return SkillRunResult(
            ok=True,
            skill=self.name,
            summary="已抓取小红书链接内容",
            payload={"platform": "xiaohongshu", "action": "fetch_url", "entry": entry},
        )

    async def run(self, arguments: dict[str, Any], context: SkillContext) -> SkillRunResult:
        _ = context
        action = clean_text(str(arguments.get("action") or ""), max_len=32).lower()
        keyword = clean_text(str(arguments.get("keyword") or ""), max_len=120)
        url = clean_text(str(arguments.get("url") or ""), max_len=260)
        max_results = self._safe_count(arguments.get("max_results", 5), default=5, upper=10)

        try:
            if action == "search_notes":
                if not keyword:
                    return SkillRunResult(ok=False, skill=self.name, summary="小红书笔记搜索词为空", error="empty_keyword")
                return await self._search(keyword, max_results=max_results, mode="notes")
            if action == "search_profiles":
                if not keyword:
                    return SkillRunResult(ok=False, skill=self.name, summary="小红书账号搜索词为空", error="empty_keyword")
                return await self._search(keyword, max_results=max_results, mode="profiles")
            if action == "fetch_url":
                if not url:
                    return SkillRunResult(ok=False, skill=self.name, summary="缺少小红书链接", error="missing_url")
                return await self._fetch_url(url)
        except ModuleNotFoundError:
            return SkillRunResult(ok=False, skill=self.name, summary="未安装 ddgs 依赖，无法搜索小红书", error="ddgs_not_installed")
        except Exception as exc:
            log.exception("xiaohongshu_search failed: action=%s", action)
            return SkillRunResult(
                ok=False,
                skill=self.name,
                summary=f"小红书技能执行失败：{clean_text(str(exc), max_len=120)}",
                error=clean_text(str(exc), max_len=120) or "runtime_error",
            )

        return SkillRunResult(ok=False, skill=self.name, summary="未知小红书动作", error="unknown_action")
