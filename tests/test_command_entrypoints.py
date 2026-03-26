import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from bot.config import Settings
from bot.handlers import admin, commands
from bot.services.skills.base import SkillRunResult


def _settings() -> Settings:
    settings = Settings(_env_file=None)
    settings.bot.auto_delete_minutes = 3
    return settings


class CommandEntrypointTests(unittest.IsolatedAsyncioTestCase):
    async def test_help_uses_shared_command_catalog_text(self) -> None:
        message = SimpleNamespace(
            chat=SimpleNamespace(id=-10001, type="supergroup"),
            from_user=SimpleNamespace(id=123, username="admin"),
            text="/help",
        )
        settings = _settings()

        with (
            patch("bot.handlers.commands.ensure_group_authorized", new=AsyncMock(return_value=True)),
            patch("bot.handlers.commands._answer", new=AsyncMock()) as answer_mock,
        ):
            await commands.cmd_help(message, session=object(), settings=settings)

        self.assertIn("命令总览", answer_mock.await_args.args[2])

    async def test_lm_list_reply_is_persistent(self) -> None:
        message = SimpleNamespace(
            chat=SimpleNamespace(id=-10001, type="supergroup"),
            from_user=SimpleNamespace(id=123, username="admin"),
            text="/lm",
        )
        settings = _settings()
        fake_memory = SimpleNamespace(list_permanent_memories=AsyncMock(return_value=[]))

        with (
            patch("bot.handlers.commands.ensure_group_authorized", new=AsyncMock(return_value=True)),
            patch("bot.handlers.commands.ensure_group_admin_permission", new=AsyncMock(return_value=True)),
            patch("bot.handlers.commands.memory_holder.get", return_value=fake_memory),
            patch("bot.handlers.commands._answer", new=AsyncMock()) as answer_mock,
        ):
            await commands.cmd_lm(message, session=object(), settings=settings)

        self.assertEqual(answer_mock.await_args.kwargs["auto_delete_minutes"], 0)
        self.assertIn("永久记忆", answer_mock.await_args.args[2])

    async def test_addrule_uses_rule_manage_skill(self) -> None:
        message = SimpleNamespace(
            chat=SimpleNamespace(id=-10001, type="supergroup"),
            from_user=SimpleNamespace(id=123, username="admin"),
            text="/addrule 新增群规 禁止发广告",
        )
        settings = _settings()
        fake_skill = SimpleNamespace(
            run_skill=AsyncMock(
                return_value=SkillRunResult(ok=True, skill="rule_manage", summary="规则添加成功")
            )
        )

        with (
            patch("bot.handlers.admin.ensure_group_authorized", new=AsyncMock(return_value=True)),
            patch("bot.handlers.admin.ensure_group_admin_permission", new=AsyncMock(return_value=True)),
            patch("bot.handlers.admin._build_skill_service", return_value=fake_skill),
            patch("bot.handlers.admin._answer", new=AsyncMock()) as answer_mock,
        ):
            await admin.cmd_addrule(message, session=object(), settings=settings)

        fake_skill.run_skill.assert_awaited()
        self.assertEqual(fake_skill.run_skill.await_args.args[0], "rule_manage")
        self.assertIn("规则添加成功", answer_mock.await_args.args[2])


if __name__ == "__main__":
    unittest.main()
