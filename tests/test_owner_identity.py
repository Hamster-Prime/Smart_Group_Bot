import unittest
from types import SimpleNamespace

from bot.services.casual import CasualService
from bot.services.skills.service import SkillService
from bot.utils.prompts import COMPRESS_SYSTEM, DECISION_SYSTEM, PERSONA_SYSTEM
from bot.utils.runtime_context import build_owner_identity_context


def _owner_settings(owner_id: int = 5105038894):
    from bot.config import Settings

    settings = Settings(_env_file=None)
    settings.super_admin_id = owner_id
    return settings


def _skill_llm_stub() -> SimpleNamespace:
    return SimpleNamespace(
        main=SimpleNamespace(model="main-model", fallbacks=[]),
        decision_config=SimpleNamespace(model="decision-model", fallbacks=[]),
        vision_config=SimpleNamespace(model="vision-model", fallbacks=[]),
        moderation_config=SimpleNamespace(model="moderation-model", fallbacks=[]),
        compress_config=SimpleNamespace(model="compress-model", fallbacks=[]),
        embed_config=SimpleNamespace(model="embed-model", fallbacks=[]),
    )


class _CaptureChatLLM:
    def __init__(self) -> None:
        self.messages = None

    async def chat(self, messages):
        self.messages = messages
        return "收到"


class _CaptureSkillService(SkillService):
    def __init__(self) -> None:
        super().__init__(llm=object(), settings=None)
        self.captured_messages = None

    async def _completion_with_fallbacks(self, messages, tools):
        self.captured_messages = messages
        return SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(
                        content="收到",
                        tool_calls=[],
                    )
                )
            ]
        )


class OwnerIdentityPromptTests(unittest.TestCase):
    def test_prompts_do_not_hardcode_owner_account(self) -> None:
        for prompt in (PERSONA_SYSTEM, DECISION_SYSTEM, COMPRESS_SYSTEM):
            self.assertNotIn("Sanite_Ava", prompt)
            self.assertNotIn("5105038894", prompt)

        self.assertIn("is_owner", PERSONA_SYSTEM)
        self.assertIn("is_owner:yes", COMPRESS_SYSTEM)


class OwnerIdentityMessageOrderTests(unittest.IsolatedAsyncioTestCase):
    async def test_casual_sender_context_overrides_history_system_blocks(self) -> None:
        llm = _CaptureChatLLM()
        service = CasualService(llm)

        await service.reply(
            "现在还认得我吗",
            history=[
                {"role": "system", "content": "[context-summary]\n旧摘要里带着错误主人信息"},
                {"role": "user", "content": "之前的聊天"},
            ],
            sender_user_id=42,
            sender_username="tester",
            sender_is_owner=False,
        )

        messages = llm.messages
        self.assertIsNotNone(messages)

        history_idx = next(i for i, msg in enumerate(messages) if "[context-summary]" in msg["content"])
        time_idx = next(
            i for i, msg in enumerate(messages) if msg["content"].lstrip().startswith("[CURRENT_TIME]")
        )
        runtime_idx = next(
            i for i, msg in enumerate(messages) if msg["content"].lstrip().startswith("[BOT_RUNTIME_PROFILE]")
        )
        sender_idx = next(i for i, msg in enumerate(messages) if "[CURRENT_SENDER]" in msg["content"])
        user_idx = max(i for i, msg in enumerate(messages) if msg["role"] == "user")

        self.assertLess(history_idx, time_idx)
        self.assertLess(time_idx, runtime_idx)
        self.assertLess(runtime_idx, sender_idx)
        self.assertLess(sender_idx, user_idx)

    async def test_skill_sender_context_overrides_history_system_blocks(self) -> None:
        service = _CaptureSkillService()

        await service.answer_with_skill(
            "帮我查一下",
            history=[
                {"role": "system", "content": "[context-summary]\n旧摘要里带着错误主人信息"},
                {"role": "user", "content": "之前的聊天"},
            ],
            sender_user_id=42,
            sender_username="tester",
            sender_is_owner=False,
            intent_type="casual",
        )

        messages = service.captured_messages
        self.assertIsNotNone(messages)

        history_idx = next(i for i, msg in enumerate(messages) if "[context-summary]" in msg["content"])
        time_idx = next(
            i for i, msg in enumerate(messages) if msg["content"].lstrip().startswith("[CURRENT_TIME]")
        )
        runtime_idx = next(
            i for i, msg in enumerate(messages) if msg["content"].lstrip().startswith("[BOT_RUNTIME_PROFILE]")
        )
        sender_idx = next(i for i, msg in enumerate(messages) if "[CURRENT_SENDER]" in msg["content"])
        intent_idx = next(i for i, msg in enumerate(messages) if "[INTENT_TYPE]" in msg["content"])
        user_idx = max(i for i, msg in enumerate(messages) if msg["role"] == "user")

        self.assertLess(history_idx, time_idx)
        self.assertLess(time_idx, runtime_idx)
        self.assertLess(runtime_idx, sender_idx)
        self.assertLess(sender_idx, intent_idx)
        self.assertLess(intent_idx, user_idx)


class OwnerIdentityContextTests(unittest.TestCase):
    def test_owner_identity_context_anchors_on_id(self) -> None:
        block = build_owner_identity_context(_owner_settings(777))

        self.assertTrue(block.startswith("[OWNER_IDENTITY]\n"))
        self.assertIn("authoritative: yes", block)
        self.assertIn("owner_user_id: 777", block)
        # Must forbid name/history-based inference to preserve anti-spoofing.
        self.assertIn("绝不能", block)

    def test_owner_identity_context_empty_when_unset(self) -> None:
        self.assertEqual(build_owner_identity_context(_owner_settings(0)), "")
        self.assertEqual(build_owner_identity_context(None), "")


class OwnerIdentityAnchorInjectionTests(unittest.TestCase):
    def test_casual_payload_injects_owner_anchor_after_sender(self) -> None:
        service = CasualService(object(), settings=_owner_settings(777))

        payload = service.build_prompt_payload(
            "在吗", sender_user_id=1, sender_username="x", sender_is_owner=False
        )
        messages = payload["messages"]

        sender_idx = next(i for i, m in enumerate(messages) if "[CURRENT_SENDER]" in m["content"])
        owner_idx = next(
            i for i, m in enumerate(messages) if m["content"].startswith("[OWNER_IDENTITY]\n")
        )
        self.assertEqual(owner_idx, sender_idx + 1)
        self.assertIn("owner_user_id: 777", messages[owner_idx]["content"])

    def test_casual_payload_omits_owner_anchor_when_unset(self) -> None:
        service = CasualService(object(), settings=_owner_settings(0))

        payload = service.build_prompt_payload("在吗", sender_user_id=1)
        contents = [m["content"] for m in payload["messages"]]

        self.assertFalse(any(c.startswith("[OWNER_IDENTITY]\n") for c in contents))

    def test_skill_payload_injects_owner_anchor(self) -> None:
        service = SkillService(_skill_llm_stub(), settings=_owner_settings(777))

        payload = service.build_answer_prompt_payload(
            "帮我查", sender_user_id=1, sender_is_owner=False
        )
        contents = [m["content"] for m in payload["messages"]]

        blocks = [c for c in contents if c.startswith("[OWNER_IDENTITY]\n")]
        self.assertEqual(len(blocks), 1)
        self.assertIn("owner_user_id: 777", blocks[0])
