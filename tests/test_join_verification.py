import os
import sqlite3
import tempfile
import unittest
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, call, patch

from sqlalchemy import select

from bot.handlers import membership
from bot.db.engine import init_db
from bot.db.models import Group, UserWarning
from bot.services.join_screening import add_global_ban, get_global_ban
from bot.services.join_verification import (
    COMBINED_VERIFICATION_PROVIDER,
    JoinVerificationSweeper,
    VERIFICATION_CALLBACK_APPROVE,
    CHALLENGE_SUBMIT_GRACE,
    VERIFICATION_CALLBACK_REJECT,
    VERIFICATION_CALLBACK_START,
    VERIFICATION_KIND_JOIN,
    VERIFICATION_KIND_MODERATION,
    ban_member,
    begin_moderation_challenge,
    build_group_prompt_keyboard,
    build_mini_app_url,
    build_private_challenge_keyboard,
    build_private_deep_link,
    build_verification_callback_data,
    claim_join_verification,
    clear_turnstile_configuration_unavailable,
    delete_join_verification,
    delete_verification_prompt,
    get_join_verification,
    get_pending_verification_for_user,
    join_verification_ready,
    join_verification_policy,
    kick_member,
    list_expired_verifications,
    maybe_send_private_verification,
    moderation_challenge_ready,
    mark_turnstile_configuration_unavailable,
    parse_private_verify_group_id,
    restrict_new_member,
    upsert_join_verification,
    verification_keys_for_provider,
    verification_service_ready,
    verification_subproviders,
)
from bot.utils.timezone import now_shanghai_naive


def _settings(**overrides):
    from bot.config import Settings

    settings = Settings(_env_file=None)
    settings.moderation.enabled = True
    settings.join_verification_enabled = True
    settings.join_verification_turnstile_site_key = "site-key"
    settings.join_verification_turnstile_secret_key = "secret-key"
    settings.join_verification_public_base_url = "https://verify.example.com"
    for key, value in overrides.items():
        setattr(settings, key, value)
    return settings


class _DbTestCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        fd, self._db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.engine, self.session_factory = await init_db(f"sqlite+aiosqlite:///{self._db_path}")
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


class HelperTests(unittest.TestCase):
    def test_ready_requires_keys_and_url(self) -> None:
        self.assertTrue(join_verification_ready(_settings()))
        self.assertFalse(join_verification_ready(_settings(join_verification_enabled=False)))
        self.assertFalse(join_verification_ready(_settings(join_verification_turnstile_site_key="")))
        self.assertFalse(join_verification_ready(_settings(join_verification_turnstile_secret_key=" ")))
        self.assertFalse(
            join_verification_ready(
                _settings(
                    join_verification_turnstile_site_key="same-key",
                    join_verification_turnstile_secret_key="same-key",
                )
            )
        )
        self.assertFalse(join_verification_ready(_settings(join_verification_public_base_url="")))

    def test_moderation_challenge_ready_does_not_require_join_feature(self) -> None:
        settings = _settings(join_verification_enabled=False)
        self.assertTrue(moderation_challenge_ready(settings))

        settings.moderation.enabled = False
        self.assertFalse(moderation_challenge_ready(settings))

    def test_group_join_verification_overrides_global_defaults(self) -> None:
        settings = _settings(join_verification_enabled=False)
        settings.join_verification_hcaptcha_site_key = "h-site"
        settings.join_verification_hcaptcha_secret_key = "h-secret"

        group_settings = {
            "join_verification_enabled": True,
            "join_verification_provider": "hcaptcha",
        }
        self.assertEqual(
            join_verification_policy(settings, group_settings),
            (True, "hcaptcha"),
        )
        self.assertTrue(join_verification_ready(settings, group_settings))
        self.assertEqual(
            verification_keys_for_provider(settings, "hcaptcha"),
            ("h-site", "h-secret"),
        )

        settings.join_verification_enabled = True
        self.assertFalse(
            join_verification_ready(
                settings,
                {"join_verification_enabled": False},
            )
        )

    def test_combined_provider_expands_to_both_base_services(self) -> None:
        self.assertEqual(
            verification_subproviders(COMBINED_VERIFICATION_PROVIDER),
            ("turnstile", "hcaptcha"),
        )
        self.assertEqual(verification_subproviders("turnstile"), ("turnstile",))
        self.assertEqual(verification_subproviders("hcaptcha"), ("hcaptcha",))

    def test_combined_provider_requires_both_key_pairs(self) -> None:
        settings = _settings(
            join_verification_provider=COMBINED_VERIFICATION_PROVIDER
        )
        # Only Turnstile keys are present: combined mode is not configured.
        self.assertFalse(verification_service_ready(settings))
        settings.join_verification_hcaptcha_site_key = "h-site"
        settings.join_verification_hcaptcha_secret_key = "h-secret"
        self.assertTrue(verification_service_ready(settings))
        self.assertTrue(join_verification_ready(settings))
        # A duplicate hCaptcha pair breaks the combined mode again.
        settings.join_verification_hcaptcha_secret_key = "h-site"
        self.assertFalse(verification_service_ready(settings))

    def test_combined_provider_blocked_when_base_service_is_blocked(self) -> None:
        settings = _settings(
            join_verification_provider=COMBINED_VERIFICATION_PROVIDER,
            join_verification_hcaptcha_site_key="h-site",
            join_verification_hcaptcha_secret_key="h-secret",
        )
        mark_turnstile_configuration_unavailable(
            settings,
            provider="hcaptcha",
            reason="invalid hCaptcha secret",
        )
        try:
            self.assertTrue(verification_service_ready(settings, "turnstile"))
            self.assertFalse(
                verification_service_ready(settings, COMBINED_VERIFICATION_PROVIDER)
            )
        finally:
            clear_turnstile_configuration_unavailable(settings, provider="hcaptcha")
        self.assertTrue(
            verification_service_ready(settings, COMBINED_VERIFICATION_PROVIDER)
        )

    def test_provider_runtime_blocks_are_isolated(self) -> None:
        settings = _settings()
        settings.join_verification_hcaptcha_site_key = "h-site"
        settings.join_verification_hcaptcha_secret_key = "h-secret"
        mark_turnstile_configuration_unavailable(
            settings,
            provider="hcaptcha",
            reason="invalid hCaptcha secret",
        )
        try:
            self.assertTrue(verification_service_ready(settings, "turnstile"))
            self.assertFalse(verification_service_ready(settings, "hcaptcha"))
        finally:
            clear_turnstile_configuration_unavailable(
                settings,
                provider="hcaptcha",
            )

    def test_mini_app_url_strips_trailing_slash(self) -> None:
        settings = _settings(join_verification_public_base_url="https://verify.example.com/")
        self.assertEqual(build_mini_app_url(settings), "https://verify.example.com/verify")
        self.assertEqual(
            build_mini_app_url(settings, "hcaptcha"),
            "https://verify.example.com/verify?provider=hcaptcha",
        )
        self.assertEqual(
            build_mini_app_url(settings, "turnstile", 7),
            "https://verify.example.com/verify?provider=turnstile&verification_id=7",
        )

    def test_deep_link_and_group_keyboard(self) -> None:
        self.assertEqual(
            build_private_deep_link("@my_bot"),
            "https://t.me/my_bot?start=verify",
        )
        keyboard = build_group_prompt_keyboard(123456)
        self.assertEqual(len(keyboard.inline_keyboard), 2)
        self.assertEqual(
            keyboard.inline_keyboard[0][0].callback_data,
            "jv:v:123456",
        )
        self.assertEqual(
            keyboard.inline_keyboard[1][0].callback_data,
            "jv:a:123456",
        )
        self.assertEqual(
            keyboard.inline_keyboard[1][1].callback_data,
            "jv:r:123456",
        )
        self.assertEqual(parse_private_verify_group_id("verify_n100123"), -100123)
        self.assertEqual(parse_private_verify_group_id("verify_p42"), 42)
        self.assertIsNone(parse_private_verify_group_id("verify_bad"))

    def test_private_keyboard_uses_web_app_button(self) -> None:
        keyboard = build_private_challenge_keyboard(_settings())
        button = keyboard.inline_keyboard[0][0]
        self.assertIsNone(getattr(button, "url", None))
        self.assertEqual(button.web_app.url, "https://verify.example.com/verify")


