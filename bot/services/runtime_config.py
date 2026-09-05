from __future__ import annotations

import asyncio
import base64
import hashlib
import inspect
import json
import logging
import os
import re
import tomllib
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any, Literal

from cryptography.fernet import Fernet, InvalidToken
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from bot.config import (
    EmbedConfig,
    ModelConfig,
    ModerationConfig,
    ProviderProfile,
    Settings,
    _build_chat_config,
    _build_embed_config,
    _build_litellm_model,
    _collect_provider_profiles,
    _load_raw_env,
    _parse_fallbacks,
    _resolve_provider_profile,
)
from bot.db.models import RuntimeConfigRecord, RuntimeConfigSecret
from bot.utils.prompts import load_prompt_defaults, set_runtime_prompts

log = logging.getLogger(__name__)

CONFIG_SCHEMA_VERSION = 1
_STATIC_SECRET_PATHS = (
    "verification.turnstile_secret_key",
    "verification.hcaptcha_secret_key",
    "tts.app_key",
    "tts.access_key",
    "movie_info.tmdb_read_access_token",
    "movie_info.imdb_api_key",
    "movie_info.imdb_aws_access_key_id",
    "movie_info.imdb_aws_secret_access_key",
    "movie_info.imdb_aws_session_token",
)


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ProviderConfig(StrictModel):
    name: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")
    provider: str = Field(default="gemini", min_length=1, max_length=64)
    api_key: str = ""
    api_base: str = ""
    stream: bool = False
    chat_endpoint: Literal["auto", "chat_completions", "responses"] = "auto"

    @field_validator("name", "provider", mode="before")
    @classmethod
    def _clean_identifier(cls, value: object) -> str:
        return str(value or "").strip().lower()

    @field_validator("api_key", "api_base", mode="before")
    @classmethod
    def _clean_text(cls, value: object) -> str:
        return str(value or "").strip()


class ModelFallbackConfig(StrictModel):
    provider: str = Field(min_length=1, max_length=64)
    model: str = Field(default="", max_length=255)
    # Provider-specific JSON for this concrete fallback model only.
    request_params: dict[str, Any] = Field(default_factory=dict)

    @field_validator("provider", mode="before")
    @classmethod
    def _clean_provider(cls, value: object) -> str:
        return str(value or "").strip().lower()

    @field_validator("model", mode="before")
    @classmethod
    def _clean_model(cls, value: object) -> str:
        return str(value or "").strip()

    @field_validator("request_params", mode="before")
    @classmethod
    def _clean_request_params(cls, value: object) -> dict[str, Any]:
        return _normalize_request_params(value)


_REQUEST_PARAMS_MAX_BYTES = 64 * 1024


def _normalize_request_params(value: object) -> dict[str, Any]:
    """Validate and copy one provider-specific JSON object.

    Runtime settings arrive from an untrusted Mini App request.  Normalizing
    through the stdlib JSON codec guarantees that values are JSON-compatible
    before they are persisted and bounds the size of one parameter object.
    """
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ValueError("request_params 必须是 JSON 对象")
    try:
        encoded = json.dumps(value, ensure_ascii=False, allow_nan=False)
        normalized = json.loads(encoded)
    except (TypeError, ValueError, RecursionError) as exc:
        raise ValueError("request_params 必须只包含 JSON 可序列化值") from exc
    if len(encoded.encode("utf-8")) > _REQUEST_PARAMS_MAX_BYTES:
        raise ValueError("request_params 不能超过 64 KiB")
    return normalized


class ChatRoleConfig(StrictModel):
    provider: str = ""
    model: str = ""
    fallbacks: list[ModelFallbackConfig] = Field(default_factory=list)
    temperature: float = Field(default=0.7, ge=0.0, le=2.0, allow_inf_nan=False)
    max_tokens: int = Field(default=2048, ge=1, le=2_000_000)
    timeout_sec: float = Field(default=12.0, ge=1.0, le=600.0, allow_inf_nan=False)
    # 单次调用（含同模型重试与整条回退链）的总时限；0 = 使用内置默认。
    total_deadline_sec: float = Field(
        default=0.0, ge=0.0, le=3600.0, allow_inf_nan=False
    )
    # Provider-specific JSON for this role's concrete primary model only.
    request_params: dict[str, Any] = Field(default_factory=dict)

    @field_validator("provider", mode="before")
    @classmethod
    def _clean_provider(cls, value: object) -> str:
        return str(value or "").strip().lower()

    @field_validator("model", mode="before")
    @classmethod
    def _clean_model(cls, value: object) -> str:
        return str(value or "").strip()

    @field_validator("request_params", mode="before")
    @classmethod
    def _clean_request_params(cls, value: object) -> dict[str, Any]:
        return _normalize_request_params(value)


class EmbedRoleConfig(StrictModel):
    provider: str = ""
    model: str = Field(default="text-embedding-004", min_length=1, max_length=255)
    fallbacks: list[ModelFallbackConfig] = Field(default_factory=list)
    timeout_sec: float = Field(default=10.0, ge=1.0, le=600.0, allow_inf_nan=False)
    # 单次调用（含同模型重试与整条回退链）的总时限；0 = 使用内置默认。
    total_deadline_sec: float = Field(
        default=0.0, ge=0.0, le=3600.0, allow_inf_nan=False
    )
    # Provider-specific JSON for this role's concrete embedding model only.
    request_params: dict[str, Any] = Field(default_factory=dict)

    @field_validator("provider", mode="before")
    @classmethod
    def _clean_provider(cls, value: object) -> str:
        return str(value or "").strip().lower()

    @field_validator("model", mode="before")
    @classmethod
    def _clean_model(cls, value: object) -> str:
        return str(value or "").strip()

    @field_validator("request_params", mode="before")
    @classmethod
    def _clean_request_params(cls, value: object) -> dict[str, Any]:
        return _normalize_request_params(value)


class ModelSettingsConfig(StrictModel):
    providers: list[ProviderConfig] = Field(
        default_factory=lambda: [ProviderConfig(name="gemini", provider="gemini")]
    )
    main: ChatRoleConfig = Field(
        default_factory=lambda: ChatRoleConfig(
            provider="gemini",
            model="gemini-2.0-flash",
            temperature=0.7,
            max_tokens=2048,
            timeout_sec=12.0,
        )
    )
    vision: ChatRoleConfig = Field(
        default_factory=lambda: ChatRoleConfig(
            temperature=0.7,
            max_tokens=2048,
            timeout_sec=15.0,
        )
    )
    decision: ChatRoleConfig = Field(
        default_factory=lambda: ChatRoleConfig(
            temperature=0.1,
            max_tokens=512,
            timeout_sec=6.0,
        )
    )
    moderation: ChatRoleConfig = Field(
        default_factory=lambda: ChatRoleConfig(
            temperature=0.1,
            max_tokens=1024,
            timeout_sec=8.0,
        )
    )
    compress: ChatRoleConfig = Field(
        default_factory=lambda: ChatRoleConfig(
            temperature=0.3,
            max_tokens=1024,
            timeout_sec=12.0,
        )
    )
    embed: EmbedRoleConfig = Field(default_factory=EmbedRoleConfig)
    retry_attempts: int = Field(default=2, ge=1, le=10)
    retry_backoff_sec: float = Field(default=0.8, ge=0.0, le=60.0, allow_inf_nan=False)
    retry_timeout_multiplier: float = Field(default=1.35, ge=1.0, le=10.0, allow_inf_nan=False)


class BotBehaviorConfig(StrictModel):
    model_config = ConfigDict(extra="forbid")

    parse_mode: str = Field(default="HTML", max_length=32)
    disable_link_preview: bool = True
    # Deprecated compatibility field. Automatic lifecycle operations must
    # never discard Telegram's backlog; keep accepting old payloads while
    # normalizing every read/write to the safe value.
    drop_pending_updates: bool = False
    inbound_debounce_seconds: float = Field(default=5.0, ge=0.0, le=60.0)
    reply_batch_timeout_seconds: float = Field(default=45.0, ge=5.0, le=120.0)
    enable_typing: bool = True
    enable_streaming: bool = True
    stream_chunk_size: int = Field(default=36, ge=8, le=4096)
    stream_edit_interval_sec: float = Field(default=1.0, ge=0.3, le=30.0)
    auto_delete_seconds: int = Field(default=0, ge=0, le=604800)
    auto_delete_categories: list[
        Literal[
            "reply",
            "management",
            "moderation",
            "media",
            "proactive",
            "keyword",
            "scheduled",
            "welcome",
            "call_admin",
            "vote",
        ]
    ] = Field(default_factory=lambda: ["management", "moderation"])
    # Per-category retention overrides (seconds); 0 or missing inherits
    # auto_delete_seconds.
    auto_delete_category_seconds: dict[
        Literal[
            "reply",
            "management",
            "moderation",
            "media",
            "proactive",
            "keyword",
            "scheduled",
            "welcome",
            "call_admin",
            "vote",
        ],
        int,
    ] = Field(default_factory=dict)
    # Per-category cleanup mode: "timer" (default, delayed delete) or
    # "button" (inline delete button; mutually exclusive with the timer).
    # Only "button" entries are persisted.
    auto_delete_category_mode: dict[
        Literal[
            "reply",
            "management",
            "moderation",
            "media",
            "proactive",
            "keyword",
            "scheduled",
            "welcome",
            "call_admin",
            "vote",
        ],
        Literal["timer", "button"],
    ] = Field(default_factory=dict)
    # Accepted only while reading records written before the seconds migration.
    auto_delete_minutes: int | None = Field(default=None, ge=0, le=10080, exclude=True)
    decision_context_items: int = Field(default=5, ge=0, le=20)
    max_context_tokens: int = Field(default=256000, ge=1024, le=2_000_000)
    max_output_tokens: int = Field(default=2048, ge=256, le=2_000_000)
    memory_recent_messages: int = Field(default=500, ge=50, le=2000)
    memory_retention_days: int = Field(default=7, ge=1, le=365)
    memory_archive_max_messages_per_group: int = Field(
        default=50000,
        ge=1000,
        le=1_000_000,
    )
    memory_recall_enabled: bool = True
    memory_recall_max_results: int = Field(default=8, ge=1, le=20)
    memory_automatic_compaction: bool = False
    proactive_default_enabled: bool = False
    proactive_idle_minutes: int = Field(default=180, ge=180, le=43200)
    proactive_jitter_minutes: int = Field(default=60, ge=0, le=1440)
    proactive_check_interval_seconds: float = Field(default=60.0, ge=15.0, le=3600.0)
    proactive_quiet_hours_start: int = Field(default=0, ge=0, le=23)
    proactive_quiet_hours_end: int = Field(default=9, ge=0, le=23)
    proactive_retry_minutes: int = Field(default=30, ge=5, le=1440)

    @field_validator("drop_pending_updates", mode="before")
    @classmethod
    def _preserve_pending_updates(cls, _value: object) -> bool:
        return False

    @model_validator(mode="after")
    def _migrate_auto_delete_minutes(self) -> BotBehaviorConfig:
        if self.auto_delete_seconds <= 0 and self.auto_delete_minutes:
            self.auto_delete_seconds = int(self.auto_delete_minutes) * 60
        self.auto_delete_categories = list(dict.fromkeys(self.auto_delete_categories))
        # Zero means "inherit the global seconds": store only real overrides.
        cleaned: dict[str, int] = {}
        for category, seconds in self.auto_delete_category_seconds.items():
            value = int(seconds)
            if value < 0 or value > 604800:
                raise ValueError("分类自动删除时间必须在 0-604800 秒之间")
            if value > 0:
                cleaned[category] = value
        self.auto_delete_category_seconds = cleaned
        # "timer" is the default: store only "button" entries.
        self.auto_delete_category_mode = {
            category: mode
            for category, mode in self.auto_delete_category_mode.items()
            if mode == "button"
        }
        return self


