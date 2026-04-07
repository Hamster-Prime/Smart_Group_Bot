import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from bot.config import Settings
from bot.handlers import admin, group
from bot.services.chat_bridge import (
    begin_chat_bridge_target_selection,
    build_chat_bridge_status_text,
    compose_chat_bridge_message,
    extract_chat_bridge_target_username,
    get_chat_bridge_state,
    is_bot_style_name,
    parse_incoming_chat_bridge_message,
    set_chat_bridge_enabled,
)


def _settings() -> Settings:
    settings = Settings(_env_file=None)
    settings.bot.auto_delete_minutes = 0
    settings.bot.enable_streaming = False
    return settings


class ChatBridgeHelperTests(unittest.TestCase):
    def test_parse_incoming_chat_bridge_message_requires_exact_format(self) -> None:
        parsed = parse_incoming_chat_bridge_message("/chat@Gansinibot 你好")

        self.assertIsNotNone(parsed)
        assert parsed is not None
        self.assertEqual(parsed.target_username, "gansinibot")
        self.assertEqual(parsed.body, "你好")
        self.assertIsNone(parse_incoming_chat_bridge_message("/chat"))
        self.assertIsNone(parse_incoming_chat_bridge_message("/chat@Gansinibot"))

    def test_extract_target_username_requires_single_username_token(self) -> None:
        self.assertEqual(extract_chat_bridge_target_username("  @ExampleBot "), "examplebot")
        self.assertEqual(extract_chat_bridge_target_username("@examplebot hi"), "")

    def test_compose_chat_bridge_message_keeps_required_prefix(self) -> None:
        self.assertEqual(
            compose_chat_bridge_message("ExampleBot", "你好"),
            "/chat@examplebot 你好",
        )
        self.assertEqual(
            compose_chat_bridge_message("ExampleBot", "/chat@otherbot 继续聊"),
            "/chat@examplebot 继续聊",
        )

    def test_bot_style_name_accepts_common_bot_suffix_forms(self) -> None:
        self.assertTrue(is_bot_style_name("gansinibot"))
        self.assertTrue(is_bot_style_name("gansini_bot"))
        self.assertTrue(is_bot_style_name("gansini-bot"))
        self.assertFalse(is_bot_style_name("gansini"))

    def test_status_text_reflects_waiting_and_active_states(self) -> None:
        waiting_text = build_chat_bridge_status_text(
            group_id=-10001,
            group_settings=begin_chat_bridge_target_selection({}, admin_id=1, prompt_message_id=9),
        )
        active_text = build_chat_bridge_status_text(
            group_id=-10001,
            group_settings={"chat_bridge_enabled": True, "chat_bridge_target_username": "peerbot"},
        )

        self.assertIn("等待目标", waiting_text)
        self.assertIn("@peerbot", active_text)