class TelegramEnforcementHelperTests(unittest.IsolatedAsyncioTestCase):
    async def test_false_bot_api_results_are_treated_as_failures(self) -> None:
        bot = SimpleNamespace(
            restrict_chat_member=AsyncMock(return_value=False),
            ban_chat_member=AsyncMock(return_value=False),
            unban_chat_member=AsyncMock(return_value=True),
        )

        self.assertFalse(await restrict_new_member(bot, -100, 42))
        self.assertFalse(await ban_member(bot, -100, 42))
        self.assertFalse(await kick_member(bot, -100, 42))
        bot.unban_chat_member.assert_not_awaited()

    async def test_prompt_delete_failure_still_removes_live_keyboard(self) -> None:
        bot = SimpleNamespace(
            delete_message=AsyncMock(side_effect=RuntimeError("cannot delete")),
            edit_message_reply_markup=AsyncMock(return_value=True),
        )

        self.assertTrue(await delete_verification_prompt(bot, -100, 77))
        bot.edit_message_reply_markup.assert_awaited_once_with(
            chat_id=-100,
            message_id=77,
            reply_markup=None,
        )


class VerificationStoreTests(_DbTestCase):
    async def test_upsert_get_delete_roundtrip(self) -> None:
        async with self.session_factory() as session:
            self.assertIsNone(await get_join_verification(session, -100, 1))
            await upsert_join_verification(
                session,
                group_id=-100,
                user_id=1,
                deadline_at=now_shanghai_naive() + timedelta(minutes=5),
                display_name="新人",
                prompt_message_id=42,
                provider="hcaptcha",
            )
            await session.commit()

            row = await get_join_verification(session, -100, 1)
            self.assertEqual(row.display_name, "新人")
            self.assertEqual(row.prompt_message_id, 42)
            self.assertEqual(row.provider, "hcaptcha")

            self.assertTrue(await delete_join_verification(session, -100, 1))
            await session.commit()
            self.assertIsNone(await get_join_verification(session, -100, 1))
            self.assertFalse(await delete_join_verification(session, -100, 1))

    async def test_rejoin_upsert_resets_deadline(self) -> None:
        async with self.session_factory() as session:
            old_deadline = now_shanghai_naive()
            await upsert_join_verification(
                session,
                group_id=-100,
                user_id=2,
                deadline_at=old_deadline,
            )
            await session.commit()

            new_deadline = now_shanghai_naive() + timedelta(minutes=5)
            await upsert_join_verification(
                session,
                group_id=-100,
                user_id=2,
                deadline_at=new_deadline,
                prompt_message_id=99,
            )
            await session.commit()

            row = await get_join_verification(session, -100, 2)
            self.assertEqual(row.deadline_at, new_deadline)
            self.assertEqual(row.prompt_message_id, 99)

    async def test_upsert_writes_and_resets_kind_and_reason(self) -> None:
        async with self.session_factory() as session:
            first_deadline = now_shanghai_naive() + timedelta(minutes=5)
            await upsert_join_verification(
                session,
                group_id=-100,
                user_id=6,
                deadline_at=first_deadline,
                kind=VERIFICATION_KIND_MODERATION,
                reason="疑似广告",
                display_name="待质询用户",
                prompt_message_id=66,
            )
            await session.commit()

            row = await get_join_verification(session, -100, 6)
            self.assertEqual(row.kind, VERIFICATION_KIND_MODERATION)
            self.assertEqual(row.reason, "疑似广告")

            second_deadline = now_shanghai_naive() + timedelta(minutes=10)
            await upsert_join_verification(
                session,
                group_id=-100,
                user_id=6,
                deadline_at=second_deadline,
            )
            await session.commit()

            row = await get_join_verification(session, -100, 6)
            self.assertEqual(row.kind, VERIFICATION_KIND_JOIN)
            self.assertEqual(row.reason, "")
            self.assertEqual(row.display_name, "")
            self.assertEqual(row.prompt_message_id, 0)
            self.assertEqual(row.deadline_at, second_deadline)

    async def test_pending_lookup_by_user(self) -> None:
        async with self.session_factory() as session:
            self.assertIsNone(await get_pending_verification_for_user(session, 3))
            await upsert_join_verification(
                session,
                group_id=-100,
                user_id=3,
                deadline_at=now_shanghai_naive() + timedelta(minutes=5),
            )
            await session.commit()
            row = await get_pending_verification_for_user(session, 3)
            self.assertEqual(row.group_id, -100)

    async def test_deleted_verification_ids_are_not_reused(self) -> None:
        now = now_shanghai_naive()
        async with self.session_factory() as session:
            await upsert_join_verification(
                session,
                group_id=-100,
                user_id=41,
                deadline_at=now + timedelta(minutes=5),
            )
            await session.commit()
            first = await get_join_verification(session, -100, 41)
            first_id = int(first.id)
            await delete_join_verification(session, -100, 41)
            await session.commit()

            await upsert_join_verification(
                session,
                group_id=-100,
                user_id=42,
                deadline_at=now + timedelta(minutes=5),
            )
            await session.commit()
            second = await get_join_verification(session, -100, 42)

        self.assertGreater(int(second.id), first_id)


    async def test_list_expired_only_returns_past_deadlines(self) -> None:
        now = now_shanghai_naive()
        async with self.session_factory() as session:
            await upsert_join_verification(
                session, group_id=-100, user_id=4, deadline_at=now - CHALLENGE_SUBMIT_GRACE - timedelta(seconds=1)
            )
            await upsert_join_verification(
                session, group_id=-100, user_id=5, deadline_at=now + timedelta(minutes=5)
            )
            await session.commit()

            expired = await list_expired_verifications(session, now=now)
            self.assertEqual([row.user_id for row in expired], [4])

    async def test_pending_and_expired_claims_each_succeed_only_once(self) -> None:
        now = now_shanghai_naive()
        async with self.session_factory() as session:
            await upsert_join_verification(
                session,
                group_id=-100,
                user_id=7,
                deadline_at=now + timedelta(minutes=5),
            )
            await upsert_join_verification(
                session,
                group_id=-100,
                user_id=8,
                deadline_at=now - CHALLENGE_SUBMIT_GRACE - timedelta(seconds=1),
                kind=VERIFICATION_KIND_MODERATION,
            )
            await session.commit()

            pending = await get_join_verification(session, -100, 7)
            expired = await get_join_verification(session, -100, 8)

            self.assertFalse(
                await claim_join_verification(
                    session,
                    verification_id=pending.id,
                    deadline_at=pending.deadline_at,
                    kind=pending.kind,
                    now=now,
                    expired=True,
                )
            )
            self.assertTrue(
                await claim_join_verification(
                    session,
                    verification_id=pending.id,
                    deadline_at=pending.deadline_at,
                    kind=pending.kind,
                    now=now,
                    expired=False,
                )
            )
            self.assertFalse(
                await claim_join_verification(
                    session,
                    verification_id=pending.id,
                    deadline_at=pending.deadline_at,
                    kind=pending.kind,
                    now=now,
                    expired=False,
                )
            )

            self.assertFalse(
                await claim_join_verification(
                    session,
                    verification_id=expired.id,
                    deadline_at=expired.deadline_at,
                    kind=expired.kind,
                    now=now,
                    expired=False,
                )
            )
            self.assertTrue(
                await claim_join_verification(
                    session,
                    verification_id=expired.id,
                    deadline_at=expired.deadline_at,
                    kind=expired.kind,
                    now=now,
                    expired=True,
                )
            )
            self.assertFalse(
                await claim_join_verification(
                    session,
                    verification_id=expired.id,
                    deadline_at=expired.deadline_at,
                    kind=expired.kind,
                    now=now,
                    expired=True,
                )
            )
            await session.commit()

            self.assertIsNone(await get_join_verification(session, -100, 7))
            self.assertIsNone(await get_join_verification(session, -100, 8))


class JoinVerificationSchemaMigrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_existing_table_is_migrated_without_losing_pending_rows(self) -> None:
        fd, db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        connection = sqlite3.connect(db_path)
        try:
            connection.executescript(
                """
                CREATE TABLE join_verifications (
                    id INTEGER NOT NULL PRIMARY KEY,
                    group_id BIGINT NOT NULL,
                    user_id BIGINT NOT NULL,
                    kind VARCHAR(32) NOT NULL DEFAULT 'join',
                    provider VARCHAR(32) NOT NULL DEFAULT 'turnstile',
                    reason TEXT NOT NULL DEFAULT '',
                    display_name VARCHAR(255) NOT NULL,
                    prompt_message_id BIGINT NOT NULL,
                    deadline_at DATETIME NOT NULL,
                    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE UNIQUE INDEX ix_join_verification_group_user
                    ON join_verifications (group_id, user_id);
                CREATE INDEX ix_join_verifications_user_id
                    ON join_verifications (user_id);
                INSERT INTO join_verifications (
                    id, group_id, user_id, kind, provider, reason,
                    display_name, prompt_message_id, deadline_at
                ) VALUES (
                    41, -100, 51, 'join', 'hcaptcha', '', '待验证用户', 777,
                    '2099-01-01 00:00:00'
                );
                """
            )
            connection.commit()
        finally:
            connection.close()

        engine = None
        try:
            engine, session_factory = await init_db(f"sqlite+aiosqlite:///{db_path}")
            async with engine.connect() as conn:
                result = await conn.exec_driver_sql(
                    "SELECT sql FROM sqlite_master "
                    "WHERE type = 'table' AND name = 'join_verifications'"
                )
                table_sql = str(result.scalar_one())
            self.assertIn("AUTOINCREMENT", table_sql.upper())

            async with session_factory() as session:
                preserved = await get_join_verification(session, -100, 51)
                self.assertIsNotNone(preserved)
                self.assertEqual(preserved.id, 41)
                self.assertEqual(preserved.provider, "hcaptcha")
                await delete_join_verification(session, -100, 51)
                await upsert_join_verification(
                    session,
                    group_id=-100,
                    user_id=52,
                    deadline_at=now_shanghai_naive() + timedelta(minutes=5),
                )
                await session.commit()
                replacement = await get_join_verification(session, -100, 52)
                self.assertGreater(replacement.id, 41)
        finally:
            if engine is not None:
                await engine.dispose()
            for suffix in ("", "-wal", "-shm"):
                try:
                    os.remove(f"{db_path}{suffix}")
                except OSError:
                    pass


def _join_event(*, user_id: int = 900, full_name: str = "新人", username: str = "newbie") -> SimpleNamespace:
    user = SimpleNamespace(
        id=user_id,
        is_bot=False,
        full_name=full_name,
        username=username,
    )
    return SimpleNamespace(
        chat=SimpleNamespace(id=-100, type="supergroup", ban=AsyncMock()),
        new_chat_member=SimpleNamespace(user=user),
        bot=SimpleNamespace(
            get_chat=AsyncMock(return_value=SimpleNamespace(bio="正经简介")),
            send_message=AsyncMock(return_value=SimpleNamespace(message_id=777)),
            delete_message=AsyncMock(return_value=True),
            restrict_chat_member=AsyncMock(),
            ban_chat_member=AsyncMock(),
            unban_chat_member=AsyncMock(),
        ),
    )


class JoinTriggersVerificationTests(_DbTestCase):
    async def _run_join(self, event, settings) -> None:
        from bot.handlers import membership

        fake_moderation = SimpleNamespace(
            check_rules=AsyncMock(return_value=(False, "", None))
        )
        async with self.session_factory() as session:
            with (
                patch("bot.handlers.membership.ModerationService", return_value=fake_moderation),
                patch("bot.handlers.membership._build_llm", return_value=object()),
            ):
                await membership.on_member_join(event, session=session, settings=settings)
            await session.commit()

    async def test_clean_join_gets_restricted_and_deep_link_prompt(self) -> None:
        event = _join_event(user_id=910)
        await self._run_join(event, _settings())

        event.bot.restrict_chat_member.assert_awaited_once()
        args = event.bot.restrict_chat_member.await_args
        self.assertEqual(args.args[:2], (-100, 910))
        self.assertFalse(args.kwargs["permissions"].can_send_messages)
        event.bot.send_message.assert_awaited_once()
        markup = event.bot.send_message.await_args.kwargs.get("reply_markup")
        self.assertIsNotNone(markup)
        self.assertEqual(markup.inline_keyboard[0][0].callback_data, "jv:v:910")
        self.assertEqual(markup.inline_keyboard[1][0].callback_data, "jv:a:910")
        self.assertEqual(markup.inline_keyboard[1][1].callback_data, "jv:r:910")

        async with self.session_factory() as session:
            row = await get_join_verification(session, -100, 910)
            self.assertIsNotNone(row)
            self.assertEqual(row.prompt_message_id, 777)

    async def test_duplicate_join_replaces_and_deletes_old_prompt(self) -> None:
        async with self.session_factory() as session:
            await upsert_join_verification(
                session,
                group_id=-100,
                user_id=918,
                deadline_at=now_shanghai_naive() + timedelta(minutes=5),
                prompt_message_id=700,
            )
            await session.commit()

        event = _join_event(user_id=918)
        await self._run_join(event, _settings())

        event.bot.delete_message.assert_awaited_once_with(-100, 700)
        async with self.session_factory() as session:
            row = await get_join_verification(session, -100, 918)
            self.assertIsNotNone(row)
            self.assertEqual(row.prompt_message_id, 777)

    async def test_disabled_feature_skips_verification(self) -> None:
        event = _join_event(user_id=911)
        await self._run_join(event, _settings(join_verification_enabled=False))

        event.bot.restrict_chat_member.assert_not_awaited()
        async with self.session_factory() as session:
            self.assertIsNone(await get_join_verification(session, -100, 911))

    async def test_group_can_disable_verification_when_global_default_is_on(self) -> None:
        async with self.session_factory() as session:
            session.add(
                Group(
                    id=-100,
                    title="No Verification",
                    settings={"join_verification_enabled": False},
                )
            )
            await session.commit()

        event = _join_event(user_id=919)
        await self._run_join(event, _settings())

        event.bot.restrict_chat_member.assert_not_awaited()
        async with self.session_factory() as session:
            self.assertIsNone(await get_join_verification(session, -100, 919))

    async def test_group_can_enable_hcaptcha_when_global_default_is_off(self) -> None:
        async with self.session_factory() as session:
            session.add(
                Group(
                    id=-100,
                    title="hCaptcha Group",
                    settings={
                        "join_verification_enabled": True,
                        "join_verification_provider": "hcaptcha",
                    },
                )
            )
            await session.commit()

        settings = _settings(join_verification_enabled=False)
        settings.join_verification_hcaptcha_site_key = "h-site"
        settings.join_verification_hcaptcha_secret_key = "h-secret"
        event = _join_event(user_id=925)
        await self._run_join(event, settings)

        event.bot.restrict_chat_member.assert_awaited_once()
        async with self.session_factory() as session:
            record = await get_join_verification(session, -100, 925)
            self.assertIsNotNone(record)
            self.assertEqual(record.provider, "hcaptcha")

    async def test_missing_turnstile_config_skips_verification(self) -> None:
        event = _join_event(user_id=912)
        await self._run_join(event, _settings(join_verification_turnstile_secret_key=""))

        event.bot.restrict_chat_member.assert_not_awaited()

    async def test_moderation_disabled_still_verifies(self) -> None:
        event = _join_event(user_id=913)
        settings = _settings()
        settings.moderation.enabled = False
        await self._run_join(event, settings)

        event.bot.restrict_chat_member.assert_awaited_once()
        async with self.session_factory() as session:
            self.assertIsNotNone(await get_join_verification(session, -100, 913))

    async def test_violating_join_is_banned_without_verification(self) -> None:
        from bot.handlers import membership

        event = _join_event(user_id=914)
        fake_moderation = SimpleNamespace(
            check_rules=AsyncMock(return_value=(True, "昵称含广告", None))
        )
        async with self.session_factory() as session:
            with (
                patch("bot.handlers.membership.ModerationService", return_value=fake_moderation),
                patch("bot.handlers.membership._build_llm", return_value=object()),
            ):
                await membership.on_member_join(event, session=session, settings=_settings())
            await session.commit()

            event.chat.ban.assert_awaited_once_with(914)
            event.bot.restrict_chat_member.assert_not_awaited()
            self.assertIsNone(await get_join_verification(session, -100, 914))

    async def test_banned_rejoin_is_banned_without_verification(self) -> None:
        from bot.handlers import membership

        event = _join_event(user_id=915)
        async with self.session_factory() as session:
            await add_global_ban(session, 915, reason="旧账", created_by=1)
            await session.commit()

            await membership.on_member_join(event, session=session, settings=_settings())

            event.chat.ban.assert_awaited_once_with(915)
            event.bot.restrict_chat_member.assert_not_awaited()

    async def test_restrict_failure_fails_open_without_record(self) -> None:
        event = _join_event(user_id=916)
        event.bot.restrict_chat_member = AsyncMock(side_effect=RuntimeError("no rights"))
        await self._run_join(event, _settings())

        event.bot.send_message.assert_not_awaited()
        async with self.session_factory() as session:
            self.assertIsNone(await get_join_verification(session, -100, 916))

    async def test_prompt_failure_lifts_restriction(self) -> None:
        event = _join_event(user_id=917)
        event.bot.send_message = AsyncMock(side_effect=RuntimeError("flood"))
        await self._run_join(event, _settings())

        # First call restricts, second call restores full permissions.
        self.assertEqual(event.bot.restrict_chat_member.await_count, 2)
        last = event.bot.restrict_chat_member.await_args
        self.assertTrue(last.kwargs["permissions"].can_send_messages)
        async with self.session_factory() as session:
            self.assertIsNone(await get_join_verification(session, -100, 917))


