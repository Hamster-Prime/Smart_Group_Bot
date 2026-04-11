from __future__ import annotations

import json
import logging
import re
from typing import Any
from urllib.parse import urlparse

from bot.services.skills.base import SkillContext, SkillRunResult
from bot.services.skills.platform_common import (
    extract_first_url,
    fetch_text,
    parse_html_summary,
    site_search,
)
from bot.utils.security import clean_multiline_text, clean_text

log = logging.getLogger(__name__)

_ROUTER_DATA_RE = re.compile(r"window\._ROUTER_DATA\s*=\s*(.*?)</script>", re.DOTALL)
_DOUYIN_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (iPhone; CPU iPhone OS 17_2 like Mac OS X) "
        "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1"
    ),
    "Referer": "https://www.douyin.com/",
}


class DouyinSearchSkill:
    name = "douyin_search"
    description = (
        "抖音内容检索与读取：解析分享链接、搜索公开视频并抓取公开内容，也可直接返回分享链接、重定向链接和相关视频地址。"
        "当前只保留搜索和拿内容，不做登录下载、发帖或其他 Cookie 写操作。"
    )
    parameters_schema = {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["parse_share", "search_videos", "fetch_url"],
                "description": "要执行的动作",
            },
            "share_text": {"type": "string", "description": "抖音分享文本或链接"},
            "keyword": {"type": "string", "description": "搜索词"},
            "url": {"type": "string", "description": "抖音链接"},
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
    def _sanitize_title(title: str, video_id: str) -> str:
        cleaned = re.sub(r'[\\/:*?"<>|]', "_", str(title or ""))
        cleaned = clean_text(cleaned, max_len=80)
        return cleaned or f"douyin_{video_id}"

    @staticmethod
    def _parse_video_id_from_final_url(final_url: str) -> str:
        parts = [part for part in urlparse(final_url).path.split("/") if part]
        if not parts:
            raise ValueError("douyin_final_url_invalid")
        last = parts[-1]
        if last in {"video", "note"} and len(parts) >= 2:
            last = parts[-2]
        return clean_text(last, max_len=40)

    @staticmethod
    def _extract_router_data_json(raw_html: str) -> dict[str, Any]:
        match = _ROUTER_DATA_RE.search(raw_html or "")
        if not match or not match.group(1):
            raise ValueError("douyin_router_data_missing")
        raw = (match.group(1) or "").strip().rstrip(";")
        return json.loads(raw)

    @staticmethod
    def _pick_video_info(router_data: dict[str, Any]) -> dict[str, Any]:
        loader_data = router_data.get("loaderData") or {}
        if not isinstance(loader_data, dict):
            raise ValueError("douyin_loader_data_missing")
        for key in ("video_(id)/page", "note_(id)/page"):
            candidate = loader_data.get(key) or {}
            if isinstance(candidate, dict) and candidate.get("videoInfoRes"):
                return candidate["videoInfoRes"]
        for candidate in loader_data.values():
            if isinstance(candidate, dict) and candidate.get("videoInfoRes"):
                return candidate["videoInfoRes"]
        raise ValueError("douyin_video_info_missing")

    async def _parse_share(self, share_text: str) -> SkillRunResult:
        share_url = extract_first_url(share_text) or clean_text(share_text, max_len=260)
        if not share_url:
            return SkillRunResult(ok=False, skill=self.name, summary="没有识别到抖音分享链接", error="missing_share_url")

        status, _, redirected_url, _ = await fetch_text(share_url, headers=_DOUYIN_HEADERS, timeout_sec=25.0)
        if status >= 400:
            return SkillRunResult(ok=False, skill=self.name, summary=f"访问抖音分享链接失败: HTTP {status}", error=f"http_{status}")

        video_id = self._parse_video_id_from_final_url(redirected_url)
        page_url = f"https://www.iesdouyin.com/share/video/{video_id}"
        status, raw_html, final_url, _ = await fetch_text(page_url, headers=_DOUYIN_HEADERS, timeout_sec=25.0)
        if status >= 400:
            return SkillRunResult(ok=False, skill=self.name, summary=f"访问抖音分享页失败: HTTP {status}", error=f"http_{status}")

        router_data = self._extract_router_data_json(raw_html)
        video_info = self._pick_video_info(router_data)
        item_list = video_info.get("item_list") or []
        if not item_list or not isinstance(item_list[0], dict):
            return SkillRunResult(ok=False, skill=self.name, summary="抖音分享页缺少视频数据", error="empty_item_list")
        item = item_list[0]
        play_addr = item.get("video", {}).get("play_addr", {})
        url_list = play_addr.get("url_list") or []
        raw_play_url = clean_text(str(url_list[0] if url_list else ""), max_len=500)
        desc = clean_multiline_text(str(item.get("desc") or ""), max_len=220)
        entry = {
            "title": self._sanitize_title(desc, video_id),
            "url": final_url,
            "share_url": share_url,
            "redirected_url": redirected_url,
            "video_id": video_id,
            "content": desc or "抖音视频",
            "download_url": raw_play_url.replace("playwm", "play") if raw_play_url else "",
        }
        return SkillRunResult(
            ok=True,
            skill=self.name,
            summary=f"已解析抖音分享视频 {video_id}",
            payload={"platform": "douyin", "action": "parse_share", "entry": entry},
        )

    async def _search_videos(self, keyword: str, *, max_results: int) -> SkillRunResult:
        results, effective_query = await site_search(
            keyword,
            query_candidates=[
                f"{keyword} 抖音 site:douyin.com/video",
                f"{keyword} douyin site:douyin.com/video",
                f"{keyword} site:iesdouyin.com/share/video",
            ],
            max_results=max_results,
            allowed_hosts=("douyin.com", "iesdouyin.com", "v.douyin.com"),
        )
        if not results:
            return SkillRunResult(ok=False, skill=self.name, summary="没有找到相关抖音视频", error="empty_result")
        return SkillRunResult(
            ok=True,
            skill=self.name,
            summary=f"找到 {len(results)} 条抖音视频结果",
            payload={
                "platform": "douyin",
                "action": "search_videos",
                "keyword": keyword,
                "effective_query": effective_query,
                "results": results,
            },
        )

    async def _fetch_url(self, url: str) -> SkillRunResult:
        if "douyin.com" in url or "iesdouyin.com" in url:
            return await self._parse_share(url)
        status, raw_html, final_url, _ = await fetch_text(url, headers=_DOUYIN_HEADERS, timeout_sec=18.0)
        if status >= 400:
            return SkillRunResult(ok=False, skill=self.name, summary=f"抖音链接抓取失败: HTTP {status}", error=f"http_{status}")
        summary = parse_html_summary(raw_html, max_content_len=1200)
        entry = {
            "title": summary["title"] or "抖音页面",
            "url": final_url,
            "author": summary["author"],
            "content": summary["description"] or summary["content"],
        }
        if not clean_multiline_text(str(entry["content"]), max_len=200):
            return SkillRunResult(ok=False, skill=self.name, summary="抖音页面内容为空", error="empty_content")
        return SkillRunResult(
            ok=True,
            skill=self.name,
            summary="已抓取抖音链接内容",
            payload={"platform": "douyin", "action": "fetch_url", "entry": entry},
        )

    async def run(self, arguments: dict[str, Any], context: SkillContext) -> SkillRunResult:
        _ = context
        action = clean_text(str(arguments.get("action") or ""), max_len=32).lower()
        share_text = clean_multiline_text(str(arguments.get("share_text") or ""), max_len=500)
        keyword = clean_text(str(arguments.get("keyword") or ""), max_len=120)
        url = clean_text(str(arguments.get("url") or ""), max_len=260)
        max_results = self._safe_count(arguments.get("max_results", 5), default=5, upper=10)

        try:
            if action == "parse_share":
                if not share_text:
                    return SkillRunResult(ok=False, skill=self.name, summary="抖音分享文本为空", error="empty_share_text")
                return await self._parse_share(share_text)
            if action == "search_videos":
                if not keyword:
                    return SkillRunResult(ok=False, skill=self.name, summary="抖音搜索词为空", error="empty_keyword")
                return await self._search_videos(keyword, max_results=max_results)
            if action == "fetch_url":
                if not url:
                    return SkillRunResult(ok=False, skill=self.name, summary="缺少抖音链接", error="missing_url")
                return await self._fetch_url(url)
        except ModuleNotFoundError:
            return SkillRunResult(ok=False, skill=self.name, summary="未安装 ddgs 依赖，无法搜索抖音", error="ddgs_not_installed")
        except Exception as exc:
            log.exception("douyin_search failed: action=%s", action)
            return SkillRunResult(
                ok=False,
                skill=self.name,
                summary=f"抖音技能执行失败：{clean_text(str(exc), max_len=120)}",
                error=clean_text(str(exc), max_len=120) or "runtime_error",
            )

        return SkillRunResult(ok=False, skill=self.name, summary="未知抖音动作", error="unknown_action")
