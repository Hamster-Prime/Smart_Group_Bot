import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from bot.config import Settings
from bot.handlers import admin, commands
from bot.services.skills.base import SkillRunResult
from bot.utils.command_catalog import build_command_guide_context


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
        self.assertIn("/clearwarnings", answer_mock.await_args.args[2])

    async def test_command_guide_includes_clearwarnings(self) -> None:
        guide = build_command_guide_context()

        self.assertIn("command: /clearwarnings", guide)
        self.assertIn("清空某用户的累计违规次数", guide)

    async def test_clearwarnings_calls_warning_reset_for_target(self) -> None:
        message = SimpleNamespace(
            chat=SimpleNamespace(id=-10001, type="supergroup"),
            from_user=SimpleNamespace(id=123, username="admin"),
            text="/clearwarnings 456",
            reply_to_message=None,
        )
        session = object()
        settings = _settings()

        with (
            patch("bot.handlers.admin.ensure_group_authorized", new=AsyncMock(return_value=True)),
            patch(
                "bot.handlers.admin.ensure_group_admin_permission",
                new=AsyncMock(return_value=True),
            ),
            patch(
                "bot.handlers.admin._clear_user_warning",
                new=AsyncMock(return_value=(2, False)),
            ) as clear_mock,
            patch("bot.handlers.admin._answer", new=AsyncMock()) as answer_mock,
        ):
            await admin.cmd_clearwarnings(message, session=session, settings=settings)

        clear_mock.assert_awaited_once_with(session, -10001, 456)
        self.assertIn("原为 2 次", answer_mock.await_args.args[2])

    async def test_warning_reset_deletes_state_and_returns_previous_values(self) -> None:
        warning = SimpleNamespace(count=4, is_banned=True)
        session = SimpleNamespace(
            execute=AsyncMock(
                return_value=SimpleNamespace(scalar_one_or_none=lambda: warning)
            ),
            delete=AsyncMock(),
        )

        cleared = await admin._clear_user_warning(session, -10001, 456)

        self.assertEqual(cleared, (4, True))
        session.delete.assert_awaited_once_with(warning)

    async def test_unban_calls_warning_reset_for_target(self) -> None:
        message = SimpleNamespace(
            chat=SimpleNamespace(id=-10001, type="supergroup"),
            from_user=SimpleNamespace(id=123, username="admin"),
            text="/unban 456",
            reply_to_message=None,
            bot=SimpleNamespace(unban_chat_member=AsyncMock()),
        )
        session = SimpleNamespace(commit=AsyncMock())
        settings = _settings()
        settings.super_admin_id = 123

        with (
            patch("bot.handlers.admin.ensure_group_authorized", new=AsyncMock(return_value=True)),
            patch("bot.handlers.admin.ensure_super_admin", new=AsyncMock(return_value=True)),
            patch("bot.handlers.admin.remove_global_ban", new=AsyncMock(return_value=False)),
            patch("bot.handlers.admin.delete_join_verifications_for_user", new=AsyncMock()),
            patch(
                "bot.handlers.admin._authorized_group_ids",
                new=AsyncMock(return_value=[-10001]),
            ),
            patch(
                "bot.handlers.admin._clear_user_warning",
                new=AsyncMock(return_value=(3, True)),
            ) as clear_mock,
            patch(
                "bot.handlers.admin.restore_member_permissions",
                new=AsyncMock(return_value=True),
            ),
            patch("bot.handlers.admin._answer", new=AsyncMock()) as answer_mock,
        ):
            await admin.cmd_unban(message, session=session, settings=settings)

        clear_mock.assert_awaited_once_with(session, -10001, 456)
        session.commit.assert_awaited_once()
        self.assertIn("累计 3 次", answer_mock.await_args.args[2])

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
