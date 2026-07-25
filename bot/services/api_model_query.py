from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

from sqlalchemy.ext.asyncio import AsyncSession

from bot.db.models import Group, GroupApiModelQuerySecret
from bot.services.runtime_config import RuntimeConfigEncryptionError, SecretCipher
from bot.utils.security import clean_text

API_MODEL_QUERY_SETTINGS_KEY = "api_model_query"
API_MODEL_QUERY_CONFIG_VERSION = 1
API_MODEL_QUERY_DEFAULT_HTTP_TIMEOUT_SEC = 15.0
API_MODEL_QUERY_DEFAULT_CHECK_TIMEOUT_SEC = 45.0


@dataclass(frozen=True, slots=True)
class ApiModelQueryConfig:
    enabled: bool = False
    base_url: str = ""
    http_timeout_sec: float = API_MODEL_QUERY_DEFAULT_HTTP_TIMEOUT_SEC
    check_timeout_sec: float = API_MODEL_QUERY_DEFAULT_CHECK_TIMEOUT_SEC
    api_key_configured: bool = False
    secret_version: int = 0


@dataclass(frozen=True, slots=True)
class ApiModelQueryConnection:
    config: ApiModelQueryConfig
    api_key: str = ""

    @property
    def ready(self) -> bool:
        return bool(
            self.config.enabled
            and self.config.base_url
            and self.config.api_key_configured
            and self.api_key
        )


def _as_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return value != 0
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on", "enable", "enabled"}
    return bool(value)


def _bounded_float(value: Any, *, default: float, minimum: float, maximum: float) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    if parsed != parsed:  # NaN
        return default
    return max(minimum, min(maximum, parsed))


def _bounded_int(value: Any, *, default: int, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return max(minimum, min(maximum, parsed))


def normalize_api_model_query_base_url(value: Any) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    if len(raw) > 1000 or any(ord(char) <= 32 or ord(char) == 127 for char in raw):
        raise ValueError("模型 API 地址格式无效")
    try:
        parsed = urlparse(raw)
        port = parsed.port
    except ValueError as exc:
        raise ValueError("模型 API 地址格式无效") from exc
    if (
        parsed.scheme.lower() != "https"
        or not parsed.netloc
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or "\\" in parsed.netloc
        or not parsed.hostname
    ):
        raise ValueError("模型 API 地址必须是不含账号、查询参数或片段的 HTTPS 地址")
    if port is not None and not 1 <= port <= 65535:
        raise ValueError("模型 API 地址端口无效")
    return raw.rstrip("/")


def get_api_model_query_config(settings_data: dict[str, Any] | None) -> ApiModelQueryConfig:
    raw = (
        settings_data.get(API_MODEL_QUERY_SETTINGS_KEY)
        if isinstance(settings_data, dict)
        else None
    )
    data = raw if isinstance(raw, dict) else {}
    try:
        base_url = normalize_api_model_query_base_url(data.get("base_url"))
    except ValueError:
        # Old or manually edited group JSON must never authorize an outbound
        # request. The Web API validates a replacement value before storing it.
        base_url = ""
    return ApiModelQueryConfig(
        enabled=_as_bool(data.get("enabled", False)),
        base_url=base_url,
        http_timeout_sec=_bounded_float(
            data.get("http_timeout_sec"),
            default=API_MODEL_QUERY_DEFAULT_HTTP_TIMEOUT_SEC,
            minimum=1.0,
            maximum=300.0,
        ),
        check_timeout_sec=_bounded_float(
            data.get("check_timeout_sec"),
            default=API_MODEL_QUERY_DEFAULT_CHECK_TIMEOUT_SEC,
            minimum=1.0,
            maximum=600.0,
        ),
        api_key_configured=_as_bool(data.get("api_key_configured", False)),
        secret_version=_bounded_int(
            data.get("secret_version"), default=0, minimum=0, maximum=2_147_483_647
        ),
    )


def set_api_model_query_config(
    settings_data: dict[str, Any] | None,
    config: ApiModelQueryConfig,
) -> dict[str, Any]:
    updated = dict(settings_data or {})
    updated[API_MODEL_QUERY_SETTINGS_KEY] = {
        "version": API_MODEL_QUERY_CONFIG_VERSION,
        "enabled": bool(config.enabled),
        "base_url": normalize_api_model_query_base_url(config.base_url),
        "http_timeout_sec": float(config.http_timeout_sec),
        "check_timeout_sec": float(config.check_timeout_sec),
        "api_key_configured": bool(config.api_key_configured),
        "secret_version": max(0, int(config.secret_version)),
    }
    return updated


def api_model_query_tool_enabled(settings_data: dict[str, Any] | None) -> bool:
    config = get_api_model_query_config(settings_data)
    return bool(config.enabled and config.base_url and config.api_key_configured)


def api_model_query_endpoint(base_url: str, endpoint: str) -> str:
    normalized = normalize_api_model_query_base_url(base_url)
    parsed = urlparse(normalized)
    path = (parsed.path or "").rstrip("/").lower()
    suffix = endpoint.lstrip("/")
    return f"{normalized}/{suffix}" if path.endswith("/v1") else f"{normalized}/v1/{suffix}"


def normalize_api_model_query_api_key(value: Any) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    if len(raw) > 1024 or any(ord(char) < 32 or ord(char) == 127 for char in raw):
        raise ValueError("模型 API Key 格式无效")
    return clean_text(raw, max_len=1024)


async def group_api_model_query_secret_exists(
    session: AsyncSession,
    group_id: int,
) -> bool:
    row = await session.get(GroupApiModelQuerySecret, int(group_id))
    return bool(row is not None and str(row.ciphertext or "").strip())


async def replace_group_api_model_query_secret(
    session: AsyncSession,
    *,
    group_id: int,
    api_key: str,
    master_key: str,
    updated_by: int,
) -> None:
    normalized = normalize_api_model_query_api_key(api_key)
    if not normalized:
        raise ValueError("模型 API Key 不能为空")
    cipher = SecretCipher(master_key)
    ciphertext = cipher.encrypt(normalized)
    row = await session.get(GroupApiModelQuerySecret, int(group_id))
    if row is None:
        session.add(
            GroupApiModelQuerySecret(
                group_id=int(group_id),
                ciphertext=ciphertext,
                updated_by=int(updated_by),
            )
        )
    else:
        row.ciphertext = ciphertext
        row.updated_by = int(updated_by)


async def clear_group_api_model_query_secret(
    session: AsyncSession,
    *,
    group_id: int,
) -> None:
    row = await session.get(GroupApiModelQuerySecret, int(group_id))
    if row is not None:
        await session.delete(row)


async def load_group_api_model_query_connection(
    session: AsyncSession,
    *,
    group_id: int,
    master_key: str,
) -> ApiModelQueryConnection:
    group = await session.get(Group, int(group_id))
    config = get_api_model_query_config(group.settings if group is not None else None)
    if not config.enabled or not config.base_url or not config.api_key_configured:
        return ApiModelQueryConnection(config=config)

    row = await session.get(GroupApiModelQuerySecret, int(group_id))
    if row is None or not str(row.ciphertext or "").strip():
        return ApiModelQueryConnection(config=config)
    cipher = SecretCipher(master_key)
    try:
        api_key = normalize_api_model_query_api_key(cipher.decrypt(row.ciphertext))
    except RuntimeConfigEncryptionError:
        raise
    return ApiModelQueryConnection(config=config, api_key=api_key)
