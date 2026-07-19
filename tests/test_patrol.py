import asyncio
import os
import tempfile
import unittest
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from sqlalchemy import select

from bot.db.engine import init_db
from bot.db.models import (
    Group,
    GroupMember,
    JoinVerification,
    MessageVector,
    ModerationRule,
    PatrolRun,
    UserProfileScreen,
    UserWarning,
)
from bot.handlers import membership
from bot.services.join_verification import (
    PATROL_VERIFY_CALLBACK_DATA,
    VERIFICATION_KIND_MODERATION,
    VERIFICATION_KIND_PATROL,
    JoinVerificationSweeper,
    get_join_verification,
    upsert_join_verification,
    verification_timeout_seconds_for_kind,
)
from bot.services.patrol import (
    PatrolService,
    PatrolViolator,
    build_patrol_warning_text,
    build_patrol_challenge_keyboard,
    get_patrol_screen_hash,
    mark_group_member_left,
    mark_patrol_screened,
    parse_schedule_time,
    patrol_due,
    patrol_policy,
    reset_roster_cache,
    seed_roster_from_history,
    track_group_member,
)
from bot.utils.timezone import now_shanghai_naive


def _settings(**overrides):
    from bot.config import Settings

    settings = Settings(_env_file=None)
    settings.moderation.enabled = True
    settings.join_verification_turnstile_site_key = "site-key"
    settings.join_verification_turnstile_secret_key = "secret-key"
    settings.join_verification_public_base_url = "https://verify.example.com"
    settings.patrol_enabled = True
    settings.patrol_fetch_bio = False
    settings.patrol_batch_pause_seconds = 0.0
    for key, value in overrides.items():
        setattr(settings, key, value)
    return settings


def _bot_mock() -> SimpleNamespace:
    return SimpleNamespace(
        restrict_chat_member=AsyncMock(return_value=True),
        ban_chat_member=AsyncMock(return_value=True),
        unban_chat_member=AsyncMock(return_value=True),
        send_message=AsyncMock(return_value=SimpleNamespace(message_id=555)),
        edit_message_text=AsyncMock(),
        get_chat=AsyncMock(side_effect=Exception("no getChat in tests")),
        get_chat_administrators=AsyncMock(return_value=[]),
        get_chat_member=AsyncMock(
            return_value=SimpleNamespace(status="member", can_send_messages=True)
        ),
        me=AsyncMock(return_value=SimpleNamespace(username="my_bot")),
    )


class _DbTestCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        fd, self._db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.engine, self.session_factory = await init_db(
            f"sqlite+aiosqlite:///{self._db_path}"
        )
        reset_roster_cache()
        from bot.services.authz import authorize_group

        async with self.session_factory() as session:
            await authorize_group(session, -100, 1)
            session.add(Group(id=-100, title="test", settings={}))
            session.add(
                ModerationRule(
                    group_id=-100,
                    rule_type="llm",
                    pattern="禁止广告",
                    action="warn",
                    enabled=True,
                )
            )
            await session.commit()

    async def asyncTearDown(self) -> None:
        await self.engine.dispose()
        for suffix in ("", "-wal", "-shm"):
            try:
                os.remove(f"{self._db_path}{suffix}")
            except OSError:
                pass

    async def _add_member(
        self,
        user_id: int,
        *,
        full_name: str = "",
        username: str = "",
        is_bot: bool = False,
    ) -> None:
        async with self.session_factory() as session:
            await track_group_member(
                session,
                -100,
                user_id=user_id,
                full_name=full_name,
                username=username,
                is_bot=is_bot,
            )
            await session.commit()