class ModerationSettingsConfig(StrictModel):
    enabled: bool = True
    warn_threshold: int = Field(default=3, ge=1, le=100)
    high_confidence_threshold: float = Field(default=0.9, ge=0.0, le=1.0, allow_inf_nan=False)
    challenge_timeout_seconds: int = Field(default=600, ge=60, le=86400)
    bot_screening_enabled: bool = True
    bot_screening_message_count: int = Field(default=5, ge=1, le=100)


class PatrolSettingsConfig(StrictModel):
    """Daily profile patrol over the known-member roster."""

    enabled: bool = False
    schedule_time: str = Field(default="04:30", max_length=5)
    batch_size: int = Field(default=500, ge=10, le=5000)
    batch_pause_seconds: float = Field(default=5.0, ge=0.0, le=600.0, allow_inf_nan=False)
    fetch_bio: bool = True
    challenge_timeout_seconds: int = Field(default=600, ge=60, le=86400)
    check_interval_seconds: float = Field(default=60.0, ge=15.0, le=3600.0, allow_inf_nan=False)

    @field_validator("schedule_time", mode="before")
    @classmethod
    def _validate_schedule_time(cls, value: object) -> str:
        text = str(value or "").strip()
        parts = text.split(":")
        if len(parts) == 2:
            try:
                hour, minute = int(parts[0]), int(parts[1])
            except ValueError:
                hour = minute = -1
            if 0 <= hour <= 23 and 0 <= minute <= 59:
                return f"{hour:02d}:{minute:02d}"
        raise ValueError("巡检时间必须是 HH:MM 格式（例如 04:30）")


class RaidGuardSettingsConfig(StrictModel):
    """Join-flood lockdown with retroactive human challenges."""

    enabled: bool = False
    pin_message: bool = True
    join_threshold: int = Field(default=8, ge=2, le=1000)
    window_seconds: int = Field(default=60, ge=5, le=3600)
    lockdown_seconds: int = Field(default=600, ge=60, le=86400)
    lookback_seconds: int = Field(default=300, ge=0, le=86400)
    challenge_timeout_seconds: int = Field(default=600, ge=60, le=86400)


class CallAdminSettingsConfig(StrictModel):
    """@admin trigger: report a situation by pinging the group's admins."""

    enabled: bool = True
    pin_message: bool = False
    cooldown_seconds: int = Field(default=60, ge=0, le=86400)


class VoteBanSettingsConfig(StrictModel):
    """Democratic vote-ban defaults; groups may override in the Mini App."""

    enabled: bool = False
    pin_message: bool = True
    vote_threshold: int = Field(default=5, ge=2, le=1000)
    duration_seconds: int = Field(default=1800, ge=60, le=86400)
    trigger_limit: int = Field(default=3, ge=1, le=1000)
    trigger_window_seconds: int = Field(default=3600, ge=60, le=604800)


class VerificationSettingsConfig(StrictModel):
    enabled: bool = False
    timeout_seconds: int = Field(default=600, ge=60, le=86400)
    check_interval_seconds: float = Field(default=30.0, ge=5.0, le=3600.0)
    provider: Literal["turnstile", "hcaptcha", "turnstile_hcaptcha"] = "turnstile"
    turnstile_site_key: str = Field(default="", max_length=255)
    turnstile_secret_key: str = Field(default="", max_length=1024)
    hcaptcha_site_key: str = Field(default="", max_length=255)
    hcaptcha_secret_key: str = Field(default="", max_length=1024)

    @field_validator(
        "turnstile_site_key",
        "turnstile_secret_key",
        "hcaptcha_site_key",
        "hcaptcha_secret_key",
        mode="before",
    )
    @classmethod
    def _clean_challenge_key(cls, value: object) -> str:
        return str(value or "").strip()


def turnstile_key_configuration_issue(
    site_key: str,
    secret_key: str,
) -> str:
    site = str(site_key or "").strip()
    secret = str(secret_key or "").strip()
    if site and secret and site == secret:
        return "Turnstile Secret Key 不能与 Site Key 相同；请填写 Cloudflare 控制台中的 Secret Key"
    return ""


def hcaptcha_key_configuration_issue(
    site_key: str,
    secret_key: str,
) -> str:
    site = str(site_key or "").strip()
    secret = str(secret_key or "").strip()
    if site and secret and site == secret:
        return "hCaptcha Secret Key 不能与 Site Key 相同；请填写 hCaptcha 控制台中的 Secret Key"
    return ""


class TTSSettingsConfig(StrictModel):
    enabled: bool = False
    http_timeout_sec: float = Field(default=20.0, ge=1.0, le=300.0)
    max_text_length: int = Field(default=500, ge=1, le=10000)
    api_base: str = Field(default="https://openspeech.bytedance.com", max_length=1000)
    app_id: str = Field(default="", max_length=255)
    app_key: str = Field(default="", max_length=1024)
    access_key: str = Field(default="", max_length=1024)
    resource_id: str = Field(default="seed-tts-2.0", max_length=255)
    model: str = Field(default="", max_length=255)
    speaker: str = Field(default="", max_length=255)
    audio_format: str = Field(default="ogg_opus", max_length=64)
    sample_rate: int = Field(default=48000, ge=8000, le=192000)
    bit_rate: int = Field(default=96000, ge=8000, le=512000)
    emotion: str = Field(default="", max_length=64)
    emotion_scale: int = Field(default=4, ge=1, le=5)
    speech_rate: int = Field(default=0, ge=-100, le=100)
    loudness_rate: int = Field(default=0, ge=-100, le=100)
    silence_duration_ms: int = Field(default=0, ge=0, le=10000)


class MusicSettingsConfig(StrictModel):
    enabled: bool = True
    http_timeout_sec: float = Field(default=15.0, ge=1.0, le=300.0)
    base_url: str = Field(default="https://music-api.gdstudio.xyz/api.php", max_length=1000)
    default_source: str = Field(default="kuwo", max_length=64)
    stable_sources: list[str] = Field(default_factory=lambda: ["kuwo", "netease", "joox", "bilibili"])


class MovieInfoSettingsConfig(StrictModel):
    enabled: bool = False
    http_timeout_sec: float = Field(default=6.0, ge=1.0, le=6.0)
    max_results: int = Field(default=6, ge=1, le=20)
    default_language: str = Field(default="zh-CN", max_length=32)
    default_region: str = Field(default="CN", max_length=16)
    tmdb_read_access_token: str = Field(default="", max_length=2048)
    imdb_data_set_id: str = Field(default="", max_length=255)
    imdb_revision_id: str = Field(default="", max_length=255)
    imdb_asset_id: str = Field(default="", max_length=255)
    imdb_api_key: str = Field(default="", max_length=2048)
    imdb_aws_access_key_id: str = Field(default="", max_length=255)
    imdb_aws_secret_access_key: str = Field(default="", max_length=2048)
    imdb_aws_session_token: str = Field(default="", max_length=4096)

    @field_validator(
        "tmdb_read_access_token",
        "imdb_data_set_id",
        "imdb_revision_id",
        "imdb_asset_id",
        "imdb_api_key",
        "imdb_aws_access_key_id",
        "imdb_aws_secret_access_key",
        "imdb_aws_session_token",
        mode="before",
    )
    @classmethod
    def _strip_provider_value(cls, value: object) -> str:
        return str(value or "").strip()

    @field_validator("default_language", mode="before")
    @classmethod
    def _normalize_language(cls, value: object) -> str:
        text = str(value or "").strip()
        if not re.fullmatch(r"[A-Za-z]{2,3}(?:-[A-Za-z]{2})?", text):
            raise ValueError("影片默认语言必须类似 zh-CN 或 en")
        language, *region = text.split("-", 1)
        return language.lower() + (f"-{region[0].upper()}" if region else "")

    @field_validator("default_region", mode="before")
    @classmethod
    def _normalize_region(cls, value: object) -> str:
        text = str(value or "").strip()
        if not re.fullmatch(r"[A-Za-z]{2}", text):
            raise ValueError("影片默认地区必须是两字母代码")
        return text.upper()


