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
            "[MERGED_MESSAGE_CONTEXT]",
            "[CURRENT_MESSAGE]",
        ):
            self.assertIn(block, DECISION_SYSTEM)

    def test_decision_prompt_is_active_but_rate_limited(self) -> None:
        self.assertIn("warm but restrained group member", DECISION_SYSTEM)
        self.assertIn("Prefer replying less often over jumping into every topic", DECISION_SYSTEM)
        self.assertIn("raise the bar for another reply", DECISION_SYSTEM)
        self.assertIn("Prefer quality over frequency.", DECISION_SYSTEM)

    def test_decision_prompt_mentions_bot_frequency_signals(self) -> None:
        self.assertIn("role=assistant", DECISION_SYSTEM)
        self.assertIn("sender_id=BOT", DECISION_SYSTEM)
        self.assertIn("replied recently or multiple times already", DECISION_SYSTEM)
        self.assertIn("the bot has already spoken recently", DECISION_SYSTEM)

    def test_decision_prompt_defaults_to_skip_when_not_needed(self) -> None:
        self.assertIn("When unsure, default to `skip`.", DECISION_SYSTEM)
        self.assertIn("stay quiet when a reply would be redundant, intrusive, or too frequent", DECISION_SYSTEM)
