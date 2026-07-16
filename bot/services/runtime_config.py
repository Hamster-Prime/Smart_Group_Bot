from __future__ import annotations

import asyncio
import base64
import hashlib
import inspect
import logging
import os
import tomllib
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any, Literal

from cryptography.fernet import Fernet, InvalidToken
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from bot.config import (
    EmbedConfig,
    ModelConfig,
    ModerationConfig,
    ProviderProfile,
    Settings,
    _build_chat_config,
    _build_embed_config,
    _collect_provider_profiles,
    _load_raw_env,
    _normalize_reasoning_effort,
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
    "sub2api.api_key",
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

    @field_validator("provider", mode="before")
    @classmethod
    def _clean_provider(cls, value: object) -> str:
        return str(value or "").strip().lower()

    @field_validator("model", mode="before")
    @classmethod
    def _clean_model(cls, value: object) -> str:
        return str(value or "").strip()


class ChatRoleConfig(StrictModel):
    provider: str = ""
    model: str = ""
    fallbacks: list[ModelFallbackConfig] = Field(default_factory=list)
    temperature: float = Field(default=0.7, ge=0.0, le=2.0, allow_inf_nan=False)
    max_tokens: int = Field(default=2048, ge=1, le=2_000_000)
    timeout_sec: float = Field(default=12.0, ge=1.0, le=600.0, allow_inf_nan=False)
    reasoning_effort: Literal["", "none", "minimal", "low", "medium", "high"] = ""

    @field_validator("provider", mode="before")
    @classmethod
    def _clean_provider(cls, value: object) -> str:
        return str(value or "").strip().lower()

    @field_validator("model", mode="before")
    @classmethod
    def _clean_model(cls, value: object) -> str:
        return str(value or "").strip()

    @field_validator("reasoning_effort", mode="before")
    @classmethod
    def _clean_effort(cls, value: object) -> str:
        return _normalize_reasoning_effort(str(value or ""))


class EmbedRoleConfig(StrictModel):
    provider: str = ""
    model: str = Field(default="text-embedding-004", min_length=1, max_length=255)
    fallbacks: list[ModelFallbackConfig] = Field(default_factory=list)
    timeout_sec: float = Field(default=10.0, ge=1.0, le=600.0, allow_inf_nan=False)

    @field_validator("provider", mode="before")
    @classmethod
    def _clean_provider(cls, value: object) -> str:
        return str(value or "").strip().lower()

    @field_validator("model", mode="before")
    @classmethod
    def _clean_model(cls, value: object) -> str:
        return str(value or "").strip()


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
            reasoning_effort="low",
        )
    )
    vision: ChatRoleConfig = Field(
        default_factory=lambda: ChatRoleConfig(
            temperature=0.7,
            max_tokens=2048,
            timeout_sec=15.0,
            reasoning_effort="none",
        )
    )
    decision: ChatRoleConfig = Field(
        default_factory=lambda: ChatRoleConfig(
            temperature=0.1,
            max_tokens=512,
            timeout_sec=6.0,
            reasoning_effort="none",
        )
    )
    moderation: ChatRoleConfig = Field(
        default_factory=lambda: ChatRoleConfig(
            temperature=0.1,
            max_tokens=1024,
            timeout_sec=8.0,
            reasoning_effort="none",
        )
    )
    compress: ChatRoleConfig = Field(
        default_factory=lambda: ChatRoleConfig(
            temperature=0.3,
            max_tokens=1024,
            timeout_sec=12.0,
            reasoning_effort="none",
        )
    )
    embed: EmbedRoleConfig = Field(default_factory=EmbedRoleConfig)
    retry_attempts: int = Field(default=2, ge=1, le=10)
    retry_backoff_sec: float = Field(default=0.8, ge=0.0, le=60.0, allow_inf_nan=False)
    retry_timeout_multiplier: float = Field(default=1.35, ge=1.0, le=10.0, allow_inf_nan=False)


