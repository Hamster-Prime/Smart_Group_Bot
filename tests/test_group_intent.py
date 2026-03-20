import unittest

from bot.services.group_intent import GroupIntentService


class _DummyLLM:
    def __init__(self, response: str) -> None:
        self.response = response
        self.calls = 0

    async def decision(self, system: str, prompt: str) -> str:
        _ = system, prompt
        self.calls += 1
        return self.response


class GroupIntentServiceTests(unittest.IsolatedAsyncioTestCase):
    async def test_non_command_chat_skips_llm(self) -> None:
        llm = _DummyLLM(
            '{"intent":"memory_manage","memory_action":"add","memory_content":"x","memory_target":"","rule_instruction":""}'
        )
        svc = GroupIntentService(llm)

        intent = await svc.detect("你还记得白菜是谁吗")

        self.assertEqual(intent.intent, "chat")
        self.assertEqual(llm.calls, 0)

    async def test_group_announcement_is_not_memory_command(self) -> None:
        llm = _DummyLLM(
            '{"intent":"memory_manage","memory_action":"add","memory_content":"x","memory_target":"","rule_instruction":""}'
        )
        svc = GroupIntentService(llm)

        intent = await svc.detect("大家记住今晚八点开会")

        self.assertEqual(intent.intent, "chat")
        self.assertEqual(llm.calls, 0)

    async def test_explicit_memory_add_still_works(self) -> None:
        llm = _DummyLLM(
            '{"intent":"memory_manage","memory_action":"add","memory_content":"白菜是主理人","memory_target":"","rule_instruction":""}'
        )
        svc = GroupIntentService(llm)

        intent = await svc.detect("记住“白菜是主理人”")

        self.assertEqual(intent.intent, "memory_manage")
        self.assertEqual(intent.memory_action, "add")
        self.assertEqual(intent.memory_content, "白菜是主理人")
        self.assertEqual(llm.calls, 1)

    async def test_reply_style_memory_add_is_allowed(self) -> None:
        llm = _DummyLLM(
            '{"intent":"memory_manage","memory_action":"add","memory_content":"Sanite&Ava | Resting","memory_target":"","rule_instruction":""}'
        )
        svc = GroupIntentService(llm)

        intent = await svc.detect(
            "这个写入永久记忆\n[reply_to:text] Sanite&Ava | Resting",
            surface_text="这个写入永久记忆",
        )

        self.assertEqual(intent.intent, "memory_manage")
        self.assertEqual(intent.memory_action, "add")
        self.assertEqual(intent.memory_content, "Sanite&Ava | Resting")
        self.assertEqual(llm.calls, 1)

    async def test_explicit_rule_add_still_works(self) -> None:
        llm = _DummyLLM(
            '{"intent":"rule_manage","memory_action":"unknown","memory_content":"","memory_target":"","rule_instruction":"增加一条规则，禁止发广告"}'
        )
        svc = GroupIntentService(llm)

        intent = await svc.detect("增加一条规则，禁止发广告")

        self.assertEqual(intent.intent, "rule_manage")
        self.assertEqual(intent.rule_instruction, "增加一条规则，禁止发广告")
        self.assertEqual(llm.calls, 1)

    async def test_memory_unknown_action_is_downgraded_to_chat(self) -> None:
        llm = _DummyLLM(
            '{"intent":"memory_manage","memory_action":"unknown","memory_content":"白菜是主理人","memory_target":"","rule_instruction":""}'
        )
        svc = GroupIntentService(llm)

        intent = await svc.detect("记住“白菜是主理人”")

        self.assertEqual(intent.intent, "chat")
        self.assertEqual(llm.calls, 1)

    async def test_how_to_write_memory_is_not_command(self) -> None:
        llm = _DummyLLM(
            '{"intent":"memory_manage","memory_action":"add","memory_content":"x","memory_target":"","rule_instruction":""}'
        )
        svc = GroupIntentService(llm)

        intent = await svc.detect("永久记忆怎么写入")

        self.assertEqual(intent.intent, "chat")
        self.assertEqual(llm.calls, 0)


if __name__ == "__main__":
    unittest.main()
