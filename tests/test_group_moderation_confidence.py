import unittest
from contextlib import ExitStack
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from bot.handlers import group
from bot.services.moderation import ModerationVerdict
from bot.services.update_completion import (
    UpdateCompletionReceipt,
    bind_update_completion,
    reset_update_completion,
)


def _settings() -> SimpleNamespace:
    return SimpleNamespace(
        super_admin_id=1,
        bot=SimpleNamespace(
            main_model="",
            decision_model="",
            compress_model="",
            moderation_model="",
            vision_model="",
            embed_model="",
            max_context_tokens=0,
            auto_delete_seconds=0,
        ),
        moderation=SimpleNamespace(enabled=True, warn_threshold=3),
        skill_sticker_file_ids="",
    )


def _message() -> SimpleNamespace:
    return SimpleNamespace(
        message_id=777,
        chat=SimpleNamespace(
            id=-10001,
            type="supergroup",
            title="test",
            ban=AsyncMock(),
        ),
        from_user=SimpleNamespace(
            id=42,
            is_bot=False,
            username="member",
            full_name="Member",
        ),
        sender_chat=None,
        text="ambiguous message",
        delete=AsyncMock(),
        bot=SimpleNamespace(
            me=AsyncMock(
                return_value=SimpleNamespace(username="selfbot", id=1)
            ),
            ban_chat_member=AsyncMock(return_value=True),
            get_chat_member=AsyncMock(return_value=SimpleNamespace(status="member")),
        ),
    )


