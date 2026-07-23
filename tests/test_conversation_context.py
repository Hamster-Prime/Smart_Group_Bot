import unittest

from bot.utils.conversation_context import (
    build_current_turn_focus_context,
    format_recent_group_context,
)


class ConversationContextTests(unittest.TestCase):
    def test_recent_group_context_uses_latest_non_system_messages_with_metadata(self) -> None:
        history = [
            {"role": "system", "content": "[context-summary]\nprevious topic"},
            {
                "role": "user",
                "content": "[id:11 username:@alice is_owner:no is_tg_admin:no trusted_source:none name:Alice] first question",
                "created_at": "2026-03-20 10:00:00",
                "sender_id": 11,
                "sender_name": "Alice",
                "message_type": "text",
            },
            {
                "role": "assistant",
                "content": "follow-up question",
                "created_at": "2026-03-20 10:00:02",
                "sender_name": "bot",
                "message_type": "assistant_reply",
            },
            {
                "role": "user",
                "content": "[id:11 username:@alice is_owner:no is_tg_admin:no trusted_source:none name:Alice] latest detail",
                "created_at": "2026-03-20 10:00:05",
                "sender_id": 11,
                "sender_name": "Alice",
                "message_type": "text",
            },
        ]

        context = format_recent_group_context(history, max_items=2, max_item_chars=120)

        self.assertIn("[RECENT_GROUP_CONTEXT]", context)
        self.assertIn("sent_at=2026-03-20 10:00:02", context)
        self.assertIn("sender=bot", context)
        self.assertIn("sender=Alice", context)
        self.assertIn("sender_id=11", context)
        self.assertIn("latest detail", context)
        self.assertNotIn("first question", context)
        self.assertNotIn("previous topic", context)

    def test_recent_group_context_marks_owner_line(self) -> None:
        history = [
            {
                "role": "user",
                "content": (
                    "[id:7 username:@root is_owner:yes is_tg_admin:no trusted_source:none "
                    "name:Root] 在吗"
                ),
                "created_at": "2026-03-20 10:00:00",
                "sender_id": 7,
                "sender_name": "Root",
                "message_type": "text",
            }
        ]

        context = format_recent_group_context(history, max_items=1, max_item_chars=120)

        self.assertIn("sender_role=owner", context)

    def test_current_turn_focus_keeps_merged_structure(self) -> None:
        merged_context = (
            "count=2\n"
            "messages sent by the same user in the debounce window:\n"
            "[1] type=text\n"
            "best ios player?\n"
            "[2] type=text\n"
            "vlc?"
        )

        context = build_current_turn_focus_context(
            "best ios player?\nvlc?",
            merged_count=2,
            merged_context=merged_context,
        )

        self.assertIn("[CURRENT_TURN_MESSAGE_COUNT]\n2", context)
        self.assertIn("[CURRENT_TURN_MESSAGES]", context)
        self.assertIn("best ios player?", context)
        self.assertIn("vlc?", context)
        self.assertIn("Do not expand the topic into a different product category", context)


if __name__ == "__main__":
    unittest.main()
