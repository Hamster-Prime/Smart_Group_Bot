import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from bot.handlers import group


class GroupChannelSenderTests(unittest.IsolatedAsyncioTestCase):
    def test_resolve_sender_identity_prefers_sender_chat_for_fake_bot_user(self) -> None:
        message = SimpleNamespace(
            from_user=SimpleNamespace(id=136817688, is_bot=True, username="Channel_Bot", full_name="Channel Bot"),
            sender_chat=SimpleNamespace(id=-1009876543210, username="test_channel", title="Test Channel"),
            author_signature="",
        )

        identity = group._resolve_sender_identity(message)

        self.assertTrue(identity.is_chat)
        self.assertEqual(identity.actor_id, -1009876543210)
        self.assertEqual(identity.username, "test_channel")
        self.assertEqual(identity.display_name, "Test Channel")

    async def test_group_message_allows_sender_chat_message_past_bot_filter(self) -> None:
        message = SimpleNamespace(
            chat=SimpleNamespace(id=-10001, type="supergroup", title="test"),
            from_user=SimpleNamespace(id=136817688, is_bot=True, username="Channel_Bot", full_name="Channel Bot"),
            sender_chat=SimpleNamespace(id=-1009876543210, username="test_channel", title="Test Channel"),
            text="1",
            bot=SimpleNamespace(me=AsyncMock(return_value=SimpleNamespace(username="selfbot", id=1))),
        )
        group_row = SimpleNamespace(settings={})
        session = object()
        settings = SimpleNamespace(bot=SimpleNamespace())

        with (
            patch("bot.handlers.group.ensure_group_authorized", new=AsyncMock(return_value=True)),
            patch("bot.handlers.group._ensure_group_row", new=AsyncMock(return_value=group_row)) as ensure_group_row,
            patch("bot.handlers.group.record_group_activity", return_value={}),
            patch("bot.handlers.group._best_effort_commit", new=AsyncMock()),
            patch("bot.handlers.group.extract_message_text", return_value=("", "text")),
        ):
            await group.on_group_message(message, session=session, settings=settings)

        ensure_group_row.assert_awaited_once_with(session, -10001, "test")

    async def test_channel_sender_is_auto_exempt_from_moderation(self) -> None:
        message = SimpleNamespace(
            chat=SimpleNamespace(id=-10001, type="supergroup", title="test"),
            from_user=SimpleNamespace(id=136817688, is_bot=True, username="Channel_Bot", full_name="Channel Bot"),
            sender_chat=SimpleNamespace(id=-1009876543210, username="test_channel", title="Test Channel"),
            text="bad text",
            bot=SimpleNamespace(me=AsyncMock(return_value=SimpleNamespace(username="selfbot", id=1))),
        )
        group_row = SimpleNamespace(settings={"mute_all_replies": True})
        session = object()
        settings = SimpleNamespace(
            bot=SimpleNamespace(
                main_model="",
                decision_model="",
                compress_model="",
                moderation_model="",
                vision_model="",
                embed_model="",
                max_context_tokens=0,
            ),
            moderation=SimpleNamespace(enabled=True),
            skill_sticker_file_ids="",
        )
        moderation_service = SimpleNamespace(
            is_user_exempt=AsyncMock(return_value=False),
            check_rules=AsyncMock(return_value=(False, "", None)),
        )

        with (
            patch("bot.handlers.group.ensure_group_authorized", new=AsyncMock(return_value=True)),
            patch("bot.handlers.group._ensure_group_row", new=AsyncMock(return_value=group_row)),
            patch("bot.handlers.group.record_group_activity", return_value=group_row.settings),
            patch("bot.handlers.group.extract_message_text", return_value=("bad text", "text")),
            patch("bot.handlers.group._append_image_context", new=AsyncMock(return_value=("bad text", ""))),
            patch("bot.handlers.group._build_reply_context_for_llm", new=AsyncMock(return_value="")),
            patch("bot.handlers.group._best_effort_commit", new=AsyncMock()),
            patch("bot.handlers.group._is_user_admin_cached", new=AsyncMock(return_value=False)),
            patch("bot.handlers.group.LLMService", return_value=object()),
            patch("bot.handlers.group.SkillService", return_value=object()),
            patch("bot.handlers.group.ModerationService", return_value=moderation_service),
        ):
            await group.on_group_message(message, session=session, settings=settings)

        moderation_service.is_user_exempt.assert_not_awaited()
        moderation_service.check_rules.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