class ModerationChallengeTests(_DbTestCase):
    async def test_begin_mutes_prompts_and_persists_moderation_record(self) -> None:
        settings = _settings(join_verification_enabled=False)
        settings.moderation.challenge_timeout_seconds = 120
        bot = SimpleNamespace(
            restrict_chat_member=AsyncMock(),
            send_message=AsyncMock(return_value=SimpleNamespace(message_id=818)),
        )
        before = now_shanghai_naive()

        async with self.session_factory() as session:
            started = await begin_moderation_challenge(
                bot=bot,
                session=session,
                settings=settings,
                group_id=-100,
                user_id=918,
                display_name="可疑用户",
                bot_username="my_bot",
                reason="疑似发布广告",
            )
            after = now_shanghai_naive()

            self.assertTrue(started)
            row = await get_join_verification(session, -100, 918)
            self.assertIsNotNone(row)
            self.assertEqual(row.kind, VERIFICATION_KIND_MODERATION)
            self.assertEqual(row.reason, "疑似发布广告")
            self.assertEqual(row.display_name, "可疑用户")
            self.assertEqual(row.prompt_message_id, 818)
            self.assertGreaterEqual(
                row.deadline_at,
                before + timedelta(seconds=120),
            )
            self.assertLessEqual(
                row.deadline_at,
                after + timedelta(seconds=120),
            )

        bot.restrict_chat_member.assert_awaited_once()
        restrict_call = bot.restrict_chat_member.await_args
        self.assertEqual(restrict_call.args[:2], (-100, 918))
        self.assertFalse(restrict_call.kwargs["permissions"].can_send_messages)

        bot.send_message.assert_awaited_once()
        prompt_call = bot.send_message.await_args
        self.assertEqual(prompt_call.args[0], -100)
        self.assertIn("低置信度", prompt_call.args[1])
        self.assertIn("疑似发布广告", prompt_call.args[1])
        self.assertEqual(prompt_call.kwargs["parse_mode"], "HTML")
        keyboard = prompt_call.kwargs["reply_markup"]
        self.assertEqual(keyboard.inline_keyboard[0][0].callback_data, "jv:v:918")
        self.assertEqual(keyboard.inline_keyboard[1][0].callback_data, "jv:a:918")
        self.assertEqual(keyboard.inline_keyboard[1][1].callback_data, "jv:r:918")

    async def test_expired_rejoin_requeues_when_group_ban_fails(self) -> None:
        settings = _settings()
        async with self.session_factory() as session:
            await upsert_join_verification(
                session,
                group_id=-100,
                user_id=919,
                deadline_at=now_shanghai_naive() - CHALLENGE_SUBMIT_GRACE - timedelta(seconds=1),
                kind=VERIFICATION_KIND_MODERATION,
                reason="疑似广告",
                display_name="可疑用户",
                prompt_message_id=819,
            )
            await session.commit()
            record = await get_join_verification(session, -100, 919)

            event = SimpleNamespace(
                chat=SimpleNamespace(
                    id=-100,
                    ban=AsyncMock(return_value=False),
                ),
                bot=SimpleNamespace(send_message=AsyncMock()),
            )
            with patch(
                "bot.handlers.membership.restrict_new_member",
                new=AsyncMock(return_value=True),
            ) as restrict:
                await membership._enforce_pending_moderation_challenge(
                    event,
                    session,
                    settings,
                    record,
                    display_name="可疑用户",
                )

        restrict.assert_awaited_once_with(event.bot, -100, 919)
        async with self.session_factory() as session:
            retry = await get_join_verification(session, -100, 919)
            warning = await session.scalar(
                select(UserWarning).where(
                    UserWarning.group_id == -100,
                    UserWarning.user_id == 919,
                )
            )
            self.assertIsNotNone(retry)
            self.assertGreater(retry.deadline_at, now_shanghai_naive())
            self.assertIsNone(warning)

    async def test_super_admin_pending_challenge_is_cleared_on_rejoin(self) -> None:
        async with self.session_factory() as session:
            await upsert_join_verification(
                session,
                group_id=-100,
                user_id=1,
                deadline_at=now_shanghai_naive() - CHALLENGE_SUBMIT_GRACE - timedelta(seconds=1),
                kind=VERIFICATION_KIND_MODERATION,
            )
            await session.commit()
            record = await get_join_verification(session, -100, 1)
            event = SimpleNamespace(
                chat=SimpleNamespace(id=-100, ban=AsyncMock()),
                bot=SimpleNamespace(
                    restrict_chat_member=AsyncMock(return_value=True),
                    get_chat_member=AsyncMock(),
                ),
            )

            await membership._enforce_pending_moderation_challenge(
                event,
                session,
                _settings(super_admin_id=1),
                record,
                display_name="Owner",
            )

        event.chat.ban.assert_not_awaited()
        event.bot.restrict_chat_member.assert_awaited_once()
        async with self.session_factory() as session:
            self.assertIsNone(await get_join_verification(session, -100, 1))


def _verification_callback(
    *,
    action: str,
    target_user_id: int,
    operator_id: int,
    message_id: int,
) -> SimpleNamespace:
    bot = SimpleNamespace(
        me=AsyncMock(return_value=SimpleNamespace(username="my_bot")),
        edit_message_text=AsyncMock(),
        delete_message=AsyncMock(return_value=True),
    )
    return SimpleNamespace(
        data=build_verification_callback_data(action, target_user_id),
        from_user=SimpleNamespace(id=operator_id),
        message=SimpleNamespace(
            message_id=message_id,
            chat=SimpleNamespace(id=-100, type="supergroup"),
        ),
        bot=bot,
        answer=AsyncMock(),
    )


