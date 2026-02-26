from __future__ import annotations

import logging
from typing import Any

import litellm

from bot.config import EmbedConfig, ModelConfig

log = logging.getLogger(__name__)

# Disable extra LiteLLM debug logs.
litellm.suppress_debug_info = True


class LLMService:
    """Unified LLM interface for main/decision/moderation/compress/embed."""

    def __init__(
        self,
        main: ModelConfig,
        decision: ModelConfig,
        compress: ModelConfig | None = None,
        *,
        moderation: ModelConfig | None = None,
        embed: EmbedConfig | None = None,
    ) -> None:
        self.main = main
        self.decision_config = decision
        self.moderation_config = moderation or decision
        self.compress_config = compress or main
        self.embed_config = embed or EmbedConfig()

    @staticmethod
    def _build_kwargs(cfg: ModelConfig) -> dict[str, Any]:
        kwargs: dict[str, Any] = {
            "model": cfg.model,
            "temperature": cfg.temperature,
            "max_tokens": cfg.max_tokens,
        }
        if cfg.api_key:
            kwargs["api_key"] = cfg.api_key
        if cfg.api_base:
            kwargs["api_base"] = cfg.api_base
        return kwargs

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
    def _preview_for_log(text: str, *, limit: int) -> tuple[str, bool]:
        """Render a single-line preview for logs and report whether it was truncated."""
        escaped = (text or "").replace("\r", "\\r").replace("\n", "\\n")
        if len(escaped) <= limit:
            return escaped, False
        return escaped[:limit], True

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

        kwargs = self._build_kwargs(cfg)
        log.info(
            "[LLM:%s] model=%s, messages=%d, max_tokens=%d",
            label,
            cfg.model,
            len(messages),
            cfg.max_tokens,
        )
        try:
            resp = await litellm.acompletion(messages=messages, **kwargs)
            content = resp.choices[0].message.content
            text = self._normalize_content_text(content)
            tokens_in = getattr(resp.usage, "prompt_tokens", 0)
            tokens_out = getattr(resp.usage, "completion_tokens", 0)
            preview_limit = 1200 if label == "moderation" else 240
            preview, truncated = self._preview_for_log(text, limit=preview_limit)
            log.info(
                "[LLM:%s] response_len=%d preview_truncated=%s preview=%s (in=%d out=%d tokens)",
                label,
                len(text),
                truncated,
                preview,
                tokens_in,
                tokens_out,
            )
            return text
        except Exception:
            log.exception("[LLM:%s] call failed model=%s", label, cfg.model)
            return ""

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
        kwargs = self._build_kwargs(self.compress_config)
        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": user_text},
        ]
        log.info(
            "[LLM:compress] model=%s, input_len=%d chars",
            self.compress_config.model,
            len(user_text),
        )
        try:
            resp = await litellm.acompletion(messages=messages, **kwargs)
            content = resp.choices[0].message.content
            text = self._normalize_content_text(content)
            log.info(
                "[LLM:compress] done: %d chars -> %d chars",
                len(user_text),
                len(text),
            )
            return text
        except Exception:
            log.exception("[LLM:compress] call failed model=%s", self.compress_config.model)
            return ""

    async def vision_describe(self, image_url: str, prompt: str) -> str:
        """Image understanding with the main model."""
        kwargs = self._build_kwargs(self.main)
        messages: list[dict[str, Any]] = [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {"type": "image_url", "image_url": {"url": image_url}},
                ],
            }
        ]
        log.info("[LLM:vision] model=%s, sending image", self.main.model)
        try:
            resp = await litellm.acompletion(messages=messages, **kwargs)
            content = resp.choices[0].message.content
            text = self._normalize_content_text(content).strip()
            tokens_in = getattr(resp.usage, "prompt_tokens", 0)
            tokens_out = getattr(resp.usage, "completion_tokens", 0)
            preview, truncated = self._preview_for_log(text, limit=240)
            log.info(
                "[LLM:vision] response_len=%d preview_truncated=%s preview=%s (in=%d out=%d tokens)",
                len(text),
                truncated,
                preview,
                tokens_in,
                tokens_out,
            )
            return text
        except Exception:
            log.exception("[LLM:vision] call failed model=%s", self.main.model)
            return ""

    async def embed(self, texts: list[str]) -> list[list[float]]:
        """Generate embeddings."""
        cfg = self.embed_config
        kwargs: dict[str, Any] = {"model": cfg.model, "input": texts}
        if cfg.api_key:
            kwargs["api_key"] = cfg.api_key
        if cfg.api_base:
            kwargs["api_base"] = cfg.api_base
        log.info("[LLM:embed] model=%s, texts=%d", cfg.model, len(texts))
        try:
            resp = await litellm.aembedding(**kwargs)
            embeddings = [item["embedding"] for item in resp.data]
            log.info("[LLM:embed] done, dim=%d", len(embeddings[0]) if embeddings else 0)
            return embeddings
        except Exception:
            log.exception("[LLM:embed] call failed model=%s", cfg.model)
            return []
