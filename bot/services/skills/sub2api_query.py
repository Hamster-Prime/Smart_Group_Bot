from __future__ import annotations

import logging
import time
from typing import Any

import aiohttp

from bot.services.skills.base import SkillContext, SkillRunResult
from bot.services.skills.platform_common import (
    ResponseTooLargeError,
    _read_limited_raw_body,
)
from bot.utils.security import clean_text

log = logging.getLogger(__name__)


class Sub2ApiQuerySkill:
    name = "sub2api_query"
    description = (
        "本站 AI 模型网关（Sub2API 中转站）的权威查询工具。当用户问：站里/中转站有哪些模型、"
        "某个模型（如 gpt-5.6、gpt-5.3-codex）能不能用/挂没挂/是否可用/测活/还活着吗、"
        "模型列表更新了吗——必须优先调用本技能，禁止用 websearch 联网搜索代替，"
        "因为答案取决于本站网关的实时状态而非公网信息。"
        "list_models=列出当前可用模型 ID；check_model=对指定模型发真实请求测活。"
        "测活前如果不知道确切模型 ID，先用 list_models 拿到可用 ID 再测。"
    )
    parameters_schema = {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["list_models", "check_model"],
                "description": (
                    "list_models=列出可用模型；check_model=对指定模型发起真实请求测活。"
                ),
            },
            "model": {
                "type": "string",
                "description": "action=check_model 时必填，模型 ID 来自 list_models 返回的 id 字段。",
            },
        },
        "required": ["action"],
        "additionalProperties": False,
    }

    def __init__(self, settings: Any | None = None) -> None:
        self.enabled = bool(getattr(settings, "sub2api_enabled", False))
        self.base_url = clean_text(
            str(getattr(settings, "sub2api_base_url", "") or ""), max_len=255
        ).rstrip("/")
        self.api_key = clean_text(
            str(getattr(settings, "sub2api_api_key", "") or ""), max_len=255
        )
        self.http_timeout_sec = max(
            5.0, float(getattr(settings, "sub2api_http_timeout_sec", 15.0) or 15.0)
        )
        self.check_timeout_sec = max(
            10.0, float(getattr(settings, "sub2api_check_timeout_sec", 45.0) or 45.0)
        )

    @property
    def available(self) -> bool:
        return self.enabled and bool(self.base_url) and bool(self.api_key)

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Accept": "application/json",
        }

    async def _request_json(
        self,
        *,
        method: str,
        url: str,
        headers: dict[str, str],
        json_body: dict[str, Any] | None = None,
        timeout_sec: float,
    ) -> tuple[int, Any, str]:
        """Returns (status, parsed_json_or_None, error_text)."""
        timeout = aiohttp.ClientTimeout(total=timeout_sec)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.request(
                # Never forward the bearer token to a redirect destination.
                # A gateway migration must be configured explicitly instead of
                # trusting a possibly compromised 3xx Location header.
                method,
                url,
                headers=headers,
                json=json_body,
                allow_redirects=False,
            ) as resp:
                try:
                    raw_bytes = await _read_limited_raw_body(resp, 2 * 1024 * 1024)
                except ResponseTooLargeError:
                    return resp.status, None, "response_too_large"
                raw = raw_bytes.decode(resp.charset or "utf-8", errors="ignore")
                try:
                    import json as _json

                    return resp.status, _json.loads(raw), ""
                except Exception:
                    return resp.status, None, raw[:300]

    @staticmethod
    def _extract_error_message(data: Any, fallback: str) -> str:
        if isinstance(data, dict):
            error_obj = data.get("error")
            if isinstance(error_obj, dict):
                message = clean_text(str(error_obj.get("message", "")), max_len=200)
                if message:
                    return message
        return clean_text(fallback, max_len=200)

    async def _list_models(self) -> SkillRunResult:
        try:
            status, data, err_text = await self._request_json(
                method="GET",
                url=f"{self.base_url}/v1/models",
                headers=self._headers(),
                timeout_sec=self.http_timeout_sec,
            )
        except Exception as exc:
            log.warning("sub2api list_models request failed: %s", exc)
            return SkillRunResult(
                ok=False,
                skill=self.name,
                summary="模型列表查询失败（网络错误）",
                error=clean_text(str(exc) or exc.__class__.__name__, max_len=200),
            )
        if status != 200:
            detail = self._extract_error_message(data, err_text)
            return SkillRunResult(
                ok=False,
                skill=self.name,
                summary=f"模型列表查询失败（HTTP {status}）",
                error=detail or f"http_{status}",
            )
        rows = data.get("data") if isinstance(data, dict) else None
        if not isinstance(rows, list):
            rows = []
        models = [
            {
                "id": clean_text(str(row.get("id", "")), max_len=120),
                "display_name": clean_text(
                    str(row.get("display_name", "") or row.get("id", "")), max_len=120
                ),
            }
            for row in rows
            if isinstance(row, dict) and str(row.get("id", "")).strip()
        ]
        return SkillRunResult(
            ok=True,
            skill=self.name,
            summary=f"查到 {len(models)} 个可用模型",
            payload={
                "action": "list_models",
                "count": len(models),
                "models": models,
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
            return "\n".join(p for p in parts if p).strip()
        return str(content or "").strip()

    async def _check_model(self, arguments: dict[str, Any]) -> SkillRunResult:
        model = clean_text(str(arguments.get("model", "")), max_len=120)
        if not model:
            return SkillRunResult(
                ok=False, skill=self.name, summary="缺少模型 ID", error="missing_model"
            )

        body = {
            "model": model,
            "messages": [{"role": "user", "content": "Reply with exactly: pong"}],
            "max_tokens": 16,
            "stream": False,
        }
        started = time.monotonic()
        try:
            status, data, err_text = await self._request_json(
                method="POST",
                url=f"{self.base_url}/v1/chat/completions",
                headers=self._headers(),
                json_body=body,
                timeout_sec=self.check_timeout_sec,
            )
        except Exception as exc:
            latency_ms = int((time.monotonic() - started) * 1000)
            log.warning("sub2api check_model request failed model=%s error=%s", model, exc)
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
                    "error_detail": clean_text(
                        str(exc) or exc.__class__.__name__, max_len=200
                    ),
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
                    "reply_preview": clean_text(reply_text, max_len=120),
                },
            )

        error_detail = self._extract_error_message(data, err_text)
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
                "error_detail": error_detail,
            },
        )

    async def run(self, arguments: dict[str, Any], context: SkillContext) -> SkillRunResult:
        _ = context  # Unused for this skill.
        if not self.enabled:
            return SkillRunResult(
                ok=False, skill=self.name, summary="Sub2API 查询技能当前已关闭", error="disabled"
            )
        if not self.base_url or not self.api_key:
            return SkillRunResult(
                ok=False,
                skill=self.name,
                summary="Sub2API 查询技能未配置 base_url 或 api_key",
                error="not_configured",
            )

        action = clean_text(str(arguments.get("action", "")), max_len=32).lower()
        if action == "list_models":
            return await self._list_models()
        if action == "check_model":
            return await self._check_model(arguments)
        return SkillRunResult(ok=False, skill=self.name, summary="未知操作", error="unknown_action")
