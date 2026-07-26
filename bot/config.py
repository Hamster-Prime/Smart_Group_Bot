from __future__ import annotations

import os
import re
import tomllib
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

try:
    from dotenv import dotenv_values
except ModuleNotFoundError:
    def dotenv_values(path: str | Path) -> dict[str, str]:
        values: dict[str, str] = {}
        file_path = Path(path)
        if not file_path.exists():
            return values
        for raw_line in file_path.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip()
            value = value.strip().strip("'").strip('"')
            if key:
                values[key] = value
        return values


class ProviderProfile(BaseModel):
    provider: str
    api_key: str | None = None
    api_base: str | None = None
    stream: bool = False
    chat_endpoint: Literal["chat_completions", "responses"] = "chat_completions"
    endpoint_path: str = "/chat/completions"


class ChatEndpointConfig(BaseModel):
    model: str = "gemini/gemini-2.0-flash"
    provider: str = ""
    api_key: str | None = None
    api_base: str | None = None
    stream: bool = False
    chat_endpoint: Literal["chat_completions", "responses"] = "chat_completions"
    endpoint_path: str = "/chat/completions"
    temperature: float = 0.7
    max_tokens: int = 2048
    timeout_sec: float = 12.0
    retry_attempts: int = 2
    retry_backoff_sec: float = 0.8
    retry_timeout_multiplier: float = 1.35
    # "" = do not send the param; none/minimal/low/medium/high are forwarded.
    reasoning_effort: str = ""


class ModelConfig(ChatEndpointConfig):
    fallbacks: list[ChatEndpointConfig] = Field(default_factory=list)


class EmbedEndpointConfig(BaseModel):
    model: str = "gemini/text-embedding-004"
    provider: str = ""
    api_key: str | None = None
    api_base: str | None = None
    endpoint_path: str = "/v1beta/models"
    timeout_sec: float = 10.0
    retry_attempts: int = 2
    retry_backoff_sec: float = 0.8
    retry_timeout_multiplier: float = 1.25


class EmbedConfig(EmbedEndpointConfig):
    fallbacks: list[EmbedEndpointConfig] = Field(default_factory=list)


class BotConfig(BaseModel):
    token: str = ""
    parse_mode: str = "HTML"
    disable_link_preview: bool = True
    drop_pending_updates: bool = False
    inbound_debounce_seconds: float = 5.0
    reply_batch_timeout_seconds: float = 45.0
    enable_typing: bool = True
    enable_streaming: bool = True
    stream_chunk_size: int = 36
    stream_edit_interval_sec: float = 1.0
    auto_delete_seconds: int = 0
    # Deprecated compatibility alias for integrations that still set minutes.
    auto_delete_minutes: int = 0
    auto_delete_categories: list[str] = Field(
        default_factory=lambda: ["management", "moderation"]
    )
    # Per-category retention overrides (seconds); 0/missing inherits
    # auto_delete_seconds.
    auto_delete_category_seconds: dict[str, int] = Field(default_factory=dict)
    # Per-category cleanup mode: missing/"timer" schedules the delayed
    # delete; "button" attaches an inline delete button instead.
    auto_delete_category_mode: dict[str, str] = Field(default_factory=dict)
    decision_context_items: int = 5
    proactive_default_enabled: bool = False
    proactive_idle_minutes: int = 180
    proactive_jitter_minutes: int = 60
    proactive_check_interval_seconds: float = 60.0
    proactive_quiet_hours_start: int = 0
    proactive_quiet_hours_end: int = 9
    proactive_retry_minutes: int = 30
    main_model: ModelConfig = ModelConfig()
    vision_model: ModelConfig = ModelConfig()
    decision_model: ModelConfig = ModelConfig(
        model="gemini/gemini-2.0-flash",
        temperature=0.1,
        max_tokens=512,
        timeout_sec=6.0,
    )
    moderation_model: ModelConfig = ModelConfig(
        model="gemini/gemini-2.0-flash",
        temperature=0.1,
        max_tokens=1024,
        timeout_sec=8.0,
    )
    compress_model: ModelConfig = ModelConfig(
        model="gemini/gemini-2.0-flash",
        temperature=0.3,
        max_tokens=1024,
        timeout_sec=12.0,
    )
    embed_model: EmbedConfig = EmbedConfig()
    max_context_tokens: int = 256000
    max_output_tokens: int = 2048