class AVSettingsConfig(StrictModel):
    enabled: bool = True
    http_timeout_sec: float = Field(default=15.0, ge=1.0, le=300.0)
    max_results: int = Field(default=18, ge=1, le=100)
    javbus_base_url: str = Field(default="https://www.javbus.com", max_length=1000)
    madouqu_base_url: str = Field(default="https://madouqu.com", max_length=1000)
    dmm_base_url: str = Field(default="https://www.dmm.co.jp", max_length=1000)
    fc2_base_url: str = Field(default="https://adult.contents.fc2.com", max_length=1000)


class StickerSettingsConfig(StrictModel):
    fallback_file_ids: list[str] = Field(default_factory=list)


class LoggingSettingsConfig(StrictModel):
    level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"
    third_party_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "WARNING"
    color: Literal["on", "off", "auto"] = "on"
    to_file: bool = False
    file_path: str = Field(default="data/bot.log", max_length=1000)
    file_max_bytes: int = Field(default=5 * 1024 * 1024, ge=1024, le=10 * 1024 * 1024 * 1024)
    file_backup_count: int = Field(default=3, ge=1, le=100)


class PromptSettingsConfig(StrictModel):
    decision: str = ""
    moderation: str = ""
    casual: str = ""
    manage_intent: str = ""
    compress: str = ""
    skill_tools: str = ""
    sticker_decision: str = ""
    reply_mode: str = ""
    persona: str = ""
    proactive_topic: str = ""
    style_distill: str = ""

    @classmethod
    def defaults(cls) -> PromptSettingsConfig:
        return cls(**load_prompt_defaults())

    @model_validator(mode="after")
    def _validate_moderation_template(self) -> PromptSettingsConfig:
        template = self.moderation or load_prompt_defaults()["moderation"]
        if "{rules_json}" not in template:
            raise ValueError("审核 Prompt 必须包含 {rules_json} 占位符")
        try:
            template.format(rules_json="[]")
        except (KeyError, IndexError, ValueError) as exc:
            raise ValueError(
                "审核 Prompt 的花括号或占位符无效；JSON 示例需使用双花括号"
            ) from exc
        return self