class VerificationCallbackTests(_DbTestCase):
    async def _add_record(
        self,
        *,
        user_id: int,
        message_id: int,
        kind: str = VERIFICATION_KIND_JOIN,
        reason: str = "",
    ):
        deadline = now_shanghai_naive() + timedelta(minutes=5)
        async with self.session_factory() as session:
            await upsert_join_verification(
                session,
                group_id=-100,
                user_id=user_id,
                deadline_at=deadline,
                kind=kind,
                reason=reason,
                display_name=f"用户{user_id}",
                prompt_message_id=message_id,
            )
            await session.commit()
        return deadline

    async def test_target_user_callback_opens_scoped_private_link(self) -> None:
        await self._add_record(user_id=940, message_id=840)
        callback = _verification_callback(
            action=VERIFICATION_CALLBACK_START,
            target_user_id=940,
            operator_id=940,
            message_id=840,
        )

        async with self.session_factory() as session:
            await membership.on_verification_callback(
                callback,
                session=session,
                settings=_settings(),
            )

        callback.answer.assert_awaited_once_with(
            url="https://t.me/my_bot?start=verify_n100"
        )
        async with self.session_factory() as session:
            self.assertIsNotNone(await get_join_verification(session, -100, 940))

    async def test_non_target_user_cannot_open_verification(self) -> None:
        await self._add_record(user_id=941, message_id=841)
        callback = _verification_callback(
            action=VERIFICATION_CALLBACK_START,
            target_user_id=941,
            operator_id=999,
            message_id=841,
        )

        async with self.session_factory() as session:
            await membership.on_verification_callback(
                callback,
                session=session,
                settings=_settings(),
            )

        self.assertTrue(callback.answer.await_args.kwargs["show_alert"])
        self.assertIn("本人", callback.answer.await_args.args[0])
        callback.bot.me.assert_not_awaited()

    async def test_stale_prompt_cannot_open_verification(self) -> None:
        await self._add_record(user_id=942, message_id=842)
        callback = _verification_callback(
            action=VERIFICATION_CALLBACK_START,
            target_user_id=942,
            operator_id=942,
            message_id=999,
        )

        async with self.session_factory() as session:
            await membership.on_verification_callback(
                callback,
                session=session,
                settings=_settings(),
            )

        self.assertTrue(callback.answer.await_args.kwargs["show_alert"])
        self.assertIn("失效", callback.answer.await_args.args[0])
        callback.bot.delete_message.assert_awaited_once_with(-100, 999)

    async def test_non_admin_cannot_approve(self) -> None:
        await self._add_record(user_id=943, message_id=843)
        callback = _verification_callback(
            action=VERIFICATION_CALLBACK_APPROVE,
            target_user_id=943,
            operator_id=10,
            message_id=843,
        )

        with patch(
            "bot.handlers.membership.is_group_admin_or_higher",
            new=AsyncMock(return_value=False),
        ):
            async with self.session_factory() as session:
                await membership.on_verification_callback(
                    callback,
                    session=session,
                    settings=_settings(),
                )

        self.assertIn("群管理员", callback.answer.await_args.args[0])
        async with self.session_factory() as session:
            self.assertIsNotNone(await get_join_verification(session, -100, 943))

    async def test_admin_approve_restores_and_consumes_record_once(self) -> None:
        await self._add_record(user_id=944, message_id=844)
        callback = _verification_callback(
            action=VERIFICATION_CALLBACK_APPROVE,
            target_user_id=944,
            operator_id=11,
            message_id=844,
        )
        auth = AsyncMock(return_value=True)
        restore = AsyncMock(return_value=True)

        with (
            patch("bot.handlers.membership.is_group_admin_or_higher", new=auth),
            patch("bot.handlers.membership.restore_member_permissions", new=restore),
        ):
            async with self.session_factory() as session:
                await membership.on_verification_callback(
                    callback,
                    session=session,
                    settings=_settings(),
                )
                await membership.on_verification_callback(
                    callback,
                    session=session,
                    settings=_settings(),
                )

        restore.assert_awaited_once_with(callback.bot, -100, 944)
        callback.bot.edit_message_text.assert_awaited_once()
        self.assertIsNone(
            callback.bot.edit_message_text.await_args.kwargs["reply_markup"]
        )
        self.assertEqual(callback.answer.await_count, 2)
        self.assertIn("失效", callback.answer.await_args.args[0])
        async with self.session_factory() as session:
            self.assertIsNone(await get_join_verification(session, -100, 944))

    async def test_admin_approve_notice_honors_moderation_auto_delete(self) -> None:
        from unittest.mock import patch

        await self._add_record(user_id=945, message_id=845)
        callback = _verification_callback(
            action=VERIFICATION_CALLBACK_APPROVE,
            target_user_id=945,
            operator_id=11,
            message_id=845,
        )
        edited = SimpleNamespace(message_id=845)
        callback.bot.edit_message_text = AsyncMock(return_value=edited)
        settings = _settings()
        settings.bot.auto_delete_seconds = 15
        settings.bot.auto_delete_categories = ["moderation"]

        with (
            patch(
                "bot.handlers.membership.is_group_admin_or_higher",
                new=AsyncMock(return_value=True),
            ),
            patch(
                "bot.handlers.membership.restore_member_permissions",
                new=AsyncMock(return_value=True),
            ),
            patch(
                "bot.handlers.membership.schedule_message_auto_delete"
            ) as schedule_mock,
        ):
            async with self.session_factory() as session:
                await membership.on_verification_callback(
                    callback, session=session, settings=settings
                )

        callback.bot.edit_message_text.assert_awaited_once()
        schedule_mock.assert_called_once_with(edited, 15)

    async def test_admin_approve_preserves_record_when_user_is_banned(self) -> None:
        await self._add_record(user_id=945, message_id=845)
        await self._add_record(user_id=946, message_id=846)
        async with self.session_factory() as session:
            await add_global_ban(session, 945, reason="global")
            session.add(UserWarning(group_id=-100, user_id=946, count=3, is_banned=True))
            await session.commit()

        restore = AsyncMock(return_value=True)
        with (
            patch(
                "bot.handlers.membership.is_group_admin_or_higher",
                new=AsyncMock(return_value=True),
            ),
            patch("bot.handlers.membership.restore_member_permissions", new=restore),
        ):
            for target, message_id in ((945, 845), (946, 846)):
                callback = _verification_callback(
                    action=VERIFICATION_CALLBACK_APPROVE,
                    target_user_id=target,
                    operator_id=11,
                    message_id=message_id,
                )
                async with self.session_factory() as session:
                    await membership.on_verification_callback(
                        callback,
                        session=session,
                        settings=_settings(),
                    )
                self.assertIn("已被封禁", callback.answer.await_args.args[0])

        restore.assert_not_awaited()
        async with self.session_factory() as session:
            self.assertIsNotNone(await get_join_verification(session, -100, 945))
            self.assertIsNotNone(await get_join_verification(session, -100, 946))

    async def test_admin_approve_requeues_when_restore_fails(self) -> None:
        old_deadline = await self._add_record(user_id=947, message_id=847)
        callback = _verification_callback(
            action=VERIFICATION_CALLBACK_APPROVE,
            target_user_id=947,
            operator_id=11,
            message_id=847,
        )

        with (
            patch(
                "bot.handlers.membership.is_group_admin_or_higher",
                new=AsyncMock(return_value=True),
            ),
            patch(
                "bot.handlers.membership.restore_member_permissions",
                new=AsyncMock(return_value=False),
            ),
        ):
            async with self.session_factory() as session:
                await membership.on_verification_callback(
                    callback,
                    session=session,
                    settings=_settings(),
                )

        async with self.session_factory() as session:
            record = await get_join_verification(session, -100, 947)
            self.assertIsNotNone(record)
            self.assertGreater(record.deadline_at, old_deadline)
            self.assertEqual(record.prompt_message_id, 847)
        callback.bot.edit_message_text.assert_not_awaited()

    async def test_admin_reject_join_kicks_without_global_ban(self) -> None:
        await self._add_record(user_id=948, message_id=848)
        callback = _verification_callback(
            action=VERIFICATION_CALLBACK_REJECT,
            target_user_id=948,
            operator_id=11,
            message_id=848,
        )
        kick = AsyncMock(return_value=True)

        with (
            patch(
                "bot.handlers.membership.is_group_admin_or_higher",
                new=AsyncMock(return_value=True),
            ),
            patch("bot.handlers.membership.kick_member", new=kick),
        ):
            async with self.session_factory() as session:
                await membership.on_verification_callback(
                    callback,
                    session=session,
                    settings=_settings(),
                )

        kick.assert_awaited_once_with(callback.bot, -100, 948)
        async with self.session_factory() as session:
            self.assertIsNone(await get_join_verification(session, -100, 948))
            self.assertIsNone(await get_global_ban(session, 948))

    async def test_admin_reject_join_requeues_when_kick_fails(self) -> None:
        old_deadline = await self._add_record(user_id=949, message_id=849)
        callback = _verification_callback(
            action=VERIFICATION_CALLBACK_REJECT,
            target_user_id=949,
            operator_id=11,
            message_id=849,
        )

        with (
            patch(
                "bot.handlers.membership.is_group_admin_or_higher",
                new=AsyncMock(return_value=True),
            ),
            patch(
                "bot.handlers.membership.kick_member",
                new=AsyncMock(return_value=False),
            ),
        ):
            async with self.session_factory() as session:
                await membership.on_verification_callback(
                    callback,
                    session=session,
                    settings=_settings(),
                )

        async with self.session_factory() as session:
            record = await get_join_verification(session, -100, 949)
            self.assertIsNotNone(record)
            self.assertGreater(record.deadline_at, old_deadline)
        callback.bot.edit_message_text.assert_not_awaited()

    async def test_admin_reject_moderation_only_bans_current_group(self) -> None:
        await self._add_record(
            user_id=950,
            message_id=850,
            kind=VERIFICATION_KIND_MODERATION,
            reason="疑似广告",
        )
        callback = _verification_callback(
            action=VERIFICATION_CALLBACK_REJECT,
            target_user_id=950,
            operator_id=12,
            message_id=850,
        )
        ban = AsyncMock(return_value=True)

        with (
            patch(
                "bot.handlers.membership.is_group_admin_or_higher",
                new=AsyncMock(return_value=True),
            ),
            patch("bot.handlers.membership.ban_member", new=ban),
        ):
            async with self.session_factory() as session:
                await membership.on_verification_callback(
                    callback,
                    session=session,
                    settings=_settings(),
                )

        ban.assert_awaited_once_with(callback.bot, -100, 950)
        async with self.session_factory() as session:
            self.assertIsNone(await get_join_verification(session, -100, 950))
            warning = await session.scalar(
                select(UserWarning).where(
                    UserWarning.group_id == -100,
                    UserWarning.user_id == 950,
                )
            )
            self.assertIsNotNone(warning)
            self.assertTrue(warning.is_banned)
            self.assertIsNone(await get_global_ban(session, 950))

    async def test_admin_reject_moderation_requeues_when_ban_fails(self) -> None:
        old_deadline = await self._add_record(
            user_id=952,
            message_id=852,
            kind=VERIFICATION_KIND_MODERATION,
            reason="疑似广告",
        )
        callback = _verification_callback(
            action=VERIFICATION_CALLBACK_REJECT,
            target_user_id=952,
            operator_id=12,
            message_id=852,
        )

        with (
            patch(
                "bot.handlers.membership.is_group_admin_or_higher",
                new=AsyncMock(return_value=True),
            ),
            patch(
                "bot.handlers.membership.ban_member",
                new=AsyncMock(return_value=False),
            ),
        ):
            async with self.session_factory() as session:
                await membership.on_verification_callback(
                    callback,
                    session=session,
                    settings=_settings(),
                )

        async with self.session_factory() as session:
            record = await get_join_verification(session, -100, 952)
            warning = await session.scalar(
                select(UserWarning).where(
                    UserWarning.group_id == -100,
                    UserWarning.user_id == 952,
                )
            )
            self.assertIsNotNone(record)
            self.assertGreater(record.deadline_at, old_deadline)
            self.assertIsNone(warning)
            self.assertIsNone(await get_global_ban(session, 952))

    async def test_admin_reject_cannot_ban_super_admin_target(self) -> None:
        await self._add_record(
            user_id=951,
            message_id=851,
            kind=VERIFICATION_KIND_MODERATION,
            reason="疑似广告",
        )
        callback = _verification_callback(
            action=VERIFICATION_CALLBACK_REJECT,
            target_user_id=951,
            operator_id=12,
            message_id=851,
        )
        ban = AsyncMock(return_value=True)
        kick = AsyncMock(return_value=True)

        with (
            patch(
                "bot.handlers.membership.is_group_admin_or_higher",
                new=AsyncMock(return_value=True),
            ),
            patch("bot.handlers.membership.ban_member", new=ban),
            patch("bot.handlers.membership.kick_member", new=kick),
        ):
            async with self.session_factory() as session:
                await membership.on_verification_callback(
                    callback,
                    session=session,
                    settings=_settings(super_admin_id=951),
                )

        ban.assert_not_awaited()
        kick.assert_not_awaited()
        self.assertTrue(callback.answer.await_args.kwargs["show_alert"])
        self.assertIn("最高管理员", callback.answer.await_args.args[0])
        async with self.session_factory() as session:
            self.assertIsNotNone(await get_join_verification(session, -100, 951))
            self.assertIsNone(await get_global_ban(session, 951))


