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
    enable_typing: bool = True
    enable_streaming: bool = True
    stream_chunk_size: int = 36
    stream_edit_interval_sec: float = 1.0
    auto_delete_minutes: int = 0
    decision_context_items: int = 5
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
    top_k: int = 3
    similarity_threshold: float = 0.55
    enable_fallback: bool = False
    enable_relaxed: bool = False
    min_reliable_score: float = 0.60


class ModerationConfig(BaseModel):
    enabled: bool = True
    warn_threshold: int = 3


class MemoryV2Config(BaseModel):
    enabled: bool = True
    working_recent_items: int = 50
    vector_backend: str = "qdrant"  # qdrant
    qdrant_host: str = "localhost"
    qdrant_port: int = 6333
    qdrant_collection_prefix: str = "chat_memory"
    hybrid_top_k: int = 20
    retrieval_candidate_multiplier: int = 3
    similarity_weight: float = 0.4
    time_weight: float = 0.3
    importance_weight: float = 0.3
    time_decay_factor: float = 0.95
    importance_llm_enabled: bool = True
    importance_llm_min: float = 0.3
    importance_llm_max: float = 0.7
    consolidation_enabled: bool = True
    consolidation_min_importance: float = 0.7
    prune_enabled: bool = True
    prune_days: int = 30
    max_concurrent_index_tasks: int = 2
    migrate_legacy_on_start: bool = True
    legacy_memory_dir: str = "memory"
    legacy_migration_marker: str = "data/memory_v2_legacy_migrated.flag"
    kg_enabled: bool = False
    kg_uri: str = ""
    kg_user: str = ""
    kg_password: str = ""


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
    bot_enable_typing: bool = True
    bot_enable_streaming: bool = True
    bot_stream_chunk_size: int = 36
    bot_stream_edit_interval_sec: float = 1.0
    bot_auto_delete_minutes: int = 0
    bot_decision_context_items: int = 5
    skill_sticker_file_ids: str = ""
    knowledge_top_k: int | None = None
    knowledge_similarity_threshold: float | None = None
    knowledge_enable_fallback: bool | None = None
    knowledge_enable_relaxed: bool | None = None
    knowledge_min_reliable_score: float | None = None
    database_url: str = "sqlite+aiosqlite:///./data/bot.db"

    memory_v2_enabled: bool = True
    memory_working_recent_items: int = 50
    memory_vector_backend: str = "qdrant"
    memory_qdrant_host: str = "localhost"
    memory_qdrant_port: int = 6333
    memory_qdrant_collection_prefix: str = "chat_memory"
    memory_hybrid_top_k: int = 20
    memory_retrieval_candidate_multiplier: int = 3
    memory_similarity_weight: float = 0.4
    memory_time_weight: float = 0.3
    memory_importance_weight: float = 0.3
    memory_time_decay_factor: float = 0.95
    memory_importance_llm_enabled: bool = True
    memory_importance_llm_min: float = 0.3
    memory_importance_llm_max: float = 0.7
    memory_consolidation_enabled: bool = True
    memory_consolidation_min_importance: float = 0.7
    memory_prune_enabled: bool = True
    memory_prune_days: int = 30
    memory_max_concurrent_index_tasks: int = 2
    memory_migrate_legacy_on_start: bool = True
    memory_legacy_memory_dir: str = "memory"
    memory_legacy_migration_marker: str = "data/memory_v2_legacy_migrated.flag"
    memory_kg_enabled: bool = False
    memory_kg_uri: str = ""
    memory_kg_user: str = ""
    memory_kg_password: str = ""

    av_enabled: bool = True
    av_http_timeout_sec: float = 15.0
    av_max_results: int = 18
    av_javbus_base_url: str = "https://www.javbus.com"
    av_madouqu_base_url: str = "https://madouqu.com"

    bot: BotConfig = BotConfig()
    knowledge: KnowledgeConfig = KnowledgeConfig()
    moderation: ModerationConfig = ModerationConfig()
    memory_v2: MemoryV2Config = MemoryV2Config()


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

    if "knowledge" in toml_data:
        settings.knowledge = KnowledgeConfig(**toml_data["knowledge"])
    if "moderation" in toml_data:
        settings.moderation = ModerationConfig(**toml_data["moderation"])

    settings.bot.token = settings.bot_token
    settings.bot.enable_typing = settings.bot_enable_typing
    settings.bot.enable_streaming = settings.bot_enable_streaming
    settings.bot.stream_chunk_size = max(8, settings.bot_stream_chunk_size)
    settings.bot.stream_edit_interval_sec = max(0.3, settings.bot_stream_edit_interval_sec)
    settings.bot.auto_delete_minutes = max(0, settings.bot_auto_delete_minutes)
    settings.bot.decision_context_items = min(20, max(0, settings.bot_decision_context_items))
    if settings.knowledge_top_k is not None:
        settings.knowledge.top_k = settings.knowledge_top_k
    if settings.knowledge_similarity_threshold is not None:
        settings.knowledge.similarity_threshold = settings.knowledge_similarity_threshold
    if settings.knowledge_enable_fallback is not None:
        settings.knowledge.enable_fallback = settings.knowledge_enable_fallback
    if settings.knowledge_enable_relaxed is not None:
        settings.knowledge.enable_relaxed = settings.knowledge_enable_relaxed
    if settings.knowledge_min_reliable_score is not None:
        settings.knowledge.min_reliable_score = settings.knowledge_min_reliable_score

    settings.knowledge.top_k = max(1, settings.knowledge.top_k)
    settings.knowledge.similarity_threshold = min(
        1.0,
        max(-1.0, settings.knowledge.similarity_threshold),
    )
    settings.knowledge.min_reliable_score = min(
        1.0,
        max(-1.0, settings.knowledge.min_reliable_score),
    )
    if settings.knowledge.min_reliable_score < settings.knowledge.similarity_threshold:
        settings.knowledge.min_reliable_score = settings.knowledge.similarity_threshold

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

    settings.memory_v2.enabled = settings.memory_v2_enabled
    settings.memory_v2.working_recent_items = max(10, settings.memory_working_recent_items)
    settings.memory_v2.vector_backend = settings.memory_vector_backend.strip().lower() or "qdrant"
    if settings.memory_v2.vector_backend != "qdrant":
        raise ValueError("MEMORY_VECTOR_BACKEND must be qdrant in Memory v2 architecture")
    settings.memory_v2.qdrant_host = settings.memory_qdrant_host.strip() or "localhost"
    settings.memory_v2.qdrant_port = max(1, settings.memory_qdrant_port)
    settings.memory_v2.qdrant_collection_prefix = (
        settings.memory_qdrant_collection_prefix.strip() or "chat_memory"
    )
    settings.memory_v2.hybrid_top_k = max(1, settings.memory_hybrid_top_k)
    settings.memory_v2.retrieval_candidate_multiplier = max(
        1,
        settings.memory_retrieval_candidate_multiplier,
    )
    settings.memory_v2.similarity_weight = max(0.0, settings.memory_similarity_weight)
    settings.memory_v2.time_weight = max(0.0, settings.memory_time_weight)
    settings.memory_v2.importance_weight = max(0.0, settings.memory_importance_weight)
    settings.memory_v2.time_decay_factor = min(1.0, max(0.0, settings.memory_time_decay_factor))
    settings.memory_v2.importance_llm_enabled = settings.memory_importance_llm_enabled
    settings.memory_v2.importance_llm_min = min(1.0, max(0.0, settings.memory_importance_llm_min))
    settings.memory_v2.importance_llm_max = min(1.0, max(0.0, settings.memory_importance_llm_max))
    settings.memory_v2.consolidation_enabled = settings.memory_consolidation_enabled
    settings.memory_v2.consolidation_min_importance = min(
        1.0,
        max(0.0, settings.memory_consolidation_min_importance),
    )
    settings.memory_v2.prune_enabled = settings.memory_prune_enabled
    settings.memory_v2.prune_days = max(1, settings.memory_prune_days)
    settings.memory_v2.max_concurrent_index_tasks = max(
        1,
        settings.memory_max_concurrent_index_tasks,
    )
    settings.memory_v2.migrate_legacy_on_start = settings.memory_migrate_legacy_on_start
    settings.memory_v2.legacy_memory_dir = (
        settings.memory_legacy_memory_dir.strip() or "memory"
    )
    settings.memory_v2.legacy_migration_marker = (
        settings.memory_legacy_migration_marker.strip()
        or "data/memory_v2_legacy_migrated.flag"
    )
    settings.memory_v2.kg_enabled = settings.memory_kg_enabled
    settings.memory_v2.kg_uri = settings.memory_kg_uri.strip()
    settings.memory_v2.kg_user = settings.memory_kg_user.strip()
    settings.memory_v2.kg_password = settings.memory_kg_password.strip()

    return settings
