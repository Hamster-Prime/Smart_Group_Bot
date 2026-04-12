from __future__ import annotations

import asyncio
import json
import logging
from types import SimpleNamespace
from typing import Any

import litellm

from bot.config import ChatEndpointConfig, EmbedConfig, EmbedEndpointConfig, ModelConfig

log = logging.getLogger(__name__)

# Disable extra LiteLLM debug logs.
litellm.suppress_debug_info = True
litellm.set_verbose = False


class LLMService:
    """Unified LLM interface for main/vision/decision/moderation/compress/embed."""

    def __init__(
        self,
        main: ModelConfig,
        decision: ModelConfig,
        compress: ModelConfig | None = None,
        *,
        moderation: ModelConfig | None = None,
        vision: ModelConfig | None = None,
        embed: EmbedConfig | None = None,
        max_context_tokens: int | None = None,
    ) -> None:
        self.main = main
        self.vision_config = vision or main
        self.decision_config = decision
        self.moderation_config = moderation or decision
        self.compress_config = compress or main
        self.embed_config = embed or EmbedConfig()
        self.max_context_tokens = max(0, int(max_context_tokens or 0))

    @classmethod
    def _resolve_request_api_base(
        cls,
        *,
        provider: str,
        api_base: str | None,
        endpoint_path: str | None,
        model: str,
    ) -> str | None:
        base = str(api_base or "").strip()
        if not base:
            return None

        provider_norm = str(provider or "").strip().lower()
        if provider_norm != "gemini":
            return base

        normalized_base = base.rstrip("/")
        normalized_endpoint = f"/{str(endpoint_path or '/v1beta/models').strip().strip('/')}"
        version_suffix = normalized_endpoint[: -len("/models")] if normalized_endpoint.endswith("/models") else ""
        lower_base = normalized_base.lower()
        lower_endpoint = normalized_endpoint.lower()
        lower_version_suffix = version_suffix.lower()

        if lower_base.endswith(lower_endpoint):
            return normalized_base[: -len("/models")]
        if lower_version_suffix and lower_base.endswith(lower_version_suffix):
            return normalized_base
        if version_suffix:
            return f"{normalized_base}{version_suffix}"
        return normalized_base

    @classmethod
    def _build_chat_kwargs(
        cls,
        cfg: ChatEndpointConfig,
        *,
        stream: bool | None = None,
    ) -> dict[str, Any]:
        kwargs: dict[str, Any] = {
            "model": cfg.model,
            "temperature": cfg.temperature,
            "max_tokens": cfg.max_tokens,
            "timeout": max(1.0, float(getattr(cfg, "timeout_sec", 12.0) or 12.0)),
        }
        if bool(getattr(cfg, "stream", False)) if stream is None else bool(stream):
            kwargs["stream"] = True
        if cfg.api_key:
            kwargs["api_key"] = cfg.api_key
        resolved_api_base = cls._resolve_request_api_base(
            provider=getattr(cfg, "provider", ""),
            api_base=cfg.api_base,
            endpoint_path=getattr(cfg, "endpoint_path", "/chat/completions"),
            model=cfg.model,
        )
        if resolved_api_base:
            kwargs["api_base"] = resolved_api_base
        return kwargs

    @classmethod
    def _build_responses_kwargs(
        cls,
        cfg: ChatEndpointConfig,
        *,
        stream: bool | None = None,
    ) -> dict[str, Any]:
        model = str(cfg.model or "").strip()
        provider = str(getattr(cfg, "provider", "") or "").strip().lower()
        if provider in {"openai", "openai_compatible"} and model.lower().startswith("openai/"):
            model = model.split("/", 1)[1]

        kwargs: dict[str, Any] = {
            "model": model,
            "temperature": cfg.temperature,
            "max_output_tokens": cfg.max_tokens,
            "timeout": max(1.0, float(getattr(cfg, "timeout_sec", 12.0) or 12.0)),
        }
        if bool(getattr(cfg, "stream", False)) if stream is None else bool(stream):
            kwargs["stream"] = True
        if cfg.api_key:
            kwargs["api_key"] = cfg.api_key
        resolved_api_base = cls._resolve_request_api_base(
            provider=provider,
            api_base=cfg.api_base,
            endpoint_path=getattr(cfg, "endpoint_path", "/chat/completions"),
            model=cfg.model,
        )
        if resolved_api_base:
            kwargs["api_base"] = resolved_api_base
        return kwargs

    @classmethod
    def _build_embed_kwargs(cls, cfg: EmbedEndpointConfig, texts: list[str]) -> dict[str, Any]:
        kwargs: dict[str, Any] = {
            "model": cfg.model,
            "input": texts,
            "timeout": max(1.0, float(getattr(cfg, "timeout_sec", 10.0) or 10.0)),
        }
        if cfg.api_key:
            kwargs["api_key"] = cfg.api_key
        resolved_api_base = cls._resolve_request_api_base(
            provider=getattr(cfg, "provider", ""),
            api_base=cfg.api_base,
            endpoint_path=getattr(cfg, "endpoint_path", "/v1beta/models"),
            model=cfg.model,
        )
        if resolved_api_base:
            kwargs["api_base"] = resolved_api_base
        return kwargs

    @staticmethod
    def _chat_candidates(cfg: ModelConfig) -> list[ChatEndpointConfig]:
        return [
            ChatEndpointConfig(
                model=cfg.model,
                provider=getattr(cfg, "provider", ""),
                api_key=cfg.api_key,
                api_base=cfg.api_base,
                stream=cfg.stream,
                chat_endpoint=getattr(cfg, "chat_endpoint", "chat_completions"),
                endpoint_path=getattr(cfg, "endpoint_path", "/chat/completions"),
                temperature=cfg.temperature,
                max_tokens=cfg.max_tokens,
                timeout_sec=cfg.timeout_sec,
                retry_attempts=cfg.retry_attempts,
                retry_backoff_sec=cfg.retry_backoff_sec,
                retry_timeout_multiplier=cfg.retry_timeout_multiplier,
            ),
            *cfg.fallbacks,
        ]

    @staticmethod
    def _embed_candidates(cfg: EmbedConfig) -> list[EmbedEndpointConfig]:
        return [
            EmbedEndpointConfig(
                model=cfg.model,
                provider=getattr(cfg, "provider", ""),
                api_key=cfg.api_key,
                api_base=cfg.api_base,
                endpoint_path=getattr(cfg, "endpoint_path", "/v1beta/models"),
                timeout_sec=cfg.timeout_sec,
                retry_attempts=cfg.retry_attempts,
                retry_backoff_sec=cfg.retry_backoff_sec,
                retry_timeout_multiplier=cfg.retry_timeout_multiplier,
            ),
            *cfg.fallbacks,
        ]

    @staticmethod
    def _normalize_content_text(content: Any) -> str:
        """Normalize text across providers that return different structures."""
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            parts: list[str] = []
            for item in content:
                if not isinstance(item, dict):
                    continue
                item_type = str(item.get("type", "") or "").strip().lower()
                if item_type in {"text", "output_text", "input_text"}:
                    txt = item.get("text", "")
                    if isinstance(txt, dict):
                        txt = txt.get("value", "")
                    txt = str(txt or "").strip()
                    if txt:
                        parts.append(txt)
                    continue
                if item_type == "refusal":
                    refusal = str(item.get("refusal", "") or "").strip()
                    if refusal:
                        parts.append(refusal)
                    continue
            return "\n".join(parts)
        return str(content or "")

    @staticmethod
    def _get_value(obj: Any, key: str, default: Any = None) -> Any:
        if isinstance(obj, dict):
            return obj.get(key, default)
        return getattr(obj, key, default)

    @staticmethod
    def _string_value(value: Any) -> str:
        if value is None:
            return ""
        enum_value = getattr(value, "value", None)
        if isinstance(enum_value, str):
            return enum_value
        return str(value)

    @staticmethod
    def _coerce_int(value: Any) -> int:
        try:
            return int(value or 0)
        except (TypeError, ValueError):
            return 0

    @staticmethod
    def _merge_stream_piece(existing: str, piece: str) -> str:
        if not piece:
            return existing
        if not existing:
            return piece
        if piece.startswith(existing):
            return piece
        if existing.endswith(piece):
            return existing
        return existing + piece

    @classmethod
    def _merge_stream_content_slot(
        cls,
        merged: dict[tuple[int, int], str],
        *,
        output_index: Any,
        content_index: Any,
        piece: str,
    ) -> None:
        if not piece:
            return
        slot = (cls._coerce_int(output_index), cls._coerce_int(content_index))
        merged[slot] = cls._merge_stream_piece(merged.get(slot, ""), piece)

    @staticmethod
    def _joined_stream_content_slots(merged: dict[tuple[int, int], str]) -> str:
        return "".join(text for _, text in sorted(merged.items()))

    @classmethod
    def _stream_delta_text(cls, delta: Any) -> str:
        content = cls._get_value(delta, "content")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            parts: list[str] = []
            for item in content:
                if isinstance(item, str):
                    parts.append(item)
                    continue
                if not isinstance(item, dict):
                    continue
                if item.get("type") == "text":
                    text_value = item.get("text", "")
                    if isinstance(text_value, dict):
                        text_value = text_value.get("value", "")
                    parts.append(str(text_value or ""))
                    continue
                if "text" in item:
                    parts.append(str(item.get("text", "") or ""))
            return "".join(parts)
        text = cls._get_value(delta, "text")
        if text is None:
            return ""
        return str(text)

    @classmethod
    def _merge_stream_tool_calls(cls, merged: list[dict[str, Any]], delta_tool_calls: Any) -> None:
        if not delta_tool_calls:
            return

        for raw_call in delta_tool_calls:
            try:
                index = int(cls._get_value(raw_call, "index", len(merged)) or 0)
            except (TypeError, ValueError):
                index = len(merged)

            while len(merged) <= index:
                merged.append(
                    {
                        "id": "",
                        "type": "function",
                        "function": {"name": "", "arguments": ""},
                    }
                )

            entry = merged[index]
            call_id = str(cls._get_value(raw_call, "id", "") or "")
            if call_id:
                entry["id"] = call_id

            call_type = cls._string_value(cls._get_value(raw_call, "type", "") or "")
            if call_type:
                entry["type"] = call_type

            raw_function = cls._get_value(raw_call, "function", {}) or {}
            function = entry.setdefault("function", {"name": "", "arguments": ""})

            name_piece = str(cls._get_value(raw_function, "name", "") or "")
            if name_piece:
                function["name"] = cls._merge_stream_piece(str(function.get("name", "") or ""), name_piece)

            args_piece = cls._get_value(raw_function, "arguments", "")
            if args_piece is None:
                args_piece = ""
            if isinstance(args_piece, dict):
                args_piece = json.dumps(args_piece, ensure_ascii=False)
            args_piece = str(args_piece)
            if args_piece:
                function["arguments"] = cls._merge_stream_piece(
                    str(function.get("arguments", "") or ""),
                    args_piece,
                )

    @classmethod
    def _coerce_usage(cls, usage: Any) -> SimpleNamespace | None:
        if not usage:
            return None
        prompt_tokens = cls._coerce_int(cls._get_value(usage, "prompt_tokens", 0))
        if not prompt_tokens:
            prompt_tokens = cls._coerce_int(cls._get_value(usage, "input_tokens", 0))

        completion_tokens = cls._coerce_int(cls._get_value(usage, "completion_tokens", 0))
        if not completion_tokens:
            completion_tokens = cls._coerce_int(cls._get_value(usage, "output_tokens", 0))

        total_tokens = cls._coerce_int(cls._get_value(usage, "total_tokens", 0))
        if not total_tokens and (prompt_tokens or completion_tokens):
            total_tokens = prompt_tokens + completion_tokens

        if not prompt_tokens and not completion_tokens and not total_tokens:
            return None
        return SimpleNamespace(
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=total_tokens,
        )

    @classmethod
    def _normalize_tool_calls(cls, raw_tool_calls: Any) -> list[dict[str, Any]]:
        normalized_tool_calls: list[dict[str, Any]] = []
        if not raw_tool_calls:
            return normalized_tool_calls

        for idx, raw_call in enumerate(raw_tool_calls, start=1):
            function = cls._get_value(raw_call, "function", {}) or {}
            name = str(
                cls._get_value(function, "name", "") or cls._get_value(raw_call, "name", "") or ""
            ).strip()
            arguments = cls._get_value(function, "arguments", None)
            if arguments is None:
                arguments = cls._get_value(raw_call, "arguments", "")
            if isinstance(arguments, dict):
                arguments = json.dumps(arguments, ensure_ascii=False)
            arguments = str(arguments or "")
            call_id = str(
                cls._get_value(raw_call, "id", "") or cls._get_value(raw_call, "call_id", "") or ""
            ).strip() or f"call_{idx}"
            call_type = cls._string_value(cls._get_value(raw_call, "type", "") or "function").strip() or "function"
            if not name and not arguments:
                continue
            normalized_tool_calls.append(
                {
                    "id": call_id,
                    "type": call_type,
                    "function": {
                        "name": name,
                        "arguments": arguments,
                    },
                }
            )
        return normalized_tool_calls

    @classmethod
    def _responses_text_piece(cls, value: Any) -> str:
        if value is None:
            return ""
        if isinstance(value, str):
            return value
        if isinstance(value, list):
            return "".join(cls._responses_text_piece(item) for item in value)

        item_type = cls._string_value(cls._get_value(value, "type", "") or "").strip().lower()
        if item_type in {"output_text", "text", "input_text"}:
            return cls._responses_text_piece(cls._get_value(value, "text", ""))
        if item_type == "refusal":
            return str(cls._get_value(value, "refusal", "") or "")

        text_value = cls._get_value(value, "text", None)
        if text_value not in (None, ""):
            return cls._responses_text_piece(text_value)

        refusal_value = cls._get_value(value, "refusal", None)
        if refusal_value not in (None, ""):
            return str(refusal_value)

        value_text = cls._get_value(value, "value", None)
        if value_text not in (None, ""):
            return str(value_text)

        return ""

    @classmethod
    def _extract_responses_message(cls, response_obj: Any) -> SimpleNamespace | None:
        output_items = cls._get_value(response_obj, "output", None) or []
        content_parts: list[str] = []
        tool_calls: list[dict[str, Any]] = []

        for item in output_items:
            item_type = cls._string_value(cls._get_value(item, "type", "") or "").strip().lower()
            if item_type == "message":
                content_parts.append(cls._responses_text_piece(cls._get_value(item, "content", None)))
                continue
            if item_type == "function_call":
                tool_calls.extend(cls._normalize_tool_calls([item]))
                continue
            if item_type in {"output_text", "text"}:
                text_piece = cls._responses_text_piece(item)
                if text_piece:
                    content_parts.append(text_piece)

        if not content_parts and not tool_calls:
            text_piece = cls._responses_text_piece(cls._get_value(response_obj, "text", None))
            if text_piece:
                content_parts.append(text_piece)

        content = "".join(content_parts)
        if not content.strip() and not tool_calls:
            return None

        return SimpleNamespace(
            content=content,
            tool_calls=tool_calls,
        )

    @classmethod
    def _build_model_response(
        cls,
        *,
        content: str,
        tool_calls: list[dict[str, Any]] | None = None,
        usage: SimpleNamespace | None = None,
    ) -> Any:
        return SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(
                        content=content,
                        tool_calls=tool_calls or [],
                    )
                )
            ],
            usage=usage,
        )

    @classmethod
    def _normalize_response_object(cls, resp: Any) -> Any:
        usage = cls._coerce_usage(cls._get_value(resp, "usage"))
        choices = cls._get_value(resp, "choices", None) or []
        if choices:
            message = cls._get_value(choices[0], "message", None)
            if message is not None:
                return cls._build_model_response(
                    content=cls._normalize_content_text(cls._get_value(message, "content", "")),
                    tool_calls=cls._normalize_tool_calls(cls._get_value(message, "tool_calls", None)),
                    usage=usage,
                )

        message = cls._extract_responses_message(resp)
        if message is not None:
            return cls._build_model_response(
                content=message.content,
                tool_calls=message.tool_calls,
                usage=usage,
            )

        return resp

    @classmethod
    def _completed_stream_response(cls, stream_resp: Any, completed_response: Any) -> Any | None:
        completed = completed_response
        if completed is None:
            completed = cls._get_value(stream_resp, "completed_response", None)

        response_obj = cls._get_value(completed, "response", None)
        if response_obj is None and completed is not None:
            response_obj = completed
        if response_obj is None:
            return None

        normalized = cls._normalize_response_object(response_obj)
        choices = cls._get_value(normalized, "choices", None) or []
        if not choices:
            return None
        return normalized

    @staticmethod
    def _should_stream_upstream(label: str, cfg: ChatEndpointConfig) -> bool:
        return bool(getattr(cfg, "stream", False))

    async def _consume_chat_stream(self, stream_resp: Any) -> Any:
        content_parts: dict[tuple[int, int], str] = {}
        tool_calls: list[dict[str, Any]] = []
        usage: SimpleNamespace | None = None
        completed_response: Any = None

        try:
            async for chunk in stream_resp:
                if chunk is None:
                    continue

                event_type = self._string_value(self._get_value(chunk, "type", "") or "").strip().lower()
                if event_type.endswith("completed"):
                    completed_response = self._get_value(chunk, "response", None) or completed_response

                chunk_usage = self._coerce_usage(self._get_value(chunk, "usage"))
                if chunk_usage is not None:
                    usage = chunk_usage

                if event_type in {"response.output_text.delta", "response.refusal.delta"}:
                    self._merge_stream_content_slot(
                        content_parts,
                        output_index=self._get_value(chunk, "output_index", 0),
                        content_index=self._get_value(chunk, "content_index", 0),
                        piece=str(self._get_value(chunk, "delta", "") or ""),
                    )
                    continue

                if event_type == "response.output_text.done":
                    self._merge_stream_content_slot(
                        content_parts,
                        output_index=self._get_value(chunk, "output_index", 0),
                        content_index=self._get_value(chunk, "content_index", 0),
                        piece=str(self._get_value(chunk, "text", "") or ""),
                    )
                    continue

                if event_type == "response.refusal.done":
                    self._merge_stream_content_slot(
                        content_parts,
                        output_index=self._get_value(chunk, "output_index", 0),
                        content_index=self._get_value(chunk, "content_index", 0),
                        piece=str(self._get_value(chunk, "refusal", "") or ""),
                    )
                    continue

                if event_type == "response.content_part.done":
                    text_piece = self._responses_text_piece(self._get_value(chunk, "part", None))
                    if text_piece:
                        self._merge_stream_content_slot(
                            content_parts,
                            output_index=self._get_value(chunk, "output_index", 0),
                            content_index=self._get_value(chunk, "content_index", 0),
                            piece=text_piece,
                        )
                    continue

                if event_type == "response.output_item.added":
                    output_item = self._get_value(chunk, "item", None)
                    if self._string_value(self._get_value(output_item, "type", "") or "").strip().lower() == "function_call":
                        self._merge_stream_tool_calls(
                            tool_calls,
                            [
                                {
                                    "index": self._get_value(chunk, "output_index", 0),
                                    "id": self._get_value(output_item, "call_id", ""),
                                    "type": "function",
                                    "function": {
                                        "name": self._get_value(output_item, "name", ""),
                                        "arguments": "",
                                    },
                                }
                            ],
                        )
                    continue

                if event_type == "response.output_item.done":
                    output_item = self._get_value(chunk, "item", None)
                    item_type = self._string_value(self._get_value(output_item, "type", "") or "").strip().lower()
                    if item_type == "function_call":
                        self._merge_stream_tool_calls(tool_calls, [output_item])
                    else:
                        text_piece = self._responses_text_piece(self._get_value(output_item, "content", None))
                        if text_piece:
                            self._merge_stream_content_slot(
                                content_parts,
                                output_index=self._get_value(chunk, "output_index", 0),
                                content_index=0,
                                piece=text_piece,
                            )
                    continue

                if event_type == "response.function_call_arguments.delta":
                    self._merge_stream_tool_calls(
                        tool_calls,
                        [
                            {
                                "index": self._get_value(chunk, "output_index", 0),
                                "type": "function",
                                "function": {
                                    "arguments": self._get_value(chunk, "delta", ""),
                                },
                            }
                        ],
                    )
                    continue

                if event_type == "response.function_call_arguments.done":
                    self._merge_stream_tool_calls(
                        tool_calls,
                        [
                            {
                                "index": self._get_value(chunk, "output_index", 0),
                                "type": "function",
                                "function": {
                                    "arguments": self._get_value(chunk, "arguments", ""),
                                },
                            }
                        ],
                    )
                    continue

                choices = self._get_value(chunk, "choices", None) or []
                if not choices:
                    continue

                choice = choices[0]
                delta = self._get_value(choice, "delta", None)
                if delta is None:
                    delta = self._get_value(choice, "message", None)
                if delta is None:
                    delta = choice

                delta_text = self._stream_delta_text(delta)
                if delta_text:
                    self._merge_stream_content_slot(
                        content_parts,
                        output_index=0,
                        content_index=0,
                        piece=delta_text,
                    )

                self._merge_stream_tool_calls(tool_calls, self._get_value(delta, "tool_calls"))
        except Exception:
            normalized = self._completed_stream_response(stream_resp, completed_response)
            if normalized is not None:
                return normalized
            raise

        normalized = self._completed_stream_response(stream_resp, completed_response)
        if normalized is not None:
            return normalized

        return self._build_model_response(
            content=self._joined_stream_content_slots(content_parts),
            tool_calls=self._normalize_tool_calls(tool_calls),
            usage=usage,
        )

    def _consume_chat_stream_sync(self, stream_resp: Any) -> Any:
        content_parts: dict[tuple[int, int], str] = {}
        tool_calls: list[dict[str, Any]] = []
        usage: SimpleNamespace | None = None
        completed_response: Any = None

        try:
            for chunk in stream_resp:
                if chunk is None:
                    continue

                event_type = self._string_value(self._get_value(chunk, "type", "") or "").strip().lower()
                if event_type.endswith("completed"):
                    completed_response = self._get_value(chunk, "response", None) or completed_response

                chunk_usage = self._coerce_usage(self._get_value(chunk, "usage"))
                if chunk_usage is not None:
                    usage = chunk_usage

                if event_type in {"response.output_text.delta", "response.refusal.delta"}:
                    self._merge_stream_content_slot(
                        content_parts,
                        output_index=self._get_value(chunk, "output_index", 0),
                        content_index=self._get_value(chunk, "content_index", 0),
                        piece=str(self._get_value(chunk, "delta", "") or ""),
                    )
                    continue

                if event_type == "response.output_text.done":
                    self._merge_stream_content_slot(
                        content_parts,
                        output_index=self._get_value(chunk, "output_index", 0),
                        content_index=self._get_value(chunk, "content_index", 0),
                        piece=str(self._get_value(chunk, "text", "") or ""),
                    )
                    continue

                if event_type == "response.refusal.done":
                    self._merge_stream_content_slot(
                        content_parts,
                        output_index=self._get_value(chunk, "output_index", 0),
                        content_index=self._get_value(chunk, "content_index", 0),
                        piece=str(self._get_value(chunk, "refusal", "") or ""),
                    )
                    continue

                if event_type == "response.content_part.done":
                    text_piece = self._responses_text_piece(self._get_value(chunk, "part", None))
                    if text_piece:
                        self._merge_stream_content_slot(
                            content_parts,
                            output_index=self._get_value(chunk, "output_index", 0),
                            content_index=self._get_value(chunk, "content_index", 0),
                            piece=text_piece,
                        )
                    continue

                if event_type == "response.output_item.added":
                    output_item = self._get_value(chunk, "item", None)
                    if self._string_value(self._get_value(output_item, "type", "") or "").strip().lower() == "function_call":
                        self._merge_stream_tool_calls(
                            tool_calls,
                            [
                                {
                                    "index": self._get_value(chunk, "output_index", 0),
                                    "id": self._get_value(output_item, "call_id", ""),
                                    "type": "function",
                                    "function": {
                                        "name": self._get_value(output_item, "name", ""),
                                        "arguments": "",
                                    },
                                }
                            ],
                        )
                    continue

                if event_type == "response.output_item.done":
                    output_item = self._get_value(chunk, "item", None)
                    item_type = self._string_value(self._get_value(output_item, "type", "") or "").strip().lower()
                    if item_type == "function_call":
                        self._merge_stream_tool_calls(tool_calls, [output_item])
                    else:
                        text_piece = self._responses_text_piece(self._get_value(output_item, "content", None))
                        if text_piece:
                            self._merge_stream_content_slot(
                                content_parts,
                                output_index=self._get_value(chunk, "output_index", 0),
                                content_index=0,
                                piece=text_piece,
                            )
                    continue

                if event_type == "response.function_call_arguments.delta":
                    self._merge_stream_tool_calls(
                        tool_calls,
                        [
                            {
                                "index": self._get_value(chunk, "output_index", 0),
                                "type": "function",
                                "function": {
                                    "arguments": self._get_value(chunk, "delta", ""),
                                },
                            }
                        ],
                    )
                    continue

                if event_type == "response.function_call_arguments.done":
                    self._merge_stream_tool_calls(
                        tool_calls,
                        [
                            {
                                "index": self._get_value(chunk, "output_index", 0),
                                "type": "function",
                                "function": {
                                    "arguments": self._get_value(chunk, "arguments", ""),
                                },
                            }
                        ],
                    )
                    continue

                choices = self._get_value(chunk, "choices", None) or []
                if not choices:
                    continue

                choice = choices[0]
                delta = self._get_value(choice, "delta", None)
                if delta is None:
                    delta = self._get_value(choice, "message", None)
                if delta is None:
                    delta = choice

                delta_text = self._stream_delta_text(delta)
                if delta_text:
                    self._merge_stream_content_slot(
                        content_parts,
                        output_index=0,
                        content_index=0,
                        piece=delta_text,
                    )

                self._merge_stream_tool_calls(tool_calls, self._get_value(delta, "tool_calls"))
        except Exception:
            normalized = self._completed_stream_response(stream_resp, completed_response)
            if normalized is not None:
                return normalized
            raise

        normalized = self._completed_stream_response(stream_resp, completed_response)
        if normalized is not None:
            return normalized

        return self._build_model_response(
            content=self._joined_stream_content_slots(content_parts),
            tool_calls=self._normalize_tool_calls(tool_calls),
            usage=usage,
        )

    @staticmethod
    def _preview_for_log(text: str, *, limit: int) -> tuple[str, bool]:
        """Render a single-line preview for logs and report whether it was truncated."""
        escaped = (text or "").replace("\r", "\\r").replace("\n", "\\n")
        if len(escaped) <= limit:
            return escaped, False
        return escaped[:limit], True

    def _context_window_total(self, cfg: ChatEndpointConfig | None = None) -> int:
        if self.max_context_tokens > 0:
            return self.max_context_tokens
        try:
            return int(litellm.get_max_tokens(model=(cfg.model if cfg is not None else self.main.model)) or 0)
        except Exception:
            return 0

    @staticmethod
    def _format_prompt_usage(used_tokens: int, total_tokens: int) -> str:
        if total_tokens > 0:
            return f"{max(0, int(used_tokens))}/{int(total_tokens)}"
        return f"{max(0, int(used_tokens))}/?"

    def count_prompt_tokens(
        self,
        messages: list[dict[str, Any]],
        *,
        tools: list[dict[str, Any]] | None = None,
        cfg: ChatEndpointConfig | None = None,
    ) -> int:
        kwargs: dict[str, Any] = {
            "model": (cfg.model if cfg is not None else self.main.model),
            "messages": messages,
        }
        if tools:
            kwargs["tools"] = tools
        try:
            return int(litellm.token_counter(**kwargs))
        except Exception:
            fallback = 0
            for msg in messages:
                fallback += len(str(msg.get("content", "")))
            if tools:
                fallback += len(str(tools))
            return fallback

    def prompt_usage_text(
        self,
        messages: list[dict[str, Any]],
        *,
        tools: list[dict[str, Any]] | None = None,
        cfg: ChatEndpointConfig | None = None,
        used_tokens: int | None = None,
    ) -> str:
        used = self.count_prompt_tokens(messages, tools=tools, cfg=cfg) if used_tokens is None else int(used_tokens)
        total = self._context_window_total(cfg)
        return self._format_prompt_usage(used, total)

    @staticmethod
    def _label_cn(label: str) -> str:
        mapping = {
            "main": "main",
            "decision": "decision",
            "moderation": "moderation",
            "compress": "compress",
            "vision": "vision",
            "embed": "embed",
            "skill": "skill",
        }
        return mapping.get(label, label)

    @staticmethod
    def _retry_attempts(cfg: ChatEndpointConfig | EmbedEndpointConfig) -> int:
        return max(1, int(getattr(cfg, "retry_attempts", 1) or 1))

    @staticmethod
    def _retry_backoff_seconds(cfg: ChatEndpointConfig | EmbedEndpointConfig, attempt: int) -> float:
        base = max(0.0, float(getattr(cfg, "retry_backoff_sec", 0.0) or 0.0))
        return base * max(1, attempt)

    @staticmethod
    def _attempt_timeout_seconds(
        cfg: ChatEndpointConfig | EmbedEndpointConfig,
        attempt: int,
    ) -> float:
        base = max(1.0, float(getattr(cfg, "timeout_sec", 12.0) or 12.0))
        multiplier = max(1.0, float(getattr(cfg, "retry_timeout_multiplier", 1.0) or 1.0))
        return base * (multiplier ** max(0, attempt - 1))

    @staticmethod
    async def _await_with_timeout(awaitable: Any, *, timeout_sec: float) -> Any:
        return await asyncio.wait_for(awaitable, timeout=max(1.0, timeout_sec))

    @staticmethod
    def _uses_responses_api(cfg: ChatEndpointConfig) -> bool:
        return str(getattr(cfg, "chat_endpoint", "chat_completions") or "chat_completions") == "responses"

    @classmethod
    def _convert_responses_content(cls, content: Any) -> Any:
        if isinstance(content, str):
            return content
        if not isinstance(content, list):
            return str(content or "")

        parts: list[dict[str, Any]] = []
        for item in content:
            if isinstance(item, str):
                if item:
                    parts.append({"type": "input_text", "text": item})
                continue
            if not isinstance(item, dict):
                text_value = str(item or "")
                if text_value:
                    parts.append({"type": "input_text", "text": text_value})
                continue

            item_type = str(item.get("type", "") or "").strip().lower()
            if item_type in {"text", "input_text", "output_text"}:
                text_value = item.get("text", "")
                if isinstance(text_value, dict):
                    text_value = text_value.get("value", "")
                text_value = str(text_value or "")
                if text_value:
                    parts.append({"type": "input_text", "text": text_value})
                continue

            if item_type in {"image_url", "input_image"}:
                image_payload = item.get("image_url", item)
                if isinstance(image_payload, dict):
                    image_url = str(image_payload.get("url", "") or image_payload.get("image_url", "") or "").strip()
                    detail = str(image_payload.get("detail", "auto") or "auto").strip()
                else:
                    image_url = str(image_payload or "").strip()
                    detail = "auto"
                if image_url:
                    parts.append(
                        {
                            "type": "input_image",
                            "image_url": image_url,
                            "detail": detail or "auto",
                        }
                    )
                continue

        if not parts:
            return cls._normalize_content_text(content)
        return parts

    @classmethod
    def _messages_to_responses_input(cls, messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        for message in messages:
            if not isinstance(message, dict):
                continue

            role = str(message.get("role", "") or "").strip().lower()
            content = message.get("content", "")

            if role in {"system", "developer", "user"}:
                items.append(
                    {
                        "role": role,
                        "content": cls._convert_responses_content(content),
                    }
                )
                continue

            if role == "assistant":
                normalized_content = cls._convert_responses_content(content)
                if normalized_content:
                    items.append({"role": "assistant", "content": normalized_content})

                raw_tool_calls = message.get("tool_calls") or []
                for tool_call in raw_tool_calls:
                    if not isinstance(tool_call, dict):
                        continue
                    function = tool_call.get("function", {}) or {}
                    name = str(function.get("name", "") or "").strip()
                    arguments = function.get("arguments", "")
                    if isinstance(arguments, dict):
                        arguments = json.dumps(arguments, ensure_ascii=False)
                    arguments = str(arguments or "")
                    call_id = str(tool_call.get("id", "") or "").strip()
                    if name and call_id:
                        items.append(
                            {
                                "type": "function_call",
                                "call_id": call_id,
                                "name": name,
                                "arguments": arguments,
                            }
                        )
                continue

            if role == "tool":
                call_id = str(message.get("tool_call_id", "") or "").strip()
                if not call_id:
                    continue
                output = content if isinstance(content, str) else cls._normalize_content_text(content)
                items.append(
                    {
                        "type": "function_call_output",
                        "call_id": call_id,
                        "output": str(output or ""),
                    }
                )

        return items

    def _responses_sync_request(
        self,
        *,
        messages: list[dict[str, Any]],
        cfg: ChatEndpointConfig,
        stream: bool = False,
        tools: list[dict[str, Any]] | None = None,
        tool_choice: str | None = None,
    ) -> Any:
        responses_fn = getattr(litellm, "responses", None)
        if responses_fn is None:
            raise RuntimeError("installed litellm does not support responses(); please upgrade litellm")

        kwargs = self._build_responses_kwargs(cfg, stream=stream)
        if tools:
            kwargs["tools"] = tools
            if tool_choice:
                kwargs["tool_choice"] = tool_choice

        input_items = self._messages_to_responses_input(messages)
        try:
            resp = responses_fn(input=input_items, **kwargs)
        except TypeError as exc:
            if "unexpected keyword argument 'input'" not in str(exc):
                raise
            resp = responses_fn(messages=messages, **kwargs)
        if stream:
            return self._consume_chat_stream_sync(resp)
        return self._normalize_response_object(resp)

    async def _chat_completion_response_with_retries(
        self,
        *,
        messages: list[dict[str, Any]],
        cfg: ChatEndpointConfig,
        label: str,
        preview_limit: int,
        tools: list[dict[str, Any]] | None = None,
        tool_choice: str | None = None,
    ) -> Any | None:
        label_cn = self._label_cn(label)
        total_attempts = self._retry_attempts(cfg)

        for attempt in range(1, total_attempts + 1):
            stream = self._should_stream_upstream(label, cfg)
            kwargs = self._build_chat_kwargs(cfg, stream=stream)
            timeout_sec = self._attempt_timeout_seconds(cfg, attempt)
            prompt_usage = self.prompt_usage_text(messages, tools=tools, cfg=cfg)
            log.info(
                "LLM request | stage=%s | model=%s | endpoint=%s | attempt=%d/%d | prompt_tokens=%s | messages=%d | tools=%d | timeout=%.1fs | stream=%s",
                label_cn,
                cfg.model,
                getattr(cfg, "endpoint_path", getattr(cfg, "chat_endpoint", "chat_completions")),
                attempt,
                total_attempts,
                prompt_usage,
                len(messages),
                len(tools or []),
                timeout_sec,
                stream,
            )
            try:
                if self._uses_responses_api(cfg):
                    resp = await self._await_with_timeout(
                        asyncio.to_thread(
                            self._responses_sync_request,
                            messages=messages,
                            cfg=cfg,
                            stream=stream,
                            tools=tools,
                            tool_choice=tool_choice,
                        ),
                        timeout_sec=timeout_sec,
                    )
                else:
                    request = (
                        litellm.acompletion(messages=messages, tools=tools, tool_choice=tool_choice, **kwargs)
                        if tools
                        else litellm.acompletion(messages=messages, **kwargs)
                    )
                    raw_resp = await self._await_with_timeout(request, timeout_sec=timeout_sec)
                    if stream:
                        resp = await self._await_with_timeout(
                            self._consume_chat_stream(raw_resp),
                            timeout_sec=timeout_sec,
                        )
                    else:
                        resp = self._normalize_response_object(raw_resp)
                resp = self._normalize_response_object(resp)
                message = resp.choices[0].message
                content = self._normalize_content_text(message.content)
                tool_calls = getattr(message, "tool_calls", None)
                if not content.strip() and not tool_calls:
                    if attempt < total_attempts:
                        log.warning(
                            "LLM empty response | stage=%s | model=%s | attempt=%d/%d | retrying_same_model",
                            label_cn,
                            cfg.model,
                            attempt,
                            total_attempts,
                        )
                        backoff = self._retry_backoff_seconds(cfg, attempt)
                        if backoff > 0:
                            await asyncio.sleep(backoff)
                        continue
                    return None

                usage = getattr(resp, "usage", None)
                tokens_in = getattr(usage, "prompt_tokens", 0)
                tokens_out = getattr(usage, "completion_tokens", 0)
                if not tokens_in:
                    tokens_in = self.count_prompt_tokens(messages, tools=tools, cfg=cfg)
                preview, truncated = self._preview_for_log(content, limit=preview_limit)
                log.info(
                    "LLM response | stage=%s | model=%s | attempt=%d/%d | len=%d | tool_calls=%d | prompt_tokens=%s | output_tokens=%d | preview_truncated=%s | preview=%s",
                    label_cn,
                    cfg.model,
                    attempt,
                    total_attempts,
                    len(content),
                    len(tool_calls or []),
                    self.prompt_usage_text(messages, tools=tools, cfg=cfg, used_tokens=tokens_in),
                    tokens_out,
                    truncated,
                    preview,
                )
                return resp
            except asyncio.TimeoutError:
                if attempt < total_attempts:
                    log.warning(
                        "LLM timeout | stage=%s | model=%s | attempt=%d/%d | timeout=%.1fs | retrying_same_model",
                        label_cn,
                        cfg.model,
                        attempt,
                        total_attempts,
                        timeout_sec,
                    )
                    backoff = self._retry_backoff_seconds(cfg, attempt)
                    if backoff > 0:
                        await asyncio.sleep(backoff)
                    continue
                log.warning(
                    "LLM timeout | stage=%s | model=%s | attempt=%d/%d | timeout=%.1fs | fallback_next",
                    label_cn,
                    cfg.model,
                    attempt,
                    total_attempts,
                    timeout_sec,
                )
            except Exception as exc:
                if attempt < total_attempts:
                    log.warning(
                        "LLM failure | stage=%s | model=%s | attempt=%d/%d | error=%s | retrying_same_model",
                        label_cn,
                        cfg.model,
                        attempt,
                        total_attempts,
                        exc,
                    )
                    backoff = self._retry_backoff_seconds(cfg, attempt)
                    if backoff > 0:
                        await asyncio.sleep(backoff)
                    continue
                log.warning(
                    "LLM failure | stage=%s | model=%s | attempt=%d/%d | error=%s | fallback_next",
                    label_cn,
                    cfg.model,
                    attempt,
                    total_attempts,
                    exc,
                )
        return None

    async def _chat_with_fallbacks(
        self,
        *,
        messages: list[dict[str, Any]],
        candidates: list[ChatEndpointConfig],
        label: str,
        preview_limit: int,
    ) -> str:
        total = len(candidates)
        for idx, cfg in enumerate(candidates, start=1):
            resp = await self._chat_completion_response_with_retries(
                messages=messages,
                cfg=cfg,
                label=label,
                preview_limit=preview_limit,
            )
            if resp is not None:
                return self._normalize_content_text(resp.choices[0].message.content)
            if idx < total:
                continue
            log.error(
                "LLM exhausted | stage=%s | model=%s | no_fallback_left",
                self._label_cn(label),
                cfg.model,
            )
            return ""
        return ""

    async def complete_with_tools(
        self,
        *,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        label: str = "skill",
        cfg: ModelConfig | None = None,
        preview_limit: int = 80,
    ) -> Any | None:
        candidates = self._chat_candidates(cfg or self.main)
        total = len(candidates)
        for idx, candidate in enumerate(candidates, start=1):
            resp = await self._chat_completion_response_with_retries(
                messages=messages,
                cfg=candidate,
                label=label,
                preview_limit=preview_limit,
                tools=tools,
                tool_choice="auto",
            )
            if resp is not None:
                return resp
            if idx < total:
                continue
            log.error(
                "LLM exhausted | stage=%s | model=%s | no_fallback_left",
                self._label_cn(label),
                candidate.model,
            )
        return None

    async def chat(
        self,
        messages: list[dict[str, Any]],
        *,
        use_decision: bool = False,
        use_moderation: bool = False,
    ) -> str:
        """Send chat completion request and return assistant text."""
        if use_moderation:
            cfg = self.moderation_config
            label = "moderation"
        elif use_decision:
            cfg = self.decision_config
            label = "decision"
        else:
            cfg = self.main
            label = "main"

        return await self._chat_with_fallbacks(
            messages=messages,
            candidates=self._chat_candidates(cfg),
            label=label,
            preview_limit=(120 if label == "moderation" else 80),
        )

    async def decision(self, system: str, user_text: str) -> str:
        """Fast decision call (uses decision model)."""
        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": user_text},
        ]
        return await self.chat(messages, use_decision=True)

    async def moderation(self, system: str, user_text: str) -> str:
        """Moderation call (uses moderation model)."""
        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": user_text},
        ]
        return await self.chat(messages, use_moderation=True)

    async def generate(self, system: str, user_text: str) -> str:
        """General generation call (uses main model)."""
        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": user_text},
        ]
        return await self.chat(messages)

    async def compress(self, system: str, user_text: str) -> str:
        """Compress conversation history (uses compress model)."""
        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": user_text},
        ]
        text = await self._chat_with_fallbacks(
            messages=messages,
            candidates=self._chat_candidates(self.compress_config),
            label="compress",
            preview_limit=100,
        )
        if text:
            log.info(
                "LLM compress summary | input_chars=%d | output_chars=%d",
                len(user_text),
                len(text),
            )
        return text

    async def vision_describe(self, image_url: str, prompt: str) -> str:
        """Image understanding with the dedicated vision model."""
        messages: list[dict[str, Any]] = [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {"type": "image_url", "image_url": {"url": image_url}},
                ],
            }
        ]
        return await self._chat_with_fallbacks(
            messages=messages,
            candidates=self._chat_candidates(self.vision_config),
            label="vision",
            preview_limit=80,
        )

    async def embed(self, texts: list[str]) -> list[list[float]]:
        """Generate embeddings."""
        candidates = self._embed_candidates(self.embed_config)
        total = len(candidates)
        for idx, cfg in enumerate(candidates, start=1):
            total_attempts = self._retry_attempts(cfg)
            for attempt in range(1, total_attempts + 1):
                kwargs = self._build_embed_kwargs(cfg, texts)
                timeout_sec = self._attempt_timeout_seconds(cfg, attempt)
                log.info(
                    "LLM request | stage=embed | model=%s | attempt=%d/%d | texts=%d | timeout=%.1fs",
                    cfg.model,
                    attempt,
                    total_attempts,
                    len(texts),
                    timeout_sec,
                )
                try:
                    resp = await self._await_with_timeout(
                        litellm.aembedding(**kwargs),
                        timeout_sec=timeout_sec,
                    )
                    embeddings = [item["embedding"] for item in resp.data]
                    if not embeddings:
                        if attempt < total_attempts:
                            log.warning(
                                "LLM empty response | stage=embed | model=%s | attempt=%d/%d | retrying_same_model",
                                cfg.model,
                                attempt,
                                total_attempts,
                            )
                            backoff = self._retry_backoff_seconds(cfg, attempt)
                            if backoff > 0:
                                await asyncio.sleep(backoff)
                            continue
                        break
                    log.info(
                        "LLM response | stage=embed | model=%s | attempt=%d/%d | dims=%d",
                        cfg.model,
                        attempt,
                        total_attempts,
                        len(embeddings[0]),
                    )
                    return embeddings
                except asyncio.TimeoutError:
                    if attempt < total_attempts:
                        log.warning(
                            "LLM timeout | stage=embed | model=%s | attempt=%d/%d | timeout=%.1fs | retrying_same_model",
                            cfg.model,
                            attempt,
                            total_attempts,
                            timeout_sec,
                        )
                        backoff = self._retry_backoff_seconds(cfg, attempt)
                        if backoff > 0:
                            await asyncio.sleep(backoff)
                        continue
                    log.warning(
                        "LLM timeout | stage=embed | model=%s | attempt=%d/%d | timeout=%.1fs | fallback_next",
                        cfg.model,
                        attempt,
                        total_attempts,
                        timeout_sec,
                    )
                    break
                except Exception as exc:
                    if attempt < total_attempts:
                        log.warning(
                            "LLM failure | stage=embed | model=%s | attempt=%d/%d | error=%s | retrying_same_model",
                            cfg.model,
                            attempt,
                            total_attempts,
                            exc,
                        )
                        backoff = self._retry_backoff_seconds(cfg, attempt)
                        if backoff > 0:
                            await asyncio.sleep(backoff)
                        continue
                    log.warning(
                        "LLM failure | stage=embed | model=%s | attempt=%d/%d | error=%s | fallback_next",
                        cfg.model,
                        attempt,
                        total_attempts,
                        exc,
                    )
                    break
            if idx < total:
                continue
            log.error("LLM exhausted | stage=embed | model=%s | no_fallback_left", cfg.model)
            return []
        return []
