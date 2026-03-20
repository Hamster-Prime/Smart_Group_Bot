import unittest

from bot.utils.security import clean_multiline_text, sanitize_history_for_llm


class SecurityUtilsTests(unittest.TestCase):
    def test_clean_multiline_text_preserves_structure(self) -> None:
        source = "### Today\n1. One\n2. Two\n\n#### Tech\nMore"

        cleaned = clean_multiline_text(source, max_len=400)

        self.assertIn("### Today\n1. One\n2. Two", cleaned)
        self.assertIn("\n\n#### Tech\n", cleaned)

    def test_sanitize_history_formats_structured_metadata(self) -> None:
        history = [
            {
                "role": "user",
                "content": "[id:42 username:@tester is_owner:no is_tg_admin:no trusted_source:none name:Alice] hello there",
                "created_at": "2026-03-20 12:34:56",
                "sender_id": 42,
                "sender_name": "Alice",
                "message_type": "text",
            },
            {
                "role": "assistant",
                "content": "hi",
                "created_at": "2026-03-20 12:34:57",
                "sender_name": "bot",
                "message_type": "assistant_reply",
            },
        ]

        messages = sanitize_history_for_llm(history, max_items=2, max_item_chars=200)

        self.assertEqual(len(messages), 2)
        self.assertIn("[HISTORY_MESSAGE]", messages[0]["content"])
        self.assertIn("sent_at: 2026-03-20 12:34:56", messages[0]["content"])
        self.assertIn("sender: Alice", messages[0]["content"])
        self.assertIn("sender_id: 42", messages[0]["content"])
        self.assertIn("content:\nhello there", messages[0]["content"])
        self.assertIn("message_type: assistant_reply", messages[1]["content"])
        self.assertIn("sender: bot", messages[1]["content"])


if __name__ == "__main__":
    unittest.main()
