from __future__ import annotations

import os
import re
import tomllib
from pathlib import Path

from dotenv import dotenv_values
from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class ProviderProfile(BaseModel):
    provider: str
    api_key: str | None = None
    api_base: str | None = None


class ChatEndpointConfig(BaseModel):
    model: str = "gemini/gemini-2.0-flash"
    api_key: str | None = None
    api_base: str | None = None
    temperature: float = 0.7
    max_tokens: int = 2048


class ModelConfig(ChatEndpointConfig):
    fallbacks: list[ChatEndpointConfig] = Field(default_factory=list)


class EmbedEndpointConfig(BaseModel):
    model: str = "gemini/text-embedding-004"
    api_key: str | None = None
    api_base: str | None = None


class EmbedConfig(EmbedEndpointConfig):
    fallbacks: list[EmbedEndpointConfig] = Field(default_factory=list)


class BotConfig(BaseModel):
    token: str = ""
    parse_mode: str = "HTML"
    drop_pending_updates: bool = True
    inbound_debounce_seconds: float = 5.0
    enable_typing: bool = True
    enable_streaming: bool = True
    stream_chunk_size: int = 36
    stream_edit_interval_sec: float = 1.0
    auto_delete_minutes: int = 0
    decision_context_items: int = 5
    proactive_default_enabled: bool = False
    proactive_idle_minutes: int = 180
    proactive_jitter_minutes: int = 60
    proactive_check_interval_seconds: float = 60.0
    proactive_quiet_hours_start: int = 0
    proactive_quiet_hours_end: int = 9
    proactive_retry_minutes: int = 30
    main_model: ModelConfig = ModelConfig()
    decision_model: ModelConfig = ModelConfig(
        model="gemini/gemini-2.0-flash",
        temperature=0.1,
        max_tokens=512,
    )
    moderation_model: ModelConfig = ModelConfig(
        model="gemini/gemini-2.0-flash",
        temperature=0.1,
        max_tokens=1024,
    )
    compress_model: ModelConfig = ModelConfig(
        model="gemini/gemini-2.0-flash",
        temperature=0.3,
        max_tokens=1024,
    )
    embed_model: EmbedConfig = EmbedConfig()
    max_context_tokens: int = 4096
    max_output_tokens: int = 2048


class ModerationConfig(BaseModel):
    enabled: bool = True
    warn_threshold: int = 3


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    bot_token: str = ""
    super_admin_id: int = 0

    # Role -> provider profile name + model.
    main_provider_name: str = ""
    main_model: str = "gemini-2.0-flash"
    main_fallbacks: str = ""

    decision_provider_name: str = ""
    decision_model: str = ""
    decision_fallbacks: str = ""

    moderation_provider_name: str = ""
    moderation_model: str = ""
    moderation_fallbacks: str = ""

    compress_provider_name: str = ""
    compress_model: str = ""
    compress_fallbacks: str = ""

    embed_provider_name: str = ""
    embed_model: str = "text-embedding-004"
    embed_fallbacks: str = ""

    max_context_tokens: int = 4096
    max_output_tokens: int = 2048
    bot_inbound_debounce_seconds: float = 5.0
    bot_enable_typing: bool = True
    bot_enable_streaming: bool = True
    bot_stream_chunk_size: int = 36
    bot_stream_edit_interval_sec: float = 1.0
    bot_auto_delete_minutes: int = 0
    bot_decision_context_items: int = 5
    bot_proactive_default_enabled: bool = False
    bot_proactive_idle_minutes: int = 180
    bot_proactive_jitter_minutes: int = 60
    bot_proactive_check_interval_seconds: float = 60.0
    bot_proactive_quiet_hours_start: int = 0
    bot_proactive_quiet_hours_end: int = 9
    bot_proactive_retry_minutes: int = 30
    skill_sticker_file_ids: str = ""
    database_url: str = "sqlite+aiosqlite:///./data/bot.db"

    av_enabled: bool = True
    av_http_timeout_sec: float = 15.0
    av_max_results: int = 18
    av_javbus_base_url: str = "https://www.javbus.com"
    av_madouqu_base_url: str = "https://madouqu.com"

    bot: BotConfig = BotConfig()
    moderation: ModerationConfig = ModerationConfig()


def _build_litellm_model(provider: str, model: str) -> str:
    """Build LiteLLM model string from provider + model name."""
    provider_norm = (provider or "").strip().lower()
    model_norm = (model or "").strip()
    if not model_norm:
        return model_norm
    if "/" in model_norm:
        return model_norm
    if provider_norm == "openai_compatible":
        return f"openai/{model_norm}"
    if provider_norm:
        return f"{provider_norm}/{model_norm}"
    return model_norm


def _load_raw_env(env_file: str = ".env") -> dict[str, str]:
    """Load env vars from .env + process env, process env takes precedence."""
    file_vars: dict[str, str] = {}
    p = Path(env_file)
    if p.exists():
        loaded = dotenv_values(p)
        file_vars = {k: str(v) for k, v in loaded.items() if k and v is not None}
    merged = {**file_vars, **os.environ}
    return {k.upper(): str(v) for k, v in merged.items() if k}


