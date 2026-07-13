import unittest

from bot.config import ChatEndpointConfig, ModelConfig
from bot.services.llm import LLMService


class ReasoningEffortKwargsTests(unittest.TestCase):
    def test_chat_kwargs_include_reasoning_effort_when_set(self) -> None:
        cfg = ChatEndpointConfig(
            model="openai/gpt-5.4-mini",
            provider="openai_compatible",
            api_key="sk-x",
            reasoning_effort="none",
        )

        kwargs = LLMService._build_chat_kwargs(cfg)

        self.assertEqual(kwargs["reasoning_effort"], "none")
        self.assertTrue(kwargs["drop_params"])

    def test_chat_kwargs_omit_reasoning_effort_when_empty(self) -> None:
        cfg = ChatEndpointConfig(
            model="openai/gpt-5.4-mini",
            provider="openai_compatible",
            api_key="sk-x",
        )

        kwargs = LLMService._build_chat_kwargs(cfg)

        self.assertNotIn("reasoning_effort", kwargs)
        self.assertNotIn("drop_params", kwargs)

    def test_responses_kwargs_include_reasoning_effort_when_set(self) -> None:
        cfg = ChatEndpointConfig(
            model="openai/gpt-5.3-codex",
            provider="openai_compatible",
            api_key="sk-x",
            chat_endpoint="responses",
            endpoint_path="/responses",
            reasoning_effort="low",
        )

        kwargs = LLMService._build_responses_kwargs(cfg)

        self.assertEqual(kwargs["reasoning"], {"effort": "low"})

    def test_fallbacks_inherit_role_reasoning_effort(self) -> None:
        from bot.config import _build_chat_config, ProviderProfile

        profiles = {
            "ark": ProviderProfile(provider="openai_compatible", api_key="k1"),
            "gemini": ProviderProfile(provider="gemini", api_key="k2"),
        }

        cfg = _build_chat_config(
            profiles=profiles,
            provider_name="ark",
            model_name="gpt-5.4-mini",
            temperature=0.7,
            max_tokens=256,
            timeout_sec=10.0,
            retry_attempts=1,
            retry_backoff_sec=0.0,
            retry_timeout_multiplier=1.0,
            fallback_spec="gemini:gemini-2.0-flash",
            reasoning_effort="none",
        )

        self.assertEqual(cfg.reasoning_effort, "none")
        self.assertEqual(cfg.fallbacks[0].reasoning_effort, "none")

    def test_chat_candidates_preserve_primary_reasoning_effort(self) -> None:
        cfg = ModelConfig(
            model="openai/gpt-5.4-mini",
            provider="openai_compatible",
            api_key="sk-x",
            reasoning_effort="none",
            fallbacks=[
                ChatEndpointConfig(
                    model="gemini/gemini-2.0-flash",
                    provider="gemini",
                    reasoning_effort="none",
                )
            ],
        )

        candidates = LLMService._chat_candidates(cfg)

        self.assertEqual(candidates[0].reasoning_effort, "none")
        self.assertEqual(candidates[1].reasoning_effort, "none")
        primary_kwargs = LLMService._build_chat_kwargs(candidates[0])
        self.assertEqual(primary_kwargs["reasoning_effort"], "none")

    def test_invalid_reasoning_effort_normalizes_to_empty(self) -> None:
        from bot.config import _normalize_reasoning_effort

        self.assertEqual(_normalize_reasoning_effort("LOW"), "low")
        self.assertEqual(_normalize_reasoning_effort(" none "), "none")
        self.assertEqual(_normalize_reasoning_effort("bogus"), "")
        self.assertEqual(_normalize_reasoning_effort(""), "")
        self.assertEqual(_normalize_reasoning_effort("off"), "none")
        self.assertEqual(_normalize_reasoning_effort("disable"), "none")


if __name__ == "__main__":
    unittest.main()
