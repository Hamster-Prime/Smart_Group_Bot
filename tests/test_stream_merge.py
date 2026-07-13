import unittest
from types import SimpleNamespace

from bot.services.llm import LLMService


class _Chunk(SimpleNamespace):
    pass


def _delta_chunk(text: str) -> SimpleNamespace:
    return SimpleNamespace(
        type="",
        usage=None,
        choices=[SimpleNamespace(delta=SimpleNamespace(content=text, tool_calls=None))],
    )


class StreamDeltaMergeTests(unittest.TestCase):
    def test_delta_suffix_repeat_is_not_swallowed(self) -> None:
        # "2000" streamed as "200" + "0": old endswith heuristic dropped the last 0.
        merged = LLMService._append_stream_piece("价格是200", "0")
        self.assertEqual(merged, "价格是2000")

    def test_delta_prefix_repeat_is_not_swallowed(self) -> None:
        merged = LLMService._append_stream_piece("aa", "aab")
        self.assertEqual(merged, "aaaab")

    def test_url_repeated_fragments_survive(self) -> None:
        # URL-encoded links legitimately repeat fragments like %E7%83.
        merged = LLMService._append_stream_piece(
            "https://s.weibo.com/weibo?q=%E7%83", "%E7%83"
        )
        self.assertEqual(merged, "https://s.weibo.com/weibo?q=%E7%83%E7%83")

    def test_empty_pieces_are_noops(self) -> None:
        self.assertEqual(LLMService._append_stream_piece("abc", ""), "abc")
        self.assertEqual(LLMService._append_stream_piece("", "abc"), "abc")


class ChatCompletionsStreamTests(unittest.IsolatedAsyncioTestCase):
    async def _consume(self, chunks: list) -> object:
        service = LLMService.__new__(LLMService)

        async def _gen():
            for chunk in chunks:
                yield chunk

        return await LLMService._consume_chat_stream(service, _gen())

    async def test_number_split_across_chunks(self) -> None:
        resp = await self._consume([_delta_chunk("总共是2万"), _delta_chunk("200"), _delta_chunk("0"), _delta_chunk("元")])
        self.assertEqual(resp.choices[0].message.content, "总共是2万2000元")

    async def test_link_split_across_chunks(self) -> None:
        parts = ["https://example.com/", "aa", "a", "/bb", "b"]
        resp = await self._consume([_delta_chunk(p) for p in parts])
        self.assertEqual(resp.choices[0].message.content, "https://example.com/aaa/bbb")


def _responses_event(event_type: str, **kwargs) -> SimpleNamespace:
    return SimpleNamespace(type=event_type, usage=None, choices=[], **kwargs)


