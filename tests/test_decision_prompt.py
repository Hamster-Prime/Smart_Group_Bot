import unittest

from bot.utils.prompts import DECISION_SYSTEM


class DecisionPromptTests(unittest.TestCase):
    def test_decision_prompt_uses_actual_runtime_block_names(self) -> None:
        for block in (
            "[IS_MENTIONED]",
            "[IS_REPLY_TO_BOT]",
            "[IS_REPLY_TO_OTHER]",
            "[MENTIONS_OTHER_USER]",
            "[SENDER_IS_OWNER]",
            "[IS_MERGED_MESSAGE]",
            "[RECENT_HISTORY_FOR_DECISION]",
            "[CURRENT_MESSAGE]",
        ):
            self.assertIn(block, DECISION_SYSTEM)

    def test_decision_prompt_is_conservative_by_default(self) -> None:
        self.assertIn("默认输出 `skip`", DECISION_SYSTEM)
        self.assertIn("不要因为你“能回答”就回答", DECISION_SYSTEM)
        self.assertIn("不要把主人的每句闲聊都接住", DECISION_SYSTEM)
