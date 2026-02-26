from __future__ import annotations

import tomllib
from pathlib import Path
from pydantic import BaseModel
from pydantic_settings import BaseSettings, SettingsConfigDict


class ModelConfig(BaseModel):
    model: str = "gemini/gemini-2.0-flash"
    api_key: str | None = None
    api_base: str | None = None
    temperature: float = 0.7
    max_tokens: int = 2048


class EmbedConfig(BaseModel):
    model: str = "gemini/text-embedding-004"
    api_key: str | None = None
    api_base: str | None = None


class BotConfig(BaseModel):
    token: str = ""
    parse_mode: str = "HTML"
    drop_pending_updates: bool = True
    enable_typing: bool = True
    enable_streaming: bool = True
    stream_chunk_size: int = 36
    stream_edit_interval_sec: float = 1.0
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


class KnowledgeConfig(BaseModel):
    top_k: int = 5
    similarity_threshold: float = 0.3


class ModerationConfig(BaseModel):
    enabled: bool = True
    warn_threshold: int = 3


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    bot_token: str = ""
    super_admin_id: int = 0
    main_provider: str = "gemini"
    main_model: str = "gemini-2.0-flash"
    main_api_key: str = ""
    main_api_base: str = ""
    decision_provider: str = ""
    decision_model: str = ""
    decision_api_key: str = ""
    decision_api_base: str = ""
    moderation_provider: str = ""
    moderation_model: str = ""
    moderation_api_key: str = ""
    moderation_api_base: str = ""
    compress_provider: str = ""
    compress_model: str = ""
    compress_api_key: str = ""
    compress_api_base: str = ""
    embed_provider: str = ""
    embed_model: str = "text-embedding-004"
    embed_api_key: str = ""
    embed_api_base: str = ""
    max_context_tokens: int = 4096
    max_output_tokens: int = 2048
    bot_enable_typing: bool = True
    bot_enable_streaming: bool = True
    bot_stream_chunk_size: int = 36
    bot_stream_edit_interval_sec: float = 1.0
    database_url: str = "sqlite+aiosqlite:///./data/bot.db"

    bot: BotConfig = BotConfig()
    knowledge: KnowledgeConfig = KnowledgeConfig()
    moderation: ModerationConfig = ModerationConfig()


def _build_litellm_model(provider: str, model: str) -> str:
    """Build litellm model string from provider + model name."""
    if provider in ("gemini", "openai"):
        return f"{provider}/{model}"
    if provider == "openai_compatible":
        return f"openai/{model}"
    return model


def load_settings(config_path: str = "config.toml") -> Settings:
    """Load settings from TOML config + .env"""
    toml_data: dict = {}
    p = Path(config_path)
    if p.exists():
        with open(p, "rb") as f:
            toml_data = tomllib.load(f)

    settings = Settings()

    # Apply TOML overrides
    if "bot" in toml_data:
        bot_data = toml_data["bot"]
        if "main_model" in bot_data:
            settings.bot.main_model = ModelConfig(**bot_data["main_model"])
        if "decision_model" in bot_data:
            settings.bot.decision_model = ModelConfig(**bot_data["decision_model"])
        if "moderation_model" in bot_data:
            settings.bot.moderation_model = ModelConfig(**bot_data["moderation_model"])
        if "parse_mode" in bot_data:
            settings.bot.parse_mode = bot_data["parse_mode"]
        if "drop_pending_updates" in bot_data:
            settings.bot.drop_pending_updates = bot_data["drop_pending_updates"]

    if "knowledge" in toml_data:
        settings.knowledge = KnowledgeConfig(**toml_data["knowledge"])
    if "moderation" in toml_data:
        settings.moderation = ModerationConfig(**toml_data["moderation"])

    # Set bot token
    settings.bot.token = settings.bot_token
    settings.bot.enable_typing = settings.bot_enable_typing
    settings.bot.enable_streaming = settings.bot_enable_streaming
    settings.bot.stream_chunk_size = max(8, settings.bot_stream_chunk_size)
    settings.bot.stream_edit_interval_sec = max(0.3, settings.bot_stream_edit_interval_sec)

    # Build main model config from env
    mc = settings.bot.main_model
    mc.model = _build_litellm_model(settings.main_provider, settings.main_model)
    mc.api_key = settings.main_api_key or mc.api_key
    if settings.main_api_base:
        mc.api_base = settings.main_api_base

    # Build decision model config (blank => reuse main model config)
    dc = settings.bot.decision_model
    d_provider = settings.decision_provider or settings.main_provider
    d_model = settings.decision_model or settings.main_model
    dc.model = _build_litellm_model(d_provider, d_model)
    dc.api_key = settings.decision_api_key or settings.main_api_key or dc.api_key
    if settings.decision_api_base:
        dc.api_base = settings.decision_api_base
    elif d_provider == "openai_compatible" and settings.main_api_base:
        dc.api_base = settings.main_api_base
    # Build moderation model config (leave blank to reuse decision, then main)
    moc = settings.bot.moderation_model
    m_provider = settings.moderation_provider or settings.decision_provider or settings.main_provider
    m_model = settings.moderation_model or settings.decision_model or settings.main_model
    moc.model = _build_litellm_model(m_provider, m_model)
    moc.api_key = (
        settings.moderation_api_key
        or settings.decision_api_key
        or settings.main_api_key
        or moc.api_key
    )
    if settings.moderation_api_base:
        moc.api_base = settings.moderation_api_base
    elif m_provider == "openai_compatible":
        moc.api_base = settings.decision_api_base or settings.main_api_base or moc.api_base

    # Build compress model config (blank => reuse main model config)
    cc = settings.bot.compress_model
    c_provider = settings.compress_provider or settings.main_provider
    c_model = settings.compress_model or settings.main_model
    cc.model = _build_litellm_model(c_provider, c_model)
    cc.api_key = settings.compress_api_key or settings.main_api_key or cc.api_key
    if settings.compress_api_base:
        cc.api_base = settings.compress_api_base
    elif c_provider == "openai_compatible" and settings.main_api_base:
        cc.api_base = settings.main_api_base

    # Build embed model config (blank => reuse main model config)
    ec = settings.bot.embed_model
    e_provider = settings.embed_provider or settings.main_provider
    e_model = settings.embed_model or "text-embedding-004"
    ec.model = _build_litellm_model(e_provider, e_model)
    ec.api_key = settings.embed_api_key or settings.main_api_key or ec.api_key
    if settings.embed_api_base:
        ec.api_base = settings.embed_api_base
    elif e_provider == "openai_compatible" and settings.main_api_base:
        ec.api_base = settings.main_api_base

    # Context token limits
    settings.bot.max_context_tokens = settings.max_context_tokens
    settings.bot.max_output_tokens = settings.max_output_tokens
    mc.max_tokens = settings.max_output_tokens

    return settings
