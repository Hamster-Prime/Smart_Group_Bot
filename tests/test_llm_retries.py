import asyncio
import unittest
from enum import Enum
from typing import Any
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from bot.config import ChatEndpointConfig, EmbedConfig, ModelConfig
from bot.services.llm import LLMService


def _chat_resp(*, content: Any = "", tool_calls: list[dict] | None = None) -> SimpleNamespace:
    return SimpleNamespace(
        choices=[
            SimpleNamespace(
                message=SimpleNamespace(
                    content=content,
                    tool_calls=tool_calls or [],
                )
            )
        ],
        usage=SimpleNamespace(prompt_tokens=42, completion_tokens=7),
    )


def _responses_resp(
    *,
    content: str = "",
    refusal: str = "",
    tool_calls: list[dict] | None = None,
    usage: Any | None = None,
) -> SimpleNamespace:
    output: list[Any] = []
    message_parts: list[Any] = []
    if content:
        message_parts.append(SimpleNamespace(type="output_text", text=content))
    if refusal:
        message_parts.append(SimpleNamespace(type="refusal", refusal=refusal))
    if message_parts:
        output.append(
            SimpleNamespace(
                type="message",
                role="assistant",
                content=message_parts,
            )
        )

    for idx, tool_call in enumerate(tool_calls or [], start=1):
        function = tool_call.get("function", {}) if isinstance(tool_call, dict) else {}
        call_id = tool_call.get("id", f"call-{idx}") if isinstance(tool_call, dict) else f"call-{idx}"
        output.append(
            SimpleNamespace(
                type="function_call",
                id=call_id,
                call_id=call_id,
                name=function.get("name", ""),
                arguments=function.get("arguments", ""),
                status="completed",
            )
        )

    return SimpleNamespace(
        output=output,
        usage=usage or {"input_tokens": 42, "output_tokens": 7, "total_tokens": 49},
    )


def _embed_resp(vector: list[float]) -> SimpleNamespace:
    return SimpleNamespace(data=[{"embedding": vector}])


class _AsyncStream:
    def __init__(self, chunks: list[Any]) -> None:
        self._chunks = list(chunks)

    def __aiter__(self) -> "_AsyncStream":
        return self

    async def __anext__(self) -> Any:
        if not self._chunks:
            raise StopAsyncIteration
        return self._chunks.pop(0)


class _SyncStream:
    def __init__(self, chunks: list[Any]) -> None:
        self._chunks = list(chunks)

    def __iter__(self) -> "_SyncStream":
        return self

    def __next__(self) -> Any:
        if not self._chunks:
            raise StopIteration
        return self._chunks.pop(0)


class _EventType(Enum):
    OUTPUT_TEXT_DELTA = "response.output_text.delta"
    RESPONSE_COMPLETED = "response.completed"