class ScheduleHelperTests(unittest.TestCase):
    def test_parse_schedule_time(self) -> None:
        self.assertEqual(parse_schedule_time("04:30"), (4, 30))
        self.assertEqual(parse_schedule_time(" 23:59 "), (23, 59))
        self.assertIsNone(parse_schedule_time("24:00"))
        self.assertIsNone(parse_schedule_time("4:60"))
        self.assertIsNone(parse_schedule_time("abc"))
        self.assertIsNone(parse_schedule_time(""))

    def test_patrol_due_once_per_day(self) -> None:
        now = datetime(2026, 7, 16, 5, 0, 0)
        # Never ran: due after the scheduled instant, not before.
        self.assertTrue(
            patrol_due(schedule_time="04:30", last_run_at=None, now=now)
        )
        self.assertFalse(
            patrol_due(
                schedule_time="04:30",
                last_run_at=None,
                now=datetime(2026, 7, 16, 4, 0, 0),
            )
        )
        # Ran today after the scheduled instant: not due again.
        self.assertFalse(
            patrol_due(
                schedule_time="04:30",
                last_run_at=datetime(2026, 7, 16, 4, 31, 0),
                now=now,
            )
        )
        # Ran yesterday: due again today.
        self.assertTrue(
            patrol_due(
                schedule_time="04:30",
                last_run_at=datetime(2026, 7, 15, 4, 31, 0),
                now=now,
            )
        )
        # Invalid schedule never fires.
        self.assertFalse(patrol_due(schedule_time="x", last_run_at=None, now=now))

    def test_patrol_policy_group_override(self) -> None:
        settings = _settings(patrol_enabled=False)
        self.assertFalse(patrol_policy(settings, None))
        self.assertTrue(patrol_policy(settings, {"patrol_enabled": True}))
        settings_on = _settings(patrol_enabled=True)
        self.assertTrue(patrol_policy(settings_on, {}))
        self.assertFalse(patrol_policy(settings_on, {"patrol_enabled": False}))

    def test_patrol_timeout_prefers_patrol_setting(self) -> None:
        settings = _settings(
            patrol_challenge_timeout_seconds=1200,
            join_verification_timeout_seconds=600,
        )
        self.assertEqual(
            verification_timeout_seconds_for_kind(settings, VERIFICATION_KIND_PATROL),
            1200,
        )
        settings.patrol_challenge_timeout_seconds = 0
        self.assertEqual(
            verification_timeout_seconds_for_kind(settings, VERIFICATION_KIND_PATROL),
            600,
        )


class PatrolTaskLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def test_shutdown_is_bounded_when_manual_run_ignores_cancellation(self) -> None:
        service = PatrolService(
            bot=_bot_mock(),
            settings=_settings(),
            session_factory=None,  # type: ignore[arg-type]
        )
        started = asyncio.Event()
        release = asyncio.Event()
        task: asyncio.Task | None = None

        async def stubborn_manual_run(_group_id: int) -> None:
            started.set()
            while not release.is_set():
                try:
                    await release.wait()
                except asyncio.CancelledError:
                    continue

        service._manual_patrol = stubborn_manual_run  # type: ignore[method-assign]
        try:
            result = service.start_manual_patrol(-100)
            self.assertTrue(result["started"])
            task = next(iter(service._manual_tasks))
            await asyncio.wait_for(started.wait(), timeout=1.0)

            before = asyncio.get_running_loop().time()
            await service.shutdown(timeout_seconds=0.02)
            self.assertLess(
                asyncio.get_running_loop().time() - before,
                0.2,
            )
            self.assertIn(task, service._manual_tasks)
            self.assertFalse(task.done())
        finally:
            release.set()
            if task is not None:
                await asyncio.wait_for(task, timeout=0.5)

        self.assertEqual(service._manual_tasks, set())


class WarningMessageTests(unittest.TestCase):
    def test_warning_mentions_all_violators(self) -> None:
        violators = [
            PatrolViolator(user_id=1, full_name="张三", username="", reason="广告昵称"),
            PatrolViolator(user_id=2, full_name="", username="spam_guy", reason="简介违规"),
        ]
        text = build_patrol_warning_text(violators, timeout_seconds=600)
        self.assertIn('tg://user?id=1', text)
        self.assertIn("张三", text)
        self.assertIn("@spam_guy", text)
        self.assertIn("广告昵称", text)
        self.assertIn("10 分钟", text)
        self.assertIn("真人质询", text)
        self.assertIn("不会封禁", text)

    def test_challenge_keyboard_uses_shared_callback(self) -> None:
        keyboard = build_patrol_challenge_keyboard()
        button = keyboard.inline_keyboard[0][0]
        self.assertEqual(button.callback_data, PATROL_VERIFY_CALLBACK_DATA)


