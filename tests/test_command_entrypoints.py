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


class _AsyncContext:
    async def __aenter__(self):
        return None

    async def __aexit__(self, exc_type, exc, traceback):
        return False


class CommandEntrypointTests(unittest.IsolatedAsyncioTestCase):
    def test_av_command_is_registered_on_router(self) -> None:
        callbacks = [handler.callback for handler in commands.router.message.handlers]

        self.assertIn(commands.cmd_av, callbacks)

    async def test_av_search_releases_db_transaction_before_external_lookup(self) -> None:
        message = SimpleNamespace(
            chat=SimpleNamespace(id=-10001, type="supergroup", title="test"),
            from_user=SimpleNamespace(id=123),
            text="/av test query",
        )
        session = SimpleNamespace(commit=AsyncMock())

        async def assert_committed_before_search(_query: str):
            session.commit.assert_awaited_once()
            return []

        service = SimpleNamespace(
            enabled=True,
            search=AsyncMock(side_effect=assert_committed_before_search),
            lookup_by_code=AsyncMock(),
        )
        group_row = SimpleNamespace(settings={"av_enabled": True})
        with (
            patch("bot.handlers.commands.ensure_group_authorized", new=AsyncMock(return_value=True)),
            patch("bot.handlers.commands._ensure_group_row", new=AsyncMock(return_value=group_row)),
            patch("bot.handlers.commands.AVSearchService", return_value=service),
            patch("bot.handlers.commands.typing_action", return_value=_AsyncContext()),
            patch("bot.handlers.commands._answer", new=AsyncMock()),
        ):
            await commands.cmd_av(message, session=session, settings=_settings())

        service.search.assert_awaited_once_with("test query")

    async def test_av_private_search_is_rejected_for_non_owner(self) -> None:
        message = SimpleNamespace(
            chat=SimpleNamespace(id=123, type="private", title=""),
            from_user=SimpleNamespace(id=123),
            text="/av test query",
        )
        session = SimpleNamespace(commit=AsyncMock())
        settings = _settings()
        settings.super_admin_id = 999

        with (
            patch("bot.handlers.commands.ensure_group_authorized", new=AsyncMock(return_value=True)),
            patch("bot.handlers.commands.AVSearchService") as service_cls,
            patch("bot.handlers.commands._answer", new=AsyncMock()) as answer_mock,
        ):
            await commands.cmd_av(message, session=session, settings=settings)

        session.commit.assert_awaited_once()
        service_cls.assert_not_called()
        self.assertIn("私聊仅最高管理员", answer_mock.await_args.args[2])

    async def test_av_group_search_requires_group_feature_flag(self) -> None:
        message = SimpleNamespace(
            chat=SimpleNamespace(id=-10001, type="supergroup", title="test"),
            from_user=SimpleNamespace(id=123),
            text="/av test query",
        )
        session = SimpleNamespace(commit=AsyncMock())
        group_row = SimpleNamespace(settings={"av_enabled": False})

        with (
            patch("bot.handlers.commands.ensure_group_authorized", new=AsyncMock(return_value=True)),
            patch("bot.handlers.commands._ensure_group_row", new=AsyncMock(return_value=group_row)),
            patch("bot.handlers.commands.AVSearchService") as service_cls,
            patch("bot.handlers.commands._answer", new=AsyncMock()) as answer_mock,
        ):
            await commands.cmd_av(message, session=session, settings=_settings())

        session.commit.assert_awaited_once()
        service_cls.assert_not_called()
        self.assertIn("当前群组未启用", answer_mock.await_args.args[2])

    async def test_av_private_search_allows_owner(self) -> None:
        message = SimpleNamespace(
            chat=SimpleNamespace(id=999, type="private", title=""),
            from_user=SimpleNamespace(id=999),
            text="/av test query",
        )
        session = SimpleNamespace(commit=AsyncMock())
        settings = _settings()
        settings.super_admin_id = 999
        service = SimpleNamespace(
            enabled=True,
            search=AsyncMock(return_value=[]),
            lookup_by_code=AsyncMock(),
        )

        with (
            patch("bot.handlers.commands.ensure_group_authorized", new=AsyncMock(return_value=True)),
            patch("bot.handlers.commands.AVSearchService", return_value=service),
            patch("bot.handlers.commands.typing_action", return_value=_AsyncContext()),
            patch("bot.handlers.commands._answer", new=AsyncMock()),
        ):
            await commands.cmd_av(message, session=session, settings=settings)

        session.commit.assert_awaited_once()
        service.search.assert_awaited_once_with("test query")

    async def test_av_private_callback_rechecks_current_owner_policy(self) -> None:
        message = SimpleNamespace(
            chat=SimpleNamespace(id=123, type="private", title=""),
        )
        callback = SimpleNamespace(
            data="avs:legacy-token:0",
            message=message,
            from_user=SimpleNamespace(id=123),
            answer=AsyncMock(),
        )
        session = SimpleNamespace(commit=AsyncMock())
        settings = _settings()
        settings.super_admin_id = 999

        with patch(
            "bot.handlers.commands.ensure_group_authorized",
            new=AsyncMock(return_value=True),
        ):
            await commands.on_av_search_paging(
                callback,
                settings=settings,
                session=session,
            )

        session.commit.assert_awaited_once()
        callback.answer.assert_awaited_once_with(
            "私聊仅最高管理员可使用 AV 查询",
            show_alert=True,
        )

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
        session = SimpleNamespace(commit=AsyncMock())
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
        session.commit.assert_awaited_once()

    async def test_warning_reset_preserves_banned_state_and_returns_previous_values(self) -> None:
        warning = SimpleNamespace(count=4, is_banned=True)
        session = SimpleNamespace(
            execute=AsyncMock(
                return_value=SimpleNamespace(scalar_one_or_none=lambda: warning)
            ),
            delete=AsyncMock(),
        )

        cleared = await admin._clear_user_warning(session, -10001, 456)

        self.assertEqual(cleared, (4, True))
        self.assertEqual(warning.count, 0)
        self.assertTrue(warning.is_banned)
        session.delete.assert_not_awaited()

    async def test_unban_warning_reset_removes_banned_state(self) -> None:
        warning = SimpleNamespace(count=4, is_banned=True)
        session = SimpleNamespace(
            execute=AsyncMock(
                return_value=SimpleNamespace(scalar_one_or_none=lambda: warning)
            ),
            delete=AsyncMock(),
        )

        cleared = await admin._clear_user_warning(
            session,
            -10001,
            456,
            preserve_ban=False,
        )

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
            patch(
                "bot.handlers.admin.lease_join_verification_for_unban",
                new=AsyncMock(),
            ),
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

        self.assertIn("崩溃恢复工单", text)
        message.bot.ban_chat_member.assert_awaited_once_with(
            -10001,
            456,
            revoke_messages=True,
        )
        delete_record.assert_not_awaited()
        delete_prompts.assert_not_awaited()

    async def test_group_ban_fails_closed_when_admin_status_is_unavailable(self) -> None:
        message = SimpleNamespace(
            chat=SimpleNamespace(id=-10001, type="supergroup"),
            from_user=SimpleNamespace(id=123, full_name="Admin"),
            reply_to_message=None,
            bot=SimpleNamespace(ban_chat_member=AsyncMock()),
        )
        with patch(
            "bot.handlers.admin._target_is_group_admin",
            new=AsyncMock(return_value=None),
        ):
            text = await admin._perform_group_ban_locked(
                message,
                SimpleNamespace(),
                _settings(),
                target_id=456,
                reason="test",
            )

        self.assertIn("避免误封", text)
        message.bot.ban_chat_member.assert_not_awaited()

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
            patch(
                "bot.handlers.admin.lease_join_verification_for_unban",
                new=AsyncMock(),
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
        message.bot.ban_chat_member.assert_awaited_once_with(
            -10001,
            456,
            revoke_messages=True,
        )
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
        session = SimpleNamespace(commit=AsyncMock())

        with (
            patch("bot.handlers.commands.ensure_group_authorized", new=AsyncMock(return_value=True)),
            patch("bot.handlers.commands.ensure_group_admin_permission", new=AsyncMock(return_value=True)),
            patch("bot.handlers.commands.memory_holder.get", return_value=fake_memory),
            patch("bot.handlers.commands._answer", new=AsyncMock()) as answer_mock,
        ):
            await commands.cmd_lm(
                message,
                session=session,
                settings=settings,
                session_factory=object(),
            )

        session.commit.assert_awaited_once()
        self.assertEqual(answer_mock.await_args.kwargs["auto_delete_seconds"], 0)
        self.assertIn("永久记忆", answer_mock.await_args.args[2])

    async def test_lm_skill_releases_auth_session_and_uses_factory(self) -> None:
        message = SimpleNamespace(
            chat=SimpleNamespace(id=-10001, type="supergroup"),
            from_user=SimpleNamespace(id=123, username="admin"),
            text="/lm add 记住测试内容",
        )
        settings = _settings()
        events: list[str] = []
        session = SimpleNamespace(
            commit=AsyncMock(side_effect=lambda: events.append("commit"))
        )
        session_factory = object()

        async def run_after_commit(*_args: object, **_kwargs: object) -> SkillRunResult:
            self.assertEqual(events, ["commit"])
            events.append("skill")
            return SkillRunResult(
                ok=True,
                skill="memory_manage",
                summary="永久记忆已写入",
            )

        fake_skill = SimpleNamespace(run_skill=AsyncMock(side_effect=run_after_commit))
        with (
            patch("bot.handlers.commands.ensure_group_authorized", new=AsyncMock(return_value=True)),
            patch("bot.handlers.commands.ensure_group_admin_permission", new=AsyncMock(return_value=True)),
            patch(
                "bot.handlers.commands.memory_holder.get",
                return_value=SimpleNamespace(),
            ),
            patch("bot.handlers.commands._build_skill_service", return_value=fake_skill),
            patch("bot.handlers.commands.typing_action", return_value=_AsyncContext()),
            patch("bot.handlers.commands._answer", new=AsyncMock()),
        ):
            await commands.cmd_lm(
                message,
                session=session,
                settings=settings,
                session_factory=session_factory,
            )

        self.assertIsNone(fake_skill.run_skill.await_args.kwargs["session"])
        self.assertIs(
            fake_skill.run_skill.await_args.kwargs["session_factory"],
            session_factory,
        )

    async def test_compact_command_is_registered_on_router(self) -> None:
        callbacks = [handler.callback for handler in commands.router.message.handlers]

        self.assertIn(commands.cmd_compact, callbacks)

    async def test_memory_list_paging_callback_stays_registered(self) -> None:
        callbacks = [handler.callback for handler in commands.router.callback_query.handlers]

        self.assertIn(commands.on_memory_list_paging, callbacks)
        self.assertIn(commands.on_memory_delete, callbacks)

    async def test_compact_releases_auth_session_before_compaction(self) -> None:
        message = SimpleNamespace(
            chat=SimpleNamespace(id=-10001, type="supergroup"),
            from_user=SimpleNamespace(id=123, username="admin"),
            text="/compact",
        )
        settings = _settings()
        events: list[str] = []
        session = SimpleNamespace(
            commit=AsyncMock(side_effect=lambda: events.append("commit"))
        )

        async def compact_after_commit(_group_id: int) -> dict:
            self.assertEqual(events, ["commit"])
            events.append("compact")
            return {"status": "ok", "compacted_messages": 7}

        fake_memory = SimpleNamespace(compact_now=AsyncMock(side_effect=compact_after_commit))
        with (
            patch("bot.handlers.commands.ensure_group_authorized", new=AsyncMock(return_value=True)),
            patch("bot.handlers.commands.ensure_group_admin_permission", new=AsyncMock(return_value=True)),
            patch("bot.handlers.commands.memory_holder.get", return_value=fake_memory),
            patch("bot.handlers.commands.typing_action", return_value=_AsyncContext()),
            patch("bot.handlers.commands._answer", new=AsyncMock()) as answer_mock,
        ):
            await commands.cmd_compact(message, session=session, settings=settings)

        fake_memory.compact_now.assert_awaited_once_with(-10001)
        self.assertIn("上下文压缩完成", answer_mock.await_args.args[2])
        self.assertIn("7", answer_mock.await_args.args[2])

    async def test_compact_rejects_private_chat(self) -> None:
        message = SimpleNamespace(
            chat=SimpleNamespace(id=123, type="private"),
            from_user=SimpleNamespace(id=123, username="admin"),
            text="/compact",
        )
        session = SimpleNamespace(commit=AsyncMock())
        fake_memory = SimpleNamespace(compact_now=AsyncMock())

        with (
            patch("bot.handlers.commands.ensure_group_authorized", new=AsyncMock(return_value=True)),
            patch("bot.handlers.commands.ensure_group_admin_permission", new=AsyncMock(return_value=True)),
            patch("bot.handlers.commands.memory_holder.get", return_value=fake_memory),
            patch("bot.handlers.commands._answer", new=AsyncMock()) as answer_mock,
        ):
            await commands.cmd_compact(message, session=session, settings=_settings())

        fake_memory.compact_now.assert_not_awaited()
        self.assertIn("请在目标群内使用", answer_mock.await_args.args[2])

    async def test_compact_reports_empty_and_failure_states(self) -> None:
        settings = _settings()
        for status, expected in (
            ("empty", "没有可压缩"),
            ("db_locked", "数据库暂时繁忙"),
            ("llm_empty", "压缩模型未返回摘要"),
        ):
            message = SimpleNamespace(
                chat=SimpleNamespace(id=-10001, type="supergroup"),
                from_user=SimpleNamespace(id=123, username="admin"),
                text="/compact",
            )
            session = SimpleNamespace(commit=AsyncMock())
            fake_memory = SimpleNamespace(
                compact_now=AsyncMock(return_value={"status": status, "compacted_messages": 0})
            )
            with (
                patch("bot.handlers.commands.ensure_group_authorized", new=AsyncMock(return_value=True)),
                patch("bot.handlers.commands.ensure_group_admin_permission", new=AsyncMock(return_value=True)),
                patch("bot.handlers.commands.memory_holder.get", return_value=fake_memory),
                patch("bot.handlers.commands.typing_action", return_value=_AsyncContext()),
                patch("bot.handlers.commands._answer", new=AsyncMock()) as answer_mock,
            ):
                await commands.cmd_compact(message, session=session, settings=settings)

            self.assertIn(expected, answer_mock.await_args.args[2], msg=f"status={status}")

    async def test_addrule_uses_rule_manage_skill(self) -> None:
        message = SimpleNamespace(
            chat=SimpleNamespace(id=-10001, type="supergroup"),
            from_user=SimpleNamespace(id=123, username="admin"),
            text="/addrule 新增群规 禁止发广告",
        )
        settings = _settings()
        events: list[str] = []
        session = SimpleNamespace(
            commit=AsyncMock(side_effect=lambda: events.append("commit"))
        )
        session_factory = object()

        async def run_after_commit(*_args: object, **_kwargs: object) -> SkillRunResult:
            self.assertEqual(events, ["commit"])
            events.append("skill")
            return SkillRunResult(ok=True, skill="rule_manage", summary="规则添加成功")

        fake_skill = SimpleNamespace(
            run_skill=AsyncMock(side_effect=run_after_commit)
        )

        with (
            patch("bot.handlers.admin.ensure_group_authorized", new=AsyncMock(return_value=True)),
            patch("bot.handlers.admin.ensure_group_admin_permission", new=AsyncMock(return_value=True)),
            patch("bot.handlers.admin._build_skill_service", return_value=fake_skill),
            patch("bot.handlers.admin._answer", new=AsyncMock()) as answer_mock,
        ):
            await admin.cmd_addrule(
                message,
                session=session,
                settings=settings,
                session_factory=session_factory,
            )

        fake_skill.run_skill.assert_awaited()
        self.assertEqual(fake_skill.run_skill.await_args.args[0], "rule_manage")
        self.assertIsNone(fake_skill.run_skill.await_args.kwargs["session"])
        self.assertIs(
            fake_skill.run_skill.await_args.kwargs["session_factory"],
            session_factory,
        )
        self.assertIn("规则添加成功", answer_mock.await_args.args[2])


if __name__ == "__main__":
    unittest.main()