def _collect_provider_profiles(raw_env: dict[str, str]) -> dict[str, ProviderProfile]:
    """
    Parse provider profiles from env:
    MODEL_PROVIDER_<NAME>_PROVIDER
    MODEL_PROVIDER_<NAME>_API_KEY
    MODEL_PROVIDER_<NAME>_API_BASE
    """
    pattern = re.compile(r"^MODEL_PROVIDER_([A-Z0-9_]+)_(PROVIDER|API_KEY|API_BASE)$")
    grouped: dict[str, dict[str, str]] = {}

    for key, value in raw_env.items():
        m = pattern.match(key)
        if not m:
            continue
        name = m.group(1).lower()
        field = m.group(2).lower()
        grouped.setdefault(name, {})[field] = (value or "").strip()

    profiles: dict[str, ProviderProfile] = {}
    for name, fields in grouped.items():
        provider = (fields.get("provider") or "").strip().lower()
        if not provider:
            raise ValueError(f"MODEL_PROVIDER_{name.upper()}_PROVIDER is required")
        api_key = (fields.get("api_key") or "").strip() or None
        api_base = (fields.get("api_base") or "").strip() or None
        profiles[name] = ProviderProfile(provider=provider, api_key=api_key, api_base=api_base)

    return profiles


def _parse_fallbacks(raw: str) -> list[tuple[str, str | None]]:
    """
    Parse fallback specs:
    "name1:model1,name2:model2,name3"
    """
    specs: list[tuple[str, str | None]] = []
    for item in (raw or "").split(","):
        token = item.strip()
        if not token:
            continue
        if ":" in token:
            provider_name, model_name = token.split(":", 1)
            provider_name = provider_name.strip().lower()
            model_name = model_name.strip() or None
        else:
            provider_name = token.strip().lower()
            model_name = None
        if not provider_name:
            raise ValueError(f"invalid fallback item: {item!r}")
        specs.append((provider_name, model_name))
    return specs


def _get_profile(profiles: dict[str, ProviderProfile], provider_name: str) -> ProviderProfile:
    key = (provider_name or "").strip().lower()
    if not key:
        raise ValueError("provider name cannot be empty")
    profile = profiles.get(key)
    if not profile:
        raise ValueError(f"provider profile not found: {provider_name}")
    return profile


def _build_chat_config(
    *,
    profiles: dict[str, ProviderProfile],
    provider_name: str,
    model_name: str,
    temperature: float,
    max_tokens: int,
    fallback_spec: str,
) -> ModelConfig:
    if not provider_name:
        raise ValueError("provider name is required")
    if not model_name:
        raise ValueError("model name is required")

    profile = _get_profile(profiles, provider_name)
    cfg = ModelConfig(
        model=_build_litellm_model(profile.provider, model_name),
        api_key=profile.api_key,
        api_base=profile.api_base,
        temperature=temperature,
        max_tokens=max_tokens,
        fallbacks=[],
    )

    for fb_provider_name, fb_model_name in _parse_fallbacks(fallback_spec):
        fb_profile = _get_profile(profiles, fb_provider_name)
        cfg.fallbacks.append(
            ChatEndpointConfig(
                model=_build_litellm_model(fb_profile.provider, fb_model_name or model_name),
                api_key=fb_profile.api_key,
                api_base=fb_profile.api_base,
                temperature=temperature,
                max_tokens=max_tokens,
            )
        )
    return cfg


def _build_embed_config(
    *,
    profiles: dict[str, ProviderProfile],
    provider_name: str,
    model_name: str,
    fallback_spec: str,
) -> EmbedConfig:
    if not provider_name:
        raise ValueError("provider name is required")
    if not model_name:
        raise ValueError("model name is required")

    profile = _get_profile(profiles, provider_name)
    cfg = EmbedConfig(
        model=_build_litellm_model(profile.provider, model_name),
        api_key=profile.api_key,
        api_base=profile.api_base,
        fallbacks=[],
    )

    for fb_provider_name, fb_model_name in _parse_fallbacks(fallback_spec):
        fb_profile = _get_profile(profiles, fb_provider_name)
        cfg.fallbacks.append(
            EmbedEndpointConfig(
                model=_build_litellm_model(fb_profile.provider, fb_model_name or model_name),
                api_key=fb_profile.api_key,
                api_base=fb_profile.api_base,
            )
        )
    return cfg