class PrivateStartTests(_DbTestCase):
    def _private_message(self, user_id: int) -> SimpleNamespace:
        return SimpleNamespace(
            chat=SimpleNamespace(id=user_id, type="private"),
            from_user=SimpleNamespace(id=user_id, full_name="新人"),
            answer=AsyncMock(),
        )

    async def test_pending_user_gets_mini_app_button(self) -> None:
        async with self.session_factory() as session:
            await upsert_join_verification(
                session,
                group_id=-100,
                user_id=920,
                deadline_at=now_shanghai_naive() + timedelta(minutes=5),
            )
            await session.commit()
            record = await get_join_verification(session, -100, 920)

            message = self._private_message(920)
            handled = await maybe_send_private_verification(message, session, _settings())

            self.assertTrue(handled)
            text = message.answer.await_args.args[0]
            self.assertIn("<b>入群真人验证</b>", text)
            self.assertIn("超时将被移出群聊", text)
            self.assertNotIn("消息审查真人验证", text)
            markup = message.answer.await_args.kwargs.get("reply_markup")
            button = markup.inline_keyboard[0][0]
            self.assertEqual(
                button.web_app.url,
                "https://verify.example.com/verify?provider=turnstile"
                f"&verification_id={record.id}",
            )
            self.assertIsNone(getattr(button, "url", None))

    async def test_moderation_record_gets_moderation_private_copy(self) -> None:
        async with self.session_factory() as session:
            await upsert_join_verification(
                session,
                group_id=-100,
                user_id=924,
                deadline_at=now_shanghai_naive() + timedelta(minutes=5),
                kind=VERIFICATION_KIND_MODERATION,
                reason="疑似广告 <链接>",
            )
            await session.commit()

            message = self._private_message(924)
            handled = await maybe_send_private_verification(
                message,
                session,
                _settings(join_verification_enabled=False),
            )

            self.assertTrue(handled)
            text = message.answer.await_args.args[0]
            self.assertIn("<b>消息审查真人验证</b>", text)
            self.assertIn("疑似广告 &lt;链接&gt;", text)
            self.assertIn("超时将被封禁", text)
            self.assertNotIn("超时将被移出群聊", text)

    async def test_group_scoped_start_selects_exact_pending_record(self) -> None:
        async with self.session_factory() as session:
            await upsert_join_verification(
                session,
                group_id=-100,
                user_id=926,
                deadline_at=now_shanghai_naive() + timedelta(minutes=4),
                provider="turnstile",
            )
            await upsert_join_verification(
                session,
                group_id=-200,
                user_id=926,
                deadline_at=now_shanghai_naive() + timedelta(minutes=5),
                provider="hcaptcha",
            )
            await session.commit()
            turnstile_record = await get_join_verification(session, -100, 926)
            hcaptcha_record = await get_join_verification(session, -200, 926)

            settings = _settings()
            settings.join_verification_hcaptcha_site_key = "h-site"
            settings.join_verification_hcaptcha_secret_key = "h-secret"

            turnstile_message = self._private_message(926)
            self.assertTrue(
                await maybe_send_private_verification(
                    turnstile_message,
                    session,
                    settings,
                    group_id=-100,
                )
            )
            turnstile_url = turnstile_message.answer.await_args.kwargs[
                "reply_markup"
            ].inline_keyboard[0][0].web_app.url
            self.assertEqual(
                turnstile_url,
                "https://verify.example.com/verify?provider=turnstile"
                f"&verification_id={turnstile_record.id}",
            )

            hcaptcha_message = self._private_message(926)
            self.assertTrue(
                await maybe_send_private_verification(
                    hcaptcha_message,
                    session,
                    settings,
                    group_id=-200,
                )
            )
            hcaptcha_url = hcaptcha_message.answer.await_args.kwargs[
                "reply_markup"
            ].inline_keyboard[0][0].web_app.url
            self.assertEqual(
                hcaptcha_url,
                "https://verify.example.com/verify?provider=hcaptcha"
                f"&verification_id={hcaptcha_record.id}",
            )

    async def test_user_without_pending_record_falls_through(self) -> None:
        async with self.session_factory() as session:
            message = self._private_message(921)
            handled = await maybe_send_private_verification(message, session, _settings())

            self.assertFalse(handled)
            message.answer.assert_not_awaited()

    async def test_expired_record_falls_through(self) -> None:
        async with self.session_factory() as session:
            await upsert_join_verification(
                session,
                group_id=-100,
                user_id=922,
                deadline_at=now_shanghai_naive() - CHALLENGE_SUBMIT_GRACE - timedelta(seconds=1),
            )
            await session.commit()

            message = self._private_message(922)
            handled = await maybe_send_private_verification(message, session, _settings())

            self.assertFalse(handled)
            message.answer.assert_not_awaited()

    async def test_missing_shared_config_falls_through(self) -> None:
        async with self.session_factory() as session:
            await upsert_join_verification(
                session,
                group_id=-100,
                user_id=923,
                deadline_at=now_shanghai_naive() + timedelta(minutes=5),
            )
            await session.commit()

            message = self._private_message(923)
            handled = await maybe_send_private_verification(
                message,
                session,
                _settings(join_verification_turnstile_secret_key=""),
            )
            self.assertFalse(handled)
            message.answer.assert_not_awaited()

    async def test_private_start_restarts_the_solve_window(self) -> None:
        # The interactive solve begins when the member reaches the private
        # chat; the remaining join-time window may be nearly consumed, so the
        # deadline is re-armed to a full timeout from now.
        settings = _settings(join_verification_timeout_seconds=120)
        async with self.session_factory() as session:
            nearly_expired = now_shanghai_naive() + timedelta(seconds=10)
            await upsert_join_verification(
                session,
                group_id=-100,
                user_id=927,
                deadline_at=nearly_expired,
            )
            await session.commit()

            message = self._private_message(927)
            handled = await maybe_send_private_verification(message, session, settings)

            self.assertTrue(handled)
            record = await get_join_verification(session, -100, 927)
            self.assertGreater(
                record.deadline_at,
                now_shanghai_naive() + timedelta(seconds=100),
            )

    async def test_private_start_never_shortens_a_longer_deadline(self) -> None:
        settings = _settings(join_verification_timeout_seconds=120)
        async with self.session_factory() as session:
            far_deadline = now_shanghai_naive() + timedelta(hours=1)
            await upsert_join_verification(
                session,
                group_id=-100,
                user_id=928,
                deadline_at=far_deadline,
            )
            await session.commit()

            message = self._private_message(928)
            handled = await maybe_send_private_verification(message, session, settings)

            self.assertTrue(handled)
            record = await get_join_verification(session, -100, 928)
            self.assertEqual(record.deadline_at, far_deadline)

    async def test_private_start_rearm_is_capped_to_record_lifetime(self) -> None:
        # Repeated /start must not defer the kick forever: the deadline never
        # exceeds created_at + 3x timeout.
        settings = _settings(join_verification_timeout_seconds=120)
        async with self.session_factory() as session:
            await upsert_join_verification(
                session,
                group_id=-100,
                user_id=929,
                deadline_at=now_shanghai_naive() + timedelta(seconds=5),
            )
            await session.commit()
            record = await get_join_verification(session, -100, 929)
            # Simulate a record issued long ago whose lifetime is nearly spent:
            # the cap (created_at + 3x timeout) is closer than now + timeout.
            record.created_at = now_shanghai_naive() - timedelta(seconds=350)
            capped = record.created_at + timedelta(seconds=360)
            await session.commit()

            message = self._private_message(929)
            handled = await maybe_send_private_verification(message, session, settings)

            self.assertTrue(handled)
            record = await get_join_verification(session, -100, 929)
            self.assertLessEqual(record.deadline_at, capped)