class RuntimeConfig(StrictModel):
    schema_version: int = Field(default=CONFIG_SCHEMA_VERSION, ge=1)
    models: ModelSettingsConfig = Field(default_factory=ModelSettingsConfig)
    bot: BotBehaviorConfig = Field(default_factory=BotBehaviorConfig)
    moderation: ModerationSettingsConfig = Field(default_factory=ModerationSettingsConfig)
    patrol: PatrolSettingsConfig = Field(default_factory=PatrolSettingsConfig)
    raid_guard: RaidGuardSettingsConfig = Field(default_factory=RaidGuardSettingsConfig)
    call_admin: CallAdminSettingsConfig = Field(default_factory=CallAdminSettingsConfig)
    vote_ban: VoteBanSettingsConfig = Field(default_factory=VoteBanSettingsConfig)
    verification: VerificationSettingsConfig = Field(default_factory=VerificationSettingsConfig)
    tts: TTSSettingsConfig = Field(default_factory=TTSSettingsConfig)
    music: MusicSettingsConfig = Field(default_factory=MusicSettingsConfig)
    movie_info: MovieInfoSettingsConfig = Field(default_factory=MovieInfoSettingsConfig)
    av: AVSettingsConfig = Field(default_factory=AVSettingsConfig)
    stickers: StickerSettingsConfig = Field(default_factory=StickerSettingsConfig)
    logging: LoggingSettingsConfig = Field(default_factory=LoggingSettingsConfig)
    prompts: PromptSettingsConfig = Field(default_factory=PromptSettingsConfig.defaults)

    @model_validator(mode="after")
    def _validate_model_references(self) -> RuntimeConfig:
        names = [provider.name for provider in self.models.providers]
        if len(names) != len(set(names)):
            raise ValueError("provider names must be unique")
        known = set(names)
        if not known:
            raise ValueError("at least one model provider is required")
        if not self.models.main.provider or self.models.main.provider not in known:
            raise ValueError("main model provider must reference an existing provider")
        if not self.models.main.model:
            raise ValueError("main model name is required")
        for role_name in ("vision", "decision", "moderation", "compress"):
            role = getattr(self.models, role_name)
            if role.provider and role.provider not in known:
                raise ValueError(f"{role_name} model provider does not exist: {role.provider}")
            for fallback in role.fallbacks:
                if fallback.provider not in known:
                    raise ValueError(f"{role_name} fallback provider does not exist: {fallback.provider}")
        for fallback in self.models.main.fallbacks:
            if fallback.provider not in known:
                raise ValueError(f"main fallback provider does not exist: {fallback.provider}")
        if self.models.embed.provider and self.models.embed.provider not in known:
            raise ValueError(f"embed model provider does not exist: {self.models.embed.provider}")
        for fallback in self.models.embed.fallbacks:
            if fallback.provider not in known:
                raise ValueError(f"embed fallback provider does not exist: {fallback.provider}")
        return self

    def secret_paths(self) -> set[str]:
        paths = set(_STATIC_SECRET_PATHS)
        paths.update(f"providers.{provider.name}.api_key" for provider in self.models.providers)
        return paths

    def extract_secrets(self) -> dict[str, str]:
        values = {
            "verification.turnstile_secret_key": self.verification.turnstile_secret_key,
            "verification.hcaptcha_secret_key": self.verification.hcaptcha_secret_key,
            "tts.app_key": self.tts.app_key,
            "tts.access_key": self.tts.access_key,
            "movie_info.tmdb_read_access_token": (
                self.movie_info.tmdb_read_access_token
            ),
            "movie_info.imdb_api_key": self.movie_info.imdb_api_key,
            "movie_info.imdb_aws_access_key_id": (
                self.movie_info.imdb_aws_access_key_id
            ),
            "movie_info.imdb_aws_secret_access_key": (
                self.movie_info.imdb_aws_secret_access_key
            ),
            "movie_info.imdb_aws_session_token": (
                self.movie_info.imdb_aws_session_token
            ),
        }
        values.update(
            {
                f"providers.{provider.name}.api_key": provider.api_key
                for provider in self.models.providers
            }
        )
        return {path: value for path, value in values.items() if value}

    def with_secrets(self, secrets: dict[str, str]) -> RuntimeConfig:
        clone = self.model_copy(deep=True)
        clone.verification.turnstile_secret_key = secrets.get(
            "verification.turnstile_secret_key", ""
        )
        clone.verification.hcaptcha_secret_key = secrets.get(
            "verification.hcaptcha_secret_key", ""
        )
        clone.tts.app_key = secrets.get("tts.app_key", "")
        clone.tts.access_key = secrets.get("tts.access_key", "")
        clone.movie_info.tmdb_read_access_token = secrets.get(
            "movie_info.tmdb_read_access_token", ""
        ).strip()
        clone.movie_info.imdb_api_key = secrets.get(
            "movie_info.imdb_api_key", ""
        ).strip()
        clone.movie_info.imdb_aws_access_key_id = secrets.get(
            "movie_info.imdb_aws_access_key_id", ""
        ).strip()
        clone.movie_info.imdb_aws_secret_access_key = secrets.get(
            "movie_info.imdb_aws_secret_access_key", ""
        ).strip()
        clone.movie_info.imdb_aws_session_token = secrets.get(
            "movie_info.imdb_aws_session_token", ""
        ).strip()
        for provider in clone.models.providers:
            provider.api_key = secrets.get(f"providers.{provider.name}.api_key", "")
        return clone

    def storage_payload(self) -> dict[str, Any]:
        clone = self.with_secrets({})
        return clone.model_dump(mode="json")

    def public_payload(self) -> dict[str, Any]:
        return self.storage_payload()

    @staticmethod
    def _fallback_spec(items: list[ModelFallbackConfig]) -> str:
        return ",".join(
            f"{item.provider}:{item.model}" if item.model else item.provider
            for item in items
        )

    @staticmethod
    def _fallback_request_params(items: list[ModelFallbackConfig]) -> list[dict[str, Any]]:
        return [dict(item.request_params) for item in items]

    def _provider_profiles(self) -> dict[str, ProviderProfile]:
        profiles: dict[str, ProviderProfile] = {}
        for item in self.models.providers:
            provider, api_base, endpoint, endpoint_path = _resolve_provider_profile(
                item.provider,
                item.api_base,
                None if item.chat_endpoint == "auto" else item.chat_endpoint,
            )
            profiles[item.name] = ProviderProfile(
                provider=provider,
                api_key=item.api_key or None,
                api_base=api_base,
                stream=item.stream,
                chat_endpoint=endpoint,
                endpoint_path=endpoint_path,
            )
        return profiles

    def apply_to_settings(
        self,
        settings: Settings,
        *,
        apply_prompts: bool = True,
    ) -> None:
        verification = self.verification
        challenge_keys: list[tuple[str, str]] = []
        if verification.provider in {"turnstile", "turnstile_hcaptcha"}:
            challenge_keys.extend(
                (
                    ("Turnstile Site Key", verification.turnstile_site_key),
                    ("Turnstile Secret Key", verification.turnstile_secret_key),
                )
            )
        if verification.provider in {"hcaptcha", "turnstile_hcaptcha"}:
            challenge_keys.extend(
                (
                    ("hCaptcha Site Key", verification.hcaptcha_site_key),
                    ("hCaptcha Secret Key", verification.hcaptcha_secret_key),
                )
            )
        if verification.enabled:
            missing = [
                label
                for label, value in (
                    *challenge_keys,
                    ("MINIAPP_PUBLIC_BASE_URL", settings.miniapp_public_base_url),
                )
                if not str(value or "").strip()
            ]
            if missing:
                raise ValueError("启用入群验证前需配置：" + "、".join(missing))
        profiles = self._provider_profiles()
        models = self.models
        main = models.main

        def effective_chat(role: ChatRoleConfig, *, parent: ChatRoleConfig) -> tuple[str, str]:
            return role.provider or parent.provider, role.model or parent.model

        vision_provider, vision_model = effective_chat(models.vision, parent=main)
        decision_provider, decision_model = effective_chat(models.decision, parent=main)
        moderation_parent = models.decision.model_copy(deep=True)
        moderation_parent.provider = decision_provider
        moderation_parent.model = decision_model
        moderation_provider, moderation_model = effective_chat(
            models.moderation,
            parent=moderation_parent,
        )
        compress_provider, compress_model = effective_chat(models.compress, parent=main)
        embed_provider = models.embed.provider or main.provider

        common_retry = {
            "retry_attempts": models.retry_attempts,
            "retry_backoff_sec": models.retry_backoff_sec,
            "retry_timeout_multiplier": models.retry_timeout_multiplier,
        }
        settings.bot.main_model = _build_chat_config(
            profiles=profiles,
            provider_name=main.provider,
            model_name=main.model,
            temperature=main.temperature,
            max_tokens=main.max_tokens,
            timeout_sec=main.timeout_sec,
            total_deadline_sec=main.total_deadline_sec,
            fallback_spec=self._fallback_spec(main.fallbacks),
            request_params=main.request_params,
            fallback_request_params=self._fallback_request_params(main.fallbacks),
            **common_retry,
        )
        settings.bot.vision_model = _build_chat_config(
            profiles=profiles,
            provider_name=vision_provider,
            model_name=vision_model,
            temperature=models.vision.temperature,
            max_tokens=models.vision.max_tokens,
            timeout_sec=models.vision.timeout_sec,
            total_deadline_sec=models.vision.total_deadline_sec,
            fallback_spec=self._fallback_spec(models.vision.fallbacks),
            request_params=models.vision.request_params,
            fallback_request_params=self._fallback_request_params(models.vision.fallbacks),
            **common_retry,
        )
        settings.bot.decision_model = _build_chat_config(
            profiles=profiles,
            provider_name=decision_provider,
            model_name=decision_model,
            temperature=models.decision.temperature,
            max_tokens=models.decision.max_tokens,
            timeout_sec=models.decision.timeout_sec,
            total_deadline_sec=models.decision.total_deadline_sec,
            fallback_spec=self._fallback_spec(models.decision.fallbacks),
            request_params=models.decision.request_params,
            fallback_request_params=self._fallback_request_params(models.decision.fallbacks),
            **common_retry,
        )
        settings.bot.moderation_model = _build_chat_config(
            profiles=profiles,
            provider_name=moderation_provider,
            model_name=moderation_model,
            temperature=models.moderation.temperature,
            max_tokens=models.moderation.max_tokens,
            timeout_sec=models.moderation.timeout_sec,
            total_deadline_sec=models.moderation.total_deadline_sec,
            fallback_spec=self._fallback_spec(models.moderation.fallbacks),
            request_params=models.moderation.request_params,
            fallback_request_params=self._fallback_request_params(models.moderation.fallbacks),
            **common_retry,
        )
        settings.bot.compress_model = _build_chat_config(
            profiles=profiles,
            provider_name=compress_provider,
            model_name=compress_model,
            temperature=models.compress.temperature,
            max_tokens=models.compress.max_tokens,
            timeout_sec=models.compress.timeout_sec,
            total_deadline_sec=models.compress.total_deadline_sec,
            fallback_spec=self._fallback_spec(models.compress.fallbacks),
            request_params=models.compress.request_params,
            fallback_request_params=self._fallback_request_params(models.compress.fallbacks),
            **common_retry,
        )
        settings.bot.embed_model = _build_embed_config(
            profiles=profiles,
            provider_name=embed_provider,
            model_name=models.embed.model,
            timeout_sec=models.embed.timeout_sec,
            total_deadline_sec=models.embed.total_deadline_sec,
            fallback_spec=self._fallback_spec(models.embed.fallbacks),
            request_params=models.embed.request_params,
            fallback_request_params=self._fallback_request_params(models.embed.fallbacks),
            **common_retry,
        )

        bot = self.bot
        settings.bot.parse_mode = bot.parse_mode
        settings.bot.disable_link_preview = bot.disable_link_preview
        settings.bot.drop_pending_updates = bot.drop_pending_updates
        settings.bot.inbound_debounce_seconds = bot.inbound_debounce_seconds
        settings.bot.reply_batch_timeout_seconds = bot.reply_batch_timeout_seconds
        settings.bot.enable_typing = bot.enable_typing
        settings.bot.enable_streaming = bot.enable_streaming
        settings.bot.stream_chunk_size = bot.stream_chunk_size
        settings.bot.stream_edit_interval_sec = bot.stream_edit_interval_sec
        settings.bot.auto_delete_seconds = bot.auto_delete_seconds
        settings.bot.auto_delete_minutes = bot.auto_delete_seconds // 60
        settings.bot.auto_delete_categories = list(bot.auto_delete_categories)
        settings.bot.auto_delete_category_seconds = dict(
            bot.auto_delete_category_seconds
        )
        settings.bot.auto_delete_category_mode = dict(
            bot.auto_delete_category_mode
        )
        settings.bot.decision_context_items = bot.decision_context_items
        settings.bot.max_context_tokens = bot.max_context_tokens
        settings.bot.max_output_tokens = bot.max_output_tokens
        settings.bot.memory_recent_messages = bot.memory_recent_messages
        settings.bot.memory_retention_days = bot.memory_retention_days
        settings.bot.memory_archive_max_messages_per_group = (
            bot.memory_archive_max_messages_per_group
        )
        settings.bot.memory_recall_enabled = bot.memory_recall_enabled
        settings.bot.memory_recall_max_results = bot.memory_recall_max_results
        settings.bot.memory_automatic_compaction = (
            bot.memory_automatic_compaction
        )
        settings.bot.proactive_default_enabled = bot.proactive_default_enabled
        settings.bot.proactive_idle_minutes = bot.proactive_idle_minutes
        settings.bot.proactive_jitter_minutes = bot.proactive_jitter_minutes
        settings.bot.proactive_check_interval_seconds = bot.proactive_check_interval_seconds
        settings.bot.proactive_quiet_hours_start = bot.proactive_quiet_hours_start
        settings.bot.proactive_quiet_hours_end = bot.proactive_quiet_hours_end
        settings.bot.proactive_retry_minutes = bot.proactive_retry_minutes
        settings.max_context_tokens = bot.max_context_tokens
        settings.max_output_tokens = bot.max_output_tokens

        settings.moderation = ModerationConfig(**self.moderation.model_dump())
        settings.patrol_enabled = self.patrol.enabled
        settings.patrol_schedule_time = self.patrol.schedule_time
        settings.patrol_batch_size = self.patrol.batch_size
        settings.patrol_batch_pause_seconds = self.patrol.batch_pause_seconds
        settings.patrol_fetch_bio = self.patrol.fetch_bio
        settings.patrol_challenge_timeout_seconds = self.patrol.challenge_timeout_seconds
        settings.patrol_check_interval_seconds = self.patrol.check_interval_seconds
        settings.raid_guard_enabled = self.raid_guard.enabled
        settings.raid_guard_pin_message = self.raid_guard.pin_message
        settings.raid_guard_join_threshold = self.raid_guard.join_threshold
        settings.raid_guard_window_seconds = self.raid_guard.window_seconds
        settings.raid_guard_lockdown_seconds = self.raid_guard.lockdown_seconds
        settings.raid_guard_lookback_seconds = self.raid_guard.lookback_seconds
        settings.raid_guard_challenge_timeout_seconds = (
            self.raid_guard.challenge_timeout_seconds
        )
        settings.call_admin_enabled = self.call_admin.enabled
        settings.call_admin_pin_message = self.call_admin.pin_message
        settings.call_admin_cooldown_seconds = self.call_admin.cooldown_seconds
        settings.vote_ban_enabled = self.vote_ban.enabled
        settings.vote_ban_pin_message = self.vote_ban.pin_message
        settings.vote_ban_threshold = self.vote_ban.vote_threshold
        settings.vote_ban_duration_seconds = self.vote_ban.duration_seconds
        settings.vote_ban_trigger_limit = self.vote_ban.trigger_limit
        settings.vote_ban_trigger_window_seconds = self.vote_ban.trigger_window_seconds
        settings.join_verification_enabled = self.verification.enabled
        settings.join_verification_timeout_seconds = self.verification.timeout_seconds
        settings.join_verification_check_interval_seconds = self.verification.check_interval_seconds
        settings.join_verification_provider = self.verification.provider
        settings.join_verification_turnstile_site_key = self.verification.turnstile_site_key
        settings.join_verification_turnstile_secret_key = self.verification.turnstile_secret_key
        settings.join_verification_hcaptcha_site_key = self.verification.hcaptcha_site_key
        settings.join_verification_hcaptcha_secret_key = self.verification.hcaptcha_secret_key

        tts = self.tts
        settings.doubao_tts_enabled = tts.enabled
        settings.doubao_tts_http_timeout_sec = tts.http_timeout_sec
        settings.doubao_tts_max_text_length = tts.max_text_length
        settings.doubao_tts_api_base = tts.api_base
        settings.doubao_tts_app_id = tts.app_id
        settings.doubao_tts_app_key = tts.app_key
        settings.doubao_tts_access_key = tts.access_key
        settings.doubao_tts_resource_id = tts.resource_id
        settings.doubao_tts_model = tts.model
        settings.doubao_tts_speaker = tts.speaker
        settings.doubao_tts_audio_format = tts.audio_format
        settings.doubao_tts_sample_rate = tts.sample_rate
        settings.doubao_tts_bit_rate = tts.bit_rate
        settings.doubao_tts_emotion = tts.emotion
        settings.doubao_tts_emotion_scale = tts.emotion_scale
        settings.doubao_tts_speech_rate = tts.speech_rate
        settings.doubao_tts_loudness_rate = tts.loudness_rate
        settings.doubao_tts_silence_duration_ms = tts.silence_duration_ms

        settings.music_api_enabled = self.music.enabled
        settings.music_api_http_timeout_sec = self.music.http_timeout_sec
        settings.music_api_base_url = self.music.base_url
        settings.music_api_default_source = self.music.default_source
        settings.music_api_stable_sources = ",".join(self.music.stable_sources)
        movie_info = self.movie_info
        if movie_info.enabled:
            tmdb_ready = bool(movie_info.tmdb_read_access_token)
            imdb_ready = all(
                (
                    movie_info.imdb_data_set_id,
                    movie_info.imdb_revision_id,
                    movie_info.imdb_asset_id,
                    movie_info.imdb_api_key,
                    movie_info.imdb_aws_access_key_id,
                    movie_info.imdb_aws_secret_access_key,
                )
            )
            if not (tmdb_ready or imdb_ready):
                raise ValueError(
                    "启用影片信息查询前，需配置 TMDB Read Access Token，"
                    "或完整的 IMDb AWS Data Exchange 凭据"
                )
        settings.movie_info_enabled = movie_info.enabled
        settings.movie_info_http_timeout_sec = movie_info.http_timeout_sec
        settings.movie_info_max_results = movie_info.max_results
        settings.movie_info_default_language = movie_info.default_language
        settings.movie_info_default_region = movie_info.default_region
        settings.movie_info_tmdb_read_access_token = (
            movie_info.tmdb_read_access_token
        )
        settings.movie_info_imdb_data_set_id = movie_info.imdb_data_set_id
        settings.movie_info_imdb_revision_id = movie_info.imdb_revision_id
        settings.movie_info_imdb_asset_id = movie_info.imdb_asset_id
        settings.movie_info_imdb_api_key = movie_info.imdb_api_key
        settings.movie_info_imdb_aws_access_key_id = (
            movie_info.imdb_aws_access_key_id
        )
        settings.movie_info_imdb_aws_secret_access_key = (
            movie_info.imdb_aws_secret_access_key
        )
        settings.movie_info_imdb_aws_session_token = (
            movie_info.imdb_aws_session_token
        )
        settings.av_enabled = self.av.enabled
        settings.av_http_timeout_sec = self.av.http_timeout_sec
        settings.av_max_results = self.av.max_results
        settings.av_javbus_base_url = self.av.javbus_base_url
        settings.av_madouqu_base_url = self.av.madouqu_base_url
        settings.av_dmm_base_url = self.av.dmm_base_url
        settings.av_fc2_base_url = self.av.fc2_base_url
        settings.skill_sticker_file_ids = ",".join(self.stickers.fallback_file_ids)
        if apply_prompts:
            set_runtime_prompts(self.prompts.model_dump())


