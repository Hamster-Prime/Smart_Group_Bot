import asyncio
import os
import tempfile
import unittest
from contextlib import ExitStack, nullcontext
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from bot.db.engine import init_db
from bot.handlers import group
from bot.services.bot_screening import (
    is_bot_whitelisted,
    record_bot_screening_pass,
    remove_bot_whitelist,
    reset_bot_screening,
)
from bot.services.moderation import ModerationVerdict

GROUP_ID = -10001
BOT_ID = 777000123


class BotScreeningServiceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        fd, self._db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.engine, self.session_factory = await init_db(f"sqlite+aiosqlite:///{self._db_path}")

    async def asyncTearDown(self) -> None:
        await self.engine.dispose()
        for suffix in ("", "-wal", "-shm"):
            try:
                os.remove(f"{self._db_path}{suffix}")
            except OSError:
                pass

    async def test_passes_accumulate_and_whitelist_at_threshold(self) -> None:
        async with self.session_factory() as session:
            for expected in (1, 2):
                count, whitelisted = await record_bot_screening_pass(
                    session, GROUP_ID, BOT_ID, threshold=3
                )
                await session.commit()
                self.assertEqual(count, expected)
                self.assertFalse(whitelisted)
                self.assertFalse(await is_bot_whitelisted(session, GROUP_ID, BOT_ID))

            count, whitelisted = await record_bot_screening_pass(
                session, GROUP_ID, BOT_ID, threshold=3
            )
            await session.commit()
            self.assertEqual(count, 3)
            self.assertTrue(whitelisted)
            self.assertTrue(await is_bot_whitelisted(session, GROUP_ID, BOT_ID))

    async def test_threshold_one_whitelists_first_message(self) -> None:
        async with self.session_factory() as session:
            count, whitelisted = await record_bot_screening_pass(
                session, GROUP_ID, BOT_ID, threshold=1
            )
            await session.commit()
            self.assertEqual(count, 1)
            self.assertTrue(whitelisted)

    async def test_reset_clears_count_but_not_whitelist(self) -> None:
        async with self.session_factory() as session:
            await record_bot_screening_pass(session, GROUP_ID, BOT_ID, threshold=3)
            await record_bot_screening_pass(session, GROUP_ID, BOT_ID, threshold=3)
            await reset_bot_screening(session, GROUP_ID, BOT_ID)
            await session.commit()

            count, whitelisted = await record_bot_screening_pass(
                session, GROUP_ID, BOT_ID, threshold=3
            )
            await session.commit()
            self.assertEqual(count, 1)
            self.assertFalse(whitelisted)

            # A whitelisted bot is unaffected by reset.
            await record_bot_screening_pass(session, GROUP_ID, BOT_ID, threshold=3)
            await record_bot_screening_pass(session, GROUP_ID, BOT_ID, threshold=3)
            await reset_bot_screening(session, GROUP_ID, BOT_ID)
            await session.commit()
            self.assertTrue(await is_bot_whitelisted(session, GROUP_ID, BOT_ID))

    async def test_whitelist_is_per_group(self) -> None:
        async with self.session_factory() as session:
            await record_bot_screening_pass(session, GROUP_ID, BOT_ID, threshold=1)
            await session.commit()
            self.assertTrue(await is_bot_whitelisted(session, GROUP_ID, BOT_ID))
            self.assertFalse(await is_bot_whitelisted(session, GROUP_ID - 1, BOT_ID))

    async def test_remove_bot_whitelist(self) -> None:
        async with self.session_factory() as session:
            await record_bot_screening_pass(session, GROUP_ID, BOT_ID, threshold=1)
            await session.commit()

            removed = await remove_bot_whitelist(session, GROUP_ID, BOT_ID)
            await session.commit()
            self.assertTrue(removed)
            self.assertFalse(await is_bot_whitelisted(session, GROUP_ID, BOT_ID))

            self.assertFalse(await remove_bot_whitelist(session, GROUP_ID, 42))

    async def test_concurrent_first_pass_is_consistent(self) -> None:
        concurrent_factory = async_sessionmaker(
            self.engine,
            class_=AsyncSession,
            expire_on_commit=False,
            autoflush=False,
        )

        async def record() -> tuple[int, bool]:
            async with concurrent_factory() as session:
                result = await record_bot_screening_pass(
                    session, GROUP_ID, BOT_ID, threshold=3
                )
                await session.commit()
                return result

        results = await asyncio.gather(record(), record())
        self.assertEqual(sorted(count for count, _ in results), [1, 2])


