from __future__ import annotations

import logging
from typing import Any

from bot.services.skills.base import SkillContext, SkillRunResult
from bot.services.skills.platform_common import (
    InvalidJsonResponseError,
    ResponseTooLargeError,
    UnsupportedContentTypeError,
    fetch_json,
    fetch_text,
    host_matches,
    parse_html_summary,
    site_search,
    strip_html,
)
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
_WEIBO_ALLOWED_HOSTS = ("weibo.com", "weibo.cn")


class WeiboSearchSkill:
    name = "weibo_search"
    description = "微博内容搜索与读取：热搜、热门微博搜索、热门 Feed、原帖链接和链接内容提取。"
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

    @staticmethod
    def _build_post_url(*, uid: str, bid: str, mid: str) -> str:
        if uid and bid:
            return f"https://weibo.com/{uid}/{bid}"
        if mid:
            return f"https://m.weibo.cn/detail/{mid}"
        return ""

    @staticmethod
    def _is_weibo_post_url(url: str) -> bool:
        normalized = clean_text(url, max_len=320).lower()
        if not host_matches(normalized, _WEIBO_ALLOWED_HOSTS):
            return False
        if any(token in normalized for token in ("/status/", "/detail/", "/tv/show/")):
            return True
        host_and_path = normalized.split("://", 1)[-1].split("/", 1)
        if len(host_and_path) < 2:
            return False
        return host_and_path[1].count("/") >= 1

    @staticmethod
    def _filter_post_results(results: list[dict[str, str]], *, max_results: int) -> list[dict[str, str]]:
        filtered: list[dict[str, str]] = []
        for row in results:
            url = clean_text(str(row.get("url") or ""), max_len=320)
            if not url or not WeiboSearchSkill._is_weibo_post_url(url):
                continue
            filtered.append(
                {
                    "title": clean_multiline_text(str(row.get("title") or url), max_len=120),
                    "url": url,
                    "snippet": clean_multiline_text(str(row.get("snippet") or ""), max_len=220),
                }
            )
            if len(filtered) >= max_results:
                break
        return filtered

    async def _fallback_web_results(
        self,
        *,
        action: str,
        query: str,
        query_candidates: list[str],
        max_results: int,
        summary_found: str,
        summary_empty: str,
        filter_posts: bool = False,
    ) -> SkillRunResult:
        results, effective_query = await site_search(
            query,
            query_candidates=query_candidates,
            max_results=max_results * 2,
            allowed_hosts=("weibo.com", "m.weibo.cn", "s.weibo.com"),
        )
        filtered = (
            self._filter_post_results(results, max_results=max_results)
            if filter_posts
            else results[:max_results]
        )
        if not filtered and filter_posts:
            filtered = results[:max_results]
        if not filtered:
            return SkillRunResult(ok=False, skill=self.name, summary=summary_empty, error="empty_result")
        return SkillRunResult(
            ok=True,
            skill=self.name,
            summary=summary_found.format(count=len(filtered)),
            payload={
                "platform": "weibo",
                "action": action,
                "effective_query": effective_query,
                "results": filtered,
                "fallback": "web_search",
                **({"keyword": query} if query else {}),
            },
        )

    async def _search_posts_via_web(self, keyword: str, *, max_results: int) -> SkillRunResult:
        return await self._fallback_web_results(
            action="search_posts",
            query=keyword,
            query_candidates=[
                f"{keyword} site:weibo.com",
                f"{keyword} site:m.weibo.cn",
                f"\"{keyword}\" site:weibo.com",
            ],
            max_results=max_results,
            summary_found="找到 {count} 条微博结果",
            summary_empty="没有找到相关微博",
            filter_posts=True,
        )

    async def _hot_search_via_web(self, *, max_results: int) -> SkillRunResult:
        return await self._fallback_web_results(
            action="hot_search",
            query="微博热搜",
            query_candidates=[
                "微博热搜 site:s.weibo.com",
                "微博热搜 site:weibo.com",
                "微博热门话题 site:weibo.com",
            ],
            max_results=max_results,
            summary_found="找到 {count} 条微博热搜相关结果",
            summary_empty="没有找到微博热搜相关结果",
        )

    async def _hot_feed_via_web(self, *, max_results: int) -> SkillRunResult:
        return await self._fallback_web_results(
            action="get_hot_feed",
            query="微博热门",
            query_candidates=[
                "微博热门 site:weibo.com",
                "微博热门动态 site:weibo.com",
                "微博热门博文 site:weibo.com",
            ],
            max_results=max_results,
            summary_found="找到 {count} 条微博热门内容结果",
            summary_empty="没有找到微博热门内容",
            filter_posts=True,
        )

    async def _hot_search(self, *, max_results: int) -> SkillRunResult:
        try:
            status, payload, _ = await fetch_json(
                "https://weibo.com/ajax/side/hotSearch",
                headers=_WEIBO_HEADERS,
                timeout_sec=18.0,
                allowed_hosts=_WEIBO_ALLOWED_HOSTS,
            )
        except (
            InvalidJsonResponseError,
            UnsupportedContentTypeError,
            ResponseTooLargeError,
        ) as exc:
            log.warning("weibo hot_search api returned non-json, fallback to web search: %s", exc)
            return await self._hot_search_via_web(max_results=max_results)

        if status >= 400:
            log.warning("weibo hot_search http error=%s, fallback to web search", status)
            return await self._hot_search_via_web(max_results=max_results)

        data = self._unwrap_data(payload)
        rows = data.get("realtime") if isinstance(data, dict) else []
        results: list[dict[str, str]] = []
        for row in (rows or [])[:max_results]:
            if not isinstance(row, dict):
                continue
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
            return await self._hot_search_via_web(max_results=max_results)
        return SkillRunResult(
            ok=True,
            skill=self.name,
            summary=f"拿到 {len(results)} 条微博热搜",
            payload={"platform": "weibo", "action": "hot_search", "results": results},
        )

    async def _search_posts(self, keyword: str, *, page: int, max_results: int) -> SkillRunResult:
        try:
            status, payload, _ = await fetch_json(
                "https://m.weibo.cn/api/container/getIndex",
                params={
                    "containerid": f"100103type=1&q={keyword}",
                    "page_type": "searchall",
                    "page": page,
                },
                headers=_WEIBO_MOBILE_HEADERS,
                timeout_sec=18.0,
                allowed_hosts=_WEIBO_ALLOWED_HOSTS,
            )
        except (
            InvalidJsonResponseError,
            UnsupportedContentTypeError,
            ResponseTooLargeError,
        ) as exc:
            log.warning("weibo search_posts api returned non-json, fallback to web search: %s", exc)
            return await self._search_posts_via_web(keyword, max_results=max_results)

        if status >= 400:
            log.warning("weibo search_posts http error=%s, fallback to web search", status)
            return await self._search_posts_via_web(keyword, max_results=max_results)

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
            url = self._build_post_url(uid=uid, bid=bid, mid=mid)
            title = text[:60] or (f"@{author} 的微博" if author else "微博结果")
            snippet = "；".join([item for item in [f"作者 @{author}" if author else "", text] if item])
            results.append(
                {
                    "title": title,
                    "url": url,
                    "snippet": snippet,
                }
            )

        if not results:
            return await self._search_posts_via_web(keyword, max_results=max_results)
        return SkillRunResult(
            ok=True,
            skill=self.name,
            summary=f"找到 {len(results)} 条微博结果",
            payload={"platform": "weibo", "action": "search_posts", "keyword": keyword, "results": results},
        )

    async def _get_hot_feed(self, *, max_results: int) -> SkillRunResult:
        try:
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
                allowed_hosts=_WEIBO_ALLOWED_HOSTS,
            )
        except (
            InvalidJsonResponseError,
            UnsupportedContentTypeError,
            ResponseTooLargeError,
        ) as exc:
            log.warning("weibo hot_feed api returned non-json, fallback to web search: %s", exc)
            return await self._hot_feed_via_web(max_results=max_results)

        if status >= 400:
            log.warning("weibo hot_feed http error=%s, fallback to web search", status)
            return await self._hot_feed_via_web(max_results=max_results)

        data = payload.get("data") if isinstance(payload, dict) else {}
        rows = data.get("statuses") if isinstance(data, dict) else []
        results: list[dict[str, str]] = []
        for row in (rows or [])[:max_results]:
            if not isinstance(row, dict):
                continue
            user = row.get("user") or {}
            text = strip_html(str(row.get("text_raw") or row.get("text") or ""), max_len=220)
            mid = clean_text(str(row.get("id") or ""), max_len=40)
            bid = clean_text(str(row.get("bid") or ""), max_len=40)
            uid = clean_text(str(user.get("id") or ""), max_len=40)
            author = clean_text(str(user.get("screen_name") or ""), max_len=60)
            url = self._build_post_url(uid=uid, bid=bid, mid=mid)
            title = text[:60] or (f"@{author} 的微博" if author else "微博热门内容")
            snippet = "；".join([item for item in [f"作者 @{author}" if author else "", text] if item])
            results.append(
                {
                    "title": title,
                    "url": url,
                    "snippet": snippet,
                }
            )

        if not results:
            return await self._hot_feed_via_web(max_results=max_results)
        return SkillRunResult(
            ok=True,
            skill=self.name,
            summary=f"拿到 {len(results)} 条微博热门 Feed",
            payload={"platform": "weibo", "action": "get_hot_feed", "results": results},
        )

    async def _fetch_url(self, url: str) -> SkillRunResult:
        status, raw_html, final_url, _ = await fetch_text(
            url,
            headers=_WEIBO_HEADERS,
            timeout_sec=18.0,
            allowed_hosts=_WEIBO_ALLOWED_HOSTS,
            max_response_bytes=1024 * 1024,
            max_decoded_bytes=2 * 1024 * 1024,
        )
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
        except ModuleNotFoundError:
            return SkillRunResult(ok=False, skill=self.name, summary="未安装 ddgs 依赖，无法进行微博网页兜底搜索", error="ddgs_not_installed")
        except Exception as exc:
            log.exception("weibo_search failed: action=%s", action)
            return SkillRunResult(
                ok=False,
                skill=self.name,
                summary=f"微博技能执行失败：{clean_text(str(exc), max_len=120)}",
                error=clean_text(str(exc), max_len=120) or "runtime_error",
            )

        return SkillRunResult(ok=False, skill=self.name, summary="未知微博动作", error="unknown_action")