class AdminChatCommandTests(unittest.IsolatedAsyncioTestCase):
    async def test_cmd_chat_without_args_shows_status(self) -> None:
        message = SimpleNamespace(
            chat=SimpleNamespace(id=-10001, type="supergroup", title="test"),
            text="/chat",
        )
        session = SimpleNamespace(flush=AsyncMock())
        group_row = SimpleNamespace(settings={"chat_bridge_enabled": True, "chat_bridge_target_username": "peerbot"})
        settings = _settings()

        with (
            patch("bot.handlers.admin.ensure_group_authorized", new=AsyncMock(return_value=True)),
            patch("bot.handlers.admin.ensure_super_admin", new=AsyncMock(return_value=True)),
            patch("bot.handlers.admin._ensure_group_row", new=AsyncMock(return_value=group_row)),
            patch("bot.handlers.admin._answer", new=AsyncMock()) as answer_mock,
        ):
            await admin.cmd_chat(message, session=session, settings=settings)

        self.assertEqual(
            answer_mock.await_args.args[2],
            build_chat_bridge_status_text(
                group_id=message.chat.id,
                group_settings=group_row.settings,
            ),
        )

    async def test_cmd_chat_enable_starts_target_selection(self) -> None:
        message = SimpleNamespace(
            chat=SimpleNamespace(id=-10001, type="supergroup", title="test"),
            from_user=SimpleNamespace(id=42),
            text="/chat enable",
        )
        session = SimpleNamespace(flush=AsyncMock())
        group_row = SimpleNamespace(settings={})
        settings = _settings()

        with (
            patch("bot.handlers.admin.ensure_group_authorized", new=AsyncMock(return_value=True)),
            patch("bot.handlers.admin.ensure_super_admin", new=AsyncMock(return_value=True)),
            patch("bot.handlers.admin._ensure_group_row", new=AsyncMock(return_value=group_row)),
            patch(
                "bot.handlers.admin.answer_with_auto_delete",
                new=AsyncMock(return_value=SimpleNamespace(message_id=77)),
            ) as answer_mock,
        ):
            await admin.cmd_chat(message, session=session, settings=settings)

        state = get_chat_bridge_state(group_row.settings)
        self.assertTrue(state.enabled)
        self.assertTrue(state.waiting_for_target)
        self.assertEqual(state.pending_admin_id, 42)
        self.assertEqual(state.prompt_message_id, 77)
        session.flush.assert_awaited()
        self.assertIn("等待目标 bot", answer_mock.await_args.args[1])

    async def test_cmd_chat_disable_clears_state(self) -> None:
        message = SimpleNamespace(
            chat=SimpleNamespace(id=-10001, type="supergroup", title="test"),
            text="/chat disable",
        )
        session = SimpleNamespace(flush=AsyncMock())
        group_row = SimpleNamespace(
            settings=begin_chat_bridge_target_selection(
                {"chat_bridge_target_username": "peerbot", "chat_bridge_enabled": True},
                admin_id=1,
                prompt_message_id=2,
            )
        )
        settings = _settings()

        with (
            patch("bot.handlers.admin.ensure_group_authorized", new=AsyncMock(return_value=True)),
            patch("bot.handlers.admin.ensure_super_admin", new=AsyncMock(return_value=True)),
            patch("bot.handlers.admin._ensure_group_row", new=AsyncMock(return_value=group_row)),
            patch("bot.handlers.admin._answer", new=AsyncMock()) as answer_mock,
        ):
            await admin.cmd_chat(message, session=session, settings=settings)

        self.assertEqual(group_row.settings, set_chat_bridge_enabled(group_row.settings, False))
        self.assertIn("已关闭", answer_mock.await_args.args[2])


class GroupChatBridgeRoutingTests(unittest.IsolatedAsyncioTestCase):
    async def test_maybe_handle_chat_bridge_turn_rejects_non_bot_sender(self) -> None:
        message = SimpleNamespace(
            chat=SimpleNamespace(id=-10001),
            from_user=SimpleNamespace(id=123, is_bot=False, full_name="Alice"),
        )
        group_row = SimpleNamespace(settings={"chat_bridge_enabled": True, "chat_bridge_target_username": "peerbot"})
        sender_identity = group._SenderIdentity(
            actor_id=123,
            username="peerbot",
            display_name="peerbot",
            is_chat=False,
        )

        handled = await group._maybe_handle_chat_bridge_turn(
            message=message,
            settings=_settings(),
            sender_identity=sender_identity,
            input_text="/chat@selfbot 你好",
            group_row=group_row,
            my_username="selfbot",
        )

        self.assertFalse(handled)

    async def test_group_message_routes_bot_chat_bridge_before_bot_filter(self) -> None:
        message = SimpleNamespace(
            chat=SimpleNamespace(id=-10001, type="supergroup", title="test"),
            from_user=SimpleNamespace(id=999, is_bot=True, username="peerbot", full_name="Peer Bot"),
            sender_chat=None,
            text="/chat@selfbot 你好",
            bot=SimpleNamespace(me=AsyncMock(return_value=SimpleNamespace(username="selfbot", id=10))),
        )
        group_row = SimpleNamespace(settings={"chat_bridge_enabled": True, "chat_bridge_target_username": "peerbot"})
        session = object()
        settings = _settings()

        with (
            patch("bot.handlers.group.ensure_group_authorized", new=AsyncMock(return_value=True)),
            patch("bot.handlers.group._ensure_group_row", new=AsyncMock(return_value=group_row)),
            patch("bot.handlers.group.record_group_activity", return_value=group_row.settings),
            patch("bot.handlers.group.extract_message_text", return_value=("/chat@selfbot 你好", "text")),
            patch("bot.handlers.group._maybe_handle_chat_bridge_target_reply", new=AsyncMock(return_value=False)),
            patch("bot.handlers.group._maybe_handle_chat_bridge_turn", new=AsyncMock(return_value=True)) as bridge_mock,
            patch("bot.handlers.group._best_effort_commit", new=AsyncMock()) as commit_mock,
        ):
            await group.on_group_message(message, session=session, settings=settings)

        bridge_mock.assert_awaited_once()
        commit_mock.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