def _settings(**moderation_overrides) -> SimpleNamespace:
    moderation = {
        "enabled": True,
        "warn_threshold": 3,
        "bot_screening_enabled": True,
        "bot_screening_message_count": 3,
        **moderation_overrides,
    }
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
        moderation=SimpleNamespace(**moderation),
        skill_sticker_file_ids="",
    )


def _bot_message() -> SimpleNamespace:
    return SimpleNamespace(
        chat=SimpleNamespace(
            id=GROUP_ID,
            type="supergroup",
            title="test",
            ban=AsyncMock(),
        ),
        from_user=SimpleNamespace(
            id=BOT_ID,
            is_bot=True,
            username="guestbot",
            full_name="Guest Bot",
        ),
        sender_chat=None,
        text="spam text",
        delete=AsyncMock(),
        bot=SimpleNamespace(
            me=AsyncMock(return_value=SimpleNamespace(username="selfbot", id=1)),
        ),
    )


def _rules_lookup_session(has_rules: bool = True) -> SimpleNamespace:
    rules_row = 9 if has_rules else None
    return SimpleNamespace(
        flush=AsyncMock(),
        commit=AsyncMock(),
        rollback=AsyncMock(),
        execute=AsyncMock(
            return_value=SimpleNamespace(scalar_one_or_none=lambda: rules_row)
        ),
        no_autoflush=nullcontext(),
    )


