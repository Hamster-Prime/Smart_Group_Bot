import asyncio
import os
import tempfile
import unittest
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from sqlalchemy import select

from bot.db.engine import init_db
from bot.db.models import Group, UserWarning
from bot.handlers import membership
from bot.services.join_verification import (
    RAID_VERIFY_CALLBACK_DATA,
    VERIFICATION_KIND_MODERATION,
    VERIFICATION_KIND_RAID,
    JoinVerificationSweeper,
    get_join_verification,
    upsert_join_verification,
    verification_timeout_seconds_for_kind,
)
from bot.services.raid_guard import (
    MANUAL_LOCKDOWN_SETTINGS_KEY,
    RAID_REMOVE_CALLBACK_DATA,
    RaidGuardService,
    RaidSuspect,
    build_raid_challenge_keyboard,
    build_raid_challenge_text,
    build_raid_lockdown_text,
    build_raid_unlock_text,
    normalize_manual_lockdown_minutes,
    raid_guard_policy,
    resolve_raid_guard_config,
)
from bot.utils.timezone import now_shanghai_naive


def _settings(**overrides):
    from bot.config import Settings

    settings = Settings(_env_file=None)
    settings.join_verification_turnstile_site_key = "site-key"
    settings.join_verification_turnstile_secret_key = "secret-key"
    settings.join_verification_public_base_url = "https://verify.example.com"
    settings.raid_guard_enabled = True
    settings.raid_guard_join_threshold = 3
    settings.raid_guard_window_seconds = 60
    settings.raid_guard_lockdown_seconds = 600
    settings.raid_guard_lookback_seconds = 300
    settings.raid_guard_challenge_timeout_seconds = 600
    for key, value in overrides.items():
        setattr(settings, key, value)
    return settings