class RuntimeConfigConflictError(RuntimeError):
    pass


class RuntimeConfigEncryptionError(RuntimeError):
    pass


class SecretCipher:
    def __init__(self, master_key: str) -> None:
        raw = str(master_key or "").strip()
        self._fernet: Fernet | None = None
        if raw:
            derived = hashlib.sha256(raw.encode("utf-8")).digest()
            self._fernet = Fernet(base64.urlsafe_b64encode(derived))

    @property
    def configured(self) -> bool:
        return self._fernet is not None

    def encrypt(self, value: str) -> str:
        if not self._fernet:
            raise RuntimeConfigEncryptionError(
                "CONFIG_MASTER_KEY is required before saving secret settings"
            )
        return self._fernet.encrypt(value.encode("utf-8")).decode("ascii")

    def decrypt(self, value: str) -> str:
        if not self._fernet:
            raise RuntimeConfigEncryptionError(
                "CONFIG_MASTER_KEY is required to decrypt stored settings"
            )
        try:
            return self._fernet.decrypt(value.encode("ascii")).decode("utf-8")
        except (InvalidToken, UnicodeError, ValueError) as exc:
            raise RuntimeConfigEncryptionError(
                "stored settings cannot be decrypted with CONFIG_MASTER_KEY"
            ) from exc


ConfigAppliedCallback = Callable[[RuntimeConfig], Awaitable[None] | None]


def _normalize_deprecated_runtime_payload(
    payload: dict[str, Any],
) -> tuple[dict[str, Any], bool]:
    """Normalize retired settings before strict schema validation.

    ``reasoning_effort`` used to be a first-class role field.  The first
    request-JSON revision then added role defaults and a global model override
    map.  Both older shapes are expanded into concrete primary/fallback model
    entries before the strict model rejects the retired fields.
    """

    normalized = dict(payload)
    changed = False

    # Runtime prompts are persisted in the database and otherwise keep the
    # retired owner-priority reply rules alive after the file defaults change.
    # Migrate only prompts containing the old rule markers so unrelated custom
    # prompts remain untouched.
    prompts_payload = normalized.get("prompts")
    if isinstance(prompts_payload, dict):
        prompt_migrations = {
            "decision": ("If [SENDER_IS_OWNER]=yes: as long as",),
            "persona": (
                "The owner's instructions have the highest priority.",
                "Always prioritize the owner's messages.",
                "you still prioritize the owner",
            ),
            "casual": (
                "more likely to prioritize the owner's messages",
                "prioritize responding, be soft and affectionate",
            ),
        }
        migrated_prompts = dict(prompts_payload)
        prompts_changed = False
        defaults = load_prompt_defaults()
        for name, markers in prompt_migrations.items():
            value = str(migrated_prompts.get(name) or "")
            if value and any(marker in value for marker in markers):
                migrated_prompts[name] = defaults[name]
                prompts_changed = True
        if prompts_changed:
            normalized["prompts"] = migrated_prompts
            changed = True
    bot_payload = payload.get("bot")
    if not (
        isinstance(bot_payload, dict)
        and "drop_pending_updates" in bot_payload
        and bot_payload["drop_pending_updates"] is False
    ):
        normalized_bot = dict(bot_payload) if isinstance(bot_payload, dict) else {}
        normalized_bot["drop_pending_updates"] = False
        normalized["bot"] = normalized_bot
        changed = True

    # The former Sub2API credential was global and therefore cannot be safely
    # migrated to any particular group. All groups intentionally start with
    # the replacement API model query feature disabled.
    if "sub2api" in normalized:
        normalized.pop("sub2api", None)
        changed = True

    models_payload = normalized.get("models")
    if isinstance(models_payload, dict):
        migrated_models = dict(models_payload)
        models_changed = False

        # Build enough provider metadata to recognize the final LiteLLM model
        # keys used by the previous global override editor.  Raw model and
        # provider/model aliases remain valid fallbacks when metadata is old or
        # incomplete.
        provider_profiles: dict[str, dict[str, str]] = {}
        raw_providers = models_payload.get("providers")
        if isinstance(raw_providers, list):
            for raw_provider in raw_providers:
                if not isinstance(raw_provider, dict):
                    continue
                name = str(raw_provider.get("name") or "").strip().lower()
                if name:
                    provider_profiles[name] = {
                        "provider": str(raw_provider.get("provider") or "").strip().lower(),
                        "api_base": str(raw_provider.get("api_base") or "").strip(),
                    }

        legacy_overrides_raw = models_payload.get("model_overrides")
        legacy_overrides: dict[str, dict[str, Any]] = {}
        if isinstance(legacy_overrides_raw, dict):
            for raw_key, raw_params in legacy_overrides_raw.items():
                key = str(raw_key or "").strip().lower()
                if key and isinstance(raw_params, dict):
                    legacy_overrides[key] = dict(raw_params)

        missing = object()

        role_parents = {
            "vision": "main",
            "decision": "main",
            "moderation": "decision",
            "compress": "main",
        }

        def effective_role_ref(role_name: str) -> tuple[str, str]:
            role = models_payload.get(role_name)
            role = role if isinstance(role, dict) else {}
            if role_name == "embed":
                main_provider, _ = effective_role_ref("main")
                return (
                    str(role.get("provider") or main_provider).strip().lower(),
                    str(role.get("model") or "text-embedding-004").strip(),
                )
            parent_name = role_parents.get(role_name)
            if parent_name:
                parent_provider, parent_model = effective_role_ref(parent_name)
            else:
                parent_provider, parent_model = "", ""
            provider = str(role.get("provider") or parent_provider).strip().lower()
            model = str(role.get("model") or parent_model).strip()
            return provider, model

        def override_for(provider_ref: object, model_name: object) -> dict[str, Any] | object:
            model = str(model_name or "").strip()
            provider_name = str(provider_ref or "").strip().lower()
            if not model or not legacy_overrides:
                return missing
            profile = provider_profiles.get(provider_name, {})
            protocol = str(profile.get("provider") or provider_name).strip().lower()
            api_base = profile.get("api_base") or None
            keys: list[str] = []
            try:
                resolved = _build_litellm_model(protocol, model, api_base=api_base)
            except Exception:
                resolved = ""
            if resolved:
                keys.append(str(resolved).strip().lower())
            keys.append(model.lower())
            if provider_name:
                keys.append(f"{provider_name}/{model}".lower())
            if protocol:
                keys.append(f"{protocol}/{model}".lower())
            for key in keys:
                if key and key in legacy_overrides:
                    return dict(legacy_overrides[key])
            return missing

        for role_name in ("main", "vision", "decision", "moderation", "compress", "embed"):
            role_payload = models_payload.get(role_name)
            if not isinstance(role_payload, dict):
                continue
            migrated_role = dict(role_payload)
            role_changed = False
            effective_provider, effective_model = effective_role_ref(role_name)

            if "reasoning_effort" in migrated_role:
                old_effort = migrated_role.pop("reasoning_effort")
                if (
                    "request_params" not in migrated_role
                    or migrated_role.get("request_params") is None
                ):
                    migrated_role["request_params"] = {"reasoning_effort": old_effort}
                role_changed = True

            role_default = migrated_role.get("request_params")
            if not isinstance(role_default, dict):
                role_default = {}
            primary_override = override_for(
                effective_provider,
                effective_model,
            )
            if primary_override is not missing:
                migrated_role["request_params"] = primary_override
                role_changed = True

            raw_fallbacks = migrated_role.get("fallbacks")
            if isinstance(raw_fallbacks, list):
                migrated_fallbacks: list[Any] = []
                for raw_fallback in raw_fallbacks:
                    if not isinstance(raw_fallback, dict):
                        migrated_fallbacks.append(raw_fallback)
                        continue
                    migrated_fallback = dict(raw_fallback)
                    fallback_model = migrated_fallback.get("model") or effective_model
                    fallback_override = override_for(
                        migrated_fallback.get("provider"),
                        fallback_model,
                    )
                    # Prior role defaults applied to every fallback. Copy them
                    # only when the old payload had no concrete fallback value;
                    # current documents already contain this field.
                    if (
                        "request_params" not in migrated_fallback
                        or migrated_fallback.get("request_params") is None
                    ) and role_default:
                        migrated_fallback["request_params"] = dict(role_default)
                        role_changed = True
                    if fallback_override is not missing:
                        migrated_fallback["request_params"] = fallback_override
                        role_changed = True
                    migrated_fallbacks.append(migrated_fallback)
                if migrated_fallbacks != raw_fallbacks:
                    migrated_role["fallbacks"] = migrated_fallbacks
                    role_changed = True

            if role_changed:
                migrated_models[role_name] = migrated_role
                models_changed = True

        if "model_overrides" in migrated_models:
            migrated_models.pop("model_overrides", None)
            models_changed = True
        if models_changed:
            normalized["models"] = migrated_models
            changed = True
    return (normalized, True) if changed else (payload, False)