class BotBehaviorConfig(StrictModel):
    model_config = ConfigDict(extra="forbid")

    parse_mode: str = Field(default="HTML", max_length=32)
    drop_pending_updates: bool = True
    inbound_debounce_seconds: float = Field(default=5.0, ge=0.0, le=60.0)
    enable_typing: bool = True
    enable_streaming: bool = True
    stream_chunk_size: int = Field(default=36, ge=8, le=4096)
    stream_edit_interval_sec: float = Field(default=1.0, ge=0.3, le=30.0)
    auto_delete_seconds: int = Field(default=0, ge=0, le=604800)
    auto_delete_categories: list[
        Literal["reply", "management", "moderation", "media", "proactive"]
    ] = Field(default_factory=lambda: ["management", "moderation"])
    # Accepted only while reading records written before the seconds migration.
    auto_delete_minutes: int | None = Field(default=None, ge=0, le=10080, exclude=True)
    decision_context_items: int = Field(default=5, ge=0, le=20)
    max_context_tokens: int = Field(default=256000, ge=1024, le=2_000_000)
    max_output_tokens: int = Field(default=2048, ge=256, le=2_000_000)
    proactive_default_enabled: bool = False
    proactive_idle_minutes: int = Field(default=180, ge=180, le=43200)
    proactive_jitter_minutes: int = Field(default=60, ge=0, le=1440)
    proactive_check_interval_seconds: float = Field(default=60.0, ge=15.0, le=3600.0)
    proactive_quiet_hours_start: int = Field(default=0, ge=0, le=23)
    proactive_quiet_hours_end: int = Field(default=9, ge=0, le=23)
    proactive_retry_minutes: int = Field(default=30, ge=5, le=1440)

    @model_validator(mode="after")
    def _migrate_auto_delete_minutes(self) -> BotBehaviorConfig:
        if self.auto_delete_seconds <= 0 and self.auto_delete_minutes:
            self.auto_delete_seconds = int(self.auto_delete_minutes) * 60
        self.auto_delete_categories = list(dict.fromkeys(self.auto_delete_categories))
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
    join_threshold: int = Field(default=8, ge=2, le=1000)
    window_seconds: int = Field(default=60, ge=5, le=3600)
    lockdown_seconds: int = Field(default=600, ge=60, le=86400)
    lookback_seconds: int = Field(default=300, ge=0, le=86400)
    challenge_timeout_seconds: int = Field(default=600, ge=60, le=86400)


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


