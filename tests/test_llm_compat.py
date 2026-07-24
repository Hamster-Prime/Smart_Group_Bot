import unittest

from bot.config import ChatEndpointConfig, _build_litellm_model, _canonical_provider
from bot.services.llm import LLMService, _IncompleteInlineReasoningError


class InlineReasoningStripTests(unittest.TestCase):
    def test_strips_closed_think_block(self) -> None:
        text = "<think>internal monologue</think>\n\n我是 MiniMax-M3。"
        self.assertEqual(LLMService._normalize_content_text(text), "我是 MiniMax-M3。")

    def test_rejects_unclosed_think_block(self) -> None:
        text = "<think>The user is asking me to introduce myself in one sen"
        with self.assertRaises(_IncompleteInlineReasoningError):
            LLMService._normalize_content_text(text)

    def test_strips_multiple_blocks_and_stray_close_tag(self) -> None:
        text = "<think>a</think>回答一</think><reasoning>b</reasoning>回答二"
        self.assertEqual(LLMService._normalize_content_text(text), "回答一回答二")

    def test_rejects_prefixed_answer_with_unclosed_tag(self) -> None:
        text = "答案在前面。<think>被截断的推理"
        with self.assertRaises(_IncompleteInlineReasoningError):
            LLMService._normalize_content_text(text)

    def test_does_not_strip_similarly_named_tags(self) -> None:
        text = "<analysis_result>可见内容</analysis_result>结尾"
        self.assertEqual(LLMService._normalize_content_text(text), text)

    def test_strips_nested_reasoning_blocks_without_leaking_inner_text(self) -> None:
        text = "<think>外层<think>内层</think>外层结尾</think>完整答案"
        self.assertEqual(LLMService._normalize_content_text(text), "完整答案")

    def test_rejects_mismatched_reasoning_tags(self) -> None:
        text = "<think>内部推理</analysis>不完整答案"
        with self.assertRaises(_IncompleteInlineReasoningError):
            LLMService._normalize_content_text(text)

    def test_plain_text_untouched(self) -> None:
        text = "1 < 2 and 3 > 2, plain text with <code>x</code>"
        self.assertEqual(LLMService._normalize_content_text(text), text)

    def test_json_payload_survives_stripping(self) -> None:
        text = '<think>判断违规</think>\n{"violation": false, "reason": "正常"}'
        self.assertEqual(
            LLMService._normalize_content_text(text),
            '{"violation": false, "reason": "正常"}',
        )


class AnthropicApiBaseTests(unittest.TestCase):
    def test_v1_suffix_is_stripped(self) -> None:
        resolved = LLMService._resolve_request_api_base(
            provider="anthropic",
            api_base="http://10.0.0.53/v1",
            endpoint_path="/v1/messages",
            model="anthropic/claude-sonnet-4-6",
        )
        self.assertEqual(resolved, "http://10.0.0.53")

    def test_messages_suffix_is_kept(self) -> None:
        resolved = LLMService._resolve_request_api_base(
            provider="anthropic",
            api_base="http://10.0.0.53/v1/messages",
            endpoint_path="/v1/messages",
            model="anthropic/claude-sonnet-4-6",
        )
        self.assertEqual(resolved, "http://10.0.0.53/v1/messages")

    def test_provider_inferred_from_model_prefix(self) -> None:
        resolved = LLMService._resolve_request_api_base(
            provider="",
            api_base="http://10.0.0.53/v1",
            endpoint_path="/v1/messages",
            model="anthropic/claude-sonnet-4-6",
        )
        self.assertEqual(resolved, "http://10.0.0.53")

    def test_official_base_untouched(self) -> None:
        resolved = LLMService._resolve_request_api_base(
            provider="anthropic",
            api_base="https://api.anthropic.com",
            endpoint_path="/v1/messages",
            model="anthropic/claude-sonnet-4-6",
        )
        self.assertEqual(resolved, "https://api.anthropic.com")


