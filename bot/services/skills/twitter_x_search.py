from __future__ import annotations

import logging
import re
from typing import Any

from bot.services.skills.base import SkillContext, SkillRunResult
from bot.services.skills.platform_common import (
    fetch_json,
    fetch_text,
    parse_html_summary,
    site_search,
    strip_html,
)
from bot.utils.security import clean_multiline_text, clean_text

log = logging.getLogger(__name__)

_STATUS_RE = re.compile(r"(?:x\.com|twitter\.com)/[^/\s]+/status/(\d+)", re.IGNORECASE)
_TWITTER_HEADERS = {
    "Referer": "https://x.com/",
    "Accept": "application/json, text/plain, */*",
}


class TwitterXSearchSkill:
    name = "twitter_x_search"
    description = (
        "Twitter/X 内容检索与读取：按平台定向搜索推文或账号，抓取单条链接内容，并可直接返回推文原链接或主页链接。"
        "当前只做搜索和公开内容读取，不做 Cookie 登录、发推、点赞等写操作。"
    )
    parameters_schema = {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["search_posts", "search_profiles", "fetch_url"],
                "description": "要执行的动作",
            },
            "keyword": {"type": "string", "description": "搜索词"},
            "url": {"type": "string", "description": "推文或个人主页链接"},
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
    def _extract_status_id(url: str) -> str:
        match = _STATUS_RE.search(url or "")
        return clean_text(match.group(1), max_len=40) if match else ""

    @staticmethod
    def _is_status_url(url: str) -> bool:
        return bool(TwitterXSearchSkill._extract_status_id(url))

    @staticmethod
    def _is_profile_url(url: str) -> bool:
        normalized = clean_text(url, max_len=300).lower()
        if "x.com/" not in normalized and "twitter.com/" not in normalized:
            return False
        if "/status/" in normalized:
            return False
        return normalized.count("/") >= 3 and not normalized.rstrip("/").endswith(("/home", "/explore", "/search"))

    @staticmethod
    def _filter_results(results: list[dict[str, str]], *, mode: str) -> list[dict[str, str]]:
        filtered: list[dict[str, str]] = []
        for row in results:
            url = clean_text(str(row.get("url") or ""), max_len=300)
            if mode == "posts" and not TwitterXSearchSkill._is_status_url(url):
                continue
            if mode == "profiles" and not TwitterXSearchSkill._is_profile_url(url):
                continue
            filtered.append(row)
        return filtered

    async def _search(self, keyword: str, *, max_results: int, mode: str) -> SkillRunResult:
        queries = [
            f"{keyword} site:x.com",
            f"{keyword} site:twitter.com",
            f"\"{keyword}\" site:x.com",
        ]
        results, effective_query = await site_search(
            keyword,
            query_candidates=queries,
            max_results=max_results * 2,
            allowed_hosts=("x.com", "twitter.com"),
        )
        filtered = self._filter_results(results, mode=mode)
        if not filtered:
            filtered = results
        filtered = filtered[:max_results]
        if not filtered:
            label = "推文" if mode == "posts" else "账号"
            return SkillRunResult(ok=False, skill=self.name, summary=f"没有找到相关 X/Twitter {label}", error="empty_result")
        label = "推文" if mode == "posts" else "账号"
        return SkillRunResult(
            ok=True,
            skill=self.name,
            summary=f"找到 {len(filtered)} 条 X/Twitter {label}结果",
            payload={
                "platform": "twitter_x",
                "action": f"search_{mode}",
                "keyword": keyword,
                "effective_query": effective_query,
                "results": filtered,
            },
        )

    async def _fetch_tweet_oembed(self, tweet_id: str) -> dict[str, str]:
        canonical_url = f"https://twitter.com/i/web/status/{tweet_id}"
        status, payload, _ = await fetch_json(
            "https://publish.twitter.com/oembed",
            params={"url": canonical_url, "omit_script": "true", "hide_thread": "false"},
            headers=_TWITTER_HEADERS,
            timeout_sec=18.0,
        )
        if status >= 400 or not isinstance(payload, dict):
            raise ValueError(f"http_{status}")
        html_block = clean_multiline_text(strip_html(str(payload.get("html") or ""), max_len=1200), max_len=1200)
        author_name = clean_text(str(payload.get("author_name") or ""), max_len=80)
        author_url = clean_text(str(payload.get("author_url") or ""), max_len=220)
        content = html_block
        if author_name and content.startswith(author_name):
            content = content[len(author_name) :].strip(" :\n")
        return {
            "title": f"@{author_name} 的推文" if author_name else "X/Twitter 推文",
            "url": canonical_url,
            "author": author_name,
            "author_url": author_url,
            "content": content or "推文内容提取为空",
        }

    async def _fetch_url(self, url: str) -> SkillRunResult:
        status_id = self._extract_status_id(url)
        entry: dict[str, str]
        if status_id:
            try:
                entry = await self._fetch_tweet_oembed(status_id)
            except Exception as exc:
                log.warning("twitter oembed failed, fallback to html: url=%s error=%s", url, exc)
                status, raw_html, final_url, _ = await fetch_text(url, headers=_TWITTER_HEADERS, timeout_sec=18.0)
                if status >= 400:
                    return SkillRunResult(ok=False, skill=self.name, summary=f"X/Twitter 链接抓取失败: HTTP {status}", error=f"http_{status}")
                summary = parse_html_summary(raw_html, max_content_len=1200)
                entry = {
                    "title": summary["title"] or "X/Twitter 页面",
                    "url": final_url,
                    "author": summary["author"],
                    "content": summary["description"] or summary["content"],
                }
        else:
            status, raw_html, final_url, _ = await fetch_text(url, headers=_TWITTER_HEADERS, timeout_sec=18.0)
            if status >= 400:
                return SkillRunResult(ok=False, skill=self.name, summary=f"X/Twitter 链接抓取失败: HTTP {status}", error=f"http_{status}")
            summary = parse_html_summary(raw_html, max_content_len=1200)
            entry = {
                "title": summary["title"] or "X/Twitter 页面",
                "url": final_url,
                "author": summary["author"],
                "content": summary["description"] or summary["content"],
            }

        if not clean_multiline_text(str(entry.get("content") or ""), max_len=200):
            return SkillRunResult(ok=False, skill=self.name, summary="X/Twitter 页面内容为空", error="empty_content")
        return SkillRunResult(
            ok=True,
            skill=self.name,
            summary="已抓取 X/Twitter 链接内容",
            payload={"platform": "twitter_x", "action": "fetch_url", "entry": entry},
        )

    async def run(self, arguments: dict[str, Any], context: SkillContext) -> SkillRunResult:
        _ = context
        action = clean_text(str(arguments.get("action") or ""), max_len=32).lower()
        keyword = clean_text(str(arguments.get("keyword") or ""), max_len=120)
        url = clean_text(str(arguments.get("url") or ""), max_len=260)
        max_results = self._safe_count(arguments.get("max_results", 5), default=5, upper=10)

        try:
            if action == "search_posts":
                if not keyword:
                    return SkillRunResult(ok=False, skill=self.name, summary="X/Twitter 搜索词为空", error="empty_keyword")
                return await self._search(keyword, max_results=max_results, mode="posts")
            if action == "search_profiles":
                if not keyword:
                    return SkillRunResult(ok=False, skill=self.name, summary="X/Twitter 账号搜索词为空", error="empty_keyword")
                return await self._search(keyword, max_results=max_results, mode="profiles")
            if action == "fetch_url":
                if not url:
                    return SkillRunResult(ok=False, skill=self.name, summary="缺少 X/Twitter 链接", error="missing_url")
                return await self._fetch_url(url)
        except ModuleNotFoundError:
            return SkillRunResult(ok=False, skill=self.name, summary="未安装 ddgs 依赖，无法搜索 X/Twitter", error="ddgs_not_installed")
        except Exception as exc:
            log.exception("twitter_x_search failed: action=%s", action)
            return SkillRunResult(
                ok=False,
                skill=self.name,
                summary=f"X/Twitter 技能执行失败：{clean_text(str(exc), max_len=120)}",
                error=clean_text(str(exc), max_len=120) or "runtime_error",
            )

        return SkillRunResult(ok=False, skill=self.name, summary="未知 X/Twitter 动作", error="unknown_action")