class SweeperTests(_DbTestCase):
    async def test_expired_super_admin_challenge_restores_instead_of_banning(self) -> None:
        async with self.session_factory() as session:
            await upsert_join_verification(
                session,
                group_id=-100,
                user_id=1,
                deadline_at=now_shanghai_naive() - CHALLENGE_SUBMIT_GRACE - timedelta(seconds=1),
                kind=VERIFICATION_KIND_MODERATION,
                prompt_message_id=776,
            )
            await session.commit()

        bot = SimpleNamespace(
            restrict_chat_member=AsyncMock(return_value=True),
            get_chat_member=AsyncMock(),
            ban_chat_member=AsyncMock(),
            unban_chat_member=AsyncMock(),
            edit_message_text=AsyncMock(),
        )
        sweeper = JoinVerificationSweeper(
            bot=bot,
            session_factory=self.session_factory,
            settings=_settings(super_admin_id=1),
        )

        self.assertEqual(await sweeper.sweep_once(), 1)

        bot.ban_chat_member.assert_not_awaited()
        bot.restrict_chat_member.assert_awaited_once()
        async with self.session_factory() as session:
            self.assertIsNone(await get_join_verification(session, -100, 1))
            warning = await session.scalar(
                select(UserWarning).where(
                    UserWarning.group_id == -100,
                    UserWarning.user_id == 1,
                )
            )
            self.assertIsNone(warning)

    async def test_sweep_handles_join_and_moderation_timeouts_differently(self) -> None:
        now = now_shanghai_naive()
        async with self.session_factory() as session:
            await upsert_join_verification(
                session,
                group_id=-100,
                user_id=930,
                deadline_at=now - CHALLENGE_SUBMIT_GRACE - timedelta(seconds=2),
                prompt_message_id=777,
            )
            await upsert_join_verification(
                session,
                group_id=-100,
                user_id=932,
                deadline_at=now - CHALLENGE_SUBMIT_GRACE - timedelta(seconds=1),
                kind=VERIFICATION_KIND_MODERATION,
                reason="疑似诈骗链接",
                prompt_message_id=778,
            )
            await upsert_join_verification(
                session, group_id=-100, user_id=931, deadline_at=now + timedelta(minutes=5)
            )
            await session.commit()

        bot = SimpleNamespace(
            ban_chat_member=AsyncMock(),
            unban_chat_member=AsyncMock(),
            edit_message_text=AsyncMock(),
        )
        sweeper = JoinVerificationSweeper(bot=bot, session_factory=self.session_factory)
        swept = await sweeper.sweep_once()

        self.assertEqual(swept, 2)
        bot.ban_chat_member.assert_has_awaits(
            [
                call(-100, 930),
                call(-100, 932),
            ]
        )
        bot.unban_chat_member.assert_awaited_once_with(-100, 930, only_if_banned=True)
        self.assertEqual(bot.edit_message_text.await_count, 2)
        finalized = {
            item.kwargs["message_id"]: item.kwargs["text"]
            for item in bot.edit_message_text.await_args_list
        }
        self.assertIn("移出群聊", finalized[777])
        self.assertIn("已封禁", finalized[778])

        async with self.session_factory() as session:
            self.assertIsNone(await get_join_verification(session, -100, 930))
            self.assertIsNone(await get_join_verification(session, -100, 932))
            self.assertIsNotNone(await get_join_verification(session, -100, 931))
            self.assertIsNone(await get_global_ban(session, 930))
            self.assertIsNone(await get_global_ban(session, 932))
            moderation_ban = await session.scalar(
                select(UserWarning).where(
                    UserWarning.group_id == -100,
                    UserWarning.user_id == 932,
                )
            )
            self.assertIsNotNone(moderation_ban)
            self.assertTrue(moderation_ban.is_banned)

    async def test_join_timeout_notice_shows_name_and_auto_deletes(self) -> None:
        from unittest.mock import patch

        now = now_shanghai_naive()
        async with self.session_factory() as session:
            await upsert_join_verification(
                session,
                group_id=-100,
                user_id=940,
                deadline_at=now - CHALLENGE_SUBMIT_GRACE - timedelta(seconds=2),
                display_name="张三",
                prompt_message_id=781,
            )
            await session.commit()

        settings = _settings()
        settings.bot.auto_delete_seconds = 30
        settings.bot.auto_delete_categories = ["moderation"]
        edited = SimpleNamespace(message_id=781)
        bot = SimpleNamespace(
            ban_chat_member=AsyncMock(return_value=True),
            unban_chat_member=AsyncMock(return_value=True),
            edit_message_text=AsyncMock(return_value=edited),
        )
        sweeper = JoinVerificationSweeper(
            bot=bot, session_factory=self.session_factory, settings=settings
        )
        with patch(
            "bot.services.join_verification.schedule_message_auto_delete"
        ) as schedule_mock:
            self.assertEqual(await sweeper.sweep_once(), 1)

        bot.edit_message_text.assert_awaited_once()
        edit_kwargs = bot.edit_message_text.await_args.kwargs
        self.assertEqual(edit_kwargs["message_id"], 781)
        self.assertEqual(edit_kwargs["parse_mode"], "HTML")
        self.assertIsNone(edit_kwargs["reply_markup"])
        # Name shown like the pass notice, and the outcome auto-deletes.
        self.assertIn("<b>张三</b>", edit_kwargs["text"])
        self.assertIn("验证超时", edit_kwargs["text"])
        schedule_mock.assert_called_once_with(edited, 30)

    async def test_failed_moderation_timeout_ban_requeues_and_restores_state(self) -> None:
        now = now_shanghai_naive()
        async with self.session_factory() as session:
            await upsert_join_verification(
                session,
                group_id=-100,
                user_id=936,
                deadline_at=now - CHALLENGE_SUBMIT_GRACE - timedelta(seconds=1),
                kind=VERIFICATION_KIND_MODERATION,
                reason="疑似诈骗链接",
                prompt_message_id=779,
            )
            await session.commit()

        bot = SimpleNamespace(
            ban_chat_member=AsyncMock(side_effect=RuntimeError("no permission")),
            unban_chat_member=AsyncMock(),
            edit_message_text=AsyncMock(),
        )
        sweeper = JoinVerificationSweeper(bot=bot, session_factory=self.session_factory)

        self.assertEqual(await sweeper.sweep_once(), 1)

        async with self.session_factory() as session:
            record = await get_join_verification(session, -100, 936)
            warning = await session.scalar(
                select(UserWarning).where(
                    UserWarning.group_id == -100,
                    UserWarning.user_id == 936,
                )
            )
            self.assertIsNotNone(record)
            self.assertGreater(record.deadline_at, now)
            self.assertIsNone(warning)
        bot.edit_message_text.assert_not_awaited()

    async def test_failed_join_timeout_kick_requeues_without_finalizing(self) -> None:
        now = now_shanghai_naive()
        async with self.session_factory() as session:
            await upsert_join_verification(
                session,
                group_id=-100,
                user_id=937,
                deadline_at=now - CHALLENGE_SUBMIT_GRACE - timedelta(seconds=1),
                prompt_message_id=780,
            )
            await session.commit()

        bot = SimpleNamespace(
            ban_chat_member=AsyncMock(return_value=False),
            unban_chat_member=AsyncMock(return_value=True),
            edit_message_text=AsyncMock(),
        )
        sweeper = JoinVerificationSweeper(bot=bot, session_factory=self.session_factory)

        self.assertEqual(await sweeper.sweep_once(), 1)

        bot.unban_chat_member.assert_not_awaited()
        bot.edit_message_text.assert_not_awaited()
        async with self.session_factory() as session:
            record = await get_join_verification(session, -100, 937)
            self.assertIsNotNone(record)
            self.assertGreater(record.deadline_at, now)

    async def test_sweep_noop_when_nothing_expired(self) -> None:
        bot = SimpleNamespace(
            ban_chat_member=AsyncMock(),
            unban_chat_member=AsyncMock(),
            edit_message_text=AsyncMock(),
        )
        sweeper = JoinVerificationSweeper(bot=bot, session_factory=self.session_factory)
        self.assertEqual(await sweeper.sweep_once(), 0)
        bot.ban_chat_member.assert_not_awaited()

    async def test_sweep_preserves_records_when_turnstile_config_is_invalid(self) -> None:
        now = now_shanghai_naive()
        async with self.session_factory() as session:
            await upsert_join_verification(
                session,
                group_id=-100,
                user_id=933,
                deadline_at=now - CHALLENGE_SUBMIT_GRACE - timedelta(seconds=1),
                kind=VERIFICATION_KIND_MODERATION,
                reason="待质询",
            )
            await session.commit()

        bot = SimpleNamespace(
            ban_chat_member=AsyncMock(),
            unban_chat_member=AsyncMock(),
            edit_message_text=AsyncMock(),
        )
        settings = _settings(
            join_verification_turnstile_site_key="same-key",
            join_verification_turnstile_secret_key="same-key",
        )
        sweeper = JoinVerificationSweeper(
            bot=bot,
            session_factory=self.session_factory,
            settings=settings,
        )

        self.assertEqual(await sweeper.sweep_once(), 0)
        bot.ban_chat_member.assert_not_awaited()
        async with self.session_factory() as session:
            record = await get_join_verification(session, -100, 933)
            self.assertIsNotNone(record)
            self.assertGreater(record.deadline_at, now)
            self.assertIsNone(await get_global_ban(session, 933))

    async def test_sweep_only_pauses_records_for_unavailable_provider(self) -> None:
        now = now_shanghai_naive()
        async with self.session_factory() as session:
            await upsert_join_verification(
                session,
                group_id=-100,
                user_id=934,
                deadline_at=now - CHALLENGE_SUBMIT_GRACE - timedelta(seconds=2),
                provider="turnstile",
            )
            await upsert_join_verification(
                session,
                group_id=-100,
                user_id=935,
                deadline_at=now - CHALLENGE_SUBMIT_GRACE - timedelta(seconds=1),
                provider="hcaptcha",
            )
            await session.commit()

        bot = SimpleNamespace(
            ban_chat_member=AsyncMock(),
            unban_chat_member=AsyncMock(),
            edit_message_text=AsyncMock(),
        )
        settings = _settings()
        settings.join_verification_hcaptcha_site_key = "same-key"
        settings.join_verification_hcaptcha_secret_key = "same-key"
        sweeper = JoinVerificationSweeper(
            bot=bot,
            session_factory=self.session_factory,
            settings=settings,
        )

        self.assertEqual(await sweeper.sweep_once(), 1)
        bot.ban_chat_member.assert_awaited_once_with(-100, 934)
        async with self.session_factory() as session:
            self.assertIsNone(await get_join_verification(session, -100, 934))
            hcaptcha_record = await get_join_verification(session, -100, 935)
            self.assertIsNotNone(hcaptcha_record)
            self.assertGreater(hcaptcha_record.deadline_at, now)

    async def test_sweep_pauses_combined_records_when_base_service_is_down(self) -> None:
        now = now_shanghai_naive()
        async with self.session_factory() as session:
            await upsert_join_verification(
                session,
                group_id=-100,
                user_id=936,
                deadline_at=now - CHALLENGE_SUBMIT_GRACE - timedelta(seconds=2),
                provider=COMBINED_VERIFICATION_PROVIDER,
            )
            await session.commit()

        bot = SimpleNamespace(
            ban_chat_member=AsyncMock(),
            unban_chat_member=AsyncMock(),
            edit_message_text=AsyncMock(),
        )
        # hCaptcha is misconfigured, so the combined challenge cannot be
        # solved; the record must be paused instead of the member kicked.
        settings = _settings()
        settings.join_verification_hcaptcha_site_key = "same-key"
        settings.join_verification_hcaptcha_secret_key = "same-key"
        sweeper = JoinVerificationSweeper(
            bot=bot,
            session_factory=self.session_factory,
            settings=settings,
        )

        self.assertEqual(await sweeper.sweep_once(), 0)
        bot.ban_chat_member.assert_not_awaited()
        async with self.session_factory() as session:
            record = await get_join_verification(session, -100, 936)
            self.assertIsNotNone(record)
            self.assertGreater(record.deadline_at, now)


