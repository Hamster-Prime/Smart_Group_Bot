from __future__ import annotations

import logging
from typing import Any

import litellm

from bot.config import EmbedConfig, ModelConfig

log = logging.getLogger(__name__)

# 关闭 litellm 额外调试日志
litellm.suppress_debug_info = True


class LLMService:
    """统一 LLM 接口：主模型、决策模型、压缩模型、嵌入模型。"""

    def __init__(
        self,
        main: ModelConfig,
        decision: ModelConfig,
        compress: ModelConfig | None = None,
        embed: EmbedConfig | None = None,
    ) -> None:
        self.main = main
        self.decision_config = decision
        self.compress_config = compress or main
        self.embed_config = embed or EmbedConfig()

    def _build_kwargs(self, cfg: ModelConfig) -> dict[str, Any]:
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
        """兼容不同模型返回结构，提取文本。"""
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

    async def chat(
        self,
        messages: list[dict[str, Any]],
        *,
        use_decision: bool = False,
    ) -> str:
        """发送对话请求，返回 assistant 文本。"""
        cfg = self.decision_config if use_decision else self.main
        label = "decision" if use_decision else "main"
        kwargs = self._build_kwargs(cfg)
        log.info("[LLM:%s] model=%s, messages=%d条", label, cfg.model, len(messages))
        try:
            resp = await litellm.acompletion(messages=messages, **kwargs)
            content = resp.choices[0].message.content
            text = self._normalize_content_text(content)
            tokens_in = getattr(resp.usage, "prompt_tokens", 0)
            tokens_out = getattr(resp.usage, "completion_tokens", 0)
            log.info("[LLM:%s] 响应: %s (in=%d out=%d tokens)", label, text[:120], tokens_in, tokens_out)
            return text
        except Exception:
            log.exception("[LLM:%s] 调用失败 model=%s", label, cfg.model)
            return ""

    async def decision(self, system: str, user_text: str) -> str:
        """快速决策调用（使用决策模型）。"""
        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": user_text},
        ]
        return await self.chat(messages, use_decision=True)

    async def generate(self, system: str, user_text: str) -> str:
        """生成回复（使用主模型）。"""
        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": user_text},
        ]
        return await self.chat(messages)

    async def compress(self, system: str, user_text: str) -> str:
        """对历史对话进行压缩（使用压缩模型）。"""
        kwargs = self._build_kwargs(self.compress_config)
        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": user_text},
        ]
        log.info("[LLM:compress] model=%s, input_len=%d chars", self.compress_config.model, len(user_text))
        try:
            resp = await litellm.acompletion(messages=messages, **kwargs)
            content = resp.choices[0].message.content
            text = self._normalize_content_text(content)
            log.info("[LLM:compress] 压缩完成: %d chars -> %d chars", len(user_text), len(text))
            return text
        except Exception:
            log.exception("[LLM:compress] 调用失败 model=%s", self.compress_config.model)
            return ""

    async def vision_describe(self, image_url: str, prompt: str) -> str:
        """图片理解：让模型读取图片并输出简短中文描述。"""
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
        log.info("[LLM:vision] model=%s, 发送1张图片", self.main.model)
        try:
            resp = await litellm.acompletion(messages=messages, **kwargs)
            content = resp.choices[0].message.content
            text = self._normalize_content_text(content).strip()
            tokens_in = getattr(resp.usage, "prompt_tokens", 0)
            tokens_out = getattr(resp.usage, "completion_tokens", 0)
            log.info("[LLM:vision] 响应: %s (in=%d out=%d tokens)", text[:120], tokens_in, tokens_out)
            return text
        except Exception:
            log.exception("[LLM:vision] 调用失败 model=%s", self.main.model)
            return ""

    async def embed(self, texts: list[str]) -> list[list[float]]:
        """生成文本向量。"""
        cfg = self.embed_config
        kwargs: dict[str, Any] = {"model": cfg.model, "input": texts}
        if cfg.api_key:
            kwargs["api_key"] = cfg.api_key
        if cfg.api_base:
            kwargs["api_base"] = cfg.api_base
        log.info("[LLM:embed] model=%s, texts=%d条", cfg.model, len(texts))
        try:
            resp = await litellm.aembedding(**kwargs)
            embeddings = [item["embedding"] for item in resp.data]
            log.info("[LLM:embed] 完成, dim=%d", len(embeddings[0]) if embeddings else 0)
            return embeddings
        except Exception:
            log.exception("[LLM:embed] 调用失败 model=%s", cfg.model)
            return []
