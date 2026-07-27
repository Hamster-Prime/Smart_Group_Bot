"""按模型角色可配置的 LLM 总时限（total_deadline_sec）。

覆盖三层：
- LLMService._stage_deadline_seconds 的 cfg 覆盖与 0 回落内置默认；
- _chat_candidates/_embed_candidates 把主配置的总时限传播到候选；
- _chat_with_fallbacks / complete_with_tools / embed 真正使用覆盖值。
"""

import asyncio
import unittest
from typing import Any
from unittest.mock import AsyncMock, patch

from bot.config import ChatEndpointConfig, EmbedConfig, EmbedEndpointConfig, ModelConfig
from bot.services import llm as llm_module
from bot.services.llm import LLMService


class StageDeadlineResolutionTests(unittest.TestCase):
    def test_zero_or_missing_override_falls_back_to_builtin_default(self) -> None:
        cfg = ChatEndpointConfig(model="openai/gpt-4.1")
        self.assertEqual(cfg.total_deadline_sec, 0.0)
        self.assertEqual(
            LLMService._stage_deadline_seconds("moderation", cfg),
            llm_module._LLM_STAGE_DEADLINES["moderation"],
        )
        self.assertEqual(
            LLMService._stage_deadline_seconds("moderation"),
            llm_module._LLM_STAGE_DEADLINES["moderation"],
        )
        self.assertEqual(LLMService._stage_deadline_seconds("unknown-stage"), 120.0)

    def test_positive_override_replaces_builtin_default(self) -> None:
        cfg = ChatEndpointConfig(model="openai/gpt-4.1", total_deadline_sec=75.0)
        self.assertEqual(LLMService._stage_deadline_seconds("moderation", cfg), 75.0)
        embed_cfg = EmbedEndpointConfig(
            model="openai/text-embedding-3-small", total_deadline_sec=42.0
        )
        self.assertEqual(LLMService._stage_deadline_seconds("embed", embed_cfg), 42.0)

    def test_builtin_defaults_still_cover_every_stage(self) -> None:
        for stage in ("main", "vision", "decision", "moderation", "compress", "embed", "skill"):
            self.assertGreater(LLMService._stage_deadline_seconds(stage), 0.0)

    def test_chat_candidates_propagate_total_deadline(self) -> None:
        cfg = ModelConfig(
            model="openai/gpt-4.1",
            total_deadline_sec=90.0,
            fallbacks=[ChatEndpointConfig(model="openai/gpt-4.1-mini")],
        )
        candidates = LLMService._chat_candidates(cfg)
        self.assertEqual(candidates[0].total_deadline_sec, 90.0)

    def test_embed_candidates_propagate_total_deadline(self) -> None:
        cfg = EmbedConfig(
            model="openai/text-embedding-3-small",
            total_deadline_sec=33.0,
        )
        candidates = LLMService._embed_candidates(cfg)
        self.assertEqual(candidates[0].total_deadline_sec, 33.0)