class UnsupportedParamTests(unittest.TestCase):
    def _bad_request(self, message: str):
        import litellm

        return litellm.BadRequestError(message=message, model="x", llm_provider="openai")

    def test_detects_rejected_temperature(self) -> None:
        exc = self._bad_request("invalid temperature: only 1 is allowed for this model")
        self.assertEqual(LLMService._unsupported_param_from_error(exc), "temperature")

    def test_ignores_unrelated_bad_request(self) -> None:
        exc = self._bad_request("model not found")
        self.assertEqual(LLMService._unsupported_param_from_error(exc), "")

    def test_ignores_non_400_errors(self) -> None:
        import litellm

        exc = litellm.RateLimitError(
            message="invalid temperature mention in quota text", model="x", llm_provider="openai"
        )
        self.assertEqual(LLMService._unsupported_param_from_error(exc), "")

    def test_chat_kwargs_exclude_params(self) -> None:
        cfg = ChatEndpointConfig(
            model="openai/kimi-k3",
            provider="openai_compatible",
            api_key="sk-x",
        )
        kwargs = LLMService._build_chat_kwargs(cfg, exclude_params={"temperature"})
        self.assertNotIn("temperature", kwargs)
        self.assertIn("max_tokens", kwargs)

    def test_responses_kwargs_exclude_params_map_to_responses_names(self) -> None:
        cfg = ChatEndpointConfig(
            model="openai/gpt-5.4-mini",
            provider="openai_compatible",
            api_key="sk-x",
            chat_endpoint="responses",
            endpoint_path="/responses",
            reasoning_effort="low",
        )
        kwargs = LLMService._build_responses_kwargs(
            cfg, exclude_params={"max_tokens", "reasoning_effort"}
        )
        self.assertNotIn("max_output_tokens", kwargs)
        self.assertNotIn("reasoning", kwargs)


class ResponsesToolFormatTests(unittest.TestCase):
    def test_nested_function_tools_are_flattened(self) -> None:
        tools = [
            {
                "type": "function",
                "function": {
                    "name": "get_weather",
                    "description": "查询天气",
                    "parameters": {"type": "object", "properties": {}},
                },
            }
        ]
        converted = LLMService._tools_to_responses_format(tools)
        self.assertEqual(
            converted,
            [
                {
                    "type": "function",
                    "name": "get_weather",
                    "description": "查询天气",
                    "parameters": {"type": "object", "properties": {}},
                }
            ],
        )

    def test_already_flat_tools_pass_through(self) -> None:
        tools = [{"type": "function", "name": "f", "parameters": {}}]
        self.assertEqual(LLMService._tools_to_responses_format(tools), tools)


class ProviderModelPrefixTests(unittest.TestCase):
    def test_alias_maps_to_native_provider(self) -> None:
        self.assertEqual(_canonical_provider("kimi"), "moonshot")
        self.assertEqual(_canonical_provider("doubao"), "volcengine")
        self.assertEqual(_canonical_provider("google"), "gemini")
        self.assertEqual(_canonical_provider("minimax"), "minimax")

    def test_native_provider_keeps_prefix(self) -> None:
        self.assertEqual(
            _build_litellm_model("minimax", "minimax-m3"),
            "minimax/minimax-m3",
        )

    def test_unknown_provider_with_custom_base_falls_back_to_openai_prefix(self) -> None:
        self.assertEqual(
            _build_litellm_model("siliconflow", "some-model", api_base="https://api.example.com/v1"),
            "openai/some-model",
        )

    def test_unknown_provider_without_base_keeps_prefix(self) -> None:
        self.assertEqual(
            _build_litellm_model("siliconflow", "some-model"),
            "siliconflow/some-model",
        )

    def test_openai_compatible_prefix(self) -> None:
        self.assertEqual(
            _build_litellm_model("openai_compatible", "minimax-m3"),
            "openai/minimax-m3",
        )


if __name__ == "__main__":
    unittest.main()