class ModerationConfig(BaseModel):
    enabled: bool = True
    warn_threshold: int = 3
    # 违规判定置信度 >= 该值时直接按规则动作处理；低于该值时删除消息并
    # 要求真人质询（质询依赖所选验证服务和公网地址配置）。
    high_confidence_threshold: float = Field(default=0.9, allow_inf_nan=False)
    # 低置信度质询限时（秒），超时自动封禁。
    challenge_timeout_seconds: int = 600
    # 其他 bot（如 guest 模式广告机）发送的消息也进入内容审核；
    # 连续 bot_screening_message_count 条干净后加入白名单不再审核。
    bot_screening_enabled: bool = True
    bot_screening_message_count: int = 5


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    bot_token: str = ""
    super_admin_id: int = 0
    config_master_key: str = ""
    miniapp_public_base_url: str = ""
    miniapp_listen_host: str = ""
    miniapp_listen_port: int = 0
    webhook_url: str = ""
    webhook_secret: str = ""

    @field_validator("super_admin_id", mode="before")
    @classmethod
    def _empty_env_int_as_zero(cls, value: object) -> object:
        # .env templates ship these as blank lines; treat "" as unset.
        if isinstance(value, str) and not value.strip():
            return 0
        return value

    # Role -> provider profile name + model.
    main_provider_name: str = ""
    main_model: str = "gemini-2.0-flash"
    main_fallbacks: str = ""
    main_timeout_sec: float = 12.0
    main_reasoning_effort: str = "low"

    vision_provider_name: str = ""
    vision_model: str = ""
    vision_fallbacks: str = ""
    vision_timeout_sec: float = 15.0
    vision_reasoning_effort: str = "none"

    decision_provider_name: str = ""
    decision_model: str = ""
    decision_fallbacks: str = ""
    decision_timeout_sec: float = 6.0
    decision_reasoning_effort: str = "none"

    moderation_provider_name: str = ""
    moderation_model: str = ""
    moderation_fallbacks: str = ""
    moderation_timeout_sec: float = 8.0
    moderation_reasoning_effort: str = "none"

    compress_provider_name: str = ""
    compress_model: str = ""
    compress_fallbacks: str = ""
    compress_timeout_sec: float = 12.0
    compress_reasoning_effort: str = "none"

    embed_provider_name: str = ""
    embed_model: str = "text-embedding-004"
    embed_fallbacks: str = ""
    embed_timeout_sec: float = 10.0
    llm_retry_attempts: int = 2
    llm_retry_backoff_sec: float = 0.8
    llm_retry_timeout_multiplier: float = 1.35

    max_context_tokens: int = 256000
    max_output_tokens: int = 2048
    bot_inbound_debounce_seconds: float = 5.0
    bot_reply_batch_timeout_seconds: float = 45.0
    bot_enable_typing: bool = True
    bot_enable_streaming: bool = True
    bot_stream_chunk_size: int = 36
    bot_stream_edit_interval_sec: float = 1.0
    bot_auto_delete_seconds: int = 0
    # Legacy one-time migration input. Runtime settings use seconds.
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

    doubao_tts_enabled: bool = False
    doubao_tts_http_timeout_sec: float = 20.0
    doubao_tts_max_text_length: int = 500
    doubao_tts_api_base: str = "https://openspeech.bytedance.com"
    doubao_tts_app_id: str = ""
    doubao_tts_app_key: str = ""
    doubao_tts_access_key: str = ""
    doubao_tts_resource_id: str = "seed-tts-2.0"
    doubao_tts_model: str = ""
    doubao_tts_speaker: str = ""
    doubao_tts_audio_format: str = "ogg_opus"
    doubao_tts_sample_rate: int = 48000
    doubao_tts_bit_rate: int = 96000
    doubao_tts_emotion: str = ""
    doubao_tts_emotion_scale: int = 4
    doubao_tts_speech_rate: int = 0
    doubao_tts_loudness_rate: int = 0
    doubao_tts_silence_duration_ms: int = 0

    music_api_enabled: bool = True
    music_api_http_timeout_sec: float = 15.0
    music_api_base_url: str = "https://music-api.gdstudio.xyz/api.php"
    music_api_default_source: str = "kuwo"
    music_api_stable_sources: str = "kuwo,netease,joox,bilibili"

    movie_info_enabled: bool = False
    movie_info_http_timeout_sec: float = 6.0
    movie_info_max_results: int = 6
    movie_info_default_language: str = "zh-CN"
    movie_info_default_region: str = "CN"
    movie_info_tmdb_read_access_token: str = ""
    movie_info_imdb_data_set_id: str = ""
    movie_info_imdb_revision_id: str = ""
    movie_info_imdb_asset_id: str = ""
    movie_info_imdb_api_key: str = ""
    movie_info_imdb_aws_access_key_id: str = ""
    movie_info_imdb_aws_secret_access_key: str = ""
    movie_info_imdb_aws_session_token: str = ""

    av_enabled: bool = True
    av_http_timeout_sec: float = 15.0
    av_max_results: int = 18
    av_javbus_base_url: str = "https://www.javbus.com"
    av_madouqu_base_url: str = "https://madouqu.com"
    av_dmm_base_url: str = "https://www.dmm.co.jp"
    av_fc2_base_url: str = "https://adult.contents.fc2.com"

    # 入群验证：新成员先全员禁言，私聊 bot 获取链接并通过
    # 通过所选真人验证服务后恢复权限。
    join_verification_enabled: bool = False
    join_verification_timeout_seconds: int = 600
    join_verification_check_interval_seconds: float = 30.0
    join_verification_provider: Literal[
        "turnstile", "hcaptcha", "turnstile_hcaptcha"
    ] = "turnstile"
    join_verification_turnstile_site_key: str = ""
    join_verification_turnstile_secret_key: str = ""
    join_verification_hcaptcha_site_key: str = ""
    join_verification_hcaptcha_secret_key: str = ""
    # 验证页面对外可访问的地址（反代/隧道后的 https 地址）。
    join_verification_public_base_url: str = ""
    join_verification_listen_host: str = "0.0.0.0"
    join_verification_listen_port: int = 8480

    # 资料自动巡检：按名单批量复查所有已知成员的名字/简介。
    patrol_enabled: bool = False
    patrol_schedule_time: str = "04:30"
    patrol_batch_size: int = 500
    patrol_batch_pause_seconds: float = 5.0
    patrol_fetch_bio: bool = True
    patrol_challenge_timeout_seconds: int = 600
    patrol_check_interval_seconds: float = 60.0

    # 爆破防护：短窗口内大量入群自动锁群，并追溯质询爆破前入群的成员。
    raid_guard_enabled: bool = False
    raid_guard_pin_message: bool = True
    raid_guard_join_threshold: int = 8
    raid_guard_window_seconds: int = 60
    raid_guard_lockdown_seconds: int = 600
    raid_guard_lookback_seconds: int = 300
    raid_guard_challenge_timeout_seconds: int = 600

    # 呼叫管理员：群成员发送 @admin 时 @ 全部（或选定）群管理员。
    call_admin_enabled: bool = True
    call_admin_pin_message: bool = False
    call_admin_cooldown_seconds: int = 60

    # 骚扰民主投票封禁：回复消息发起投票，达到阈值即封禁被回复用户。
    vote_ban_enabled: bool = False
    vote_ban_pin_message: bool = True
    vote_ban_threshold: int = 5
    vote_ban_duration_seconds: int = 1800
    vote_ban_trigger_limit: int = 3
    vote_ban_trigger_window_seconds: int = 3600

    bot: BotConfig = BotConfig()
    moderation: ModerationConfig = ModerationConfig()


