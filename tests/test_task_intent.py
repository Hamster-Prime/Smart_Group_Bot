import unittest
from datetime import datetime

from bot.services.task_intent import TaskIntentService


class _DummyLLM:
    def __init__(self, response: str) -> None:
        self.response = response
        self.calls = 0

    async def decision(self, system: str, prompt: str) -> str:
        _ = system, prompt
        self.calls += 1
        return self.response


class TaskIntentServiceTests(unittest.IsolatedAsyncioTestCase):
    async def test_non_task_chat_skips_llm(self) -> None:
        llm = _DummyLLM(
            '{"intent":"task_manage","task_action":"add","task_type":"reminder","due_at":"2026-03-19 21:00:00","task_content":"吃饭","ack_text":"好"}'
        )
        svc = TaskIntentService(llm)

        intent = await svc.detect("我晚上九点吃饭")

        self.assertEqual(intent.intent, "chat")
        self.assertEqual(llm.calls, 0)

    async def test_explicit_reminder_add_works(self) -> None:
        llm = _DummyLLM(
            '{"intent":"task_manage","task_action":"add","task_type":"reminder","due_at":"2026-03-19 21:00:00","task_content":"吃饭","ack_text":"好，今晚九点提醒你吃饭。"}'
        )
        svc = TaskIntentService(llm)

        intent = await svc.detect("记得今晚九点提醒我吃饭")

        self.assertEqual(intent.intent, "task_manage")
        self.assertEqual(intent.task_action, "add")
        self.assertEqual(intent.task_type, "reminder")
        self.assertEqual(intent.task_content, "吃饭")
        self.assertIsInstance(intent.due_at, datetime)
        self.assertEqual(llm.calls, 1)

    async def test_agent_task_add_works(self) -> None:
        llm = _DummyLLM(
            '{"intent":"task_manage","task_action":"add","task_type":"agent_task","due_at":"2026-03-19 15:00:00","task_content":"查询今天的科技新闻并概述","ack_text":"好，下午三点我来处理。"}'
        )
        svc = TaskIntentService(llm)

        intent = await svc.detect("3点帮我查询今天的科技新闻并概述")

        self.assertEqual(intent.intent, "task_manage")
        self.assertEqual(intent.task_type, "agent_task")
        self.assertEqual(intent.task_content, "查询今天的科技新闻并概述")
        self.assertIsInstance(intent.due_at, datetime)
        self.assertEqual(llm.calls, 1)

    async def test_shorthand_future_task_without_help_me_still_works(self) -> None:
        llm = _DummyLLM(
            '{"intent":"task_manage","task_action":"add","task_type":"agent_task","due_at":"2026-03-19 20:32:00","task_content":"查找今天的新闻并概述","ack_text":"好，到时间我来处理。"}'
        )
        svc = TaskIntentService(llm)

        intent = await svc.detect("晚上8点32查找今天的新闻并概述")

        self.assertEqual(intent.intent, "task_manage")
        self.assertEqual(intent.task_type, "agent_task")
        self.assertEqual(intent.task_content, "查找今天的新闻并概述")
        self.assertEqual(llm.calls, 1)

    async def test_scheduled_keyword_task_still_works(self) -> None:
        llm = _DummyLLM(
            '{"intent":"task_manage","task_action":"add","task_type":"agent_task","due_at":"2026-03-19 20:33:00","task_content":"查找今天的新闻并概述","ack_text":"好，到时间我来处理。"}'
        )
        svc = TaskIntentService(llm)

        intent = await svc.detect("定时晚上8点33查找今天的新闻并概述")

        self.assertEqual(intent.intent, "task_manage")
        self.assertEqual(intent.task_type, "agent_task")
        self.assertEqual(llm.calls, 1)

    async def test_bot_mentioned_shorthand_task_still_works(self) -> None:
        llm = _DummyLLM(
            '{"intent":"task_manage","task_action":"add","task_type":"agent_task","due_at":"2026-03-19 20:32:00","task_content":"查找今天的新闻并概述","ack_text":"好，到时间我来处理。"}'
        )
        svc = TaskIntentService(llm)

        intent = await svc.detect("@sanite_share_bot 到了晚上8点32查找今天的新闻并概述")

        self.assertEqual(intent.intent, "task_manage")
        self.assertEqual(intent.task_type, "agent_task")
        self.assertEqual(llm.calls, 1)


if __name__ == "__main__":
    unittest.main()
