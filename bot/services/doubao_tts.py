from __future__ import annotations

import asyncio
import base64
import html
import json
import logging
import os
import re
import subprocess
import tempfile
import uuid
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import aiohttp
from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest, TelegramRetryAfter
from aiogram.types import BufferedInputFile, Message

from bot.config import Settings
from bot.db.models import Group
from bot.utils.telegram import (
    is_reply_target_missing_error,
    sanitize_outgoing_text,
    schedule_message_auto_delete,
)

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

log = logging.getLogger(__name__)

TTS_GROUP_MODE_KEY = "tts_mode"
TTS_MODE_OFF = "off"
TTS_MODE_ON = "on"
TTS_MODE_ALWAYS = "always"
_VALID_TTS_MODES = {TTS_MODE_OFF, TTS_MODE_ON, TTS_MODE_ALWAYS}
_SEGMENT_SPLIT_RE = re.compile(r"(?<=[\u3002\uff01\uff1f!?；;…\n])")
_HTML_TAG_RE = re.compile(r"<[^>]+>")
_SEMICOLON_RE = re.compile(r"[；;]+")
_URL_RE = re.compile(r"https?://\S+|www\.\S+", flags=re.IGNORECASE)
_MARKDOWN_LINK_RE = re.compile(r"\[([^\]]+)\]\(([^)]+)\)")
_INLINE_CODE_RE = re.compile(r"`([^`\n]+)`")
_FENCED_CODE_RE = re.compile(r"```(?:[\w+-]+)?\s*([\s\S]*?)```")
_ORDERED_LIST_RE = re.compile(r"(^|[\s，。！？；;：:])(\d+)\s*[\.\)]\s*")
_BULLET_LIST_RE = re.compile(r"(^|\n)\s*[-*•]+\s*")
_SLASH_RE = re.compile(r"\s*[\\/|]+\s*")
_WS_COMMA_RE = re.compile(r"\s*[,，]+\s*")
_WS_COLON_RE = re.compile(r"\s*[:：]+\s*")
_REPEATED_PAUSE_RE = re.compile(r"[，,、]{2,}")
_REPEATED_STOP_RE = re.compile(r"[。\.]{2,}")
_REPEATED_QA_RE = re.compile(r"[!?！？]{2,}")
_MARKDOWN_DECORATION_RE = re.compile(r"[*_~>#]+")
_EMOTION_FALLBACK_CODES = {1014}
_NEWS_KEYWORDS = ("新闻", "快讯", "播报", "报道", "日讯", "通知", "公告", "提醒")
_APOLOGY_KEYWORDS = ("抱歉", "对不起", "不好意思", "失礼", "给您带来不便", "很遗憾")
_COMFORT_KEYWORDS = ("没事", "别怕", "慢慢来", "辛苦了", "抱抱", "晚安", "乖", "别难过")
_HAPPY_KEYWORDS = ("太好了", "开心", "高兴", "好耶", "太棒了", "绝了", "哈哈", "成功了", "喜欢", "赢了")
_ANGER_KEYWORDS = ("气死", "离谱", "无语", "烦死", "警告", "严禁", "禁止", "立刻", "马上", "闭嘴", "住手")
_SUSPENSE_KEYWORDS = ("等等", "不对劲", "奇怪", "悬疑", "诡异", "突然", "怎么回事", "别回头")

def _contains_any(text: str, keywords: tuple[str, ...]) -> bool:
    return any(keyword in text for keyword in keywords)


def normalize_tts_mode(value: Any, *, default: str = TTS_MODE_OFF) -> str:
    raw = value
    if isinstance(value, dict):
        raw = value.get(TTS_GROUP_MODE_KEY)

    text = str(raw or "").strip().lower()
    if text in _VALID_TTS_MODES:
        return text
    if text in {"enable", "enabled", "true", "1"}:
        return TTS_MODE_ON
    if text in {"disable", "disabled", "false", "0"}:
        return TTS_MODE_OFF
    return default


def is_tts_tool_enabled(value: Any) -> bool:
    return normalize_tts_mode(value) in {TTS_MODE_ON, TTS_MODE_ALWAYS}


def is_tts_always_enabled(value: Any) -> bool:
    return normalize_tts_mode(value) == TTS_MODE_ALWAYS


def set_tts_mode(group_settings: dict | None, mode: str) -> dict[str, Any]:
    normalized = normalize_tts_mode(mode)
    settings_data = dict(group_settings or {})
    settings_data[TTS_GROUP_MODE_KEY] = normalized
    return settings_data


async def load_group_tts_mode(
    session: AsyncSession | None,
    group_id: int,
    *,
    default: str = TTS_MODE_OFF,
) -> str:
    if session is None or group_id == 0:
        return default
    row = await session.get(Group, group_id)
    if row is None:
        return default
    return normalize_tts_mode(row.settings, default=default)