class RosterTests(_DbTestCase):
    async def test_track_and_leave_roundtrip(self) -> None:
        await self._add_member(7, full_name="Alice", username="alice")
        async with self.session_factory() as session:
            row = await session.scalar(
                select(GroupMember).where(
                    GroupMember.group_id == -100, GroupMember.user_id == 7
                )
            )
            self.assertIsNotNone(row)
            self.assertEqual(row.full_name, "Alice")
            self.assertFalse(row.left)

        async with self.session_factory() as session:
            await mark_group_member_left(session, -100, 7)
            await session.commit()
        async with self.session_factory() as session:
            row = await session.scalar(
                select(GroupMember).where(
                    GroupMember.group_id == -100, GroupMember.user_id == 7
                )
            )
            self.assertTrue(row.left)

        # Re-tracking (rejoin / new message) clears the left flag.
        await self._add_member(7, full_name="Alice2", username="alice")
        async with self.session_factory() as session:
            row = await session.scalar(
                select(GroupMember).where(
                    GroupMember.group_id == -100, GroupMember.user_id == 7
                )
            )
            self.assertFalse(row.left)
            self.assertEqual(row.full_name, "Alice2")

    async def test_seed_roster_from_history(self) -> None:
        async with self.session_factory() as session:
            session.add(
                MessageVector(
                    group_id=-100,
                    message_id="m1",
                    role="user",
                    sender_id=31,
                    sender_name="旧用户",
                    content="hi",
                )
            )
            session.add(
                MessageVector(
                    group_id=-100,
                    message_id="m2",
                    role="assistant",
                    sender_id=999,
                    sender_name="bot",
                    content="reply",
                )
            )
            await session.commit()

        async with self.session_factory() as session:
            seeded = await seed_roster_from_history(session, -100)
            await session.commit()
        self.assertEqual(seeded, 1)
        async with self.session_factory() as session:
            row = await session.scalar(
                select(GroupMember).where(
                    GroupMember.group_id == -100, GroupMember.user_id == 31
                )
            )
            self.assertIsNotNone(row)
            self.assertEqual(row.full_name, "旧用户")
            # Second seed is a no-op.
            self.assertEqual(await seed_roster_from_history(session, -100), 0)


