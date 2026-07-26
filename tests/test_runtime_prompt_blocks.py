import unittest

from bot.utils.prompts import (
    CASUAL_SYSTEM,
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

    def test_core_prompts_discourage_blank_line_bubbles(self) -> None:
        for prompt in (CASUAL_SYSTEM, PERSONA_SYSTEM):
            self.assertIn("do not leave blank lines", prompt)
            self.assertIn(
                'a line containing only `[[SPLIT]]` is the signal for "send as separate messages"',
                prompt,
            )
            self.assertIn("a blank line never splits anything", prompt)

    def test_core_prompts_forbid_parenthetical_stage_directions(self) -> None:
        for prompt in (CASUAL_SYSTEM, PERSONA_SYSTEM):
            self.assertIn("Do not write bracketed action descriptions or stage directions", prompt)
            self.assertIn("(swings feet)", prompt)

    def test_skill_prompt_declares_split_marker_rule(self) -> None:
        self.assertIn(
            'a line containing only `[[SPLIT]]` is treated as "send as separate messages"',
            SKILL_TOOL_SYSTEM,
        )
        self.assertIn("a blank line never splits anything", SKILL_TOOL_SYSTEM)
