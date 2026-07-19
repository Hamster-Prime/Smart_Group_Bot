import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from bot.handlers import group
from bot.services.moderation import ModerationVerdict


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
            patch(
                "bot.handlers.group._fresh_group_authorized_for_moderation",
                new=AsyncMock(return_value=True),
            ),
            patch(
                "bot.handlers.group._record_group_activity_cas",
                new=AsyncMock(return_value=group_row.settings),
            ) as record_activity,
            patch("bot.handlers.group._best_effort_commit", new=AsyncMock()),
            patch("bot.handlers.group.extract_message_text", return_value=("", "text")),
        ):
            await group.on_group_message(message, session=session, settings=settings)

        record_activity.assert_awaited_once_with(
            session,
            group_id=-10001,
            title="test",
            settings=settings,
        )

    async def test_linked_channel_sender_is_not_auto_exempt_from_moderation(self) -> None:
        message = SimpleNamespace(
            chat=SimpleNamespace(
                id=-10001,
                type="supergroup",
                title="test",
                ban_sender_chat=AsyncMock(return_value=True),
            ),
            from_user=SimpleNamespace(id=136817688, is_bot=True, username="Channel_Bot", full_name="Channel Bot"),
            sender_chat=SimpleNamespace(id=-1009876543210, username="test_channel", title="Test Channel"),
            text="bad text",
            delete=AsyncMock(),
            bot=SimpleNamespace(me=AsyncMock(return_value=SimpleNamespace(username="selfbot", id=1))),
        )
        group_row = SimpleNamespace(settings={"mute_all_replies": True})
        session = SimpleNamespace(commit=AsyncMock())
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
        rule = SimpleNamespace(
            id=7,
            action="ban",
            rule_type="llm",
            pattern="禁止频道广告",
        )
        moderation_service = SimpleNamespace(
            is_user_exempt=AsyncMock(return_value=False),
            evaluate=AsyncMock(
                return_value=ModerationVerdict(
                    violated=True,
                    reason="频道广告",
                    rule=rule,
                    conclusive=True,
                    confidence=0.7,
                )
            ),
            is_high_confidence=lambda _verdict: False,
            record_violation=AsyncMock(),
        )
        challenge = AsyncMock()

        with (
            patch("bot.handlers.group.ensure_group_authorized", new=AsyncMock(return_value=True)),
            patch(
                "bot.handlers.group._fresh_group_authorized_for_moderation",
                new=AsyncMock(return_value=True),
            ),
            patch(
                "bot.handlers.group._record_group_activity_cas",
                new=AsyncMock(return_value=group_row.settings),
            ),
            patch("bot.handlers.group.extract_message_text", return_value=("bad text", "text")),
            patch("bot.handlers.group._append_image_context", new=AsyncMock(return_value=("bad text", ""))),
            patch("bot.handlers.group._build_reply_context_for_llm", new=AsyncMock(return_value="")),
            patch("bot.handlers.group._best_effort_commit", new=AsyncMock()),
            patch("bot.handlers.group._is_user_admin_cached", new=AsyncMock(return_value=False)),
            patch("bot.handlers.group.LLMService", return_value=object()),
            patch("bot.handlers.group.SkillService", return_value=object()),
            patch("bot.handlers.group.ModerationService", return_value=moderation_service),
            patch("bot.handlers.group.begin_moderation_challenge", new=challenge),
            patch("bot.handlers.group.answer_with_auto_delete", new=AsyncMock()),
        ):
            await group.on_group_message(message, session=session, settings=settings)

        moderation_service.is_user_exempt.assert_awaited_once_with(session, -10001, -1009876543210)
        moderation_service.evaluate.assert_awaited_once_with(session, -10001, "bad text")
        message.chat.ban_sender_chat.assert_awaited_once_with(-1009876543210)
        message.delete.assert_awaited_once()
        moderation_service.record_violation.assert_awaited_once()
        self.assertGreaterEqual(session.commit.await_count, 3)
        challenge.assert_not_awaited()

    async def test_sender_chat_failed_ban_is_retried_on_same_event(self) -> None:
        violation = SimpleNamespace(
            id=8,
            notice_sent_at=None,
            ban_enforced=None,
            action_taken="ban_applied",
        )
        message = SimpleNamespace(
            message_id=88,
            chat=SimpleNamespace(
                id=-10001,
                type="supergroup",
                title="test",
                ban_sender_chat=AsyncMock(side_effect=[False, True]),
            ),
            from_user=SimpleNamespace(
                id=136817688,
                is_bot=True,
                username="Channel_Bot",
                full_name="Channel Bot",
            ),
            sender_chat=SimpleNamespace(
                id=-1009876543210,
                username="test_channel",
                title="Test Channel",
            ),
            text="bad text",
            delete=AsyncMock(),
            bot=SimpleNamespace(
                me=AsyncMock(return_value=SimpleNamespace(username="selfbot", id=1))
            ),
        )
        group_row = SimpleNamespace(settings={"mute_all_replies": True})
        session = SimpleNamespace(commit=AsyncMock())
        settings = SimpleNamespace(
            bot=SimpleNamespace(
                main_model="",
                decision_model="",
                compress_model="",
                moderation_model="",
                vision_model="",
                embed_model="",
                max_context_tokens=0,
                auto_delete_categories=[],
                auto_delete_seconds=0,
            ),
            moderation=SimpleNamespace(enabled=True),
            skill_sticker_file_ids="",
        )
        rule = SimpleNamespace(
            id=7,
            action="ban",
            rule_type="llm",
            pattern="禁止频道广告",
        )
        moderation_service = SimpleNamespace(
            is_user_exempt=AsyncMock(return_value=False),
            evaluate=AsyncMock(
                return_value=ModerationVerdict(
                    violated=True,
                    reason="频道广告",
                    rule=rule,
                    conclusive=True,
                    confidence=0.7,
                )
            ),
            is_high_confidence=lambda _verdict: False,
            record_violation=AsyncMock(return_value=violation),
        )

        common_patches = (
            patch("bot.handlers.group.ensure_group_authorized", new=AsyncMock(return_value=True)),
            patch(
                "bot.handlers.group._fresh_group_authorized_for_moderation",
                new=AsyncMock(return_value=True),
            ),
            patch(
                "bot.handlers.group._record_group_activity_cas",
                new=AsyncMock(return_value=group_row.settings),
            ),
            patch("bot.handlers.group.extract_message_text", return_value=("bad text", "text")),
            patch("bot.handlers.group._append_image_context", new=AsyncMock(return_value=("bad text", ""))),
            patch("bot.handlers.group._build_reply_context_for_llm", new=AsyncMock(return_value="")),
            patch("bot.handlers.group._best_effort_commit", new=AsyncMock()),
            patch("bot.handlers.group._is_user_admin_cached", new=AsyncMock(return_value=False)),
            patch("bot.handlers.group.LLMService", return_value=object()),
            patch("bot.handlers.group.SkillService", return_value=object()),
            patch("bot.handlers.group.ModerationService", return_value=moderation_service),
            patch("bot.handlers.group.answer_with_auto_delete", new=AsyncMock()),
        )
        with common_patches[0], common_patches[1], common_patches[2], common_patches[3], common_patches[4], common_patches[5], common_patches[6], common_patches[7], common_patches[8], common_patches[9], common_patches[10], common_patches[11]:
            await group.on_group_message(message, session=session, settings=settings)
            await group.on_group_message(message, session=session, settings=settings)

        self.assertEqual(message.chat.ban_sender_chat.await_count, 2)
        self.assertTrue(violation.ban_enforced)

    async def test_anonymous_sender_using_current_group_identity_is_auto_exempt(self) -> None:
        message = SimpleNamespace(
            chat=SimpleNamespace(id=-10001, type="supergroup", title="test"),
            from_user=SimpleNamespace(
                id=1087968824,
                is_bot=True,
                username="GroupAnonymousBot",
                full_name="Group",
            ),
            sender_chat=SimpleNamespace(id=-10001, username=None, title="test"),
            text="admin announcement",
            bot=SimpleNamespace(
                me=AsyncMock(return_value=SimpleNamespace(username="selfbot", id=1))
            ),
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
            evaluate=AsyncMock(),
        )

        with (
            patch("bot.handlers.group.ensure_group_authorized", new=AsyncMock(return_value=True)),
            patch(
                "bot.handlers.group._fresh_group_authorized_for_moderation",
                new=AsyncMock(return_value=True),
            ),
            patch(
                "bot.handlers.group._record_group_activity_cas",
                new=AsyncMock(return_value=group_row.settings),
            ),
            patch("bot.handlers.group.extract_message_text", return_value=("admin announcement", "text")),
            patch("bot.handlers.group._append_image_context", new=AsyncMock(return_value=("admin announcement", ""))),
            patch("bot.handlers.group._build_reply_context_for_llm", new=AsyncMock(return_value="")),
            patch("bot.handlers.group._best_effort_commit", new=AsyncMock()),
            patch("bot.handlers.group._is_user_admin_cached", new=AsyncMock(return_value=False)),
            patch("bot.handlers.group.LLMService", return_value=object()),
            patch("bot.handlers.group.SkillService", return_value=object()),
            patch("bot.handlers.group.ModerationService", return_value=moderation_service),
        ):
            await group.on_group_message(message, session=session, settings=settings)

        moderation_service.is_user_exempt.assert_not_awaited()
        moderation_service.evaluate.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