class PatrolScanTests(_DbTestCase):
    def _service(self, settings=None) -> tuple[PatrolService, SimpleNamespace]:
        settings = settings or _settings()
        bot = _bot_mock()
        service = PatrolService(
            bot=bot,
            settings=settings,
            session_factory=self.session_factory,
        )
        return service, bot

    async def test_patrol_mutes_warns_and_persists_challenges(self) -> None:
        await self._add_member(11, full_name="正常用户")
        await self._add_member(12, full_name="卖广告的", username="ad_guy")
        await self._add_member(13, full_name="Bot 用户", is_bot=True)

        service, bot = self._service()

        async def fake_screen(session, moderation, *, group_id, user_id, profile_text):
            if user_id == 12:
                return True, "昵称含广告", True
            return False, "", True

        with patch(
            "bot.services.patrol.screen_member_profile_verbose",
            side_effect=fake_screen,
        ):
            summary = await service.run_group_patrol(-100)

        self.assertEqual(summary["status"], "completed")
        self.assertEqual(summary["scanned"], 2)
        self.assertEqual(summary["violations"], 1)

        # Violator is fully muted, warning posted with the shared button.
        bot.restrict_chat_member.assert_awaited_once()
        self.assertEqual(bot.restrict_chat_member.await_args.args[1], 12)
        bot.send_message.assert_awaited_once()
        sent_text = bot.send_message.await_args.args[1]
        self.assertIn("@ad_guy", sent_text)
        self.assertIn("资料巡检警告", sent_text)
        keyboard = bot.send_message.await_args.kwargs["reply_markup"]
        self.assertEqual(
            keyboard.inline_keyboard[0][0].callback_data, PATROL_VERIFY_CALLBACK_DATA
        )

        async with self.session_factory() as session:
            record = await get_join_verification(session, -100, 12)
            self.assertIsNotNone(record)
            self.assertEqual(record.kind, VERIFICATION_KIND_PATROL)
            self.assertEqual(record.prompt_message_id, 555)
            # Clean member has no record but a cached patrol pass.
            self.assertIsNone(await get_join_verification(session, -100, 11))
            self.assertTrue(await get_patrol_screen_hash(session, -100, 11))
            run = await session.get(PatrolRun, -100)
            self.assertIsNotNone(run)
            self.assertEqual(run.last_violations, 1)
            self.assertFalse(run.running)

    async def test_patrol_preparation_is_durable_before_mute(self) -> None:
        service, bot = self._service()

        async def assert_prepared(_chat_id: int, user_id: int, **_kwargs):
            async with self.session_factory() as session:
                record = await get_join_verification(session, -100, user_id)
                self.assertIsNotNone(record)
                self.assertEqual(record.status, "preparing")
            return True

        bot.restrict_chat_member.side_effect = assert_prepared
        enforced = await service._enforce_violators(
            -100,
            [PatrolViolator(14, "准备中用户", "preparing", "违规")],
        )

        self.assertEqual([item.user_id for item in enforced], [14])
        async with self.session_factory() as session:
            record = await get_join_verification(session, -100, 14)
            self.assertEqual(record.status, "pending")

    async def test_deauthorization_mid_batch_stops_patrol_and_restores_mute(self) -> None:
        from bot.services.authz import deauthorize_group

        service, bot = self._service()
        calls = 0

        async def deauthorize_after_first_mute(*_args, **_kwargs):
            nonlocal calls
            calls += 1
            if calls == 1:
                async with self.session_factory() as session:
                    await deauthorize_group(session, -100)
                    await session.commit()
            return True

        bot.restrict_chat_member.side_effect = deauthorize_after_first_mute
        enforced = await service._enforce_violators(
            -100,
            [
                PatrolViolator(17, "首个用户", "first", "违规"),
                PatrolViolator(18, "第二用户", "second", "违规"),
            ],
        )

        self.assertEqual(enforced, [])
        self.assertEqual(bot.restrict_chat_member.await_count, 2)
        self.assertEqual(
            [item.args[1] for item in bot.restrict_chat_member.await_args_list],
            [17, 17],
        )
        bot.send_message.assert_not_awaited()
        async with self.session_factory() as session:
            self.assertIsNone(await get_join_verification(session, -100, 17))
            self.assertIsNone(await get_join_verification(session, -100, 18))

    async def test_cancelled_patrol_prompt_restores_and_removes_preparation(self) -> None:
        service, bot = self._service()
        bot.send_message.side_effect = asyncio.CancelledError()

        with self.assertRaises(asyncio.CancelledError):
            await service._enforce_violators(
                -100,
                [PatrolViolator(15, "取消用户", "cancelled", "违规")],
            )

        self.assertEqual(bot.restrict_chat_member.await_count, 2)
        async with self.session_factory() as session:
            self.assertIsNone(await get_join_verification(session, -100, 15))

    async def test_patrol_activation_failure_restores_and_removes_preparation(self) -> None:
        service, bot = self._service()
        with patch(
            "bot.services.patrol.activate_prepared_join_verification",
            new=AsyncMock(return_value=False),
        ):
            enforced = await service._enforce_violators(
                -100,
                [PatrolViolator(16, "激活失败", "activate_fail", "违规")],
            )

        self.assertEqual(enforced, [])
        self.assertEqual(bot.restrict_chat_member.await_count, 2)
        async with self.session_factory() as session:
            self.assertIsNone(await get_join_verification(session, -100, 16))

    async def test_second_patrol_skips_cached_profiles(self) -> None:
        await self._add_member(21, full_name="正常用户")
        service, _bot = self._service()
        screen = AsyncMock(return_value=(False, "", True))
        with patch(
            "bot.services.patrol.screen_member_profile_verbose", new=screen
        ):
            first = await service.run_group_patrol(-100)
            second = await service.run_group_patrol(-100)
        self.assertEqual(first["scanned"], 1)
        self.assertEqual(second["scanned"], 0)
        self.assertEqual(screen.await_count, 1)

    async def test_inconclusive_verdict_is_not_cached(self) -> None:
        await self._add_member(22, full_name="模糊用户")
        service, _bot = self._service()
        screen = AsyncMock(return_value=(False, "", False))
        with patch(
            "bot.services.patrol.screen_member_profile_verbose", new=screen
        ):
            await service.run_group_patrol(-100)
            await service.run_group_patrol(-100)
        # Not cached, so the second run screens again.
        self.assertEqual(screen.await_count, 2)

    async def test_admins_and_pending_challenges_are_skipped(self) -> None:
        settings = _settings(super_admin_id=42)
        await self._add_member(42, full_name="超管")
        await self._add_member(43, full_name="群管理员")
        await self._add_member(44, full_name="已有质询")
        service, bot = self._service(settings)
        bot.get_chat_administrators.return_value = [
            SimpleNamespace(user=SimpleNamespace(id=43))
        ]
        async with self.session_factory() as session:
            await upsert_join_verification(
                session,
                group_id=-100,
                user_id=44,
                deadline_at=now_shanghai_naive() + timedelta(minutes=5),
                kind=VERIFICATION_KIND_MODERATION,
            )
            await session.commit()

        screen = AsyncMock(return_value=(True, "违规", True))
        with patch(
            "bot.services.patrol.screen_member_profile_verbose", new=screen
        ):
            summary = await service.run_group_patrol(-100)
        self.assertEqual(summary["scanned"], 0)
        self.assertEqual(summary["violations"], 0)
        screen.assert_not_awaited()
        bot.restrict_chat_member.assert_not_awaited()

    async def test_patrol_requires_verification_service(self) -> None:
        settings = _settings(join_verification_turnstile_secret_key="")
        await self._add_member(51, full_name="卖广告的")
        service, bot = self._service(settings)
        with patch(
            "bot.services.patrol.screen_member_profile_verbose",
            new=AsyncMock(return_value=(True, "违规", True)),
        ):
            summary = await service.run_group_patrol(-100)
        self.assertEqual(summary["status"], "verification_unavailable")
        bot.restrict_chat_member.assert_not_awaited()

    async def test_batching_covers_all_members(self) -> None:
        settings = _settings(patrol_batch_size=10)
        for user_id in range(100, 125):
            await self._add_member(user_id, full_name=f"用户{user_id}")
        service, _bot = self._service(settings)
        screen = AsyncMock(return_value=(False, "", True))
        with patch(
            "bot.services.patrol.screen_member_profile_verbose", new=screen
        ):
            summary = await service.run_group_patrol(-100)
        self.assertEqual(summary["scanned"], 25)

    async def test_mute_failure_skips_violator(self) -> None:
        await self._add_member(61, full_name="违规但禁言失败")
        service, bot = self._service()
        bot.restrict_chat_member.return_value = False
        with patch(
            "bot.services.patrol.screen_member_profile_verbose",
            new=AsyncMock(return_value=(True, "违规", True)),
        ):
            summary = await service.run_group_patrol(-100)
        self.assertEqual(summary["violations"], 0)
        bot.send_message.assert_not_awaited()
        async with self.session_factory() as session:
            self.assertIsNone(await get_join_verification(session, -100, 61))

    async def test_warning_failure_restores_permissions(self) -> None:
        await self._add_member(62, full_name="违规")
        service, bot = self._service()
        bot.send_message.side_effect = Exception("telegram down")
        with patch(
            "bot.services.patrol.screen_member_profile_verbose",
            new=AsyncMock(return_value=(True, "违规", True)),
        ):
            summary = await service.run_group_patrol(-100)
        self.assertEqual(summary["violations"], 0)
        # Muted, then restored after the warning could not be posted.
        self.assertGreaterEqual(bot.restrict_chat_member.await_count, 2)
        async with self.session_factory() as session:
            self.assertIsNone(await get_join_verification(session, -100, 62))

    async def test_manual_trigger_rejects_concurrent_run(self) -> None:
        service, _bot = self._service()
        lock = service._group_locks.setdefault(-100, __import__("asyncio").Lock())
        await lock.acquire()
        try:
            result = service.start_manual_patrol(-100)
            self.assertFalse(result["started"])
        finally:
            lock.release()

    async def test_run_once_fires_only_due_and_enabled_groups(self) -> None:
        settings = _settings(patrol_schedule_time="00:00")
        service, _bot = self._service(settings)
        service.run_group_patrol = AsyncMock(return_value={"status": "completed"})
        await service.run_once()
        service.run_group_patrol.assert_awaited_once_with(-100)

        # A completed run today suppresses re-firing.
        service.run_group_patrol.reset_mock()
        async with self.session_factory() as session:
            session.add(
                PatrolRun(group_id=-100, last_run_at=now_shanghai_naive())
            )
            await session.commit()
        await service.run_once()
        service.run_group_patrol.assert_not_awaited()