class Sub2APISettingsConfig(StrictModel):
    enabled: bool = False
    base_url: str = Field(default="", max_length=1000)
    api_key: str = Field(default="", max_length=1024)
    http_timeout_sec: float = Field(default=15.0, ge=1.0, le=300.0)
    check_timeout_sec: float = Field(default=45.0, ge=1.0, le=600.0)


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
    file_path: str = Field(default="bot.log", max_length=1000)
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
    verification: VerificationSettingsConfig = Field(default_factory=VerificationSettingsConfig)
    tts: TTSSettingsConfig = Field(default_factory=TTSSettingsConfig)
    music: MusicSettingsConfig = Field(default_factory=MusicSettingsConfig)
    sub2api: Sub2APISettingsConfig = Field(default_factory=Sub2APISettingsConfig)
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
            "sub2api.api_key": self.sub2api.api_key,
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
        clone.sub2api.api_key = secrets.get("sub2api.api_key", "")
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
            fallback_spec=self._fallback_spec(main.fallbacks),
            reasoning_effort=main.reasoning_effort,
            **common_retry,
        )
        settings.bot.vision_model = _build_chat_config(
            profiles=profiles,
            provider_name=vision_provider,
            model_name=vision_model,
            temperature=models.vision.temperature,
            max_tokens=models.vision.max_tokens,
            timeout_sec=models.vision.timeout_sec,
            fallback_spec=self._fallback_spec(models.vision.fallbacks),
            reasoning_effort=models.vision.reasoning_effort,
            **common_retry,
        )
        settings.bot.decision_model = _build_chat_config(
            profiles=profiles,
            provider_name=decision_provider,
            model_name=decision_model,
            temperature=models.decision.temperature,
            max_tokens=models.decision.max_tokens,
            timeout_sec=models.decision.timeout_sec,
            fallback_spec=self._fallback_spec(models.decision.fallbacks),
            reasoning_effort=models.decision.reasoning_effort,
            **common_retry,
        )
        settings.bot.moderation_model = _build_chat_config(
            profiles=profiles,
            provider_name=moderation_provider,
            model_name=moderation_model,
            temperature=models.moderation.temperature,
            max_tokens=models.moderation.max_tokens,
            timeout_sec=models.moderation.timeout_sec,
            fallback_spec=self._fallback_spec(models.moderation.fallbacks),
            reasoning_effort=models.moderation.reasoning_effort,
            **common_retry,
        )
        settings.bot.compress_model = _build_chat_config(
            profiles=profiles,
            provider_name=compress_provider,
            model_name=compress_model,
            temperature=models.compress.temperature,
            max_tokens=models.compress.max_tokens,
            timeout_sec=models.compress.timeout_sec,
            fallback_spec=self._fallback_spec(models.compress.fallbacks),
            reasoning_effort=models.compress.reasoning_effort,
            **common_retry,
        )
        settings.bot.embed_model = _build_embed_config(
            profiles=profiles,
            provider_name=embed_provider,
            model_name=models.embed.model,
            timeout_sec=models.embed.timeout_sec,
            fallback_spec=self._fallback_spec(models.embed.fallbacks),
            **common_retry,
        )

        bot = self.bot
        settings.bot.parse_mode = bot.parse_mode
        settings.bot.drop_pending_updates = bot.drop_pending_updates
        settings.bot.inbound_debounce_seconds = bot.inbound_debounce_seconds
        settings.bot.enable_typing = bot.enable_typing
        settings.bot.enable_streaming = bot.enable_streaming
        settings.bot.stream_chunk_size = bot.stream_chunk_size
        settings.bot.stream_edit_interval_sec = bot.stream_edit_interval_sec
        settings.bot.auto_delete_seconds = bot.auto_delete_seconds
        settings.bot.auto_delete_minutes = bot.auto_delete_seconds // 60
        settings.bot.auto_delete_categories = list(bot.auto_delete_categories)
        settings.bot.decision_context_items = bot.decision_context_items
        settings.bot.max_context_tokens = bot.max_context_tokens
        settings.bot.max_output_tokens = bot.max_output_tokens
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
        settings.raid_guard_join_threshold = self.raid_guard.join_threshold
        settings.raid_guard_window_seconds = self.raid_guard.window_seconds
        settings.raid_guard_lockdown_seconds = self.raid_guard.lockdown_seconds
        settings.raid_guard_lookback_seconds = self.raid_guard.lookback_seconds
        settings.raid_guard_challenge_timeout_seconds = (
            self.raid_guard.challenge_timeout_seconds
        )
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
        settings.sub2api_enabled = self.sub2api.enabled
        settings.sub2api_base_url = self.sub2api.base_url
        settings.sub2api_api_key = self.sub2api.api_key
        settings.sub2api_http_timeout_sec = self.sub2api.http_timeout_sec
        settings.sub2api_check_timeout_sec = self.sub2api.check_timeout_sec
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
                    config = RuntimeConfig.model_validate(row.payload or {})
                    secrets = await self._load_secret_map(session)
                    config = config.with_secrets(secrets)
                    revision = int(row.revision or 1)

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
            candidate = RuntimeConfig.model_validate(payload)
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
                "bot.drop_pending_updates",
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