# Common vendor names people type that map onto litellm's native provider ids.
_PROVIDER_ALIASES = {
    "google": "gemini",
    "claude": "anthropic",
    "minimaxi": "minimax",
    "kimi": "moonshot",
    "moonshotai": "moonshot",
    "doubao": "volcengine",
    "ark": "volcengine",
    "qwen": "dashscope",
    "alibaba": "dashscope",
    "grok": "xai",
}


def _canonical_provider(provider: str) -> str:
    normalized = (provider or "").strip().lower()
    return _PROVIDER_ALIASES.get(normalized, normalized)


def _litellm_supports_provider(provider: str) -> bool:
    try:
        import litellm

        known = {str(getattr(item, "value", item)) for item in litellm.provider_list}
    except Exception:
        return True
    return provider in known


def _build_litellm_model(provider: str, model: str, *, api_base: str | None = None) -> str:
    """Build LiteLLM model string from provider + model name.

    Most native LiteLLM providers, including `anthropic`, use `<provider>/<model>`.
    OpenAI-compatible gateways — explicit (`openai_compatible`) or implied by a
    provider name litellm has no native adapter for while a custom api_base is
    configured — are normalized to the `openai/<model>` prefix.
    """
    provider_norm = _canonical_provider(provider)
    model_norm = (model or "").strip()
    if not model_norm:
        return model_norm
    if "/" in model_norm:
        return model_norm
    if provider_norm == "openai_compatible":
        return f"openai/{model_norm}"
    if (
        provider_norm
        and str(api_base or "").strip()
        and not _litellm_supports_provider(provider_norm)
    ):
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


