import html
import re
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from bot.services.manage_intent import GroupIntent
from bot.services.skills.base import SkillContext
from bot.services.skills.memory_manage import MemoryManageSkill
from bot.services.skills.rule_manage import RuleManageSkill, _format_rule_list


class ManagementSkillDeleteGuardTests(unittest.IsolatedAsyncioTestCase):
    def test_memory_list_stays_within_telegram_utf16_limit(self) -> None:
        items = [
            SimpleNamespace(id=9_223_372_036_854_775_000 + index, content="😀" * 140)
            for index in range(20)
        ]

        rendered = MemoryManageSkill._format_memory_list(items)
        visible = html.unescape(re.sub(r"<[^>]+>", "", rendered))

        self.assertLessEqual(len(visible.encode("utf-16-le")) // 2, 4096)
        self.assertIn("<b>永久记忆</b>", rendered)
        self.assertIn("<code>#9223372036854775000</code>", rendered)

    def test_rule_list_stays_within_telegram_utf16_limit(self) -> None:
        rules = [
            SimpleNamespace(
                id=9_223_372_036_854_775_000 + index,
                rule_type="llm",
                action="ban",
                enabled=True,
                pattern="🚫" * 120,
            )
            for index in range(20)
        ]

        rendered = _format_rule_list(rules)
        visible = html.unescape(re.sub(r"<[^>]+>", "", rendered))

        self.assertLessEqual(len(visible.encode("utf-16-le")) // 2, 4096)
        self.assertIn("<b>群审核规则</b>", rendered)

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


if __name__ == "__main__":
    unittest.main()
