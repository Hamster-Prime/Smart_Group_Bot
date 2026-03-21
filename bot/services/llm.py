from __future__ import annotations

import logging
from typing import Any

import litellm

from bot.config import ChatEndpointConfig, EmbedConfig, EmbedEndpointConfig, ModelConfig

log = logging.getLogger(__name__)

# Disable extra LiteLLM debug logs.
litellm.suppress_debug_info = True
litellm.set_verbose = False


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
        max_context_tokens: int | None = None,
    ) -> None:
        self.main = main
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
        }
        if cfg.api_key:
            kwargs["api_key"] = cfg.api_key
        if cfg.api_base:
            kwargs["api_base"] = cfg.api_base
        return kwargs

    @staticmethod
    def _build_embed_kwargs(cfg: EmbedEndpointConfig, texts: list[str]) -> dict[str, Any]:
        kwargs: dict[str, Any] = {"model": cfg.model, "input": texts}
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
                temperature=cfg.temperature,
                max_tokens=cfg.max_tokens,
            ),
            *cfg.fallbacks,
        ]

    @staticmethod
    def _embed_candidates(cfg: EmbedConfig) -> list[EmbedEndpointConfig]:
        return [
            EmbedEndpointConfig(model=cfg.model, api_key=cfg.api_key, api_base=cfg.api_base),
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
            "main": "主回复",
            "decision": "决策",
            "moderation": "审核",
            "compress": "压缩",
            "vision": "视觉",
            "embed": "嵌入",
        }
        return mapping.get(label, label)

    async def _chat_with_fallbacks(
        self,
        *,
        messages: list[dict[str, Any]],
        candidates: list[ChatEndpointConfig],
        label: str,
        preview_limit: int,
    ) -> str:
        label_cn = self._label_cn(label)
        total = len(candidates)
        for idx, cfg in enumerate(candidates, start=1):
            kwargs = self._build_chat_kwargs(cfg)
            prompt_usage = self.prompt_usage_text(messages, cfg=cfg)
            log.info(
                "【LLM请求】阶段=%s | 尝试=%d/%d | 模型=%s | prompt_tokens=%s | 消息数=%d | max_tokens=%d",
                label_cn,
                idx,
                total,
                cfg.model,
                prompt_usage,
                len(messages),
                cfg.max_tokens,
            )
            try:
                resp = await litellm.acompletion(messages=messages, **kwargs)
                content = resp.choices[0].message.content
                text = self._normalize_content_text(content)
                if not text.strip():
                    log.warning(
                        "【LLM空响应】阶段=%s | 尝试=%d/%d | 模型=%s",
                        label_cn,
                        idx,
                        total,
                        cfg.model,
                    )
                    continue
                tokens_in = getattr(resp.usage, "prompt_tokens", 0)
                tokens_out = getattr(resp.usage, "completion_tokens", 0)
                preview, truncated = self._preview_for_log(text, limit=preview_limit)
                log.info(
                    "【LLM返回】阶段=%s | 尝试=%d/%d | 长度=%d | prompt_tokens=%s | 输出tokens=%d | 预览截断=%s | 预览=%s",
                    label_cn,
                    idx,
                    total,
                    len(text),
                    self.prompt_usage_text(messages, cfg=cfg, used_tokens=tokens_in),
                    tokens_out,
                    truncated,
                    preview,
                )
                return text
            except Exception as exc:
                if idx < total:
                    log.warning(
                        "【LLM失败】阶段=%s | 尝试=%d/%d | 模型=%s | 错误=%s | 准备fallback",
                        label_cn,
                        idx,
                        total,
                        cfg.model,
                        exc,
                    )
                    continue
                log.exception(
                    "【LLM失败】阶段=%s | 尝试=%d/%d | 模型=%s (已无fallback)",
                    label_cn,
                    idx,
                    total,
                    cfg.model,
                )
                return ""
        return ""

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
                "【LLM返回】阶段=%s | 压缩前=%d字符 | 压缩后=%d字符",
                self._label_cn("compress"),
                len(user_text),
                len(text),
            )
        return text

    async def vision_describe(self, image_url: str, prompt: str) -> str:
        """Image understanding with the main model."""
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
            candidates=self._chat_candidates(self.main),
            label="vision",
            preview_limit=80,
        )

    async def embed(self, texts: list[str]) -> list[list[float]]:
        """Generate embeddings."""
        candidates = self._embed_candidates(self.embed_config)
        total = len(candidates)
        for idx, cfg in enumerate(candidates, start=1):
            kwargs = self._build_embed_kwargs(cfg, texts)
            log.info(
                "【LLM请求】阶段=%s | 尝试=%d/%d | 模型=%s | 文本数=%d",
                self._label_cn("embed"),
                idx,
                total,
                cfg.model,
                len(texts),
            )
            try:
                resp = await litellm.aembedding(**kwargs)
                embeddings = [item["embedding"] for item in resp.data]
                if not embeddings:
                    log.warning(
                        "【LLM空响应】阶段=%s | 尝试=%d/%d | 模型=%s",
                        self._label_cn("embed"),
                        idx,
                        total,
                        cfg.model,
                    )
                    continue
                log.info(
                    "【LLM返回】阶段=%s | 尝试=%d/%d | 向量维度=%d",
                    self._label_cn("embed"),
                    idx,
                    total,
                    len(embeddings[0]),
                )
                return embeddings
            except Exception as exc:
                if idx < total:
                    log.warning(
                        "【LLM失败】阶段=%s | 尝试=%d/%d | 模型=%s | 错误=%s | 准备fallback",
                        self._label_cn("embed"),
                        idx,
                        total,
                        cfg.model,
                        exc,
                    )
                    continue
                log.exception(
                    "【LLM失败】阶段=%s | 尝试=%d/%d | 模型=%s (已无fallback)",
                    self._label_cn("embed"),
                    idx,
                    total,
                    cfg.model,
                )
                return []
        return []