def build_tts_status_text(
    *,
    group_id: int,
    group_settings: dict | None,
    service_ready: bool,
) -> str:
    mode = normalize_tts_mode(group_settings)
    mode_label = {
        TTS_MODE_OFF: "关闭",
        TTS_MODE_ON: "开启（主模型可按需调用 TTS skill）",
        TTS_MODE_ALWAYS: "始终使用 TTS 输出",
    }[mode]
    service_label = "已配置" if service_ready else "未配置"
    return (
        "<b>TTS 设置</b>\n"
        f"<b>群ID</b>: {group_id}\n"
        f"<b>模式</b>: {mode_label}\n"
        f"<b>Doubao TTS</b>: {service_label}"
    )


def build_tts_preference_context(
    tts_mode: Any,
    *,
    service_ready: bool,
) -> str:
    mode = normalize_tts_mode(tts_mode)
    if mode == TTS_MODE_OFF or not service_ready:
        return ""

    lines = [
        "[GROUP_TTS_PREFERENCE]",
        f"tts_mode: {mode}",
        "tts_service_ready: yes",
    ]
    if mode == TTS_MODE_ALWAYS:
        lines.extend(
            [
                "This group is configured for voice-first replies.",
                "For normal outgoing replies, prefer calling doubao_tts instead of returning plain text.",
                "If doubao_tts is available and a spoken reply is possible, do not default to text-only output.",
                "If you use doubao_tts, its text must be the exact final spoken reply, with no extra action explanation.",
            ]
        )
        return "\n".join(lines)

    lines.extend(
        [
            "This group has TTS enabled. You have the ability to send voice messages via doubao_tts.",
            "Use your own judgment to decide when voice is more natural and engaging than text.",
            "Prefer voice for: greetings, comfort, goodnights, celebrations, playful banter, emotional reactions, companion-style replies, clingy or cute interactions with the owner, short readings or broadcasts, and any reply where spoken delivery clearly adds warmth or personality.",
            "Keep text for: factual answers, link-heavy or list-heavy replies, management operations, search result summaries, long explanations, and structured content.",
            "When in doubt between voice and text for a short, emotional, or conversational reply, lean toward voice.",
            "If you use doubao_tts, its text must be the exact final spoken reply, with no extra action explanation.",
        ]
    )
    return "\n".join(lines)


@dataclass(slots=True)
class TTSSynthesisResult:
    ok: bool
    text: str = ""
    audio_bytes: bytes = b""
    audio_format: str = "ogg_opus"
    usage: dict[str, Any] = field(default_factory=dict)
    error: str = ""
    logid: str = ""


@dataclass(slots=True)
class TTSStyleDecision:
    emotion: str = ""
    emotion_scale: int | None = None
    speech_rate: int | None = None
    loudness_rate: int | None = None
    context: str = ""


