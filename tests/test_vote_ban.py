import asyncio
import os
import tempfile
import unittest
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from aiogram.exceptions import TelegramBadRequest
from sqlalchemy import select

from bot.db.engine import init_db
from bot.db.models import (
    BanAuditEvent,
    Group,
    JoinVerification,
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
    build_vote_text,
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


class VoteBanTaskLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def asyncTearDown(self) -> None:
        await vote_ban.flush_vote_ban_tasks(timeout_seconds=0.5)

    async def test_shutdown_flush_joins_vote_tasks(self) -> None:
        release = asyncio.Event()

        async def cancellation_resistant() -> None:
            try:
                await asyncio.Future()
            except asyncio.CancelledError:
                await release.wait()

        task = asyncio.create_task(cancellation_resistant())
        await asyncio.sleep(0)
        vote_ban._expiry_tasks[999] = task
        flush = asyncio.create_task(
            vote_ban.flush_vote_ban_tasks(timeout_seconds=1.0)
        )
        await asyncio.sleep(0.01)
        self.assertFalse(flush.done())
        release.set()
        await asyncio.wait_for(flush, timeout=0.5)
        self.assertFalse(vote_ban._expiry_tasks)
        self.assertFalse(vote_ban._enforcement_tasks)

    async def test_shutdown_timeout_keeps_cancellation_resistant_task_registered(self) -> None:
        release = asyncio.Event()

        async def cancellation_resistant() -> None:
            try:
                await asyncio.Future()
            except asyncio.CancelledError:
                await release.wait()

        task = asyncio.create_task(cancellation_resistant())
        await asyncio.sleep(0)
        vote_ban._enforcement_tasks[1000] = task

        await vote_ban.flush_vote_ban_tasks(timeout_seconds=0.01)
        self.assertIs(vote_ban._enforcement_tasks.get(1000), task)
        self.assertFalse(task.done())

        release.set()
        await asyncio.wait_for(task, timeout=0.5)
        await vote_ban.flush_vote_ban_tasks(timeout_seconds=0.1)
        self.assertNotIn(1000, vote_ban._enforcement_tasks)

    async def test_replaced_expiry_task_cannot_retire_new_owner(self) -> None:
        kwargs = {
            "session_factory": object(),
            "bot": SimpleNamespace(),
            "settings": _settings(),
            "session_id": 321,
            "delay_seconds": 3600,
        }
        vote_ban.schedule_vote_expiry(**kwargs)
        await asyncio.sleep(0)
        first = vote_ban._expiry_tasks[321]

        vote_ban.schedule_vote_expiry(**kwargs)
        second = vote_ban._expiry_tasks[321]
        self.assertIsNot(first, second)
        await asyncio.sleep(0)

        self.assertIs(vote_ban._expiry_tasks.get(321), second)

    async def test_cancel_vote_expiry_stops_the_countdown_task(self) -> None:
        kwargs = {
            "session_factory": object(),
            "bot": SimpleNamespace(),
            "settings": _settings(),
            "session_id": 654,
            "delay_seconds": 3600,
        }
        vote_ban.schedule_vote_expiry(**kwargs)
        await asyncio.sleep(0)
        task = vote_ban._expiry_tasks[654]

        vote_ban.cancel_vote_expiry(654)
        await asyncio.sleep(0)

        self.assertNotIn(654, vote_ban._expiry_tasks)
        self.assertTrue(task.cancelled())


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


class VoteBanMessageLayoutTests(unittest.TestCase):
    def test_active_vote_separates_context_initiator_details_and_timer(self) -> None:
        record = SimpleNamespace(
            target_user_id=42,
            target_display="被举报者",
            starter_user_id=7,
            starter_display="发起人",
            reason="疑似&lt;广告&gt;",
            evidence="推广链接",
            threshold=3,
            deadline_at=now_shanghai_naive() + timedelta(minutes=10),
        )

        text = build_vote_text(record, approvals=1)

        self.assertIn("<b>民主投票封禁 · 进行中</b>", text)
        self.assertIn("<b>目标</b>　<a href=\"tg://user?id=42\">被举报者</a>", text)
        self.assertIn("<b>被举报消息</b>　推广链接", text)
        self.assertIn("<blockquote><b>发起人</b>　<a href=\"tg://user?id=7\">发起人</a></blockquote>", text)
        self.assertIn("<blockquote expandable>", text)
        self.assertIn("疑似&amp;lt;广告&amp;gt;", text)
        self.assertIn("投票 <b><i>10</i> 分钟</b> 后自动失效。", text)
        self.assertNotIn("当前票数", text)

    def test_start_failures_render_as_not_started_without_changing_summary(self) -> None:
        now = now_shanghai_naive()
        quota = vote_ban.VoteBanQuotaState(
            allowed=False,
            limit=1,
            used=1,
            remaining=0,
            window_seconds=3600,
            window_started_at=now,
            reset_at=now + timedelta(hours=1),
            retry_after_seconds=3600,
        )
        cases = (
            ("disabled", "本群未启用民主投票封禁。", None),
            (
                "starter_quota_exhausted",
                "额度已用完，请 1 小时后再试。",
                quota,
            ),
            (
                "send_failed",
                "投票消息发送失败，本次未扣除额度，请稍后重试。",
                quota,
            ),
        )

        for code, summary, result_quota in cases:
            with self.subTest(code=code):
                result = vote_ban.VoteBanStartResult(
                    False,
                    code,
                    summary,
                    quota=result_quota,
                )

                self.assertEqual(result.summary, summary)
                self.assertNotIn("<blockquote", result.summary)
                self.assertIn(
                    "<b>民主投票封禁 · 未发起</b>",
                    result.telegram_text,
                )
                self.assertIn("<blockquote expandable>", result.telegram_text)
                self.assertIn(summary, result.telegram_text)


class VoteBanGenerationGuardTests(unittest.IsolatedAsyncioTestCase):
    async def test_finalize_does_not_ban_after_recovery_generation_is_lost(self) -> None:
        lease_token = now_shanghai_naive()
        record = SimpleNamespace(
            id=12,
            status="enforcing",
            enforcing_started_at=lease_token,
            group_id=-100,
            target_user_id=555,
        )
        recovery = SimpleNamespace(
            verification_id=99,
            lease_until=lease_token + timedelta(minutes=5),
        )
        session = SimpleNamespace(
            refresh=AsyncMock(),
            commit=AsyncMock(),
        )
        callback = SimpleNamespace(
            bot=SimpleNamespace(ban_chat_member=AsyncMock()),
        )
        with (
            patch.object(
                group,
                "join_verification_lease_is_current",
                new=AsyncMock(return_value=False),
            ),
            patch.object(group, "apply_vote_ban", new=AsyncMock()) as apply_ban,
        ):
            result = await group._finalize_vote_enforcement(
                callback,
                _settings(),
                session,
                None,
                record=record,
                session_id=12,
                approvals=2,
                recovery=recovery,
            )

        self.assertEqual(result, (False, False))
        apply_ban.assert_not_awaited()
        session.commit.assert_awaited_once()


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
        await vote_ban.flush_vote_ban_tasks(timeout_seconds=0.5)
        await self.engine.dispose()
        for suffix in ("", "-wal", "-shm"):
            try:
                os.remove(f"{self._db_path}{suffix}")
            except OSError:
                pass

    async def _open_session(self, threshold: int = 3, *, target_user_id: int = 555) -> int:
        config = resolve_vote_ban_config(
            _settings(vote_ban_threshold=threshold), {}
        )
        async with self.session_factory() as session:
            record = await open_vote_session(
                session,
                group_id=-100,
                target_user_id=target_user_id,
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
        action: str = "vote",
    ) -> SimpleNamespace:
        return SimpleNamespace(
            data=f"vban:{action}:{session_id}",
            message=SimpleNamespace(
                chat=SimpleNamespace(id=-100, type="supergroup"),
                message_id=777,
                reply_markup=None,
            ),
            from_user=SimpleNamespace(
                id=voter_id, full_name="操作者", username=""
            ),
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

    async def test_closed_session_rejects_late_ballot(self) -> None:
        session_id = await self._open_session()
        async with self.session_factory() as session:
            self.assertTrue(
                await claim_session_status(
                    session,
                    session_id,
                    expected="active",
                    new_status="cancelled",
                    resolution="admin_cancel",
                    resolver_user_id=42,
                    resolver_display="管理员",
                )
            )
            await session.commit()
        async with self.session_factory() as session:
            self.assertFalse(await record_vote(session, session_id, 11))
            await session.commit()
            self.assertEqual(await count_approvals(session, session_id), 1)

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

    async def test_countdown_refresh_updates_timer_and_live_keyboard(self) -> None:
        session_id = await self._open_session(threshold=3)
        bot = SimpleNamespace(edit_message_text=AsyncMock(return_value=True))

        remaining = await vote_ban._refresh_active_vote_message(
            session_factory=self.session_factory,
            bot=bot,
            settings=self.settings,
            session_id=session_id,
        )

        self.assertIsNotNone(remaining)
        kwargs = bot.edit_message_text.await_args.kwargs
        self.assertIn("投票 <b><i>10</i> 分钟</b> 后自动失效。", kwargs["text"])
        self.assertNotIn("当前票数", kwargs["text"])
        self.assertEqual(
            kwargs["reply_markup"].inline_keyboard[0][0].text,
            "投票封禁（1/3）",
        )

    async def test_restore_active_vote_resumes_countdown_refresh(self) -> None:
        session_id = await self._open_session(threshold=3)
        refreshed = asyncio.Event()

        async def record_refresh(**_kwargs):
            refreshed.set()
            return True

        bot = SimpleNamespace(edit_message_text=AsyncMock(side_effect=record_refresh))
        with patch.object(vote_ban, "VOTE_BAN_COUNTDOWN_REFRESH_SECONDS", 0.01):
            await vote_ban.restore_vote_ban_tasks(
                session_factory=self.session_factory,
                bot=bot,
                settings=self.settings,
            )
            await asyncio.wait_for(refreshed.wait(), timeout=0.5)

        self.assertIn(session_id, vote_ban._expiry_tasks)
        vote_ban.cancel_vote_expiry(session_id)

    async def test_countdown_refresh_cannot_overwrite_newer_vote_edit(self) -> None:
        session_id = await self._open_session(threshold=3)
        ticker_started = asyncio.Event()
        release_ticker = asyncio.Event()
        edits: list[dict] = []

        async def delayed_edit(**kwargs):
            edits.append(kwargs)
            if len(edits) == 1:
                ticker_started.set()
                await release_ticker.wait()
            return True

        bot = SimpleNamespace(
            edit_message_text=AsyncMock(side_effect=delayed_edit),
            get_chat_member=AsyncMock(return_value=SimpleNamespace(status="member")),
        )
        ticker = asyncio.create_task(
            vote_ban._refresh_active_vote_message(
                session_factory=self.session_factory,
                bot=bot,
                settings=self.settings,
                session_id=session_id,
            )
        )
        await asyncio.wait_for(ticker_started.wait(), timeout=0.5)

        callback = self._callback(session_id, voter_id=11, bot=bot)

        async def run_vote_callback() -> None:
            async with self.session_factory() as session:
                await group.on_vote_ban_action(callback, self.settings, session=session)

        callback_task = asyncio.create_task(run_vote_callback())
        for _ in range(50):
            async with self.session_factory() as session:
                approvals = await count_approvals(session, session_id)
                await session.commit()
            if approvals == 2:
                break
            await asyncio.sleep(0.01)
        self.assertEqual(approvals, 2)

        release_ticker.set()
        await asyncio.wait_for(asyncio.gather(ticker, callback_task), timeout=1.0)

        self.assertEqual(len(edits), 2)
        self.assertEqual(
            edits[0]["reply_markup"].inline_keyboard[0][0].text,
            "投票封禁（1/3）",
        )
        self.assertEqual(
            edits[-1]["reply_markup"].inline_keyboard[0][0].text,
            "投票封禁（2/3）",
        )

    async def test_vote_below_threshold_updates_message(self) -> None:
        session_id = await self._open_session(threshold=3)
        callback = self._callback(session_id, voter_id=11)
        async with self.session_factory() as session:
            async def edit_without_db_lease(**_kwargs):
                self.assertFalse(session.in_transaction())
                self.assertEqual(self.engine.sync_engine.pool.checkedout(), 0)

            callback.bot.edit_message_text = AsyncMock(
                side_effect=edit_without_db_lease
            )
            await group.on_vote_ban_action(callback, self.settings, session=session)
        callback.bot.ban_chat_member.assert_not_awaited()
        callback.bot.edit_message_text.assert_awaited()
        kwargs = callback.bot.edit_message_text.await_args.kwargs
        self.assertIn("民主投票封禁 · 进行中", kwargs["text"])
        self.assertIn("<blockquote expandable>", kwargs["text"])
        self.assertIsNotNone(kwargs["reply_markup"])
        self.assertEqual(
            kwargs["reply_markup"].inline_keyboard[0][0].text,
            "投票封禁（2/3）",
        )
        async with self.session_factory() as session:
            record = await session.get(VoteBanSession, session_id)
            self.assertEqual(record.status, "active")

    async def test_membership_lookup_does_not_hold_database_transaction(self) -> None:
        session_id = await self._open_session(threshold=3)
        callback = self._callback(session_id, voter_id=11)
        async with self.session_factory() as session:
            async def membership_lookup(*_args, **_kwargs):
                self.assertFalse(session.in_transaction())
                self.assertEqual(self.engine.sync_engine.pool.checkedout(), 0)
                return SimpleNamespace(status="member")

            callback.bot.get_chat_member = AsyncMock(side_effect=membership_lookup)
            await group.on_vote_ban_action(callback, self.settings, session=session)

        callback.bot.get_chat_member.assert_awaited_once()

    async def test_threshold_reached_bans_and_finalizes(self) -> None:
        session_id = await self._open_session(threshold=2)
        callback = self._callback(session_id, voter_id=11)
        async with self.session_factory() as session:
            async def ban_without_db_lease(*_args, **_kwargs):
                self.assertFalse(session.in_transaction())
                self.assertEqual(self.engine.sync_engine.pool.checkedout(), 0)
                return True

            async def edit_without_db_lease(**_kwargs):
                self.assertFalse(session.in_transaction())
                self.assertEqual(self.engine.sync_engine.pool.checkedout(), 0)

            callback.bot.ban_chat_member = AsyncMock(
                side_effect=ban_without_db_lease
            )
            callback.bot.edit_message_text = AsyncMock(
                side_effect=edit_without_db_lease
            )
            await group.on_vote_ban_action(callback, self.settings, session=session)
        callback.bot.ban_chat_member.assert_awaited_once_with(
            -100,
            555,
            revoke_messages=True,
        )
        kwargs = callback.bot.edit_message_text.await_args.kwargs
        self.assertIn("已封禁", kwargs["text"])
        self.assertIn("民主投票封禁 · 已结束", kwargs["text"])
        self.assertIn("<blockquote expandable>", kwargs["text"])
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

    async def test_admin_action_rejected_for_non_admin(self) -> None:
        session_id = await self._open_session()
        for action in ("cancel", "ban"):
            callback = self._callback(session_id, voter_id=11, action=action)
            with patch.object(
                group, "is_group_admin_or_higher", new=AsyncMock(return_value=False)
            ):
                async with self.session_factory() as session:
                    await group.on_vote_ban_action(
                        callback, self.settings, session=session
                    )
            callback.bot.ban_chat_member.assert_not_awaited()
            self.assertIn("仅群管理员", callback.answer.await_args.args[0])
        async with self.session_factory() as session:
            record = await session.get(VoteBanSession, session_id)
            self.assertEqual(record.status, "active")

    async def test_admin_cancel_finalizes_without_ban(self) -> None:
        session_id = await self._open_session()
        callback = self._callback(session_id, voter_id=42, action="cancel")
        vote_ban.schedule_vote_expiry(
            session_factory=self.session_factory,
            bot=callback.bot,
            settings=self.settings,
            session_id=session_id,
            delay_seconds=600,
        )
        await asyncio.sleep(0)
        self.assertIn(session_id, vote_ban._expiry_tasks)
        with patch.object(
            group, "is_group_admin_or_higher", new=AsyncMock(return_value=True)
        ):
            async with self.session_factory() as session:
                await group.on_vote_ban_action(callback, self.settings, session=session)
        callback.bot.ban_chat_member.assert_not_awaited()
        self.assertIn("已取消", callback.answer.await_args.args[0])
        self.assertNotIn(session_id, vote_ban._expiry_tasks)
        kwargs = callback.bot.edit_message_text.await_args.kwargs
        self.assertIn("取消本次投票", kwargs["text"])
        self.assertIsNone(kwargs["reply_markup"])
        async with self.session_factory() as session:
            record = await session.get(VoteBanSession, session_id)
            self.assertEqual(record.status, "cancelled")
            self.assertEqual(record.resolution, "admin_cancel")
            self.assertEqual(record.resolver_user_id, 42)
            event = await session.scalar(
                select(BanAuditEvent).where(
                    BanAuditEvent.reference_type == "vote_session",
                    BanAuditEvent.reference_id == session_id,
                )
            )
            self.assertEqual(event.action, "vote_cancel")
            self.assertEqual(event.source, "democratic_vote_admin_cancel")
            self.assertEqual(event.outcome, "cancelled")
            self.assertEqual(event.actor_user_id, 42)
            self.assertEqual(event.details.get("resolution"), "admin_cancel")
            warning = await session.scalar(
                select(UserWarning).where(
                    UserWarning.group_id == -100, UserWarning.user_id == 555
                )
            )
            self.assertFalse(bool(warning.is_banned) if warning else False)

    async def test_concurrent_cancel_cannot_be_overwritten_by_live_vote_edit(self) -> None:
        session_id = await self._open_session(threshold=3)
        callback = self._callback(session_id, voter_id=11)
        edits: list[dict] = []

        async def edit_then_cancel(**kwargs):
            edits.append(kwargs)
            if len(edits) == 1:
                async with self.session_factory() as cancel_session:
                    self.assertTrue(
                        await claim_session_status(
                            cancel_session,
                            session_id,
                            expected="active",
                            new_status="cancelled",
                            resolution="admin_cancel",
                            resolver_user_id=42,
                            resolver_display="管理员",
                        )
                    )
                    await cancel_session.commit()
            return True

        callback.bot.edit_message_text = AsyncMock(side_effect=edit_then_cancel)
        async with self.session_factory() as session:
            await group.on_vote_ban_action(callback, self.settings, session=session)

        self.assertEqual(len(edits), 2)
        self.assertIsNotNone(edits[0]["reply_markup"])
        self.assertIsNone(edits[-1]["reply_markup"])
        self.assertIn("取消本次投票", edits[-1]["text"])
        self.assertIn("其他操作结束", callback.answer.await_args.args[0])

    async def test_admin_direct_ban_finalizes_with_audit(self) -> None:
        session_id = await self._open_session(threshold=3)
        callback = self._callback(session_id, voter_id=42, action="ban")
        with patch.object(
            group, "is_group_admin_or_higher", new=AsyncMock(return_value=True)
        ):
            async with self.session_factory() as session:
                await group.on_vote_ban_action(callback, self.settings, session=session)
        callback.bot.ban_chat_member.assert_awaited_once_with(
            -100,
            555,
            revoke_messages=True,
        )
        self.assertIn("已直接封禁", callback.answer.await_args.args[0])
        kwargs = callback.bot.edit_message_text.await_args.kwargs
        self.assertIn("直接封禁", kwargs["text"])
        self.assertIsNone(kwargs["reply_markup"])
        async with self.session_factory() as session:
            record = await session.get(VoteBanSession, session_id)
            self.assertEqual(record.status, "passed")
            self.assertEqual(record.resolution, "admin_ban")
            self.assertEqual(record.resolver_user_id, 42)
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
            self.assertEqual(event.source, "democratic_vote_admin_ban")
            self.assertEqual(event.actor_user_id, 42)
            self.assertEqual(event.details.get("resolution"), "admin_ban")

    async def test_admin_direct_ban_failure_is_persisted_as_failed(self) -> None:
        session_id = await self._open_session(threshold=3)
        bot = SimpleNamespace(
            ban_chat_member=AsyncMock(side_effect=RuntimeError("api down")),
            edit_message_text=AsyncMock(),
            get_chat_member=AsyncMock(return_value=SimpleNamespace(status="member")),
        )
        callback = self._callback(session_id, voter_id=42, action="ban", bot=bot)
        with patch.object(
            group, "is_group_admin_or_higher", new=AsyncMock(return_value=True)
        ):
            async with self.session_factory() as session:
                await group.on_vote_ban_action(callback, self.settings, session=session)
        self.assertIn("失败", callback.answer.await_args.args[0])
        async with self.session_factory() as session:
            record = await session.get(VoteBanSession, session_id)
            self.assertEqual(record.status, "failed")
            self.assertEqual(record.resolution, "admin_ban")
            warning = await session.scalar(
                select(UserWarning).where(
                    UserWarning.group_id == -100, UserWarning.user_id == 555
                )
            )
            self.assertFalse(bool(warning.is_banned) if warning else False)

    async def test_admin_action_on_overdue_session_expires_first(self) -> None:
        session_id = await self._open_session()
        async with self.session_factory() as session:
            record = await session.get(VoteBanSession, session_id)
            record.deadline_at = now_shanghai_naive() - timedelta(seconds=5)
            await session.commit()
        callback = self._callback(session_id, voter_id=42, action="ban")
        with patch.object(
            group, "is_group_admin_or_higher", new=AsyncMock(return_value=True)
        ):
            async with self.session_factory() as session:
                await group.on_vote_ban_action(callback, self.settings, session=session)
        callback.bot.ban_chat_member.assert_not_awaited()
        self.assertIn("超时", callback.answer.await_args.args[0])
        async with self.session_factory() as session:
            record = await session.get(VoteBanSession, session_id)
            self.assertEqual(record.status, "expired")

    async def test_admin_ban_recovery_keeps_admin_resolution_audit(self) -> None:
        session_id = await self._open_session(threshold=3)
        async with self.session_factory() as session:
            self.assertTrue(
                await claim_session_status(
                    session,
                    session_id,
                    expected="active",
                    new_status="enforcing",
                    resolution="admin_ban",
                    resolver_user_id=42,
                    resolver_display="管理员",
                )
            )
            record = await session.get(VoteBanSession, session_id)
            record.enforcing_started_at = now_shanghai_naive() - timedelta(minutes=5)
            await session.commit()

        bot = SimpleNamespace(
            get_chat_member=AsyncMock(
                return_value=SimpleNamespace(status="member")
            ),
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
        kwargs = bot.edit_message_text.await_args.kwargs
        self.assertIn("直接封禁", kwargs["text"])
        async with self.session_factory() as session:
            event = await session.scalar(
                select(BanAuditEvent).where(
                    BanAuditEvent.reference_type == "vote_session",
                    BanAuditEvent.reference_id == session_id,
                )
            )
            self.assertEqual(event.source, "democratic_vote_admin_ban")
            self.assertEqual(event.actor_user_id, 42)

    async def test_keyboard_carries_vote_and_admin_buttons(self) -> None:
        markup = vote_ban.build_vote_keyboard(9, 1, 3)
        callback_data = [
            button.callback_data
            for row in markup.inline_keyboard
            for button in row
        ]
        self.assertEqual(
            callback_data,
            ["vban:vote:9", "vban:cancel:9", "vban:ban:9"],
        )

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
                get_chat_member=AsyncMock(
                    return_value=SimpleNamespace(status="member")
                ),
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
                get_chat_member=AsyncMock(
                    return_value=SimpleNamespace(status="member")
                ),
                ban_chat_member=AsyncMock(return_value=True)
            )
            banned = await apply_vote_ban(
                bot, session, group_id=-100, target_user_id=555
            )
        self.assertTrue(banned)
        bot.ban_chat_member.assert_awaited_once_with(
            -100,
            555,
            revoke_messages=True,
        )
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
            get_chat_member=AsyncMock(
                return_value=SimpleNamespace(status="member")
            ),
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
        bot.ban_chat_member.assert_awaited_once_with(
            -100,
            555,
            revoke_messages=True,
        )
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

    async def test_manual_unban_cancels_active_and_enforcing_votes(self) -> None:
        active_id = await self._open_session(target_user_id=555)
        enforcing_id = await self._open_session(target_user_id=556)
        async with self.session_factory() as session:
            self.assertTrue(
                await claim_session_status(
                    session,
                    enforcing_id,
                    expected="active",
                    new_status="enforcing",
                )
            )
            old_recovery = await vote_ban.lease_join_verification_for_unban(
                session,
                -100,
                556,
                manual_unban=False,
            )
            self.assertIsNotNone(old_recovery)
            await session.commit()

        async with self.session_factory() as session:
            active_recovery = await vote_ban.lease_join_verification_for_unban(
                session,
                -100,
                555,
            )
            newer_recovery = await vote_ban.lease_join_verification_for_unban(
                session,
                -100,
                556,
            )
            self.assertIsNotNone(active_recovery)
            self.assertIsNotNone(newer_recovery)
            await session.commit()

        async with self.session_factory() as session:
            active = await session.get(VoteBanSession, active_id)
            enforcing = await session.get(VoteBanSession, enforcing_id)
            self.assertEqual(active.status, "cancelled")
            self.assertEqual(enforcing.status, "cancelled")
            self.assertEqual(active.resolution, "manual_unban")
            self.assertEqual(enforcing.resolution, "manual_unban")
            current = await session.scalar(
                select(JoinVerification).where(
                    JoinVerification.group_id == -100,
                    JoinVerification.user_id == 556,
                )
            )
            self.assertEqual(current.lease_until, newer_recovery.lease_until)

    async def test_old_vote_generation_cannot_publish_after_manual_unban(self) -> None:
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
            old_recovery = await vote_ban.lease_join_verification_for_unban(
                session,
                -100,
                555,
                manual_unban=False,
            )
            record = await session.get(VoteBanSession, session_id)
            old_vote_token = record.enforcing_started_at
            await session.commit()

        async with self.session_factory() as session:
            newer_recovery = await vote_ban.lease_join_verification_for_unban(
                session,
                -100,
                555,
            )
            await session.commit()

        async with self.session_factory() as session:
            record = await session.get(VoteBanSession, session_id)
            persisted = await record_vote_ban_outcome(
                session,
                record,
                approvals=2,
                banned=True,
                lease_token=old_vote_token,
                recovery=old_recovery,
            )
            self.assertFalse(persisted)

        async with self.session_factory() as session:
            record = await session.get(VoteBanSession, session_id)
            warning = await session.scalar(
                select(UserWarning).where(
                    UserWarning.group_id == -100,
                    UserWarning.user_id == 555,
                )
            )
            current = await session.scalar(
                select(JoinVerification).where(
                    JoinVerification.group_id == -100,
                    JoinVerification.user_id == 555,
                )
            )
            self.assertEqual(record.status, "cancelled")
            self.assertIsNone(warning)
            self.assertEqual(current.lease_until, newer_recovery.lease_until)

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
            old_recovery = await vote_ban.lease_join_verification_for_unban(
                session,
                -100,
                555,
                manual_unban=False,
            )
            await session.commit()
            record = await session.get(VoteBanSession, session_id)
            old_token = record.enforcing_started_at
            new_token = old_token + timedelta(minutes=2)
            record.enforcing_started_at = new_token
            new_recovery = await vote_ban.lease_join_verification_for_unban(
                session,
                -100,
                555,
                now=now_shanghai_naive() + timedelta(minutes=2),
                manual_unban=False,
            )
            await session.commit()

        async with self.session_factory() as session:
            record = await session.get(VoteBanSession, session_id)
            persisted = await record_vote_ban_outcome(
                session,
                record,
                approvals=2,
                banned=False,
                lease_token=old_token,
                recovery=old_recovery,
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
                recovery=new_recovery,
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

    async def test_threshold_rechecks_target_and_refuses_new_admin(self) -> None:
        session_id = await self._open_session(threshold=2)
        bot = SimpleNamespace(
            # First lookup validates the voter; the second fresh lookup protects
            # a target promoted while the vote was in progress.
            get_chat_member=AsyncMock(
                side_effect=(
                    SimpleNamespace(status="member"),
                    SimpleNamespace(status="administrator"),
                )
            ),
            ban_chat_member=AsyncMock(return_value=True),
            edit_message_text=AsyncMock(),
        )
        callback = self._callback(session_id, voter_id=11, bot=bot)

        async with self.session_factory() as session:
            await group.on_vote_ban_action(callback, self.settings, session=session)

        bot.ban_chat_member.assert_not_awaited()
        async with self.session_factory() as session:
            record = await session.get(VoteBanSession, session_id)
            self.assertEqual(record.status, "failed")
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
        self.assertNotIn("1/3", text)
        markup = message.bot.send_message.await_args.kwargs["reply_markup"]
        self.assertEqual(markup.inline_keyboard[0][0].text, "投票封禁（1/3）")
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

    async def test_target_status_lookup_releases_database_transaction_first(self) -> None:
        message = self._message(reply=self._reply())
        async with self.session_factory() as session:
            async def get_chat_member(_group_id: int, _user_id: int):
                self.assertFalse(session.in_transaction())
                return SimpleNamespace(status="administrator")

            message.bot.get_chat_member = AsyncMock(side_effect=get_chat_member)
            result = await vote_ban.start_vote_ban(
                message,
                session,
                self.settings,
            )

        self.assertFalse(result.ok)
        self.assertEqual(result.code, "admin_target")

    async def test_delivery_callback_precedes_post_send_commit_failure(self) -> None:
        message = self._message(reply=self._reply())
        events: list[str] = []
        prompt_sent = False

        async def send_prompt(*_args: object, **_kwargs: object) -> SimpleNamespace:
            nonlocal prompt_sent
            prompt_sent = True
            events.append("send_returned")
            return SimpleNamespace(message_id=888)

        message.bot.send_message = AsyncMock(side_effect=send_prompt)
        async with self.session_factory() as session:
            original_commit = session.commit

            async def commit_with_post_send_failure() -> None:
                if prompt_sent:
                    events.append("post_send_commit")
                    raise RuntimeError("message-id commit failed")
                await original_commit()

            with patch.object(
                session,
                "commit",
                new=AsyncMock(side_effect=commit_with_post_send_failure),
            ):
                with self.assertRaisesRegex(RuntimeError, "message-id commit failed"):
                    await vote_ban.start_vote_ban(
                        message,
                        session,
                        self.settings,
                        on_delivery=lambda: events.append("delivery_confirmed"),
                    )

        self.assertEqual(
            events,
            ["send_returned", "delivery_confirmed", "post_send_commit"],
        )

    async def test_post_send_commit_failure_recovers_message_id_and_arms_expiry(
        self,
    ) -> None:
        message = self._message(reply=self._reply())
        prompt_sent = False

        async def send_prompt(*_args: object, **_kwargs: object) -> SimpleNamespace:
            nonlocal prompt_sent
            prompt_sent = True
            return SimpleNamespace(message_id=888)

        message.bot.send_message = AsyncMock(side_effect=send_prompt)
        schedule_expiry = patch("bot.services.vote_ban.schedule_vote_expiry")
        async with self.session_factory() as session:
            original_commit = session.commit

            async def commit_with_post_send_failure() -> None:
                if prompt_sent:
                    raise RuntimeError("message-id commit failed")
                await original_commit()

            with (
                patch.object(
                    session,
                    "commit",
                    new=AsyncMock(side_effect=commit_with_post_send_failure),
                ),
                schedule_expiry as schedule_mock,
            ):
                result = await vote_ban.start_vote_ban(
                    message,
                    session,
                    self.settings,
                    session_factory=self.session_factory,
                )

        self.assertTrue(result.ok)
        schedule_mock.assert_called_once()
        async with self.session_factory() as session:
            record = await session.scalar(select(VoteBanSession))
            self.assertEqual(record.message_id, 888)
            self.assertEqual(record.status, "active")

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
        answer_text = answer_mock.await_args.args[2]
        self.assertIn("<b>民主投票封禁 · 未发起</b>", answer_text)
        self.assertIn("<blockquote expandable>", answer_text)
        self.assertIn("最多只能发起 1 次", answer_text)
        self.assertIn("<b>已用额度</b>　<code>1 / 1</code>", answer_text)
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
        answer_text = answer_mock.await_args.args[2]
        message.bot.send_message.assert_awaited_once()
        self.assertIn("<b>民主投票封禁 · 未发起</b>", answer_text)
        self.assertIn("<blockquote expandable>", answer_text)
        self.assertIn("未扣除额度", answer_text)
        self.assertIn("<b>剩余额度</b>　<code>3</code>", answer_text)
        async with self.session_factory() as session:
            bucket = await session.get(VoteBanQuotaBucket, (-100, 10))
            record = await session.scalar(select(VoteBanSession))
            self.assertEqual(bucket.used_count, 0)
            self.assertEqual(record.status, "cancelled")

    async def test_missing_reply_target_retries_poll_without_reply_anchor(self) -> None:
        message = self._message(reply=self._reply())
        reply_missing = TelegramBadRequest(
            method=SimpleNamespace(),
            message="Bad Request: reply message not found",
        )
        message.bot.send_message = AsyncMock(
            side_effect=[reply_missing, SimpleNamespace(message_id=888)]
        )

        async with self.session_factory() as session:
            result = await vote_ban.start_vote_ban(
                message,
                session,
                self.settings,
            )

        self.assertTrue(result.ok)
        self.assertEqual(message.bot.send_message.await_count, 2)
        first_call, second_call = message.bot.send_message.await_args_list
        self.assertIn("reply_to_message_id", first_call.kwargs)
        self.assertNotIn("reply_to_message_id", second_call.kwargs)

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
        answer_text = answer_mock.await_args.args[2]
        self.assertIn("<b>民主投票封禁 · 未发起</b>", answer_text)
        self.assertIn("<blockquote expandable>", answer_text)
        self.assertIn("未启用", answer_text)


if __name__ == "__main__":
    unittest.main()
