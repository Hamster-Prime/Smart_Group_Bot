import unittest

from bot.services.reply_mode import ReplyModeService
from bot.utils.prompts import REPLY_MODE_SYSTEM


class _CaptureDecisionLLM:
    def __init__(self, response: str) -> None:
        self.response = response
        self.calls = 0
        self.system = ""
        self.prompt = ""

    async def decision(self, system: str, prompt: str) -> str:
        self.calls += 1
        self.system = system
        self.prompt = prompt
        return self.response


class ReplyModeBatchTests(unittest.IsolatedAsyncioTestCase):
    async def test_decide_many_uses_single_llm_request_for_multiple_replies(self) -> None:
        llm = _CaptureDecisionLLM('{"modes":["reply","message","reply"]}')
        service = ReplyModeService(llm)

        result = await service.decide_many(
            user_text="check these drafts",
            assistant_replies=["first draft", "second draft", "third draft"],
            msg_type="text",
            is_mentioned=True,
            is_reply_to_bot=False,
            is_reply_to_other=False,
            merged_count=3,
            merged_context="input 1\ninput 2\ninput 3",
        )

        self.assertEqual(result, ["reply", "message", "reply"])
        self.assertEqual(llm.calls, 1)
        self.assertIn("[ASSISTANT_DRAFT_REPLIES]", llm.prompt)
        self.assertIn("[REPLY_1]", llm.prompt)
        self.assertIn("[REPLY_2]", llm.prompt)
        self.assertIn("[REPLY_3]", llm.prompt)

    async def test_decide_accepts_reasoning_prefixed_single_reply_output(self) -> None:
        llm = _CaptureDecisionLLM("reply\n\nReasoning: direct thread continuation")
        service = ReplyModeService(llm)

        result = await service.decide(
            user_text="what do you think",
            assistant_reply="continuing the thread",
            msg_type="text",
            is_mentioned=True,
            is_reply_to_bot=False,
            is_reply_to_other=False,
        )

        self.assertEqual(result, "reply")
        self.assertEqual(llm.calls, 1)

    def test_reply_mode_prompt_mentions_multi_reply_block(self) -> None:
        self.assertIn("[ASSISTANT_DRAFT_REPLIES]", REPLY_MODE_SYSTEM)
        self.assertIn('{"modes":["reply","message"]}', REPLY_MODE_SYSTEM)


if __name__ == "__main__":
    unittest.main()