class PatrolCallbackTests(_DbTestCase):
    def _callback(self, operator_id: int) -> SimpleNamespace:
        bot = SimpleNamespace(
            me=AsyncMock(return_value=SimpleNamespace(username="my_bot")),
        )
        return SimpleNamespace(
            data=PATROL_VERIFY_CALLBACK_DATA,
            from_user=SimpleNamespace(id=operator_id),
            message=SimpleNamespace(
                message_id=555,
                chat=SimpleNamespace(id=-100, type="supergroup"),
            ),
            bot=bot,
            answer=AsyncMock(),
        )

    async def _add_patrol_record(self, user_id: int) -> None:
        async with self.session_factory() as session:
            await upsert_join_verification(
                session,
                group_id=-100,
                user_id=user_id,
                deadline_at=now_shanghai_naive() + timedelta(minutes=10),
                kind=VERIFICATION_KIND_PATROL,
                reason="资料命中群规",
                display_name="违规者",
                prompt_message_id=555,
            )
            await session.commit()

    async def test_violator_gets_private_deep_link(self) -> None:
        await self._add_patrol_record(70)
        callback = self._callback(70)
        async with self.session_factory() as session:
            await membership.on_patrol_verify_callback(
                callback, session=session, settings=_settings()
            )
        callback.answer.assert_awaited_once_with(
            url="https://t.me/my_bot?start=verify_n100"
        )

    async def test_non_violator_is_rejected(self) -> None:
        await self._add_patrol_record(70)
        callback = self._callback(71)
        async with self.session_factory() as session:
            await membership.on_patrol_verify_callback(
                callback, session=session, settings=_settings()
            )
        callback.answer.assert_awaited_once_with(
            "仅被点名的违规成员可点击", show_alert=True
        )

    async def test_join_kind_record_does_not_authorize_patrol_button(self) -> None:
        async with self.session_factory() as session:
            await upsert_join_verification(
                session,
                group_id=-100,
                user_id=72,
                deadline_at=now_shanghai_naive() + timedelta(minutes=10),
                kind="join",
            )
            await session.commit()
        callback = self._callback(72)
        async with self.session_factory() as session:
            await membership.on_patrol_verify_callback(
                callback, session=session, settings=_settings()
            )
        callback.answer.assert_awaited_once_with(
            "仅被点名的违规成员可点击", show_alert=True
        )