def _env_truthy(value: str | None) -> bool:
    normalized = (value or "").strip().lower()
    return normalized in {"1", "true", "yes", "on", "y", "t"}


_VALID_REASONING_EFFORTS = {"none", "minimal", "low", "medium", "high"}
_REASONING_EFFORT_ALIASES = {
    "off": "none",
    "disable": "none",
    "disabled": "none",
    "0": "none",
    "false": "none",
}


def _normalize_reasoning_effort(raw: str | None) -> str:
    value = (raw or "").strip().lower()
    if not value:
        return ""
    value = _REASONING_EFFORT_ALIASES.get(value, value)
    if value in _VALID_REASONING_EFFORTS:
        return value
    return ""


_API_BASE_SUFFIX_RULES: tuple[
    tuple[str, str, str, Literal["chat_completions", "responses"]],
    ...,
] = (
    ("/v1/chat/completions", "/chat/completions", "openai", "chat_completions"),
    ("/chat/completions", "/chat/completions", "openai", "chat_completions"),
    ("/v1/responses", "/responses", "openai", "responses"),
    ("/responses", "/responses", "openai", "responses"),
    ("/v1/messages", "/v1/messages", "anthropic", "chat_completions"),
    ("/messages", "/messages", "anthropic", "chat_completions"),
    ("/v1beta/models", "/models", "gemini", "chat_completions"),
    ("/v1/models", "/models", "gemini", "chat_completions"),
)


def _normalize_chat_endpoint(provider: str, raw_value: str | None) -> Literal["chat_completions", "responses"]:
    provider_norm = (provider or "").strip().lower()
    value = (raw_value or "").strip().lower()

    if not value:
        return "responses" if provider_norm == "openai" else "chat_completions"
    if value in {"/chat/completions", "chat/completions", "chat_completions", "chat-completions", "chat"}:
        return "chat_completions"
    if value in {"/responses", "responses", "response"}:
        if provider_norm not in {"openai", "openai_compatible"}:
            raise ValueError(
                f"MODEL_PROVIDER_<NAME>_CHAT_ENDPOINT=/responses is only supported for openai/openai_compatible, got {provider!r}"
            )
        return "responses"
    raise ValueError(
        f"invalid MODEL_PROVIDER_<NAME>_CHAT_ENDPOINT value: {raw_value!r}; expected /chat/completions or /responses"
    )


def _normalize_api_base(raw_value: str | None) -> str | None:
    value = (raw_value or "").strip()
    if not value:
        return None
    return value.rstrip("/")


def _default_endpoint_path(provider: str, chat_endpoint: Literal["chat_completions", "responses"]) -> str:
    provider_norm = (provider or "").strip().lower()
    if provider_norm == "anthropic":
        return "/v1/messages"
    if provider_norm == "gemini":
        return "/v1beta/models"
    if chat_endpoint == "responses":
        return "/responses"
    return "/chat/completions"


def _infer_provider_profile_from_api_base(
    provider: str,
    raw_api_base: str | None,
) -> tuple[str, str | None, Literal["chat_completions", "responses"] | None, str | None]:
    provider_norm = _canonical_provider(provider)
    api_base = _normalize_api_base(raw_api_base)
    if not api_base:
        return provider_norm, None, None, None

    lower_base = api_base.lower()
    for match_suffix, strip_suffix, inferred_provider, inferred_endpoint in _API_BASE_SUFFIX_RULES:
        if not lower_base.endswith(match_suffix):
            continue
        normalized_base = api_base[: -len(strip_suffix)].rstrip("/") or None
        if inferred_provider == "openai" and provider_norm == "openai_compatible":
            return provider_norm, normalized_base, inferred_endpoint, match_suffix
        return inferred_provider, normalized_base, inferred_endpoint, match_suffix

    return provider_norm, api_base, None, None


