import unittest

from bot.utils.conversation_context import (
    build_current_turn_focus_context,
    format_recent_group_context,
)


class ConversationContextTests(unittest.TestCase):
    def test_recent_group_context_uses_latest_non_system_messages(self) -> None:
        history = [
            {"role": "system", "content": "[context-summary]\n之前在聊播放器"},
            {"role": "user", "content": "A：iOS 上看电影用啥"},
            {"role": "assistant", "content": "B：你是想找播放器还是网盘"},
            {"role": "user", "content": "C：主要是播放器，想要稳定点"},
        ]

        context = format_recent_group_context(history, max_items=2, max_item_chars=120)

        self.assertIn("[assistant] B：你是想找播放器还是网盘", context)
        self.assertIn("[user] C：主要是播放器，想要稳定点", context)
        self.assertNotIn("之前在聊播放器", context)
        self.assertNotIn("[user] A：iOS 上看电影用啥", context)

    def test_current_turn_focus_keeps_merged_structure(self) -> None:
        merged_context = (
            "count=2\n"
            "以下是同一用户在当前抖动窗口内连续发送的消息，按时间顺序排列：\n"
            "[1] type=text\n"
            "ios上最好用的是啥啊\n"
            "[2] type=text\n"
            "sen吗"
        )

        context = build_current_turn_focus_context(
            "ios上最好用的是啥啊\nsen吗",
            merged_count=2,
            merged_context=merged_context,
        )

        self.assertIn("[CURRENT_TURN_MESSAGE_COUNT]\n2", context)
        self.assertIn("[CURRENT_TURN_MESSAGES]", context)
        self.assertIn("ios上最好用的是啥啊", context)
        self.assertIn("sen吗", context)
        self.assertIn("不要把“最好用的是啥”这类问法自动扩展成别的品类", context)


if __name__ == "__main__":
    unittest.main()
