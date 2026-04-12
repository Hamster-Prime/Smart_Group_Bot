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

    @staticmethod
    def _build_chat_kwargs(cfg: ChatEndpointConfig) -> dict[str, Any]:
        kwargs: dict[str, Any] = {
            "model": cfg.model,
            "temperature": cfg.temperature,
            "max_tokens": cfg.max_tokens,
            "timeout": max(1.0, float(getattr(cfg, "timeout_sec", 12.0) or 12.0)),
        }
        if getattr(cfg, "stream", False):
            kwargs["stream"] = True
        if cfg.api_key:
            kwargs["api_key"] = cfg.api_key
        if cfg.api_base:
            kwargs["api_base"] = cfg.api_base
        return kwargs

    @staticmethod
    def _build_embed_kwargs(cfg: EmbedEndpointConfig, texts: list[str]) -> dict[str, Any]:
        kwargs: dict[str, Any] = {
            "model": cfg.model,
            "input": texts,
            "timeout": max(1.0, float(getattr(cfg, "timeout_sec", 10.0) or 10.0)),
        }
        if cfg.api_key:
            kwargs["api_key"] = cfg.api_key
        if cfg.api_base:
            kwargs["api_base"] = cfg.api_base
        return kwargs

    @staticmethod
    def _chat_candidates(cfg: ModelConfig) -> list[ChatEndpointConfig]:
        return [
            ChatEndpointConfig(
                model=cfg.model,
                api_key=cfg.api_key,
                api_base=cfg.api_base,
                stream=cfg.stream,
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
                api_key=cfg.api_key,
                api_base=cfg.api_base,
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
                if isinstance(item, dict) and item.get("type") == "text":
                    txt = str(item.get("text", "")).strip()
                    if txt:
                        parts.append(txt)
            return "\n".join(parts)
        return str(content or "")

    @staticmethod
    def _get_value(obj: Any, key: str, default: Any = None) -> Any:
        if isinstance(obj, dict):
            return obj.get(key, default)
        return getattr(obj, key, default)

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

            call_type = str(cls._get_value(raw_call, "type", "") or "")
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
        return SimpleNamespace(
            prompt_tokens=int(cls._get_value(usage, "prompt_tokens", 0) or 0),
            completion_tokens=int(cls._get_value(usage, "completion_tokens", 0) or 0),
            total_tokens=int(cls._get_value(usage, "total_tokens", 0) or 0),
        )

    async def _consume_chat_stream(self, stream_resp: Any) -> Any:
        content_parts: list[str] = []
        tool_calls: list[dict[str, Any]] = []
        usage: SimpleNamespace | None = None

        async for chunk in stream_resp:
            if chunk is None:
                continue

            chunk_usage = self._coerce_usage(self._get_value(chunk, "usage"))
            if chunk_usage is not None:
                usage = chunk_usage

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
                content_parts.append(delta_text)

            self._merge_stream_tool_calls(tool_calls, self._get_value(delta, "tool_calls"))

        normalized_tool_calls: list[dict[str, Any]] = []
        for idx, tool_call in enumerate(tool_calls, start=1):
            function = tool_call.get("function", {}) if isinstance(tool_call, dict) else {}
            name = str(function.get("name", "") or "").strip()
            arguments = str(function.get("arguments", "") or "")
            if not name and not arguments:
                continue
            normalized_tool_calls.append(
                {
                    "id": str(tool_call.get("id", "") or "").strip() or f"call_{idx}",
                    "type": str(tool_call.get("type", "") or "").strip() or "function",
                    "function": {
                        "name": name,
                        "arguments": arguments,
                    },
                }
            )

        return SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(
                        content="".join(content_parts),
                        tool_calls=normalized_tool_calls,
                    )
                )
            ],
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
            kwargs = self._build_chat_kwargs(cfg)
            timeout_sec = self._attempt_timeout_seconds(cfg, attempt)
            prompt_usage = self.prompt_usage_text(messages, tools=tools, cfg=cfg)
            log.info(
                "LLM request | stage=%s | model=%s | attempt=%d/%d | prompt_tokens=%s | messages=%d | tools=%d | timeout=%.1fs | stream=%s",
                label_cn,
                cfg.model,
                attempt,
                total_attempts,
                prompt_usage,
                len(messages),
                len(tools or []),
                timeout_sec,
                bool(getattr(cfg, "stream", False)),
            )
            try:
                request = (
                    litellm.acompletion(messages=messages, tools=tools, tool_choice=tool_choice, **kwargs)
                    if tools
                    else litellm.acompletion(messages=messages, **kwargs)
                )
                raw_resp = await self._await_with_timeout(request, timeout_sec=timeout_sec)
                if getattr(cfg, "stream", False):
                    resp = await self._await_with_timeout(
                        self._consume_chat_stream(raw_resp),
                        timeout_sec=timeout_sec,
                    )
                else:
                    resp = raw_resp
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