class BotSenderModerationHandlerTests(unittest.IsolatedAsyncioTestCase):
    async def _run(
        self,
        *,
        message: SimpleNamespace | None = None,
        session: SimpleNamespace | None = None,
        settings: SimpleNamespace | None = None,
        moderation_service: SimpleNamespace | None = None,
        whitelisted: bool = False,
        record_pass: AsyncMock | None = None,
        reset: AsyncMock | None = None,
        msg_type: str = "text",
    ) -> SimpleNamespace:
        message = message or _bot_message()
        session = session if session is not None else _rules_lookup_session()
        settings = settings or _settings()
        moderation_service = moderation_service or SimpleNamespace(
            is_user_exempt=AsyncMock(return_value=False),
            evaluate=AsyncMock(),
        )
        message._test_session = session
        message._test_moderation = moderation_service
        message._test_record_pass = record_pass or AsyncMock(return_value=(1, False))
        message._test_reset = reset or AsyncMock()
        group_row = SimpleNamespace(settings={})
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
                return_value=("spam text", msg_type),
            ),
            patch(
                "bot.handlers.group._append_image_context",
                new=AsyncMock(return_value=("spam text", "")),
            ),
            patch("bot.handlers.group._best_effort_commit", new=AsyncMock()),
            patch("bot.handlers.group.LLMService", return_value=object()),
            patch(
                "bot.handlers.group.ModerationService",
                return_value=moderation_service,
            ),
            patch(
                "bot.handlers.group.is_bot_whitelisted",
                new=AsyncMock(return_value=whitelisted),
            ),
            patch(
                "bot.handlers.group.record_bot_screening_pass",
                new=message._test_record_pass,
            ),
            patch(
                "bot.handlers.group.reset_bot_screening",
                new=message._test_reset,
            ),
        ]
        with ExitStack() as stack:
            for patcher in patches:
                stack.enter_context(patcher)
            await group.on_group_message(message, session=session, settings=settings)
        return message

    async def test_clean_message_records_screening_pass(self) -> None:
        verdict = ModerationVerdict(
            violated=False, reason="", rule=None, conclusive=True, confidence=0.9
        )
        moderation = SimpleNamespace(
            is_user_exempt=AsyncMock(return_value=False),
            evaluate=AsyncMock(return_value=verdict),
        )

        message = await self._run(moderation_service=moderation)

        moderation.evaluate.assert_awaited_once()
        message._test_record_pass.assert_awaited_once()
        args = message._test_record_pass.await_args.args
        self.assertEqual(args[1:], (GROUP_ID, BOT_ID, 3))
        message._test_session.commit.assert_awaited()
        message.delete.assert_not_awaited()
        message._test_reset.assert_not_awaited()

    async def test_whitelisted_bot_skips_moderation(self) -> None:
        moderation = SimpleNamespace(
            is_user_exempt=AsyncMock(return_value=False),
            evaluate=AsyncMock(),
        )

        message = await self._run(moderation_service=moderation, whitelisted=True)

        moderation.evaluate.assert_not_awaited()
        message._test_record_pass.assert_not_awaited()

    async def test_bot_screening_disabled_ignores_bot_message(self) -> None:
        moderation = SimpleNamespace(
            is_user_exempt=AsyncMock(return_value=False),
            evaluate=AsyncMock(),
        )

        message = await self._run(
            settings=_settings(bot_screening_enabled=False),
            moderation_service=moderation,
        )

        moderation.evaluate.assert_not_awaited()
        message._test_record_pass.assert_not_awaited()
        message.delete.assert_not_awaited()

    async def test_violation_deletes_resets_counter_and_counts_warning(self) -> None:
        rule = SimpleNamespace(id=5, action="delete", rule_type="llm", pattern="禁止广告")
        verdict = ModerationVerdict(
            violated=True, reason="广告", rule=rule, conclusive=True, confidence=0.95
        )
        moderation = SimpleNamespace(
            is_user_exempt=AsyncMock(return_value=False),
            evaluate=AsyncMock(return_value=verdict),
            is_high_confidence=lambda _verdict: True,
            add_warning=AsyncMock(return_value=(1, False)),
            record_violation=AsyncMock(return_value=SimpleNamespace(id=501)),
        )
        answer = AsyncMock()

        with patch("bot.handlers.group.answer_with_auto_delete", new=answer):
            message = await self._run(moderation_service=moderation)

        message.delete.assert_awaited_once()
        message._test_reset.assert_awaited_once()
        message._test_record_pass.assert_not_awaited()
        message.chat.ban.assert_not_awaited()
        moderation.add_warning.assert_awaited_once()
        self.assertEqual(moderation.record_violation.await_args.args[4], "ban_warning")
        answer.assert_awaited_once()
        keyboard = answer.await_args.kwargs["reply_markup"]
        self.assertEqual(keyboard.inline_keyboard[0][1].callback_data, "mact:undo:501")

    async def test_repeat_violations_escalate_to_ban(self) -> None:
        rule = SimpleNamespace(id=5, action="delete", rule_type="llm", pattern="禁止广告")
        verdict = ModerationVerdict(
            violated=True, reason="广告", rule=rule, conclusive=True, confidence=0.95
        )
        moderation = SimpleNamespace(
            is_user_exempt=AsyncMock(return_value=False),
            evaluate=AsyncMock(return_value=verdict),
            is_high_confidence=lambda _verdict: True,
            add_warning=AsyncMock(return_value=(3, True)),
            record_violation=AsyncMock(return_value=SimpleNamespace(id=502)),
        )
        answer = AsyncMock()

        with patch("bot.handlers.group.answer_with_auto_delete", new=answer):
            message = await self._run(moderation_service=moderation)

        message.chat.ban.assert_awaited_once_with(BOT_ID)
        self.assertEqual(moderation.record_violation.await_args.args[4], "ban_applied")

    async def test_high_confidence_ban_rule_bans_bot(self) -> None:
        rule = SimpleNamespace(id=6, action="ban", rule_type="llm", pattern="禁止广告")
        verdict = ModerationVerdict(
            violated=True, reason="广告", rule=rule, conclusive=True, confidence=0.99
        )
        moderation = SimpleNamespace(
            is_user_exempt=AsyncMock(return_value=False),
            evaluate=AsyncMock(return_value=verdict),
            is_high_confidence=lambda _verdict: True,
            record_violation=AsyncMock(),
        )
        answer = AsyncMock()

        with patch("bot.handlers.group.answer_with_auto_delete", new=answer):
            message = await self._run(moderation_service=moderation)

        message.delete.assert_awaited_once()
        message.chat.ban.assert_awaited_once_with(BOT_ID)
        self.assertEqual(moderation.record_violation.await_args.args[4], "bot_ban")

    async def test_low_confidence_ban_rule_takes_counted_path(self) -> None:
        rule = SimpleNamespace(id=7, action="ban", rule_type="llm", pattern="禁止广告")
        verdict = ModerationVerdict(
            violated=True, reason="疑似广告", rule=rule, conclusive=True, confidence=0.7
        )
        moderation = SimpleNamespace(
            is_user_exempt=AsyncMock(return_value=False),
            evaluate=AsyncMock(return_value=verdict),
            is_high_confidence=lambda _verdict: False,
            add_warning=AsyncMock(return_value=(1, False)),
            record_violation=AsyncMock(return_value=SimpleNamespace(id=503)),
        )
        answer = AsyncMock()

        with patch("bot.handlers.group.answer_with_auto_delete", new=answer):
            message = await self._run(moderation_service=moderation)

        message.delete.assert_awaited_once()
        message.chat.ban.assert_not_awaited()
        message._test_reset.assert_awaited_once()
        self.assertEqual(moderation.record_violation.await_args.args[4], "ban_warning")

    async def test_inconclusive_verdict_takes_no_action(self) -> None:
        verdict = ModerationVerdict(
            violated=True, reason="格式不完整", rule=None, conclusive=False, confidence=0.0
        )
        moderation = SimpleNamespace(
            is_user_exempt=AsyncMock(return_value=False),
            evaluate=AsyncMock(return_value=verdict),
            record_violation=AsyncMock(),
        )

        message = await self._run(moderation_service=moderation)

        message.delete.assert_not_awaited()
        message._test_reset.assert_not_awaited()
        message._test_record_pass.assert_not_awaited()
        moderation.record_violation.assert_not_awaited()

    async def test_inconclusive_clean_verdict_does_not_count_as_pass(self) -> None:
        verdict = ModerationVerdict(
            violated=False, reason="", rule=None, conclusive=False, confidence=0.0
        )
        moderation = SimpleNamespace(
            is_user_exempt=AsyncMock(return_value=False),
            evaluate=AsyncMock(return_value=verdict),
        )

        message = await self._run(moderation_service=moderation)

        message._test_record_pass.assert_not_awaited()

    async def test_no_enabled_rules_freezes_screening(self) -> None:
        moderation = SimpleNamespace(
            is_user_exempt=AsyncMock(return_value=False),
            evaluate=AsyncMock(),
        )

        message = await self._run(
            session=_rules_lookup_session(has_rules=False),
            moderation_service=moderation,
        )

        moderation.evaluate.assert_not_awaited()
        message._test_record_pass.assert_not_awaited()

    async def test_manual_exempt_bot_skips_screening(self) -> None:
        moderation = SimpleNamespace(
            is_user_exempt=AsyncMock(return_value=True),
            evaluate=AsyncMock(),
        )

        message = await self._run(moderation_service=moderation)

        moderation.evaluate.assert_not_awaited()
        message._test_record_pass.assert_not_awaited()

    async def test_moderation_disabled_ignores_bot_message(self) -> None:
        moderation = SimpleNamespace(
            is_user_exempt=AsyncMock(return_value=False),
            evaluate=AsyncMock(),
        )

        message = await self._run(
            settings=_settings(enabled=False),
            moderation_service=moderation,
        )

        moderation.evaluate.assert_not_awaited()
        message._test_record_pass.assert_not_awaited()

    async def test_bot_video_caption_still_screened(self) -> None:
        verdict = ModerationVerdict(
            violated=False, reason="", rule=None, conclusive=True, confidence=0.9
        )
        moderation = SimpleNamespace(
            is_user_exempt=AsyncMock(return_value=False),
            evaluate=AsyncMock(return_value=verdict),
        )

        message = await self._run(
            moderation_service=moderation, msg_type="video_caption"
        )

        moderation.evaluate.assert_awaited_once()
        message._test_record_pass.assert_awaited_once()

    async def test_placeholder_media_never_credits_whitelist_pass(self) -> None:
        # "[voice]"/"[video]" placeholders carry no screenable content; a
        # trivially-clean verdict must not let an ad bot farm the whitelist.
        for placeholder_type in ("voice", "video", "video_note", "audio", "sticker"):
            with self.subTest(msg_type=placeholder_type):
                moderation = SimpleNamespace(
                    is_user_exempt=AsyncMock(return_value=False),
                    evaluate=AsyncMock(),
                )

                message = await self._run(
                    moderation_service=moderation, msg_type=placeholder_type
                )

                moderation.evaluate.assert_not_awaited()
                message._test_record_pass.assert_not_awaited()

    async def test_media_with_vision_text_is_screened(self) -> None:
        verdict = ModerationVerdict(
            violated=False, reason="", rule=None, conclusive=True, confidence=0.9
        )
        moderation = SimpleNamespace(
            is_user_exempt=AsyncMock(return_value=False),
            evaluate=AsyncMock(return_value=verdict),
        )
        message = _bot_message()
        session = _rules_lookup_session()
        settings = _settings()
        record_pass = AsyncMock(return_value=(1, False))
        group_row = SimpleNamespace(settings={})
        with ExitStack() as stack:
            for patcher in [
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
                    return_value=("[image]", "photo"),
                ),
                patch(
                    "bot.handlers.group._append_image_context",
                    new=AsyncMock(
                        return_value=("[image]\n识别出的广告文本", "识别出的广告文本")
                    ),
                ),
                patch("bot.handlers.group._best_effort_commit", new=AsyncMock()),
                patch("bot.handlers.group.LLMService", return_value=object()),
                patch(
                    "bot.handlers.group.ModerationService",
                    return_value=moderation,
                ),
                patch(
                    "bot.handlers.group.is_bot_whitelisted",
                    new=AsyncMock(return_value=False),
                ),
                patch(
                    "bot.handlers.group.record_bot_screening_pass",
                    new=record_pass,
                ),
                patch("bot.handlers.group.reset_bot_screening", new=AsyncMock()),
            ]:
                stack.enter_context(patcher)
            await group.on_group_message(message, session=session, settings=settings)

        moderation.evaluate.assert_awaited_once()
        record_pass.assert_awaited_once()