def _bot_mock() -> SimpleNamespace:
    return SimpleNamespace(
        restrict_chat_member=AsyncMock(return_value=True),
        ban_chat_member=AsyncMock(return_value=True),
        unban_chat_member=AsyncMock(return_value=True),
        send_message=AsyncMock(return_value=SimpleNamespace(message_id=777)),
        edit_message_text=AsyncMock(),
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
        from bot.services.authz import authorize_group

        async with self.session_factory() as session:
            await authorize_group(session, -100, 1)
            await session.commit()

    async def asyncTearDown(self) -> None:
        await self.engine.dispose()
        for suffix in ("", "-wal", "-shm"):
            try:
                os.remove(f"{self._db_path}{suffix}")
            except OSError:
                pass

    def _service(self, settings=None) -> tuple[RaidGuardService, SimpleNamespace]:
        settings = settings or _settings()
        bot = _bot_mock()
        service = RaidGuardService(
            bot=bot,
            settings=settings,
            session_factory=self.session_factory,
        )
        return service, bot

    async def _join(
        self,
        service: RaidGuardService,
        user_id: int,
        *,
        group_id: int = -100,
        full_name: str = "",
        username: str = "",
        group_settings: dict | None = None,
    ) -> bool:
        return await service.handle_join(
            group_id=group_id,
            user_id=user_id,
            full_name=full_name or f"用户{user_id}",
            username=username,
            group_settings=group_settings,
        )


class PolicyTests(unittest.TestCase):
    def test_policy_group_override(self) -> None:
        settings = _settings(raid_guard_enabled=False)
        self.assertFalse(raid_guard_policy(settings, None))
        self.assertTrue(raid_guard_policy(settings, {"raid_guard_enabled": True}))
        settings_on = _settings()
        self.assertTrue(raid_guard_policy(settings_on, {}))
        self.assertFalse(
            raid_guard_policy(settings_on, {"raid_guard_enabled": False})
        )
        self.assertTrue(raid_guard_policy(settings_on, {"raid_guard_enabled": "on"}))

    def test_resolve_config_group_numeric_overrides(self) -> None:
        settings = _settings()
        config = resolve_raid_guard_config(
            settings,
            {
                "raid_guard_join_threshold": 12,
                "raid_guard_window_seconds": 30,
                "raid_guard_lockdown_seconds": 1200,
                "raid_guard_lookback_seconds": 0,
                "raid_guard_challenge_timeout_seconds": 900,
            },
        )
        self.assertEqual(config.join_threshold, 12)
        self.assertEqual(config.window_seconds, 30)
        self.assertEqual(config.lockdown_seconds, 1200)
        self.assertEqual(config.lookback_seconds, 0)
        self.assertEqual(config.challenge_timeout_seconds, 900)

    def test_resolve_config_invalid_overrides_inherit_global(self) -> None:
        settings = _settings()
        config = resolve_raid_guard_config(
            settings,
            {
                "raid_guard_join_threshold": "abc",
                "raid_guard_window_seconds": None,
                "raid_guard_lockdown_seconds": -5,
            },
        )
        self.assertEqual(config.join_threshold, 3)
        self.assertEqual(config.window_seconds, 60)
        self.assertEqual(config.lockdown_seconds, 600)

    def test_raid_timeout_prefers_raid_setting(self) -> None:
        settings = _settings(
            raid_guard_challenge_timeout_seconds=1200,
            join_verification_timeout_seconds=600,
        )
        self.assertEqual(
            verification_timeout_seconds_for_kind(settings, VERIFICATION_KIND_RAID),
            1200,
        )
        settings.raid_guard_challenge_timeout_seconds = 0
        self.assertEqual(
            verification_timeout_seconds_for_kind(settings, VERIFICATION_KIND_RAID),
            600,
        )

    def test_raid_timeout_honors_group_override(self) -> None:
        settings = _settings(raid_guard_challenge_timeout_seconds=1200)
        self.assertEqual(
            verification_timeout_seconds_for_kind(
                settings,
                VERIFICATION_KIND_RAID,
                {"raid_guard_challenge_timeout_seconds": 900},
            ),
            900,
        )
        # Invalid override inherits the global.
        self.assertEqual(
            verification_timeout_seconds_for_kind(
                settings,
                VERIFICATION_KIND_RAID,
                {"raid_guard_challenge_timeout_seconds": "abc"},
            ),
            1200,
        )


class MessageTests(unittest.TestCase):
    def test_lockdown_text_mentions_counts_and_duration(self) -> None:
        text = build_raid_lockdown_text(
            joined_count=8, window_seconds=60, lockdown_seconds=600
        )
        self.assertIn("爆破防护已触发", text)
        self.assertIn("8 名成员", text)
        self.assertIn("1 分钟", text)
        self.assertIn("10 分钟", text)
        self.assertIn("自动移出", text)

    def test_challenge_text_mentions_all_suspects(self) -> None:
        now = now_shanghai_naive()
        suspects = [
            RaidSuspect(user_id=1, full_name="张三", username="", joined_at=now),
            RaidSuspect(user_id=2, full_name="", username="spam_guy", joined_at=now),
        ]
        text = build_raid_challenge_text(suspects, timeout_seconds=600)
        self.assertIn('tg://user?id=1', text)
        self.assertIn("张三", text)
        self.assertIn("@spam_guy", text)
        self.assertIn("10 分钟", text)
        self.assertIn("真人质询", text)
        self.assertIn("不会封禁", text)

    def test_challenge_keyboard_uses_shared_callback(self) -> None:
        keyboard = build_raid_challenge_keyboard()
        button = keyboard.inline_keyboard[0][0]
        self.assertEqual(button.callback_data, RAID_VERIFY_CALLBACK_DATA)
        self.assertEqual(len(keyboard.inline_keyboard), 2)
        self.assertEqual(
            keyboard.inline_keyboard[1][0].callback_data,
            RAID_REMOVE_CALLBACK_DATA,
        )
        self.assertIn("仅管理员", keyboard.inline_keyboard[1][0].text)

    def test_unlock_text_describes_recovery(self) -> None:
        text = build_raid_unlock_text()
        self.assertIn("爆破防护已解除", text)
        self.assertIn("恢复接收新成员", text)

    def test_manual_duration_is_always_minutes(self) -> None:
        self.assertIsNone(normalize_manual_lockdown_minutes(None))
        self.assertEqual(normalize_manual_lockdown_minutes("15"), 15)
        for invalid in (0, -1, "abc", True):
            with self.assertRaises(ValueError):
                normalize_manual_lockdown_minutes(invalid)


class DetectionTests(_DbTestCase):
    async def test_below_threshold_does_not_trigger(self) -> None:
        service, bot = self._service()
        self.assertFalse(await self._join(service, 1))
        self.assertFalse(await self._join(service, 2))
        self.assertFalse(service.lockdown_active(-100))
        bot.send_message.assert_not_awaited()

    async def test_threshold_triggers_lockdown_and_challenges(self) -> None:
        service, bot = self._service()
        self.assertFalse(await self._join(service, 1, username="one"))
        self.assertFalse(await self._join(service, 2, username="two"))
        # The triggering join is consumed by the raid path.
        self.assertTrue(await self._join(service, 3, username="three"))
        self.assertTrue(service.lockdown_active(-100))

        # One lockdown notice + one challenge message.
        self.assertEqual(bot.send_message.await_count, 2)
        notice = bot.send_message.await_args_list[0].args[1]
        self.assertIn("爆破防护已触发", notice)
        challenge_call = bot.send_message.await_args_list[1]
        challenge = challenge_call.args[1]
        for handle in ("@one", "@two", "@three"):
            self.assertIn(handle, challenge)
        keyboard = challenge_call.kwargs["reply_markup"]
        self.assertEqual(
            keyboard.inline_keyboard[0][0].callback_data, RAID_VERIFY_CALLBACK_DATA
        )

        # All three are muted with pending raid records.
        self.assertEqual(bot.restrict_chat_member.await_count, 3)
        async with self.session_factory() as session:
            for user_id in (1, 2, 3):
                record = await get_join_verification(session, -100, user_id)
                self.assertIsNotNone(record)
                self.assertEqual(record.kind, VERIFICATION_KIND_RAID)
                self.assertEqual(record.prompt_message_id, 777)

    async def test_lockdown_notice_is_not_auto_deleted(self) -> None:
        from unittest.mock import patch

        settings = _settings()
        settings.bot.auto_delete_seconds = 30
        settings.bot.auto_delete_categories = ["moderation"]
        service, _bot = self._service(settings=settings)

        with patch(
            "bot.services.raid_guard.schedule_message_auto_delete",
            create=True,
        ) as schedule_mock:
            await self._join(service, 1, username="one")
            await self._join(service, 2, username="two")
            self.assertTrue(await self._join(service, 3, username="three"))

        self.assertTrue(service.lockdown_active(-100))
        # State-change notices are intentionally persistent regardless of the
        # generic moderation retention policy.
        schedule_mock.assert_not_called()

    async def test_joins_outside_window_do_not_trigger(self) -> None:
        service, _bot = self._service()
        await self._join(service, 1)
        await self._join(service, 2)
        # Age the recorded joins beyond the detection window.
        for event in service._recent_joins[-100]:
            event.at = event.at - timedelta(seconds=120)
        self.assertFalse(await self._join(service, 3))
        self.assertFalse(service.lockdown_active(-100))

    async def test_lookback_includes_pre_trigger_joiners(self) -> None:
        service, bot = self._service()
        await self._join(service, 10, username="early")
        # Early joiner is outside the trigger window but inside the lookback.
        for event in service._recent_joins[-100]:
            event.at = event.at - timedelta(seconds=90)
        await self._join(service, 1)
        await self._join(service, 2)
        self.assertTrue(await self._join(service, 3))
        challenge = bot.send_message.await_args_list[1].args[1]
        self.assertIn("@early", challenge)
        async with self.session_factory() as session:
            record = await get_join_verification(session, -100, 10)
            self.assertIsNotNone(record)
            self.assertEqual(record.kind, VERIFICATION_KIND_RAID)

    async def test_lockdown_kicks_new_joins_without_notice(self) -> None:
        service, bot = self._service()
        for user_id in (1, 2, 3):
            await self._join(service, user_id)
        bot.send_message.reset_mock()
        bot.ban_chat_member.reset_mock()
        bot.unban_chat_member.reset_mock()

        self.assertTrue(await self._join(service, 99))
        bot.ban_chat_member.assert_awaited_once_with(-100, 99)
        bot.unban_chat_member.assert_awaited_once_with(-100, 99, only_if_banned=True)
        # No extra notice during lockdown: only the original announcement.
        bot.send_message.assert_not_awaited()
        async with self.session_factory() as session:
            self.assertIsNone(await get_join_verification(session, -100, 99))

    async def test_lockdown_expires(self) -> None:
        service, _bot = self._service()
        for user_id in (1, 2, 3):
            await self._join(service, user_id)
        self.assertTrue(service.lockdown_active(-100))
        service._lockdown_until[-100] = now_shanghai_naive() - timedelta(seconds=1)
        self.assertFalse(service.lockdown_active(-100))
        # A join after expiry flows through normal handling again.
        self.assertFalse(await self._join(service, 50))

    async def test_lockdown_expiry_sends_persistent_unlock_notice(self) -> None:
        service, bot = self._service()
        for user_id in (1, 2, 3):
            await self._join(service, user_id)
        service._cancel_lockdown_timer(-100)
        expired_at = now_shanghai_naive() - timedelta(seconds=1)
        service._lockdown_until[-100] = expired_at
        bot.send_message.reset_mock()

        self.assertTrue(await service._expire_lockdown(-100, expired_at))
        self.assertFalse(service.lockdown_active(-100))
        bot.send_message.assert_awaited_once()
        self.assertIn("爆破防护已解除", bot.send_message.await_args.args[1])

    async def test_manual_lockdown_works_when_automatic_policy_is_off(self) -> None:
        settings = _settings(raid_guard_enabled=False)
        service, bot = self._service(settings)

        deadline = await service.enable_manual_lockdown(
            -100,
            duration_minutes="5",
        )
        self.assertIsNotNone(deadline)
        self.assertTrue(service.manual_lockdown_active(-100))
        self.assertIn("5 分钟", bot.send_message.await_args.args[1])

        bot.send_message.reset_mock()
        self.assertTrue(await self._join(service, 99))
        bot.ban_chat_member.assert_awaited_once_with(-100, 99)
        self.assertTrue(await service.disable_manual_lockdown(-100))
        self.assertFalse(service.lockdown_active(-100))
        self.assertIn("爆破防护已解除", bot.send_message.await_args.args[1])

    async def test_manual_lockdown_persists_without_replacing_group_settings(self) -> None:
        async with self.session_factory() as session:
            session.add(
                Group(
                    id=-100,
                    title="测试群",
                    settings={"welcome_message": "原有欢迎语", "nested": {"ok": True}},
                )
            )
            await session.commit()

        service, _bot = self._service()
        self.assertIsNone(await service.enable_manual_lockdown(-100))
        async with self.session_factory() as session:
            group = await session.get(Group, -100)
            self.assertEqual(group.settings["welcome_message"], "原有欢迎语")
            self.assertEqual(group.settings["nested"], {"ok": True})
            self.assertEqual(
                group.settings[MANUAL_LOCKDOWN_SETTINGS_KEY],
                {"version": 1, "indefinite": True},
            )

        self.assertTrue(await service.disable_manual_lockdown(-100))
        async with self.session_factory() as session:
            group = await session.get(Group, -100)
            self.assertNotIn(MANUAL_LOCKDOWN_SETTINGS_KEY, group.settings)
            self.assertEqual(group.settings["welcome_message"], "原有欢迎语")
            self.assertEqual(group.settings["nested"], {"ok": True})

    async def test_timed_manual_lockdown_is_restored_after_restart(self) -> None:
        first, _first_bot = self._service()
        deadline = await first.enable_manual_lockdown(-100, duration_minutes=5)
        self.assertIsNotNone(deadline)
        first.reset()

        restored, restored_bot = self._service()
        summary = await restored.restore_manual_lockdowns()
        self.assertEqual(summary, {"restored": 1, "expired": 0, "invalid": 0})
        self.assertTrue(restored.manual_lockdown_active(-100))
        self.assertEqual(restored.lockdown_status(-100)["until"], deadline)
        # A restart restores state silently; the original activation notice is
        # not repeated.
        restored_bot.send_message.assert_not_awaited()
        await restored.disable_manual_lockdown(-100)

    async def test_indefinite_manual_lockdown_is_restored_after_restart(self) -> None:
        first, _first_bot = self._service()
        await first.enable_manual_lockdown(-100)
        first.reset()

        restored, restored_bot = self._service()
        summary = await restored.restore_manual_lockdowns()
        self.assertEqual(summary["restored"], 1)
        self.assertTrue(restored.manual_lockdown_active(-100))
        self.assertIsNone(restored.lockdown_status(-100)["until"])
        restored_bot.send_message.assert_not_awaited()
        await restored.disable_manual_lockdown(-100)

    async def test_expired_persisted_lockdown_is_cleaned_and_announced_once(self) -> None:
        expired_until = (now_shanghai_naive() - timedelta(minutes=1)).isoformat()
        async with self.session_factory() as session:
            session.add(
                Group(
                    id=-100,
                    title="测试群",
                    settings={
                        "welcome_message": "保留",
                        MANUAL_LOCKDOWN_SETTINGS_KEY: {
                            "version": 1,
                            "until": expired_until,
                        },
                    },
                )
            )
            await session.commit()

        first, first_bot = self._service()
        self.assertEqual(
            await first.restore_manual_lockdowns(),
            {"restored": 0, "expired": 1, "invalid": 0},
        )
        first_bot.send_message.assert_awaited_once()
        self.assertIn("爆破防护已解除", first_bot.send_message.await_args.args[1])

        second, second_bot = self._service()
        self.assertEqual(
            await second.restore_manual_lockdowns(),
            {"restored": 0, "expired": 0, "invalid": 0},
        )
        second_bot.send_message.assert_not_awaited()
        async with self.session_factory() as session:
            group = await session.get(Group, -100)
            self.assertEqual(group.settings["welcome_message"], "保留")
            self.assertNotIn(MANUAL_LOCKDOWN_SETTINGS_KEY, group.settings)

    async def test_manual_timer_expiry_clears_persisted_state_before_notice(self) -> None:
        service, bot = self._service()
        deadline = await service.enable_manual_lockdown(-100, duration_minutes=1)
        self.assertIsNotNone(deadline)
        service._cancel_lockdown_timer(-100)
        bot.send_message.reset_mock()

        with patch(
            "bot.services.raid_guard.now_shanghai_naive",
            return_value=deadline + timedelta(seconds=1),
        ):
            self.assertTrue(await service._expire_lockdown(-100, deadline))

        bot.send_message.assert_awaited_once()
        self.assertIn("爆破防护已解除", bot.send_message.await_args.args[1])
        async with self.session_factory() as session:
            group = await session.get(Group, -100)
            self.assertNotIn(MANUAL_LOCKDOWN_SETTINGS_KEY, group.settings)

    async def test_disabled_group_override_bypasses_detection(self) -> None:
        service, bot = self._service()
        overrides = {"raid_guard_enabled": False}
        for user_id in (1, 2, 3, 4, 5):
            self.assertFalse(
                await self._join(service, user_id, group_settings=overrides)
            )
        self.assertFalse(service.lockdown_active(-100))
        bot.send_message.assert_not_awaited()

    async def test_group_threshold_override(self) -> None:
        service, _bot = self._service()
        overrides = {"raid_guard_join_threshold": 2}
        self.assertFalse(await self._join(service, 1, group_settings=overrides))
        self.assertTrue(await self._join(service, 2, group_settings=overrides))
        self.assertTrue(service.lockdown_active(-100))

    async def test_single_user_rejoin_loop_does_not_trigger(self) -> None:
        # One account bouncing leave/rejoin must not lock the group.
        service, bot = self._service()
        for _ in range(10):
            self.assertFalse(await self._join(service, 7))
        self.assertFalse(service.lockdown_active(-100))
        bot.send_message.assert_not_awaited()

    async def test_lockdown_kick_failure_does_not_consume_join(self) -> None:
        service, bot = self._service()
        for user_id in (1, 2, 3):
            await self._join(service, user_id)
        bot.ban_chat_member.side_effect = Exception("flood wait")
        # Kick failed: the join must fall through to the normal pipeline.
        self.assertFalse(await self._join(service, 99))

    async def test_repelled_joins_do_not_extend_lockdown(self) -> None:
        service, _bot = self._service()
        for user_id in (1, 2, 3):
            await self._join(service, user_id)
        deadline = service._lockdown_until[-100]
        await self._join(service, 99)
        self.assertEqual(service._lockdown_until[-100], deadline)

    async def test_trigger_join_not_consumed_when_challenge_fails(self) -> None:
        service, bot = self._service()

        async def send(chat_id, text, **kwargs):
            if "真人质询" in text:
                raise Exception("telegram down")
            return SimpleNamespace(message_id=777)

        bot.send_message = AsyncMock(side_effect=send)
        self.assertFalse(await self._join(service, 1))
        self.assertFalse(await self._join(service, 2))
        # The lockdown still engages, but the triggering member's challenge
        # could not be issued, so their join flows to the normal pipeline.
        self.assertFalse(await self._join(service, 3))
        self.assertTrue(service.lockdown_active(-100))

    async def test_windows_are_per_group(self) -> None:
        service, _bot = self._service()
        await self._join(service, 1, group_id=-100)
        await self._join(service, 2, group_id=-100)
        # Two joins in another group must not contribute to -100's window.
        await self._join(service, 3, group_id=-200)
        self.assertFalse(service.lockdown_active(-100))
        self.assertFalse(service.lockdown_active(-200))

    async def test_admins_banned_and_pending_are_not_challenged(self) -> None:
        # Threshold 5 so the trigger fires on the LAST join below and every
        # user is part of the wave (not repelled by an already-active lockdown).
        settings = _settings(super_admin_id=42, raid_guard_join_threshold=5)
        service, bot = self._service(settings)
        bot.get_chat_administrators.return_value = [
            SimpleNamespace(user=SimpleNamespace(id=2))
        ]
        from bot.services.join_screening import add_global_ban

        async with self.session_factory() as session:
            await add_global_ban(session, 3, reason="测试", source="manual")
            await upsert_join_verification(
                session,
                group_id=-100,
                user_id=4,
                deadline_at=now_shanghai_naive() + timedelta(minutes=5),
                kind=VERIFICATION_KIND_MODERATION,
            )
            await session.commit()

        await self._join(service, 42)  # super admin
        await self._join(service, 2)  # group admin
        await self._join(service, 3)  # globally banned
        await self._join(service, 4)  # existing moderation challenge
        await self._join(service, 5)  # the only challengeable suspect

        self.assertTrue(service.lockdown_active(-100))
        async with self.session_factory() as session:
            for user_id in (42, 2, 3):
                self.assertIsNone(await get_join_verification(session, -100, user_id))
            existing = await get_join_verification(session, -100, 4)
            self.assertEqual(existing.kind, VERIFICATION_KIND_MODERATION)
            record = await get_join_verification(session, -100, 5)
            self.assertIsNotNone(record)
            self.assertEqual(record.kind, VERIFICATION_KIND_RAID)
        # Only the challengeable suspect is muted.
        self.assertEqual(bot.restrict_chat_member.await_count, 1)
        self.assertEqual(bot.restrict_chat_member.await_args.args[1], 5)

    async def test_unavailable_verification_locks_but_skips_challenges(self) -> None:
        settings = _settings(join_verification_turnstile_secret_key="")
        service, bot = self._service(settings)
        for user_id in (1, 2, 3):
            await self._join(service, user_id)
        self.assertTrue(service.lockdown_active(-100))
        # Lockdown notice only; nobody muted without a challenge path.
        self.assertEqual(bot.send_message.await_count, 1)
        bot.restrict_chat_member.assert_not_awaited()
        async with self.session_factory() as session:
            for user_id in (1, 2, 3):
                self.assertIsNone(await get_join_verification(session, -100, user_id))

    async def test_challenge_message_failure_restores_permissions(self) -> None:
        service, bot = self._service()

        async def send(chat_id, text, **kwargs):
            if "真人质询" in text:
                raise Exception("telegram down")
            return SimpleNamespace(message_id=777)

        bot.send_message = AsyncMock(side_effect=send)
        for user_id in (1, 2, 3):
            await self._join(service, user_id)
        # Muted then restored: restrict called twice per member (mute+allow).
        self.assertEqual(bot.restrict_chat_member.await_count, 6)
        async with self.session_factory() as session:
            for user_id in (1, 2, 3):
                self.assertIsNone(await get_join_verification(session, -100, user_id))

    async def test_mute_failure_skips_suspect(self) -> None:
        service, bot = self._service()

        async def restrict(chat_id, user_id, **kwargs):
            return user_id != 2

        bot.restrict_chat_member = AsyncMock(side_effect=restrict)
        for user_id in (1, 2, 3):
            await self._join(service, user_id)
        async with self.session_factory() as session:
            self.assertIsNone(await get_join_verification(session, -100, 2))
            for user_id in (1, 3):
                self.assertIsNotNone(
                    await get_join_verification(session, -100, user_id)
                )

    async def test_chunking_splits_challenge_messages(self) -> None:
        service, bot = self._service(_settings(raid_guard_join_threshold=20))
        for user_id in range(1, 21):
            await self._join(service, user_id)
        # 1 lockdown notice + ceil(20/15)=2 challenge chunks.
        self.assertEqual(bot.send_message.await_count, 3)
        async with self.session_factory() as session:
            for user_id in range(1, 21):
                self.assertIsNotNone(
                    await get_join_verification(session, -100, user_id)
                )


class CallbackTests(_DbTestCase):
    def _callback(
        self,
        operator_id: int,
        *,
        data: str = RAID_VERIFY_CALLBACK_DATA,
    ) -> SimpleNamespace:
        bot = SimpleNamespace(
            me=AsyncMock(return_value=SimpleNamespace(username="my_bot")),
            ban_chat_member=AsyncMock(return_value=True),
            unban_chat_member=AsyncMock(return_value=True),
            edit_message_reply_markup=AsyncMock(return_value=True),
        )
        return SimpleNamespace(
            data=data,
            from_user=SimpleNamespace(id=operator_id),
            message=SimpleNamespace(
                message_id=777,
                chat=SimpleNamespace(id=-100, type="supergroup"),
            ),
            bot=bot,
            answer=AsyncMock(),
        )

    async def _add_raid_record(self, user_id: int) -> None:
        async with self.session_factory() as session:
            await upsert_join_verification(
                session,
                group_id=-100,
                user_id=user_id,
                deadline_at=now_shanghai_naive() + timedelta(minutes=10),
                kind=VERIFICATION_KIND_RAID,
                reason="爆破防护：短时间内批量加入",
                display_name="嫌疑成员",
                prompt_message_id=777,
            )
            await session.commit()

    async def test_suspect_gets_private_deep_link(self) -> None:
        await self._add_raid_record(70)
        callback = self._callback(70)
        async with self.session_factory() as session:
            await membership.on_raid_verify_callback(
                callback, session=session, settings=_settings()
            )
        callback.answer.assert_awaited_once_with(
            url="https://t.me/my_bot?start=verify_n100"
        )

    async def test_non_suspect_is_rejected(self) -> None:
        await self._add_raid_record(70)
        callback = self._callback(71)
        async with self.session_factory() as session:
            await membership.on_raid_verify_callback(
                callback, session=session, settings=_settings()
            )
        callback.answer.assert_awaited_once_with(
            "仅被点名的违规成员可点击", show_alert=True
        )

    async def test_other_kind_record_does_not_authorize_raid_button(self) -> None:
        async with self.session_factory() as session:
            await upsert_join_verification(
                session,
                group_id=-100,
                user_id=72,
                deadline_at=now_shanghai_naive() + timedelta(minutes=10),
                kind="patrol",
            )
            await session.commit()
        callback = self._callback(72)
        async with self.session_factory() as session:
            await membership.on_raid_verify_callback(
                callback, session=session, settings=_settings()
            )
        callback.answer.assert_awaited_once_with(
            "仅被点名的违规成员可点击", show_alert=True
        )

    async def test_admin_can_bulk_remove_pending_suspects_for_prompt(self) -> None:
        from unittest.mock import patch

        await self._add_raid_record(73)
        await self._add_raid_record(74)
        callback = self._callback(42, data=RAID_REMOVE_CALLBACK_DATA)
        with patch(
            "bot.handlers.membership.is_group_admin_or_higher",
            new=AsyncMock(return_value=True),
        ):
            async with self.session_factory() as session:
                await membership.on_raid_remove_callback(
                    callback,
                    session=session,
                    settings=_settings(),
                )

        self.assertEqual(callback.bot.ban_chat_member.await_count, 2)
        self.assertEqual(callback.bot.unban_chat_member.await_count, 2)
        callback.bot.edit_message_reply_markup.assert_awaited_once_with(
            chat_id=-100,
            message_id=777,
            reply_markup=None,
        )
        self.assertIn("已移除 2", callback.answer.await_args.args[0])
        async with self.session_factory() as session:
            self.assertIsNone(await get_join_verification(session, -100, 73))
            self.assertIsNone(await get_join_verification(session, -100, 74))

    async def test_non_admin_cannot_bulk_remove_suspects(self) -> None:
        from unittest.mock import patch

        await self._add_raid_record(75)
        callback = self._callback(99, data=RAID_REMOVE_CALLBACK_DATA)
        with patch(
            "bot.handlers.membership.is_group_admin_or_higher",
            new=AsyncMock(return_value=False),
        ):
            async with self.session_factory() as session:
                await membership.on_raid_remove_callback(
                    callback,
                    session=session,
                    settings=_settings(),
                )

        callback.bot.ban_chat_member.assert_not_awaited()
        self.assertIn("仅群管理员", callback.answer.await_args.args[0])
        async with self.session_factory() as session:
            self.assertIsNotNone(await get_join_verification(session, -100, 75))


class TimeoutTests(_DbTestCase):
    async def test_expired_challenge_of_banned_member_is_not_kicked(self) -> None:
        # Screening global-banned the member after the raid challenge was
        # issued; the sweeper must NOT kick (ban+unban would lift the ban).
        from bot.services.join_screening import add_global_ban

        bot = _bot_mock()
        async with self.session_factory() as session:
            await upsert_join_verification(
                session,
                group_id=-100,
                user_id=81,
                deadline_at=now_shanghai_naive() - timedelta(minutes=10),
                kind=VERIFICATION_KIND_RAID,
                prompt_message_id=777,
            )
            await add_global_ban(session, 81, reason="资料命中群规", source="join_screening")
            await session.commit()

        sweeper = JoinVerificationSweeper(
            bot=bot,
            session_factory=self.session_factory,
            settings=_settings(),
        )
        await sweeper.sweep_once()
        bot.ban_chat_member.assert_not_awaited()
        bot.unban_chat_member.assert_not_awaited()
        async with self.session_factory() as session:
            self.assertIsNone(await get_join_verification(session, -100, 81))

    async def test_expired_raid_challenge_kicks_without_ban(self) -> None:
        bot = _bot_mock()
        settings = _settings()
        async with self.session_factory() as session:
            await upsert_join_verification(
                session,
                group_id=-100,
                user_id=80,
                deadline_at=now_shanghai_naive() - timedelta(minutes=10),
                kind=VERIFICATION_KIND_RAID,
                reason="爆破防护：短时间内批量加入",
                display_name="超时者",
                prompt_message_id=777,
            )
            await session.commit()

        sweeper = JoinVerificationSweeper(
            bot=bot,
            session_factory=self.session_factory,
            settings=settings,
        )
        handled = await sweeper.sweep_once()
        self.assertEqual(handled, 1)

        # Kick = ban + unban; no group-ban row.
        bot.ban_chat_member.assert_awaited_once_with(-100, 80)
        bot.unban_chat_member.assert_awaited_once_with(-100, 80, only_if_banned=True)
        async with self.session_factory() as session:
            self.assertIsNone(await get_join_verification(session, -100, 80))
            warning = await session.scalar(
                select(UserWarning).where(
                    UserWarning.group_id == -100, UserWarning.user_id == 80
                )
            )
            self.assertIsNone(warning)
        # The shared challenge prompt is never edited away; a separate notice.
        bot.edit_message_text.assert_not_awaited()
        bot.send_message.assert_awaited_once()
        notice = bot.send_message.await_args.args[1]
        self.assertIn("爆破防护质询超时", notice)
        self.assertIn("已移出群聊", notice)

    async def test_raid_timeout_notice_honors_moderation_auto_delete(self) -> None:
        from unittest.mock import patch

        bot = _bot_mock()
        settings = _settings()
        settings.bot.auto_delete_seconds = 45
        settings.bot.auto_delete_categories = ["moderation"]
        async with self.session_factory() as session:
            await upsert_join_verification(
                session,
                group_id=-100,
                user_id=81,
                deadline_at=now_shanghai_naive() - timedelta(minutes=10),
                kind=VERIFICATION_KIND_RAID,
                reason="爆破防护：短时间内批量加入",
                display_name="超时者",
                prompt_message_id=777,
            )
            await session.commit()

        sweeper = JoinVerificationSweeper(
            bot=bot,
            session_factory=self.session_factory,
            settings=settings,
        )
        with patch(
            "bot.services.join_verification.schedule_message_auto_delete"
        ) as schedule_mock:
            self.assertEqual(await sweeper.sweep_once(), 1)

        bot.send_message.assert_awaited_once()
        schedule_mock.assert_called_once()
        sent_arg, seconds_arg = schedule_mock.call_args.args
        self.assertEqual(sent_arg.message_id, 777)
        self.assertEqual(seconds_arg, 45)


class GroupSettingsAPITests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        import hashlib
        import hmac
        import json
        import time
        from urllib.parse import urlencode

        from aiohttp.test_utils import TestClient, TestServer

        from bot.config import Settings
        from bot.services.authz import authorize_group
        from bot.services.runtime_config import RuntimeConfigManager
        from bot.services.verify_web import VerifyWebServer

        self._bot_token = "42:TEST_TOKEN"

        def signed_init_data(user_id: int) -> str:
            pairs = {
                "auth_date": str(int(time.time())),
                "query_id": "AAF-raid-test",
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
            config_master_key="raid-web-test-key",
        )
        self.settings.bot.token = self._bot_token
        self.settings.miniapp_public_base_url = "https://bot.example.com"
        self.settings.miniapp_listen_host = "127.0.0.1"
        self.settings.miniapp_listen_port = 8480
        self.manager = RuntimeConfigManager(
            session_factory=self.session_factory,
            settings=self.settings,
            legacy_config_path="/tmp/nonexistent-raid-web.toml",
            legacy_raw_env={},
        )
        await self.manager.initialize()
        # initialize() re-applies the stored (default) runtime config onto
        # settings; restore the verification config the enable gate needs.
        self.settings.join_verification_turnstile_site_key = "site-key"
        self.settings.join_verification_turnstile_secret_key = "secret-key"
        self.settings.join_verification_public_base_url = (
            self.settings.miniapp_public_base_url
        )

        async with self.session_factory() as session:
            await authorize_group(session, -100, 42)
            await session.commit()

        self.bot = SimpleNamespace(token=self._bot_token)
        server = VerifyWebServer(
            bot=self.bot,
            settings=self.settings,
            session_factory=self.session_factory,
            runtime_config=self.manager,
        )
        self.client = TestClient(TestServer(server.build_app()))
        await self.client.start_server()

    async def asyncTearDown(self) -> None:
        await self.client.close()
        await self.engine.dispose()
        for suffix in ("", "-wal", "-shm"):
            try:
                os.remove(f"{self._db_path}{suffix}")
            except OSError:
                pass

    def _headers(self, user_id: int = 42) -> dict[str, str]:
        return {"Authorization": f"tma {self._signed_init_data(user_id)}"}

    async def _group_document(self) -> dict:
        groups = await self.client.get("/api/v1/groups", headers=self._headers())
        return next(
            group
            for group in (await groups.json())["groups"]
            if int(group["id"]) == -100
        )

    async def test_runtime_config_roundtrip(self) -> None:
        document = self.manager.api_document()
        config = document["config"]
        self.assertIn("raid_guard", config)
        config["raid_guard"]["enabled"] = True
        config["raid_guard"]["join_threshold"] = 15
        config["raid_guard"]["window_seconds"] = 45
        response = await self.client.put(
            "/api/v1/settings",
            headers=self._headers(),
            json={"revision": document["revision"], "config": config},
        )
        self.assertEqual(response.status, 200)
        saved = (await response.json())["config"]["raid_guard"]
        self.assertTrue(saved["enabled"])
        self.assertEqual(saved["join_threshold"], 15)
        self.assertEqual(saved["window_seconds"], 45)
        self.assertTrue(self.settings.raid_guard_enabled)
        self.assertEqual(self.settings.raid_guard_join_threshold, 15)
        self.assertEqual(self.settings.raid_guard_window_seconds, 45)

    async def test_runtime_config_rejects_out_of_range(self) -> None:
        document = self.manager.api_document()
        config = document["config"]
        config["raid_guard"]["join_threshold"] = 1
        response = await self.client.put(
            "/api/v1/settings",
            headers=self._headers(),
            json={"revision": document["revision"], "config": config},
        )
        self.assertEqual(response.status, 400)

    async def test_group_override_roundtrip(self) -> None:
        document = await self._group_document()
        self.assertIsNone(document["settings"]["raid_guard_enabled"])
        self.assertIsNone(document["settings"]["raid_guard_join_threshold"])

        response = await self.client.put(
            "/api/v1/groups/-100/settings",
            headers=self._headers(),
            json={
                "revision": document["revision"],
                "settings": {
                    "raid_guard_enabled": True,
                    "raid_guard_join_threshold": 5,
                    "raid_guard_challenge_timeout_seconds": 900,
                },
            },
        )
        self.assertEqual(response.status, 200)
        saved = (await response.json())["group"]
        self.assertTrue(saved["settings"]["raid_guard_enabled"])
        self.assertEqual(saved["settings"]["raid_guard_join_threshold"], 5)
        self.assertEqual(
            saved["settings"]["raid_guard_challenge_timeout_seconds"], 900
        )

        # Clearing restores inherit-global (null).
        response = await self.client.put(
            "/api/v1/groups/-100/settings",
            headers=self._headers(),
            json={
                "revision": saved["revision"],
                "settings": {
                    "raid_guard_enabled": None,
                    "raid_guard_join_threshold": None,
                },
            },
        )
        self.assertEqual(response.status, 200)
        cleared = (await response.json())["group"]
        self.assertIsNone(cleared["settings"]["raid_guard_enabled"])
        self.assertIsNone(cleared["settings"]["raid_guard_join_threshold"])
        self.assertEqual(
            cleared["settings"]["raid_guard_challenge_timeout_seconds"], 900
        )

    async def test_group_override_rejects_out_of_range(self) -> None:
        document = await self._group_document()
        response = await self.client.put(
            "/api/v1/groups/-100/settings",
            headers=self._headers(),
            json={
                "revision": document["revision"],
                "settings": {"raid_guard_join_threshold": 1},
            },
        )
        self.assertEqual(response.status, 400)

    async def test_group_enable_requires_verification_config(self) -> None:
        self.settings.join_verification_turnstile_secret_key = ""
        document = await self._group_document()
        response = await self.client.put(
            "/api/v1/groups/-100/settings",
            headers=self._headers(),
            json={
                "revision": document["revision"],
                "settings": {"raid_guard_enabled": True},
            },
        )
        self.assertEqual(response.status, 400)
        body = await response.json()
        self.assertEqual(
            body["error"]["code"], "verification_provider_unavailable"
        )


if __name__ == "__main__":
    unittest.main()