class LLMRetryTests(unittest.IsolatedAsyncioTestCase):
    def _make_llm(self) -> LLMService:
        main = ModelConfig(
            model="openai/gpt-4.1",
            timeout_sec=1.0,
            retry_attempts=2,
            retry_backoff_sec=0.0,
            retry_timeout_multiplier=1.0,
            fallbacks=[
                ChatEndpointConfig(
                    model="openai/gpt-4.1-mini",
                    timeout_sec=1.0,
                    retry_attempts=2,
                    retry_backoff_sec=0.0,
                    retry_timeout_multiplier=1.0,
                )
            ],
        )
        decision = ModelConfig(
            model="openai/gpt-4.1-mini",
            timeout_sec=1.0,
            retry_attempts=2,
            retry_backoff_sec=0.0,
            retry_timeout_multiplier=1.0,
        )
        embed = EmbedConfig(
            model="openai/text-embedding-3-small",
            timeout_sec=1.0,
            retry_attempts=2,
            retry_backoff_sec=0.0,
            retry_timeout_multiplier=1.0,
        )
        return LLMService(main, decision, moderation=decision, compress=main, embed=embed)

    async def test_chat_retries_same_model_before_fallback(self) -> None:
        llm = self._make_llm()
        mock_completion = AsyncMock(side_effect=[asyncio.TimeoutError(), _chat_resp(content="ok")])

        with (
            patch("bot.services.llm.litellm.acompletion", mock_completion),
            patch("bot.services.llm.litellm.token_counter", return_value=128),
            patch("bot.services.llm.litellm.get_max_tokens", return_value=8192),
        ):
            result = await llm.generate("sys", "hi")

        self.assertEqual(result, "ok")
        self.assertEqual(mock_completion.await_count, 2)
        self.assertEqual(mock_completion.await_args_list[0].kwargs["model"], "openai/gpt-4.1")
        self.assertEqual(mock_completion.await_args_list[1].kwargs["model"], "openai/gpt-4.1")

    async def test_chat_falls_back_after_same_model_retries(self) -> None:
        llm = self._make_llm()
        mock_completion = AsyncMock(
            side_effect=[
                asyncio.TimeoutError(),
                asyncio.TimeoutError(),
                _chat_resp(content="fallback-ok"),
            ]
        )

        with (
            patch("bot.services.llm.litellm.acompletion", mock_completion),
            patch("bot.services.llm.litellm.token_counter", return_value=128),
            patch("bot.services.llm.litellm.get_max_tokens", return_value=8192),
        ):
            result = await llm.generate("sys", "hi")

        self.assertEqual(result, "fallback-ok")
        self.assertEqual(mock_completion.await_count, 3)
        self.assertEqual(mock_completion.await_args_list[0].kwargs["model"], "openai/gpt-4.1")
        self.assertEqual(mock_completion.await_args_list[1].kwargs["model"], "openai/gpt-4.1")
        self.assertEqual(mock_completion.await_args_list[2].kwargs["model"], "openai/gpt-4.1-mini")

    async def test_complete_with_tools_accepts_empty_content_when_tool_calls_exist(self) -> None:
        llm = self._make_llm()
        mock_completion = AsyncMock(
            side_effect=[
                RuntimeError("temporary failure"),
                _chat_resp(
                    tool_calls=[
                        {
                            "id": "call-1",
                            "function": {
                                "name": "websearch",
                                "arguments": '{"query":"weather"}',
                            },
                        }
                    ]
                ),
            ]
        )

        with (
            patch("bot.services.llm.litellm.acompletion", mock_completion),
            patch("bot.services.llm.litellm.token_counter", return_value=128),
            patch("bot.services.llm.litellm.get_max_tokens", return_value=8192),
        ):
            resp = await llm.complete_with_tools(
                messages=[{"role": "user", "content": "weather"}],
                tools=[
                    {
                        "type": "function",
                        "function": {
                            "name": "websearch",
                            "parameters": {"type": "object"},
                        },
                    }
                ],
            )

        self.assertIsNotNone(resp)
        self.assertEqual(mock_completion.await_count, 2)
        self.assertEqual(resp.choices[0].message.tool_calls[0]["function"]["name"], "websearch")

    async def test_generate_normalizes_anthropic_text_blocks(self) -> None:
        llm = self._make_llm()
        mock_completion = AsyncMock(
            return_value=_chat_resp(
                content=[
                    {"type": "text", "text": "first line"},
                    {"type": "tool_use", "name": "ignored"},
                    {"type": "text", "text": "second line"},
                ]
            )
        )

        with (
            patch("bot.services.llm.litellm.acompletion", mock_completion),
            patch("bot.services.llm.litellm.token_counter", return_value=128),
            patch("bot.services.llm.litellm.get_max_tokens", return_value=8192),
        ):
            result = await llm.generate("sys", "hi")

        self.assertEqual(result, "first line\nsecond line")
        self.assertEqual(mock_completion.await_count, 1)

    async def test_generate_supports_streaming_provider_requests(self) -> None:
        llm = self._make_llm()
        llm.main.stream = True
        mock_completion = AsyncMock(
            return_value=_AsyncStream(
                [
                    SimpleNamespace(
                        choices=[SimpleNamespace(delta=SimpleNamespace(content="hello "))],
                    ),
                    SimpleNamespace(
                        choices=[SimpleNamespace(delta=SimpleNamespace(content="world"))],
                        usage=SimpleNamespace(prompt_tokens=42, completion_tokens=2),
                    ),
                ]
            )
        )

        with (
            patch("bot.services.llm.litellm.acompletion", mock_completion),
            patch("bot.services.llm.litellm.token_counter", return_value=128),
            patch("bot.services.llm.litellm.get_max_tokens", return_value=8192),
        ):
            result = await llm.generate("sys", "hi")

        self.assertEqual(result, "hello world")
        self.assertEqual(mock_completion.await_count, 1)
        self.assertTrue(mock_completion.await_args.kwargs["stream"])

    async def test_complete_with_tools_supports_streaming_tool_calls(self) -> None:
        llm = self._make_llm()
        llm.main.stream = True
        mock_completion = AsyncMock(
            return_value=_AsyncStream(
                [
                    SimpleNamespace(
                        choices=[
                            SimpleNamespace(
                                delta=SimpleNamespace(
                                    tool_calls=[
                                        SimpleNamespace(
                                            index=0,
                                            id="call-1",
                                            function=SimpleNamespace(
                                                name="web",
                                                arguments='{"que',
                                            ),
                                        )
                                    ]
                                )
                            )
                        ]
                    ),
                    SimpleNamespace(
                        choices=[
                            SimpleNamespace(
                                delta=SimpleNamespace(
                                    tool_calls=[
                                        SimpleNamespace(
                                            index=0,
                                            function=SimpleNamespace(
                                                name="search",
                                                arguments='ry":"weather"}',
                                            ),
                                        )
                                    ]
                                )
                            )
                        ]
                    ),
                ]
            )
        )

        with (
            patch("bot.services.llm.litellm.acompletion", mock_completion),
            patch("bot.services.llm.litellm.token_counter", return_value=128),
            patch("bot.services.llm.litellm.get_max_tokens", return_value=8192),
        ):
            resp = await llm.complete_with_tools(
                messages=[{"role": "user", "content": "weather"}],
                tools=[
                    {
                        "type": "function",
                        "function": {
                            "name": "websearch",
                            "parameters": {"type": "object"},
                        },
                    }
                ],
            )

        self.assertIsNotNone(resp)
        self.assertEqual(resp.choices[0].message.tool_calls[0]["function"]["name"], "websearch")
        self.assertEqual(resp.choices[0].message.tool_calls[0]["function"]["arguments"], '{"query":"weather"}')
        self.assertTrue(mock_completion.await_args.kwargs["stream"])

    async def test_embed_retries_same_model_before_fallback(self) -> None:
        llm = self._make_llm()
        mock_embedding = AsyncMock(side_effect=[asyncio.TimeoutError(), _embed_resp([0.1, 0.2])])

        with patch("bot.services.llm.litellm.aembedding", mock_embedding):
            result = await llm.embed(["hello"])

        self.assertEqual(result, [[0.1, 0.2]])
        self.assertEqual(mock_embedding.await_count, 2)
        self.assertEqual(mock_embedding.await_args_list[0].kwargs["model"], "openai/text-embedding-3-small")
        self.assertEqual(mock_embedding.await_args_list[1].kwargs["model"], "openai/text-embedding-3-small")

    async def test_generate_uses_responses_api_for_openai_provider(self) -> None:
        llm = self._make_llm()
        llm.main.provider = "openai"
        llm.main.chat_endpoint = "responses"
        llm.main.endpoint_path = "/responses"
        mock_responses = Mock(return_value=_responses_resp(content="ok"))
        mock_completion = AsyncMock()

        with (
            patch("bot.services.llm.litellm.responses", mock_responses, create=True),
            patch("bot.services.llm.litellm.acompletion", mock_completion),
            patch("bot.services.llm.litellm.token_counter", return_value=128),
            patch("bot.services.llm.litellm.get_max_tokens", return_value=8192),
        ):
            result = await llm.generate("sys", "hi")

        self.assertEqual(result, "ok")
        mock_responses.assert_called_once()
        self.assertEqual(mock_responses.call_args.kwargs["model"], "gpt-4.1")
        self.assertEqual(mock_responses.call_args.kwargs["input"][0]["role"], "system")
        self.assertEqual(mock_completion.await_count, 0)

    async def test_generate_logs_endpoint_path_in_request_log(self) -> None:
        llm = self._make_llm()
        llm.main.provider = "anthropic"
        llm.main.model = "anthropic/claude-haiku"
        llm.main.endpoint_path = "/v1/messages"
        mock_completion = AsyncMock(return_value=_chat_resp(content="ok"))

        with (
            patch("bot.services.llm.litellm.acompletion", mock_completion),
            patch("bot.services.llm.litellm.token_counter", return_value=128),
            patch("bot.services.llm.litellm.get_max_tokens", return_value=8192),
            patch("bot.services.llm.log.info") as mock_log_info,
        ):
            result = await llm.generate("sys", "hi")

        self.assertEqual(result, "ok")
        request_calls = [
            call
            for call in mock_log_info.call_args_list
            if call.args and isinstance(call.args[0], str) and call.args[0].startswith("LLM request | stage=%s")
        ]
        self.assertTrue(request_calls)
        self.assertEqual(request_calls[0].args[3], "/v1/messages")

    async def test_complete_with_tools_uses_responses_api_for_openai_compatible_when_enabled(self) -> None:
        llm = self._make_llm()
        llm.main.provider = "openai_compatible"
        llm.main.chat_endpoint = "responses"
        llm.main.api_base = "https://gateway.example/v1"
        mock_responses = Mock(
            return_value=_responses_resp(
                tool_calls=[
                    {
                        "id": "call-1",
                        "function": {
                            "name": "websearch",
                            "arguments": '{"query":"weather"}',
                        },
                    }
                ]
            )
        )

        with (
            patch("bot.services.llm.litellm.responses", mock_responses, create=True),
            patch("bot.services.llm.litellm.token_counter", return_value=128),
            patch("bot.services.llm.litellm.get_max_tokens", return_value=8192),
        ):
            resp = await llm.complete_with_tools(
                messages=[{"role": "user", "content": "weather"}],
                tools=[
                    {
                        "type": "function",
                        "function": {
                            "name": "websearch",
                            "parameters": {"type": "object"},
                        },
                    }
                ],
            )

        self.assertIsNotNone(resp)
        self.assertEqual(resp.choices[0].message.tool_calls[0]["function"]["name"], "websearch")
        mock_responses.assert_called_once()
        self.assertEqual(mock_responses.call_args.kwargs["model"], "gpt-4.1")
        self.assertEqual(mock_responses.call_args.kwargs["api_base"], "https://gateway.example/v1")
        self.assertEqual(len(mock_responses.call_args.kwargs["tools"]), 1)

    async def test_generate_supports_streaming_responses_api_requests(self) -> None:
        llm = self._make_llm()
        llm.main.provider = "openai"
        llm.main.chat_endpoint = "responses"
        llm.main.stream = True
        completed = _responses_resp(
            content="hello world",
            usage={"input_tokens": 42, "output_tokens": 2, "total_tokens": 44},
        )
        mock_responses = Mock(
            return_value=_SyncStream(
                [
                    SimpleNamespace(
                        type="response.output_text.delta",
                        delta="hello ",
                    ),
                    SimpleNamespace(
                        type="response.output_text.delta",
                        delta="world",
                    ),
                    SimpleNamespace(
                        type="response.completed",
                        response=completed,
                    ),
                ]
            )
        )

        with (
            patch("bot.services.llm.litellm.responses", mock_responses, create=True),
            patch("bot.services.llm.litellm.token_counter", return_value=128),
            patch("bot.services.llm.litellm.get_max_tokens", return_value=8192),
        ):
            result = await llm.generate("sys", "hi")

        self.assertEqual(result, "hello world")
        mock_responses.assert_called_once()
        self.assertTrue(mock_responses.call_args.kwargs["stream"])

    async def test_vision_responses_requests_honor_streaming_gateways(self) -> None:
        llm = self._make_llm()
        llm.vision_config.provider = "openai"
        llm.vision_config.chat_endpoint = "responses"
        llm.vision_config.stream = True
        completed = _responses_resp(content="a cute cat sticker")
        mock_responses = Mock(
            return_value=_SyncStream(
                [
                    SimpleNamespace(type="response.output_text.delta", delta="a cute "),
                    SimpleNamespace(type="response.output_text.delta", delta="cat sticker"),
                    SimpleNamespace(type="response.completed", response=completed),
                ]
            )
        )

        with (
            patch("bot.services.llm.litellm.responses", mock_responses, create=True),
            patch("bot.services.llm.litellm.token_counter", return_value=128),
            patch("bot.services.llm.litellm.get_max_tokens", return_value=8192),
        ):
            result = await llm.vision_describe("https://example.com/cat.jpg", "describe")

        self.assertEqual(result, "a cute cat sticker")
        mock_responses.assert_called_once()
        self.assertTrue(bool(mock_responses.call_args.kwargs.get("stream")))

    async def test_generate_supports_streaming_responses_refusal_events(self) -> None:
        llm = self._make_llm()
        llm.main.provider = "openai"
        llm.main.chat_endpoint = "responses"
        llm.main.stream = True
        completed = _responses_resp(
            refusal="抱歉，我不能帮助处理这个请求。",
            usage={"input_tokens": 42, "output_tokens": 9, "total_tokens": 51},
        )
        mock_responses = Mock(
            return_value=_SyncStream(
                [
                    SimpleNamespace(
                        type="response.refusal.delta",
                        delta="抱歉，",
                        output_index=0,
                        content_index=0,
                    ),
                    SimpleNamespace(
                        type="response.refusal.delta",
                        delta="我不能帮助处理这个请求。",
                        output_index=0,
                        content_index=0,
                    ),
                    SimpleNamespace(
                        type="response.completed",
                        response=completed,
                    ),
                ]
            )
        )

        with (
            patch("bot.services.llm.litellm.responses", mock_responses, create=True),
            patch("bot.services.llm.litellm.token_counter", return_value=128),
            patch("bot.services.llm.litellm.get_max_tokens", return_value=8192),
        ):
            result = await llm.generate("sys", "hi")

        self.assertEqual(result, "抱歉，我不能帮助处理这个请求。")
        mock_responses.assert_called_once()
        self.assertTrue(mock_responses.call_args.kwargs["stream"])

    async def test_generate_supports_streaming_responses_done_events_without_completed_event(self) -> None:
        llm = self._make_llm()
        llm.main.provider = "openai"
        llm.main.chat_endpoint = "responses"
        llm.main.stream = True
        mock_responses = Mock(
            return_value=_SyncStream(
                [
                    SimpleNamespace(
                        type="response.output_text.done",
                        output_index=0,
                        content_index=0,
                        text="hello world",
                    ),
                    SimpleNamespace(
                        type="response.output_item.done",
                        output_index=0,
                        item=SimpleNamespace(
                            type="message",
                            role="assistant",
                            content=[SimpleNamespace(type="output_text", text="hello world")],
                        ),
                    ),
                ]
            )
        )

        with (
            patch("bot.services.llm.litellm.responses", mock_responses, create=True),
            patch("bot.services.llm.litellm.token_counter", return_value=128),
            patch("bot.services.llm.litellm.get_max_tokens", return_value=8192),
        ):
            result = await llm.generate("sys", "hi")

        self.assertEqual(result, "hello world")
        mock_responses.assert_called_once()
        self.assertTrue(mock_responses.call_args.kwargs["stream"])

    async def test_generate_supports_streaming_responses_enum_event_types_with_empty_completed_output(self) -> None:
        llm = self._make_llm()
        llm.main.provider = "openai"
        llm.main.chat_endpoint = "responses"
        llm.main.stream = True
        completed = SimpleNamespace(
            output=[],
            usage={"input_tokens": 42, "output_tokens": 2, "total_tokens": 44},
        )
        mock_responses = Mock(
            return_value=_SyncStream(
                [
                    SimpleNamespace(
                        type=_EventType.OUTPUT_TEXT_DELTA,
                        delta="hello world",
                        output_index=0,
                        content_index=0,
                    ),
                    SimpleNamespace(
                        type=_EventType.RESPONSE_COMPLETED,
                        response=completed,
                    ),
                ]
            )
        )

        with (
            patch("bot.services.llm.litellm.responses", mock_responses, create=True),
            patch("bot.services.llm.litellm.token_counter", return_value=128),
            patch("bot.services.llm.litellm.get_max_tokens", return_value=8192),
        ):
            result = await llm.generate("sys", "hi")

        self.assertEqual(result, "hello world")
        mock_responses.assert_called_once()
        self.assertTrue(mock_responses.call_args.kwargs["stream"])

    async def test_decision_requests_honor_streaming_gateways(self) -> None:
        llm = self._make_llm()
        llm.decision_config.stream = True
        mock_completion = AsyncMock(
            return_value=_AsyncStream(
                [
                    SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(content="cas"))]),
                    SimpleNamespace(
                        choices=[SimpleNamespace(delta=SimpleNamespace(content="ual"))],
                        usage=SimpleNamespace(prompt_tokens=42, completion_tokens=2),
                    ),
                ]
            )
        )

        with (
            patch("bot.services.llm.litellm.acompletion", mock_completion),
            patch("bot.services.llm.litellm.token_counter", return_value=128),
            patch("bot.services.llm.litellm.get_max_tokens", return_value=8192),
        ):
            result = await llm.decision("sys", "hi")

        self.assertEqual(result, "casual")
        self.assertTrue(bool(mock_completion.await_args.kwargs.get("stream")))

    async def test_generate_normalizes_gemini_proxy_api_base_to_version_root(self) -> None:
        llm = self._make_llm()
        llm.main.model = "gemini/gemini-3.1-flash-lite-preview"
        llm.main.provider = "gemini"
        llm.main.api_base = "https://gateway.example/v1beta/models"
        llm.main.endpoint_path = "/v1beta/models"
        mock_completion = AsyncMock(return_value=_chat_resp(content="ok"))

        with (
            patch("bot.services.llm.litellm.acompletion", mock_completion),
            patch("bot.services.llm.litellm.token_counter", return_value=128),
            patch("bot.services.llm.litellm.get_max_tokens", return_value=8192),
        ):
            result = await llm.generate("sys", "hi")

        self.assertEqual(result, "ok")
        self.assertEqual(
            mock_completion.await_args.kwargs["api_base"],
            "https://gateway.example/v1beta",
        )

    async def test_embed_normalizes_gemini_proxy_api_base_to_version_root(self) -> None:
        llm = self._make_llm()
        llm.embed_config.model = "gemini/text-embedding-004"
        llm.embed_config.provider = "gemini"
        llm.embed_config.api_base = "https://gateway.example/v1beta/models"
        llm.embed_config.endpoint_path = "/v1beta/models"
        mock_embedding = AsyncMock(return_value=_embed_resp([0.1, 0.2, 0.3]))

        with patch("bot.services.llm.litellm.aembedding", mock_embedding):
            vectors = await llm.embed(["hello"])

        self.assertEqual(vectors, [[0.1, 0.2, 0.3]])
        self.assertEqual(
            mock_embedding.await_args.kwargs["api_base"],
            "https://gateway.example/v1beta",
        )

    def test_messages_to_responses_input_converts_tool_round_trip(self) -> None:
        llm = self._make_llm()

        input_items = llm._messages_to_responses_input(
            [
                {"role": "system", "content": "You are helpful."},
                {"role": "user", "content": "weather"},
                {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [
                        {
                            "id": "call-1",
                            "function": {
                                "name": "websearch",
                                "arguments": '{"query":"weather"}',
                            },
                        }
                    ],
                },
                {
                    "role": "tool",
                    "tool_call_id": "call-1",
                    "name": "websearch",
                    "content": '{"ok":true}',
                },
            ]
        )

        self.assertEqual(input_items[0], {"role": "system", "content": "You are helpful."})
        self.assertEqual(input_items[1], {"role": "user", "content": "weather"})
        self.assertEqual(
            input_items[2],
            {
                "type": "function_call",
                "call_id": "call-1",
                "name": "websearch",
                "arguments": '{"query":"weather"}',
            },
        )
        self.assertEqual(
            input_items[3],
            {
                "type": "function_call_output",
                "call_id": "call-1",
                "output": '{"ok":true}',
            },
        )


if __name__ == "__main__":
    unittest.main()