class RuntimeConfigManager:
    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        settings: Settings,
        legacy_config_path: str = "config.toml",
        legacy_raw_env: dict[str, str] | None = None,
    ) -> None:
        self.session_factory = session_factory
        self.settings = settings
        self.legacy_config_path = legacy_config_path
        self.legacy_raw_env = legacy_raw_env
        self._cipher = SecretCipher(settings.config_master_key)
        self._config: RuntimeConfig | None = None
        self._revision = 0
        self._lock = asyncio.Lock()
        self._on_applied: ConfigAppliedCallback | None = None

    @property
    def config(self) -> RuntimeConfig:
        if self._config is None:
            raise RuntimeError("runtime config manager is not initialized")
        return self._config

    @property
    def revision(self) -> int:
        return self._revision

    def set_apply_callback(self, callback: ConfigAppliedCallback | None) -> None:
        self._on_applied = callback

    async def _load_secret_map(self, session: AsyncSession) -> dict[str, str]:
        result = await session.execute(select(RuntimeConfigSecret))
        rows = list(result.scalars().all())
        if rows and not self._cipher.configured:
            raise RuntimeConfigEncryptionError(
                "CONFIG_MASTER_KEY is missing but encrypted settings already exist"
            )
        return {row.name: self._cipher.decrypt(row.ciphertext) for row in rows}

    async def _write_secret_map(
        self,
        session: AsyncSession,
        secrets: dict[str, str],
        *,
        updated_by: int,
    ) -> None:
        result = await session.execute(select(RuntimeConfigSecret))
        existing = {row.name: row for row in result.scalars().all()}
        for name, row in existing.items():
            if name not in secrets:
                await session.delete(row)
        for name, value in secrets.items():
            row = existing.get(name)
            ciphertext = self._cipher.encrypt(value)
            if row is None:
                session.add(
                    RuntimeConfigSecret(
                        name=name,
                        ciphertext=ciphertext,
                        updated_by=updated_by,
                    )
                )
            else:
                row.ciphertext = ciphertext
                row.updated_by = updated_by

    async def initialize(self) -> RuntimeConfig:
        async with self._lock:
            async with self.session_factory() as session:
                row = await session.get(RuntimeConfigRecord, 1)
                if row is None:
                    config = build_legacy_runtime_config(
                        self.legacy_config_path,
                        settings=self.settings,
                        raw_env=self.legacy_raw_env,
                    )
                    # Resolve provider endpoints before committing the imported document.
                    config.apply_to_settings(
                        self.settings.model_copy(deep=True),
                        apply_prompts=False,
                    )
                    secrets = config.extract_secrets()
                    session.add(
                        RuntimeConfigRecord(
                            id=1,
                            schema_version=CONFIG_SCHEMA_VERSION,
                            revision=1,
                            payload=config.storage_payload(),
                            updated_by=0,
                        )
                    )
                    if secrets:
                        await self._write_secret_map(session, secrets, updated_by=0)
                    await session.commit()
                    revision = 1
                    log.info("Imported legacy runtime settings into the database")
                else:
                    raw_payload, payload_normalized = (
                        _normalize_deprecated_runtime_payload(row.payload or {})
                    )
                    config = RuntimeConfig.model_validate(raw_payload)
                    retired_secret = await session.execute(
                        delete(RuntimeConfigSecret).where(
                            RuntimeConfigSecret.name == "sub2api.api_key"
                        )
                    )
                    retired_secret_deleted = int(retired_secret.rowcount or 0) > 0
                    secrets = await self._load_secret_map(session)
                    config = config.with_secrets(secrets)
                    revision = int(row.revision or 1)
                    if payload_normalized or retired_secret_deleted:
                        # This is an internal safety migration, not an operator
                        # edit. Preserve the revision so an already-open admin
                        # page does not encounter a needless conflict.
                        if payload_normalized:
                            row.payload = raw_payload
                            row.schema_version = CONFIG_SCHEMA_VERSION
                        await session.commit()
                        log.info(
                            "Normalized deprecated runtime settings without "
                            "changing runtime config revision"
                        )

            config.apply_to_settings(self.settings)
            self._config = config
            self._revision = revision
            return config

    async def _notify_applied(self, config: RuntimeConfig) -> None:
        callback = self._on_applied
        if callback is None:
            return
        try:
            result = callback(config)
            if inspect.isawaitable(result):
                await result
        except Exception:
            # Persistence already committed. Report the save as successful and
            # keep the database/live Settings revision coherent; a failed
            # component-specific refresh is logged for operator action.
            log.exception("runtime config post-apply callback failed")

    async def save(
        self,
        payload: dict[str, Any],
        *,
        expected_revision: int,
        updated_by: int,
        secret_changes: dict[str, dict[str, str]] | None = None,
    ) -> RuntimeConfig:
        async with self._lock:
            current = self.config
            normalized_payload, _ = _normalize_deprecated_runtime_payload(payload)
            candidate = RuntimeConfig.model_validate(normalized_payload)
            allowed_paths = candidate.secret_paths()
            secrets = current.extract_secrets()

            # Non-empty secret fields are accepted for non-browser callers, but
            # the Mini App normally uses the explicit replace/clear protocol.
            secrets.update(candidate.extract_secrets())
            for path, change in (secret_changes or {}).items():
                if path not in allowed_paths:
                    raise ValueError(f"unknown secret setting: {path}")
                action = str((change or {}).get("action") or "").strip().lower()
                if action == "keep":
                    continue
                if action == "clear":
                    secrets.pop(path, None)
                    continue
                if action == "replace":
                    value = str((change or {}).get("value") or "").strip()
                    if not value:
                        raise ValueError(f"replacement secret is empty: {path}")
                    secrets[path] = value
                    continue
                raise ValueError(f"invalid secret action for {path}")
            secrets = {
                path: value
                for path, value in secrets.items()
                if path in allowed_paths and value
            }
            candidate = candidate.with_secrets(secrets)
            turnstile_issue = turnstile_key_configuration_issue(
                candidate.verification.turnstile_site_key,
                candidate.verification.turnstile_secret_key,
            )
            current_turnstile_issue = turnstile_key_configuration_issue(
                current.verification.turnstile_site_key,
                current.verification.turnstile_secret_key,
            )
            unchanged_legacy_issue = bool(
                current_turnstile_issue
                and turnstile_issue
                and candidate.verification.turnstile_site_key
                == current.verification.turnstile_site_key
                and candidate.verification.turnstile_secret_key
                == current.verification.turnstile_secret_key
            )
            if turnstile_issue and not unchanged_legacy_issue:
                raise ValueError(turnstile_issue)
            hcaptcha_issue = hcaptcha_key_configuration_issue(
                candidate.verification.hcaptcha_site_key,
                candidate.verification.hcaptcha_secret_key,
            )
            current_hcaptcha_issue = hcaptcha_key_configuration_issue(
                current.verification.hcaptcha_site_key,
                current.verification.hcaptcha_secret_key,
            )
            unchanged_hcaptcha_issue = bool(
                current_hcaptcha_issue
                and hcaptcha_issue
                and candidate.verification.hcaptcha_site_key
                == current.verification.hcaptcha_site_key
                and candidate.verification.hcaptcha_secret_key
                == current.verification.hcaptcha_secret_key
            )
            if hcaptcha_issue and not unchanged_hcaptcha_issue:
                raise ValueError(hcaptcha_issue)
            candidate.apply_to_settings(
                self.settings.model_copy(deep=True),
                apply_prompts=False,
            )

            async with self.session_factory() as session:
                expected = int(expected_revision)
                result = await session.execute(
                    update(RuntimeConfigRecord)
                    .where(
                        RuntimeConfigRecord.id == 1,
                        RuntimeConfigRecord.revision == expected,
                    )
                    .values(
                        payload=candidate.storage_payload(),
                        schema_version=CONFIG_SCHEMA_VERSION,
                        revision=expected + 1,
                        updated_by=int(updated_by),
                    )
                )
                if result.rowcount != 1:
                    await session.rollback()
                    actual = await session.scalar(
                        select(RuntimeConfigRecord.revision).where(
                            RuntimeConfigRecord.id == 1
                        )
                    )
                    raise RuntimeConfigConflictError(
                        f"runtime config revision changed: expected {expected}, got {int(actual or 0)}"
                    )
                await self._write_secret_map(session, secrets, updated_by=int(updated_by))
                await session.commit()
                revision = expected + 1

            candidate.apply_to_settings(self.settings)
            self._config = candidate
            self._revision = revision
            await self._notify_applied(candidate)
            return candidate

    def api_document(self) -> dict[str, Any]:
        config = self.config
        return {
            "revision": self.revision,
            "config": config.public_payload(),
            "configured_secrets": sorted(config.extract_secrets()),
            "bootstrap": {
                "public_base_url": self.settings.miniapp_public_base_url,
                "listen_host": self.settings.miniapp_listen_host,
                "listen_port": self.settings.miniapp_listen_port,
                "database_url": _redact_database_url(self.settings.database_url),
                "master_key_configured": self._cipher.configured,
            },
            "restart_required_paths": [
                "bot.parse_mode",
            ],
        }