class ResponsesApiStreamTests(unittest.IsolatedAsyncioTestCase):
    async def _consume(self, chunks: list) -> object:
        service = LLMService.__new__(LLMService)

        async def _gen():
            for chunk in chunks:
                yield chunk

        return await LLMService._consume_chat_stream(service, _gen())

    async def test_output_text_done_replaces_deltas_without_duplication(self) -> None:
        resp = await self._consume(
            [
                _responses_event(
                    "response.output_text.delta", output_index=0, content_index=0, delta="20"
                ),
                _responses_event(
                    "response.output_text.delta", output_index=0, content_index=0, delta="00"
                ),
                _responses_event(
                    "response.output_text.done", output_index=0, content_index=0, text="2000"
                ),
            ]
        )
        self.assertEqual(resp.choices[0].message.content, "2000")

    async def test_output_text_done_without_deltas_sets_text(self) -> None:
        resp = await self._consume(
            [
                _responses_event(
                    "response.output_text.done", output_index=0, content_index=0, text="hello 2000"
                ),
            ]
        )
        self.assertEqual(resp.choices[0].message.content, "hello 2000")

    async def test_delta_suffix_repeat_survives_in_responses_stream(self) -> None:
        resp = await self._consume(
            [
                _responses_event(
                    "response.output_text.delta", output_index=0, content_index=0, delta="200"
                ),
                _responses_event(
                    "response.output_text.delta", output_index=0, content_index=0, delta="0"
                ),
            ]
        )
        self.assertEqual(resp.choices[0].message.content, "2000")

    async def test_tool_arguments_done_replaces_delta_fragments(self) -> None:
        resp = await self._consume(
            [
                _responses_event(
                    "response.output_item.added",
                    output_index=0,
                    item=SimpleNamespace(type="function_call", call_id="c1", name="websearch"),
                ),
                _responses_event(
                    "response.function_call_arguments.delta", output_index=0, delta='{"query": "20'
                ),
                _responses_event(
                    "response.function_call_arguments.delta", output_index=0, delta='0'
                ),
                _responses_event(
                    "response.function_call_arguments.delta", output_index=0, delta='0"}'
                ),
                _responses_event(
                    "response.function_call_arguments.done",
                    output_index=0,
                    arguments='{"query": "2000"}',
                ),
            ]
        )
        tool_calls = resp.choices[0].message.tool_calls
        self.assertEqual(len(tool_calls), 1)
        self.assertEqual(tool_calls[0]["function"]["arguments"], '{"query": "2000"}')
        self.assertEqual(tool_calls[0]["function"]["name"], "websearch")

    async def test_tool_name_not_duplicated_by_added_then_done(self) -> None:
        resp = await self._consume(
            [
                _responses_event(
                    "response.output_item.added",
                    output_index=0,
                    item=SimpleNamespace(type="function_call", call_id="c1", name="websearch"),
                ),
                _responses_event(
                    "response.output_item.done",
                    output_index=0,
                    item=SimpleNamespace(
                        type="function_call",
                        call_id="c1",
                        name="websearch",
                        arguments='{"query": "x"}',
                    ),
                ),
            ]
        )
        tool_calls = resp.choices[0].message.tool_calls
        self.assertEqual(tool_calls[0]["function"]["name"], "websearch")
        self.assertEqual(tool_calls[0]["function"]["arguments"], '{"query": "x"}')

    async def test_tool_arguments_delta_fragments_without_done(self) -> None:
        resp = await self._consume(
            [
                _responses_event(
                    "response.output_item.added",
                    output_index=0,
                    item=SimpleNamespace(type="function_call", call_id="c1", name="webfetch"),
                ),
                _responses_event(
                    "response.function_call_arguments.delta", output_index=0, delta='{"url": "https://e.com/aa'
                ),
                _responses_event(
                    "response.function_call_arguments.delta", output_index=0, delta='a"}'
                ),
            ]
        )
        tool_calls = resp.choices[0].message.tool_calls
        self.assertEqual(tool_calls[0]["function"]["arguments"], '{"url": "https://e.com/aaa"}')

    async def test_output_item_done_message_replaces_slot(self) -> None:
        resp = await self._consume(
            [
                _responses_event(
                    "response.output_text.delta", output_index=0, content_index=0, delta="20"
                ),
                _responses_event(
                    "response.output_item.done",
                    output_index=0,
                    item=SimpleNamespace(
                        type="message",
                        content=[SimpleNamespace(type="output_text", text="2000元")],
                    ),
                ),
            ]
        )
        self.assertEqual(resp.choices[0].message.content, "2000元")


class SyncStreamConsumerTests(unittest.TestCase):
    def test_sync_consumer_matches_async_semantics(self) -> None:
        service = LLMService.__new__(LLMService)
        chunks = [_delta_chunk("总共200"), _delta_chunk("0"), _delta_chunk("元")]
        resp = LLMService._consume_chat_stream_sync(service, iter(chunks))
        self.assertEqual(resp.choices[0].message.content, "总共2000元")

    def test_sync_done_event_replaces(self) -> None:
        service = LLMService.__new__(LLMService)
        chunks = [
            _responses_event(
                "response.output_text.delta", output_index=0, content_index=0, delta="20"
            ),
            _responses_event(
                "response.output_text.done", output_index=0, content_index=0, text="2000"
            ),
        ]
        resp = LLMService._consume_chat_stream_sync(service, iter(chunks))
        self.assertEqual(resp.choices[0].message.content, "2000")


if __name__ == "__main__":
    unittest.main()
