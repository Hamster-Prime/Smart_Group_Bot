import asyncio
import unittest
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
        mock_responses = Mock(return_value=_chat_resp(content="ok"))
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

    async def test_complete_with_tools_uses_responses_api_for_openai_compatible_when_enabled(self) -> None:
        llm = self._make_llm()
        llm.main.provider = "openai_compatible"
        llm.main.chat_endpoint = "responses"
        llm.main.api_base = "https://gateway.example/v1"
        mock_responses = Mock(
            return_value=_chat_resp(
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
        mock_responses = Mock(
            return_value=_SyncStream(
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
            patch("bot.services.llm.litellm.responses", mock_responses, create=True),
            patch("bot.services.llm.litellm.token_counter", return_value=128),
            patch("bot.services.llm.litellm.get_max_tokens", return_value=8192),
        ):
            result = await llm.generate("sys", "hi")

        self.assertEqual(result, "hello world")
        mock_responses.assert_called_once()
        self.assertTrue(mock_responses.call_args.kwargs["stream"])

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