class StageDeadlineEnforcementTests(unittest.IsolatedAsyncioTestCase):
    def _make_llm(self, **model_kwargs: Any) -> LLMService:
        cfg = ModelConfig(
            model="openai/gpt-4.1",
            api_key="test-key",
            timeout_sec=5.0,
            retry_attempts=1,
            retry_backoff_sec=0.0,
            retry_timeout_multiplier=1.0,
            **model_kwargs,
        )
        return LLMService(cfg, cfg, moderation=cfg, compress=cfg)

    async def test_tiny_override_cuts_off_a_slow_call(self) -> None:
        llm = self._make_llm(total_deadline_sec=0.05)

        async def _slow_completion(**_kwargs: Any) -> Any:
            await asyncio.sleep(5.0)

        with (
            patch("bot.services.llm.litellm.acompletion", side_effect=_slow_completion),
            patch("bot.services.llm.litellm.token_counter", return_value=128),
            patch("bot.services.llm.litellm.get_max_tokens", return_value=8192),
        ):
            started = asyncio.get_running_loop().time()
            result = await llm.moderation("sys", "hi")
            elapsed = asyncio.get_running_loop().time() - started

        self.assertEqual(result, "")
        self.assertLess(elapsed, 2.0)

    async def test_tiny_override_cuts_off_complete_with_tools(self) -> None:
        llm = self._make_llm(total_deadline_sec=0.05)

        async def _slow_completion(**_kwargs: Any) -> Any:
            await asyncio.sleep(5.0)

        with (
            patch("bot.services.llm.litellm.acompletion", side_effect=_slow_completion),
            patch("bot.services.llm.litellm.token_counter", return_value=128),
            patch("bot.services.llm.litellm.get_max_tokens", return_value=8192),
        ):
            started = asyncio.get_running_loop().time()
            resp = await llm.complete_with_tools(
                messages=[{"role": "user", "content": "hi"}],
                tools=[],
            )
            elapsed = asyncio.get_running_loop().time() - started

        self.assertIsNone(resp)
        self.assertLess(elapsed, 2.0)

    async def test_tiny_override_cuts_off_embed(self) -> None:
        cfg = ModelConfig(
            model="openai/gpt-4.1",
            api_key="test-key",
            timeout_sec=5.0,
            retry_attempts=1,
            retry_backoff_sec=0.0,
            retry_timeout_multiplier=1.0,
        )
        embed = EmbedConfig(
            model="openai/text-embedding-3-small",
            api_key="test-key",
            timeout_sec=5.0,
            retry_attempts=1,
            retry_backoff_sec=0.0,
            retry_timeout_multiplier=1.0,
            total_deadline_sec=0.05,
        )
        llm = LLMService(cfg, cfg, embed=embed)

        async def _slow_embedding(**_kwargs: Any) -> Any:
            await asyncio.sleep(5.0)

        with patch("bot.services.llm.litellm.aembedding", side_effect=_slow_embedding):
            started = asyncio.get_running_loop().time()
            result = await llm.embed(["hello"])
            elapsed = asyncio.get_running_loop().time() - started

        self.assertEqual(result, [])
        self.assertLess(elapsed, 2.0)

    async def test_generous_override_lets_a_slow_success_finish(self) -> None:
        # 内置 moderation 默认是 35s；把默认打到 0.01s 会掐断的场景下，
        # 正向覆盖必须放行。这里用响应耗时 0.2s、覆盖 30s 验证覆盖生效路径。
        llm = self._make_llm(total_deadline_sec=30.0)

        async def _slowish_completion(**_kwargs: Any) -> Any:
            await asyncio.sleep(0.2)
            from types import SimpleNamespace

            return SimpleNamespace(
                choices=[
                    SimpleNamespace(
                        message=SimpleNamespace(content="ok", tool_calls=[])
                    )
                ],
                usage=SimpleNamespace(prompt_tokens=1, completion_tokens=1),
            )

        with (
            patch("bot.services.llm.litellm.acompletion", side_effect=_slowish_completion),
            patch("bot.services.llm.litellm.token_counter", return_value=128),
            patch("bot.services.llm.litellm.get_max_tokens", return_value=8192),
            patch.dict(llm_module._LLM_STAGE_DEADLINES, {"moderation": 0.01}),
        ):
            result = await llm.moderation("sys", "hi")

        self.assertEqual(result, "ok")


class LegacyEnvWiringTests(unittest.TestCase):
    def test_settings_expose_per_role_total_deadline_fields(self) -> None:
        from bot.config import Settings

        settings = Settings(_env_file=None)
        for role in ("main", "vision", "decision", "moderation", "compress", "embed"):
            self.assertEqual(
                getattr(settings, f"{role}_total_deadline_sec"), 0.0, role
            )

        settings = Settings(_env_file=None, moderation_total_deadline_sec=50.0)
        self.assertEqual(settings.moderation_total_deadline_sec, 50.0)


if __name__ == "__main__":
    unittest.main()
