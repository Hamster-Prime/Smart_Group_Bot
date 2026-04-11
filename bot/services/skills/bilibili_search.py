from __future__ import annotations

import html
import logging
import re
from datetime import datetime
from typing import Any

from bot.services.skills.base import SkillContext, SkillRunResult
from bot.services.skills.platform_common import fetch_json, site_search, strip_html
from bot.utils.security import clean_multiline_text, clean_text

log = logging.getLogger(__name__)

_BVID_RE = re.compile(r"(BV[0-9A-Za-z]{10})", re.IGNORECASE)
_BILI_HEADERS = {
    "Referer": "https://www.bilibili.com/",
    "Origin": "https://www.bilibili.com",
    "Accept": "application/json, text/plain, */*",
}


class BilibiliSearchSkill:
    name = "bilibili_search"
    description = (
        "B站内容检索与读取：搜索视频和UP主、获取视频详情/字幕摘录/热门/排行榜，"
        "并可直接返回视频或UP主页链接。"
    )
    parameters_schema = {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": [
                    "search_videos",
                    "search_users",
                    "get_video",
                    "get_hot",
                    "get_rank",
                ],
                "description": "要执行的动作",
            },
            "keyword": {"type": "string", "description": "搜索关键词"},
            "bvid_or_url": {"type": "string", "description": "BV号或 B站视频链接"},
            "page": {"type": "integer", "description": "页码，默认 1", "default": 1},
            "max_results": {"type": "integer", "description": "返回数量 1-10", "default": 5},
            "include_subtitle": {"type": "boolean", "description": "是否尝试抓取字幕摘录", "default": True},
            "include_comments": {"type": "boolean", "description": "是否尝试抓取热门评论", "default": False},
        },
        "required": ["action"],
        "additionalProperties": False,
    }

    api_base = "https://api.bilibili.com"

    @staticmethod
    def _normalize_bvid(token: str) -> str:
        text = clean_text(str(token or ""), max_len=24).strip()
        if len(text) < 2:
            return text
        if text[:2].lower() == "bv":
            return f"BV{text[2:]}"
        return text

    @staticmethod
    def _extract_bvid(text: str) -> str:
        match = _BVID_RE.search(text or "")
        return BilibiliSearchSkill._normalize_bvid(match.group(1)) if match else ""

    @staticmethod
    def _safe_count(value: Any, *, default: int = 5, upper: int = 10) -> int:
        try:
            count = int(value)
        except Exception:
            count = default
        return max(1, min(upper, count))

    @staticmethod
    def _wants_comments_from_text(text: str) -> bool:
        normalized = clean_multiline_text(text, max_len=400)
        return any(token in normalized for token in ("评论", "热评", "回复", "评论区"))

    @staticmethod
    def _format_pubdate(value: Any) -> str:
        try:
            ts = int(value)
        except Exception:
            return ""
        return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")

    @staticmethod
    def _compose_video_url(bvid: str) -> str:
        return f"https://www.bilibili.com/video/{bvid}"

    async def _api_get(self, path: str, *, params: dict[str, Any] | None = None) -> Any:
        status, payload, _ = await fetch_json(
            f"{self.api_base}{path}",
            params=params,
            headers=_BILI_HEADERS,
            timeout_sec=18.0,
        )
        if status >= 400:
            raise ValueError(f"http_{status}")
        if not isinstance(payload, dict):
            raise ValueError("invalid_payload")
        code = int(payload.get("code", 0) or 0)
        if code != 0:
            raise ValueError(clean_text(str(payload.get("message") or payload.get("msg") or code), max_len=120))
        return payload.get("data")

    @staticmethod
    def _normalize_video_results(rows: list[dict[str, Any]], *, max_results: int) -> list[dict[str, str]]:
        results: list[dict[str, str]] = []
        for row in rows[:max_results]:
            bvid = BilibiliSearchSkill._normalize_bvid(str(row.get("bvid") or ""))
            title = strip_html(str(row.get("title") or row.get("typename") or bvid), max_len=140)
            author = clean_text(str(row.get("author") or ""), max_len=60)
            duration = clean_text(str(row.get("duration") or ""), max_len=32)
            desc = strip_html(str(row.get("description") or row.get("desc") or ""), max_len=180)
            snippet = "；".join([item for item in [author, duration, desc] if item])
            results.append(
                {
                    "title": title or bvid or "未命名视频",
                    "url": BilibiliSearchSkill._compose_video_url(bvid) if bvid else "",
                    "snippet": snippet,
                    "bvid": bvid,
                }
            )
        return [item for item in results if item.get("url")]

    @staticmethod
    def _normalize_user_results(rows: list[dict[str, Any]], *, max_results: int) -> list[dict[str, str]]:
        results: list[dict[str, str]] = []
        for row in rows[:max_results]:
            mid = clean_text(str(row.get("mid") or row.get("uid") or ""), max_len=24)
            uname = strip_html(str(row.get("uname") or row.get("name") or mid), max_len=120)
            fans = clean_text(str(row.get("fans") or row.get("fans_count") or ""), max_len=40)
            videos = clean_text(str(row.get("videos") or row.get("video") or ""), max_len=32)
            desc = strip_html(
                str(row.get("usign") or row.get("upic") or row.get("desc") or ""),
                max_len=160,
            )
            snippet = "；".join([item for item in [f"粉丝 {fans}" if fans else "", f"视频 {videos}" if videos else "", desc] if item])
            results.append(
                {
                    "title": uname or mid or "未命名UP主",
                    "url": f"https://space.bilibili.com/{mid}" if mid else "",
                    "snippet": snippet,
                    "mid": mid,
                }
            )
        return [item for item in results if item.get("url")]

    @staticmethod
    def _web_filter_results(
        results: list[dict[str, str]],
        *,
        mode: str,
        max_results: int,
    ) -> list[dict[str, str]]:
        filtered: list[dict[str, str]] = []
        for row in results:
            url = clean_text(str(row.get("url") or ""), max_len=300)
            if not url:
                continue
            if mode == "video" and "/video/" not in url:
                continue
            if mode == "user" and "space.bilibili.com" not in url and "/space/" not in url:
                continue
            filtered.append(
                {
                    "title": clean_multiline_text(str(row.get("title") or url), max_len=140),
                    "url": url,
                    "snippet": clean_multiline_text(str(row.get("snippet") or ""), max_len=180),
                }
            )
            if len(filtered) >= max_results:
                break
        return filtered

    async def _search_videos_via_web(self, keyword: str, *, max_results: int) -> tuple[list[dict[str, str]], str]:
        results, effective_query = await site_search(
            keyword,
            query_candidates=[
                f"{keyword} site:bilibili.com/video",
                f"{keyword} site:www.bilibili.com/video",
                f"{keyword} B站 视频",
            ],
            max_results=max_results * 2,
            allowed_hosts=("bilibili.com",),
        )
        return self._web_filter_results(results, mode="video", max_results=max_results), effective_query

    async def _search_users_via_web(self, keyword: str, *, max_results: int) -> tuple[list[dict[str, str]], str]:
        results, effective_query = await site_search(
            keyword,
            query_candidates=[
                f"{keyword} site:space.bilibili.com",
                f"{keyword} site:bilibili.com/space",
                f"{keyword} B站 UP 主",
            ],
            max_results=max_results * 2,
            allowed_hosts=("space.bilibili.com", "bilibili.com"),
        )
        return self._web_filter_results(results, mode="user", max_results=max_results), effective_query

    async def _search_videos(self, keyword: str, *, page: int, max_results: int) -> SkillRunResult:
        effective_query = keyword
        try:
            data = await self._api_get(
                "/x/web-interface/search/type",
                params={
                    "search_type": "video",
                    "keyword": keyword,
                    "page": max(1, page),
                    "page_size": max_results,
                },
            )
            rows = data.get("result") if isinstance(data, dict) else []
            results = self._normalize_video_results(rows or [], max_results=max_results)
        except ValueError as exc:
            if str(exc) != "http_412":
                raise
            log.warning("bilibili video search hit 412, fallback to web search | keyword=%s", keyword)
            results, effective_query = await self._search_videos_via_web(keyword, max_results=max_results)
        if not results:
            return SkillRunResult(ok=False, skill=self.name, summary="没有找到相关 B 站视频", error="empty_result")
        return SkillRunResult(
            ok=True,
            skill=self.name,
            summary=f"找到 {len(results)} 条 B 站视频结果",
            payload={
                "platform": "bilibili",
                "action": "search_videos",
                "keyword": keyword,
                "effective_query": effective_query,
                "results": results,
            },
        )

    async def _search_users(self, keyword: str, *, page: int, max_results: int) -> SkillRunResult:
        effective_query = keyword
        try:
            data = await self._api_get(
                "/x/web-interface/search/type",
                params={
                    "search_type": "bili_user",
                    "keyword": keyword,
                    "page": max(1, page),
                    "page_size": max_results,
                },
            )
            rows = data.get("result") if isinstance(data, dict) else []
            results = self._normalize_user_results(rows or [], max_results=max_results)
        except ValueError as exc:
            if str(exc) != "http_412":
                raise
            log.warning("bilibili user search hit 412, fallback to web search | keyword=%s", keyword)
            results, effective_query = await self._search_users_via_web(keyword, max_results=max_results)
        if not results:
            return SkillRunResult(ok=False, skill=self.name, summary="没有找到相关 B 站UP主", error="empty_result")
        return SkillRunResult(
            ok=True,
            skill=self.name,
            summary=f"找到 {len(results)} 条 B 站UP主结果",
            payload={
                "platform": "bilibili",
                "action": "search_users",
                "keyword": keyword,
                "effective_query": effective_query,
                "results": results,
            },
        )

    async def _get_hot(self, *, page: int, max_results: int) -> SkillRunResult:
        data = await self._api_get(
            "/x/web-interface/popular",
            params={"pn": max(1, page), "ps": max_results},
        )
        rows = data.get("list") if isinstance(data, dict) else []
        results = self._normalize_video_results(rows or [], max_results=max_results)
        if not results:
            return SkillRunResult(ok=False, skill=self.name, summary="没有拿到 B 站热门视频", error="empty_result")
        return SkillRunResult(
            ok=True,
            skill=self.name,
            summary=f"拿到 {len(results)} 条 B 站热门视频",
            payload={"platform": "bilibili", "action": "get_hot", "results": results},
        )

    async def _get_rank(self, *, max_results: int) -> SkillRunResult:
        data = await self._api_get(
            "/x/web-interface/ranking/v2",
            params={"rid": 0, "type": "all"},
        )
        rows: list[dict[str, Any]]
        if isinstance(data, dict):
            rows = data.get("list") or []
        elif isinstance(data, list):
            rows = data
        else:
            rows = []
        results = self._normalize_video_results(rows, max_results=max_results)
        if not results:
            return SkillRunResult(ok=False, skill=self.name, summary="没有拿到 B 站排行榜", error="empty_result")
        return SkillRunResult(
            ok=True,
            skill=self.name,
            summary=f"拿到 {len(results)} 条 B 站排行榜视频",
            payload={"platform": "bilibili", "action": "get_rank", "results": results},
        )

    async def _fetch_subtitle_excerpt(self, *, bvid: str, cid: int) -> str:
        try:
            player_data = await self._api_get("/x/player/v2", params={"bvid": bvid, "cid": cid})
        except Exception as exc:
            log.warning("bilibili subtitle metadata failed: bvid=%s error=%s", bvid, exc)
            return ""
        subtitles = (
            player_data.get("subtitle", {}).get("subtitles", [])
            if isinstance(player_data, dict)
            else []
        )
        if not subtitles:
            return ""
        subtitle_url = clean_text(str(subtitles[0].get("subtitle_url") or ""), max_len=500)
        if not subtitle_url:
            return ""
        if subtitle_url.startswith("//"):
            subtitle_url = f"https:{subtitle_url}"
        try:
            status, payload, _ = await fetch_json(subtitle_url, headers=_BILI_HEADERS, timeout_sec=18.0)
        except Exception as exc:
            log.warning("bilibili subtitle fetch failed: bvid=%s error=%s", bvid, exc)
            return ""
        if status >= 400 or not isinstance(payload, dict):
            return ""
        lines = []
        for item in payload.get("body", [])[:12]:
            if not isinstance(item, dict):
                continue
            content = clean_text(str(item.get("content") or ""), max_len=80)
            if content:
                lines.append(content)
        return clean_multiline_text(" ".join(lines), max_len=320)

    async def _fetch_top_comments(self, *, aid: int, max_results: int) -> list[dict[str, str]]:
        try:
            data = await self._api_get(
                "/x/v2/reply/main",
                params={"oid": aid, "type": 1, "mode": 3, "next": 0, "ps": max_results},
            )
        except Exception as exc:
            log.warning("bilibili comments fetch failed: aid=%s error=%s", aid, exc)
            return []
        replies = data.get("replies") if isinstance(data, dict) else []
        comments: list[dict[str, str]] = []
        for row in replies or []:
            if len(comments) >= max_results:
                break
            member = row.get("member") if isinstance(row, dict) else {}
            content = row.get("content") if isinstance(row, dict) else {}
            message = clean_multiline_text(
                html.unescape(str(content.get("message") or "")),
                max_len=200,
            )
            if not message:
                continue
            comments.append(
                {
                    "author": clean_text(str(member.get("uname") or ""), max_len=60),
                    "content": message,
                }
            )
        return comments

    async def _get_video(
        self,
        bvid_or_url: str,
        *,
        include_subtitle: bool,
        include_comments: bool,
        max_results: int,
    ) -> SkillRunResult:
        bvid = self._extract_bvid(bvid_or_url)
        if not bvid:
            return SkillRunResult(ok=False, skill=self.name, summary="没有识别到有效 BV 号", error="invalid_bvid")

        data = await self._api_get("/x/web-interface/view", params={"bvid": bvid})
        if not isinstance(data, dict):
            return SkillRunResult(ok=False, skill=self.name, summary="B 站视频详情格式异常", error="invalid_payload")

        owner = data.get("owner") or {}
        stat = data.get("stat") or {}
        cid = int(data.get("cid") or (data.get("pages") or [{}])[0].get("cid") or 0)
        entry: dict[str, Any] = {
            "title": clean_multiline_text(str(data.get("title") or bvid), max_len=180),
            "url": self._compose_video_url(bvid),
            "bvid": bvid,
            "owner": clean_text(str(owner.get("name") or ""), max_len=60),
            "desc": clean_multiline_text(str(data.get("desc") or ""), max_len=520),
            "duration_sec": int(data.get("duration") or 0),
            "pubdate": self._format_pubdate(data.get("pubdate")),
            "stats": {
                "view": int(stat.get("view") or 0),
                "reply": int(stat.get("reply") or 0),
                "like": int(stat.get("like") or 0),
                "coin": int(stat.get("coin") or 0),
                "favorite": int(stat.get("favorite") or 0),
                "share": int(stat.get("share") or 0),
            },
        }

        if include_subtitle and cid > 0:
            subtitle_excerpt = await self._fetch_subtitle_excerpt(bvid=bvid, cid=cid)
            if subtitle_excerpt:
                entry["subtitle_excerpt"] = subtitle_excerpt
        if include_comments:
            aid = int(data.get("aid") or 0)
            if aid > 0:
                comments = await self._fetch_top_comments(aid=aid, max_results=min(5, max_results))
                if comments:
                    entry["comments"] = comments

        title = clean_text(entry["title"], max_len=80)
        return SkillRunResult(
            ok=True,
            skill=self.name,
            summary=f"已获取 B 站视频《{title}》详情",
            payload={"platform": "bilibili", "action": "get_video", "entry": entry},
        )

    async def run(self, arguments: dict[str, Any], context: SkillContext) -> SkillRunResult:
        action = clean_text(str(arguments.get("action") or ""), max_len=32).lower()
        keyword = clean_text(str(arguments.get("keyword") or ""), max_len=120)
        bvid_or_url = clean_text(str(arguments.get("bvid_or_url") or ""), max_len=220)
        page = self._safe_count(arguments.get("page", 1), default=1, upper=50)
        max_results = self._safe_count(arguments.get("max_results", 5), default=5, upper=10)
        include_subtitle = bool(arguments.get("include_subtitle", True))
        include_comments = bool(arguments.get("include_comments", False))
        if action == "get_video" and not include_comments and self._wants_comments_from_text(context.current_user_text):
            include_comments = True

        try:
            if action == "search_videos":
                if not keyword:
                    return SkillRunResult(ok=False, skill=self.name, summary="B 站搜索词为空", error="empty_keyword")
                return await self._search_videos(keyword, page=page, max_results=max_results)
            if action == "search_users":
                if not keyword:
                    return SkillRunResult(ok=False, skill=self.name, summary="B 站UP主搜索词为空", error="empty_keyword")
                return await self._search_users(keyword, page=page, max_results=max_results)
            if action == "get_hot":
                return await self._get_hot(page=page, max_results=max_results)
            if action == "get_rank":
                return await self._get_rank(max_results=max_results)
            if action == "get_video":
                if not bvid_or_url:
                    return SkillRunResult(ok=False, skill=self.name, summary="缺少 BV 号或 B 站链接", error="missing_bvid")
                return await self._get_video(
                    bvid_or_url,
                    include_subtitle=include_subtitle,
                    include_comments=include_comments,
                    max_results=max_results,
                )
        except Exception as exc:
            log.exception("bilibili_search failed: action=%s", action)
            return SkillRunResult(
                ok=False,
                skill=self.name,
                summary=f"B 站技能执行失败：{clean_text(str(exc), max_len=120)}",
                error=clean_text(str(exc), max_len=120) or "runtime_error",
            )

        return SkillRunResult(ok=False, skill=self.name, summary="未知 B 站动作", error="unknown_action")