class PatrolTimeoutTests(_DbTestCase):
    async def test_expired_patrol_kicks_without_ban(self) -> None:
        bot = _bot_mock()
        settings = _settings()
        async with self.session_factory() as session:
            await upsert_join_verification(
                session,
                group_id=-100,
                user_id=80,
                deadline_at=now_shanghai_naive() - timedelta(minutes=10),
                kind=VERIFICATION_KIND_PATROL,
                reason="资料命中群规",
                display_name="超时者",
                prompt_message_id=555,
            )
            await session.commit()

        sweeper = JoinVerificationSweeper(
            bot=bot,
            session_factory=self.session_factory,
            settings=settings,
        )
        handled = await sweeper.sweep_once()
        self.assertEqual(handled, 1)

        # Direct removal preserves history and creates no durable ban.
        bot.ban_chat_member.assert_not_awaited()
        bot.unban_chat_member.assert_awaited_once_with(
            -100,
            80,
            only_if_banned=False,
        )
        async with self.session_factory() as session:
            self.assertIsNone(await get_join_verification(session, -100, 80))
            warning = await session.scalar(
                select(UserWarning).where(
                    UserWarning.group_id == -100, UserWarning.user_id == 80
                )
            )
            self.assertIsNone(warning)
        # The shared warning prompt is never edited away; a separate notice is sent.
        bot.edit_message_text.assert_not_awaited()
        bot.send_message.assert_awaited_once()
        self.assertIn("已移出群聊", bot.send_message.await_args.args[1])

    async def test_patrol_hash_storage_roundtrip(self) -> None:
        await self._add_member(90, full_name="缓存用户")
        async with self.session_factory() as session:
            await mark_patrol_screened(session, -100, 90, patrol_hash="abc123")
            await session.commit()
        async with self.session_factory() as session:
            self.assertEqual(
                await get_patrol_screen_hash(session, -100, 90), "abc123"
            )
            # Cache is per-group: another group sees no pass.
            self.assertEqual(await get_patrol_screen_hash(session, -200, 90), "")
            # The message-middleware profile cache is untouched.
            row = await session.get(UserProfileScreen, (-100, 90))
            self.assertIsNone(row)


