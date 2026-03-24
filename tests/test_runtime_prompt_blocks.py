import unittest

from bot.utils.prompts import REPLY_MODE_SYSTEM, STICKER_DECISION_SYSTEM


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