class DoubaoTTSService:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.enabled = bool(getattr(settings, "doubao_tts_enabled", False))
        self.api_base = str(
            getattr(settings, "doubao_tts_api_base", "https://openspeech.bytedance.com")
        ).strip() or "https://openspeech.bytedance.com"
        self.app_id = str(getattr(settings, "doubao_tts_app_id", "") or "").strip()
        self.app_key = str(getattr(settings, "doubao_tts_app_key", "") or "").strip()
        self.access_key = str(getattr(settings, "doubao_tts_access_key", "") or "").strip()
        self.resource_id = str(getattr(settings, "doubao_tts_resource_id", "seed-tts-2.0") or "").strip()
        self.speaker = str(getattr(settings, "doubao_tts_speaker", "") or "").strip()
        self.model = str(getattr(settings, "doubao_tts_model", "") or "").strip()
        self.audio_format = str(getattr(settings, "doubao_tts_audio_format", "ogg_opus") or "ogg_opus").strip()
        self.sample_rate = int(getattr(settings, "doubao_tts_sample_rate", 24000) or 24000)
        self.bit_rate = int(getattr(settings, "doubao_tts_bit_rate", 0) or 0)
        self.emotion = str(getattr(settings, "doubao_tts_emotion", "") or "").strip()
        self.emotion_scale = int(getattr(settings, "doubao_tts_emotion_scale", 4) or 4)
        self.speech_rate = int(getattr(settings, "doubao_tts_speech_rate", 0) or 0)
        self.loudness_rate = int(getattr(settings, "doubao_tts_loudness_rate", 0) or 0)
        self.silence_duration_ms = int(getattr(settings, "doubao_tts_silence_duration_ms", 0) or 0)
        self.http_timeout_sec = float(getattr(settings, "doubao_tts_http_timeout_sec", 20.0) or 20.0)
        self.max_text_length = int(getattr(settings, "doubao_tts_max_text_length", 500) or 500)

    @property
    def available(self) -> bool:
        return bool(
            self.enabled and self.api_base and self.app_id and self.access_key and self.resource_id and self.speaker
        )

    @property
    def api_url(self) -> str:
        return f"{self.api_base.rstrip('/')}/api/v3/tts/unidirectional"

    @property
    def file_extension(self) -> str:
        if self.audio_format == "ogg_opus":
            return ".ogg"
        if self.audio_format == "mp3":
            return ".mp3"
        if self.audio_format == "pcm":
            return ".pcm"
        return ".bin"

    @staticmethod
    def _clamp_emotion_scale(value: int | None) -> int:
        if value is None:
            return 4
        return min(5, max(1, int(value)))

    @staticmethod
    def _clamp_audio_rate(value: int | None) -> int:
        if value is None:
            return 0
        return min(100, max(-50, int(value)))

    def _normalize_context(self, text: str) -> str:
        cleaned = sanitize_outgoing_text(text or "")
        cleaned = html.unescape(_HTML_TAG_RE.sub(" ", cleaned))
        cleaned = _MARKDOWN_LINK_RE.sub(r"\1", cleaned)
        cleaned = _URL_RE.sub("相关内容", cleaned)
        cleaned = _MARKDOWN_DECORATION_RE.sub(" ", cleaned)
        cleaned = re.sub(r"\s+", " ", cleaned)
        cleaned = cleaned.strip(" ，。！？：、")
        return cleaned[:160]

    def _infer_style(self, text: str) -> TTSStyleDecision:
        raw = sanitize_outgoing_text(text or "")
        normalized = self.normalize_text(raw)
        if not normalized:
            return TTSStyleDecision()

        exclaim_count = raw.count("!") + raw.count("！")
        question_count = raw.count("?") + raw.count("？")

        if _contains_any(normalized, _NEWS_KEYWORDS):
            return TTSStyleDecision(
                emotion="calm",
                emotion_scale=2,
                speech_rate=0,
                loudness_rate=0,
                context="用沉稳清晰的播报声，像在做正式新闻或信息播报，吐字清楚，节奏平稳。",
            )
        if _contains_any(normalized, _APOLOGY_KEYWORDS):
            return TTSStyleDecision(
                emotion="sad",
                emotion_scale=4 if exclaim_count else 3,
                speech_rate=-12,
                loudness_rate=-6,
                context="用真诚克制的声音，像在认真道歉并安抚对方情绪，语速稍慢，带着明显歉意。",
            )
        if _contains_any(normalized, _COMFORT_KEYWORDS):
            return TTSStyleDecision(
                emotion="calm",
                emotion_scale=3,
                speech_rate=-18,
                loudness_rate=-8,
                context="用温柔轻声的声音，像在安慰身边的人，语速放慢，语气柔和有陪伴感。",
            )
        if _contains_any(normalized, _ANGER_KEYWORDS):
            return TTSStyleDecision(
                emotion="angry",
                emotion_scale=5 if exclaim_count else 4,
                speech_rate=12,
                loudness_rate=6,
                context="用严肃不满的语气，情绪明确但不要失控，吐字更重，节奏略快，带着警告或指责感。",
            )
        if _contains_any(normalized, _HAPPY_KEYWORDS) or exclaim_count >= 2:
            return TTSStyleDecision(
                emotion="cheerful" if exclaim_count >= 2 else "happy",
                emotion_scale=min(5, max(3, 3 + exclaim_count)),
                speech_rate=12 if exclaim_count else 8,
                loudness_rate=4 if exclaim_count else 2,
                context="用开心明亮的声音，像在分享好消息，语调上扬，节奏轻快，带着明显兴奋感。",
            )
        if _contains_any(normalized, _SUSPENSE_KEYWORDS):
            return TTSStyleDecision(
                emotion="calm",
                emotion_scale=2,
                speech_rate=-10,
                loudness_rate=-3,
                context="用压低嗓音的叙述方式，语速稍慢，带一点紧张和悬念感，像在铺垫故事转折。",
            )
        if question_count and len(normalized) <= 40:
            return TTSStyleDecision(
                emotion="calm",
                emotion_scale=2,
                speech_rate=0,
                loudness_rate=0,
                context="用自然口语的方式发问，尾音轻微上扬，像在和朋友当面确认一件事。",
            )
        return TTSStyleDecision()

    def _resolve_style(
        self,
        text: str,
        *,
        emotion: str = "",
        emotion_scale: int | None = None,
        speech_rate: int | None = None,
        loudness_rate: int | None = None,
        context: str = "",
        use_default_emotion: bool = True,
        auto_style: bool = True,
    ) -> TTSStyleDecision:
        chosen_emotion = str(emotion or "").strip()
        chosen_context = self._normalize_context(context)
        chosen_speech_rate = self.speech_rate if speech_rate is None else int(speech_rate)
        chosen_loudness_rate = self.loudness_rate if loudness_rate is None else int(loudness_rate)
        chosen_emotion_scale = emotion_scale

        if use_default_emotion and not chosen_emotion:
            chosen_emotion = self.emotion

        inferred = TTSStyleDecision()
        if auto_style and not chosen_emotion and not chosen_context:
            inferred = self._infer_style(text)
            if inferred.emotion:
                chosen_emotion = inferred.emotion
            if inferred.context:
                chosen_context = inferred.context
            if chosen_emotion_scale is None:
                chosen_emotion_scale = inferred.emotion_scale
            if speech_rate is None and self.speech_rate == 0 and inferred.speech_rate is not None:
                chosen_speech_rate = inferred.speech_rate
            if loudness_rate is None and self.loudness_rate == 0 and inferred.loudness_rate is not None:
                chosen_loudness_rate = inferred.loudness_rate

        if chosen_emotion and chosen_emotion_scale is None:
            chosen_emotion_scale = self.emotion_scale

        return TTSStyleDecision(
            emotion=chosen_emotion,
            emotion_scale=self._clamp_emotion_scale(chosen_emotion_scale) if chosen_emotion else None,
            speech_rate=self._clamp_audio_rate(chosen_speech_rate),
            loudness_rate=self._clamp_audio_rate(chosen_loudness_rate),
            context=chosen_context,
        )

    @staticmethod
    def _should_retry_without_emotion(*, code: int, message: str, style: TTSStyleDecision) -> bool:
        if not style.emotion:
            return False
        lowered = (message or "").lower()
        return code in _EMOTION_FALLBACK_CODES or "emotion" in lowered or "情感" in message

    def normalize_text(self, text: str) -> str:
        cleaned = sanitize_outgoing_text(text or "")
        cleaned = html.unescape(_HTML_TAG_RE.sub(" ", cleaned))
        cleaned = _MARKDOWN_LINK_RE.sub(r"\1", cleaned)
        cleaned = _FENCED_CODE_RE.sub(lambda match: f" {match.group(1).strip()} ", cleaned)
        cleaned = _INLINE_CODE_RE.sub(r"\1", cleaned)
        cleaned = _URL_RE.sub(" 链接 ", cleaned)
        cleaned = _BULLET_LIST_RE.sub(lambda match: f"{match.group(1)}，", cleaned)
        cleaned = _ORDERED_LIST_RE.sub(lambda match: f"{match.group(1)}{match.group(2)}、", cleaned)
        cleaned = _MARKDOWN_DECORATION_RE.sub(" ", cleaned)

        replacements = {
            "；": "，",
            ";": "，",
            ":": "：",
            "\n": "，",
            "\r": "，",
            "\t": "，",
            "&": "和",
            "+": "加",
            "=": "等于",
            "~": "，",
            "…": "，",
        }
        for src, target in replacements.items():
            cleaned = cleaned.replace(src, target)

        cleaned = _SEMICOLON_RE.sub("，", cleaned)
        cleaned = _SLASH_RE.sub("，", cleaned)
        cleaned = _WS_COLON_RE.sub("：", cleaned)
        cleaned = _WS_COMMA_RE.sub("，", cleaned)
        cleaned = re.sub(r"\s+", " ", cleaned)
        cleaned = _REPEATED_PAUSE_RE.sub("，", cleaned)
        cleaned = _REPEATED_STOP_RE.sub("。", cleaned)
        cleaned = _REPEATED_QA_RE.sub("。", cleaned)
        cleaned = re.sub(r"\s*([，。！？：、])\s*", r"\1", cleaned)
        cleaned = cleaned.strip(" ，。！？：、")
        return cleaned

    def split_text(self, text: str) -> list[str]:
        normalized = self.normalize_text(text)
        if not normalized:
            return []
        if len(normalized) <= self.max_text_length:
            return [normalized]

        segments: list[str] = []
        buffer = ""
        for chunk in _SEGMENT_SPLIT_RE.split(normalized):
            piece = chunk.strip()
            if not piece:
                continue
            if len(piece) > self.max_text_length:
                if buffer:
                    segments.append(buffer)
                    buffer = ""
                cursor = 0
                while cursor < len(piece):
                    end = min(cursor + self.max_text_length, len(piece))
                    split_at = piece.rfind(" ", cursor, end)
                    if split_at <= cursor:
                        split_at = end
                    else:
                        split_at += 1
                    segment = piece[cursor:split_at].strip()
                    if segment:
                        segments.append(segment)
                    cursor = split_at
                continue

            candidate = f"{buffer} {piece}".strip() if buffer else piece
            if len(candidate) <= self.max_text_length:
                buffer = candidate
            else:
                if buffer:
                    segments.append(buffer)
                buffer = piece

        if buffer:
            segments.append(buffer)
        return segments

    def _build_headers(self, request_id: str) -> dict[str, str]:
        headers = {
            "Content-Type": "application/json",
            "X-Api-App-Id": self.app_id,
            "X-Api-Access-Key": self.access_key,
            "X-Api-Resource-Id": self.resource_id,
            "X-Api-Request-Id": request_id,
            "X-Control-Require-Usage-Tokens-Return": "text_words",
        }
        if self.app_key:
            headers["X-Api-App-Key"] = self.app_key
        return headers

    def _build_payload(
        self,
        text: str,
        *,
        uid: str,
        emotion: str = "",
        emotion_scale: int | None = None,
        speech_rate: int | None = None,
        loudness_rate: int | None = None,
        context: str = "",
        audio_format: str | None = None,
        sample_rate: int | None = None,
        bit_rate: int | None = None,
        use_default_emotion: bool = True,
        auto_style: bool = True,
    ) -> dict[str, Any]:
        style = self._resolve_style(
            text,
            emotion=emotion,
            emotion_scale=emotion_scale,
            speech_rate=speech_rate,
            loudness_rate=loudness_rate,
            context=context,
            use_default_emotion=use_default_emotion,
            auto_style=auto_style,
        )
        actual_format = (audio_format or self.audio_format).strip() or self.audio_format
        actual_sample_rate = int(sample_rate or self.sample_rate or 24000)
        actual_bit_rate = int(bit_rate or self.bit_rate or 0)
        audio_params: dict[str, Any] = {
            "format": actual_format,
            "sample_rate": actual_sample_rate,
        }
        if actual_bit_rate > 0:
            audio_params["bit_rate"] = actual_bit_rate

        if style.emotion:
            audio_params["emotion"] = style.emotion
            if style.emotion_scale is not None:
                audio_params["emotion_scale"] = style.emotion_scale

        if style.speech_rate:
            audio_params["speech_rate"] = style.speech_rate

        if style.loudness_rate:
            audio_params["loudness_rate"] = style.loudness_rate

        additions: dict[str, Any] = {
            "disable_markdown_filter": True,
            "cache_config": {"text_type": 1, "use_cache": True},
        }
        if self.silence_duration_ms > 0:
            additions["silence_duration"] = self.silence_duration_ms
        if style.context:
            additions["context_texts"] = [style.context]

        req_params: dict[str, Any] = {
            "text": text,
            "speaker": self.speaker,
            "audio_params": audio_params,
            # Volcengine expects req_params.additions as a JSON string, not an object.
            "additions": json.dumps(additions, ensure_ascii=False, separators=(",", ":")),
        }
        if self.model:
            req_params["model"] = self.model

        return {
            "user": {"uid": uid or self.app_id or "smart-group-bot"},
            "namespace": "UnidirectionalTTS",
            "req_params": req_params,
        }

    @staticmethod
    async def _iter_json_objects(resp: aiohttp.ClientResponse) -> list[dict[str, Any]]:
        decoder = json.JSONDecoder()
        buffer = ""
        items: list[dict[str, Any]] = []

        async for raw in resp.content.iter_any():
            if not raw:
                continue
            buffer += raw.decode("utf-8", errors="ignore")
            while True:
                stripped = buffer.lstrip()
                if not stripped:
                    buffer = ""
                    break
                if stripped != buffer:
                    buffer = stripped
                try:
                    parsed, end_idx = decoder.raw_decode(buffer)
                except json.JSONDecodeError:
                    break
                if isinstance(parsed, dict):
                    items.append(parsed)
                buffer = buffer[end_idx:]

        stripped = buffer.lstrip()
        if stripped:
            try:
                parsed = json.loads(stripped)
            except json.JSONDecodeError:
                log.debug("tts stream tail ignored: %s", stripped[:160])
            else:
                if isinstance(parsed, dict):
                    items.append(parsed)
        return items

    async def synthesize(
        self,
        text: str,
        *,
        uid: str = "",
        emotion: str = "",
        emotion_scale: int | None = None,
        speech_rate: int | None = None,
        loudness_rate: int | None = None,
        context: str = "",
        audio_format: str | None = None,
        sample_rate: int | None = None,
        bit_rate: int | None = None,
        _use_default_emotion: bool = True,
        _auto_style: bool = True,
    ) -> TTSSynthesisResult:
        if not self.available:
            return TTSSynthesisResult(ok=False, error="tts_not_configured")

        normalized = self.normalize_text(text)
        if not normalized:
            return TTSSynthesisResult(ok=False, error="empty_text")
        if len(normalized) > self.max_text_length:
            return TTSSynthesisResult(ok=False, error="text_too_long", text=normalized)

        style = self._resolve_style(
            normalized,
            emotion=emotion,
            emotion_scale=emotion_scale,
            speech_rate=speech_rate,
            loudness_rate=loudness_rate,
            context=context,
            use_default_emotion=_use_default_emotion,
            auto_style=_auto_style,
        )

        request_id = str(uuid.uuid4())
        timeout = aiohttp.ClientTimeout(total=max(5.0, self.http_timeout_sec))
        try:
            async with aiohttp.ClientSession(timeout=timeout) as client:
                async with client.post(
                    self.api_url,
                    headers=self._build_headers(request_id),
                    json=self._build_payload(
                        normalized,
                        uid=uid,
                        emotion=style.emotion,
                        emotion_scale=style.emotion_scale,
                        speech_rate=style.speech_rate,
                        loudness_rate=style.loudness_rate,
                        context=style.context,
                        audio_format=audio_format,
                        sample_rate=sample_rate,
                        bit_rate=bit_rate,
                        use_default_emotion=False,
                        auto_style=False,
                    ),
                ) as resp:
                    logid = str(resp.headers.get("X-Tt-Logid", "") or "")
                    if resp.status >= 400:
                        body = (await resp.text()).strip()
                        log.warning("doubao tts http error status=%s body=%s", resp.status, body[:200])
                        return TTSSynthesisResult(
                            ok=False,
                            text=normalized,
                            error=f"http_{resp.status}",
                            logid=logid,
                        )

                    audio_parts: list[bytes] = []
                    usage: dict[str, Any] = {}
                    finished = False
                    for item in await self._iter_json_objects(resp):
                        code = int(item.get("code", 0) or 0)
                        message = str(item.get("message", "") or "").strip()
                        if code == 0:
                            data = item.get("data")
                            if isinstance(data, str) and data:
                                try:
                                    audio_parts.append(base64.b64decode(data))
                                except Exception:
                                    log.warning("doubao tts audio chunk decode failed | logid=%s", logid)
                            continue
                        if code == 20000000:
                            raw_usage = item.get("usage")
                            if isinstance(raw_usage, dict):
                                usage = raw_usage
                            finished = True
                            break
                        if self._should_retry_without_emotion(code=code, message=message, style=style):
                            log.info(
                                "doubao tts emotion unsupported, retrying without emotion | emotion=%s logid=%s",
                                style.emotion,
                                logid,
                            )
                            return await self.synthesize(
                                normalized,
                                uid=uid,
                                emotion="",
                                emotion_scale=None,
                                speech_rate=style.speech_rate,
                                loudness_rate=style.loudness_rate,
                                context=style.context,
                                audio_format=audio_format,
                                sample_rate=sample_rate,
                                bit_rate=bit_rate,
                                _use_default_emotion=False,
                                _auto_style=False,
                            )
                        log.warning("doubao tts failed code=%s message=%s logid=%s", code, message, logid)
                        return TTSSynthesisResult(
                            ok=False,
                            text=normalized,
                            error=message or f"code_{code}",
                            logid=logid,
                        )

                    if not audio_parts:
                        return TTSSynthesisResult(
                            ok=False,
                            text=normalized,
                            error="empty_audio",
                            logid=logid,
                        )
                    if not finished:
                        log.warning("doubao tts stream ended before session finish | logid=%s", logid)
                        return TTSSynthesisResult(
                            ok=False,
                            text=normalized,
                            error="incomplete_stream",
                            logid=logid,
                        )
                    return TTSSynthesisResult(
                        ok=True,
                        text=normalized,
                        audio_bytes=b"".join(audio_parts),
                        audio_format=(audio_format or self.audio_format or "").strip() or self.audio_format,
                        usage=usage,
                        logid=logid,
                    )
        except Exception as exc:
            log.exception("doubao tts request failed")
            return TTSSynthesisResult(ok=False, text=normalized, error=str(exc))

    @staticmethod
    def _convert_mp3_to_ogg_opus(mp3_bytes: bytes) -> bytes:
        src_fd, src_path = tempfile.mkstemp(suffix=".mp3")
        dst_fd, dst_path = tempfile.mkstemp(suffix=".ogg")
        os.close(src_fd)
        os.close(dst_fd)
        try:
            with open(src_path, "wb") as f:
                f.write(mp3_bytes)
            cmd = [
                "ffmpeg",
                "-y",
                "-i",
                src_path,
                "-c:a",
                "libopus",
                "-b:a",
                "96k",
                "-vbr",
                "on",
                "-application",
                "voip",
                dst_path,
            ]
            proc = subprocess.run(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
                timeout=30,
            )
            if proc.returncode != 0:
                err = proc.stderr.decode("utf-8", errors="ignore")
                raise RuntimeError(f"ffmpeg_failed:{proc.returncode}:{err[:200]}")
            with open(dst_path, "rb") as f:
                return f.read()
        finally:
            for path in (src_path, dst_path):
                try:
                    if os.path.exists(path):
                        os.remove(path)
                except Exception:
                    pass

    async def synthesize_voice_payload(
        self,
        text: str,
        *,
        uid: str = "",
        emotion: str = "",
        emotion_scale: int | None = None,
        speech_rate: int | None = None,
        loudness_rate: int | None = None,
        context: str = "",
    ) -> TTSSynthesisResult:
        if self.audio_format != "ogg_opus":
            return await self.synthesize(
                text,
                uid=uid,
                emotion=emotion,
                emotion_scale=emotion_scale,
                speech_rate=speech_rate,
                loudness_rate=loudness_rate,
                context=context,
            )

        source_bitrate = max(128000, int(self.bit_rate or 0))
        mp3_result = await self.synthesize(
            text,
            uid=uid,
            emotion=emotion,
            emotion_scale=emotion_scale,
            speech_rate=speech_rate,
            loudness_rate=loudness_rate,
            context=context,
            audio_format="mp3",
            sample_rate=max(44100, int(self.sample_rate or 44100)),
            bit_rate=source_bitrate,
        )
        if not mp3_result.ok or not mp3_result.audio_bytes:
            return mp3_result

        try:
            ogg_bytes = await asyncio.to_thread(self._convert_mp3_to_ogg_opus, mp3_result.audio_bytes)
        except Exception as exc:
            log.exception("tts ffmpeg transcode failed")
            return TTSSynthesisResult(
                ok=False,
                text=mp3_result.text,
                error=str(exc),
                logid=mp3_result.logid,
            )
        return TTSSynthesisResult(
            ok=True,
            text=mp3_result.text,
            audio_bytes=ogg_bytes,
            audio_format="ogg_opus",
            usage=mp3_result.usage,
            logid=mp3_result.logid,
        )

    async def _send_to_message(
        self,
        message: Message,
        *,
        audio_bytes: bytes,
        index: int,
        delivery_mode: str,
        reply_to_message_id: int | None,
        auto_delete_minutes: int,
        caption: str = "",
        parse_mode: str | None = None,
    ) -> bool:
        filename = f"tts_{index + 1}{self.file_extension}"
        send_as_reply = (delivery_mode or "reply").strip().lower() != "message"
        requested_reply_target = int(reply_to_message_id or 0) or None
        current_reply_target = requested_reply_target
        attempt = 0
        while attempt <= 2:
            try:
                file_obj = BufferedInputFile(audio_bytes, filename=filename)
                if self.audio_format == "ogg_opus":
                    if send_as_reply:
                        if current_reply_target:
                            sent = await message.bot.send_voice(
                                chat_id=message.chat.id,
                                voice=file_obj,
                                reply_to_message_id=current_reply_target,
                                caption=caption or None,
                                parse_mode=parse_mode,
                            )
                        elif requested_reply_target:
                            sent = await message.answer_voice(
                                voice=file_obj,
                                caption=caption or None,
                                parse_mode=parse_mode,
                            )
                        else:
                            sent = await message.reply_voice(
                                voice=file_obj,
                                caption=caption or None,
                                parse_mode=parse_mode,
                            )
                    else:
                        sent = await message.answer_voice(
                            voice=file_obj,
                            caption=caption or None,
                            parse_mode=parse_mode,
                        )
                else:
                    if send_as_reply:
                        if current_reply_target:
                            sent = await message.bot.send_audio(
                                chat_id=message.chat.id,
                                audio=file_obj,
                                reply_to_message_id=current_reply_target,
                                caption=caption or None,
                                parse_mode=parse_mode,
                                title="Doubao TTS",
                            )
                        elif requested_reply_target:
                            sent = await message.answer_audio(
                                audio=file_obj,
                                caption=caption or None,
                                parse_mode=parse_mode,
                                title="Doubao TTS",
                            )
                        else:
                            sent = await message.reply_audio(
                                audio=file_obj,
                                caption=caption or None,
                                parse_mode=parse_mode,
                                title="Doubao TTS",
                            )
                    else:
                        sent = await message.answer_audio(
                            audio=file_obj,
                            caption=caption or None,
                            parse_mode=parse_mode,
                            title="Doubao TTS",
                        )
                schedule_message_auto_delete(sent, auto_delete_minutes)
                return True
            except TelegramRetryAfter as exc:
                wait_s = max(0.5, float(getattr(exc, "retry_after", 1.0))) + 0.2
                await asyncio.sleep(wait_s)
                attempt += 1
            except TelegramBadRequest as exc:
                detail = str(exc).lower()
                if send_as_reply and current_reply_target and is_reply_target_missing_error(detail):
                    current_reply_target = None
                    attempt += 1
                    continue
                log.warning("tts telegram send failed: %s", exc)
                return False
            except Exception:
                if attempt >= 2:
                    log.exception("tts telegram send failed")
                    return False
                attempt += 1
                await asyncio.sleep(0.6 * attempt)
        return False

    async def send_message_tts(
        self,
        message: Message,
        text: str,
        *,
        delivery_mode: str = "reply",
        reply_to_message_id: int | None = None,
        auto_delete_minutes: int = 0,
        uid: str = "",
        emotion: str = "",
        emotion_scale: int | None = None,
        speech_rate: int | None = None,
        loudness_rate: int | None = None,
        context: str = "",
    ) -> bool:
        if not self.available:
            return False

        segments = self.split_text(text)
        if not segments:
            return False

        for idx, segment in enumerate(segments):
            if self.audio_format == "ogg_opus":
                result = await self.synthesize_voice_payload(
                    segment,
                    uid=uid,
                    emotion=emotion,
                    emotion_scale=emotion_scale,
                    speech_rate=speech_rate,
                    loudness_rate=loudness_rate,
                    context=context,
                )
            else:
                result = await self.synthesize(
                    segment,
                    uid=uid,
                    emotion=emotion,
                    emotion_scale=emotion_scale,
                    speech_rate=speech_rate,
                    loudness_rate=loudness_rate,
                    context=context,
                )
            if not result.ok or not result.audio_bytes:
                return False
            ok = await self._send_to_message(
                message,
                audio_bytes=result.audio_bytes,
                index=idx,
                delivery_mode=delivery_mode,
                reply_to_message_id=reply_to_message_id if idx == 0 else None,
                auto_delete_minutes=auto_delete_minutes,
            )
            if not ok:
                return False
        return True

    async def send_chat_tts(
        self,
        bot: Bot,
        chat_id: int,
        text: str,
        *,
        reply_to_message_id: int | None = None,
        fallback_mention_user_id: int = 0,
        fallback_mention_name: str = "",
        auto_delete_minutes: int = 0,
        uid: str = "",
        emotion: str = "",
        emotion_scale: int | None = None,
        speech_rate: int | None = None,
        loudness_rate: int | None = None,
        context: str = "",
    ) -> bool:
        if not self.available:
            return False

        segments = self.split_text(text)
        if not segments:
            return False

        fallback_caption_html = ""
        if fallback_mention_user_id:
            shown = html.escape((fallback_mention_name or str(fallback_mention_user_id)).strip())
            fallback_caption_html = f'<a href="tg://user?id={fallback_mention_user_id}">@{shown}</a>'

        for idx, segment in enumerate(segments):
            if self.audio_format == "ogg_opus":
                result = await self.synthesize_voice_payload(
                    segment,
                    uid=uid,
                    emotion=emotion,
                    emotion_scale=emotion_scale,
                    speech_rate=speech_rate,
                    loudness_rate=loudness_rate,
                    context=context,
                )
            else:
                result = await self.synthesize(
                    segment,
                    uid=uid,
                    emotion=emotion,
                    emotion_scale=emotion_scale,
                    speech_rate=speech_rate,
                    loudness_rate=loudness_rate,
                    context=context,
                )
            if not result.ok or not result.audio_bytes:
                return False

            attempt = 0
            current_reply_to = reply_to_message_id if idx == 0 else None
            current_caption = ""
            current_parse_mode: str | None = None
            while attempt <= 2:
                try:
                    file_obj = BufferedInputFile(
                        result.audio_bytes,
                        filename=f"tts_{idx + 1}{self.file_extension}",
                    )
                    if self.audio_format == "ogg_opus":
                        sent = await bot.send_voice(
                            chat_id=chat_id,
                            voice=file_obj,
                            reply_to_message_id=current_reply_to,
                            caption=current_caption or None,
                            parse_mode=current_parse_mode,
                        )
                    else:
                        sent = await bot.send_audio(
                            chat_id=chat_id,
                            audio=file_obj,
                            reply_to_message_id=current_reply_to,
                            caption=current_caption or None,
                            parse_mode=current_parse_mode,
                            title="Doubao TTS",
                        )
                    schedule_message_auto_delete(sent, auto_delete_minutes)
                    break
                except TelegramRetryAfter as exc:
                    wait_s = max(0.5, float(getattr(exc, "retry_after", 1.0))) + 0.2
                    await asyncio.sleep(wait_s)
                    attempt += 1
                except TelegramBadRequest as exc:
                    detail = str(exc).lower()
                    if current_reply_to and is_reply_target_missing_error(detail):
                        current_reply_to = None
                        if idx == 0 and fallback_caption_html:
                            current_caption = fallback_caption_html
                            current_parse_mode = "HTML"
                        attempt += 1
                        continue
                    log.warning("tts bot send failed chat_id=%s error=%s", chat_id, exc)
                    return False
                except Exception:
                    if attempt >= 2:
                        log.exception("tts bot send failed chat_id=%s", chat_id)
                        return False
                    attempt += 1
                    await asyncio.sleep(0.6 * attempt)
            else:
                return False
        return True
