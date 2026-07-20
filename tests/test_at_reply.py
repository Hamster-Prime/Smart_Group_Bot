import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from bot.config import Settings
from bot.handlers import admin, group
from bot.services.at_reply import build_at_reply_status_text, is_at_reply_enabled, set_at_reply_enabled


def _settings() -> Settings:
    settings = Settings(_env_file=None)
    settings.bot.auto_delete_minutes = 0
    return settings


class AtReplyModeHelpersTests(unittest.TestCase):
    def test_mode_defaults_to_disabled(self) -> None:
        self.assertFalse(is_at_reply_enabled({}))
        self.assertFalse(is_at_reply_enabled(None))

    def test_set_mode_round_trip(self) -> None:
        enabled = set_at_reply_enabled({}, True)
        self.assertTrue(is_at_reply_enabled(enabled))

        disabled = set_at_reply_enabled(enabled, False)
        self.assertFalse(is_at_reply_enabled(disabled))
        self.assertNotIn("at_reply_mode", disabled)

    def test_status_text_mentions_enabled_behavior(self) -> None:
        text = build_at_reply_status_text(group_id=-10001, group_settings={"at_reply_mode": True})

        self.assertIn("@bot", text)
        self.assertIn("bot", text)


class AdminAtReplyCommandTests(unittest.IsolatedAsyncioTestCase):
    async def test_cmd_atreply_without_args_shows_status(self) -> None:
        message = SimpleNamespace(
            chat=SimpleNamespace(id=-10001, type="supergroup", title="test"),
            text="/atreply",
        )
        session = SimpleNamespace()
        group_row = SimpleNamespace(settings={"at_reply_mode": True})
        settings = _settings()

        with (
            patch("bot.handlers.admin.ensure_group_authorized", new=AsyncMock(return_value=True)),
            patch("bot.handlers.admin.ensure_super_admin", new=AsyncMock(return_value=True)),
            patch("bot.handlers.admin._ensure_group_row", new=AsyncMock(return_value=group_row)),
            patch("bot.handlers.admin._answer", new=AsyncMock()) as answer_mock,
        ):
            await admin.cmd_atreply(message, session=session, settings=settings)

        self.assertEqual(
            answer_mock.await_args.args[2],
            build_at_reply_status_text(
                group_id=message.chat.id,
                group_settings=group_row.settings,
            ),
        )

    async def test_cmd_atreply_requires_super_admin(self) -> None:
        message = SimpleNamespace(
            chat=SimpleNamespace(id=-10001, type="supergroup", title="test"),
            text="/atreply enable",
        )
        session = SimpleNamespace()
        settings = _settings()

        with (
            patch("bot.handlers.admin.ensure_group_authorized", new=AsyncMock(return_value=True)),
            patch("bot.handlers.admin.ensure_super_admin", new=AsyncMock(return_value=False)) as super_admin_mock,
            patch("bot.handlers.admin._ensure_group_row", new=AsyncMock()) as ensure_group_row_mock,
            patch("bot.handlers.admin._answer", new=AsyncMock()) as answer_mock,
        ):
            await admin.cmd_atreply(message, session=session, settings=settings)

        super_admin_mock.assert_awaited_once()
        ensure_group_row_mock.assert_not_awaited()
        answer_mock.assert_not_awaited()

    async def test_cmd_atreply_rejects_invalid_args(self) -> None:
        message = SimpleNamespace(
            chat=SimpleNamespace(id=-10001, type="supergroup", title="test"),
            text="/atreply on",
        )
        session = SimpleNamespace()
        group_row = SimpleNamespace(settings={})
        settings = _settings()

        with (
            patch("bot.handlers.admin.ensure_group_authorized", new=AsyncMock(return_value=True)),
            patch("bot.handlers.admin.ensure_super_admin", new=AsyncMock(return_value=True)),
            patch("bot.handlers.admin._ensure_group_row", new=AsyncMock(return_value=group_row)),
            patch("bot.handlers.admin._answer", new=AsyncMock()) as answer_mock,
        ):
            await admin.cmd_atreply(message, session=session, settings=settings)

        self.assertEqual(group_row.settings, {})
        self.assertEqual(answer_mock.await_args.args[2], admin._AT_REPLY_USAGE)

    async def test_cmd_atreply_enable_updates_settings(self) -> None:
        message = SimpleNamespace(
            chat=SimpleNamespace(id=-10001, type="supergroup", title="test"),
            text="/atreply enable",
        )
        session = SimpleNamespace()
        group_row = SimpleNamespace(settings={})
        settings = _settings()

        with (
            patch("bot.handlers.admin.ensure_group_authorized", new=AsyncMock(return_value=True)),
            patch("bot.handlers.admin.ensure_super_admin", new=AsyncMock(return_value=True)),
            patch("bot.handlers.admin._ensure_group_row", new=AsyncMock(return_value=group_row)),
            patch("bot.handlers.admin._answer", new=AsyncMock()) as answer_mock,
        ):
            await admin.cmd_atreply(message, session=session, settings=settings)

        self.assertTrue(group_row.settings["at_reply_mode"])
        self.assertEqual(
            answer_mock.await_args.args[2],
            build_at_reply_status_text(
                group_id=message.chat.id,
                group_settings=group_row.settings,
            ),
        )

    async def test_cmd_atreply_disable_clears_settings(self) -> None:
        message = SimpleNamespace(
            chat=SimpleNamespace(id=-10001, type="supergroup", title="test"),
            text="/atreply disable",
        )
        session = SimpleNamespace()
        group_row = SimpleNamespace(settings={"at_reply_mode": True})
        settings = _settings()

        with (
            patch("bot.handlers.admin.ensure_group_authorized", new=AsyncMock(return_value=True)),
            patch("bot.handlers.admin.ensure_super_admin", new=AsyncMock(return_value=True)),
            patch("bot.handlers.admin._ensure_group_row", new=AsyncMock(return_value=group_row)),
            patch("bot.handlers.admin._answer", new=AsyncMock()) as answer_mock,
        ):
            await admin.cmd_atreply(message, session=session, settings=settings)

        self.assertNotIn("at_reply_mode", group_row.settings)
        self.assertEqual(
            answer_mock.await_args.args[2],
            build_at_reply_status_text(
                group_id=message.chat.id,
                group_settings=group_row.settings,
            ),
        )