class MemberLeaveCleanupTests(_DbTestCase):
    async def test_leave_removes_pending_verification(self) -> None:
        from bot.handlers import membership

        async with self.session_factory() as session:
            await upsert_join_verification(
                session,
                group_id=-100,
                user_id=940,
                deadline_at=now_shanghai_naive() + timedelta(minutes=5),
                prompt_message_id=880,
            )
            await session.commit()

            event = SimpleNamespace(
                chat=SimpleNamespace(id=-100, type="supergroup"),
                new_chat_member=SimpleNamespace(
                    user=SimpleNamespace(id=940, is_bot=False)
                ),
                bot=SimpleNamespace(delete_message=AsyncMock(return_value=True)),
            )
            await membership.on_member_leave(event, session=session, settings=_settings())
            await session.commit()

            self.assertIsNone(await get_join_verification(session, -100, 940))
            event.bot.delete_message.assert_awaited_once_with(-100, 880)

    async def test_leave_retains_pending_moderation_challenge(self) -> None:
        from bot.handlers import membership

        async with self.session_factory() as session:
            await upsert_join_verification(
                session,
                group_id=-100,
                user_id=941,
                deadline_at=now_shanghai_naive() + timedelta(minutes=5),
                kind=VERIFICATION_KIND_MODERATION,
                reason="待完成质询",
            )
            await session.commit()

            event = SimpleNamespace(
                chat=SimpleNamespace(id=-100, type="supergroup"),
                new_chat_member=SimpleNamespace(
                    user=SimpleNamespace(id=941, is_bot=False)
                ),
            )
            await membership.on_member_leave(event, session=session, settings=_settings())
            await session.commit()

            row = await get_join_verification(session, -100, 941)
            self.assertIsNotNone(row)
            self.assertEqual(row.kind, VERIFICATION_KIND_MODERATION)
            self.assertEqual(row.reason, "待完成质询")

    async def test_rejoin_restricts_pending_moderation_without_resetting_deadline(self) -> None:
        from bot.handlers import membership

        deadline = now_shanghai_naive() + timedelta(minutes=5)
        async with self.session_factory() as session:
            await upsert_join_verification(
                session,
                group_id=-100,
                user_id=942,
                deadline_at=deadline,
                kind=VERIFICATION_KIND_MODERATION,
                reason="待完成质询",
                prompt_message_id=889,
            )
            await session.commit()

            event = _join_event(user_id=942)
            await membership.on_member_join(
                event,
                session=session,
                settings=_settings(),
            )

            event.bot.restrict_chat_member.assert_awaited_once()
            permissions = event.bot.restrict_chat_member.await_args.kwargs["permissions"]
            self.assertFalse(permissions.can_send_messages)
            event.bot.get_chat.assert_not_awaited()

            row = await get_join_verification(session, -100, 942)
            self.assertIsNotNone(row)
            self.assertEqual(row.kind, VERIFICATION_KIND_MODERATION)
            self.assertEqual(row.deadline_at, deadline)
            self.assertEqual(row.prompt_message_id, 889)


if __name__ == "__main__":
    unittest.main()