def _resolve_provider_profile(
    provider: str,
    raw_api_base: str | None,
    raw_chat_endpoint: str | None,
) -> tuple[str, str | None, Literal["chat_completions", "responses"], str]:
    effective_provider, api_base, inferred_endpoint, inferred_path = _infer_provider_profile_from_api_base(
        provider,
        raw_api_base,
    )
    if inferred_endpoint is not None:
        if raw_chat_endpoint:
            explicit_endpoint = _normalize_chat_endpoint(effective_provider, raw_chat_endpoint)
            if explicit_endpoint != inferred_endpoint:
                raise ValueError(
                    "MODEL_PROVIDER_<NAME>_CHAT_ENDPOINT conflicts with the endpoint suffix in "
                    f"MODEL_PROVIDER_<NAME>_API_BASE: {raw_chat_endpoint!r} vs {raw_api_base!r}"
                )
        return effective_provider, api_base, inferred_endpoint, inferred_path or _default_endpoint_path(
            effective_provider,
            inferred_endpoint,
        )
    chat_endpoint = _normalize_chat_endpoint(effective_provider, raw_chat_endpoint)
    return effective_provider, api_base, chat_endpoint, _default_endpoint_path(effective_provider, chat_endpoint)


def _collect_provider_profiles(raw_env: dict[str, str]) -> dict[str, ProviderProfile]:
    """
    Parse provider profiles from env:
    MODEL_PROVIDER_<NAME>_PROVIDER
    MODEL_PROVIDER_<NAME>_API_KEY
    MODEL_PROVIDER_<NAME>_API_BASE
    MODEL_PROVIDER_<NAME>_STREAM
    MODEL_PROVIDER_<NAME>_CHAT_ENDPOINT
    """
    pattern = re.compile(
        r"^MODEL_PROVIDER_([A-Z0-9_]+)_(PROVIDER|API_KEY|API_BASE|STREAM|CHAT_ENDPOINT)$"
    )
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
        effective_provider, api_base, chat_endpoint, endpoint_path = _resolve_provider_profile(
            provider,
            fields.get("api_base"),
            fields.get("chat_endpoint"),
        )
        profiles[name] = ProviderProfile(
            provider=effective_provider,
            api_key=api_key,
            api_base=api_base,
            stream=_env_truthy(fields.get("stream")),
            chat_endpoint=chat_endpoint,
            endpoint_path=endpoint_path,
        )

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
    timeout_sec: float,
    retry_attempts: int,
    retry_backoff_sec: float,
    retry_timeout_multiplier: float,
    fallback_spec: str,
    reasoning_effort: str = "",
) -> ModelConfig:
    if not provider_name:
        raise ValueError("provider name is required")
    if not model_name:
        raise ValueError("model name is required")

    effort = _normalize_reasoning_effort(reasoning_effort)
    profile = _get_profile(profiles, provider_name)
    cfg = ModelConfig(
        model=_build_litellm_model(profile.provider, model_name, api_base=profile.api_base),
        provider=profile.provider,
        api_key=profile.api_key,
        api_base=profile.api_base,
        stream=profile.stream,
        chat_endpoint=profile.chat_endpoint,
        endpoint_path=profile.endpoint_path,
        temperature=temperature,
        max_tokens=max_tokens,
        timeout_sec=timeout_sec,
        retry_attempts=retry_attempts,
        retry_backoff_sec=retry_backoff_sec,
        retry_timeout_multiplier=retry_timeout_multiplier,
        reasoning_effort=effort,
        fallbacks=[],
    )

    for fb_provider_name, fb_model_name in _parse_fallbacks(fallback_spec):
        fb_profile = _get_profile(profiles, fb_provider_name)
        cfg.fallbacks.append(
            ChatEndpointConfig(
                model=_build_litellm_model(
                    fb_profile.provider,
                    fb_model_name or model_name,
                    api_base=fb_profile.api_base,
                ),
                provider=fb_profile.provider,
                api_key=fb_profile.api_key,
                api_base=fb_profile.api_base,
                stream=fb_profile.stream,
                chat_endpoint=fb_profile.chat_endpoint,
                endpoint_path=fb_profile.endpoint_path,
                temperature=temperature,
                max_tokens=max_tokens,
                timeout_sec=timeout_sec,
                retry_attempts=retry_attempts,
                retry_backoff_sec=retry_backoff_sec,
                retry_timeout_multiplier=retry_timeout_multiplier,
                reasoning_effort=effort,
            )
        )
    return cfg


