import unittest
from contextlib import ExitStack
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from bot.handlers import group
from bot.services.moderation import ModerationVerdict


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
            auto_delete_minutes=0,
        ),
        moderation=SimpleNamespace(enabled=True, warn_threshold=3),
        skill_sticker_file_ids="",
    )


def _message() -> SimpleNamespace:
    return SimpleNamespace(
        chat=SimpleNamespace(id=-10001, type="supergroup", title="test"),
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
            )
        ),
    )


class GroupModerationConfidenceTests(unittest.IsolatedAsyncioTestCase):
    async def _run(self, moderation_service: SimpleNamespace, **extra_patches):
        message = _message()
        group_row = SimpleNamespace(settings={"mute_all_replies": True})
        patches = [
            patch(
                "bot.handlers.group.ensure_group_authorized",
                new=AsyncMock(return_value=True),
            ),
            patch(
                "bot.handlers.group._ensure_group_row",
                new=AsyncMock(return_value=group_row),
            ),
            patch(
                "bot.handlers.group.record_group_activity",
                return_value=group_row.settings,
            ),
            patch(
                "bot.handlers.group.extract_message_text",
                return_value=("ambiguous message", "text"),
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
                session=object(),
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
            record_violation=AsyncMock(),
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
            record_violation=AsyncMock(),
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
