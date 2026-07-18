import asyncio
import os
import tempfile
import unittest
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from sqlalchemy import select

from bot.db.engine import init_db
from bot.db.models import (
    BanAuditEvent,
    Group,
    UserWarning,
    VoteBanQuotaBucket,
    VoteBanSession,
    VoteBanVote,
)
from bot.handlers import commands, group
from bot.services import vote_ban
from bot.services.authz import authorize_group
from bot.services.vote_ban import (
    apply_vote_ban,
    claim_session_status,
    count_approvals,
    open_vote_session,
    recover_stale_vote_enforcement,
    record_vote_ban_outcome,
    record_vote,
    resolve_vote_ban_config,
)
from bot.utils.timezone import now_shanghai_naive


def _settings(**overrides) -> SimpleNamespace:
    values = {
        "super_admin_id": 1,
        "vote_ban_enabled": True,
        "vote_ban_threshold": 3,
        "vote_ban_duration_seconds": 600,
        "vote_ban_trigger_limit": 3,
        "vote_ban_trigger_window_seconds": 3600,
        "bot": SimpleNamespace(
            auto_delete_categories=[],
            auto_delete_seconds=0,
            auto_delete_category_seconds={},
            auto_delete_category_mode={},
        ),
    }
    values.update(overrides)
    return SimpleNamespace(**values)


class VoteBanConfigTests(unittest.TestCase):
    def test_group_overrides_and_clamping(self) -> None:
        settings = _settings()
        config = resolve_vote_ban_config(settings, None)
        self.assertTrue(config.enabled)
        self.assertEqual(config.threshold, 3)
        self.assertEqual(config.duration_seconds, 600)
        self.assertEqual(config.trigger_limit, 3)
        self.assertEqual(config.trigger_window_seconds, 3600)

        config = resolve_vote_ban_config(
            settings,
            {
                "vote_ban_enabled": False,
                "vote_ban_threshold": 10,
                "vote_ban_duration_seconds": 120,
                "vote_ban_trigger_limit": 2,
                "vote_ban_trigger_window_seconds": 300,
            },
        )
        self.assertFalse(config.enabled)
        self.assertEqual(config.threshold, 10)
        self.assertEqual(config.duration_seconds, 120)
        self.assertEqual(config.trigger_limit, 2)
        self.assertEqual(config.trigger_window_seconds, 300)

        settings.vote_ban_threshold = 1
        config = resolve_vote_ban_config(settings, {})
        self.assertEqual(config.threshold, 2)


class VoteBanFlowTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        fd, self._db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.engine, self.session_factory = await init_db(
            f"sqlite+aiosqlite:///{self._db_path}"
        )
        async with self.session_factory() as session:
            session.add(Group(id=-100, title="test", settings={}))
            await authorize_group(session, -100, 1)
            await session.commit()
        self.settings = _settings()

    async def asyncTearDown(self) -> None:
        await self.engine.dispose()
        for suffix in ("", "-wal", "-shm"):
            try:
                os.remove(f"{self._db_path}{suffix}")
            except OSError:
                pass

    async def _open_session(self, threshold: int = 3) -> int:
        config = resolve_vote_ban_config(
            _settings(vote_ban_threshold=threshold), {}
        )
        async with self.session_factory() as session:
            record = await open_vote_session(
                session,
                group_id=-100,
                target_user_id=555,
                target_display="骚扰者",
                target_username="",
                starter_user_id=10,
                starter_display="发起人",
                reason="骚扰消息",
                config=config,
            )
            record.message_id = 777
            await session.commit()
            return int(record.id)

    def _callback(
        self,
        session_id: int,
        *,
        voter_id: int,
        bot: SimpleNamespace | None = None,
    ) -> SimpleNamespace:
        return SimpleNamespace(
            data=f"vban:vote:{session_id}",
            message=SimpleNamespace(
                chat=SimpleNamespace(id=-100, type="supergroup"),
                message_id=777,
                reply_markup=None,
            ),
            from_user=SimpleNamespace(id=voter_id),
            bot=bot
            or SimpleNamespace(
                ban_chat_member=AsyncMock(return_value=True),
                edit_message_text=AsyncMock(),
                get_chat_member=AsyncMock(
                    return_value=SimpleNamespace(status="member")
                ),
            ),
            answer=AsyncMock(),
        )

    async def test_starter_vote_counts_and_duplicate_rejected(self) -> None:
        session_id = await self._open_session()
        async with self.session_factory() as session:
            self.assertEqual(await count_approvals(session, session_id), 1)
            self.assertFalse(await record_vote(session, session_id, 10))
            self.assertTrue(await record_vote(session, session_id, 11))
            await session.commit()
            self.assertEqual(await count_approvals(session, session_id), 2)

    async def test_one_active_session_per_target(self) -> None:
        await self._open_session()
        config = resolve_vote_ban_config(self.settings, {})
        async with self.session_factory() as session:
            duplicate = await open_vote_session(
                session,
                group_id=-100,
                target_user_id=555,
                target_display="骚扰者",
                target_username="",
                starter_user_id=99,
                starter_display="其他人",
                reason="",
                config=config,
            )
        self.assertIsNone(duplicate)

    async def test_vote_below_threshold_updates_message(self) -> None:
        session_id = await self._open_session(threshold=3)
        callback = self._callback(session_id, voter_id=11)
        async with self.session_factory() as session:
            await group.on_vote_ban_action(callback, self.settings, session=session)
        callback.bot.ban_chat_member.assert_not_awaited()
        callback.bot.edit_message_text.assert_awaited()
        kwargs = callback.bot.edit_message_text.await_args.kwargs
        self.assertIn("2/3", kwargs["text"])
        self.assertIsNotNone(kwargs["reply_markup"])
        async with self.session_factory() as session:
            record = await session.get(VoteBanSession, session_id)
            self.assertEqual(record.status, "active")

    async def test_threshold_reached_bans_and_finalizes(self) -> None:
        session_id = await self._open_session(threshold=2)
        callback = self._callback(session_id, voter_id=11)
        async with self.session_factory() as session:
            await group.on_vote_ban_action(callback, self.settings, session=session)
        callback.bot.ban_chat_member.assert_awaited_once_with(-100, 555)
        kwargs = callback.bot.edit_message_text.await_args.kwargs
        self.assertIn("已封禁", kwargs["text"])
        self.assertIsNone(kwargs["reply_markup"])
        async with self.session_factory() as session:
            record = await session.get(VoteBanSession, session_id)
            self.assertEqual(record.status, "passed")
            warning = await session.scalar(
                select(UserWarning).where(
                    UserWarning.group_id == -100, UserWarning.user_id == 555
                )
            )
            self.assertTrue(warning.is_banned)
            event = await session.scalar(
                select(BanAuditEvent).where(
                    BanAuditEvent.reference_type == "vote_session",
                    BanAuditEvent.reference_id == session_id,
                )
            )
            self.assertEqual(event.outcome, "succeeded")
            self.assertEqual(event.target_user_id, 555)

    async def test_self_vote_rejected(self) -> None:
        session_id = await self._open_session()
        callback = self._callback(session_id, voter_id=555)
        async with self.session_factory() as session:
            await group.on_vote_ban_action(callback, self.settings, session=session)
        callback.answer.assert_awaited()
        self.assertIn("自己", callback.answer.await_args.args[0])
        async with self.session_factory() as session:
            self.assertEqual(await count_approvals(session, session_id), 1)

    async def test_non_member_vote_rejected(self) -> None:
        session_id = await self._open_session()
        callback = self._callback(session_id, voter_id=11)
        callback.bot.get_chat_member = AsyncMock(
            return_value=SimpleNamespace(status="left")
        )
        async with self.session_factory() as session:
            await group.on_vote_ban_action(callback, self.settings, session=session)
        self.assertIn("成员", callback.answer.await_args.args[0])
        async with self.session_factory() as session:
            self.assertEqual(await count_approvals(session, session_id), 1)

    async def test_overdue_session_lazily_expires(self) -> None:
        session_id = await self._open_session()
        async with self.session_factory() as session:
            record = await session.get(VoteBanSession, session_id)
            record.deadline_at = now_shanghai_naive() - timedelta(seconds=5)
            await session.commit()
        callback = self._callback(session_id, voter_id=11)
        async with self.session_factory() as session:
            await group.on_vote_ban_action(callback, self.settings, session=session)
        callback.bot.ban_chat_member.assert_not_awaited()
        self.assertIn("超时", callback.answer.await_args.args[0])
        async with self.session_factory() as session:
            record = await session.get(VoteBanSession, session_id)
            self.assertEqual(record.status, "expired")
        kwargs = callback.bot.edit_message_text.await_args.kwargs
        self.assertIn("超时", kwargs["text"])
        self.assertIsNone(kwargs["reply_markup"])

    async def test_ban_failure_rolls_back_warning_flag(self) -> None:
        async with self.session_factory() as session:
            bot = SimpleNamespace(
                ban_chat_member=AsyncMock(side_effect=RuntimeError("api down"))
            )
            banned = await apply_vote_ban(
                bot, session, group_id=-100, target_user_id=555
            )
        self.assertFalse(banned)
        async with self.session_factory() as session:
            warning = await session.scalar(
                select(UserWarning).where(
                    UserWarning.group_id == -100, UserWarning.user_id == 555
                )
            )
            self.assertFalse(bool(warning.is_banned) if warning else False)

    async def test_telegram_success_is_not_published_before_final_outcome(self) -> None:
        async with self.session_factory() as session:
            bot = SimpleNamespace(
                ban_chat_member=AsyncMock(return_value=True)
            )
            banned = await apply_vote_ban(
                bot, session, group_id=-100, target_user_id=555
            )
        self.assertTrue(banned)
        async with self.session_factory() as session:
            warning = await session.scalar(
                select(UserWarning).where(
                    UserWarning.group_id == -100,
                    UserWarning.user_id == 555,
                )
            )
            self.assertIsNone(warning)

    async def test_stale_enforcing_session_is_recovered_idempotently(self) -> None:
        session_id = await self._open_session(threshold=2)
        async with self.session_factory() as session:
            self.assertTrue(await record_vote(session, session_id, 11))
            self.assertTrue(
                await claim_session_status(
                    session,
                    session_id,
                    expected="active",
                    new_status="enforcing",
                )
            )
            record = await session.get(VoteBanSession, session_id)
            record.enforcing_started_at = now_shanghai_naive() - timedelta(minutes=5)
            await session.commit()

        bot = SimpleNamespace(
            ban_chat_member=AsyncMock(return_value=True),
            edit_message_text=AsyncMock(),
        )
        async with self.session_factory() as session:
            record = await session.get(VoteBanSession, session_id)
            status = await recover_stale_vote_enforcement(
                bot=bot,
                session=session,
                settings=self.settings,
                record=record,
            )
        self.assertEqual(status, "passed")
        bot.ban_chat_member.assert_awaited_once_with(-100, 555)
        async with self.session_factory() as session:
            record = await session.get(VoteBanSession, session_id)
            warning = await session.scalar(
                select(UserWarning).where(
                    UserWarning.group_id == -100,
                    UserWarning.user_id == 555,
                )
            )
            event = await session.scalar(
                select(BanAuditEvent).where(
                    BanAuditEvent.reference_type == "vote_session",
                    BanAuditEvent.reference_id == session_id,
                )
            )
            self.assertEqual(record.status, "passed")
            self.assertIsNone(record.enforcing_started_at)
            self.assertTrue(warning.is_banned)
            self.assertEqual(event.outcome, "succeeded")

    async def test_old_enforcement_lease_cannot_overwrite_new_owner(self) -> None:
        session_id = await self._open_session(threshold=2)
        async with self.session_factory() as session:
            self.assertTrue(await record_vote(session, session_id, 11))
            self.assertTrue(
                await claim_session_status(
                    session,
                    session_id,
                    expected="active",
                    new_status="enforcing",
                )
            )
            await session.commit()
            record = await session.get(VoteBanSession, session_id)
            old_token = record.enforcing_started_at
            new_token = old_token + timedelta(minutes=2)
            record.enforcing_started_at = new_token
            await session.commit()

        async with self.session_factory() as session:
            record = await session.get(VoteBanSession, session_id)
            persisted = await record_vote_ban_outcome(
                session,
                record,
                approvals=2,
                banned=False,
                lease_token=old_token,
            )
        self.assertFalse(persisted)

        async with self.session_factory() as session:
            record = await session.get(VoteBanSession, session_id)
            self.assertEqual(record.status, "enforcing")
            self.assertEqual(record.enforcing_started_at, new_token)
            events = list(
                (
                    await session.scalars(
                        select(BanAuditEvent).where(
                            BanAuditEvent.reference_type == "vote_session",
                            BanAuditEvent.reference_id == session_id,
                        )
                    )
                ).all()
            )
            self.assertEqual(events, [])

            persisted = await record_vote_ban_outcome(
                session,
                record,
                approvals=2,
                banned=True,
                lease_token=new_token,
            )
            self.assertTrue(persisted)

        async with self.session_factory() as session:
            record = await session.get(VoteBanSession, session_id)
            events = list(
                (
                    await session.scalars(
                        select(BanAuditEvent).where(
                            BanAuditEvent.reference_type == "vote_session",
                            BanAuditEvent.reference_id == session_id,
                        )
                    )
                ).all()
            )
            self.assertEqual(record.status, "passed")
            self.assertEqual(len(events), 1)
            self.assertEqual(events[0].outcome, "succeeded")

    async def test_threshold_ban_failure_is_persisted_as_failed_outcome(self) -> None:
        session_id = await self._open_session(threshold=2)
        bot = SimpleNamespace(
            ban_chat_member=AsyncMock(side_effect=RuntimeError("api down")),
            edit_message_text=AsyncMock(),
            get_chat_member=AsyncMock(return_value=SimpleNamespace(status="member")),
        )
        callback = self._callback(session_id, voter_id=11, bot=bot)
        async with self.session_factory() as session:
            await group.on_vote_ban_action(callback, self.settings, session=session)
        async with self.session_factory() as session:
            record = await session.get(VoteBanSession, session_id)
            event = await session.scalar(
                select(BanAuditEvent).where(
                    BanAuditEvent.reference_type == "vote_session",
                    BanAuditEvent.reference_id == session_id,
                )
            )
            self.assertEqual(record.status, "failed")
            self.assertEqual(event.outcome, "failed")
            warning = await session.scalar(
                select(UserWarning).where(
                    UserWarning.group_id == -100,
                    UserWarning.user_id == 555,
                )
            )
            self.assertFalse(bool(warning.is_banned) if warning else False)

    async def test_status_claim_is_single_winner(self) -> None:
        session_id = await self._open_session()
        async with self.session_factory() as session:
            first = await claim_session_status(
                session, session_id, expected="active", new_status="passed"
            )
            await session.commit()
        async with self.session_factory() as session:
            second = await claim_session_status(
                session, session_id, expected="active", new_status="expired"
            )
            await session.commit()
        self.assertTrue(first)
        self.assertFalse(second)


class VoteBanCommandTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        vote_ban._start_locks.clear()
        fd, self._db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.engine, self.session_factory = await init_db(
            f"sqlite+aiosqlite:///{self._db_path}"
        )
        async with self.session_factory() as session:
            session.add(
                Group(id=-100, title="test", settings={"vote_ban_enabled": True})
            )
            await authorize_group(session, -100, 1)
            await session.commit()
        self.settings = _settings()

    async def asyncTearDown(self) -> None:
        await self.engine.dispose()
        for suffix in ("", "-wal", "-shm"):
            try:
                os.remove(f"{self._db_path}{suffix}")
            except OSError:
                pass

    def _message(self, *, reply, starter_id: int = 10) -> SimpleNamespace:
        return SimpleNamespace(
            chat=SimpleNamespace(id=-100, type="supergroup", title="test"),
            from_user=SimpleNamespace(id=starter_id, full_name="发起人"),
            sender_chat=None,
            text="/voteban",
            reply_to_message=reply,
            bot=SimpleNamespace(
                send_message=AsyncMock(
                    return_value=SimpleNamespace(message_id=888)
                ),
                get_chat_member=AsyncMock(
                    return_value=SimpleNamespace(status="member")
                ),
            ),
        )

    @staticmethod
    def _reply(target_id: int = 555, *, is_bot: bool = False) -> SimpleNamespace:
        return SimpleNamespace(
            message_id=42,
            from_user=SimpleNamespace(
                id=target_id,
                full_name="骚扰者",
                username="spammer",
                is_bot=is_bot,
            ),
            sender_chat=None,
            text="骚扰内容",
            caption=None,
        )

    async def test_creates_session_and_sends_prompt(self) -> None:
        message = self._message(reply=self._reply())
        with patch(
            "bot.handlers.commands.ensure_group_authorized",
            new=AsyncMock(return_value=True),
        ):
            async with self.session_factory() as session:
                await commands.cmd_voteban(
                    message,
                    session=session,
                    settings=self.settings,
                    session_factory=None,
                )
        message.bot.send_message.assert_awaited()
        text = message.bot.send_message.await_args.args[1]
        self.assertIn("民主投票封禁", text)
        self.assertIn("1/3", text)
        async with self.session_factory() as session:
            record = await session.scalar(select(VoteBanSession))
            self.assertEqual(record.target_user_id, 555)
            self.assertEqual(record.message_id, 888)
            self.assertEqual(record.status, "active")
            votes = (await session.scalars(select(VoteBanVote))).all()
            self.assertEqual([vote.user_id for vote in votes], [10])

    async def test_rejects_admin_target(self) -> None:
        message = self._message(reply=self._reply())
        message.bot.get_chat_member = AsyncMock(
            return_value=SimpleNamespace(status="administrator")
        )
        with (
            patch("bot.handlers.commands.ensure_group_authorized", new=AsyncMock(return_value=True)),
            patch("bot.handlers.commands._answer", new=AsyncMock()) as answer_mock,
        ):
            async with self.session_factory() as session:
                await commands.cmd_voteban(
                    message,
                    session=session,
                    settings=self.settings,
                    session_factory=None,
                )
        message.bot.send_message.assert_not_awaited()
        self.assertIn("管理员", answer_mock.await_args.args[2])

    async def test_target_status_lookup_failure_fails_closed(self) -> None:
        message = self._message(reply=self._reply())
        message.bot.get_chat_member = AsyncMock(side_effect=RuntimeError("api down"))
        with (
            patch("bot.handlers.commands.ensure_group_authorized", new=AsyncMock(return_value=True)),
            patch("bot.handlers.commands._answer", new=AsyncMock()) as answer_mock,
        ):
            async with self.session_factory() as session:
                await commands.cmd_voteban(
                    message,
                    session=session,
                    settings=self.settings,
                    session_factory=None,
                )
        message.bot.send_message.assert_not_awaited()
        self.assertIn("确认", answer_mock.await_args.args[2])

    async def test_persistent_trigger_quota_blocks_next_target(self) -> None:
        self.settings.vote_ban_trigger_limit = 1
        first = self._message(reply=self._reply(target_id=555))
        second = self._message(reply=self._reply(target_id=556))
        with patch(
            "bot.handlers.commands.ensure_group_authorized",
            new=AsyncMock(return_value=True),
        ):
            async with self.session_factory() as session:
                await commands.cmd_voteban(
                    first,
                    session=session,
                    settings=self.settings,
                    session_factory=None,
                )
        vote_ban._start_locks.clear()  # Simulate a process restart.
        with (
            patch("bot.handlers.commands.ensure_group_authorized", new=AsyncMock(return_value=True)),
            patch("bot.handlers.commands._answer", new=AsyncMock()) as answer_mock,
        ):
            async with self.session_factory() as session:
                await commands.cmd_voteban(
                    second,
                    session=session,
                    settings=self.settings,
                    session_factory=None,
                )
        second.bot.send_message.assert_not_awaited()
        self.assertIn("最多只能发起 1 次", answer_mock.await_args.args[2])
        async with self.session_factory() as session:
            bucket = await session.get(VoteBanQuotaBucket, (-100, 10))
            self.assertEqual(bucket.used_count, 1)

    async def test_prompt_send_failure_releases_trigger_quota(self) -> None:
        message = self._message(reply=self._reply())
        message.bot.send_message = AsyncMock(side_effect=RuntimeError("send failed"))
        with (
            patch("bot.handlers.commands.ensure_group_authorized", new=AsyncMock(return_value=True)),
            patch("bot.handlers.commands._answer", new=AsyncMock()) as answer_mock,
        ):
            async with self.session_factory() as session:
                await commands.cmd_voteban(
                    message,
                    session=session,
                    settings=self.settings,
                    session_factory=None,
                )
        self.assertIn("未扣除额度", answer_mock.await_args.args[2])
        async with self.session_factory() as session:
            bucket = await session.get(VoteBanQuotaBucket, (-100, 10))
            record = await session.scalar(select(VoteBanSession))
            self.assertEqual(bucket.used_count, 0)
            self.assertEqual(record.status, "cancelled")

    async def test_concurrent_starts_cannot_exceed_single_use_quota(self) -> None:
        self.settings.vote_ban_trigger_limit = 1
        messages = [
            self._message(reply=self._reply(target_id=601)),
            self._message(reply=self._reply(target_id=602)),
        ]

        async def invoke(message):
            async with self.session_factory() as session:
                return await vote_ban.start_vote_ban(
                    message,
                    session,
                    self.settings,
                    trigger_source="command",
                )

        results = await asyncio.gather(*(invoke(message) for message in messages))
        self.assertEqual(sum(1 for result in results if result.ok), 1)
        self.assertEqual(
            sum(
                message.bot.send_message.await_count
                for message in messages
            ),
            1,
        )
        self.assertEqual(
            {result.code for result in results},
            {"started", "starter_quota_exhausted"},
        )

    async def test_expired_quota_window_resets(self) -> None:
        self.settings.vote_ban_trigger_limit = 1
        first = self._message(reply=self._reply(target_id=611))
        second = self._message(reply=self._reply(target_id=612))
        async with self.session_factory() as session:
            self.assertTrue(
                (
                    await vote_ban.start_vote_ban(
                        first,
                        session,
                        self.settings,
                    )
                ).ok
            )
        async with self.session_factory() as session:
            bucket = await session.get(VoteBanQuotaBucket, (-100, 10))
            bucket.window_started_at = now_shanghai_naive() - timedelta(hours=2)
            await session.commit()
        async with self.session_factory() as session:
            result = await vote_ban.start_vote_ban(
                second,
                session,
                self.settings,
            )
        self.assertTrue(result.ok)
        self.assertEqual(result.quota.used, 1)

    async def test_requires_reply_target(self) -> None:
        message = self._message(reply=None)
        with (
            patch("bot.handlers.commands.ensure_group_authorized", new=AsyncMock(return_value=True)),
            patch("bot.handlers.commands._answer", new=AsyncMock()) as answer_mock,
        ):
            async with self.session_factory() as session:
                await commands.cmd_voteban(
                    message,
                    session=session,
                    settings=self.settings,
                    session_factory=None,
                )
        self.assertIn("命令用法", answer_mock.await_args.args[2])

    async def test_disabled_group_rejects(self) -> None:
        async with self.session_factory() as session:
            row = await session.get(Group, -100)
            row.settings = {"vote_ban_enabled": False}
            await session.commit()
        message = self._message(reply=self._reply())
        with (
            patch("bot.handlers.commands.ensure_group_authorized", new=AsyncMock(return_value=True)),
            patch("bot.handlers.commands._answer", new=AsyncMock()) as answer_mock,
        ):
            async with self.session_factory() as session:
                await commands.cmd_voteban(
                    message,
                    session=session,
                    settings=self.settings,
                    session_factory=None,
                )
        message.bot.send_message.assert_not_awaited()
        self.assertIn("未启用", answer_mock.await_args.args[2])


if __name__ == "__main__":
    unittest.main()
