import unittest

from pydantic import ValidationError

from bot.config import (
    ChatEndpointConfig,
    ProviderProfile,
    _build_chat_config,
)
from bot.services.llm import LLMService
from bot.services.runtime_config import (
    ChatRoleConfig,
    ModelFallbackConfig,
    ModelSettingsConfig,
    _normalize_deprecated_runtime_payload,
)


class RequestParamsKwargsTests(unittest.TestCase):
    def test_chat_kwargs_merge_custom_params_without_overriding_system_fields(self) -> None:
        cfg = ChatEndpointConfig(
            model="openai/gpt-5.4-mini",
            provider="openai_compatible",
            api_key="sk-x",
            request_params={
                "reasoning_effort": "low",
                "top_p": 0.8,
                "max_tokens": 999999,
                "timeout": 0.1,
                "api_key": "attacker-key",
                "allowed_openai_params": ["api_key"],
            },
        )
        kwargs = LLMService._build_chat_kwargs(cfg)
        self.assertEqual(
            kwargs["extra_body"],
            {"reasoning_effort": "low", "top_p": 0.8},
        )
        self.assertNotIn("reasoning_effort", kwargs)
        self.assertNotIn("top_p", kwargs)
        self.assertNotIn("allowed_openai_params", kwargs["extra_body"])
        self.assertEqual(kwargs["max_tokens"], 2048)
        self.assertEqual(kwargs["timeout"], 12.0)
        self.assertEqual(kwargs["api_key"], "sk-x")

    def test_responses_kwargs_accept_endpoint_native_reasoning_json(self) -> None:
        cfg = ChatEndpointConfig(
            model="openai/gpt-5.3-codex",
            provider="openai_compatible",
            api_key="sk-x",
            chat_endpoint="responses",
            endpoint_path="/responses",
            request_params={"reasoning": {"effort": "low"}, "top_p": 0.9},
        )
        kwargs = LLMService._build_responses_kwargs(cfg)
        self.assertEqual(
            kwargs["extra_body"],
            {"reasoning": {"effort": "low"}, "top_p": 0.9},
        )

    def test_exclude_params_remove_system_params_after_custom_merge(self) -> None:
        cfg = ChatEndpointConfig(
            model="openai/gpt-5.4-mini",
            provider="openai_compatible",
            api_key="sk-x",
            chat_endpoint="responses",
            endpoint_path="/responses",
            request_params={"reasoning": {"effort": "low"}},
        )
        kwargs = LLMService._build_responses_kwargs(cfg, exclude_params={"max_tokens"})
        self.assertNotIn("max_output_tokens", kwargs)
        self.assertEqual(kwargs["extra_body"], {"reasoning": {"effort": "low"}})

    def test_openai_custom_body_bypasses_litellm_model_param_validation(self) -> None:
        from litellm.utils import get_optional_params

        cfg = ChatEndpointConfig(
            model="openai/minimax-m3",
            provider="openai_compatible",
            request_params={
                "thinking": {"type": "enabled"},
                "reasoning_effort": "low",
            },
        )
        kwargs = LLMService._build_chat_kwargs(cfg)
        optional = get_optional_params(
            model="minimax-m3",
            custom_llm_provider="openai",
            extra_body=kwargs["extra_body"],
        )
        self.assertEqual(
            optional["extra_body"],
            {"thinking": {"type": "enabled"}, "reasoning_effort": "low"},
        )

    def test_native_provider_keeps_provider_parameter_mapping(self) -> None:
        cfg = ChatEndpointConfig(
            model="deepseek/deepseek-reasoner",
            provider="deepseek",
            request_params={"thinking": {"type": "enabled"}},
        )
        self.assertEqual(
            LLMService._custom_request_kwargs(cfg),
            {"thinking": {"type": "enabled"}},
        )


class PerModelRequestParamsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.profiles = {
            "ark": ProviderProfile(provider="openai_compatible", api_key="k1"),
            "gemini": ProviderProfile(provider="gemini", api_key="k2"),
        }

    def test_primary_and_fallback_params_are_independent(self) -> None:
        cfg = _build_chat_config(
            profiles=self.profiles,
            provider_name="ark",
            model_name="gpt-5.4-mini",
            temperature=0.7,
            max_tokens=256,
            timeout_sec=10.0,
            retry_attempts=1,
            retry_backoff_sec=0.0,
            retry_timeout_multiplier=1.0,
            fallback_spec="gemini:gemini-2.0-flash",
            request_params={"reasoning_effort": "low", "top_p": 0.7},
            fallback_request_params=[{"reasoning_effort": "high"}],
        )
        self.assertEqual(
            cfg.request_params,
            {"reasoning_effort": "low", "top_p": 0.7},
        )
        self.assertEqual(cfg.fallbacks[0].request_params, {"reasoning_effort": "high"})

    def test_fallback_without_params_is_empty(self) -> None:
        cfg = _build_chat_config(
            profiles=self.profiles,
            provider_name="ark",
            model_name="gpt-5.4-mini",
            temperature=0.7,
            max_tokens=256,
            timeout_sec=10.0,
            retry_attempts=1,
            retry_backoff_sec=0.0,
            retry_timeout_multiplier=1.0,
            fallback_spec="gemini:gemini-2.0-flash",
            request_params={"thinking": {"type": "enabled"}},
        )
        self.assertEqual(cfg.request_params, {"thinking": {"type": "enabled"}})
        self.assertEqual(cfg.fallbacks[0].request_params, {})

    def test_reasoning_effort_is_no_longer_a_model_config_field(self) -> None:
        cfg = ChatEndpointConfig(model="openai/gpt-5.4-mini")
        self.assertFalse(hasattr(cfg, "reasoning_effort"))


class RequestParamsValidationTests(unittest.TestCase):
    def test_nested_provider_json_is_preserved(self) -> None:
        role = ChatRoleConfig(
            request_params={"thinking": {"type": "enabled"}, "reasoning_effort": "low"}
        )
        self.assertEqual(
            role.request_params,
            {"thinking": {"type": "enabled"}, "reasoning_effort": "low"},
        )

    def test_request_params_must_be_a_json_object(self) -> None:
        for value in ([], "{}", {"value": float("nan")}):
            with self.subTest(value=value), self.assertRaises(ValidationError):
                ChatRoleConfig(request_params=value)

    def test_fallback_request_params_must_be_a_json_object(self) -> None:
        fallback = ModelFallbackConfig(provider="gemini", model="gemini-2.0-flash")
        self.assertEqual(fallback.request_params, {})
        with self.assertRaises(ValidationError):
            ModelFallbackConfig(provider="gemini", request_params=[])

    def test_model_overrides_are_removed_from_schema(self) -> None:
        self.assertFalse(hasattr(ModelSettingsConfig(), "model_overrides"))
        with self.assertRaises(ValidationError):
            ModelSettingsConfig(model_overrides={})

    def test_old_role_field_migrates_to_request_params(self) -> None:
        payload = {
            "models": {"main": {"reasoning_effort": "low"}},
            "bot": {"drop_pending_updates": False},
        }
        normalized, changed = _normalize_deprecated_runtime_payload(payload)
        self.assertTrue(changed)
        self.assertNotIn("reasoning_effort", normalized["models"]["main"])
        self.assertEqual(
            normalized["models"]["main"]["request_params"],
            {"reasoning_effort": "low"},
        )

    def test_old_role_default_and_global_overrides_migrate_to_each_model(self) -> None:
        payload = {
            "models": {
                "providers": [
                    {"name": "ark", "provider": "openai_compatible", "api_base": ""},
                    {"name": "gemini", "provider": "gemini", "api_base": ""},
                ],
                "main": {
                    "provider": "ark",
                    "model": "gpt-5.4-mini",
                    "request_params": {"top_p": 0.7},
                    "fallbacks": [
                        {"provider": "gemini", "model": "gemini-2.0-flash"},
                        {"provider": "gemini", "model": "gemini-2.0-pro"},
                    ],
                },
                "model_overrides": {
                    "openai/gpt-5.4-mini": {"reasoning_effort": "high"},
                    "gemini/gemini-2.0-flash": {"thinking": {"type": "disabled"}},
                },
            },
            "bot": {"drop_pending_updates": False},
        }
        normalized, changed = _normalize_deprecated_runtime_payload(payload)
        self.assertTrue(changed)
        models = normalized["models"]
        self.assertNotIn("model_overrides", models)
        self.assertEqual(models["main"]["request_params"], {"reasoning_effort": "high"})
        self.assertEqual(
            models["main"]["fallbacks"][0]["request_params"],
            {"thinking": {"type": "disabled"}},
        )
        self.assertEqual(
            models["main"]["fallbacks"][1]["request_params"],
            {"top_p": 0.7},
        )


if __name__ == "__main__":
    unittest.main()
