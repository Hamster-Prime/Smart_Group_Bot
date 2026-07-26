import unittest

from bot.utils.bot_identity import (
    build_bot_identity_context,
    get_bot_identity,
    reset_bot_identity,
    set_bot_identity,
)


class BotIdentityTests(unittest.TestCase):
    def tearDown(self) -> None:
        reset_bot_identity()

    def test_unset_identity_renders_empty(self) -> None:
        reset_bot_identity()
        self.assertEqual(build_bot_identity_context(), "")

    def test_set_identity_renders_block(self) -> None:
        set_bot_identity(user_id=12345, username="@NewNameBot", display_name="新名字")

        context = build_bot_identity_context()
        self.assertTrue(context.startswith("[BOT_IDENTITY]\n"))
        self.assertIn("bot_username: @NewNameBot".replace("@New", "@New"), context)
        self.assertIn("bot_username: @NewNameBot", context)
        self.assertIn("bot_display_name: 新名字", context)
        self.assertIn("bot_user_id: 12345", context)
        # leading @ is normalized away in storage
        self.assertEqual(get_bot_identity().username, "NewNameBot")

    def test_with_persona_injects_identity_block(self) -> None:
        from bot.utils.prompts import with_persona

        set_bot_identity(user_id=1, username="freshbot", display_name="小鲜")
        combined = with_persona("do the task")

        # anchor on rendered block content: persona prose also mentions the bare tokens
        self.assertIn("[BOT_IDENTITY]\nauthoritative: yes", combined)
        self.assertIn("bot_username: @freshbot", combined)
        self.assertIn("[TASK_PROMPT]\ndo the task", combined)
        # identity comes after persona but before task prompt
        self.assertLess(
            combined.index("[BOT_IDENTITY]\nauthoritative: yes"),
            combined.index("[TASK_PROMPT]\ndo the task"),
        )

    def test_with_persona_without_runtime_identity_keeps_project_info(self) -> None:
        from bot.utils.prompts import with_persona

        reset_bot_identity()
        combined = with_persona("do the task")

        self.assertNotIn("[BOT_IDENTITY]\nauthoritative: yes", combined)
        self.assertNotIn("bot_username:", combined)
        self.assertIn("[BOT_PROJECT_INFO]\nauthoritative: yes", combined)
        self.assertIn("[TASK_PROMPT]\ndo the task", combined)

    def test_persona_and_decision_prompts_have_no_hardcoded_bot_name(self) -> None:
        from bot.utils.prompts import DECISION_SYSTEM, PERSONA_SYSTEM

        for prompt_text in (PERSONA_SYSTEM, DECISION_SYSTEM):
            self.assertNotIn("gansini", prompt_text.lower())
            self.assertNotIn("感思你", prompt_text)

    def test_decision_context_includes_identity_when_set(self) -> None:
        import asyncio
        from types import SimpleNamespace
        from unittest.mock import AsyncMock

        from bot.services.decision import DecisionService

        set_bot_identity(user_id=9, username="freshbot", display_name="小鲜")
        llm = SimpleNamespace(decision=AsyncMock(return_value="skip"))
        service = DecisionService(llm, context_items=0)

        asyncio.run(service.decide("随便聊聊"))

        prompt_context = llm.decision.await_args.args[1]
        self.assertIn("[BOT_IDENTITY]", prompt_context)
        self.assertIn("@freshbot", prompt_context)


if __name__ == "__main__":
    unittest.main()
