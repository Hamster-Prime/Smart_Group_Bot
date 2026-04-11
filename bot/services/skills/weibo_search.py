from __future__ import annotations

import logging
from typing import Any

from bot.services.skills.base import SkillContext, SkillRunResult
from bot.services.skills.platform_common import fetch_json, fetch_text, parse_html_summary, strip_html
from bot.utils.security import clean_multiline_text, clean_text

log = logging.getLogger(__name__)

_WEIBO_HEADERS = {
    "Referer": "https://weibo.com/",
    "Accept": "application/json, text/plain, */*",
}
_WEIBO_MOBILE_HEADERS = {
    "Referer": "https://m.weibo.cn/",
    "Accept": "application/json, text/plain, */*",
    "X-Requested-With": "XMLHttpRequest",
}


class WeiboSearchSkill:
    name = "weibo_search"
    description = (
        "微博内容检索与读取：热搜、热门微博搜索、热门 Feed、原帖链接和链接内容提取。"
    )
    parameters_schema = {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["hot_search", "search_posts", "get_hot_feed", "fetch_url"],
                "description": "要执行的动作",
            },
            "keyword": {"type": "string", "description": "微博搜索关键词"},
            "url": {"type": "string", "description": "微博链接"},
            "page": {"type": "integer", "description": "页码，默认 1", "default": 1},
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
    def _unwrap_data(payload: Any) -> dict[str, Any]:
        if not isinstance(payload, dict):
            return {}
        data = payload.get("data")
        return data if isinstance(data, dict) else payload

    async def _hot_search(self, *, max_results: int) -> SkillRunResult:
        status, payload, _ = await fetch_json(
            "https://weibo.com/ajax/side/hotSearch",
            headers=_WEIBO_HEADERS,
            timeout_sec=18.0,
        )
        if status >= 400:
            return SkillRunResult(ok=False, skill=self.name, summary=f"微博热搜请求失败: HTTP {status}", error=f"http_{status}")
        data = self._unwrap_data(payload)
        rows = data.get("realtime") if isinstance(data, dict) else []
        results: list[dict[str, str]] = []
        for row in (rows or [])[:max_results]:
            word = clean_text(str(row.get("word") or row.get("note") or ""), max_len=120)
            if not word:
                continue
            hot = clean_text(str(row.get("num") or row.get("raw_hot") or ""), max_len=40)
            label = clean_text(str(row.get("label_name") or row.get("icon_desc") or ""), max_len=40)
            snippet = "；".join([item for item in [label, f"热度 {hot}" if hot else ""] if item])
            results.append(
                {
                    "title": word,
                    "url": f"https://s.weibo.com/weibo?q={word}",
                    "snippet": snippet,
                }
            )
        if not results:
            return SkillRunResult(ok=False, skill=self.name, summary="没有拿到微博热搜结果", error="empty_result")
        return SkillRunResult(
            ok=True,
            skill=self.name,
            summary=f"拿到 {len(results)} 条微博热搜",
            payload={"platform": "weibo", "action": "hot_search", "results": results},
        )

    async def _search_posts(self, keyword: str, *, page: int, max_results: int) -> SkillRunResult:
        status, payload, _ = await fetch_json(
            "https://m.weibo.cn/api/container/getIndex",
            params={
                "containerid": f"100103type=1&q={keyword}",
                "page_type": "searchall",
                "page": page,
            },
            headers=_WEIBO_MOBILE_HEADERS,
            timeout_sec=18.0,
        )
        if status >= 400:
            return SkillRunResult(ok=False, skill=self.name, summary=f"微博搜索失败: HTTP {status}", error=f"http_{status}")
        data = self._unwrap_data(payload)
        cards = data.get("cards") if isinstance(data, dict) else []
        results: list[dict[str, str]] = []
        for card in cards or []:
            if len(results) >= max_results or not isinstance(card, dict) or card.get("card_type") != 9:
                continue
            mblog = card.get("mblog") or {}
            user = mblog.get("user") or {}
            text = strip_html(str(mblog.get("text") or ""), max_len=220)
            mid = clean_text(str(mblog.get("id") or ""), max_len=40)
            bid = clean_text(str(mblog.get("bid") or ""), max_len=40)
            uid = clean_text(str(user.get("id") or ""), max_len=40)
            author = clean_text(str(user.get("screen_name") or ""), max_len=60)
            url = ""
            if uid and bid:
                url = f"https://weibo.com/{uid}/{bid}"
            elif mid:
                url = f"https://m.weibo.cn/detail/{mid}"
            results.append(
                {
                    "title": text[:60] or f"@{author} 的微博",
                    "url": url,
                    "snippet": "；".join([item for item in [f"作者 @{author}" if author else "", text] if item]),
                }
            )
        if not results:
            return SkillRunResult(ok=False, skill=self.name, summary="没有找到相关微博", error="empty_result")
        return SkillRunResult(
            ok=True,
            skill=self.name,
            summary=f"找到 {len(results)} 条微博结果",
            payload={"platform": "weibo", "action": "search_posts", "keyword": keyword, "results": results},
        )

    async def _get_hot_feed(self, *, max_results: int) -> SkillRunResult:
        status, payload, _ = await fetch_json(
            "https://weibo.com/ajax/feed/hottimeline",
            params={
                "since_id": "0",
                "refresh": "0",
                "group_id": "102803",
                "containerid": "102803",
                "extparam": "discover|new_feed",
                "max_id": "0",
                "count": max_results,
            },
            headers=_WEIBO_HEADERS,
            timeout_sec=18.0,
        )
        if status >= 400:
            return SkillRunResult(ok=False, skill=self.name, summary=f"微博热门 Feed 失败: HTTP {status}", error=f"http_{status}")
        data = payload.get("data") if isinstance(payload, dict) else {}
        rows = data.get("statuses") if isinstance(data, dict) else []
        results: list[dict[str, str]] = []
        for row in (rows or [])[:max_results]:
            user = row.get("user") or {}
            text = strip_html(str(row.get("text_raw") or row.get("text") or ""), max_len=220)
            mid = clean_text(str(row.get("id") or ""), max_len=40)
            bid = clean_text(str(row.get("bid") or ""), max_len=40)
            uid = clean_text(str(user.get("id") or ""), max_len=40)
            author = clean_text(str(user.get("screen_name") or ""), max_len=60)
            url = ""
            if uid and bid:
                url = f"https://weibo.com/{uid}/{bid}"
            elif mid:
                url = f"https://m.weibo.cn/detail/{mid}"
            results.append(
                {
                    "title": text[:60] or f"@{author} 的微博",
                    "url": url,
                    "snippet": "；".join([item for item in [f"作者 @{author}" if author else "", text] if item]),
                }
            )
        if not results:
            return SkillRunResult(ok=False, skill=self.name, summary="没有拿到微博热门 Feed", error="empty_result")
        return SkillRunResult(
            ok=True,
            skill=self.name,
            summary=f"拿到 {len(results)} 条微博热门 Feed",
            payload={"platform": "weibo", "action": "get_hot_feed", "results": results},
        )

    async def _fetch_url(self, url: str) -> SkillRunResult:
        status, raw_html, final_url, _ = await fetch_text(url, headers=_WEIBO_HEADERS, timeout_sec=18.0)
        if status >= 400:
            return SkillRunResult(ok=False, skill=self.name, summary=f"微博链接抓取失败: HTTP {status}", error=f"http_{status}")
        summary = parse_html_summary(raw_html, max_content_len=1200)
        entry = {
            "title": summary["title"] or "微博页面",
            "url": final_url,
            "author": summary["author"],
            "content": summary["description"] or summary["content"],
        }
        if not clean_multiline_text(str(entry["content"]), max_len=200):
            return SkillRunResult(ok=False, skill=self.name, summary="微博页面内容为空", error="empty_content")
        return SkillRunResult(
            ok=True,
            skill=self.name,
            summary="已抓取微博链接内容",
            payload={"platform": "weibo", "action": "fetch_url", "entry": entry},
        )

    async def run(self, arguments: dict[str, Any], context: SkillContext) -> SkillRunResult:
        _ = context
        action = clean_text(str(arguments.get("action") or ""), max_len=32).lower()
        keyword = clean_text(str(arguments.get("keyword") or ""), max_len=120)
        url = clean_text(str(arguments.get("url") or ""), max_len=260)
        page = self._safe_count(arguments.get("page", 1), default=1, upper=50)
        max_results = self._safe_count(arguments.get("max_results", 5), default=5, upper=10)

        try:
            if action == "hot_search":
                return await self._hot_search(max_results=max_results)
            if action == "search_posts":
                if not keyword:
                    return SkillRunResult(ok=False, skill=self.name, summary="微博搜索词为空", error="empty_keyword")
                return await self._search_posts(keyword, page=page, max_results=max_results)
            if action == "get_hot_feed":
                return await self._get_hot_feed(max_results=max_results)
            if action == "fetch_url":
                if not url:
                    return SkillRunResult(ok=False, skill=self.name, summary="缺少微博链接", error="missing_url")
                return await self._fetch_url(url)
        except Exception as exc:
            log.exception("weibo_search failed: action=%s", action)
            return SkillRunResult(
                ok=False,
                skill=self.name,
                summary=f"微博技能执行失败：{clean_text(str(exc), max_len=120)}",
                error=clean_text(str(exc), max_len=120) or "runtime_error",
            )

        return SkillRunResult(ok=False, skill=self.name, summary="未知微博动作", error="unknown_action")