def load_settings(config_path: str = "config.toml") -> Settings:
    """Load settings from TOML config + .env."""
    toml_data: dict = {}
    p = Path(config_path)
    if p.exists():
        with open(p, "rb") as f:
            toml_data = tomllib.load(f)

    settings = Settings()

    # Apply TOML overrides.
    if "bot" in toml_data:
        bot_data = toml_data["bot"]
        if "main_model" in bot_data:
            settings.bot.main_model = ModelConfig(**bot_data["main_model"])
        if "decision_model" in bot_data:
            settings.bot.decision_model = ModelConfig(**bot_data["decision_model"])
        if "moderation_model" in bot_data:
            settings.bot.moderation_model = ModelConfig(**bot_data["moderation_model"])
        if "compress_model" in bot_data:
            settings.bot.compress_model = ModelConfig(**bot_data["compress_model"])
        if "embed_model" in bot_data:
            settings.bot.embed_model = EmbedConfig(**bot_data["embed_model"])
        if "parse_mode" in bot_data:
            settings.bot.parse_mode = bot_data["parse_mode"]
        if "drop_pending_updates" in bot_data:
            settings.bot.drop_pending_updates = bot_data["drop_pending_updates"]

    if "moderation" in toml_data:
        settings.moderation = ModerationConfig(**toml_data["moderation"])

    settings.bot.token = settings.bot_token
    settings.bot.inbound_debounce_seconds = max(0.0, float(settings.bot_inbound_debounce_seconds))
    settings.bot.enable_typing = settings.bot_enable_typing
    settings.bot.enable_streaming = settings.bot_enable_streaming
    settings.bot.stream_chunk_size = max(8, settings.bot_stream_chunk_size)
    settings.bot.stream_edit_interval_sec = max(0.3, settings.bot_stream_edit_interval_sec)
    settings.bot.auto_delete_minutes = max(0, settings.bot_auto_delete_minutes)
    settings.bot.decision_context_items = min(20, max(0, settings.bot_decision_context_items))
    settings.bot.proactive_default_enabled = settings.bot_proactive_default_enabled
    settings.bot.proactive_idle_minutes = max(180, int(settings.bot_proactive_idle_minutes))
    settings.bot.proactive_jitter_minutes = max(0, int(settings.bot_proactive_jitter_minutes))
    settings.bot.proactive_check_interval_seconds = max(
        15.0, float(settings.bot_proactive_check_interval_seconds)
    )
    settings.bot.proactive_quiet_hours_start = min(23, max(0, int(settings.bot_proactive_quiet_hours_start)))
    settings.bot.proactive_quiet_hours_end = min(23, max(0, int(settings.bot_proactive_quiet_hours_end)))
    settings.bot.proactive_retry_minutes = max(5, int(settings.bot_proactive_retry_minutes))

    # New provider registry + role binding.
    raw_env = _load_raw_env()
    profiles = _collect_provider_profiles(raw_env)
    if not profiles:
        raise ValueError(
            "no provider profiles found, define MODEL_PROVIDER_<NAME>_PROVIDER/API_KEY/API_BASE in .env"
        )

    main_provider_name = settings.main_provider_name.strip().lower()
    if not main_provider_name:
        raise ValueError("MAIN_PROVIDER_NAME is required")
    main_model_name = settings.main_model.strip()
    if not main_model_name:
        raise ValueError("MAIN_MODEL is required")

    decision_provider_name = (settings.decision_provider_name or main_provider_name).strip().lower()
    moderation_provider_name = (
        settings.moderation_provider_name or decision_provider_name or main_provider_name
    ).strip().lower()
    compress_provider_name = (settings.compress_provider_name or main_provider_name).strip().lower()
    embed_provider_name = (settings.embed_provider_name or main_provider_name).strip().lower()

    decision_model_name = (settings.decision_model or main_model_name).strip()
    moderation_model_name = (settings.moderation_model or decision_model_name).strip()
    compress_model_name = (settings.compress_model or main_model_name).strip()
    embed_model_name = (settings.embed_model or "text-embedding-004").strip()

    settings.bot.main_model = _build_chat_config(
        profiles=profiles,
        provider_name=main_provider_name,
        model_name=main_model_name,
        temperature=settings.bot.main_model.temperature,
        max_tokens=max(1, settings.max_output_tokens),
        fallback_spec=settings.main_fallbacks,
    )
    settings.bot.decision_model = _build_chat_config(
        profiles=profiles,
        provider_name=decision_provider_name,
        model_name=decision_model_name,
        temperature=settings.bot.decision_model.temperature,
        max_tokens=settings.bot.decision_model.max_tokens,
        fallback_spec=settings.decision_fallbacks,
    )
    settings.bot.moderation_model = _build_chat_config(
        profiles=profiles,
        provider_name=moderation_provider_name,
        model_name=moderation_model_name,
        temperature=settings.bot.moderation_model.temperature,
        max_tokens=settings.bot.moderation_model.max_tokens,
        fallback_spec=settings.moderation_fallbacks,
    )
    settings.bot.compress_model = _build_chat_config(
        profiles=profiles,
        provider_name=compress_provider_name,
        model_name=compress_model_name,
        temperature=settings.bot.compress_model.temperature,
        max_tokens=settings.bot.compress_model.max_tokens,
        fallback_spec=settings.compress_fallbacks,
    )
    settings.bot.embed_model = _build_embed_config(
        profiles=profiles,
        provider_name=embed_provider_name,
        model_name=embed_model_name,
        fallback_spec=settings.embed_fallbacks,
    )

    settings.bot.max_context_tokens = settings.max_context_tokens
    settings.bot.max_output_tokens = settings.max_output_tokens
    settings.bot.main_model.max_tokens = max(1, settings.max_output_tokens)

    return settings
