import asyncio
import html
import os
import re
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
    claim_join_verification,
    get_join_verification,
    upsert_join_verification,
    verification_timeout_seconds_for_kind,
)
from bot.services.raid_guard import (
    MANUAL_LOCKDOWN_SETTINGS_KEY,
    RAID_GUARD_PIN_OWNERSHIP_SETTINGS_KEY,
    RAID_GUARD_DISABLE_CALLBACK_DATA,
    RAID_REMOVE_CALLBACK_DATA,
    RaidGuardService,
    RaidRemovalResult,
    RaidSuspect,
    build_raid_challenge_keyboard,
    build_raid_challenge_text,
    build_raid_lockdown_keyboard,
    build_raid_lockdown_text,
    build_raid_unlock_text,
    normalize_manual_lockdown_minutes,
    raid_guard_policy,
    remove_raid_challenged_users,
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
    settings.raid_guard_pin_message = True
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
        pin_chat_message=AsyncMock(return_value=True),
        unpin_chat_message=AsyncMock(return_value=True),
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

    def test_resolve_config_includes_group_pin_override(self) -> None:
        settings = _settings(raid_guard_pin_message=True)

        self.assertTrue(resolve_raid_guard_config(settings).pin_message)
        self.assertFalse(
            resolve_raid_guard_config(
                settings,
                {"raid_guard_pin_message": False},
            ).pin_message
        )

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
        self.assertIn("<b>爆破防护 · 已触发</b>", text)
        self.assertIn("<blockquote expandable>", text)
        self.assertIn("8 名成员", text)
        self.assertIn("1 分钟", text)
        self.assertIn("10 分钟", text)
        self.assertIn("</blockquote>\n\n<b>锁定时长：10 分钟。</b>", text)
        self.assertIn("自动移出", text)

    def test_challenge_text_mentions_all_suspects(self) -> None:
        now = now_shanghai_naive()
        suspects = [
            RaidSuspect(user_id=1, full_name="张三", username="", joined_at=now),
            RaidSuspect(user_id=2, full_name="", username="spam_guy", joined_at=now),
        ]
        text = build_raid_challenge_text(suspects, timeout_seconds=600)
        self.assertIn("<b>爆破防护 · 真人质询</b>", text)
        self.assertIn("<s>已完成爆破风险识别</s>", text)
        self.assertIn("<blockquote expandable>", text)
        self.assertIn('tg://user?id=1', text)
        self.assertIn("张三", text)
        self.assertIn("@spam_guy", text)
        self.assertIn("10 分钟", text)
        self.assertIn("<b>请在 10 分钟 内完成。</b>", text)
        self.assertIn("真人质询", text)
        self.assertIn("不会封禁", text)

    def test_challenge_long_batch_stays_within_telegram_limit_without_duplicates(self) -> None:
        now = now_shanghai_naive()
        suspects = [
            RaidSuspect(
                user_id=index,
                full_name="😀" * 128,
                username="",
                joined_at=now,
            )
            for index in range(1, 9)
        ]

        text = build_raid_challenge_text(suspects, timeout_seconds=600)
        visible = html.unescape(re.sub(r"<[^>]+>", "", text))

        self.assertLessEqual(len(visible.encode("utf-16-le")) // 2, 4096)
        for suspect in suspects:
            self.assertEqual(text.count(f"tg://user?id={suspect.user_id}"), 1)

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

    def test_lockdown_keyboard_has_admin_release_action(self) -> None:
        keyboard = build_raid_lockdown_keyboard()
        button = keyboard.inline_keyboard[0][0]

        self.assertEqual(button.text, "解除爆破防护")
        self.assertEqual(button.callback_data, RAID_GUARD_DISABLE_CALLBACK_DATA)

    def test_unlock_text_describes_recovery(self) -> None:
        text = build_raid_unlock_text()
        self.assertIn("爆破防护 · 已解除", text)
        self.assertIn("<blockquote expandable>", text)
        self.assertIn("恢复接收新成员", text)

    def test_manual_duration_is_always_minutes(self) -> None:
        self.assertIsNone(normalize_manual_lockdown_minutes(None))
        self.assertEqual(normalize_manual_lockdown_minutes("15"), 15)
        for invalid in (0, -1, "abc", True):
            with self.assertRaises(ValueError):
                normalize_manual_lockdown_minutes(invalid)


class RaidTaskLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def test_reset_clears_status_pin_intents(self) -> None:
        service = RaidGuardService(
            bot=_bot_mock(),
            settings=_settings(),
            session_factory=None,  # type: ignore[arg-type]
        )
        service._status_pin_intents[-100] = True
        service._owned_pin_message_ids[-100] = {777}

        service.reset()

        self.assertEqual(service._status_pin_intents, {})
        self.assertEqual(service._owned_pin_message_ids, {})

    async def test_shutdown_unpins_active_automatic_status(self) -> None:
        bot = _bot_mock()
        service = RaidGuardService(
            bot=bot,
            settings=_settings(),
            session_factory=None,  # type: ignore[arg-type]
        )
        service._lockdown_until[-100] = now_shanghai_naive() + timedelta(minutes=1)
        service._lockdown_source[-100] = "automatic"
        service._status_message_ids[-100] = 777
        service._status_pin_intents[-100] = True

        await service.shutdown()

        bot.unpin_chat_message.assert_awaited_once_with(
            chat_id=-100,
            message_id=777,
        )

    async def test_scheduled_unlock_ignores_a_new_lockdown(self) -> None:
        bot = _bot_mock()
        service = RaidGuardService(
            bot=bot,
            settings=_settings(),
            session_factory=None,  # type: ignore[arg-type]
        )
        service._status_message_ids[-100] = 777
        service._status_pin_intents[-100] = True
        service._schedule_unlock_notice(-100, source="automatic")
        service._lockdown_until[-100] = now_shanghai_naive() + timedelta(minutes=1)
        service._lockdown_source[-100] = "manual"

        await asyncio.sleep(0)

        bot.edit_message_text.assert_not_awaited()
        bot.unpin_chat_message.assert_not_awaited()
        self.assertTrue(service.lockdown_status_message_matches(-100, 777))

    async def test_passive_expiry_is_tracked_and_drained_on_shutdown(self) -> None:
        bot = _bot_mock()
        service = RaidGuardService(
            bot=bot,
            settings=_settings(),
            session_factory=None,  # type: ignore[arg-type]
        )
        started = asyncio.Event()

        async def slow_notice(*_args, **_kwargs):
            started.set()
            await asyncio.sleep(60)

        bot.edit_message_text.side_effect = slow_notice
        service._lockdown_until[-100] = now_shanghai_naive() - timedelta(seconds=1)
        service._lockdown_source[-100] = "automatic"
        service._status_message_ids[-100] = 777

        self.assertFalse(service.lockdown_active(-100))
        await asyncio.wait_for(started.wait(), timeout=1.0)
        self.assertIn(-100, service._lockdown_expiry_tasks)
        await service.shutdown()
        self.assertEqual(service._lockdown_expiry_tasks, {})

    async def test_notice_capacity_and_shutdown_remain_bounded_when_cancel_is_ignored(
        self,
    ) -> None:
        import bot.services.raid_guard as raid_guard_module

        bot = _bot_mock()
        service = RaidGuardService(
            bot=bot,
            settings=_settings(),
            session_factory=None,  # type: ignore[arg-type]
        )
        started = asyncio.Event()
        release = asyncio.Event()
        rejected_started = asyncio.Event()

        async def stubborn_notice(*_args, **_kwargs):
            started.set()
            while not release.is_set():
                try:
                    await release.wait()
                except asyncio.CancelledError:
                    continue

        async def rejected_notice() -> None:
            rejected_started.set()

        bot.edit_message_text.side_effect = stubborn_notice
        service._status_message_ids[-100] = 777
        try:
            with (
                patch.object(raid_guard_module, "_NOTICE_CALL_CAPACITY", 1),
                patch.object(
                    raid_guard_module,
                    "_NOTICE_CALL_BACKPRESSURE_SECONDS",
                    0.01,
                ),
            ):
                service._schedule_unlock_notice(-100, source="automatic")
                await asyncio.wait_for(started.wait(), timeout=1.0)

                with self.assertRaises(raid_guard_module._NoticeCallCapacityError):
                    await raid_guard_module._bounded_notice_call(
                        rejected_notice(),
                        timeout_seconds=1.0,
                    )
                self.assertFalse(rejected_started.is_set())

                before = asyncio.get_running_loop().time()
                await service.shutdown(timeout_seconds=0.02)
                self.assertLess(
                    asyncio.get_running_loop().time() - before,
                    0.2,
                )
                self.assertEqual(service._unlock_notice_tasks, {})
                self.assertEqual(
                    len(raid_guard_module._active_notice_call_tasks()),
                    1,
                )
        finally:
            release.set()
            await raid_guard_module.flush_raid_guard_telegram_tasks(
                timeout_seconds=0.5
            )

        self.assertEqual(len(raid_guard_module._active_notice_call_tasks()), 0)

    async def test_concurrent_notice_starts_never_exceed_reserved_capacity(self) -> None:
        import bot.services.raid_guard as raid_guard_module

        release = asyncio.Event()
        capacity_reached = asyncio.Event()
        active = 0
        peak = 0
        total_started = 0

        async def stubborn_notice() -> None:
            nonlocal active, peak, total_started
            active += 1
            total_started += 1
            peak = max(peak, active)
            if active == 2:
                capacity_reached.set()
            try:
                while not release.is_set():
                    try:
                        await release.wait()
                    except asyncio.CancelledError:
                        continue
            finally:
                active -= 1

        owners: list[asyncio.Task] = []
        try:
            with (
                patch.object(raid_guard_module, "_NOTICE_CALL_CAPACITY", 2),
                patch.object(
                    raid_guard_module,
                    "_NOTICE_CALL_BACKPRESSURE_SECONDS",
                    0.02,
                ),
            ):
                owners = [
                    asyncio.create_task(
                        raid_guard_module._bounded_notice_call(
                            stubborn_notice(),
                            timeout_seconds=60.0,
                        )
                    )
                    for _ in range(8)
                ]
                await asyncio.wait_for(capacity_reached.wait(), timeout=1.0)
                await asyncio.sleep(0.05)
                self.assertEqual(total_started, 2)
                self.assertEqual(peak, 2)
                self.assertEqual(
                    len(raid_guard_module._active_notice_call_tasks()),
                    2,
                )
        finally:
            for owner in owners:
                owner.cancel()
            if owners:
                await asyncio.gather(*owners, return_exceptions=True)
            release.set()
            await raid_guard_module.flush_raid_guard_telegram_tasks(
                timeout_seconds=0.5
            )

    async def test_notice_acquire_cancel_same_tick_returns_won_permit(self) -> None:
        import bot.services.raid_guard as raid_guard_module

        class RacingSemaphore:
            def __init__(self) -> None:
                self.owner: asyncio.Task[object] | None = None
                self.acquired = 0
                self.released = 0

            async def acquire(self) -> bool:
                self.acquired += 1
                assert self.owner is not None
                asyncio.get_running_loop().call_soon(self.owner.cancel)
                return True

            def release(self) -> None:
                self.released += 1

        semaphore = RacingSemaphore()
        state = SimpleNamespace(
            semaphore=semaphore,
            lock=asyncio.Lock(),
            draining=False,
            tasks=set(),
            waiters=0,
        )

        async def never_started() -> None:
            return None

        with patch.object(
            raid_guard_module,
            "_notice_call_state",
            return_value=state,
        ):
            owner = asyncio.create_task(
                raid_guard_module._start_notice_call(never_started())
            )
            semaphore.owner = owner
            result = await asyncio.gather(owner, return_exceptions=True)

        self.assertIsInstance(result[0], asyncio.CancelledError)
        self.assertEqual(semaphore.acquired, 1)
        self.assertEqual(semaphore.released, 1)
        self.assertEqual(state.waiters, 0)

    async def test_bulk_remove_releases_read_transaction_before_telegram(self) -> None:
        import bot.services.raid_guard as raid_guard_module

        transaction_open = False
        record = SimpleNamespace(
            id=1,
            group_id=-100,
            user_id=79,
            provider="turnstile",
            reason="raid",
            display_name="suspect",
            prompt_message_id=777,
            deadline_at=now_shanghai_naive() + timedelta(minutes=5),
            kind=VERIFICATION_KIND_RAID,
            status="pending",
            lease_until=None,
        )

        async def execute(_statement):
            nonlocal transaction_open
            transaction_open = True
            return SimpleNamespace(
                scalars=lambda: SimpleNamespace(all=lambda: [record])
            )

        async def commit() -> None:
            nonlocal transaction_open
            transaction_open = False

        async def rollback() -> None:
            nonlocal transaction_open
            transaction_open = False

        async def query_true(*_args, **_kwargs) -> bool:
            nonlocal transaction_open
            transaction_open = True
            return True

        async def query_false(*_args, **_kwargs) -> bool:
            nonlocal transaction_open
            transaction_open = True
            return False

        async def assert_no_open_transaction(*_args, **_kwargs) -> bool:
            self.assertFalse(transaction_open)
            return True

        session = SimpleNamespace(
            execute=AsyncMock(side_effect=execute),
            commit=AsyncMock(side_effect=commit),
            rollback=AsyncMock(side_effect=rollback),
        )
        bot = SimpleNamespace(
            ban_chat_member=AsyncMock(side_effect=assert_no_open_transaction),
            unban_chat_member=AsyncMock(side_effect=assert_no_open_transaction),
        )
        with (
            patch.object(
                raid_guard_module,
                "is_group_authorized",
                new=AsyncMock(side_effect=query_true),
            ),
            patch.object(
                raid_guard_module,
                "is_globally_banned",
                new=AsyncMock(side_effect=query_false),
            ),
            patch.object(
                raid_guard_module,
                "claim_join_verification",
                new=AsyncMock(side_effect=query_true),
            ),
            patch.object(
                raid_guard_module,
                "renew_join_verification_lease",
                new=AsyncMock(side_effect=query_true),
            ),
            patch.object(
                raid_guard_module,
                "verification_release_blocked_by_ban",
                new=AsyncMock(side_effect=query_false),
            ),
            patch.object(
                raid_guard_module,
                "join_verification_lease_is_current",
                new=AsyncMock(side_effect=query_true),
            ),
            patch.object(
                raid_guard_module,
                "chat_member_is_present",
                new=AsyncMock(side_effect=assert_no_open_transaction),
            ),
            patch.object(
                raid_guard_module,
                "complete_leased_join_verification",
                new=AsyncMock(side_effect=query_true),
            ),
        ):
            result = await remove_raid_challenged_users(
                bot=bot,
                session=session,  # type: ignore[arg-type]
                settings=_settings(),
                group_id=-100,
                prompt_message_id=777,
            )

        self.assertEqual(result.removed_user_ids, (79,))
        bot.ban_chat_member.assert_awaited_once_with(
            -100,
            79,
            revoke_messages=True,
        )
        bot.unban_chat_member.assert_awaited_once_with(
            -100,
            79,
            only_if_banned=True,
        )


class DetectionTests(_DbTestCase):
    async def test_raid_preparation_is_durable_before_mute(self) -> None:
        service, bot = self._service()
        config = resolve_raid_guard_config(service.settings)

        async def assert_prepared(_chat_id: int, user_id: int, **_kwargs):
            async with self.session_factory() as session:
                record = await get_join_verification(session, -100, user_id)
                self.assertIsNotNone(record)
                self.assertEqual(record.status, "preparing")
            return True

        bot.restrict_chat_member.side_effect = assert_prepared
        enforced = await service._challenge_suspects(
            -100,
            [RaidSuspect(90, "准备中用户", "preparing", now_shanghai_naive())],
            config,
            "turnstile",
        )

        self.assertEqual([item.user_id for item in enforced], [90])
        async with self.session_factory() as session:
            record = await get_join_verification(session, -100, 90)
            self.assertEqual(record.status, "pending")

    async def test_deauthorization_mid_batch_stops_raid_and_restores_mute(self) -> None:
        from bot.services.authz import deauthorize_group

        service, bot = self._service()
        config = resolve_raid_guard_config(service.settings)
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
        enforced = await service._challenge_suspects(
            -100,
            [
                RaidSuspect(92, "首个用户", "first", now_shanghai_naive()),
                RaidSuspect(93, "第二用户", "second", now_shanghai_naive()),
            ],
            config,
            "turnstile",
        )

        self.assertEqual(enforced, [])
        # The chunk is intentionally muted concurrently.  Both requests may
        # already be in flight when the first one observes deauthorization;
        # every member that was muted must then receive a compensating restore.
        self.assertEqual(bot.restrict_chat_member.await_count, 4)
        self.assertCountEqual(
            [item.args[1] for item in bot.restrict_chat_member.await_args_list],
            [92, 92, 93, 93],
        )
        bot.send_message.assert_not_awaited()
        async with self.session_factory() as session:
            self.assertIsNone(await get_join_verification(session, -100, 92))
            self.assertIsNone(await get_join_verification(session, -100, 93))

    async def test_cancelled_raid_prompt_restores_and_removes_preparation(self) -> None:
        service, bot = self._service()
        config = resolve_raid_guard_config(service.settings)
        bot.send_message.side_effect = asyncio.CancelledError()

        with self.assertRaises(asyncio.CancelledError):
            await service._challenge_suspects(
                -100,
                [RaidSuspect(91, "取消用户", "cancelled", now_shanghai_naive())],
                config,
                "turnstile",
            )

        self.assertEqual(bot.restrict_chat_member.await_count, 2)
        async with self.session_factory() as session:
            self.assertIsNone(await get_join_verification(session, -100, 91))

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
        self.assertIn("爆破防护 · 已触发", notice)
        status_keyboard = bot.send_message.await_args_list[0].kwargs["reply_markup"]
        self.assertEqual(
            status_keyboard.inline_keyboard[0][0].callback_data,
            RAID_GUARD_DISABLE_CALLBACK_DATA,
        )
        bot.pin_chat_message.assert_awaited_once_with(
            chat_id=-100,
            message_id=777,
            disable_notification=True,
        )
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
        bot.ban_chat_member.assert_awaited_once_with(
            -100,
            99,
            revoke_messages=True,
        )
        bot.unban_chat_member.assert_awaited_once_with(
            -100,
            99,
            only_if_banned=True,
        )
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

    async def test_lockdown_expiry_updates_persistent_status_message(self) -> None:
        service, bot = self._service()
        for user_id in (1, 2, 3):
            await self._join(service, user_id)
        service._cancel_lockdown_timer(-100)
        expired_at = now_shanghai_naive() - timedelta(seconds=1)
        service._lockdown_until[-100] = expired_at
        bot.send_message.reset_mock()

        self.assertTrue(await service._expire_lockdown(-100, expired_at))
        self.assertFalse(service.lockdown_active(-100))
        bot.send_message.assert_not_awaited()
        bot.edit_message_text.assert_awaited_once_with(
            chat_id=-100,
            message_id=777,
            text=build_raid_unlock_text(),
            parse_mode="HTML",
            reply_markup=None,
        )
        bot.unpin_chat_message.assert_awaited_once_with(
            chat_id=-100,
            message_id=777,
        )

    async def test_automatic_publish_inflight_is_retired_by_concurrent_off(
        self,
    ) -> None:
        service, bot = self._service()
        config = resolve_raid_guard_config(service.settings)
        service._arm_lockdown(
            -100,
            duration_seconds=config.lockdown_seconds,
            source="automatic",
            pin_message=True,
        )
        send_started = asyncio.Event()
        release_send = asyncio.Event()

        async def slow_send(*_args, **_kwargs):
            send_started.set()
            await release_send.wait()
            return SimpleNamespace(message_id=777)

        bot.send_message.side_effect = slow_send
        activation = asyncio.create_task(
            service._activate_lockdown(-100, [], config)
        )
        await asyncio.wait_for(send_started.wait(), timeout=1.0)
        disable = asyncio.create_task(service.disable_manual_lockdown(-100))
        await asyncio.sleep(0)
        self.assertFalse(disable.done())

        release_send.set()
        self.assertEqual(await activation, [])
        self.assertTrue(await disable)

        self.assertFalse(service.lockdown_active(-100))
        bot.pin_chat_message.assert_awaited_once_with(
            chat_id=-100,
            message_id=777,
            disable_notification=True,
        )
        bot.unpin_chat_message.assert_awaited_once_with(
            chat_id=-100,
            message_id=777,
        )
        self.assertNotIn(-100, service._status_message_ids)
        self.assertNotIn(-100, service._status_pin_intents)

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
        bot.pin_chat_message.assert_awaited_once_with(
            chat_id=-100,
            message_id=777,
            disable_notification=True,
        )
        self.assertEqual(
            bot.send_message.await_args.kwargs["reply_markup"].inline_keyboard[0][0].text,
            "解除爆破防护",
        )

        bot.send_message.reset_mock()
        self.assertTrue(await self._join(service, 99))
        bot.ban_chat_member.assert_awaited_once_with(
            -100,
            99,
            revoke_messages=True,
        )
        bot.unban_chat_member.assert_awaited_once_with(
            -100,
            99,
            only_if_banned=True,
        )
        self.assertTrue(await service.disable_manual_lockdown(-100))
        self.assertFalse(service.lockdown_active(-100))
        bot.send_message.assert_not_awaited()
        bot.edit_message_text.assert_awaited_once_with(
            chat_id=-100,
            message_id=777,
            text=build_raid_unlock_text(),
            parse_mode="HTML",
            reply_markup=None,
        )
        bot.unpin_chat_message.assert_awaited_once_with(
            chat_id=-100,
            message_id=777,
        )

    async def test_manual_reconfiguration_updates_existing_status_message(self) -> None:
        service, bot = self._service()

        await service.enable_manual_lockdown(-100, duration_minutes=5)
        await service.enable_manual_lockdown(-100, duration_minutes=10)

        bot.send_message.assert_awaited_once()
        bot.edit_message_text.assert_awaited_once()
        edit = bot.edit_message_text.await_args
        self.assertEqual(edit.kwargs["chat_id"], -100)
        self.assertEqual(edit.kwargs["message_id"], 777)
        self.assertIn("10 分钟", edit.kwargs["text"])
        self.assertEqual(
            edit.kwargs["reply_markup"].inline_keyboard[0][0].callback_data,
            RAID_GUARD_DISABLE_CALLBACK_DATA,
        )
        self.assertTrue(service.lockdown_status_message_matches(-100, 777))

    async def test_concurrent_manual_on_then_off_cannot_leave_orphan_pin(self) -> None:
        service, bot = self._service()
        send_started = asyncio.Event()
        release_send = asyncio.Event()

        async def slow_send(*_args, **_kwargs):
            send_started.set()
            await release_send.wait()
            return SimpleNamespace(message_id=777)

        bot.send_message.side_effect = slow_send
        enable = asyncio.create_task(
            service.enable_manual_lockdown(-100, duration_minutes=5)
        )
        await asyncio.wait_for(send_started.wait(), timeout=1.0)
        disable = asyncio.create_task(service.disable_manual_lockdown(-100))
        await asyncio.sleep(0)
        self.assertFalse(disable.done())

        release_send.set()
        self.assertIsNotNone(await enable)
        self.assertTrue(await disable)

        self.assertFalse(service.lockdown_active(-100))
        bot.pin_chat_message.assert_awaited_once_with(
            chat_id=-100,
            message_id=777,
            disable_notification=True,
        )
        bot.unpin_chat_message.assert_awaited_once_with(
            chat_id=-100,
            message_id=777,
        )
        async with self.session_factory() as session:
            group = await session.get(Group, -100)
            self.assertNotIn(MANUAL_LOCKDOWN_SETTINGS_KEY, group.settings)

    async def test_two_concurrent_manual_on_calls_share_one_status_message(self) -> None:
        service, bot = self._service()
        send_started = asyncio.Event()
        release_send = asyncio.Event()

        async def slow_send(*_args, **_kwargs):
            send_started.set()
            await release_send.wait()
            return SimpleNamespace(message_id=777)

        bot.send_message.side_effect = slow_send
        first = asyncio.create_task(
            service.enable_manual_lockdown(-100, duration_minutes=5)
        )
        await asyncio.wait_for(send_started.wait(), timeout=1.0)
        second = asyncio.create_task(
            service.enable_manual_lockdown(-100, duration_minutes=10)
        )
        await asyncio.sleep(0)
        self.assertFalse(second.done())
        self.assertEqual(bot.send_message.await_count, 1)

        release_send.set()
        first_until, second_until = await asyncio.gather(first, second)

        self.assertIsNotNone(first_until)
        self.assertIsNotNone(second_until)
        self.assertGreater(second_until, first_until)
        self.assertEqual(bot.send_message.await_count, 1)
        bot.edit_message_text.assert_awaited_once()
        self.assertEqual(bot.edit_message_text.await_args.kwargs["message_id"], 777)
        self.assertIn("10 分钟", bot.edit_message_text.await_args.kwargs["text"])
        self.assertEqual(bot.pin_chat_message.await_count, 2)
        bot.unpin_chat_message.assert_not_awaited()
        self.assertEqual(service.lockdown_status(-100)["until"], second_until)
        async with self.session_factory() as session:
            group = await session.get(Group, -100)
            state = group.settings[MANUAL_LOCKDOWN_SETTINGS_KEY]
            self.assertEqual(state["status_message_id"], 777)
            self.assertTrue(state["until"].startswith(second_until.isoformat()))
        service.reset(-100)

    async def test_manual_status_id_is_persisted_before_pin(self) -> None:
        service, bot = self._service()

        async def assert_persisted_before_pin(**kwargs):
            async with self.session_factory() as session:
                group = await session.get(Group, -100)
                state = group.settings[MANUAL_LOCKDOWN_SETTINGS_KEY]
                self.assertEqual(state["status_message_id"], kwargs["message_id"])
                ownership = group.settings[
                    RAID_GUARD_PIN_OWNERSHIP_SETTINGS_KEY
                ]
                self.assertEqual(ownership["message_ids"], [kwargs["message_id"]])
            return True

        bot.pin_chat_message.side_effect = assert_persisted_before_pin
        await service.enable_manual_lockdown(-100, duration_minutes=5)

        bot.pin_chat_message.assert_awaited_once_with(
            chat_id=-100,
            message_id=777,
            disable_notification=True,
        )
        service.reset(-100)

    async def test_reconfigure_unpin_failure_is_retried_at_terminal(self) -> None:
        service, bot = self._service()
        await service.enable_manual_lockdown(-100, duration_minutes=5)
        async with self.session_factory() as session:
            group = await session.get(Group, -100)
            group.settings = {
                **dict(group.settings or {}),
                "raid_guard_pin_message": False,
            }
            await session.commit()

        bot.unpin_chat_message.side_effect = [RuntimeError("temporary"), True]
        await service.enable_manual_lockdown(-100, duration_minutes=10)

        self.assertFalse(service._status_pin_intents[-100])
        async with self.session_factory() as session:
            group = await session.get(Group, -100)
            self.assertEqual(
                group.settings[RAID_GUARD_PIN_OWNERSHIP_SETTINGS_KEY][
                    "message_ids"
                ],
                [777],
            )

        self.assertTrue(await service.disable_manual_lockdown(-100))
        self.assertEqual(bot.unpin_chat_message.await_count, 2)
        async with self.session_factory() as session:
            group = await session.get(Group, -100)
            self.assertNotIn(MANUAL_LOCKDOWN_SETTINGS_KEY, group.settings)
            self.assertNotIn(
                RAID_GUARD_PIN_OWNERSHIP_SETTINGS_KEY,
                group.settings,
            )

    async def test_delayed_cleanup_cannot_unpin_concurrent_reenable(self) -> None:
        service, bot = self._service()
        await service.enable_manual_lockdown(-100, duration_minutes=5)
        async with self.session_factory() as session:
            group = await session.get(Group, -100)
            group.settings = {
                **dict(group.settings or {}),
                "raid_guard_pin_message": False,
            }
            await session.commit()

        cleanup_started = asyncio.Event()
        release_cleanup = asyncio.Event()
        unpin_attempt = 0

        async def controlled_unpin(**_kwargs):
            nonlocal unpin_attempt
            unpin_attempt += 1
            if unpin_attempt == 1:
                raise RuntimeError("temporary")
            cleanup_started.set()
            await release_cleanup.wait()
            return True

        bot.unpin_chat_message.side_effect = controlled_unpin
        await service.enable_manual_lockdown(-100, duration_minutes=10)
        self.assertFalse(service._status_pin_intents[-100])

        # Replace the normal delayed retry with a deterministic in-flight
        # cleanup, then re-enable pinning while that exact unpin owns the
        # per-group pin-operation lock.
        service._schedule_pin_cleanup(-100, delay_seconds=0.0)
        await asyncio.wait_for(cleanup_started.wait(), timeout=1.0)
        async with self.session_factory() as session:
            group = await session.get(Group, -100)
            group.settings = {
                **dict(group.settings or {}),
                "raid_guard_pin_message": True,
            }
            await session.commit()

        previous_until = service.lockdown_status(-100)["until"]
        edit_count_before_reenable = bot.edit_message_text.await_count
        reenable = asyncio.create_task(
            service.enable_manual_lockdown(-100, duration_minutes=15)
        )

        async def wait_until_rearmed() -> None:
            while service.lockdown_status(-100)["until"] == previous_until:
                await asyncio.sleep(0)

        await asyncio.wait_for(wait_until_rearmed(), timeout=1.0)
        self.assertFalse(reenable.done())
        self.assertEqual(
            bot.edit_message_text.await_count,
            edit_count_before_reenable,
        )
        self.assertEqual(bot.pin_chat_message.await_count, 1)

        release_cleanup.set()
        await asyncio.wait_for(reenable, timeout=2.0)

        self.assertEqual(bot.unpin_chat_message.await_count, 2)
        self.assertEqual(bot.pin_chat_message.await_count, 2)
        self.assertTrue(service._status_pin_intents[-100])
        async with self.session_factory() as session:
            group = await session.get(Group, -100)
            self.assertEqual(
                group.settings[RAID_GUARD_PIN_OWNERSHIP_SETTINGS_KEY][
                    "message_ids"
                ],
                [777],
            )
        service.reset(-100)

    async def test_replacement_keeps_old_pin_ownership_until_release(self) -> None:
        service, bot = self._service()
        bot.send_message.side_effect = [
            SimpleNamespace(message_id=777),
            SimpleNamespace(message_id=888),
        ]
        await service.enable_manual_lockdown(-100, duration_minutes=5)
        bot.edit_message_text.side_effect = RuntimeError("edit failed")
        bot.unpin_chat_message.side_effect = [
            RuntimeError("temporary"),
            True,
            True,
        ]

        await service.enable_manual_lockdown(-100, duration_minutes=10)

        self.assertEqual(service._status_message_ids[-100], 888)
        async with self.session_factory() as session:
            group = await session.get(Group, -100)
            self.assertEqual(
                group.settings[RAID_GUARD_PIN_OWNERSHIP_SETTINGS_KEY][
                    "message_ids"
                ],
                [777, 888],
            )

        self.assertTrue(await service.disable_manual_lockdown(-100))
        self.assertEqual(bot.unpin_chat_message.await_count, 3)
        async with self.session_factory() as session:
            group = await session.get(Group, -100)
            self.assertNotIn(
                RAID_GUARD_PIN_OWNERSHIP_SETTINGS_KEY,
                group.settings,
            )

    async def test_terminal_unpin_failure_is_recovered_after_restart(self) -> None:
        service, bot = self._service()
        await service.enable_manual_lockdown(-100, duration_minutes=5)
        bot.unpin_chat_message.side_effect = RuntimeError("temporary")

        self.assertTrue(await service.disable_manual_lockdown(-100))
        async with self.session_factory() as session:
            group = await session.get(Group, -100)
            self.assertNotIn(MANUAL_LOCKDOWN_SETTINGS_KEY, group.settings)
            self.assertEqual(
                group.settings[RAID_GUARD_PIN_OWNERSHIP_SETTINGS_KEY][
                    "message_ids"
                ],
                [777],
            )
        service.reset(-100)

        restored, restored_bot = self._service()
        self.assertEqual(
            await restored.restore_manual_lockdowns(),
            {"restored": 0, "expired": 0, "invalid": 0},
        )
        restored_bot.unpin_chat_message.assert_awaited_once_with(
            chat_id=-100,
            message_id=777,
        )
        async with self.session_factory() as session:
            group = await session.get(Group, -100)
            self.assertNotIn(
                RAID_GUARD_PIN_OWNERSHIP_SETTINGS_KEY,
                group.settings,
            )

    async def test_automatic_pin_ownership_is_recovered_after_crash(self) -> None:
        service, _bot = self._service()
        for user_id in (1, 2, 3):
            await self._join(service, user_id)
        service.reset(-100)

        restored, restored_bot = self._service()
        self.assertEqual(
            await restored.restore_manual_lockdowns(),
            {"restored": 0, "expired": 0, "invalid": 0},
        )
        restored_bot.unpin_chat_message.assert_awaited_once_with(
            chat_id=-100,
            message_id=777,
        )
        async with self.session_factory() as session:
            group = await session.get(Group, -100)
            self.assertNotIn(
                RAID_GUARD_PIN_OWNERSHIP_SETTINGS_KEY,
                group.settings,
            )

    async def test_manual_status_persistence_failure_does_not_pin(self) -> None:
        service, bot = self._service()
        original_update = service._update_persisted_manual_state
        update_calls = 0

        async def fail_status_id_update(*args, **kwargs):
            nonlocal update_calls
            update_calls += 1
            if update_calls == 2:
                return None
            return await original_update(*args, **kwargs)

        with patch.object(
            service,
            "_update_persisted_manual_state",
            side_effect=fail_status_id_update,
        ):
            await service.enable_manual_lockdown(-100, duration_minutes=5)

        bot.pin_chat_message.assert_not_awaited()
        async with self.session_factory() as session:
            group = await session.get(Group, -100)
            state = group.settings[MANUAL_LOCKDOWN_SETTINGS_KEY]
            self.assertNotIn("status_message_id", state)
        service.reset(-100)

    async def test_unlock_edit_failure_does_not_post_a_second_status_message(self) -> None:
        service, bot = self._service()

        await service.enable_manual_lockdown(-100)
        bot.send_message.reset_mock()
        bot.edit_message_text.side_effect = Exception("message is too old")

        self.assertTrue(await service.disable_manual_lockdown(-100))
        bot.send_message.assert_not_awaited()
        bot.edit_message_text.assert_awaited_once()
        bot.unpin_chat_message.assert_awaited_once_with(
            chat_id=-100,
            message_id=777,
        )
        self.assertFalse(service.lockdown_status_message_matches(-100, 777))

    async def test_active_status_edit_failure_replaces_the_release_control(self) -> None:
        service, bot = self._service()

        bot.send_message.side_effect = [
            SimpleNamespace(message_id=777),
            SimpleNamespace(message_id=888),
        ]
        await service.enable_manual_lockdown(-100, duration_minutes=5)
        bot.edit_message_text.side_effect = Exception("message is too old")
        await service.enable_manual_lockdown(-100, duration_minutes=10)

        self.assertEqual(bot.send_message.await_count, 2)
        self.assertTrue(service.lockdown_status_message_matches(-100, 888))
        bot.unpin_chat_message.assert_awaited_once_with(
            chat_id=-100,
            message_id=777,
        )
        self.assertEqual(bot.pin_chat_message.await_count, 2)
        self.assertEqual(
            bot.pin_chat_message.await_args_list[-1].kwargs["message_id"],
            888,
        )
        replacement = bot.send_message.await_args
        self.assertIn("10 分钟", replacement.args[1])
        self.assertEqual(
            replacement.kwargs["reply_markup"].inline_keyboard[0][0].callback_data,
            RAID_GUARD_DISABLE_CALLBACK_DATA,
        )

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
                {
                    "version": 1,
                    "indefinite": True,
                    "pin_message": True,
                    "status_message_id": 777,
                },
            )

        self.assertTrue(await service.disable_manual_lockdown(-100))
        async with self.session_factory() as session:
            group = await session.get(Group, -100)
            self.assertNotIn(MANUAL_LOCKDOWN_SETTINGS_KEY, group.settings)
            self.assertEqual(group.settings["welcome_message"], "原有欢迎语")
            self.assertEqual(group.settings["nested"], {"ok": True})

    async def test_manual_lockdown_reads_and_persists_disabled_pin_override(self) -> None:
        async with self.session_factory() as session:
            session.add(
                Group(
                    id=-100,
                    title="测试群",
                    settings={"raid_guard_pin_message": False},
                )
            )
            await session.commit()

        service, bot = self._service()
        await service.enable_manual_lockdown(-100)

        bot.pin_chat_message.assert_not_awaited()
        async with self.session_factory() as session:
            group = await session.get(Group, -100)
            self.assertFalse(
                group.settings[MANUAL_LOCKDOWN_SETTINGS_KEY]["pin_message"]
            )

        service.reset()
        restored, restored_bot = self._service()
        self.assertEqual(
            await restored.restore_manual_lockdowns(),
            {"restored": 1, "expired": 0, "invalid": 0},
        )
        self.assertFalse(restored._status_pin_intents[-100])
        restored_bot.pin_chat_message.assert_not_awaited()
        self.assertTrue(await restored.disable_manual_lockdown(-100))
        bot.unpin_chat_message.assert_not_awaited()
        restored_bot.unpin_chat_message.assert_not_awaited()

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
        self.assertTrue(restored._status_pin_intents[-100])
        # A restart restores state silently; the original activation notice is
        # not repeated.
        restored_bot.send_message.assert_not_awaited()
        restored_bot.pin_chat_message.assert_awaited_once_with(
            chat_id=-100,
            message_id=777,
            disable_notification=True,
        )
        await restored.disable_manual_lockdown(-100)
        restored_bot.edit_message_text.assert_awaited_once_with(
            chat_id=-100,
            message_id=777,
            text=build_raid_unlock_text(),
            parse_mode="HTML",
            reply_markup=None,
        )
        restored_bot.unpin_chat_message.assert_awaited_once_with(
            chat_id=-100,
            message_id=777,
        )

    async def test_repeated_restore_resynchronizes_persisted_pin_intent(self) -> None:
        first, _first_bot = self._service()
        await first.enable_manual_lockdown(-100, duration_minutes=5)
        first.reset()

        restored, restored_bot = self._service()
        await restored.restore_manual_lockdowns()
        restored._status_pin_intents[-100] = False
        await restored.restore_manual_lockdowns()

        self.assertTrue(restored._status_pin_intents[-100])
        self.assertEqual(restored_bot.pin_chat_message.await_count, 2)
        restored_bot.unpin_chat_message.assert_not_awaited()

        async with self.session_factory() as session:
            group = await session.get(Group, -100)
            settings_data = dict(group.settings or {})
            manual = dict(settings_data[MANUAL_LOCKDOWN_SETTINGS_KEY])
            manual["pin_message"] = False
            settings_data[MANUAL_LOCKDOWN_SETTINGS_KEY] = manual
            group.settings = settings_data
            await session.commit()
        restored._status_pin_intents[-100] = True

        await restored.restore_manual_lockdowns()

        self.assertFalse(restored._status_pin_intents[-100])
        restored_bot.unpin_chat_message.assert_awaited_once_with(
            chat_id=-100,
            message_id=777,
        )
        async with self.session_factory() as session:
            group = await session.get(Group, -100)
            self.assertNotIn(
                RAID_GUARD_PIN_OWNERSHIP_SETTINGS_KEY,
                group.settings,
            )
        restored.reset(-100)

    async def test_indefinite_manual_lockdown_is_restored_after_restart(self) -> None:
        first, _first_bot = self._service()
        await first.enable_manual_lockdown(-100)
        first.reset()

        restored, restored_bot = self._service()
        summary = await restored.restore_manual_lockdowns()
        self.assertEqual(summary["restored"], 1)
        self.assertTrue(restored.manual_lockdown_active(-100))
        self.assertIsNone(restored.lockdown_status(-100)["until"])
        self.assertTrue(restored._status_pin_intents[-100])
        restored_bot.send_message.assert_not_awaited()
        restored_bot.pin_chat_message.assert_awaited_once_with(
            chat_id=-100,
            message_id=777,
            disable_notification=True,
        )
        await restored.disable_manual_lockdown(-100)
        restored_bot.unpin_chat_message.assert_awaited_once_with(
            chat_id=-100,
            message_id=777,
        )

    async def test_expired_persisted_lockdown_is_cleaned_and_updates_once(self) -> None:
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
                            "pin_message": True,
                            "status_message_id": 777,
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
        first_bot.send_message.assert_not_awaited()
        first_bot.edit_message_text.assert_awaited_once_with(
            chat_id=-100,
            message_id=777,
            text=build_raid_unlock_text(),
            parse_mode="HTML",
            reply_markup=None,
        )
        first_bot.unpin_chat_message.assert_awaited_once_with(
            chat_id=-100,
            message_id=777,
        )

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

        bot.send_message.assert_not_awaited()
        bot.edit_message_text.assert_awaited_once_with(
            chat_id=-100,
            message_id=777,
            text=build_raid_unlock_text(),
            parse_mode="HTML",
            reply_markup=None,
        )
        bot.unpin_chat_message.assert_awaited_once_with(
            chat_id=-100,
            message_id=777,
        )
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

    async def test_group_pin_override_disables_automatic_status_pin(self) -> None:
        service, bot = self._service()
        overrides = {"raid_guard_pin_message": False}

        for user_id in (1, 2, 3):
            await self._join(service, user_id, group_settings=overrides)

        self.assertTrue(service.lockdown_active(-100))
        bot.pin_chat_message.assert_not_awaited()

    async def test_pin_failure_does_not_block_automatic_lockdown(self) -> None:
        service, bot = self._service()
        bot.pin_chat_message.side_effect = RuntimeError("no pin permission")

        for user_id in (1, 2, 3):
            await self._join(service, user_id)

        self.assertTrue(service.lockdown_active(-100))
        self.assertEqual(bot.send_message.await_count, 2)
        bot.pin_chat_message.assert_awaited_once()

    async def test_unpin_failure_does_not_block_manual_unlock(self) -> None:
        service, bot = self._service()
        await service.enable_manual_lockdown(-100)
        bot.unpin_chat_message.side_effect = RuntimeError("no pin permission")

        self.assertTrue(await service.disable_manual_lockdown(-100))

        self.assertFalse(service.lockdown_active(-100))
        bot.edit_message_text.assert_awaited_once()
        bot.unpin_chat_message.assert_awaited_once_with(
            chat_id=-100,
            message_id=777,
        )

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
        bot.unban_chat_member.side_effect = Exception("flood wait")
        # Kick failed: the join must fall through to the normal pipeline.
        self.assertFalse(await self._join(service, 99))
        bot.ban_chat_member.assert_awaited_once_with(
            -100,
            99,
            revoke_messages=True,
        )

    async def test_lockdown_existing_non_raid_challenge_falls_through(self) -> None:
        service, bot = self._service()
        await service.enable_manual_lockdown(-100)
        async with self.session_factory() as session:
            await upsert_join_verification(
                session,
                group_id=-100,
                user_id=99,
                deadline_at=now_shanghai_naive() + timedelta(minutes=5),
                kind=VERIFICATION_KIND_MODERATION,
            )
            await session.commit()

        # RaidGuard cannot borrow/overwrite another workflow's durable row.
        # False lets membership re-enforce the existing pending challenge.
        self.assertFalse(await self._join(service, 99))
        bot.ban_chat_member.assert_not_awaited()
        async with self.session_factory() as session:
            existing = await get_join_verification(session, -100, 99)
            self.assertIsNotNone(existing)
            self.assertEqual(existing.kind, VERIFICATION_KIND_MODERATION)

    async def test_lockdown_insert_race_never_clobbers_new_moderation_challenge(self) -> None:
        import bot.services.raid_guard as raid_guard_module

        service, bot = self._service()
        await service.enable_manual_lockdown(-100)
        original_prepare = raid_guard_module.prepare_join_verification

        async def concurrent_prepare(session, **kwargs):
            # Simulate a moderation worker winning after RaidGuard's initial
            # SELECT but before its insert-if-absent.
            await upsert_join_verification(
                session,
                group_id=-100,
                user_id=100,
                deadline_at=now_shanghai_naive() + timedelta(minutes=5),
                kind=VERIFICATION_KIND_MODERATION,
            )
            await session.commit()
            return await original_prepare(session, **kwargs)

        with patch.object(
            raid_guard_module,
            "prepare_join_verification",
            side_effect=concurrent_prepare,
        ):
            self.assertFalse(await self._join(service, 100))

        bot.ban_chat_member.assert_not_awaited()
        async with self.session_factory() as session:
            existing = await get_join_verification(session, -100, 100)
            self.assertIsNotNone(existing)
            self.assertEqual(existing.kind, VERIFICATION_KIND_MODERATION)

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
            get_chat_member=AsyncMock(
                return_value=SimpleNamespace(
                    status="member",
                    can_send_messages=True,
                )
            ),
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

    async def test_background_bulk_remove_result_uses_progress_layout(self) -> None:
        callback = self._callback(42, data=RAID_REMOVE_CALLBACK_DATA)
        callback.message.answer = AsyncMock()

        await membership._publish_raid_removal_result(
            callback,
            group_id=-100,
            prompt_message_id=777,
            result=RaidRemovalResult(
                pending_count=1,
                removed_user_ids=(73,),
                failed_user_ids=(74,),
            ),
        )

        rendered = callback.message.answer.await_args.args[0]
        self.assertIn("<b>爆破防护批量移除 · 待重试</b>", rendered)
        self.assertIn("<s>已提交批量移除</s>", rendered)
        self.assertIn("<b>下一步</b>　等待后台重试", rendered)
        self.assertIn("<blockquote expandable>", rendered)
        self.assertEqual(
            callback.message.answer.await_args.kwargs["parse_mode"],
            "HTML",
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
        for user_id, ban_call, unban_call in zip(
            (73, 74),
            callback.bot.ban_chat_member.await_args_list,
            callback.bot.unban_chat_member.await_args_list,
            strict=True,
        ):
            self.assertEqual(ban_call.args, (-100, user_id))
            self.assertEqual(ban_call.kwargs, {"revoke_messages": True})
            self.assertEqual(unban_call.args, (-100, user_id))
            self.assertEqual(unban_call.kwargs, {"only_if_banned": True})
        callback.bot.edit_message_reply_markup.assert_awaited_once_with(
            chat_id=-100,
            message_id=777,
            reply_markup=None,
        )
        self.assertIn("已移除 2", callback.answer.await_args.args[0])
        async with self.session_factory() as session:
            self.assertIsNone(await get_join_verification(session, -100, 73))
            self.assertIsNone(await get_join_verification(session, -100, 74))

    async def test_cancelled_bulk_remove_keeps_lease(self) -> None:
        await self._add_raid_record(76)
        callback = self._callback(42, data=RAID_REMOVE_CALLBACK_DATA)

        async def cancelled_remove(*_args, **_kwargs):
            async with self.session_factory() as check_session:
                row = await get_join_verification(check_session, -100, 76)
                self.assertIsNotNone(row)
                self.assertEqual(row.status, "enforcing")
                self.assertIsNotNone(row.lease_until)
            raise asyncio.CancelledError()

        callback.bot.unban_chat_member.side_effect = cancelled_remove
        with patch(
            "bot.handlers.membership.is_group_admin_or_higher",
            new=AsyncMock(return_value=True),
        ):
            async with self.session_factory() as session:
                with self.assertRaises(asyncio.CancelledError):
                    await membership.on_raid_remove_callback(
                        callback,
                        session=session,
                        settings=_settings(),
                    )

        # A cancelled temporary-ban cleanup leaves the durable lease for retry.
        callback.bot.ban_chat_member.assert_awaited_once_with(
            -100,
            76,
            revoke_messages=True,
        )
        callback.bot.unban_chat_member.assert_awaited_once_with(
            -100, 76, only_if_banned=True
        )
        async with self.session_factory() as session:
            row = await get_join_verification(session, -100, 76)
            self.assertIsNotNone(row)
            self.assertEqual(row.status, "enforcing")

    async def test_deauthorization_before_bulk_kick_stops_telegram_enforcement(self) -> None:
        await self._add_raid_record(78)
        callback = self._callback(42, data=RAID_REMOVE_CALLBACK_DATA)
        authorization = AsyncMock(side_effect=[True, True, False])

        with patch(
            "bot.services.raid_guard.is_group_authorized",
            new=authorization,
        ):
            async with self.session_factory() as session:
                result = await remove_raid_challenged_users(
                    bot=callback.bot,
                    session=session,
                    settings=_settings(),
                    group_id=-100,
                    prompt_message_id=777,
                )

        self.assertEqual(result.removed_user_ids, ())
        self.assertEqual(result.failed_user_ids, (78,))
        callback.bot.ban_chat_member.assert_not_awaited()
        callback.bot.unban_chat_member.assert_not_awaited()
        async with self.session_factory() as session:
            row = await get_join_verification(session, -100, 78)
            self.assertIsNotNone(row)
            self.assertEqual(row.status, "enforcing")

    async def test_bulk_remove_keeps_keyboard_when_record_has_active_lease(self) -> None:
        await self._add_raid_record(77)
        now = now_shanghai_naive()
        async with self.session_factory() as session:
            row = await get_join_verification(session, -100, 77)
            self.assertTrue(
                await claim_join_verification(
                    session,
                    verification_id=row.id,
                    deadline_at=row.deadline_at,
                    kind=row.kind,
                    now=now,
                    expired=False,
                    lease_until=now + timedelta(minutes=2),
                )
            )
            await session.commit()

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

        callback.bot.ban_chat_member.assert_not_awaited()
        callback.bot.edit_message_reply_markup.assert_not_awaited()
        self.assertIn("移除失败", callback.answer.await_args.args[0])

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
    async def test_expired_challenge_of_banned_member_reapplies_group_ban(self) -> None:
        # A global registry row is policy, not proof that this group's remote
        # Telegram ban succeeded. Recovery must idempotently enforce it.
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
        bot.ban_chat_member.assert_awaited_once_with(
            -100,
            81,
            revoke_messages=True,
        )
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

        # Temporary ban+unban leaves no durable group-ban row.
        bot.ban_chat_member.assert_awaited_once_with(
            -100,
            80,
            revoke_messages=True,
        )
        bot.unban_chat_member.assert_awaited_once_with(
            -100,
            80,
            only_if_banned=True,
        )
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
            "bot.services.join_verification.schedule_message_auto_delete_durable",
            new=AsyncMock(return_value=True),
        ) as schedule_mock:
            self.assertEqual(await sweeper.sweep_once(), 1)

        bot.send_message.assert_awaited_once()
        schedule_mock.assert_awaited_once()
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
