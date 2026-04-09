import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from bot.services.reply_mode import ReplyModeService
from bot.services.sticker_decision import StickerDecisionService, _RATIO_BASELINE
from bot.services.sticker_library import sticker_library


class _CaptureDecisionLLM:
    def __init__(self, response: str = "reply") -> None:
        self.response = response
        self.system = ""
        self.prompt = ""

    async def decision(self, system: str, prompt: str) -> str:
        self.system = system
        self.prompt = prompt
        return self.response


class _CaptureGenerateLLM:
    def __init__(self, response: str) -> None:
        self.response = response
        self.system = ""
        self.prompt = ""

    async def generate(self, system: str, prompt: str) -> str:
        self.system = system
        self.prompt = prompt
        return self.response


class RuntimePromptContextTests(unittest.IsolatedAsyncioTestCase):
    async def test_reply_mode_service_uses_english_runtime_blocks(self) -> None:
        llm = _CaptureDecisionLLM()
        service = ReplyModeService(llm)

        result = await service.decide(
            user_text="这是当前消息",
            assistant_reply="这是拟回复",
            msg_type="text",
            is_mentioned=True,
            is_reply_to_bot=False,
            is_reply_to_other=True,
            merged_count=2,
            merged_context="第一条\n第二条",
        )

        self.assertEqual(result, "reply")
        self.assertIn("[IS_MERGED_MESSAGE]\nyes", llm.prompt)
        self.assertIn("[IS_MENTIONED]\nyes", llm.prompt)
        self.assertIn("[IS_REPLY_TO_BOT]\nno", llm.prompt)
        self.assertIn("[IS_REPLY_TO_OTHER]\nyes", llm.prompt)
        self.assertIn("[MERGED_MESSAGE_CONTEXT]", llm.prompt)
        self.assertIn("[CURRENT_MESSAGE]", llm.prompt)
        self.assertIn("[ASSISTANT_DRAFT_REPLY]", llm.prompt)
        self.assertNotIn("[是否@机器人]", llm.prompt)
        self.assertNotIn("[是否回复机器人]", llm.prompt)

    async def test_reply_mode_service_can_choose_message_for_merged_turn(self) -> None:
        llm = _CaptureDecisionLLM(response="message")
        service = ReplyModeService(llm)

        result = await service.decide(
            user_text="不要用回复格式 直接发三条",
            assistant_reply="第一条",
            msg_type="text",
            is_mentioned=True,
            is_reply_to_bot=False,
            is_reply_to_other=False,
            merged_count=3,
            merged_context="第一句\n第二句\n第三句",
        )

        self.assertEqual(result, "message")

    async def test_sticker_decision_service_uses_english_runtime_blocks(self) -> None:
        llm = _CaptureGenerateLLM(
            '{"send": false, "sticker_file_id": "", "query": "", "reason": "skip"}'
        )
        service = StickerDecisionService(llm)
        group_id = 987654
        _RATIO_BASELINE[group_id] = (0, 0)
        candidates = [
            {
                "file_id": "file_1",
                "emoji": "🙂",
                "set_name": "test",
                "description": "开心",
                "seen_count": 1,
                "sent_count": 0,
                "source": "learned",
            }
        ]

        try:
            with (
                patch.object(sticker_library, "total_sent_count", new=AsyncMock(return_value=0)),
                patch.object(sticker_library, "list_candidates", new=AsyncMock(return_value=candidates)),
            ):
                result = await service.decide(
                    session=SimpleNamespace(),
                    group_id=group_id,
                    action="casual",
                    msg_type="text",
                    is_mentioned=True,
                    is_reply_to_bot=False,
                    user_text="来点气氛",
                    assistant_reply="这条我接一下",
                    reply_source="casual",
                    assistant_reply_count=5,
                )
        finally:
            _RATIO_BASELINE.pop(group_id, None)

        self.assertFalse(result.send)
        self.assertIn("[REPLY_ACTION]\ncasual", llm.prompt)
        self.assertIn("[IS_MENTIONED]\nyes", llm.prompt)
        self.assertIn("[IS_REPLY_TO_BOT]\nno", llm.prompt)
        self.assertIn("[REPLY_SOURCE]\ncasual", llm.prompt)
        self.assertIn("[CURRENT_MESSAGE]", llm.prompt)
        self.assertIn("[ASSISTANT_DRAFT_REPLY]", llm.prompt)
        self.assertIn("[STICKER_CANDIDATES]", llm.prompt)
        self.assertNotIn("[是否@机器人]", llm.prompt)
        self.assertNotIn("[是否回复机器人]", llm.prompt)