def _redact_database_url(url: str) -> str:
    value = str(url or "")
    if "://" not in value or "@" not in value:
        return value
    scheme, rest = value.split("://", 1)
    credentials, host = rest.rsplit("@", 1)
    if ":" in credentials:
        username = credentials.split(":", 1)[0]
        credentials = f"{username}:***"
    return f"{scheme}://{credentials}@{host}"


def _legacy_fallbacks(
    raw: str,
    configured: list[object] | None = None,
) -> list[ModelFallbackConfig]:
    configured = configured or []
    result: list[ModelFallbackConfig] = []
    for index, (provider, model) in enumerate(_parse_fallbacks(raw)):
        source = configured[index] if index < len(configured) else None
        result.append(
            ModelFallbackConfig(
                provider=provider,
                model=model or "",
                request_params=dict(getattr(source, "request_params", {}) or {}),
            )
        )
    return result


def _env_bool(
    name: str,
    default: bool,
    *,
    values: dict[str, str] | None = None,
) -> bool:
    raw = (values or {}).get(name, os.getenv(name))
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on", "y", "t"}


def _env_int(
    name: str,
    default: int,
    *,
    values: dict[str, str] | None = None,
) -> int:
    try:
        raw = (values or {}).get(name, os.getenv(name, str(default)))
        return int(str(raw).strip())
    except (TypeError, ValueError):
        return default


def _env_choice(
    name: str,
    default: str,
    allowed: set[str],
    *,
    values: dict[str, str],
) -> str:
    value = str(values.get(name, default) or default).strip()
    normalized = value.upper() if default.isupper() else value.lower()
    return normalized if normalized in allowed else default


def _apply_legacy_toml(settings: Settings, config_path: str) -> None:
    path = Path(config_path)
    if not path.exists():
        return
    try:
        with path.open("rb") as stream:
            data = tomllib.load(stream)
    except Exception as exc:
        raise ValueError(
            f"failed to read legacy TOML settings: {path}"
        ) from exc
    bot_data = data.get("bot") if isinstance(data, dict) else None
    if isinstance(bot_data, dict):
        for key, target in (
            ("main_model", "main_model"),
            ("vision_model", "vision_model"),
            ("decision_model", "decision_model"),
            ("moderation_model", "moderation_model"),
            ("compress_model", "compress_model"),
        ):
            raw = bot_data.get(key)
            if isinstance(raw, dict):
                setattr(settings.bot, target, ModelConfig(**raw))
        raw_embed = bot_data.get("embed_model")
        if isinstance(raw_embed, dict):
            settings.bot.embed_model = EmbedConfig(**raw_embed)
        if "parse_mode" in bot_data:
            settings.bot.parse_mode = str(bot_data["parse_mode"] or "HTML")
        if "disable_link_preview" in bot_data:
            settings.bot.disable_link_preview = bool(
                bot_data["disable_link_preview"]
            )
        if "drop_pending_updates" in bot_data:
            settings.bot.drop_pending_updates = bool(bot_data["drop_pending_updates"])
        if "reply_batch_timeout_seconds" in bot_data:
            settings.bot.reply_batch_timeout_seconds = min(
                120.0,
                max(5.0, float(bot_data["reply_batch_timeout_seconds"])),
            )
        for key in (
            "memory_recent_messages",
            "memory_retention_days",
            "memory_archive_max_messages_per_group",
            "memory_recall_max_results",
        ):
            if key in bot_data:
                setattr(settings.bot, key, int(bot_data[key]))
        for key in ("memory_recall_enabled", "memory_automatic_compaction"):
            if key in bot_data:
                setattr(settings.bot, key, bool(bot_data[key]))
    moderation_data = data.get("moderation") if isinstance(data, dict) else None
    if isinstance(moderation_data, dict):
        settings.moderation = ModerationConfig(**moderation_data)