class GroupModerationConfidenceTests(unittest.IsolatedAsyncioTestCase):
    async def _run(
        self,
        moderation_service: SimpleNamespace,
        *,
        message: SimpleNamespace | None = None,
        msg_type: str = "text",
        **extra_patches,
    ):
        message = message or _message()
        session = SimpleNamespace(
            flush=AsyncMock(),
            commit=AsyncMock(),
            rollback=AsyncMock(),
            delete=AsyncMock(),
            execute=AsyncMock(return_value=SimpleNamespace(rowcount=1)),
        )
        message._test_session = session
        group_row = SimpleNamespace(settings={"mute_all_replies": True})
        patches = [
            patch(
                "bot.handlers.group.ensure_group_authorized",
                new=AsyncMock(return_value=True),
            ),
            patch(
                "bot.handlers.group._fresh_group_authorized_for_moderation",
                new=AsyncMock(return_value=True),
            ),
            patch(
                "bot.handlers.group._record_group_activity_cas",
                new=AsyncMock(return_value=group_row.settings),
            ),
            patch(
                "bot.handlers.group.extract_message_text",
                return_value=("ambiguous message", msg_type),
            ),
            patch(
                "bot.handlers.group._append_image_context",
                new=AsyncMock(return_value=("ambiguous message", "")),
            ),
            patch(
                "bot.handlers.group._build_reply_context_for_llm",
                new=AsyncMock(return_value=""),
            ),
            patch("bot.handlers.group._best_effort_commit", new=AsyncMock()),
            patch(
                "bot.handlers.group._is_user_admin_cached",
                new=AsyncMock(return_value=False),
            ),
            patch("bot.handlers.group.LLMService", return_value=object()),
            patch(
                "bot.handlers.group.ModerationService",
                return_value=moderation_service,
            ),
        ]
        patches.extend(extra_patches.values())
        with ExitStack() as stack:
            for patcher in patches:
                stack.enter_context(patcher)
            await group.on_group_message(
                message,
                session=session,
                settings=_settings(),
            )
        return message

    async def test_low_confidence_deletes_and_issues_challenge(self) -> None:
        verdict = ModerationVerdict(
            violated=True,
            reason="语义存在歧义",
            rule=None,
            conclusive=True,
            confidence=0.7,
        )
        moderation = SimpleNamespace(
            is_user_exempt=AsyncMock(return_value=False),
            evaluate=AsyncMock(return_value=verdict),
            is_high_confidence=lambda _verdict: False,
            record_violation=AsyncMock(
                return_value=SimpleNamespace(id=320, notice_sent_at=None)
            ),
        )
        begin = AsyncMock(return_value=True)

        message = await self._run(
            moderation,
            ready=patch("bot.handlers.group.moderation_challenge_ready", return_value=True),
            begin=patch("bot.handlers.group.begin_moderation_challenge", new=begin),
        )

        message.delete.assert_awaited_once()
        begin.assert_awaited_once()
        self.assertEqual(begin.await_args.kwargs["user_id"], 42)
        self.assertEqual(begin.await_args.kwargs["reason"], "语义存在歧义")
        moderation.record_violation.assert_awaited_once()
        self.assertEqual(moderation.record_violation.await_args.args[4], "challenge")
        self.assertEqual(
            moderation.record_violation.await_args.kwargs["source_message_id"],
            777,
        )

    async def test_failed_challenge_releases_source_key_for_warn_fallback(self) -> None:
        verdict = ModerationVerdict(
            violated=True,
            reason="语义存在歧义",
            rule=None,
            conclusive=True,
            confidence=0.7,
        )
        provisional = SimpleNamespace(id=325, notice_sent_at=None)
        final = SimpleNamespace(id=326, notice_sent_at=None)
        moderation = SimpleNamespace(
            is_user_exempt=AsyncMock(return_value=False),
            evaluate=AsyncMock(return_value=verdict),
            is_high_confidence=lambda _verdict: False,
            record_violation=AsyncMock(side_effect=[provisional, final]),
        )
        begin = AsyncMock(return_value=False)
        answer = AsyncMock()

        message = await self._run(
            moderation,
            ready=patch(
                "bot.handlers.group.moderation_challenge_ready",
                return_value=True,
            ),
            begin=patch(
                "bot.handlers.group.begin_moderation_challenge",
                new=begin,
            ),
            answer=patch(
                "bot.handlers.group.answer_with_auto_delete",
                new=answer,
            ),
        )

        self.assertEqual(
            [call.args[4] for call in moderation.record_violation.await_args_list],
            ["challenge", "warn"],
        )
        message._test_session.delete.assert_awaited_once_with(provisional)
        answer.assert_awaited_once()

    async def test_deauthorization_during_verdict_suppresses_terminal_action(self) -> None:
        verdict = ModerationVerdict(
            violated=True,
            reason="明确违规",
            rule=None,
            conclusive=True,
            confidence=0.99,
        )
        moderation = SimpleNamespace(
            is_user_exempt=AsyncMock(return_value=False),
            evaluate=AsyncMock(return_value=verdict),
            is_high_confidence=lambda _verdict: True,
            record_violation=AsyncMock(),
        )
        fresh = AsyncMock(side_effect=[True, False])
        answer = AsyncMock()

        message = await self._run(
            moderation,
            fresh=patch(
                "bot.handlers.group._fresh_group_authorized_for_moderation",
                new=fresh,
            ),
            answer=patch("bot.handlers.group.answer_with_auto_delete", new=answer),
        )

        self.assertEqual(fresh.await_count, 2)
        moderation.record_violation.assert_not_awaited()
        message.delete.assert_not_awaited()
        message.chat.ban.assert_not_awaited()
        answer.assert_not_awaited()

    async def test_high_confidence_uses_existing_rule_action(self) -> None:
        verdict = ModerationVerdict(
            violated=True,
            reason="明确违规",
            rule=None,
            conclusive=True,
            confidence=0.95,
        )
        moderation = SimpleNamespace(
            is_user_exempt=AsyncMock(return_value=False),
            evaluate=AsyncMock(return_value=verdict),
            is_high_confidence=lambda _verdict: True,
            record_violation=AsyncMock(return_value=SimpleNamespace(id=321)),
        )
        begin = AsyncMock(return_value=True)
        answer = AsyncMock()

        message = await self._run(
            moderation,
            begin=patch("bot.handlers.group.begin_moderation_challenge", new=begin),
            answer=patch("bot.handlers.group.answer_with_auto_delete", new=answer),
        )

        message.delete.assert_not_awaited()
        begin.assert_not_awaited()
        moderation.record_violation.assert_awaited_once()
        self.assertEqual(moderation.record_violation.await_args.args[4], "warn")
        answer.assert_awaited_once()
        message._test_session.flush.assert_awaited_once()
        self.assertEqual(message._test_session.commit.await_count, 3)
        self.assertEqual(
            moderation.record_violation.await_args.kwargs["source_message_id"],
            777,
        )
        keyboard = answer.await_args.kwargs["reply_markup"]
        self.assertEqual(
            [button.callback_data for button in keyboard.inline_keyboard[0]],
            ["mact:ban:321", "mact:undo:321", "mact:exempt:321"],
        )

    async def test_warn_retry_reuses_source_event_and_sends_notice_once(self) -> None:
        verdict = ModerationVerdict(
            violated=True,
            reason="明确违规",
            rule=None,
            conclusive=True,
            confidence=0.95,
        )
        violation = SimpleNamespace(id=322, notice_sent_at=None)
        moderation = SimpleNamespace(
            is_user_exempt=AsyncMock(return_value=False),
            evaluate=AsyncMock(return_value=verdict),
            is_high_confidence=lambda _verdict: True,
            record_violation=AsyncMock(return_value=violation),
        )
        answer = AsyncMock()
        message = _message()

        for _ in range(2):
            await self._run(
                moderation,
                message=message,
                answer=patch(
                    "bot.handlers.group.answer_with_auto_delete",
                    new=answer,
                ),
            )

        self.assertEqual(answer.await_count, 1)
        self.assertEqual(moderation.record_violation.await_count, 2)
        for call in moderation.record_violation.await_args_list:
            self.assertEqual(call.kwargs["source_message_id"], 777)
        self.assertIsNotNone(violation.notice_sent_at)

    async def test_delete_action_persists_source_message_id(self) -> None:
        rule = SimpleNamespace(
            id=12,
            action="delete",
            rule_type="llm",
            pattern="禁止广告",
        )
        verdict = ModerationVerdict(
            violated=True,
            reason="广告",
            rule=rule,
            conclusive=True,
            confidence=0.95,
        )
        moderation = SimpleNamespace(
            is_user_exempt=AsyncMock(return_value=False),
            evaluate=AsyncMock(return_value=verdict),
            is_high_confidence=lambda _verdict: True,
            record_violation=AsyncMock(
                return_value=SimpleNamespace(id=323, notice_sent_at=None)
            ),
        )
        answer = AsyncMock()

        message = await self._run(
            moderation,
            answer=patch("bot.handlers.group.answer_with_auto_delete", new=answer),
        )

        self.assertEqual(
            moderation.record_violation.await_args.kwargs["source_message_id"],
            777,
        )
        message.delete.assert_awaited_once()
        answer.assert_awaited_once()

    async def test_sender_chat_ban_retry_is_idempotent(self) -> None:
        rule = SimpleNamespace(
            id=13,
            action="ban",
            rule_type="llm",
            pattern="禁止频道广告",
        )
        verdict = ModerationVerdict(
            violated=True,
            reason="频道广告",
            rule=rule,
            conclusive=True,
            confidence=0.7,
        )
        violation = SimpleNamespace(id=324, notice_sent_at=None)
        moderation = SimpleNamespace(
            is_user_exempt=AsyncMock(return_value=False),
            evaluate=AsyncMock(return_value=verdict),
            is_high_confidence=lambda _verdict: False,
            record_violation=AsyncMock(return_value=violation),
        )
        answer = AsyncMock()
        message = _message()
        message.sender_chat = SimpleNamespace(
            id=-1009876543210,
            username="channel",
            title="Channel",
        )
        message.from_user.is_bot = True
        message.chat.ban_sender_chat = AsyncMock(return_value=True)

        for _ in range(2):
            await self._run(
                moderation,
                message=message,
                answer=patch(
                    "bot.handlers.group.answer_with_auto_delete",
                    new=answer,
                ),
            )

        message.chat.ban_sender_chat.assert_awaited_once_with(-1009876543210)
        self.assertEqual(answer.await_count, 1)
        for call in moderation.record_violation.await_args_list:
            self.assertEqual(call.kwargs["source_message_id"], 777)

    async def test_super_admin_is_automatically_exempt_from_moderation(self) -> None:
        message = _message()
        message.from_user.id = 1
        moderation = SimpleNamespace(
            is_user_exempt=AsyncMock(return_value=False),
            evaluate=AsyncMock(),
        )

        await self._run(moderation, message=message)

        moderation.is_user_exempt.assert_not_awaited()
        moderation.evaluate.assert_not_awaited()

    async def test_human_video_caption_is_moderated_before_media_bypass(self) -> None:
        verdict = ModerationVerdict(
            violated=False,
            reason="",
            rule=None,
            conclusive=True,
            confidence=0.0,
        )
        moderation = SimpleNamespace(
            is_user_exempt=AsyncMock(return_value=False),
            evaluate=AsyncMock(return_value=verdict),
        )

        await self._run(moderation, msg_type="video_caption")

        moderation.is_user_exempt.assert_awaited_once()
        moderation.evaluate.assert_awaited_once()

    async def test_counted_ban_policy_warning_uses_durable_event_state(self) -> None:
        rule = SimpleNamespace(
            id=9,
            action="ban",
            rule_type="llm",
            pattern="禁止广告",
        )
        verdict = ModerationVerdict(
            violated=True,
            reason="广告",
            rule=rule,
            conclusive=True,
            confidence=0.99,
        )
        moderation = SimpleNamespace(
            is_user_exempt=AsyncMock(return_value=False),
            evaluate=AsyncMock(return_value=verdict),
            is_high_confidence=lambda _verdict: True,
            add_warning=AsyncMock(return_value=(2, False)),
            record_violation=AsyncMock(return_value=SimpleNamespace(id=654)),
        )
        answer = AsyncMock()

        message = await self._run(
            moderation,
            answer=patch("bot.handlers.group.answer_with_auto_delete", new=answer),
        )

        self.assertEqual(moderation.record_violation.await_args.args[4], "ban_warning")
        self.assertEqual(
            moderation.record_violation.await_args.kwargs["source_message_id"],
            777,
        )
        message.chat.ban.assert_not_awaited()
        keyboard = answer.await_args.kwargs["reply_markup"]
        self.assertEqual(keyboard.inline_keyboard[0][1].callback_data, "mact:undo:654")

    async def test_auto_ban_uses_applied_event_state(self) -> None:
        rule = SimpleNamespace(
            id=10,
            action="ban",
            rule_type="llm",
            pattern="禁止广告",
        )
        verdict = ModerationVerdict(
            violated=True,
            reason="广告",
            rule=rule,
            conclusive=True,
            confidence=0.99,
        )
        violation_event = SimpleNamespace(id=655)
        moderation = SimpleNamespace(
            is_user_exempt=AsyncMock(return_value=False),
            evaluate=AsyncMock(return_value=verdict),
            is_high_confidence=lambda _verdict: True,
            add_warning=AsyncMock(return_value=(3, True)),
            record_violation=AsyncMock(return_value=violation_event),
        )
        answer = AsyncMock()

        message = await self._run(
            moderation,
            answer=patch("bot.handlers.group.answer_with_auto_delete", new=answer),
        )

        self.assertEqual(moderation.record_violation.await_args.args[4], "ban_warning")
        self.assertEqual(violation_event.action_taken, "ban_applied")
        message.bot.ban_chat_member.assert_awaited_once_with(
            -10001,
            42,
            revoke_messages=True,
        )
        self.assertEqual(
            answer.await_args.kwargs["reply_markup"].inline_keyboard[0][0].callback_data,
            "mact:ban:655",
        )

    async def test_ambiguous_auto_ban_retains_durable_policy_for_retry(self) -> None:
        rule = SimpleNamespace(
            id=11,
            action="ban",
            rule_type="llm",
            pattern="禁止广告",
        )
        verdict = ModerationVerdict(
            violated=True,
            reason="广告",
            rule=rule,
            conclusive=True,
            confidence=0.99,
        )
        moderation = SimpleNamespace(
            is_user_exempt=AsyncMock(return_value=False),
            evaluate=AsyncMock(return_value=verdict),
            is_high_confidence=lambda _verdict: True,
            add_warning=AsyncMock(return_value=(3, True)),
            record_violation=AsyncMock(return_value=SimpleNamespace(id=656)),
        )
        answer = AsyncMock()

        failed_message = _message()
        failed_message.bot.ban_chat_member.return_value = False
        receipt = UpdateCompletionReceipt()
        token = bind_update_completion(receipt)
        try:
            message = await self._run(
                moderation,
                message=failed_message,
                answer=patch(
                    "bot.handlers.group.answer_with_auto_delete",
                    new=answer,
                ),
            )
        finally:
            reset_update_completion(token)

        self.assertEqual(message._test_session.execute.await_count, 0)
        self.assertEqual(message._test_session.commit.await_count, 4)
        message.bot.ban_chat_member.assert_awaited_once_with(
            -10001,
            42,
            revoke_messages=True,
        )
        self.assertIn("封禁结果未确认", answer.await_args.args[1])
        self.assertTrue(receipt.deferred)
        self.assertFalse(await receipt.wait())

    async def test_invalid_confidence_never_falls_back_to_direct_action(self) -> None:
        verdict = ModerationVerdict(
            violated=True,
            reason="格式不完整",
            rule=None,
            conclusive=False,
            confidence=0.0,
        )
        moderation = SimpleNamespace(
            is_user_exempt=AsyncMock(return_value=False),
            evaluate=AsyncMock(return_value=verdict),
            is_high_confidence=lambda _verdict: False,
            record_violation=AsyncMock(),
        )
        begin = AsyncMock(return_value=False)

        message = await self._run(
            moderation,
            ready=patch("bot.handlers.group.moderation_challenge_ready", return_value=False),
            begin=patch("bot.handlers.group.begin_moderation_challenge", new=begin),
        )

        message.delete.assert_not_awaited()
        begin.assert_not_awaited()
        moderation.record_violation.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