class PatrolWebAPITests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        import hashlib
        import hmac
        import json
        import time
        from urllib.parse import urlencode

        from aiohttp.test_utils import TestClient, TestServer

        from bot.config import Settings
        from bot.services.runtime_config import RuntimeConfigManager
        from bot.services.verify_web import VerifyWebServer
        from bot.services.authz import authorize_group
        from bot.services.patrol import init_patrol_service

        self._bot_token = "42:TEST_TOKEN"

        def signed_init_data(user_id: int) -> str:
            pairs = {
                "auth_date": str(int(time.time())),
                "query_id": "AAF-patrol-test",
                "user": json.dumps(
                    {"id": user_id, "first_name": "Owner"}, separators=(",", ":")
                ),
            }
            check_string = "\n".join(
                f"{key}={value}" for key, value in sorted(pairs.items())
            )
            secret = hmac.new(
                b"WebAppData", self._bot_token.encode(), hashlib.sha256
            ).digest()
            pairs["hash"] = hmac.new(
                secret, check_string.encode(), hashlib.sha256
            ).hexdigest()
            return urlencode(pairs)

        self._signed_init_data = signed_init_data

        fd, self._db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.engine, self.session_factory = await init_db(
            f"sqlite+aiosqlite:///{self._db_path}"
        )
        self.settings = Settings(
            _env_file=None,
            bot_token=self._bot_token,
            super_admin_id=42,
            config_master_key="patrol-web-test-key",
        )
        self.settings.bot.token = self._bot_token
        self.settings.miniapp_public_base_url = "https://bot.example.com"
        self.settings.miniapp_listen_host = "127.0.0.1"
        self.settings.miniapp_listen_port = 8480
        self.settings.join_verification_public_base_url = (
            self.settings.miniapp_public_base_url
        )
        self.settings.join_verification_turnstile_site_key = "site-key"
        self.settings.join_verification_turnstile_secret_key = "secret-key"
        self.settings.moderation.enabled = True
        self.manager = RuntimeConfigManager(
            session_factory=self.session_factory,
            settings=self.settings,
            legacy_config_path="/tmp/nonexistent-patrol-web.toml",
            legacy_raw_env={},
        )
        await self.manager.initialize()
        # initialize() re-applies the stored (default) runtime config onto
        # settings; restore the verification config the patrol gate needs.
        self.settings.join_verification_turnstile_site_key = "site-key"
        self.settings.join_verification_turnstile_secret_key = "secret-key"
        self.settings.join_verification_public_base_url = (
            self.settings.miniapp_public_base_url
        )
        self.settings.moderation.enabled = True

        async with self.session_factory() as session:
            await authorize_group(session, -100, 42)
            await session.commit()

        self.patrol = PatrolService(
            bot=_bot_mock(),
            settings=self.settings,
            session_factory=self.session_factory,
        )
        init_patrol_service(self.patrol)

        self.bot = SimpleNamespace(
            token=self._bot_token,
            ban_chat_member=AsyncMock(),
            unban_chat_member=AsyncMock(),
            restrict_chat_member=AsyncMock(),
        )
        server = VerifyWebServer(
            bot=self.bot,
            settings=self.settings,
            session_factory=self.session_factory,
            runtime_config=self.manager,
        )
        self.client = TestClient(TestServer(server.build_app()))
        await self.client.start_server()

    async def asyncTearDown(self) -> None:
        from bot.services.patrol import init_patrol_service

        init_patrol_service(None)
        await self.client.close()
        await self.engine.dispose()
        for suffix in ("", "-wal", "-shm"):
            try:
                os.remove(f"{self._db_path}{suffix}")
            except OSError:
                pass

    def _headers(self, user_id: int = 42) -> dict[str, str]:
        return {"Authorization": f"tma {self._signed_init_data(user_id)}"}

    async def test_patrol_status_endpoint(self) -> None:
        response = await self.client.get(
            "/api/v1/groups/-100/patrol", headers=self._headers()
        )
        self.assertEqual(response.status, 200)
        payload = (await response.json())["patrol"]
        self.assertFalse(payload["running"])
        self.assertIsNone(payload["last_run_at"])
        self.assertIn("enabled", payload)

    async def test_manual_trigger_starts_patrol(self) -> None:
        self.patrol.run_group_patrol = AsyncMock(
            return_value={"status": "completed", "scanned": 0, "violations": 0}
        )
        response = await self.client.post(
            "/api/v1/groups/-100/patrol", headers=self._headers()
        )
        self.assertEqual(response.status, 200)
        payload = await response.json()
        self.assertTrue(payload["patrol"]["started"])
        # Let the fire-and-forget task run.
        import asyncio as _asyncio

        await _asyncio.sleep(0)
        self.patrol.run_group_patrol.assert_awaited_once_with(-100)

    async def test_manual_trigger_conflicts_while_running(self) -> None:
        import asyncio as _asyncio

        lock = self.patrol._group_locks.setdefault(-100, _asyncio.Lock())
        await lock.acquire()
        try:
            response = await self.client.post(
                "/api/v1/groups/-100/patrol", headers=self._headers()
            )
            self.assertEqual(response.status, 409)
        finally:
            lock.release()

    async def test_manual_trigger_requires_group_access(self) -> None:
        response = await self.client.post(
            "/api/v1/groups/-100/patrol", headers=self._headers(user_id=7)
        )
        self.assertEqual(response.status, 403)

    async def test_manual_trigger_requires_verification_config(self) -> None:
        self.settings.join_verification_turnstile_secret_key = ""
        response = await self.client.post(
            "/api/v1/groups/-100/patrol", headers=self._headers()
        )
        self.assertEqual(response.status, 400)
        body = await response.json()
        self.assertEqual(
            body["error"]["code"], "verification_provider_unavailable"
        )

    async def test_group_patrol_enabled_override(self) -> None:
        groups = await self.client.get("/api/v1/groups", headers=self._headers())
        document = next(
            group
            for group in (await groups.json())["groups"]
            if int(group["id"]) == -100
        )
        self.assertIsNone(document["settings"]["patrol_enabled"])

        response = await self.client.put(
            "/api/v1/groups/-100/settings",
            headers=self._headers(),
            json={
                "revision": document["revision"],
                "settings": {"patrol_enabled": True},
            },
        )
        self.assertEqual(response.status, 200)
        saved = (await response.json())["group"]
        self.assertTrue(saved["settings"]["patrol_enabled"])

        # Clearing restores inherit-global (null).
        response = await self.client.put(
            "/api/v1/groups/-100/settings",
            headers=self._headers(),
            json={
                "revision": saved["revision"],
                "settings": {"patrol_enabled": None},
            },
        )
        self.assertEqual(response.status, 200)
        cleared = (await response.json())["group"]
        self.assertIsNone(cleared["settings"]["patrol_enabled"])


if __name__ == "__main__":
    unittest.main()