class UnaiexemptBotWhitelistTests(unittest.IsolatedAsyncioTestCase):
    async def test_unaiexempt_revokes_bot_whitelist(self) -> None:
        from bot.handlers import admin

        message = SimpleNamespace(
            chat=SimpleNamespace(id=GROUP_ID, type="supergroup"),
            from_user=SimpleNamespace(id=1, is_bot=False),
            reply_to_message=SimpleNamespace(
                from_user=SimpleNamespace(
                    id=BOT_ID, is_bot=True, full_name="Guest Bot"
                ),
            ),
            text="/unaiexempt",
        )
        session = SimpleNamespace(
            execute=AsyncMock(
                return_value=SimpleNamespace(scalar_one_or_none=lambda: None)
            ),
            delete=AsyncMock(),
            commit=AsyncMock(),
        )
        settings = SimpleNamespace(bot=SimpleNamespace(auto_delete_seconds=0))
        answer = AsyncMock()
        revoke = AsyncMock(return_value=True)

        with (
            patch(
                "bot.handlers.admin.ensure_group_authorized",
                new=AsyncMock(return_value=True),
            ),
            patch(
                "bot.handlers.admin.ensure_group_admin_permission",
                new=AsyncMock(return_value=True),
            ),
            patch("bot.handlers.admin.remove_bot_whitelist", new=revoke),
            patch("bot.handlers.admin._answer", new=answer),
        ):
            await admin.cmd_unaiexempt(message, session=session, settings=settings)

        revoke.assert_awaited_once_with(session, GROUP_ID, BOT_ID)
        answer.assert_awaited_once()
        self.assertIn("已撤销 bot 审核白名单", answer.await_args.args[2])

    async def test_unaiexempt_bot_without_records_reports_absent(self) -> None:
        from bot.handlers import admin

        message = SimpleNamespace(
            chat=SimpleNamespace(id=GROUP_ID, type="supergroup"),
            from_user=SimpleNamespace(id=1, is_bot=False),
            reply_to_message=SimpleNamespace(
                from_user=SimpleNamespace(
                    id=BOT_ID, is_bot=True, full_name="Guest Bot"
                ),
            ),
            text="/unaiexempt",
        )
        session = SimpleNamespace(
            execute=AsyncMock(
                return_value=SimpleNamespace(scalar_one_or_none=lambda: None)
            ),
            delete=AsyncMock(),
            commit=AsyncMock(),
        )
        settings = SimpleNamespace(bot=SimpleNamespace(auto_delete_seconds=0))
        answer = AsyncMock()

        with (
            patch(
                "bot.handlers.admin.ensure_group_authorized",
                new=AsyncMock(return_value=True),
            ),
            patch(
                "bot.handlers.admin.ensure_group_admin_permission",
                new=AsyncMock(return_value=True),
            ),
            patch(
                "bot.handlers.admin.remove_bot_whitelist",
                new=AsyncMock(return_value=False),
            ),
            patch("bot.handlers.admin._answer", new=answer),
        ):
            await admin.cmd_unaiexempt(message, session=session, settings=settings)

        self.assertIn("当前不在豁免名单", answer.await_args.args[2])


if __name__ == "__main__":
    unittest.main()
