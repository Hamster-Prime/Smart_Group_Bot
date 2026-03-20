from __future__ import annotations

import asyncio
import html
import logging
import re
from typing import Any

import aiohttp
from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest, TelegramRetryAfter
from aiogram.types import Message

from bot.services.skills.base import SkillContext, SkillRunResult
from bot.utils.telegram import (
    is_reply_target_missing_error,
    sanitize_outgoing_text,
    schedule_message_auto_delete,
)
from bot.utils.security import clean_multiline_text, clean_text

log = logging.getLogger(__name__)

_SOURCE_RE = re.compile(r"^[a-z][a-z0-9_]{0,31}$")
_LRC_TIMESTAMP_RE = re.compile(r"\[[0-9]{1,2}:[0-9]{2}(?:\.[0-9]{1,3})?\]")
_BLANK_LINE_RE = re.compile(r"\n{3,}")
_DEFAULT_API_BASE_URL = "https://music-api.gdstudio.xyz/api.php"
_DEFAULT_STABLE_SOURCES = ("kuwo", "netease", "joox", "bilibili")
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
        "Search songs with the GD Studio music API, send a song directly as Telegram audio using a remote URL, "
        "or fetch a song URL, album cover, or lyrics. When ids are unknown, search first and then use the "
        "returned track_id / pic_id / lyric_id."
    )
    parameters_schema = {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["search", "send_audio", "get_url", "get_cover", "get_lyric"],
                "description": "Which music action to run.",
            },
            "keyword": {
                "type": "string",
                "description": (
                    "Required when action=search. Also accepted by action=send_audio when you want the tool to "
                    "search first and send the top match."
                ),
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
                "description": "Track id from a search result. Required when action=get_url, or optional for action=send_audio.",
            },
            "quality": {
                "type": "integer",
                "enum": [128, 192, 320, 740, 999],
                "description": "Preferred bitrate for action=get_url. action=send_audio always sends 320k mp3.",
            },
            "title": {
                "type": "string",
                "description": "Optional Telegram audio title override when action=send_audio.",
            },
            "performer": {
                "type": "string",
                "description": "Optional Telegram audio performer override when action=send_audio.",
            },
            "caption_text": {
                "type": "string",
                "description": (
                    "Optional short natural assistant sentence to place in the same Telegram audio message caption "
                    "when action=send_audio."
                ),
            },
            "delivery_mode": {
                "type": "string",
                "enum": ["reply", "message"],
                "description": "When action=send_audio, reply replies to the triggering message; message sends standalone.",
                "default": "reply",
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
            str(getattr(settings, "music_api_default_source", "kuwo") or "kuwo"),
            allow_auto=False,
        )
        self.default_source = default_source or "kuwo"

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
    def _normalize_delivery_mode(value: Any) -> str:
        mode = clean_text(str(value or "reply"), max_len=16).lower()
        if mode not in {"reply", "message"}:
            return "reply"
        return mode

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
        parsed = self._parse_url_payload(data, source=source, track_id=track_id, requested_quality=quality)
        return SkillRunResult(
            ok=True,
            skill=self.name,
            summary="已获取歌曲链接",
            payload={"action": "get_url", **parsed},
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

    def _parse_url_payload(
        self,
        data: Any,
        *,
        source: str,
        track_id: str,
        requested_quality: int,
    ) -> dict[str, Any]:
        if not isinstance(data, dict):
            raise _MusicAPIError("歌曲链接返回格式异常", "invalid_payload")

        url = clean_text(str(data.get("url", "")), max_len=2000)
        if not url:
            raise _MusicAPIError("没有拿到可用的歌曲链接", "empty_url")

        bitrate = self._normalize_int(data.get("br", requested_quality), default=requested_quality, minimum=0, maximum=999)
        size_kb = self._normalize_int(data.get("size", 0), default=0, minimum=0, maximum=2_000_000_000)
        return {
            "source": source,
            "track_id": track_id,
            "requested_quality": requested_quality,
            "bitrate": bitrate,
            "size_kb": size_kb,
            "url": url,
        }

    @staticmethod
    def _build_default_caption(title: str, performer: str) -> str:
        if title and performer:
            return f"这首《{title}》给你，{performer} 的版本，听听看合不合口味。"
        if title:
            return f"这首《{title}》给你，听听看是不是你想找的这版。"
        return "给你挑了一首，听听看喜不喜欢。"

    def _resolve_send_audio_caption(self, arguments: dict[str, Any], *, title: str, performer: str) -> str:
        caption_text = clean_multiline_text(str(arguments.get("caption_text", "")), max_len=800)
        if not caption_text:
            caption_text = self._build_default_caption(title, performer)
        return sanitize_outgoing_text(caption_text).strip()[:900]

    @staticmethod
    def _resolve_send_audio_quality(_: dict[str, Any], __: SkillContext) -> int:
        return 320

    async def _resolve_send_audio_target(self, arguments: dict[str, Any]) -> dict[str, Any]:
        track_id = clean_text(str(arguments.get("track_id", "")), max_len=64)
        source = self._normalize_source(str(arguments.get("source", "")), allow_auto=True) or "auto"
        title = clean_text(str(arguments.get("title", "")), max_len=200)
        performer = clean_text(str(arguments.get("performer", "")), max_len=200)

        if track_id:
            return {
                "track_id": track_id,
                "source": self.default_source if source == "auto" else source,
                "title": title,
                "performer": performer,
            }

        keyword = clean_text(str(arguments.get("keyword", "")), max_len=120)
        if not keyword:
            raise _MusicAPIError("send_audio 需要提供 track_id 或 keyword", "missing_send_target")

        result = await self._search(
            {
                "keyword": keyword,
                "source": source,
                "count": arguments.get("count", 5),
                "page": arguments.get("page", 1),
            }
        )
        if not result.ok:
            raise _MusicAPIError(result.summary, result.error or "no_results")

        results = result.payload.get("results") if isinstance(result.payload, dict) else None
        if not isinstance(results, list) or not results:
            raise _MusicAPIError("没有找到可发送的歌曲", "no_results")

        first = results[0] if isinstance(results[0], dict) else {}
        return {
            "track_id": clean_text(str(first.get("track_id", "")), max_len=64),
            "source": self._normalize_source(str(first.get("source", "")), allow_auto=False) or self.default_source,
            "title": title or clean_text(str(first.get("name", "")), max_len=200),
            "performer": performer or clean_text(str(first.get("artist_text", "")), max_len=200),
        }

    async def _send_audio_to_message(
        self,
        message: Message,
        *,
        audio_url: str,
        title: str,
        performer: str,
        caption_text: str,
        delivery_mode: str,
        auto_delete_minutes: int,
    ) -> bool:
        send_as_reply = delivery_mode == "reply"
        attempt = 0
        while attempt <= 2:
            try:
                kwargs = {
                    "audio": audio_url,
                    "title": title or None,
                    "performer": performer or None,
                    "caption": html.escape(caption_text) or None,
                    "parse_mode": "HTML",
                }
                if send_as_reply:
                    sent = await message.reply_audio(**kwargs)
                else:
                    sent = await message.answer_audio(**kwargs)
                schedule_message_auto_delete(sent, auto_delete_minutes)
                return True
            except TelegramRetryAfter as exc:
                wait_s = max(0.5, float(getattr(exc, "retry_after", 1.0))) + 0.2
                await asyncio.sleep(wait_s)
                attempt += 1
            except TelegramBadRequest as exc:
                log.warning("music audio send failed in message context: %s", exc)
                return False
            except Exception:
                if attempt >= 2:
                    log.exception("music audio send failed in message context")
                    return False
                attempt += 1
                await asyncio.sleep(0.6 * attempt)
        return False

    async def _send_audio_to_chat(
        self,
        bot: Bot,
        *,
        chat_id: int,
        audio_url: str,
        title: str,
        performer: str,
        caption_text: str,
        reply_to_message_id: int | None = None,
        fallback_mention_user_id: int = 0,
        fallback_mention_name: str = "",
        auto_delete_minutes: int = 0,
    ) -> bool:
        attempt = 0
        current_reply_to = reply_to_message_id
        current_caption = html.escape(caption_text) if caption_text else ""
        current_parse_mode: str | None = "HTML" if current_caption else None
        fallback_caption_html = ""
        if fallback_mention_user_id:
            shown = html.escape((fallback_mention_name or str(fallback_mention_user_id)).strip())
            fallback_caption_html = f'<a href="tg://user?id={fallback_mention_user_id}">@{shown}</a> {current_caption}'.strip()

        while attempt <= 2:
            try:
                sent = await bot.send_audio(
                    chat_id=chat_id,
                    audio=audio_url,
                    title=title or None,
                    performer=performer or None,
                    reply_to_message_id=current_reply_to,
                    caption=current_caption or None,
                    parse_mode=current_parse_mode,
                )
                schedule_message_auto_delete(sent, auto_delete_minutes)
                return True
            except TelegramRetryAfter as exc:
                wait_s = max(0.5, float(getattr(exc, "retry_after", 1.0))) + 0.2
                await asyncio.sleep(wait_s)
                attempt += 1
            except TelegramBadRequest as exc:
                detail = str(exc).lower()
                if current_reply_to and is_reply_target_missing_error(detail):
                    current_reply_to = None
                    if fallback_caption_html:
                        current_caption = fallback_caption_html
                        current_parse_mode = "HTML"
                    attempt += 1
                    continue
                log.warning("music audio send failed in bot context chat_id=%s error=%s", chat_id, exc)
                return False
            except Exception:
                if attempt >= 2:
                    log.exception("music audio send failed in bot context chat_id=%s", chat_id)
                    return False
                attempt += 1
                await asyncio.sleep(0.6 * attempt)
        return False

    async def _send_audio(self, arguments: dict[str, Any], context: SkillContext) -> SkillRunResult:
        if context.message is None and (context.bot is None or int(context.chat_id or 0) == 0):
            return SkillRunResult(ok=False, skill=self.name, summary="当前上下文不支持发送音乐", error="missing_chat_context")

        target = await self._resolve_send_audio_target(arguments)
        quality = self._resolve_send_audio_quality(arguments, context)
        delivery_mode = self._normalize_delivery_mode(arguments.get("delivery_mode", "reply"))

        url_payload = self._parse_url_payload(
            await self._request_json(
                {
                    "types": "url",
                    "source": target["source"],
                    "id": target["track_id"],
                    "br": quality,
                }
            ),
            source=target["source"],
            track_id=target["track_id"],
            requested_quality=quality,
        )

        title = clean_text(str(target.get("title", "")), max_len=200)
        performer = clean_text(str(target.get("performer", "")), max_len=200)
        caption_text = self._resolve_send_audio_caption(arguments, title=title, performer=performer)
        audio_url = clean_text(str(url_payload.get("url", "")), max_len=2000)

        ok = False
        if context.message is not None:
            ok = await self._send_audio_to_message(
                context.message,
                audio_url=audio_url,
                title=title,
                performer=performer,
                caption_text=caption_text,
                delivery_mode=delivery_mode,
                auto_delete_minutes=0,
            )
        elif context.bot is not None and int(context.chat_id or 0):
            ok = await self._send_audio_to_chat(
                context.bot,
                chat_id=int(context.chat_id),
                audio_url=audio_url,
                title=title,
                performer=performer,
                caption_text=caption_text,
                auto_delete_minutes=0,
            )

        if not ok:
            return SkillRunResult(ok=False, skill=self.name, summary="音乐发送失败", error="send_audio_failed")

        context.handled = True
        context.embedded_reply_sent = True
        context.embedded_reply_text = caption_text
        return SkillRunResult(
            ok=True,
            skill=self.name,
            summary="",
            payload={
                "action": "send_audio",
                **url_payload,
                "title": title,
                "performer": performer,
                "caption_text": caption_text,
                "delivery_mode": delivery_mode,
                "transport": "telegram_remote_url",
            },
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

        if action == "send_audio":
            try:
                return await self._send_audio(arguments, context)
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
