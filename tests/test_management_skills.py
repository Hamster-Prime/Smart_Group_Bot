import unittest
from unittest.mock import AsyncMock, patch

from bot.services.manage_intent import GroupIntent
from bot.services.manage_intent import TaskIntent
from bot.services.skills.base import SkillContext
from bot.services.skills.memory_manage import MemoryManageSkill
from bot.services.skills.rule_manage import RuleManageSkill
from bot.services.skills.task_manage import TaskManageSkill


class ManagementSkillDeleteGuardTests(unittest.IsolatedAsyncioTestCase):
    async def test_memory_manage_delete_redirects_to_lm(self) -> None:
        skill = MemoryManageSkill()
        context = SkillContext(
            llm=object(),
            chat_id=-10001,
            sender_is_tg_admin=True,
            current_user_text="删除永久记忆 #12",
        )

        with patch(
            "bot.services.skills.memory_manage.GroupIntentService.detect",
            new=AsyncMock(return_value=GroupIntent(intent="memory_manage", memory_action="delete")),
        ):
            result = await skill.run({}, context)

        self.assertTrue(result.ok)
        self.assertIn("/lm", result.summary)

    async def test_rule_manage_delete_redirects_to_rules(self) -> None:
        skill = RuleManageSkill()
        context = SkillContext(
            session=object(),
            llm=object(),
            chat_id=-10001,
            sender_is_tg_admin=True,
            current_user_text="删除第 3 条群规",
        )

        with patch(
            "bot.services.skills.rule_manage.GroupIntentService.detect",
            new=AsyncMock(return_value=GroupIntent(intent="rule_manage", rule_action="delete")),
        ):
            result = await skill.run({}, context)

        self.assertTrue(result.ok)
        self.assertIn("/rules", result.summary)

    async def test_task_manage_delete_redirects_to_tasks(self) -> None:
        skill = TaskManageSkill()
        context = SkillContext(
            session=object(),
            llm=object(),
            chat_id=-10001,
            current_user_text="取消今晚9点提醒我吃饭",
        )

        with patch(
            "bot.services.skills.task_manage.TaskIntentService.detect",
            new=AsyncMock(return_value=TaskIntent(intent="task_manage", task_action="delete")),
        ):
            result = await skill.run({}, context)

        self.assertTrue(result.ok)
        self.assertIn("/tasks", result.summary)
        self.assertIn("/canceltask", result.summary)


if __name__ == "__main__":
    unittest.main()
