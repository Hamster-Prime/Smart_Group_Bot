import unittest
from types import SimpleNamespace

from bot.services.casual import CasualService
from bot.services.reply_output import (
    REPLY_OUTPUT_AWARENESS,
    REPLY_OUTPUT_PROTOCOL,
    REPLY_OUTPUT_SCHEMA,
    parse_reply_output,
)
from bot.services.skills.service import SkillService


def _llm_stub() -> SimpleNamespace:
    return SimpleNamespace(
        main=SimpleNamespace(model="main-model", fallbacks=[]),
        decision_config=SimpleNamespace(model="decision-model", fallbacks=[]),
        vision_config=SimpleNamespace(model="vision-model", fallbacks=[]),
        moderation_config=SimpleNamespace(model="moderation-model", fallbacks=[]),
        compress_config=SimpleNamespace(model="compress-model", fallbacks=[]),
        embed_config=SimpleNamespace(model="embed-model", fallbacks=[]),
    )


class ReplyOutputParserTests(unittest.TestCase):
    def test_plain_text_stays_single_message(self) -> None:
        parsed = parse_reply_output("just one reply")

        self.assertEqual(parsed.messages, ["just one reply"])
        self.assertEqual(len(parsed.message_specs), 1)
        self.assertEqual(parsed.message_specs[0].delivery_mode, "auto")
        self.assertEqual(parsed.message_specs[0].reply_to, "auto")
        self.assertFalse(parsed.explicit_no_reply)
        self.assertFalse(parsed.used_json)

    def test_plain_text_single_newlines_stay_in_single_message(self) -> None:
        raw = "第一行\n第二行\n第三行"
        parsed = parse_reply_output(raw)

        self.assertEqual(parsed.messages, [raw])
        self.assertFalse(parsed.explicit_no_reply)
        self.assertFalse(parsed.used_json)

    def test_plain_text_blank_lines_stay_in_one_message(self) -> None:
        raw = "哈哈哈哈（气鼓鼓）\n\n才不要呢！主人打错字又怎样，我能看懂就好啦\n\n再说了，我会盯着看的呀~"
        parsed = parse_reply_output(raw)

        self.assertEqual(parsed.messages, [raw])
        self.assertTrue(all(spec.delivery_mode == "auto" for spec in parsed.message_specs))
        self.assertTrue(all(spec.reply_to == "auto" for spec in parsed.message_specs))
        self.assertFalse(parsed.explicit_no_reply)
        self.assertFalse(parsed.used_json)

    def test_fenced_code_keeps_blank_lines_and_indentation(self) -> None:
        raw = (
            "配置如下：\n\n"
            "```yaml\n"
            "dns:\n"
            "  default-nameserver:\n"
            "    - 223.5.5.5\n\n"
            "  fallback:\n"
            "    - https://example.com/dns-query\n"
            "```\n\n"
            "这是补充说明。"
        )

        parsed = parse_reply_output(raw)

        self.assertEqual(parsed.messages, [raw])
        self.assertFalse(parsed.used_json)

    def test_plain_text_is_not_truncated_at_1200_characters(self) -> None:
        raw = "```text\n" + ("a" * 2400) + "\n```"

        parsed = parse_reply_output(raw)

        self.assertEqual(parsed.messages, [raw])

    def test_json_multiple_messages_are_preserved_in_order(self) -> None:
        parsed = parse_reply_output(
            f'{{"schema":"{REPLY_OUTPUT_SCHEMA}","messages":["first reply","second reply"],"should_reply":true}}'
        )

        self.assertEqual(parsed.messages, ["first reply", "second reply"])
        self.assertFalse(parsed.explicit_no_reply)
        self.assertTrue(parsed.used_json)

    def test_json_message_objects_keep_delivery_metadata(self) -> None:
        parsed = parse_reply_output(
            f'{{"schema":"{REPLY_OUTPUT_SCHEMA}","messages":['
            '{"text":"first reply","delivery_mode":"message"},'
            '{"text":"second reply","delivery_mode":"reply","reply_to":"input_1"}'
            ']}'
        )

        self.assertEqual(parsed.messages, ["first reply", "second reply"])
        self.assertEqual(parsed.message_specs[0].delivery_mode, "message")
        self.assertEqual(parsed.message_specs[0].reply_to, "auto")
        self.assertEqual(parsed.message_specs[1].delivery_mode, "reply")
        self.assertEqual(parsed.message_specs[1].reply_to, "input_1")

    def test_json_can_explicitly_skip_reply(self) -> None:
        parsed = parse_reply_output(
            f'{{"schema":"{REPLY_OUTPUT_SCHEMA}","should_reply":false,"reason":"not_addressed_to_bot"}}'
        )

        self.assertEqual(parsed.messages, [])
        self.assertTrue(parsed.explicit_no_reply)
        self.assertEqual(parsed.reason, "not_addressed_to_bot")
        self.assertTrue(parsed.used_json)

    def test_unversioned_multiple_messages_json_is_visible_text(self) -> None:
        raw = '{"messages":["first reply","second reply"]}'
        parsed = parse_reply_output(raw)

        self.assertEqual(parsed.messages, [raw])
        self.assertFalse(parsed.used_json)

    def test_unversioned_silence_json_is_visible_text(self) -> None:
        raw = '{"should_reply":false,"reason":"not_addressed_to_bot"}'
        parsed = parse_reply_output(raw)

        self.assertEqual(parsed.messages, [raw])
        self.assertFalse(parsed.explicit_no_reply)
        self.assertFalse(parsed.used_json)

    def test_bare_message_json_example_is_visible_text(self) -> None:
        raw = '{"message":"hello"}'

        parsed = parse_reply_output(raw)

        self.assertEqual(parsed.messages, [raw])
        self.assertFalse(parsed.used_json)

    def test_unrelated_action_json_is_visible_text(self) -> None:
        raw = '{"action":"allow"}'

        parsed = parse_reply_output(raw)

        self.assertEqual(parsed.messages, [raw])
        self.assertFalse(parsed.used_json)

    def test_fenced_json_is_visible_markdown_not_protocol(self) -> None:
        raw = '```json\n{"should_reply":false,"reason":"example"}\n```'

        parsed = parse_reply_output(raw)

        self.assertEqual(parsed.messages, [raw])
        self.assertFalse(parsed.explicit_no_reply)
        self.assertFalse(parsed.used_json)

    def test_embedded_json_is_not_extracted_from_markdown(self) -> None:
        raw = '示例：\n\n{"messages":["one","two"]}\n\n以上只是示例。'

        parsed = parse_reply_output(raw)

        self.assertEqual(parsed.messages, [raw])
        self.assertFalse(parsed.used_json)

    def test_unknown_json_is_treated_as_plain_text(self) -> None:
        parsed = parse_reply_output('{"foo":"bar"}')

        self.assertEqual(parsed.messages, ['{"foo":"bar"}'])
        self.assertFalse(parsed.explicit_no_reply)
        self.assertFalse(parsed.used_json)


