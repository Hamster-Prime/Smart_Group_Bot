import unittest
from unittest.mock import patch

from bot import config as config_module
from bot.config import Settings


class ChatBridgeConfigTests(unittest.TestCase):
    def test_load_settings_builds_independent_chat_bridge_model(self) -> None:
        settings = Settings(_env_file=None)
        settings.main_provider_name = "ark"
        settings.main_model = "main-reply"
        settings.main_fallbacks = ""
        settings.chat_bridge_provider_name = "gemini"
        settings.chat_bridge_model = "bridge-reply"
        settings.chat_bridge_fallbacks = "ark:bridge-fallback"
        settings.decision_provider_name = "ark"
        settings.decision_model = "decision-model"
        settings.decision_fallbacks = ""
        settings.moderation_provider_name = "ark"
        settings.moderation_model = "moderation-model"
        settings.moderation_fallbacks = ""
        settings.compress_provider_name = "ark"
        settings.compress_model = "compress-model"
        settings.compress_fallbacks = ""
        settings.embed_provider_name = "gemini"
        settings.embed_model = "embed-model"
        settings.embed_fallbacks = ""

        raw_env = {
            "MODEL_PROVIDER_ARK_PROVIDER": "openai_compatible",
            "MODEL_PROVIDER_ARK_API_KEY": "ark-key",
            "MODEL_PROVIDER_ARK_API_BASE": "https://ark.example/v1",
            "MODEL_PROVIDER_ARK_STREAM": "true",
            "MODEL_PROVIDER_GEMINI_PROVIDER": "gemini",
            "MODEL_PROVIDER_GEMINI_API_KEY": "gemini-key",
            "MODEL_PROVIDER_GEMINI_API_BASE": "https://gemini.example",
            "MODEL_PROVIDER_GEMINI_STREAM": "false",
        }

        with (
            patch("bot.config.Settings", return_value=settings),
            patch("bot.config._load_raw_env", return_value=raw_env),
        ):
            loaded = config_module.load_settings(config_path="missing.toml")

        self.assertEqual(loaded.bot.main_model.model, "openai/main-reply")
        self.assertEqual(loaded.bot.main_model.chat_endpoint, "chat_completions")
        self.assertEqual(loaded.bot.chat_bridge_model.model, "gemini/bridge-reply")
        self.assertEqual(loaded.bot.chat_bridge_model.api_key, "gemini-key")
        self.assertEqual(loaded.bot.chat_bridge_model.api_base, "https://gemini.example")
        self.assertTrue(loaded.bot.main_model.stream)
        self.assertFalse(loaded.bot.chat_bridge_model.stream)
        self.assertEqual(len(loaded.bot.chat_bridge_model.fallbacks), 1)
        self.assertEqual(loaded.bot.chat_bridge_model.fallbacks[0].model, "openai/bridge-fallback")
        self.assertTrue(loaded.bot.chat_bridge_model.fallbacks[0].stream)

    def test_load_settings_defaults_chat_bridge_model_to_main_binding(self) -> None:
        settings = Settings(_env_file=None)
        settings.main_provider_name = "ark"
        settings.main_model = "main-reply"
        settings.decision_provider_name = "ark"
        settings.decision_model = "decision-model"
        settings.moderation_provider_name = "ark"
        settings.moderation_model = "moderation-model"
        settings.compress_provider_name = "ark"
        settings.compress_model = "compress-model"
        settings.embed_provider_name = "ark"
        settings.embed_model = "embed-model"

        raw_env = {
            "MODEL_PROVIDER_ARK_PROVIDER": "openai_compatible",
            "MODEL_PROVIDER_ARK_API_KEY": "ark-key",
            "MODEL_PROVIDER_ARK_API_BASE": "https://ark.example/v1",
        }

        with (
            patch("bot.config.Settings", return_value=settings),
            patch("bot.config._load_raw_env", return_value=raw_env),
        ):
            loaded = config_module.load_settings(config_path="missing.toml")

        self.assertEqual(loaded.bot.chat_bridge_model.model, "openai/main-reply")
        self.assertEqual(loaded.bot.chat_bridge_model.api_key, "ark-key")
        self.assertEqual(loaded.bot.chat_bridge_model.chat_endpoint, "chat_completions")

    def test_load_settings_supports_anthropic_provider_profile(self) -> None:
        settings = Settings(_env_file=None)
        settings.main_provider_name = "anthropic"
        settings.main_model = "claude-sonnet"
        settings.main_fallbacks = "ark:main-fallback"
        settings.chat_bridge_provider_name = "anthropic"
        settings.chat_bridge_model = "claude-haiku"
        settings.chat_bridge_fallbacks = ""
        settings.decision_provider_name = "anthropic"
        settings.decision_model = "claude-decision"
        settings.decision_fallbacks = ""
        settings.moderation_provider_name = "anthropic"
        settings.moderation_model = "claude-moderation"
        settings.moderation_fallbacks = ""
        settings.compress_provider_name = "anthropic"
        settings.compress_model = "claude-compress"
        settings.compress_fallbacks = ""
        settings.embed_provider_name = "ark"
        settings.embed_model = "text-embedding-3-small"
        settings.embed_fallbacks = ""

        raw_env = {
            "MODEL_PROVIDER_ANTHROPIC_PROVIDER": "anthropic",
            "MODEL_PROVIDER_ANTHROPIC_API_KEY": "anthropic-key",
            "MODEL_PROVIDER_ARK_PROVIDER": "openai_compatible",
            "MODEL_PROVIDER_ARK_API_KEY": "ark-key",
            "MODEL_PROVIDER_ARK_API_BASE": "https://ark.example/v1",
        }

        with (
            patch("bot.config.Settings", return_value=settings),
            patch("bot.config._load_raw_env", return_value=raw_env),
        ):
            loaded = config_module.load_settings(config_path="missing.toml")

        self.assertEqual(loaded.bot.main_model.model, "anthropic/claude-sonnet")
        self.assertEqual(loaded.bot.main_model.api_key, "anthropic-key")
        self.assertIsNone(loaded.bot.main_model.api_base)
        self.assertEqual(len(loaded.bot.main_model.fallbacks), 1)
        self.assertEqual(loaded.bot.main_model.fallbacks[0].model, "openai/main-fallback")
        self.assertEqual(loaded.bot.chat_bridge_model.model, "anthropic/claude-haiku")
        self.assertEqual(loaded.bot.decision_model.model, "anthropic/claude-decision")
        self.assertEqual(loaded.bot.embed_model.model, "openai/text-embedding-3-small")

    def test_load_settings_defaults_openai_provider_to_responses_api(self) -> None:
        settings = Settings(_env_file=None)
        settings.main_provider_name = "openai_main"
        settings.main_model = "gpt-4.1"
        settings.decision_provider_name = "openai_main"
        settings.decision_model = "gpt-4.1-mini"
        settings.moderation_provider_name = "openai_main"
        settings.moderation_model = "gpt-4.1-mini"
        settings.compress_provider_name = "openai_main"
        settings.compress_model = "gpt-4.1-nano"
        settings.embed_provider_name = "openai_main"
        settings.embed_model = "text-embedding-3-small"

        raw_env = {
            "MODEL_PROVIDER_OPENAI_MAIN_PROVIDER": "openai",
            "MODEL_PROVIDER_OPENAI_MAIN_API_KEY": "openai-key",
        }

        with (
            patch("bot.config.Settings", return_value=settings),
            patch("bot.config._load_raw_env", return_value=raw_env),
        ):
            loaded = config_module.load_settings(config_path="missing.toml")

        self.assertEqual(loaded.bot.main_model.model, "openai/gpt-4.1")
        self.assertEqual(loaded.bot.main_model.chat_endpoint, "responses")
        self.assertEqual(loaded.bot.decision_model.chat_endpoint, "responses")

    def test_load_settings_allows_openai_compatible_switch_to_responses_api(self) -> None:
        settings = Settings(_env_file=None)
        settings.main_provider_name = "ark"
        settings.main_model = "gpt-4.1"
        settings.decision_provider_name = "ark"
        settings.decision_model = "gpt-4.1-mini"
        settings.moderation_provider_name = "ark"
        settings.moderation_model = "gpt-4.1-mini"
        settings.compress_provider_name = "ark"
        settings.compress_model = "gpt-4.1-nano"
        settings.embed_provider_name = "ark"
        settings.embed_model = "text-embedding-3-small"

        raw_env = {
            "MODEL_PROVIDER_ARK_PROVIDER": "openai_compatible",
            "MODEL_PROVIDER_ARK_API_KEY": "ark-key",
            "MODEL_PROVIDER_ARK_API_BASE": "https://ark.example/v1",
            "MODEL_PROVIDER_ARK_CHAT_ENDPOINT": "/responses",
        }

        with (
            patch("bot.config.Settings", return_value=settings),
            patch("bot.config._load_raw_env", return_value=raw_env),
        ):
            loaded = config_module.load_settings(config_path="missing.toml")

        self.assertEqual(loaded.bot.main_model.chat_endpoint, "responses")
        self.assertEqual(loaded.bot.main_model.api_base, "https://ark.example/v1")


if __name__ == "__main__":
    unittest.main()
