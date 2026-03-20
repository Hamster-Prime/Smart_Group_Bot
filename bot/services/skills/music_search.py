from __future__ import annotations

import asyncio
import logging
import re
from typing import Any

import aiohttp

from bot.services.skills.base import SkillContext, SkillRunResult
from bot.utils.security import clean_multiline_text, clean_text

log = logging.getLogger(__name__)

_SOURCE_RE = re.compile(r"^[a-z][a-z0-9_]{0,31}$")
_LRC_TIMESTAMP_RE = re.compile(r"\[[0-9]{1,2}:[0-9]{2}(?:\.[0-9]{1,3})?\]")
_BLANK_LINE_RE = re.compile(r"\n{3,}")
_DEFAULT_API_BASE_URL = "https://music-api.gdstudio.xyz/api.php"
_DEFAULT_STABLE_SOURCES = ("netease", "kuwo", "joox", "bilibili")
_ALLOWED_QUALITIES = {128, 192, 320, 740, 999}
_ALLOWED_IMAGE_SIZES = {300, 500}


class _MusicAPIError(Exception):
    def __init__(self, summary: str, code: str) -> None:
        super().__init__(summary)
        self.summary = summary
        self.code = code


class MusicSearchSkill:
    name = "music_search"
    description = (
        "Search songs with the GD Studio music API, or fetch a song URL, album cover, or lyrics. "
        "When ids are unknown, search first and then use the returned track_id / pic_id / lyric_id."
    )
    parameters_schema = {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["search", "get_url", "get_cover", "get_lyric"],
                "description": "Which music action to run.",
            },
            "keyword": {
                "type": "string",
                "description": "Required when action=search. Song, artist, or album keyword.",
            },
            "source": {
                "type": "string",
                "description": (
                    "Optional music source. Use the source returned by search results for follow-up calls. "
                    "For search, omit it or use auto to try stable sources."
                ),
            },
            "count": {
                "type": "integer",
                "description": "Search result count when action=search, recommended 3-8.",
                "default": 5,
            },
            "page": {
                "type": "integer",
                "description": "Search result page number when action=search.",
                "default": 1,
            },
            "track_id": {
                "type": "string",
                "description": "Track id from a search result. Required when action=get_url.",
            },
            "quality": {
                "type": "integer",
                "enum": [128, 192, 320, 740, 999],
                "description": "Preferred bitrate when action=get_url.",
                "default": 999,
            },
            "pic_id": {
                "type": "string",
                "description": "Album cover id from a search result. Required when action=get_cover.",
            },
            "image_size": {
                "type": "integer",
                "enum": [300, 500],
                "description": "Preferred cover image size when action=get_cover.",
                "default": 500,
            },
            "lyric_id": {
                "type": "string",
                "description": (
                    "Lyric id from a search result. Required when action=get_lyric. "
                    "Usually the same as track_id."
                ),
            },
            "plain_text": {
                "type": "boolean",
                "description": "When action=get_lyric, also return a plain-text lyric without LRC timestamps.",
                "default": False,
            },
        },
        "required": ["action"],
        "additionalProperties": False,
    }

    def __init__(self, settings: Any | None = None) -> None:
        self.enabled = bool(getattr(settings, "music_api_enabled", True))
        self.api_base_url = clean_text(
            str(getattr(settings, "music_api_base_url", _DEFAULT_API_BASE_URL) or _DEFAULT_API_BASE_URL),
            max_len=255,
        )
        self.http_timeout_sec = max(5.0, float(getattr(settings, "music_api_http_timeout_sec", 15.0) or 15.0))

        default_source = self._normalize_source(
            str(getattr(settings, "music_api_default_source", "netease") or "netease"),
            allow_auto=False,
        )
        self.default_source = default_source or "netease"

        stable_sources = self._parse_sources(
            str(getattr(settings, "music_api_stable_sources", ",".join(_DEFAULT_STABLE_SOURCES)) or "")
        )
        self.stable_sources = stable_sources or list(_DEFAULT_STABLE_SOURCES)

    @classmethod
    def _normalize_source(cls, value: str, *, allow_auto: bool) -> str:
        source = clean_text(value, max_len=32).lower().replace("-", "_")
        if not source:
            return "auto" if allow_auto else ""
        if allow_auto and source == "auto":
            return "auto"
        if not _SOURCE_RE.fullmatch(source):
            return ""
        return source

    @classmethod
    def _parse_sources(cls, raw: str) -> list[str]:
        out: list[str] = []
        seen: set[str] = set()
        for item in (raw or "").split(","):
            source = cls._normalize_source(item, allow_auto=False)
            if not source or source in seen:
                continue
            seen.add(source)
            out.append(source)
        return out

    @staticmethod
    def _normalize_int(value: Any, *, default: int, minimum: int, maximum: int) -> int:
        try:
            parsed = int(value)
        except Exception:
            parsed = default
        return max(minimum, min(parsed, maximum))

    @staticmethod
    def _normalize_bool(value: Any, *, default: bool = False) -> bool:
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            normalized = value.strip().lower()
            if normalized in {"1", "true", "yes", "on"}:
                return True
            if normalized in {"0", "false", "no", "off"}:
                return False
        return default

    @staticmethod
    def _dedupe_keep_order(items: list[str]) -> list[str]:
        out: list[str] = []
        seen: set[str] = set()
        for item in items:
            value = (item or "").strip()
            if not value or value in seen:
                continue
            seen.add(value)
            out.append(value)
        return out

    def _build_search_sources(self, requested_source: str) -> list[str]:
        if requested_source and requested_source != "auto":
            return [requested_source]
        return self._dedupe_keep_order([self.default_source, *self.stable_sources])

    @staticmethod
    def _normalize_artists(raw: Any) -> list[str]:
        if isinstance(raw, str):
            artist = clean_text(raw, max_len=120)
            return [artist] if artist else []
        if not isinstance(raw, list):
            return []

        artists: list[str] = []
        for item in raw:
            artist = clean_text(str(item or ""), max_len=80)
            if artist:
                artists.append(artist)
        return artists

    @classmethod
    def _normalize_track(cls, item: Any) -> dict[str, Any]:
        row = item if isinstance(item, dict) else {}
        artists = cls._normalize_artists(row.get("artist"))
        return {
            "track_id": clean_text(str(row.get("id", "")), max_len=64),
            "name": clean_text(str(row.get("name", "")), max_len=200),
            "artists": artists,
            "artist_text": clean_text(" / ".join(artists), max_len=240),
            "album": clean_text(str(row.get("album", "")), max_len=200),
            "pic_id": clean_text(str(row.get("pic_id", "")), max_len=64),
            "lyric_id": clean_text(str(row.get("lyric_id", "")), max_len=64),
            "source": clean_text(str(row.get("source", "")), max_len=32).lower(),
        }

    @staticmethod
    def _strip_lrc_timestamps(text: str) -> str:
        plain = clean_multiline_text(text, max_len=8000)
        plain = _LRC_TIMESTAMP_RE.sub("", plain)
        plain = re.sub(r"(?m)^\[[^\]]+\]\s*", "", plain)
        plain = "\n".join(line.strip() for line in plain.splitlines())
        plain = _BLANK_LINE_RE.sub("\n\n", plain)
        return clean_multiline_text(plain, max_len=6000)

    async def _request_json(self, params: dict[str, Any]) -> Any:
        headers = {
            "User-Agent": "SmartGroupBot/1.0 (+https://example.local)",
            "Accept": "application/json,text/plain;q=0.9,*/*;q=0.8",
        }
        timeout = aiohttp.ClientTimeout(total=self.http_timeout_sec)

        try:
            async with aiohttp.ClientSession(timeout=timeout, headers=headers) as session:
                async with session.get(self.api_base_url, params=params, allow_redirects=True) as resp:
                    if resp.status >= 400:
                        raise _MusicAPIError(f"音乐接口请求失败: HTTP {resp.status}", f"http_{resp.status}")
                    try:
                        return await resp.json(content_type=None)
                    except Exception:
                        raw = await resp.text(errors="ignore")
                        log.warning("music_search invalid json | params=%s body=%s", params, raw[:200])
                        raise _MusicAPIError("音乐接口返回了无法解析的数据", "invalid_json") from None
        except asyncio.TimeoutError as exc:
            raise _MusicAPIError("音乐接口请求超时", "timeout") from exc
        except aiohttp.ClientError as exc:
            raise _MusicAPIError("音乐接口连接失败", exc.__class__.__name__) from exc

    async def _search(self, arguments: dict[str, Any]) -> SkillRunResult:
        keyword = clean_text(str(arguments.get("keyword", "")), max_len=120)
        if not keyword:
            return SkillRunResult(ok=False, skill=self.name, summary="歌曲关键词为空", error="empty_keyword")

        requested_source = self._normalize_source(str(arguments.get("source", "")), allow_auto=True) or "auto"
        count = self._normalize_int(arguments.get("count", 5), default=5, minimum=1, maximum=10)
        page = self._normalize_int(arguments.get("page", 1), default=1, minimum=1, maximum=50)
        attempted_sources = self._build_search_sources(requested_source)

        for source in attempted_sources:
            data = await self._request_json(
                {
                    "types": "search",
                    "source": source,
                    "name": keyword,
                    "count": count,
                    "pages": page,
                }
            )
            if not isinstance(data, list):
                continue

            results = [self._normalize_track(item) for item in data]
            results = [item for item in results if item["track_id"]]
            if not results:
                continue

            return SkillRunResult(
                ok=True,
                skill=self.name,
                summary=f"找到 {len(results)} 首相关歌曲",
                payload={
                    "action": "search",
                    "query": keyword,
                    "source": source,
                    "requested_source": requested_source,
                    "attempted_sources": attempted_sources,
                    "page": page,
                    "count": count,
                    "results": results,
                },
            )

        return SkillRunResult(
            ok=False,
            skill=self.name,
            summary=f"没有找到与“{keyword}”相关的歌曲",
            error="no_results",
        )

    async def _get_url(self, arguments: dict[str, Any]) -> SkillRunResult:
        track_id = clean_text(str(arguments.get("track_id", "")), max_len=64)
        if not track_id:
            return SkillRunResult(ok=False, skill=self.name, summary="缺少 track_id", error="missing_track_id")

        source = self._normalize_source(str(arguments.get("source", "")), allow_auto=False) or self.default_source
        quality = self._normalize_int(arguments.get("quality", 999), default=999, minimum=128, maximum=999)
        if quality not in _ALLOWED_QUALITIES:
            quality = 999

        data = await self._request_json(
            {
                "types": "url",
                "source": source,
                "id": track_id,
                "br": quality,
            }
        )
        if not isinstance(data, dict):
            return SkillRunResult(ok=False, skill=self.name, summary="歌曲链接返回格式异常", error="invalid_payload")

        url = clean_text(str(data.get("url", "")), max_len=2000)
        if not url:
            return SkillRunResult(ok=False, skill=self.name, summary="没有拿到可用的歌曲链接", error="empty_url")

        bitrate = self._normalize_int(data.get("br", quality), default=quality, minimum=0, maximum=999)
        size_kb = self._normalize_int(data.get("size", 0), default=0, minimum=0, maximum=2_000_000_000)
        return SkillRunResult(
            ok=True,
            skill=self.name,
            summary="已获取歌曲链接",
            payload={
                "action": "get_url",
                "source": source,
                "track_id": track_id,
                "requested_quality": quality,
                "bitrate": bitrate,
                "size_kb": size_kb,
                "url": url,
            },
        )

    async def _get_cover(self, arguments: dict[str, Any]) -> SkillRunResult:
        pic_id = clean_text(str(arguments.get("pic_id", "")), max_len=64)
        if not pic_id:
            return SkillRunResult(ok=False, skill=self.name, summary="缺少 pic_id", error="missing_pic_id")

        source = self._normalize_source(str(arguments.get("source", "")), allow_auto=False) or self.default_source
        image_size = self._normalize_int(arguments.get("image_size", 500), default=500, minimum=300, maximum=500)
        if image_size not in _ALLOWED_IMAGE_SIZES:
            image_size = 500

        data = await self._request_json(
            {
                "types": "pic",
                "source": source,
                "id": pic_id,
                "size": image_size,
            }
        )
        if not isinstance(data, dict):
            return SkillRunResult(ok=False, skill=self.name, summary="专辑图返回格式异常", error="invalid_payload")

        url = clean_text(str(data.get("url", "")), max_len=2000)
        if not url:
            return SkillRunResult(ok=False, skill=self.name, summary="没有拿到可用的专辑图链接", error="empty_cover_url")

        return SkillRunResult(
            ok=True,
            skill=self.name,
            summary="已获取专辑图链接",
            payload={
                "action": "get_cover",
                "source": source,
                "pic_id": pic_id,
                "image_size": image_size,
                "url": url,
            },
        )

    async def _get_lyric(self, arguments: dict[str, Any]) -> SkillRunResult:
        lyric_id = clean_text(str(arguments.get("lyric_id", "") or arguments.get("track_id", "")), max_len=64)
        if not lyric_id:
            return SkillRunResult(ok=False, skill=self.name, summary="缺少 lyric_id", error="missing_lyric_id")

        source = self._normalize_source(str(arguments.get("source", "")), allow_auto=False) or self.default_source
        plain_text = self._normalize_bool(arguments.get("plain_text", False), default=False)

        data = await self._request_json(
            {
                "types": "lyric",
                "source": source,
                "id": lyric_id,
            }
        )
        if not isinstance(data, dict):
            return SkillRunResult(ok=False, skill=self.name, summary="歌词返回格式异常", error="invalid_payload")

        lyric = clean_multiline_text(str(data.get("lyric", "")), max_len=8000)
        translated_lyric = clean_multiline_text(str(data.get("tlyric", "")), max_len=8000)
        if not lyric and not translated_lyric:
            return SkillRunResult(ok=False, skill=self.name, summary="没有拿到可用的歌词", error="empty_lyric")

        payload: dict[str, Any] = {
            "action": "get_lyric",
            "source": source,
            "lyric_id": lyric_id,
            "lyric": lyric,
            "translated_lyric": translated_lyric,
        }
        if plain_text:
            payload["plain_lyric"] = self._strip_lrc_timestamps(lyric)
            if translated_lyric:
                payload["plain_translated_lyric"] = self._strip_lrc_timestamps(translated_lyric)

        return SkillRunResult(
            ok=True,
            skill=self.name,
            summary="已获取歌词",
            payload=payload,
        )

    async def run(self, arguments: dict[str, Any], context: SkillContext) -> SkillRunResult:
        _ = context  # Unused for this skill.
        if not self.enabled:
            return SkillRunResult(ok=False, skill=self.name, summary="音乐搜索技能当前已关闭", error="disabled")

        action = clean_text(str(arguments.get("action", "")), max_len=32).lower()
        if action == "search":
            try:
                return await self._search(arguments)
            except _MusicAPIError as exc:
                return SkillRunResult(ok=False, skill=self.name, summary=exc.summary, error=exc.code)

        if action == "get_url":
            try:
                return await self._get_url(arguments)
            except _MusicAPIError as exc:
                return SkillRunResult(ok=False, skill=self.name, summary=exc.summary, error=exc.code)

        if action == "get_cover":
            try:
                return await self._get_cover(arguments)
            except _MusicAPIError as exc:
                return SkillRunResult(ok=False, skill=self.name, summary=exc.summary, error=exc.code)

        if action == "get_lyric":
            try:
                return await self._get_lyric(arguments)
            except _MusicAPIError as exc:
                return SkillRunResult(ok=False, skill=self.name, summary=exc.summary, error=exc.code)

        return SkillRunResult(ok=False, skill=self.name, summary="未知音乐操作", error="unknown_action")