class PendingReplyActionResolutionTests(unittest.IsolatedAsyncioTestCase):
    def test_expected_at_reply_skip_is_debug_only(self) -> None:
        with (
            patch.object(group.log, "debug") as debug_log,
            patch.object(group.log, "info") as info_log,
        ):
            group._log_pending_reply_action(
                group_id=-10001,
                action="skip",
                action_forced=True,
                explicit_mention=False,
                reply_to_bot=False,
                elapsed_ms=3,
            )

        debug_log.assert_called_once()
        info_log.assert_not_called()

    def test_forced_mention_reply_remains_info(self) -> None:
        with (
            patch.object(group.log, "debug") as debug_log,
            patch.object(group.log, "info") as info_log,
        ):
            group._log_pending_reply_action(
                group_id=-10001,
                action="casual",
                action_forced=True,
                explicit_mention=True,
                reply_to_bot=False,
                elapsed_ms=4,
            )

        debug_log.assert_not_called()
        info_log.assert_called_once()

    async def test_at_reply_enabled_skips_unmentioned_without_decision(self) -> None:
        decision_svc = SimpleNamespace(decide=AsyncMock(return_value="casual"))

        action, forced = await group._resolve_pending_reply_action(
            decision_svc=decision_svc,
            group_settings={"at_reply_mode": True},
            explicit_mention=False,
            input_text="hello",
            is_mentioned=False,
            is_reply=False,
            is_reply_to_bot=False,
            is_reply_to_other=False,
            mentions_other_user=False,
            is_owner=False,
            is_tg_admin=False,
            user_tag="id:123",
            msg_type="text",
            history=[],
            merged_count=1,
            merged_context="",
        )

        self.assertEqual(action, "skip")
        self.assertTrue(forced)
        decision_svc.decide.assert_not_awaited()

    async def test_at_reply_enabled_forces_casual_on_explicit_mention(self) -> None:
        decision_svc = SimpleNamespace(decide=AsyncMock(return_value="skip"))

        action, forced = await group._resolve_pending_reply_action(
            decision_svc=decision_svc,
            group_settings={"at_reply_mode": True},
            explicit_mention=True,
            input_text="@bot help",
            is_mentioned=True,
            is_reply=False,
            is_reply_to_bot=False,
            is_reply_to_other=False,
            mentions_other_user=False,
            is_owner=False,
            is_tg_admin=False,
            user_tag="id:123",
            msg_type="text",
            history=[],
            merged_count=1,
            merged_context="",
        )

        self.assertEqual(action, "casual")
        self.assertTrue(forced)
        decision_svc.decide.assert_not_awaited()

    async def test_at_reply_enabled_forces_casual_on_reply_to_bot(self) -> None:
        decision_svc = SimpleNamespace(decide=AsyncMock(return_value="skip"))

        action, forced = await group._resolve_pending_reply_action(
            decision_svc=decision_svc,
            group_settings={"at_reply_mode": True},
            explicit_mention=False,
            input_text="continue",
            is_mentioned=False,
            is_reply=True,
            is_reply_to_bot=True,
            is_reply_to_other=False,
            mentions_other_user=False,
            is_owner=False,
            is_tg_admin=False,
            user_tag="id:123",
            msg_type="text",
            history=[],
            merged_count=1,
            merged_context="",
        )

        self.assertEqual(action, "casual")
        self.assertTrue(forced)
        decision_svc.decide.assert_not_awaited()

    async def test_explicit_mention_bypasses_decision_even_when_at_reply_disabled(self) -> None:
        decision_svc = SimpleNamespace(decide=AsyncMock(return_value="skip"))

        action, forced = await group._resolve_pending_reply_action(
            decision_svc=decision_svc,
            group_settings={},
            explicit_mention=True,
            input_text="@bot help",
            is_mentioned=True,
            is_reply=False,
            is_reply_to_bot=False,
            is_reply_to_other=False,
            mentions_other_user=False,
            is_owner=False,
            is_tg_admin=False,
            user_tag="id:123",
            msg_type="text",
            history=[],
            merged_count=1,
            merged_context="",
        )

        self.assertEqual(action, "casual")
        self.assertTrue(forced)
        decision_svc.decide.assert_not_awaited()

    async def test_reply_to_bot_bypasses_decision_even_when_at_reply_disabled(self) -> None:
        decision_svc = SimpleNamespace(decide=AsyncMock(return_value="skip"))

        action, forced = await group._resolve_pending_reply_action(
            decision_svc=decision_svc,
            group_settings={},
            explicit_mention=False,
            input_text="continue",
            is_mentioned=False,
            is_reply=True,
            is_reply_to_bot=True,
            is_reply_to_other=False,
            mentions_other_user=False,
            is_owner=False,
            is_tg_admin=False,
            user_tag="id:123",
            msg_type="text",
            history=[],
            merged_count=1,
            merged_context="",
        )

        self.assertEqual(action, "casual")
        self.assertTrue(forced)
        decision_svc.decide.assert_not_awaited()

    async def test_non_mention_still_uses_decision_service_when_at_reply_disabled(self) -> None:
        decision_svc = SimpleNamespace(decide=AsyncMock(return_value="casual"))

        action, forced = await group._resolve_pending_reply_action(
            decision_svc=decision_svc,
            group_settings={},
            explicit_mention=False,
            input_text="hello",
            is_mentioned=False,
            is_reply=False,
            is_reply_to_bot=False,
            is_reply_to_other=False,
            mentions_other_user=False,
            is_owner=False,
            is_tg_admin=False,
            user_tag="id:123",
            msg_type="text",
            history=[],
            merged_count=1,
            merged_context="",
        )

        self.assertEqual(action, "casual")
        self.assertFalse(forced)
        decision_svc.decide.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