class ReplyOutputPromptTests(unittest.TestCase):
    def test_protocol_mentions_compact_single_message_guidance_and_per_message_control(self) -> None:
        self.assertIn("0, 1, or many outgoing messages", REPLY_OUTPUT_PROTOCOL)
        self.assertIn(REPLY_OUTPUT_SCHEMA, REPLY_OUTPUT_PROTOCOL)
        self.assertIn("Blank lines", REPLY_OUTPUT_PROTOCOL)
        self.assertIn("remain inside that one message", REPLY_OUTPUT_PROTOCOL)
        self.assertIn("must not be wrapped in a Markdown code fence", REPLY_OUTPUT_PROTOCOL)
        self.assertIn("Blank lines never create additional outgoing messages", REPLY_OUTPUT_AWARENESS)
        self.assertIn("strict schema-tagged JSON protocol", REPLY_OUTPUT_AWARENESS)

    def test_casual_prompt_includes_reply_output_protocol(self) -> None:
        payload = CasualService(_llm_stub()).build_prompt_payload("test")

        self.assertIn(REPLY_OUTPUT_PROTOCOL, [item["content"] for item in payload["messages"]])
        self.assertIn(REPLY_OUTPUT_AWARENESS, [item["content"] for item in payload["messages"]])

    def test_skill_prompt_includes_reply_output_protocol(self) -> None:
        payload = SkillService(_llm_stub()).build_answer_prompt_payload("test")

        self.assertIn(REPLY_OUTPUT_PROTOCOL, [item["content"] for item in payload["messages"]])
        self.assertIn(REPLY_OUTPUT_AWARENESS, [item["content"] for item in payload["messages"]])


if __name__ == "__main__":
    unittest.main()