def _legacy_fallbacks(raw: str) -> list[ModelFallbackConfig]:
    return [
        ModelFallbackConfig(provider=provider, model=model or "")
        for provider, model in _parse_fallbacks(raw)
    ]


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
        if "drop_pending_updates" in bot_data:
            settings.bot.drop_pending_updates = bool(bot_data["drop_pending_updates"])
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
            fallbacks=_legacy_fallbacks(settings.main_fallbacks),
            temperature=settings.bot.main_model.temperature,
            max_tokens=max(1, settings.max_output_tokens),
            timeout_sec=settings.main_timeout_sec,
            reasoning_effort=settings.main_reasoning_effort,
        ),
        vision=ChatRoleConfig(
            provider=role_provider(settings.vision_provider_name),
            model=settings.vision_model.strip(),
            fallbacks=_legacy_fallbacks(settings.vision_fallbacks),
            temperature=settings.bot.vision_model.temperature,
            max_tokens=settings.bot.vision_model.max_tokens,
            timeout_sec=settings.vision_timeout_sec,
            reasoning_effort=settings.vision_reasoning_effort,
        ),
        decision=ChatRoleConfig(
            provider=role_provider(settings.decision_provider_name),
            model=settings.decision_model.strip(),
            fallbacks=_legacy_fallbacks(settings.decision_fallbacks),
            temperature=settings.bot.decision_model.temperature,
            max_tokens=settings.bot.decision_model.max_tokens,
            timeout_sec=settings.decision_timeout_sec,
            reasoning_effort=settings.decision_reasoning_effort,
        ),
        moderation=ChatRoleConfig(
            provider=role_provider(settings.moderation_provider_name),
            model=settings.moderation_model.strip(),
            fallbacks=_legacy_fallbacks(settings.moderation_fallbacks),
            temperature=settings.bot.moderation_model.temperature,
            max_tokens=settings.bot.moderation_model.max_tokens,
            timeout_sec=settings.moderation_timeout_sec,
            reasoning_effort=settings.moderation_reasoning_effort,
        ),
        compress=ChatRoleConfig(
            provider=role_provider(settings.compress_provider_name),
            model=settings.compress_model.strip(),
            fallbacks=_legacy_fallbacks(settings.compress_fallbacks),
            temperature=settings.bot.compress_model.temperature,
            max_tokens=settings.bot.compress_model.max_tokens,
            timeout_sec=settings.compress_timeout_sec,
            reasoning_effort=settings.compress_reasoning_effort,
        ),
        embed=EmbedRoleConfig(
            provider=role_provider(settings.embed_provider_name),
            model=settings.embed_model.strip() or "text-embedding-004",
            fallbacks=_legacy_fallbacks(settings.embed_fallbacks),
            timeout_sec=settings.embed_timeout_sec,
        ),
        retry_attempts=settings.llm_retry_attempts,
        retry_backoff_sec=settings.llm_retry_backoff_sec,
        retry_timeout_multiplier=settings.llm_retry_timeout_multiplier,
    )

    return RuntimeConfig(
        models=model_settings,
        bot=BotBehaviorConfig(
            parse_mode=settings.bot.parse_mode,
            drop_pending_updates=settings.bot.drop_pending_updates,
            inbound_debounce_seconds=settings.bot_inbound_debounce_seconds,
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
            decision_context_items=settings.bot_decision_context_items,
            max_context_tokens=settings.max_context_tokens,
            max_output_tokens=settings.max_output_tokens,
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
            join_threshold=settings.raid_guard_join_threshold,
            window_seconds=settings.raid_guard_window_seconds,
            lockdown_seconds=settings.raid_guard_lockdown_seconds,
            lookback_seconds=settings.raid_guard_lookback_seconds,
            challenge_timeout_seconds=settings.raid_guard_challenge_timeout_seconds,
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
        sub2api=Sub2APISettingsConfig(
            enabled=settings.sub2api_enabled,
            base_url=settings.sub2api_base_url,
            api_key=settings.sub2api_api_key,
            http_timeout_sec=settings.sub2api_http_timeout_sec,
            check_timeout_sec=settings.sub2api_check_timeout_sec,
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
            file_path=str(raw_env.get("LOG_FILE_PATH", "bot.log")).strip() or "bot.log",
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
