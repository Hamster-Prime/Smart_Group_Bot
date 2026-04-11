import unittest

from bot.utils.prompts import (
    CASUAL_SYSTEM,
    CHAT_BRIDGE_SYSTEM,
    PERSONA_SYSTEM,
    REPLY_MODE_SYSTEM,
    SKILL_TOOL_SYSTEM,
    STICKER_DECISION_SYSTEM,
)


class RuntimePromptBlockTests(unittest.TestCase):
    def test_reply_mode_prompt_lists_runtime_blocks(self) -> None:
        for block in (
            "[CURRENT_TIME]",
            "[IS_MERGED_MESSAGE]",
            "[MERGED_MESSAGE_COUNT]",
            "[IS_MENTIONED]",
            "[IS_REPLY_TO_BOT]",
            "[IS_REPLY_TO_OTHER]",
            "[MESSAGE_TYPE]",
            "[MERGED_MESSAGE_CONTEXT]",
            "[CURRENT_MESSAGE]",
            "[ASSISTANT_DRAFT_REPLY]",
        ):
            self.assertIn(block, REPLY_MODE_SYSTEM)

    def test_sticker_decision_prompt_lists_runtime_blocks(self) -> None:
        for block in (
            "[REPLY_ACTION]",
            "[MESSAGE_TYPE]",
            "[IS_MENTIONED]",
            "[IS_REPLY_TO_BOT]",
            "[REPLY_SOURCE]",
            "[CURRENT_MESSAGE]",
            "[ASSISTANT_DRAFT_REPLY]",
            "[STICKER_CANDIDATES]",
        ):
            self.assertIn(block, STICKER_DECISION_SYSTEM)

    def test_chat_bridge_prompt_lists_runtime_blocks(self) -> None:
        for block in (
            "[CURRENT_TIME]",
            "[CHAT_BRIDGE_MODE]",
            "[PEER_BOT]",
            "[RECENT_GROUP_CONTEXT]",
            "[CURRENT_TURN_FOCUS]",
            "[CURRENT_BRIDGE_MESSAGE]",
        ):
            self.assertIn(block, CHAT_BRIDGE_SYSTEM)

    def test_core_prompts_discourage_blank_line_bubbles(self) -> None:
        for prompt in (CASUAL_SYSTEM, PERSONA_SYSTEM):
            self.assertIn("不要留空行", prompt)
            self.assertIn("空行就是“分开发送多条消息”的信号", prompt)

    def test_skill_prompt_declares_blank_line_split_rule(self) -> None:
        self.assertIn("纯文本里一旦出现空行，就会被当成分开发送多条消息", SKILL_TOOL_SYSTEM)

    def test_chat_bridge_prompt_discourages_blank_line_bubbles(self) -> None:
        self.assertIn("any blank line will be treated as a separator for multiple outgoing messages", CHAT_BRIDGE_SYSTEM)
        self.assertIn("do not output blank lines for spacing", CHAT_BRIDGE_SYSTEM)
