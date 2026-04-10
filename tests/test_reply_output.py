import unittest
from types import SimpleNamespace

from bot.services.casual import CasualService
from bot.services.reply_output import (
    REPLY_OUTPUT_AWARENESS,
    REPLY_OUTPUT_PROTOCOL,
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

    def test_plain_text_blank_lines_stay_in_single_message_without_json(self) -> None:
        raw = "哈哈哈哈（气鼓鼓）\n\n才不要呢！主人打错字又怎样，我能看懂就好啦\n\n再说了，我会盯着看的呀~"
        parsed = parse_reply_output(raw)

        self.assertEqual(parsed.messages, [raw])
        self.assertFalse(parsed.explicit_no_reply)
        self.assertFalse(parsed.used_json)

    def test_json_multiple_messages_are_preserved_in_order(self) -> None:
        parsed = parse_reply_output(
            '{"messages":["first reply","second reply"],"should_reply":true}'
        )

        self.assertEqual(parsed.messages, ["first reply", "second reply"])
        self.assertFalse(parsed.explicit_no_reply)
        self.assertTrue(parsed.used_json)

    def test_json_message_objects_keep_delivery_metadata(self) -> None:
        parsed = parse_reply_output(
            '{"messages":['
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
            '```json\n{"should_reply":false,"reason":"not_addressed_to_bot"}\n```'
        )

        self.assertEqual(parsed.messages, [])
        self.assertTrue(parsed.explicit_no_reply)
        self.assertEqual(parsed.reason, "not_addressed_to_bot")
        self.assertTrue(parsed.used_json)

    def test_unknown_json_is_treated_as_plain_text(self) -> None:
        parsed = parse_reply_output('{"foo":"bar"}')

        self.assertEqual(parsed.messages, ['{"foo":"bar"}'])
        self.assertFalse(parsed.explicit_no_reply)
        self.assertFalse(parsed.used_json)


class ReplyOutputPromptTests(unittest.TestCase):
    def test_protocol_mentions_compact_single_message_guidance_and_per_message_control(self) -> None:
        self.assertIn("0, 1, or many outgoing messages", REPLY_OUTPUT_PROTOCOL)
        self.assertIn("MUST use message objects", REPLY_OUTPUT_PROTOCOL)
        self.assertIn("avoid blank lines unless they are functionally necessary", REPLY_OUTPUT_PROTOCOL)
        self.assertIn("If you want separate bubbles, use JSON multi-message output.", REPLY_OUTPUT_PROTOCOL)
        self.assertIn("avoid blank lines unless they carry real structure", REPLY_OUTPUT_AWARENESS)
        self.assertIn("Use message objects", REPLY_OUTPUT_AWARENESS)

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
