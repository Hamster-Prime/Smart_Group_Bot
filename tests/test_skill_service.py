import unittest
from types import SimpleNamespace

from bot.services.skills.base import SkillRunResult
from bot.services.skills.service import SkillService


def _resp(*, content: str = "", tool_calls: list[dict] | None = None) -> SimpleNamespace:
    return SimpleNamespace(
        choices=[
            SimpleNamespace(
                message=SimpleNamespace(
                    content=content,
                    tool_calls=tool_calls or [],
                )
            )
        ]
    )


class _PlannedSkillService(SkillService):
    def __init__(self, responses: list[SimpleNamespace]) -> None:
        super().__init__(llm=object(), settings=None)
        self._responses = list(responses)

    async def _completion_with_fallbacks(self, messages, tools):
        if not self._responses:
            return None
        return self._responses.pop(0)

    async def _run_tool(self, *, name, arguments, context, skills=None):
        if name == "send_sticker":
            context.handled = True
            context.sticker_sent = True
            context.sticker_file_id = "sticker-file-id"
            context.suppress_followup_text = True
            return SkillRunResult(ok=True, skill=name, summary="")

        if name == "doubao_tts":
            context.handled = True
            context.tts_sent = True
            context.tts_text = "你好呀"
            context.suppress_followup_text = True
            return SkillRunResult(ok=True, skill=name, summary="你好呀")

        return SkillRunResult(ok=False, skill=name, summary="", error="unknown_skill")


class SkillServiceFollowupSuppressionTests(unittest.IsolatedAsyncioTestCase):
    async def test_send_sticker_does_not_return_followup_text(self) -> None:
        service = _PlannedSkillService(
            [
                _resp(
                    tool_calls=[
                        {
                            "id": "call-1",
                            "function": {
                                "name": "send_sticker",
                                "arguments": '{"query":"无语"}',
                            },
                        }
                    ]
                ),
                _resp(content="因为字多（贴纸贴贴完毕🤣）"),
            ]
        )

        result = await service.answer_with_skill("发个贴纸", intent_type="casual")

        self.assertTrue(result.handled)
        self.assertTrue(result.sticker_sent)
        self.assertEqual(result.sticker_file_id, "sticker-file-id")
        self.assertEqual(result.text, "")

    async def test_tts_skill_does_not_return_followup_text(self) -> None:
        service = _PlannedSkillService(
            [
                _resp(
                    tool_calls=[
                        {
                            "id": "call-1",
                            "function": {
                                "name": "doubao_tts",
                                "arguments": '{"text":"你好呀"}',
                            },
                        }
                    ]
                ),
                _resp(content="我发语音啦"),
            ]
        )

        result = await service.answer_with_skill("说一句你好呀", intent_type="casual")

        self.assertTrue(result.handled)
        self.assertTrue(result.tts_sent)
        self.assertEqual(result.tts_text, "你好呀")
        self.assertEqual(result.text, "")


if __name__ == "__main__":
    unittest.main()