def _build_embed_config(
    *,
    profiles: dict[str, ProviderProfile],
    provider_name: str,
    model_name: str,
    timeout_sec: float,
    retry_attempts: int,
    retry_backoff_sec: float,
    retry_timeout_multiplier: float,
    fallback_spec: str,
) -> EmbedConfig:
    if not provider_name:
        raise ValueError("provider name is required")
    if not model_name:
        raise ValueError("model name is required")

    profile = _get_profile(profiles, provider_name)
    cfg = EmbedConfig(
        model=_build_litellm_model(profile.provider, model_name, api_base=profile.api_base),
        provider=profile.provider,
        api_key=profile.api_key,
        api_base=profile.api_base,
        endpoint_path=profile.endpoint_path,
        timeout_sec=timeout_sec,
        retry_attempts=retry_attempts,
        retry_backoff_sec=retry_backoff_sec,
        retry_timeout_multiplier=retry_timeout_multiplier,
        fallbacks=[],
    )

    for fb_provider_name, fb_model_name in _parse_fallbacks(fallback_spec):
        fb_profile = _get_profile(profiles, fb_provider_name)
        cfg.fallbacks.append(
            EmbedEndpointConfig(
                model=_build_litellm_model(
                    fb_profile.provider,
                    fb_model_name or model_name,
                    api_base=fb_profile.api_base,
                ),
                provider=fb_profile.provider,
                api_key=fb_profile.api_key,
                api_base=fb_profile.api_base,
                endpoint_path=fb_profile.endpoint_path,
                timeout_sec=timeout_sec,
                retry_attempts=retry_attempts,
                retry_backoff_sec=retry_backoff_sec,
                retry_timeout_multiplier=retry_timeout_multiplier,
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
        if "vision_model" in bot_data:
            settings.bot.vision_model = ModelConfig(**bot_data["vision_model"])
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
        if "disable_link_preview" in bot_data:
            settings.bot.disable_link_preview = bool(
                bot_data["disable_link_preview"]
            )
        if "drop_pending_updates" in bot_data:
            settings.bot.drop_pending_updates = bot_data["drop_pending_updates"]
        if "reply_batch_timeout_seconds" in bot_data:
            settings.bot.reply_batch_timeout_seconds = min(
                120.0,
                max(5.0, float(bot_data["reply_batch_timeout_seconds"])),
            )

    if "moderation" in toml_data:
        settings.moderation = ModerationConfig(**toml_data["moderation"])
    settings.moderation.high_confidence_threshold = min(
        1.0, max(0.0, float(settings.moderation.high_confidence_threshold))
    )
    settings.moderation.challenge_timeout_seconds = max(
        60, int(settings.moderation.challenge_timeout_seconds)
    )
    settings.moderation.bot_screening_message_count = min(
        100, max(1, int(settings.moderation.bot_screening_message_count))
    )

    settings.bot.token = settings.bot_token
    settings.bot.inbound_debounce_seconds = max(0.0, float(settings.bot_inbound_debounce_seconds))
    if "bot_reply_batch_timeout_seconds" in getattr(settings, "model_fields_set", set()):
        settings.bot.reply_batch_timeout_seconds = min(
            120.0,
            max(5.0, float(settings.bot_reply_batch_timeout_seconds)),
        )
    settings.bot.enable_typing = settings.bot_enable_typing
    settings.bot.enable_streaming = settings.bot_enable_streaming
    settings.bot.stream_chunk_size = max(8, settings.bot_stream_chunk_size)
    settings.bot.stream_edit_interval_sec = max(0.3, settings.bot_stream_edit_interval_sec)
    configured_auto_delete_seconds = int(settings.bot_auto_delete_seconds or 0)
    explicit_seconds = "bot_auto_delete_seconds" in getattr(
        settings, "model_fields_set", set()
    )
    if not explicit_seconds and configured_auto_delete_seconds <= 0 and settings.bot_auto_delete_minutes > 0:
        configured_auto_delete_seconds = int(settings.bot_auto_delete_minutes) * 60
    settings.bot.auto_delete_seconds = max(0, configured_auto_delete_seconds)
    settings.bot.auto_delete_minutes = settings.bot.auto_delete_seconds // 60
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

    settings.join_verification_timeout_seconds = max(
        60, int(settings.join_verification_timeout_seconds)
    )
    settings.join_verification_check_interval_seconds = max(
        5.0, float(settings.join_verification_check_interval_seconds)
    )
    if settings.join_verification_provider not in {
        "turnstile",
        "hcaptcha",
        "turnstile_hcaptcha",
    }:
        settings.join_verification_provider = "turnstile"
    settings.join_verification_listen_port = min(
        65535, max(1, int(settings.join_verification_listen_port))
    )
    if settings.join_verification_enabled:
        challenge_keys: list[tuple[str, str]] = []
        if settings.join_verification_provider in {"hcaptcha", "turnstile_hcaptcha"}:
            challenge_keys.extend(
                (
                    (
                        "JOIN_VERIFICATION_HCAPTCHA_SITE_KEY",
                        settings.join_verification_hcaptcha_site_key,
                    ),
                    (
                        "JOIN_VERIFICATION_HCAPTCHA_SECRET_KEY",
                        settings.join_verification_hcaptcha_secret_key,
                    ),
                )
            )
        if settings.join_verification_provider in {"turnstile", "turnstile_hcaptcha"}:
            challenge_keys.extend(
                (
                    (
                        "JOIN_VERIFICATION_TURNSTILE_SITE_KEY",
                        settings.join_verification_turnstile_site_key,
                    ),
                    (
                        "JOIN_VERIFICATION_TURNSTILE_SECRET_KEY",
                        settings.join_verification_turnstile_secret_key,
                    ),
                )
            )
        missing = [
            name
            for name, value in (
                *challenge_keys,
                ("JOIN_VERIFICATION_PUBLIC_BASE_URL", settings.join_verification_public_base_url),
            )
            if not value.strip()
        ]
        if missing:
            raise ValueError(
                "JOIN_VERIFICATION_ENABLED=true requires " + ", ".join(missing)
            )

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

    vision_provider_name = (settings.vision_provider_name or main_provider_name).strip().lower()
    decision_provider_name = (settings.decision_provider_name or main_provider_name).strip().lower()
    moderation_provider_name = (
        settings.moderation_provider_name or decision_provider_name or main_provider_name
    ).strip().lower()
    compress_provider_name = (settings.compress_provider_name or main_provider_name).strip().lower()
    embed_provider_name = (settings.embed_provider_name or main_provider_name).strip().lower()

    vision_model_name = (settings.vision_model or main_model_name).strip()
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
        timeout_sec=max(1.0, float(settings.main_timeout_sec)),
        retry_attempts=max(1, int(settings.llm_retry_attempts)),
        retry_backoff_sec=max(0.0, float(settings.llm_retry_backoff_sec)),
        retry_timeout_multiplier=max(1.0, float(settings.llm_retry_timeout_multiplier)),
        fallback_spec=settings.main_fallbacks,
        reasoning_effort=settings.main_reasoning_effort,
    )
    settings.bot.vision_model = _build_chat_config(
        profiles=profiles,
        provider_name=vision_provider_name,
        model_name=vision_model_name,
        temperature=settings.bot.vision_model.temperature,
        max_tokens=settings.bot.vision_model.max_tokens,
        timeout_sec=max(1.0, float(settings.vision_timeout_sec)),
        retry_attempts=max(1, int(settings.llm_retry_attempts)),
        retry_backoff_sec=max(0.0, float(settings.llm_retry_backoff_sec)),
        retry_timeout_multiplier=max(1.0, float(settings.llm_retry_timeout_multiplier)),
        fallback_spec=settings.vision_fallbacks,
        reasoning_effort=settings.vision_reasoning_effort,
    )
    settings.bot.decision_model = _build_chat_config(
        profiles=profiles,
        provider_name=decision_provider_name,
        model_name=decision_model_name,
        temperature=settings.bot.decision_model.temperature,
        max_tokens=settings.bot.decision_model.max_tokens,
        timeout_sec=max(1.0, float(settings.decision_timeout_sec)),
        retry_attempts=max(1, int(settings.llm_retry_attempts)),
        retry_backoff_sec=max(0.0, float(settings.llm_retry_backoff_sec)),
        retry_timeout_multiplier=max(1.0, float(settings.llm_retry_timeout_multiplier)),
        fallback_spec=settings.decision_fallbacks,
        reasoning_effort=settings.decision_reasoning_effort,
    )
    settings.bot.moderation_model = _build_chat_config(
        profiles=profiles,
        provider_name=moderation_provider_name,
        model_name=moderation_model_name,
        temperature=settings.bot.moderation_model.temperature,
        max_tokens=settings.bot.moderation_model.max_tokens,
        timeout_sec=max(1.0, float(settings.moderation_timeout_sec)),
        retry_attempts=max(1, int(settings.llm_retry_attempts)),
        retry_backoff_sec=max(0.0, float(settings.llm_retry_backoff_sec)),
        retry_timeout_multiplier=max(1.0, float(settings.llm_retry_timeout_multiplier)),
        fallback_spec=settings.moderation_fallbacks,
        reasoning_effort=settings.moderation_reasoning_effort,
    )
    settings.bot.compress_model = _build_chat_config(
        profiles=profiles,
        provider_name=compress_provider_name,
        model_name=compress_model_name,
        temperature=settings.bot.compress_model.temperature,
        max_tokens=settings.bot.compress_model.max_tokens,
        timeout_sec=max(1.0, float(settings.compress_timeout_sec)),
        retry_attempts=max(1, int(settings.llm_retry_attempts)),
        retry_backoff_sec=max(0.0, float(settings.llm_retry_backoff_sec)),
        retry_timeout_multiplier=max(1.0, float(settings.llm_retry_timeout_multiplier)),
        fallback_spec=settings.compress_fallbacks,
        reasoning_effort=settings.compress_reasoning_effort,
    )
    settings.bot.embed_model = _build_embed_config(
        profiles=profiles,
        provider_name=embed_provider_name,
        model_name=embed_model_name,
        timeout_sec=max(1.0, float(settings.embed_timeout_sec)),
        retry_attempts=max(1, int(settings.llm_retry_attempts)),
        retry_backoff_sec=max(0.0, float(settings.llm_retry_backoff_sec)),
        retry_timeout_multiplier=max(1.0, float(settings.llm_retry_timeout_multiplier)),
        fallback_spec=settings.embed_fallbacks,
    )

    settings.bot.max_context_tokens = settings.max_context_tokens
    settings.bot.max_output_tokens = settings.max_output_tokens
    settings.bot.main_model.max_tokens = max(1, settings.max_output_tokens)

    return settings


def load_bootstrap_settings() -> Settings:
    """Load only values required before the database-backed config is available.

    Runtime options are applied by ``RuntimeConfigManager`` after the database
    is initialized. Legacy join-verification web variables remain accepted so
    existing deployments can migrate without changing their reverse proxy in
    the same release.
    """
    settings = Settings()
    settings.bot.token = settings.bot_token.strip()

    public_base_url = (
        settings.miniapp_public_base_url
        or settings.join_verification_public_base_url
    ).strip().rstrip("/")
    listen_host = (
        settings.miniapp_listen_host
        or settings.join_verification_listen_host
        or "0.0.0.0"
    ).strip()
    listen_port = min(
        65535,
        max(
            1,
            int(
                settings.miniapp_listen_port
                or settings.join_verification_listen_port
                or 8480
            ),
        ),
    )

    settings.miniapp_public_base_url = public_base_url
    settings.miniapp_listen_host = listen_host
    settings.miniapp_listen_port = listen_port
    # Existing verification code consumes these aliases. They now describe the
    # shared Mini App server instead of verification-specific infrastructure.
    settings.join_verification_public_base_url = public_base_url
    settings.join_verification_listen_host = listen_host
    settings.join_verification_listen_port = listen_port
    return settings


def validate_bootstrap_settings(settings: Settings) -> None:
    """Reject deployments that cannot be administered or decrypt runtime secrets.

    ``start.py`` performs a friendly local preflight, but production containers
    invoke ``python -m bot`` directly.  Keep the authoritative validation in the
    application package so every entry point fails before opening the database or
    listening on the public Mini App server.
    """

    token = str(getattr(settings.bot, "token", "") or settings.bot_token or "").strip()
    if not token or token.lower() in {"your_bot_token_here", "replace_me", "changeme"}:
        raise ValueError("BOT_TOKEN is required and must not use a template placeholder")
    if int(settings.super_admin_id or 0) <= 0:
        raise ValueError("SUPER_ADMIN_ID must be a positive Telegram user ID")
    if not str(settings.config_master_key or "").strip():
        raise ValueError("CONFIG_MASTER_KEY is required and must remain stable")

    settings.bot.token = token
