from __future__ import annotations

import logging
import time
from typing import Any

from bot.services.api_model_query import (
    ApiModelQueryConnection,
    api_model_query_endpoint,
    load_group_api_model_query_connection,
)
from bot.services.runtime_config import RuntimeConfigEncryptionError
from bot.services.skills.base import SkillContext, SkillRunResult
from bot.services.skills.platform_common import UnsafeUrlError, request_json
from bot.utils.security import clean_text

log = logging.getLogger(__name__)

_MAX_MODELS_IN_RESULT = 500


class ApiModelQuerySkill:
    name = "api_model_query"
    description = (
        "查询当前群单独配置的 OpenAI 兼容模型 API。"
        "当用户问本群配置的 API/中转站有哪些模型、某个模型能否使用、是否存活或要求测活时使用。"
        "list_models 会实时拉取 /v1/models；check_model 会先重新拉取模型列表，"
        "只对列表中精确存在的模型 ID 发起最小 Chat Completions 测试。"
        "不要用 websearch 代替，因为结果取决于本群配置的实时 API。"
    )
    parameters_schema = {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["list_models", "check_model"],
                "description": "list_models=列出模型；check_model=测试列表中的指定模型。",
            },
            "model": {
                "type": "string",
                "description": "action=check_model 时必填，必须是 list_models 返回的精确模型 ID。",
            },
        },
        "required": ["action"],
        "additionalProperties": False,
    }

    def __init__(self, settings: Any | None = None) -> None:
        self.master_key = str(getattr(settings, "config_master_key", "") or "")

    def execution_timeout_seconds(self, arguments: dict[str, Any]) -> float:
        """Upper bound used by SkillService; the HTTP request has the exact group timeout."""

        action = clean_text(str(arguments.get("action", "")), max_len=32).lower()
        return 610.0 if action == "check_model" else 310.0

    @staticmethod
    def _headers(connection: ApiModelQueryConnection) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {connection.api_key}",
            "Accept": "application/json",
        }

    @staticmethod
    def _safe_upstream_text(
        value: Any,
        *,
        connection: ApiModelQueryConnection,
        max_len: int,
    ) -> str:
        """Remove the group's credential before upstream text reaches logs or the LLM."""

        raw = str(value or "")
        if connection.api_key:
            raw = raw.replace(connection.api_key, "[API_KEY_REDACTED]")
        return clean_text(raw, max_len=max_len)

    @classmethod
    def _extract_error_message(
        cls,
        data: Any,
        fallback: str,
        *,
        connection: ApiModelQueryConnection,
    ) -> str:
        if isinstance(data, dict):
            error_obj = data.get("error")
            if isinstance(error_obj, dict):
                message = cls._safe_upstream_text(
                    error_obj.get("message", ""),
                    connection=connection,
                    max_len=200,
                )
                if message:
                    return message
            message = cls._safe_upstream_text(
                data.get("message", ""),
                connection=connection,
                max_len=200,
            )
            if message:
                return message
        return cls._safe_upstream_text(
            fallback,
            connection=connection,
            max_len=200,
        )

    async def _request_json(
        self,
        *,
        connection: ApiModelQueryConnection,
        method: str,
        endpoint: str,
        json_body: dict[str, Any] | None = None,
        timeout_sec: float,
    ) -> tuple[int, Any, str]:
        url = api_model_query_endpoint(connection.config.base_url, endpoint)
        status, data, _final_url, error_text = await request_json(
            url,
            method=method,
            headers=self._headers(connection),
            json_body=json_body,
            timeout_sec=timeout_sec,
            max_response_bytes=2 * 1024 * 1024,
            max_decoded_bytes=2 * 1024 * 1024,
        )
        return status, data, error_text

    async def _fetch_models(
        self,
        connection: ApiModelQueryConnection,
        *,
        timeout_sec: float | None = None,
    ) -> tuple[SkillRunResult | None, list[dict[str, str]], int]:
        try:
            status, data, error_text = await self._request_json(
                connection=connection,
                method="GET",
                endpoint="models",
                timeout_sec=(
                    connection.config.http_timeout_sec
                    if timeout_sec is None
                    else max(0.1, float(timeout_sec))
                ),
            )
        except UnsafeUrlError as exc:
            log.warning("api model list rejected unsafe url | group config | error=%s", exc)
            return (
                SkillRunResult(
                    ok=False,
                    skill=self.name,
                    summary="本群配置的模型 API 地址不安全，已拒绝访问",
                    error="unsafe_url",
                ),
                [],
                0,
            )
        except Exception as exc:
            safe_error = self._safe_upstream_text(
                exc,
                connection=connection,
                max_len=200,
            )
            log.warning("api model list request failed: %s", safe_error)
            return (
                SkillRunResult(
                    ok=False,
                    skill=self.name,
                    summary="模型列表查询失败（网络错误）",
                    error=safe_error or exc.__class__.__name__,
                ),
                [],
                0,
            )
        if status != 200:
            detail = self._extract_error_message(
                data,
                error_text,
                connection=connection,
            )
            return (
                SkillRunResult(
                    ok=False,
                    skill=self.name,
                    summary=f"模型列表查询失败（HTTP {status}）",
                    error=detail or f"http_{status}",
                ),
                [],
                0,
            )
        rows = data.get("data") if isinstance(data, dict) else None
        if not isinstance(rows, list):
            return (
                SkillRunResult(
                    ok=False,
                    skill=self.name,
                    summary="模型列表响应格式不兼容（缺少 data 数组）",
                    error="invalid_models_response",
                ),
                [],
                0,
            )

        models: list[dict[str, str]] = []
        seen: set[str] = set()
        total_count = 0
        for row in rows:
            if not isinstance(row, dict):
                continue
            raw_model_id = str(row.get("id", ""))
            if connection.api_key and connection.api_key in raw_model_id:
                log.warning("api model list omitted credential-bearing model id")
                continue
            model_id = clean_text(raw_model_id, max_len=120)
            if not model_id or model_id in seen:
                continue
            seen.add(model_id)
            total_count += 1
            if len(models) >= _MAX_MODELS_IN_RESULT:
                continue
            models.append(
                {
                    "id": model_id,
                    "display_name": self._safe_upstream_text(
                        row.get("display_name", "") or model_id,
                        connection=connection,
                        max_len=120,
                    ),
                }
            )
        return None, models, total_count

    async def _list_models(self, connection: ApiModelQueryConnection) -> SkillRunResult:
        failure, models, total_count = await self._fetch_models(connection)
        if failure is not None:
            return failure
        truncated = total_count > len(models)
        return SkillRunResult(
            ok=True,
            skill=self.name,
            summary=f"查到 {total_count} 个可用模型",
            payload={
                "action": "list_models",
                "count": total_count,
                "models": models,
                "truncated": truncated,
            },
        )

    @staticmethod
    def _completion_reply_text(data: Any) -> str:
        if not isinstance(data, dict):
            return ""
        choices = data.get("choices")
        if not isinstance(choices, list) or not choices:
            return ""
        first = choices[0] if isinstance(choices[0], dict) else {}
        message = first.get("message") if isinstance(first.get("message"), dict) else {}
        content = message.get("content")
        if isinstance(content, list):
            parts = [
                str(item.get("text", ""))
                for item in content
                if isinstance(item, dict) and item.get("type") == "text"
            ]
            return "\n".join(part for part in parts if part).strip()
        return str(content or "").strip()

    async def _check_model(
        self,
        arguments: dict[str, Any],
        connection: ApiModelQueryConnection,
    ) -> SkillRunResult:
        model = clean_text(str(arguments.get("model", "")), max_len=120)
        if not model:
            return SkillRunResult(
                ok=False, skill=self.name, summary="缺少模型 ID", error="missing_model"
            )

        check_started = time.monotonic()
        list_failure, models, total_count = await self._fetch_models(
            connection,
            timeout_sec=min(
                connection.config.http_timeout_sec,
                connection.config.check_timeout_sec,
            ),
        )
        if list_failure is not None:
            return SkillRunResult(
                ok=False,
                skill=self.name,
                summary=f"测活前无法确认模型列表：{list_failure.summary}",
                payload={"action": "check_model", "model": model},
                error=list_failure.error or "models_unavailable",
            )
        available_ids = {item["id"] for item in models}
        # A response over the result cap cannot safely prove that a hidden ID
        # was listed, so fail closed and ask the user to use one of the returned IDs.
        if model not in available_ids:
            return SkillRunResult(
                ok=False,
                skill=self.name,
                summary=f"模型 {model} 不在本群 API 当前返回的模型列表中",
                payload={
                    "action": "check_model",
                    "model": model,
                    "listed_model_count": total_count,
                    "available_model_ids": [item["id"] for item in models],
                },
                error="model_not_listed",
            )

        remaining_timeout_sec = connection.config.check_timeout_sec - (
            time.monotonic() - check_started
        )
        if remaining_timeout_sec <= 0:
            return SkillRunResult(
                ok=True,
                skill=self.name,
                summary=f"模型 {model} 不可用（测试超时）",
                payload={
                    "action": "check_model",
                    "model": model,
                    "alive": False,
                    "latency_ms": int((time.monotonic() - check_started) * 1000),
                    "http_status": 0,
                    "error_detail": "check_timeout",
                },
            )

        body = {
            "model": model,
            "messages": [{"role": "user", "content": "Reply with exactly: pong"}],
            "max_tokens": 16,
            "stream": False,
        }
        started = time.monotonic()
        try:
            status, data, error_text = await self._request_json(
                connection=connection,
                method="POST",
                endpoint="chat/completions",
                json_body=body,
                timeout_sec=remaining_timeout_sec,
            )
        except UnsafeUrlError as exc:
            log.warning("api model check rejected unsafe url | model=%s error=%s", model, exc)
            return SkillRunResult(
                ok=False,
                skill=self.name,
                summary="本群配置的模型 API 地址不安全，已拒绝访问",
                error="unsafe_url",
            )
        except Exception as exc:
            latency_ms = int((time.monotonic() - started) * 1000)
            safe_error = self._safe_upstream_text(
                exc,
                connection=connection,
                max_len=200,
            )
            log.warning(
                "api model check request failed | model=%s error=%s",
                model,
                safe_error,
            )
            return SkillRunResult(
                ok=True,
                skill=self.name,
                summary=f"模型 {model} 不可用（请求失败）",
                payload={
                    "action": "check_model",
                    "model": model,
                    "alive": False,
                    "latency_ms": latency_ms,
                    "http_status": 0,
                    "error_detail": safe_error or exc.__class__.__name__,
                },
            )
        latency_ms = int((time.monotonic() - started) * 1000)
        reply_text = self._completion_reply_text(data)
        alive = status == 200 and bool(reply_text)
        if alive:
            return SkillRunResult(
                ok=True,
                skill=self.name,
                summary=f"模型 {model} 可用（{latency_ms}ms）",
                payload={
                    "action": "check_model",
                    "model": model,
                    "alive": True,
                    "latency_ms": latency_ms,
                    "http_status": status,
                },
            )

        detail = self._extract_error_message(
            data,
            error_text,
            connection=connection,
        )
        return SkillRunResult(
            ok=True,
            skill=self.name,
            summary=f"模型 {model} 不可用（HTTP {status}）",
            payload={
                "action": "check_model",
                "model": model,
                "alive": False,
                "latency_ms": latency_ms,
                "http_status": status,
                "error_detail": detail,
            },
        )

    async def run(self, arguments: dict[str, Any], context: SkillContext) -> SkillRunResult:
        group_id = int(context.chat_id or 0)
        if not group_id and context.message is not None:
            group_id = int(getattr(getattr(context.message, "chat", None), "id", 0) or 0)
        if not group_id:
            return SkillRunResult(
                ok=False,
                skill=self.name,
                summary="API 模型查询只能在已配置的群聊中使用",
                error="group_context_required",
            )

        try:
            if context.session is not None:
                connection = await load_group_api_model_query_connection(
                    context.session,
                    group_id=group_id,
                    master_key=self.master_key,
                )
            elif context.session_factory is not None:
                async with context.session_factory() as session:
                    connection = await load_group_api_model_query_connection(
                        session,
                        group_id=group_id,
                        master_key=self.master_key,
                    )
            else:
                return SkillRunResult(
                    ok=False,
                    skill=self.name,
                    summary="无法读取本群 API 模型查询配置",
                    error="session_required",
                )
        except (RuntimeConfigEncryptionError, ValueError):
            log.exception("group api model key decrypt failed | group=%s", group_id)
            return SkillRunResult(
                ok=False,
                skill=self.name,
                summary="本群模型 API Key 无法解密，请管理员重新保存",
                error="secret_unavailable",
            )

        if not connection.config.enabled:
            return SkillRunResult(
                ok=False,
                skill=self.name,
                summary="本群未开启 API 模型查询",
                error="disabled_for_group",
            )
        if not connection.ready:
            return SkillRunResult(
                ok=False,
                skill=self.name,
                summary="本群 API 模型查询尚未配置完整",
                error="not_configured_for_group",
            )

        action = clean_text(str(arguments.get("action", "")), max_len=32).lower()
        if action == "list_models":
            return await self._list_models(connection)
        if action == "check_model":
            return await self._check_model(arguments, connection)
        return SkillRunResult(
            ok=False, skill=self.name, summary="未知操作", error="unknown_action"
        )