def build_legacy_runtime_config(
    config_path: str = "config.toml",
    *,
    settings: Settings | None = None,
    raw_env: dict[str, str] | None = None,
) -> RuntimeConfig:
    """Build the one-time import document from old env/TOML settings."""
    settings = settings or Settings()
    _apply_legacy_toml(settings, config_path)
    raw_env = _load_raw_env() if raw_env is None else raw_env
    # A malformed legacy provider must stop the one-time import. Persisting a
    # fallback document here would make the corrected env impossible to import
    # on the next start because the database row would already exist.
    legacy_profiles = _collect_provider_profiles(raw_env)

    providers = [
        ProviderConfig(
            name=name,
            provider=profile.provider,
            api_key=profile.api_key or "",
            api_base=profile.api_base or "",
            stream=profile.stream,
            chat_endpoint=profile.chat_endpoint,
        )
        for name, profile in legacy_profiles.items()
    ]
    if not providers:
        providers = [ProviderConfig(name="gemini", provider="gemini")]

    known_names = {provider.name for provider in providers}
    main_provider = settings.main_provider_name.strip().lower()
    if main_provider not in known_names:
        main_provider = providers[0].name

    def role_provider(raw: str, parent: str = main_provider) -> str:
        value = str(raw or "").strip().lower()
        return value if value in known_names else parent

    model_settings = ModelSettingsConfig(
        providers=providers,
        main=ChatRoleConfig(
            provider=main_provider,
            model=settings.main_model.strip() or "gemini-2.0-flash",
            fallbacks=_legacy_fallbacks(
                settings.main_fallbacks,
                settings.bot.main_model.fallbacks,
            ),
            temperature=settings.bot.main_model.temperature,
            max_tokens=max(1, settings.max_output_tokens),
            timeout_sec=settings.main_timeout_sec,
            total_deadline_sec=max(0.0, settings.main_total_deadline_sec),
            request_params=dict(settings.bot.main_model.request_params),
        ),
        vision=ChatRoleConfig(
            provider=role_provider(settings.vision_provider_name),
            model=settings.vision_model.strip(),
            fallbacks=_legacy_fallbacks(
                settings.vision_fallbacks,
                settings.bot.vision_model.fallbacks,
            ),
            temperature=settings.bot.vision_model.temperature,
            max_tokens=settings.bot.vision_model.max_tokens,
            timeout_sec=settings.vision_timeout_sec,
            total_deadline_sec=max(0.0, settings.vision_total_deadline_sec),
            request_params=dict(settings.bot.vision_model.request_params),
        ),
        decision=ChatRoleConfig(
            provider=role_provider(settings.decision_provider_name),
            model=settings.decision_model.strip(),
            fallbacks=_legacy_fallbacks(
                settings.decision_fallbacks,
                settings.bot.decision_model.fallbacks,
            ),
            temperature=settings.bot.decision_model.temperature,
            max_tokens=settings.bot.decision_model.max_tokens,
            timeout_sec=settings.decision_timeout_sec,
            total_deadline_sec=max(0.0, settings.decision_total_deadline_sec),
            request_params=dict(settings.bot.decision_model.request_params),
        ),
        moderation=ChatRoleConfig(
            provider=role_provider(settings.moderation_provider_name),
            model=settings.moderation_model.strip(),
            fallbacks=_legacy_fallbacks(
                settings.moderation_fallbacks,
                settings.bot.moderation_model.fallbacks,
            ),
            temperature=settings.bot.moderation_model.temperature,
            max_tokens=settings.bot.moderation_model.max_tokens,
            timeout_sec=settings.moderation_timeout_sec,
            total_deadline_sec=max(0.0, settings.moderation_total_deadline_sec),
            request_params=dict(settings.bot.moderation_model.request_params),
        ),
        compress=ChatRoleConfig(
            provider=role_provider(settings.compress_provider_name),
            model=settings.compress_model.strip(),
            fallbacks=_legacy_fallbacks(
                settings.compress_fallbacks,
                settings.bot.compress_model.fallbacks,
            ),
            temperature=settings.bot.compress_model.temperature,
            max_tokens=settings.bot.compress_model.max_tokens,
            timeout_sec=settings.compress_timeout_sec,
            total_deadline_sec=max(0.0, settings.compress_total_deadline_sec),
            request_params=dict(settings.bot.compress_model.request_params),
        ),
        embed=EmbedRoleConfig(
            provider=role_provider(settings.embed_provider_name),
            model=settings.embed_model.strip() or "text-embedding-004",
            fallbacks=_legacy_fallbacks(
                settings.embed_fallbacks,
                settings.bot.embed_model.fallbacks,
            ),
            timeout_sec=settings.embed_timeout_sec,
            total_deadline_sec=max(0.0, settings.embed_total_deadline_sec),
            request_params=dict(settings.bot.embed_model.request_params),
        ),
        retry_attempts=settings.llm_retry_attempts,
        retry_backoff_sec=settings.llm_retry_backoff_sec,
        retry_timeout_multiplier=settings.llm_retry_timeout_multiplier,
    )

    return RuntimeConfig(
        models=model_settings,
        bot=BotBehaviorConfig(
            parse_mode=settings.bot.parse_mode,
            disable_link_preview=settings.bot.disable_link_preview,
            drop_pending_updates=settings.bot.drop_pending_updates,
            inbound_debounce_seconds=settings.bot_inbound_debounce_seconds,
            reply_batch_timeout_seconds=(
                settings.bot_reply_batch_timeout_seconds
                if "bot_reply_batch_timeout_seconds"
                in getattr(settings, "model_fields_set", set())
                else settings.bot.reply_batch_timeout_seconds
            ),
            enable_typing=settings.bot_enable_typing,
            enable_streaming=settings.bot_enable_streaming,
            stream_chunk_size=settings.bot_stream_chunk_size,
            stream_edit_interval_sec=settings.bot_stream_edit_interval_sec,
            auto_delete_seconds=(
                int(settings.bot_auto_delete_seconds)
                if "bot_auto_delete_seconds" in getattr(settings, "model_fields_set", set())
                else int(settings.bot_auto_delete_seconds)
                or max(0, int(settings.bot_auto_delete_minutes)) * 60
            ),
            auto_delete_categories=list(settings.bot.auto_delete_categories),
            auto_delete_category_seconds=dict(
                settings.bot.auto_delete_category_seconds
            ),
            auto_delete_category_mode=dict(settings.bot.auto_delete_category_mode),
            decision_context_items=settings.bot_decision_context_items,
            max_context_tokens=settings.max_context_tokens,
            max_output_tokens=settings.max_output_tokens,
            memory_recent_messages=(
                settings.bot_memory_recent_messages
                if "bot_memory_recent_messages"
                in getattr(settings, "model_fields_set", set())
                else settings.bot.memory_recent_messages
            ),
            memory_retention_days=(
                settings.bot_memory_retention_days
                if "bot_memory_retention_days"
                in getattr(settings, "model_fields_set", set())
                else settings.bot.memory_retention_days
            ),
            memory_archive_max_messages_per_group=(
                settings.bot_memory_archive_max_messages_per_group
                if "bot_memory_archive_max_messages_per_group"
                in getattr(settings, "model_fields_set", set())
                else settings.bot.memory_archive_max_messages_per_group
            ),
            memory_recall_enabled=(
                settings.bot_memory_recall_enabled
                if "bot_memory_recall_enabled"
                in getattr(settings, "model_fields_set", set())
                else settings.bot.memory_recall_enabled
            ),
            memory_recall_max_results=(
                settings.bot_memory_recall_max_results
                if "bot_memory_recall_max_results"
                in getattr(settings, "model_fields_set", set())
                else settings.bot.memory_recall_max_results
            ),
            memory_automatic_compaction=(
                settings.bot_memory_automatic_compaction
                if "bot_memory_automatic_compaction"
                in getattr(settings, "model_fields_set", set())
                else settings.bot.memory_automatic_compaction
            ),
            proactive_default_enabled=settings.bot_proactive_default_enabled,
            proactive_idle_minutes=settings.bot_proactive_idle_minutes,
            proactive_jitter_minutes=settings.bot_proactive_jitter_minutes,
            proactive_check_interval_seconds=settings.bot_proactive_check_interval_seconds,
            proactive_quiet_hours_start=settings.bot_proactive_quiet_hours_start,
            proactive_quiet_hours_end=settings.bot_proactive_quiet_hours_end,
            proactive_retry_minutes=settings.bot_proactive_retry_minutes,
        ),
        moderation=ModerationSettingsConfig(**settings.moderation.model_dump()),
        patrol=PatrolSettingsConfig(
            enabled=settings.patrol_enabled,
            schedule_time=settings.patrol_schedule_time,
            batch_size=settings.patrol_batch_size,
            batch_pause_seconds=settings.patrol_batch_pause_seconds,
            fetch_bio=settings.patrol_fetch_bio,
            challenge_timeout_seconds=settings.patrol_challenge_timeout_seconds,
            check_interval_seconds=settings.patrol_check_interval_seconds,
        ),
        raid_guard=RaidGuardSettingsConfig(
            enabled=settings.raid_guard_enabled,
            pin_message=settings.raid_guard_pin_message,
            join_threshold=settings.raid_guard_join_threshold,
            window_seconds=settings.raid_guard_window_seconds,
            lockdown_seconds=settings.raid_guard_lockdown_seconds,
            lookback_seconds=settings.raid_guard_lookback_seconds,
            challenge_timeout_seconds=settings.raid_guard_challenge_timeout_seconds,
        ),
        call_admin=CallAdminSettingsConfig(
            enabled=settings.call_admin_enabled,
            pin_message=settings.call_admin_pin_message,
            cooldown_seconds=settings.call_admin_cooldown_seconds,
        ),
        vote_ban=VoteBanSettingsConfig(
            enabled=settings.vote_ban_enabled,
            pin_message=settings.vote_ban_pin_message,
            vote_threshold=settings.vote_ban_threshold,
            duration_seconds=settings.vote_ban_duration_seconds,
            trigger_limit=settings.vote_ban_trigger_limit,
            trigger_window_seconds=settings.vote_ban_trigger_window_seconds,
        ),
        verification=VerificationSettingsConfig(
            enabled=settings.join_verification_enabled,
            timeout_seconds=settings.join_verification_timeout_seconds,
            check_interval_seconds=settings.join_verification_check_interval_seconds,
            provider=settings.join_verification_provider,
            turnstile_site_key=settings.join_verification_turnstile_site_key,
            turnstile_secret_key=settings.join_verification_turnstile_secret_key,
            hcaptcha_site_key=settings.join_verification_hcaptcha_site_key,
            hcaptcha_secret_key=settings.join_verification_hcaptcha_secret_key,
        ),
        tts=TTSSettingsConfig(
            enabled=settings.doubao_tts_enabled,
            http_timeout_sec=settings.doubao_tts_http_timeout_sec,
            max_text_length=settings.doubao_tts_max_text_length,
            api_base=settings.doubao_tts_api_base,
            app_id=settings.doubao_tts_app_id,
            app_key=settings.doubao_tts_app_key,
            access_key=settings.doubao_tts_access_key,
            resource_id=settings.doubao_tts_resource_id,
            model=settings.doubao_tts_model,
            speaker=settings.doubao_tts_speaker,
            audio_format=settings.doubao_tts_audio_format,
            sample_rate=settings.doubao_tts_sample_rate,
            bit_rate=settings.doubao_tts_bit_rate,
            emotion=settings.doubao_tts_emotion,
            emotion_scale=settings.doubao_tts_emotion_scale,
            speech_rate=settings.doubao_tts_speech_rate,
            loudness_rate=settings.doubao_tts_loudness_rate,
            silence_duration_ms=settings.doubao_tts_silence_duration_ms,
        ),
        music=MusicSettingsConfig(
            enabled=settings.music_api_enabled,
            http_timeout_sec=settings.music_api_http_timeout_sec,
            base_url=settings.music_api_base_url,
            default_source=settings.music_api_default_source,
            stable_sources=[
                value.strip()
                for value in settings.music_api_stable_sources.split(",")
                if value.strip()
            ],
        ),
        movie_info=MovieInfoSettingsConfig(
            enabled=settings.movie_info_enabled,
            http_timeout_sec=settings.movie_info_http_timeout_sec,
            max_results=settings.movie_info_max_results,
            default_language=settings.movie_info_default_language,
            default_region=settings.movie_info_default_region,
            tmdb_read_access_token=settings.movie_info_tmdb_read_access_token,
            imdb_data_set_id=settings.movie_info_imdb_data_set_id,
            imdb_revision_id=settings.movie_info_imdb_revision_id,
            imdb_asset_id=settings.movie_info_imdb_asset_id,
            imdb_api_key=settings.movie_info_imdb_api_key,
            imdb_aws_access_key_id=settings.movie_info_imdb_aws_access_key_id,
            imdb_aws_secret_access_key=(
                settings.movie_info_imdb_aws_secret_access_key
            ),
            imdb_aws_session_token=settings.movie_info_imdb_aws_session_token,
        ),
        av=AVSettingsConfig(
            enabled=settings.av_enabled,
            http_timeout_sec=settings.av_http_timeout_sec,
            max_results=settings.av_max_results,
            javbus_base_url=settings.av_javbus_base_url,
            madouqu_base_url=settings.av_madouqu_base_url,
            dmm_base_url=settings.av_dmm_base_url,
            fc2_base_url=settings.av_fc2_base_url,
        ),
        stickers=StickerSettingsConfig(
            fallback_file_ids=[
                value.strip()
                for value in settings.skill_sticker_file_ids.split(",")
                if value.strip()
            ]
        ),
        logging=LoggingSettingsConfig(
            level=_env_choice(
                "LOG_LEVEL",
                "INFO",
                {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"},
                values=raw_env,
            ),
            third_party_level=_env_choice(
                "LOG_THIRD_PARTY_LEVEL",
                "WARNING",
                {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"},
                values=raw_env,
            ),
            color=_env_choice(
                "LOG_COLOR",
                "on",
                {"on", "off", "auto"},
                values=raw_env,
            ),
            to_file=_env_bool("LOG_TO_FILE", False, values=raw_env),
            file_path=(
                str(raw_env.get("LOG_FILE_PATH", "data/bot.log")).strip()
                or "data/bot.log"
            ),
            file_max_bytes=max(
                1024,
                _env_int(
                    "LOG_FILE_MAX_BYTES",
                    5 * 1024 * 1024,
                    values=raw_env,
                ),
            ),
            file_backup_count=max(
                1,
                _env_int(
                    "LOG_FILE_BACKUP_COUNT",
                    3,
                    values=raw_env,
                ),
            ),
        ),
        prompts=PromptSettingsConfig.defaults(),
    )
