import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from bot.config import Settings
from bot.handlers import admin, commands
from bot.services.skills.base import SkillRunResult
from bot.utils.command_catalog import build_command_guide_context


def _settings() -> Settings:
    settings = Settings(_env_file=None)
    settings.bot.auto_delete_seconds = 3
    settings.bot.auto_delete_categories = ["management"]
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
        self.assertIn("command: /raidguard", guide)

    async def test_raidguard_numeric_argument_uses_minutes(self) -> None:
        message = SimpleNamespace(
            chat=SimpleNamespace(id=-10001, type="supergroup"),
            from_user=SimpleNamespace(id=123, username="admin"),
            text="/raidguard 15",
        )
        service = SimpleNamespace(
            enable_manual_lockdown=AsyncMock(),
            disable_manual_lockdown=AsyncMock(),
            lockdown_status=lambda _group_id: {"active": False},
        )
        with (
            patch("bot.handlers.admin.ensure_group_authorized", new=AsyncMock(return_value=True)),
            patch("bot.handlers.admin._ensure_ban_command_admin", new=AsyncMock(return_value=True)),
            patch("bot.handlers.admin.get_raid_guard_service", return_value=service),
            patch("bot.handlers.admin._answer", new=AsyncMock()) as answer_mock,
        ):
            await admin.cmd_raidguard(message, session=object(), settings=_settings())

        service.enable_manual_lockdown.assert_awaited_once_with(
            -10001,
            duration_minutes=15,
        )
        self.assertIn("15 分钟", answer_mock.await_args.args[2])

    async def test_settings_entry_allows_authorized_group_admin(self) -> None:
        message = SimpleNamespace(
            chat=SimpleNamespace(id=99, type="private"),
            from_user=SimpleNamespace(id=99),
        )
        settings = _settings()
        settings.miniapp_public_base_url = "https://bot.example.com"
        session = SimpleNamespace(scalar=AsyncMock(return_value=1))
        with patch("bot.handlers.commands._answer", new=AsyncMock()) as answer_mock:
            await commands.cmd_settings(message, settings=settings, session=session)

        self.assertEqual(answer_mock.await_args.kwargs["auto_delete_seconds"], 0)
        keyboard = answer_mock.await_args.kwargs["reply_markup"]
        self.assertEqual(
            keyboard.inline_keyboard[0][0].web_app.url,
            "https://bot.example.com/settings",
        )

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

    async def test_super_admin_unban_prompts_for_scope(self) -> None:
        message = SimpleNamespace(
            chat=SimpleNamespace(id=-10001, type="supergroup"),
            from_user=SimpleNamespace(id=123, username="admin", full_name="Admin"),
            text="/unban 456",
            reply_to_message=None,
            bot=SimpleNamespace(unban_chat_member=AsyncMock()),
            answer=AsyncMock(return_value=SimpleNamespace(message_id=900)),
        )
        session = SimpleNamespace(commit=AsyncMock())
        settings = _settings()
        settings.super_admin_id = 123

        with (
            patch("bot.handlers.admin.ensure_group_authorized", new=AsyncMock(return_value=True)),
            patch("bot.handlers.admin._answer", new=AsyncMock()),
        ):
            await admin.cmd_unban(message, session=session, settings=settings)

        message.answer.assert_awaited_once()
        kwargs = message.answer.await_args.kwargs
        self.assertIn("请选择解封范围", message.answer.await_args.args[0])
        self.assertEqual(len(kwargs["reply_markup"].inline_keyboard[0]), 2)
        message.bot.unban_chat_member.assert_not_awaited()

    async def test_group_ban_failure_keeps_pending_verification(self) -> None:
        verification = SimpleNamespace(prompt_message_id=321)
        message = SimpleNamespace(
            chat=SimpleNamespace(id=-10001, type="supergroup"),
            from_user=SimpleNamespace(id=123, full_name="Admin"),
            reply_to_message=None,
            bot=SimpleNamespace(
                ban_chat_member=AsyncMock(side_effect=RuntimeError("denied")),
            ),
        )
        session = SimpleNamespace(commit=AsyncMock(), rollback=AsyncMock())

        with (
            patch("bot.handlers.admin._target_is_group_admin", new=AsyncMock(return_value=False)),
            patch("bot.handlers.admin.get_join_verification", new=AsyncMock(return_value=verification)),
            patch("bot.handlers.admin.delete_join_verification", new=AsyncMock()) as delete_record,
            patch("bot.handlers.admin.delete_verification_prompts", new=AsyncMock()) as delete_prompts,
            patch("bot.handlers.admin.record_ban_event", new=AsyncMock()),
        ):
            text = await admin._perform_group_ban_locked(
                message,
                session,
                _settings(),
                target_id=456,
                reason="test",
            )

        self.assertIn("原警告和真人验证状态均已保留", text)
        delete_record.assert_not_awaited()
        delete_prompts.assert_not_awaited()

    async def test_group_ban_success_cleans_verification_prompt_after_commit(self) -> None:
        verification = SimpleNamespace(prompt_message_id=322)
        message = SimpleNamespace(
            chat=SimpleNamespace(id=-10001, type="supergroup"),
            from_user=SimpleNamespace(id=123, full_name="Admin"),
            reply_to_message=None,
            bot=SimpleNamespace(ban_chat_member=AsyncMock(return_value=True)),
        )
        session = SimpleNamespace(commit=AsyncMock(), rollback=AsyncMock())

        with (
            patch("bot.handlers.admin._target_is_group_admin", new=AsyncMock(return_value=False)),
            patch(
                "bot.handlers.admin.get_join_verification",
                new=AsyncMock(side_effect=[verification, verification]),
            ),
            patch("bot.handlers.admin._mark_group_banned_after_telegram", new=AsyncMock()) as mark_banned,
            patch("bot.handlers.admin.delete_join_verification", new=AsyncMock()) as delete_record,
            patch("bot.handlers.admin.delete_verification_prompts", new=AsyncMock()) as delete_prompts,
            patch("bot.handlers.admin.record_ban_event", new=AsyncMock()),
        ):
            text = await admin._perform_group_ban_locked(
                message,
                session,
                _settings(),
                target_id=456,
                reason="test",
            )

        self.assertIn("本群封禁完成", text)
        mark_banned.assert_awaited_once()
        delete_record.assert_awaited_once_with(session, -10001, 456)
        delete_prompts.assert_awaited_once_with(message.bot, {(-10001, 322)})

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

        self.assertEqual(answer_mock.await_args.kwargs["auto_delete_seconds"], 0)
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
