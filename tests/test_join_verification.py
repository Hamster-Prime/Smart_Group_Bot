import asyncio
import os
import sqlite3
import tempfile
import time
import unittest
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, call, patch

from aiogram.exceptions import (
    TelegramBadRequest,
    TelegramForbiddenError,
    TelegramNetworkError,
)
from aiogram.enums import ChatMemberStatus
from sqlalchemy import select

from bot.handlers import membership
from bot.db.engine import init_db
from bot.db.models import AuthorizedGroup, Group, JoinVerification, UserWarning
from bot.services.join_screening import add_global_ban, get_global_ban, remove_global_ban
from bot.services.request_priority import (
    ExecutionPriority,
    execution_priority_scope,
)
from bot.services.join_verification import (
    COMBINED_VERIFICATION_PROVIDER,
    JoinVerificationSweeper,
    PRIVATE_CHALLENGE_CLOSED_TEXT,
    PRIVATE_CHALLENGE_SUPERSEDED_TEXT,
    VERIFICATION_CALLBACK_APPROVE,
    CHALLENGE_SUBMIT_GRACE,
    VERIFICATION_CALLBACK_REJECT,
    VERIFICATION_CALLBACK_START,
    VERIFICATION_KIND_JOIN,
    VERIFICATION_KIND_MODERATION,
    VERIFICATION_KIND_PATROL,
    VERIFICATION_STATUS_RELEASING,
    VERIFICATION_STATUS_UNBANNING,
    abort_prepared_join_verification,
    ban_member,
    ban_member_result,
    begin_moderation_challenge,
    build_group_prompt_keyboard,
    build_group_prompt_text,
    build_mini_app_url,
    build_private_challenge_keyboard,
    build_private_deep_link,
    build_verification_callback_data,
    chat_member_is_present,
    claim_join_verification,
    enforce_ban_with_policy_reconciliation_result,
    activate_prepared_join_verification,
    complete_leased_join_verification,
    clear_turnstile_configuration_unavailable,
    delete_join_verification,
    delete_verification_prompt,
    get_join_verification,
    get_pending_verification_for_user,
    join_verification_ready,
    join_verification_policy,
    kick_member,
    lease_expired_join_verification,
    lease_join_verification_for_unban,
    lease_join_verifications_for_user_unban,
    list_expired_preparing_verifications,
    list_expired_verifications,
    maybe_send_private_verification,
    moderation_challenge_ready,
    mark_turnstile_configuration_unavailable,
    parse_private_verify_group_id,
    prepare_join_verification,
    reconcile_moderation_ban_after_lost_lease,
    reconcile_stale_verification_restriction,
    restore_member_permissions,
    restrict_new_member,
    release_join_verification_lease,
    resume_group_verification_recovery,
    spoiler_display_name,
    telegram_group_is_unreachable_error,
    telegram_group_requires_operator_action,
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
    def test_group_error_classification_requires_deterministic_telegram_error(self) -> None:
        method = SimpleNamespace()
        self.assertTrue(
            telegram_group_is_unreachable_error(
                TelegramBadRequest(method=method, message="CHAT_NOT_FOUND")
            )
        )
        self.assertTrue(
            telegram_group_requires_operator_action(
                TelegramBadRequest(method=method, message="CHAT_ADMIN_REQUIRED")
            )
        )
        self.assertFalse(telegram_group_is_unreachable_error(RuntimeError("chat not found")))
        self.assertFalse(
            telegram_group_is_unreachable_error(
                TelegramNetworkError(method=method, message="chat not found")
            )
        )

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

    def test_spoiler_display_name_wraps_name_in_spoiler(self) -> None:
        # The real name is preserved but hidden behind a tap-to-reveal spoiler,
        # so it is not passively visible during the verification window.
        self.assertEqual(
            spoiler_display_name("广告用户加V信xyz", 42),
            "<tg-spoiler>广告用户加V信xyz</tg-spoiler>",
        )
        self.assertEqual(
            spoiler_display_name("张三", 42), "<tg-spoiler>张三</tg-spoiler>"
        )
        # HTML-significant characters in the name are escaped inside the spoiler.
        self.assertEqual(
            spoiler_display_name("<b>&x", 42), "<tg-spoiler>&lt;b&gt;&amp;x</tg-spoiler>"
        )
        # Empty names fall back to the numeric id, without a spoiler.
        self.assertEqual(spoiler_display_name("", 42), "42")
        self.assertEqual(spoiler_display_name("   ", 42), "42")

    def test_spoiler_display_name_strips_bidi_and_blank_characters(self) -> None:
        # A leading RLO (U+202E) is stripped: even hidden in a spoiler it would
        # reverse the rest of the notice line (bidi spoofing).
        self.assertEqual(
            spoiler_display_name("‮www.spam.com", 42),
            "<tg-spoiler>www.spam.com</tg-spoiler>",
        )
        # An all-blank name (Hangul/braille fillers) falls back to the id.
        self.assertEqual(spoiler_display_name("ㅤㅤ", 42), "42")
        self.assertEqual(
            spoiler_display_name("⠀广告", 42), "<tg-spoiler>广告</tg-spoiler>"
        )
        # Zero-width space and joiner are removed from the revealed name.
        self.assertEqual(
            spoiler_display_name("​‍张三", 42), "<tg-spoiler>张三</tg-spoiler>"
        )

    def test_group_prompt_spoilers_display_name(self) -> None:
        text = build_group_prompt_text(
            user_id=42,
            display_name="加微信xw123领福利",
            timeout_seconds=300,
        )
        # The name is hidden behind a spoiler nested inside the mention link,
        # so it gets no passive exposure but admins can tap to reveal it.
        self.assertIn(
            '<a href="tg://user?id=42"><tg-spoiler>加微信xw123领福利</tg-spoiler></a>',
            text,
        )
        self.assertIn("<b>入群验证 · 待完成</b>", text)
        self.assertIn("<s>已加入群聊</s>", text)
        self.assertIn("<blockquote expandable>", text)

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

    def test_private_terminal_notices_do_not_mislabel_challenge_kind(self) -> None:
        self.assertIn("<b>真人验证 · 已结束</b>", PRIVATE_CHALLENGE_CLOSED_TEXT)
        self.assertIn("<b>真人验证 · 已更新</b>", PRIVATE_CHALLENGE_SUPERSEDED_TEXT)
        self.assertNotIn("入群验证", PRIVATE_CHALLENGE_CLOSED_TEXT)


class _NoopSession:
    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        return None

    async def commit(self) -> None:
        return None

    async def rollback(self) -> None:
        return None


class _NoopSessionFactory:
    def __call__(self) -> _NoopSession:
        return _NoopSession()


class SecurityReconciliationTests(unittest.IsolatedAsyncioTestCase):
    async def test_membership_confirmation_distinguishes_left_and_uncertain(self) -> None:
        bot = SimpleNamespace(
            get_chat_member=AsyncMock(
                side_effect=[
                    SimpleNamespace(status="left"),
                    SimpleNamespace(status="restricted"),
                    RuntimeError("network down"),
                ]
            )
        )
        self.assertFalse(await chat_member_is_present(bot, -100, 1))
        self.assertTrue(await chat_member_is_present(bot, -100, 1))
        self.assertIsNone(await chat_member_is_present(bot, -100, 1))

    async def test_lost_restriction_restores_only_when_no_new_intent_exists(self) -> None:
        bot = SimpleNamespace()
        restore = AsyncMock(return_value=True)
        restrict = AsyncMock(return_value=True)
        ban = AsyncMock(return_value=True)
        recovery = SimpleNamespace(
            verification_id=9,
            group_id=-100,
            user_id=77,
            kind=VERIFICATION_KIND_MODERATION,
            lease_until=now_shanghai_naive() + timedelta(minutes=1),
            prompt_message_id=0,
        )
        with (
            patch(
                "bot.services.join_verification.verification_release_blocked_by_ban",
                new=AsyncMock(side_effect=[False, False]),
            ),
            patch(
                "bot.services.join_verification.get_join_verification",
                new=AsyncMock(return_value=None),
            ),
            patch(
                "bot.services.join_verification.restore_member_permissions",
                new=restore,
            ),
            patch(
                "bot.services.join_verification.restrict_new_member",
                new=restrict,
            ),
            patch(
                "bot.services.join_verification.ban_member",
                new=ban,
            ),
            patch(
                "bot.services.join_verification.prepare_join_verification",
                new=AsyncMock(return_value=recovery),
            ) as prepare_recovery,
            patch(
                "bot.services.join_verification.delete_prepared_join_verification",
                new=AsyncMock(return_value=True),
            ) as delete_recovery,
        ):
            self.assertTrue(
                await reconcile_stale_verification_restriction(
                    bot,
                    _NoopSessionFactory(),
                    -100,
                    77,
                )
            )

        restore.assert_awaited_once_with(bot, -100, 77)
        restrict.assert_not_awaited()
        ban.assert_not_awaited()
        prepare_recovery.assert_awaited_once()
        delete_recovery.assert_awaited_once()

    async def test_lost_restriction_does_not_unmute_a_new_challenge(self) -> None:
        record = SimpleNamespace(status="pending")
        restore = AsyncMock(return_value=True)
        with (
            patch(
                "bot.services.join_verification.verification_release_blocked_by_ban",
                new=AsyncMock(return_value=False),
            ),
            patch(
                "bot.services.join_verification.get_join_verification",
                new=AsyncMock(return_value=record),
            ),
            patch(
                "bot.services.join_verification.restore_member_permissions",
                new=restore,
            ),
        ):
            self.assertTrue(
                await reconcile_stale_verification_restriction(
                    SimpleNamespace(),
                    _NoopSessionFactory(),
                    -100,
                    78,
                )
            )
        restore.assert_not_awaited()


class TelegramEnforcementHelperTests(unittest.IsolatedAsyncioTestCase):
    async def test_restrict_timeout_confirms_applied_remote_state(self) -> None:
        bot = SimpleNamespace(
            restrict_chat_member=AsyncMock(),
            get_chat_member=AsyncMock(),
        )
        outcomes = iter(
            [
                asyncio.TimeoutError(),
                SimpleNamespace(status="restricted", can_send_messages=False),
            ]
        )

        async def bounded(awaitable, **_kwargs):
            awaitable.close()
            outcome = next(outcomes)
            if isinstance(outcome, Exception):
                raise outcome
            return outcome

        with patch(
            "bot.services.join_verification._bounded_telegram_call",
            new=AsyncMock(side_effect=bounded),
        ):
            self.assertTrue(await restrict_new_member(bot, -100, 40))

    async def test_restore_timeout_confirms_applied_remote_state(self) -> None:
        bot = SimpleNamespace(
            restrict_chat_member=AsyncMock(),
            get_chat_member=AsyncMock(),
        )
        outcomes = iter(
            [
                asyncio.TimeoutError(),
                SimpleNamespace(status="member", can_send_messages=True),
            ]
        )

        async def bounded(awaitable, **_kwargs):
            awaitable.close()
            outcome = next(outcomes)
            if isinstance(outcome, Exception):
                raise outcome
            return outcome

        with patch(
            "bot.services.join_verification._bounded_telegram_call",
            new=AsyncMock(side_effect=bounded),
        ):
            self.assertTrue(await restore_member_permissions(bot, -100, 41))

    async def test_restore_treats_already_left_member_as_complete(self) -> None:
        bot = SimpleNamespace(
            restrict_chat_member=AsyncMock(side_effect=RuntimeError("user not found")),
            get_chat_member=AsyncMock(return_value=SimpleNamespace(status="left")),
        )

        self.assertTrue(await restore_member_permissions(bot, -100, 42))
        bot.get_chat_member.assert_awaited_once_with(-100, 42)

    async def test_restore_owner_rejection_confirms_released_state(self) -> None:
        method = SimpleNamespace()
        bot = SimpleNamespace(
            restrict_chat_member=AsyncMock(
                side_effect=TelegramBadRequest(
                    method=method,
                    message="Bad Request: can't restrict chat owner",
                )
            ),
            get_chat_member=AsyncMock(
                return_value=SimpleNamespace(status="creator")
            ),
        )

        self.assertTrue(await restore_member_permissions(bot, -100, 43))
        bot.get_chat_member.assert_awaited_once_with(-100, 43)

    async def test_false_bot_api_results_are_treated_as_failures(self) -> None:
        bot = SimpleNamespace(
            restrict_chat_member=AsyncMock(return_value=False),
            ban_chat_member=AsyncMock(return_value=False),
            unban_chat_member=AsyncMock(return_value=False),
        )

        self.assertFalse(await restrict_new_member(bot, -100, 42))
        self.assertFalse(await ban_member(bot, -100, 42))
        with patch(
            "bot.services.join_verification.asyncio.sleep",
            new=AsyncMock(),
        ):
            self.assertFalse(await kick_member(bot, -100, 42))
        bot.unban_chat_member.assert_not_awaited()

    async def test_ban_response_loss_is_confirmed_from_telegram_state(self) -> None:
        bot = SimpleNamespace(
            ban_chat_member=AsyncMock(side_effect=RuntimeError("response lost")),
            get_chat_member=AsyncMock(return_value=SimpleNamespace(status="kicked")),
        )

        self.assertTrue(await ban_member(bot, -100, 44))
        bot.ban_chat_member.assert_awaited_once_with(
            -100,
            44,
            revoke_messages=True,
        )
        bot.get_chat_member.assert_awaited_once_with(-100, 44)

    async def test_ban_rights_rejection_is_nonretryable_operator_action(self) -> None:
        method = SimpleNamespace()
        bot = SimpleNamespace(
            ban_chat_member=AsyncMock(
                side_effect=TelegramBadRequest(
                    method=method,
                    message=(
                        "Bad Request: not enough rights to "
                        "restrict/unrestrict chat member"
                    ),
                )
            ),
            get_chat_member=AsyncMock(return_value=SimpleNamespace(status="member")),
        )

        result = await ban_member_result(bot, -100, 45)
        self.assertIsNone(result.final_banned)
        self.assertFalse(result.retryable)
        self.assertTrue(result.operator_action_required)
        self.assertFalse(result.group_unreachable)

    async def test_ban_owner_rejection_is_nonretryable_operator_action(self) -> None:
        method = SimpleNamespace()
        bot = SimpleNamespace(
            ban_chat_member=AsyncMock(
                side_effect=TelegramBadRequest(
                    method=method,
                    message="Bad Request: can't remove chat owner",
                )
            ),
            get_chat_member=AsyncMock(return_value=SimpleNamespace(status="creator")),
        )

        result = await ban_member_result(bot, -100, 46)
        self.assertIsNone(result.final_banned)
        self.assertFalse(result.retryable)
        self.assertTrue(result.operator_action_required)

    async def test_ban_unreachable_group_is_nonretryable(self) -> None:
        method = SimpleNamespace()
        bot = SimpleNamespace(
            ban_chat_member=AsyncMock(
                side_effect=TelegramForbiddenError(
                    method=method,
                    message="Forbidden: bot was kicked from the supergroup chat",
                )
            ),
            get_chat_member=AsyncMock(
                side_effect=TelegramForbiddenError(
                    method=method,
                    message="Forbidden: bot was kicked from the supergroup chat",
                )
            ),
        )

        result = await ban_member_result(bot, -100, 47)
        self.assertIsNone(result.final_banned)
        self.assertFalse(result.retryable)
        self.assertTrue(result.group_unreachable)
        self.assertFalse(result.operator_action_required)

    async def test_ban_transient_failure_remains_retryable(self) -> None:
        bot = SimpleNamespace(
            ban_chat_member=AsyncMock(side_effect=RuntimeError("boom")),
            get_chat_member=AsyncMock(side_effect=RuntimeError("boom")),
        )

        result = await ban_member_result(bot, -100, 48)
        self.assertIsNone(result.final_banned)
        self.assertTrue(result.retryable)
        self.assertFalse(result.operator_action_required)
        self.assertFalse(result.group_unreachable)

    async def test_ban_rights_rejection_confirmed_remote_ban_is_success(self) -> None:
        method = SimpleNamespace()
        bot = SimpleNamespace(
            ban_chat_member=AsyncMock(
                side_effect=TelegramBadRequest(
                    method=method,
                    message="Bad Request: not enough rights to restrict/unrestrict chat member",
                )
            ),
            get_chat_member=AsyncMock(return_value=SimpleNamespace(status="kicked")),
        )

        result = await ban_member_result(bot, -100, 49)
        self.assertIs(result.final_banned, True)
        self.assertFalse(result.retryable)
        self.assertFalse(result.operator_action_required)

    async def test_reconciliation_child_propagates_deterministic_failure(self) -> None:
        method = SimpleNamespace()
        bot = SimpleNamespace(
            ban_chat_member=AsyncMock(
                side_effect=TelegramBadRequest(
                    method=method,
                    message=(
                        "Bad Request: not enough rights to "
                        "restrict/unrestrict chat member"
                    ),
                )
            ),
            unban_chat_member=AsyncMock(return_value=True),
            get_chat_member=AsyncMock(return_value=SimpleNamespace(status="member")),
        )
        preserve_ban = AsyncMock(side_effect=[False, True, True])

        result = await enforce_ban_with_policy_reconciliation_result(
            bot,
            -100,
            50,
            preserve_ban,
        )

        self.assertIsNone(result.final_banned)
        self.assertFalse(result.retryable)
        self.assertTrue(result.operator_action_required)

    async def test_post_unban_reban_propagates_deterministic_failure(self) -> None:
        method = SimpleNamespace()
        bot = SimpleNamespace(
            ban_chat_member=AsyncMock(
                side_effect=TelegramBadRequest(
                    method=method,
                    message=(
                        "Bad Request: not enough rights to "
                        "restrict/unrestrict chat member"
                    ),
                )
            ),
            unban_chat_member=AsyncMock(return_value=True),
            restrict_chat_member=AsyncMock(return_value=True),
            get_chat_member=AsyncMock(return_value=SimpleNamespace(status="member")),
        )
        preserve_ban = AsyncMock(side_effect=[False, False, True, True])

        result = await enforce_ban_with_policy_reconciliation_result(
            bot,
            -100,
            51,
            preserve_ban,
        )

        self.assertIsNone(result.final_banned)
        self.assertFalse(result.retryable)
        self.assertTrue(result.operator_action_required)

    async def test_restriction_reconciliation_propagates_rights_failure(self) -> None:
        method = SimpleNamespace()
        bot = SimpleNamespace(
            ban_chat_member=AsyncMock(return_value=True),
            unban_chat_member=AsyncMock(return_value=True),
            restrict_chat_member=AsyncMock(
                side_effect=TelegramBadRequest(
                    method=method,
                    message=(
                        "Bad Request: not enough rights to "
                        "restrict/unrestrict chat member"
                    ),
                )
            ),
            get_chat_member=AsyncMock(return_value=SimpleNamespace(status="member")),
        )
        preserve_ban = AsyncMock(side_effect=[False, False, False, False, False])

        result = await enforce_ban_with_policy_reconciliation_result(
            bot,
            -100,
            52,
            preserve_ban,
            AsyncMock(return_value=True),
        )

        self.assertIsNone(result.final_banned)
        self.assertFalse(result.retryable)
        self.assertTrue(result.operator_action_required)

    async def test_ban_created_after_reconciliation_is_reapplied(self) -> None:
        bot = SimpleNamespace(
            ban_chat_member=AsyncMock(return_value=True),
            unban_chat_member=AsyncMock(return_value=True),
            get_chat_member=AsyncMock(return_value=SimpleNamespace(status="member")),
            restrict_chat_member=AsyncMock(return_value=True),
        )
        # The policy is released for the unban, then a new durable ban appears
        # before the post-mutation confirmation and must be applied remotely.
        preserve_ban = AsyncMock(side_effect=[False, True, True, True])

        result = await enforce_ban_with_policy_reconciliation_result(
            bot,
            -100,
            53,
            preserve_ban,
        )

        self.assertIs(result.final_banned, True)
        self.assertEqual(bot.ban_chat_member.await_count, 1)
        bot.unban_chat_member.assert_awaited_once_with(-100, 53, only_if_banned=True)

    async def test_policy_read_failure_after_ban_remains_retryable(self) -> None:
        bot = SimpleNamespace(
            ban_chat_member=AsyncMock(return_value=True),
        )
        preserve_ban = AsyncMock(
            side_effect=[True, RuntimeError("database unavailable")]
        )

        result = await enforce_ban_with_policy_reconciliation_result(
            bot,
            -100,
            54,
            preserve_ban,
        )

        self.assertIsNone(result.final_banned)
        self.assertTrue(result.retryable)
        bot.ban_chat_member.assert_awaited_once_with(
            -100,
            54,
            revoke_messages=True,
        )

    async def test_release_committed_during_ban_is_reconciled_remotely(self) -> None:
        policy = {"blocked": True}

        async def ban_and_release(*_args, **_kwargs):
            policy["blocked"] = False
            return True

        async def preserve_ban() -> bool:
            return policy["blocked"]

        bot = SimpleNamespace(
            ban_chat_member=AsyncMock(side_effect=ban_and_release),
            unban_chat_member=AsyncMock(return_value=True),
            restrict_chat_member=AsyncMock(return_value=True),
        )

        result = await enforce_ban_with_policy_reconciliation_result(
            bot,
            -100,
            55,
            preserve_ban,
        )

        self.assertIs(result.final_banned, False)
        self.assertIs(result.final_restricted, False)
        bot.ban_chat_member.assert_awaited_once()
        bot.unban_chat_member.assert_awaited_once_with(
            -100,
            55,
            only_if_banned=True,
        )

    async def test_restriction_removed_during_restrict_is_restored(self) -> None:
        policy = {"restricted": True}

        async def mutate_permissions(*_args, **_kwargs):
            if policy["restricted"]:
                policy["restricted"] = False
            return True

        bot = SimpleNamespace(
            unban_chat_member=AsyncMock(return_value=True),
            restrict_chat_member=AsyncMock(side_effect=mutate_permissions),
        )

        result = await enforce_ban_with_policy_reconciliation_result(
            bot,
            -100,
            56,
            AsyncMock(return_value=False),
            AsyncMock(side_effect=lambda: policy["restricted"]),
        )

        self.assertIs(result.final_banned, False)
        self.assertIs(result.final_restricted, False)
        self.assertEqual(bot.restrict_chat_member.await_count, 2)

    async def test_restriction_created_during_restore_is_reapplied(self) -> None:
        policy = {"restricted": False}
        permission_calls = 0

        async def mutate_permissions(*_args, **_kwargs):
            nonlocal permission_calls
            permission_calls += 1
            if permission_calls == 1:
                policy["restricted"] = True
            return True

        bot = SimpleNamespace(
            unban_chat_member=AsyncMock(return_value=True),
            restrict_chat_member=AsyncMock(side_effect=mutate_permissions),
        )

        result = await enforce_ban_with_policy_reconciliation_result(
            bot,
            -100,
            60,
            AsyncMock(return_value=False),
            AsyncMock(side_effect=lambda: policy["restricted"]),
        )

        self.assertIs(result.final_banned, False)
        self.assertIs(result.final_restricted, True)
        self.assertEqual(bot.restrict_chat_member.await_count, 2)

    async def test_unban_owner_rejection_confirms_released_state(self) -> None:
        method = SimpleNamespace()
        bot = SimpleNamespace(
            unban_chat_member=AsyncMock(
                side_effect=TelegramBadRequest(
                    method=method,
                    message="Bad Request: can't remove chat owner",
                )
            ),
            get_chat_member=AsyncMock(
                return_value=SimpleNamespace(status="creator")
            ),
            restrict_chat_member=AsyncMock(return_value=True),
        )

        result = await enforce_ban_with_policy_reconciliation_result(
            bot,
            -100,
            57,
            AsyncMock(return_value=False),
        )

        self.assertIs(result.final_banned, False)
        self.assertFalse(result.retryable)
        bot.get_chat_member.assert_awaited_once_with(-100, 57)

    async def test_unban_rights_rejection_while_still_kicked_is_terminal(self) -> None:
        method = SimpleNamespace()
        bot = SimpleNamespace(
            unban_chat_member=AsyncMock(
                side_effect=TelegramBadRequest(
                    method=method,
                    message=(
                        "Bad Request: not enough rights to "
                        "restrict/unrestrict chat member"
                    ),
                )
            ),
            get_chat_member=AsyncMock(
                return_value=SimpleNamespace(status=ChatMemberStatus.KICKED)
            ),
        )

        result = await enforce_ban_with_policy_reconciliation_result(
            bot,
            -100,
            58,
            AsyncMock(return_value=False),
        )

        self.assertIsNone(result.final_banned)
        self.assertFalse(result.retryable)
        self.assertTrue(result.operator_action_required)

    async def test_reconciliation_cancellation_waits_for_cleanup(self) -> None:
        started = asyncio.Event()
        release = asyncio.Event()

        async def preserve_ban() -> bool:
            started.set()
            await release.wait()
            return False

        bot = SimpleNamespace(
            unban_chat_member=AsyncMock(return_value=True),
            restrict_chat_member=AsyncMock(return_value=True),
        )
        task = asyncio.create_task(
            reconcile_moderation_ban_after_lost_lease(
                bot,
                -100,
                59,
                preserve_ban,
            )
        )
        await started.wait()
        task.cancel()
        release.set()

        with self.assertRaises(asyncio.CancelledError):
            await task
        bot.unban_chat_member.assert_awaited_once_with(
            -100,
            59,
            only_if_banned=True,
        )

    async def test_kick_preserves_existing_durable_ban_policy(self) -> None:
        bot = SimpleNamespace(
            ban_chat_member=AsyncMock(return_value=True),
            unban_chat_member=AsyncMock(return_value=True),
        )
        preserve_ban = AsyncMock(return_value=True)

        self.assertTrue(
            await kick_member(
                bot,
                -100,
                45,
                preserve_ban=preserve_ban,
            )
        )
        preserve_ban.assert_awaited_once()
        bot.unban_chat_member.assert_not_awaited()
        bot.ban_chat_member.assert_awaited_once_with(
            -100,
            45,
            revoke_messages=True,
        )

    async def test_kick_reenforces_ban_created_during_removal(self) -> None:
        bot = SimpleNamespace(
            ban_chat_member=AsyncMock(return_value=True),
            unban_chat_member=AsyncMock(return_value=True),
        )
        preserve_ban = AsyncMock(side_effect=(False, True))

        self.assertTrue(
            await kick_member(
                bot,
                -100,
                46,
                preserve_ban=preserve_ban,
            )
        )
        bot.unban_chat_member.assert_awaited_once_with(
            -100,
            46,
            only_if_banned=True,
        )
        self.assertEqual(
            bot.ban_chat_member.await_args_list,
            [
                call(-100, 46, revoke_messages=True),
                call(-100, 46, revoke_messages=True),
            ],
        )

    async def test_lost_moderation_lease_unbans_when_policy_was_removed(self) -> None:
        bot = SimpleNamespace(
            unban_chat_member=AsyncMock(return_value=True),
            ban_chat_member=AsyncMock(return_value=True),
            restrict_chat_member=AsyncMock(return_value=True),
        )
        preserve_ban = AsyncMock(return_value=False)

        self.assertTrue(
            await reconcile_moderation_ban_after_lost_lease(
                bot,
                -100,
                46,
                preserve_ban,
            )
        )
        bot.unban_chat_member.assert_awaited_once_with(
            -100,
            46,
            only_if_banned=True,
        )
        bot.restrict_chat_member.assert_awaited_once()
        bot.ban_chat_member.assert_not_awaited()

    async def test_manual_unban_reenforces_policy_created_during_race(self) -> None:
        from bot.services.join_verification import unban_member

        bot = SimpleNamespace(
            unban_chat_member=AsyncMock(return_value=True),
            ban_chat_member=AsyncMock(return_value=True),
        )
        preserve_ban = AsyncMock(return_value=True)

        self.assertFalse(
            await unban_member(
                bot,
                -100,
                47,
                preserve_ban=preserve_ban,
            )
        )
        bot.ban_chat_member.assert_awaited_once_with(
            -100,
            47,
            revoke_messages=True,
        )
        bot.unban_chat_member.assert_not_awaited()

    async def test_kick_uses_revoke_then_unbans_for_rejoin(self) -> None:
        bot = SimpleNamespace(
            ban_chat_member=AsyncMock(),
            unban_chat_member=AsyncMock(return_value=True),
        )

        self.assertTrue(await kick_member(bot, -100, 42))
        bot.unban_chat_member.assert_awaited_once_with(
            -100,
            42,
            only_if_banned=True,
        )
        bot.ban_chat_member.assert_awaited_once_with(
            -100,
            42,
            revoke_messages=True,
        )

    async def test_cancellation_during_unban_does_not_cancel_cleanup(self) -> None:
        unban_started = asyncio.Event()
        release_unban = asyncio.Event()
        cleanup_cancelled = False

        async def slow_unban(*args: object, **kwargs: object) -> bool:
            nonlocal cleanup_cancelled
            del args, kwargs
            unban_started.set()
            try:
                await release_unban.wait()
            except asyncio.CancelledError:
                cleanup_cancelled = True
                raise
            return True

        bot = SimpleNamespace(
            ban_chat_member=AsyncMock(return_value=True),
            unban_chat_member=AsyncMock(side_effect=slow_unban),
        )
        task = asyncio.create_task(kick_member(bot, -100, 43))
        await asyncio.wait_for(unban_started.wait(), timeout=1.0)
        task.cancel()
        await asyncio.sleep(0)
        release_unban.set()

        with self.assertRaises(asyncio.CancelledError):
            await task

        self.assertFalse(cleanup_cancelled)
        bot.unban_chat_member.assert_awaited_once_with(
            -100,
            43,
            only_if_banned=True,
        )
        bot.ban_chat_member.assert_awaited_once_with(
            -100,
            43,
            revoke_messages=True,
        )

    async def test_stubborn_telegram_children_are_capacity_limited_and_boundedly_drained(
        self,
    ) -> None:
        import bot.services.join_verification as verification_module

        timeout_started = asyncio.Event()
        cancellation_started = asyncio.Event()
        release = asyncio.Event()
        rejected_started = asyncio.Event()

        async def stubborn_call(started: asyncio.Event) -> bool:
            started.set()
            while not release.is_set():
                try:
                    await release.wait()
                except asyncio.CancelledError:
                    continue
            return True

        async def rejected_call() -> bool:
            rejected_started.set()
            return True

        try:
            with (
                patch.object(verification_module, "_TELEGRAM_CALL_CAPACITY", 2),
                patch.object(
                    verification_module,
                    "_TELEGRAM_CALL_BACKPRESSURE_SECONDS",
                    0.01,
                ),
            ):
                with execution_priority_scope(ExecutionPriority.HIGH):
                    owner = asyncio.create_task(
                        verification_module._bounded_telegram_call(
                            stubborn_call(timeout_started),
                            timeout_seconds=0.01,
                        )
                    )
                await asyncio.wait_for(timeout_started.wait(), timeout=1.0)
                with self.assertRaises(asyncio.TimeoutError):
                    await owner

                with execution_priority_scope(ExecutionPriority.CRITICAL):
                    cancelled_owner = asyncio.create_task(
                        verification_module._bounded_telegram_call(
                            stubborn_call(cancellation_started),
                            timeout_seconds=60.0,
                        )
                    )
                await asyncio.wait_for(cancellation_started.wait(), timeout=1.0)
                cancelled_owner.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await cancelled_owner

                self.assertEqual(
                    len(verification_module._active_telegram_call_tasks()),
                    2,
                )
                with self.assertRaises(verification_module._TelegramCallCapacityError):
                    await verification_module._bounded_telegram_call(
                        rejected_call(),
                        timeout_seconds=1.0,
                    )
                self.assertFalse(rejected_started.is_set())

                before = asyncio.get_running_loop().time()
                await verification_module.flush_kick_cleanup_tasks(
                    timeout_seconds=0.02
                )
                self.assertLess(
                    asyncio.get_running_loop().time() - before,
                    0.2,
                )
                self.assertEqual(
                    len(verification_module._active_telegram_call_tasks()),
                    2,
                )
        finally:
            release.set()
            await verification_module.flush_join_verification_telegram_tasks(
                timeout_seconds=0.5
            )

        self.assertEqual(
            len(verification_module._active_telegram_call_tasks()),
            0,
        )

    async def test_concurrent_telegram_starts_never_exceed_reserved_capacity(self) -> None:
        import bot.services.join_verification as verification_module

        release = asyncio.Event()
        capacity_reached = asyncio.Event()
        active = 0
        peak = 0
        total_started = 0

        async def stubborn_call() -> bool:
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
                return True
            finally:
                active -= 1

        owners: list[asyncio.Task] = []
        try:
            with (
                patch.object(verification_module, "_TELEGRAM_CALL_CAPACITY", 2),
                patch.object(
                    verification_module,
                    "_TELEGRAM_CALL_BACKPRESSURE_SECONDS",
                    0.02,
                ),
            ):
                with execution_priority_scope(ExecutionPriority.CRITICAL):
                    owners = [
                        asyncio.create_task(
                            verification_module._bounded_telegram_call(
                                stubborn_call(),
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
                    len(verification_module._active_telegram_call_tasks()),
                    2,
                )
        finally:
            for owner in owners:
                owner.cancel()
            if owners:
                await asyncio.gather(*owners, return_exceptions=True)
            release.set()
            await verification_module.flush_join_verification_telegram_tasks(
                timeout_seconds=0.5
            )

    async def test_security_saturation_keeps_critical_reserve_available(self) -> None:
        import bot.services.join_verification as verification_module

        release = asyncio.Event()
        high_started = [asyncio.Event() for _ in range(3)]
        critical_started = asyncio.Event()

        async def hold(started: asyncio.Event) -> bool:
            started.set()
            await release.wait()
            return True

        owners: list[asyncio.Task[object]] = []
        try:
            with (
                patch.object(verification_module, "_TELEGRAM_CALL_CAPACITY", 4),
                patch.object(
                    verification_module,
                    "_TELEGRAM_CALL_CRITICAL_RESERVE",
                    1,
                ),
                patch.object(
                    verification_module,
                    "_TELEGRAM_CALL_NORMAL_CAPACITY",
                    3,
                ),
                patch.object(
                    verification_module,
                    "_TELEGRAM_CALL_BACKPRESSURE_SECONDS",
                    0.03,
                ),
            ):
                with execution_priority_scope(ExecutionPriority.HIGH):
                    owners.extend(
                        asyncio.create_task(
                            verification_module._bounded_telegram_call(
                                hold(started),
                                timeout_seconds=60.0,
                            )
                        )
                        for started in high_started
                    )
                await asyncio.gather(
                    *(
                        asyncio.wait_for(started.wait(), timeout=1.0)
                        for started in high_started
                    )
                )

                with execution_priority_scope(ExecutionPriority.CRITICAL):
                    critical = asyncio.create_task(
                        verification_module._bounded_telegram_call(
                            hold(critical_started),
                            timeout_seconds=60.0,
                        )
                    )
                owners.append(critical)
                await asyncio.wait_for(critical_started.wait(), timeout=1.0)

                rejected_started = asyncio.Event()
                with execution_priority_scope(ExecutionPriority.HIGH):
                    rejected = asyncio.create_task(
                        verification_module._bounded_telegram_call(
                            hold(rejected_started),
                            timeout_seconds=1.0,
                        )
                    )
                with self.assertRaises(
                    verification_module._TelegramCallCapacityError
                ):
                    await rejected
                self.assertFalse(rejected_started.is_set())
        finally:
            release.set()
            if owners:
                await asyncio.gather(*owners, return_exceptions=True)
            await verification_module.flush_join_verification_telegram_tasks(
                timeout_seconds=0.5
            )

    async def test_started_telegram_timeout_is_not_misclassified_as_saturation(
        self,
    ) -> None:
        import bot.services.join_verification as verification_module

        async def fail_after_admission() -> bool:
            raise asyncio.TimeoutError("upstream timeout")

        with self.assertRaisesRegex(asyncio.TimeoutError, "upstream timeout"):
            with execution_priority_scope(ExecutionPriority.CRITICAL):
                await verification_module._bounded_telegram_call(
                    fail_after_admission(),
                    timeout_seconds=1.0,
                )

    async def test_stale_cancellation_resistant_gate_saturation_is_fatal(self) -> None:
        import bot.services.join_verification as verification_module

        release = asyncio.Event()
        started = [asyncio.Event(), asyncio.Event()]

        async def stubborn(marker: asyncio.Event) -> bool:
            marker.set()
            while not release.is_set():
                try:
                    await release.wait()
                except asyncio.CancelledError:
                    continue
            return True

        owners: list[asyncio.Task[object]] = []
        try:
            with (
                patch.object(verification_module, "_TELEGRAM_CALL_CAPACITY", 2),
                patch.object(
                    verification_module,
                    "_TELEGRAM_CALL_BACKPRESSURE_SECONDS",
                    0.02,
                ),
            ):
                with execution_priority_scope(ExecutionPriority.CRITICAL):
                    owners = [
                        asyncio.create_task(
                            verification_module._bounded_telegram_call(
                                stubborn(marker),
                                timeout_seconds=0.1,
                            )
                        )
                        for marker in started
                    ]
                await asyncio.gather(
                    *(asyncio.wait_for(marker.wait(), timeout=1.0) for marker in started)
                )
                await asyncio.gather(*owners, return_exceptions=True)

                state = verification_module._telegram_call_state()
                stale = time.monotonic() - 121.0
                for task in tuple(state.orphan_started_at):
                    state.orphan_started_at[task] = stale
                    state.active_started_at[task] = stale

                snapshot = (
                    verification_module.join_verification_telegram_health_snapshot()
                )
                self.assertTrue(snapshot["fatal"])
                self.assertFalse(snapshot["ok"])
                self.assertTrue(snapshot["exhausted"])
                self.assertEqual(snapshot["orphan_count"], 2)
                self.assertGreaterEqual(snapshot["oldest_orphan_seconds"], 120.0)
        finally:
            release.set()
            await verification_module.flush_join_verification_telegram_tasks(
                timeout_seconds=0.5
            )

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
    async def test_unban_journal_is_inserted_without_existing_verification(self) -> None:
        now = now_shanghai_naive()
        async with self.session_factory() as session:
            recovery = await lease_join_verification_for_unban(
                session,
                -100,
                95,
                now=now,
            )
            self.assertIsNotNone(recovery)
            await session.commit()

        async with self.session_factory() as session:
            row = await get_join_verification(session, -100, 95)
            self.assertIsNotNone(row)
            self.assertEqual(row.status, VERIFICATION_STATUS_UNBANNING)
            self.assertEqual(row.lease_until, recovery.lease_until)

    async def test_global_unban_creates_per_group_placeholder_journal(self) -> None:
        async with self.session_factory() as session:
            recoveries = await lease_join_verifications_for_user_unban(
                session,
                93,
                group_ids=(-100,),
                now=now_shanghai_naive(),
            )
            await session.commit()
        self.assertEqual([(item.group_id, item.user_id) for item in recoveries], [(-100, 93)])
        async with self.session_factory() as session:
            row = await get_join_verification(session, -100, 93)
            self.assertEqual(row.status, VERIFICATION_STATUS_UNBANNING)

    async def test_unban_journal_invalidates_stale_enforcement_completion(self) -> None:
        now = now_shanghai_naive()
        deadline = now - CHALLENGE_SUBMIT_GRACE - timedelta(seconds=2)
        old_lease = now + timedelta(seconds=90)
        async with self.session_factory() as session:
            await upsert_join_verification(
                session,
                group_id=-100,
                user_id=94,
                deadline_at=deadline,
                kind=VERIFICATION_KIND_MODERATION,
            )
            await session.commit()
            row = await get_join_verification(session, -100, 94)
            self.assertTrue(
                await claim_join_verification(
                    session,
                    verification_id=int(row.id),
                    deadline_at=deadline,
                    kind=VERIFICATION_KIND_MODERATION,
                    now=now,
                    expired=True,
                    lease_until=old_lease,
                )
            )
            await session.commit()
            recovery = await lease_join_verification_for_unban(
                session,
                -100,
                94,
                now=now,
            )
            self.assertIsNotNone(recovery)
            await session.commit()
            self.assertFalse(
                await complete_leased_join_verification(
                    session,
                    verification_id=int(row.id),
                    lease_until=old_lease,
                )
            )
            await session.rollback()

        async with self.session_factory() as session:
            row = await get_join_verification(session, -100, 94)
            self.assertIsNotNone(row)
            self.assertEqual(row.status, VERIFICATION_STATUS_UNBANNING)

    async def test_preparing_is_durable_but_not_visible_as_pending_until_activation(self) -> None:
        deadline = now_shanghai_naive() + timedelta(minutes=5)
        async with self.session_factory() as session:
            prepared = await prepare_join_verification(
                session,
                group_id=-100,
                user_id=98,
                deadline_at=deadline,
                display_name="准备中用户",
            )
            self.assertIsNotNone(prepared)
            await session.commit()

        async with self.session_factory() as session:
            row = await get_join_verification(session, -100, 98)
            self.assertEqual(row.status, "preparing")
            self.assertIsNone(await get_pending_verification_for_user(session, 98))
            self.assertTrue(
                await activate_prepared_join_verification(
                    session,
                    prepared=prepared,
                    prompt_message_id=701,
                    deadline_at=deadline,
                )
            )
            await session.commit()

        async with self.session_factory() as session:
            row = await get_pending_verification_for_user(session, 98)
            self.assertIsNotNone(row)
            self.assertEqual(row.status, "pending")
            self.assertIsNone(row.lease_until)
            self.assertEqual(row.prompt_message_id, 701)

    async def test_preparing_does_not_overwrite_other_kind_pending_record(self) -> None:
        async with self.session_factory() as session:
            await upsert_join_verification(
                session,
                group_id=-100,
                user_id=97,
                deadline_at=now_shanghai_naive() + timedelta(minutes=5),
                kind=VERIFICATION_KIND_MODERATION,
            )
            await session.commit()
            self.assertIsNone(
                await prepare_join_verification(
                    session,
                    group_id=-100,
                    user_id=97,
                    deadline_at=now_shanghai_naive() + timedelta(minutes=5),
                    kind=VERIFICATION_KIND_JOIN,
                )
            )

        async with self.session_factory() as session:
            row = await get_join_verification(session, -100, 97)
            self.assertEqual(row.kind, VERIFICATION_KIND_MODERATION)
            self.assertEqual(row.status, "pending")

    async def test_abort_losing_prepare_cas_does_not_restore_active_challenge(self) -> None:
        deadline = now_shanghai_naive() + timedelta(minutes=5)
        async with self.session_factory() as session:
            prepared = await prepare_join_verification(
                session,
                group_id=-100,
                user_id=96,
                deadline_at=deadline,
            )
            self.assertIsNotNone(prepared)
            await session.commit()
            self.assertTrue(
                await activate_prepared_join_verification(
                    session,
                    prepared=prepared,
                    prompt_message_id=702,
                    deadline_at=deadline,
                )
            )
            await session.commit()

            bot = SimpleNamespace(restrict_chat_member=AsyncMock())
            self.assertFalse(
                await abort_prepared_join_verification(
                    bot,
                    session,
                    prepared=prepared,
                    prompt_message_id=702,
                )
            )
            bot.restrict_chat_member.assert_not_awaited()

        async with self.session_factory() as session:
            row = await get_join_verification(session, -100, 96)
            self.assertIsNotNone(row)
            self.assertEqual(row.status, "pending")
            self.assertEqual(row.prompt_message_id, 702)

    async def test_expired_enforcement_lease_survives_worker_crash_and_recovers(self) -> None:
        now = now_shanghai_naive()
        deadline = now - CHALLENGE_SUBMIT_GRACE - timedelta(seconds=5)
        async with self.session_factory() as session:
            await upsert_join_verification(
                session,
                group_id=-100,
                user_id=99,
                deadline_at=deadline,
            )
            await session.commit()

        async with self.session_factory() as session:
            record = await get_join_verification(session, -100, 99)
            lease_until = now + timedelta(seconds=90)
            self.assertTrue(
                await lease_expired_join_verification(
                    session,
                    record=record,
                    now=now,
                    lease_until=lease_until,
                )
            )
            await session.commit()

        async with self.session_factory() as session:
            record = await get_join_verification(session, -100, 99)
            self.assertEqual(record.status, "enforcing")
            self.assertFalse(
                await claim_join_verification(
                    session,
                    verification_id=record.id,
                    deadline_at=record.deadline_at,
                    kind=record.kind,
                    now=now,
                    expired=False,
                )
            )
            self.assertEqual(await list_expired_verifications(session, now=now), [])
            stale = await list_expired_verifications(
                session,
                now=lease_until + timedelta(seconds=1),
            )
            self.assertEqual([item.id for item in stale], [record.id])

            retry_at = now + timedelta(seconds=60)
            self.assertTrue(
                await release_join_verification_lease(
                    session,
                    verification_id=record.id,
                    lease_until=lease_until,
                    retry_at=retry_at,
                )
            )
            await session.commit()

        async with self.session_factory() as session:
            record = await get_join_verification(session, -100, 99)
            self.assertEqual(record.status, "pending")
            second_now = retry_at + CHALLENGE_SUBMIT_GRACE + timedelta(seconds=1)
            second_lease = second_now + timedelta(seconds=90)
            self.assertTrue(
                await lease_expired_join_verification(
                    session,
                    record=record,
                    now=second_now,
                    lease_until=second_lease,
                )
            )
            self.assertTrue(
                await complete_leased_join_verification(
                    session,
                    verification_id=record.id,
                    lease_until=second_lease,
                )
            )
            await session.commit()
            self.assertIsNone(await get_join_verification(session, -100, 99))

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

    async def test_pending_and_expired_claims_each_lease_only_once(self) -> None:
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

            pending_claim = await get_join_verification(session, -100, 7)
            expired_claim = await get_join_verification(session, -100, 8)
            self.assertEqual(pending_claim.status, "enforcing")
            self.assertIsNotNone(pending_claim.lease_until)
            self.assertEqual(expired_claim.status, "enforcing")
            self.assertIsNotNone(expired_claim.lease_until)


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
                indexes = await conn.exec_driver_sql(
                    "SELECT name FROM sqlite_master "
                    "WHERE type = 'index' AND tbl_name = 'join_verifications'"
                )
                index_names = {str(row[0]) for row in indexes.fetchall()}
            self.assertIn("AUTOINCREMENT", table_sql.upper())
            self.assertIn("ix_join_verifications_status_lease", index_names)

            async with session_factory() as session:
                preserved = await get_join_verification(session, -100, 51)
                self.assertIsNotNone(preserved)
                self.assertEqual(preserved.id, 41)
                self.assertEqual(preserved.provider, "hcaptcha")
                self.assertEqual(preserved.status, "pending")
                self.assertIsNone(preserved.lease_until)
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

    async def test_captcha_era_rows_become_permission_release_work_items(self) -> None:
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
                    answer VARCHAR(16) NOT NULL,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    prompt_message_id BIGINT NOT NULL,
                    deadline_at DATETIME NOT NULL,
                    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                INSERT INTO join_verifications (
                    id, group_id, user_id, answer, attempts,
                    prompt_message_id, deadline_at
                ) VALUES (
                    9, -200, 88, '1234', 0, 321, '2099-01-01 00:00:00'
                );
                """
            )
            connection.commit()
        finally:
            connection.close()

        engine = None
        try:
            engine, session_factory = await init_db(
                f"sqlite+aiosqlite:///{db_path}"
            )
            async with session_factory() as session:
                recovered = await get_join_verification(session, -200, 88)
                self.assertIsNotNone(recovered)
                self.assertEqual(recovered.id, 9)
                self.assertEqual(recovered.status, "releasing")
                self.assertIsNotNone(recovered.lease_until)
                self.assertEqual(recovered.prompt_message_id, 321)
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
            get_chat_member=AsyncMock(
                return_value=SimpleNamespace(status="member", can_send_messages=True)
            ),
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

    async def test_clean_join_mutes_before_profile_screening(self) -> None:
        from bot.handlers import membership

        event = _join_event(user_id=909)
        fake_moderation = SimpleNamespace(
            check_rules=AsyncMock(return_value=(False, "", None))
        )

        async def restrict_chat_member(*_args, **_kwargs):
            permissions = _kwargs.get("permissions")
            if permissions is not None and not permissions.can_send_messages:
                fake_moderation.check_rules.assert_not_awaited()
                event.bot.get_chat.assert_not_awaited()
            return True

        event.bot.restrict_chat_member = AsyncMock(side_effect=restrict_chat_member)
        async with self.session_factory() as session:
            with (
                patch("bot.handlers.membership.ModerationService", return_value=fake_moderation),
                patch("bot.handlers.membership._build_llm", return_value=object()),
            ):
                await membership.on_member_join(event, session=session, settings=_settings())
            await session.commit()

        event.bot.restrict_chat_member.assert_awaited_once()
        fake_moderation.check_rules.assert_awaited_once()

    async def test_join_racing_messages_are_swept_after_mute(self) -> None:
        from bot.services.recent_messages import (
            clear_recent_member_messages,
            record_group_message,
        )

        clear_recent_member_messages()
        self.addCleanup(clear_recent_member_messages)
        event = _join_event(user_id=908)
        event.bot.delete_messages = AsyncMock(return_value=True)
        # Simulate messages the member raced in before the join update was
        # processed: the gate middleware records them, the join flow marks the
        # join, and the post-restrict sweep must retract them.
        record_group_message(-100, 908, 501)
        record_group_message(-100, 908, 502)
        await self._run_join(event, _settings())

        event.bot.restrict_chat_member.assert_awaited_once()
        event.bot.delete_messages.assert_awaited_once_with(
            chat_id=-100,
            message_ids=[501, 502],
        )

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

    async def test_duplicate_join_releases_read_transaction_before_restrict(self) -> None:
        async with self.session_factory() as session:
            await upsert_join_verification(
                session,
                group_id=-100,
                user_id=919,
                deadline_at=now_shanghai_naive() + timedelta(minutes=5),
                prompt_message_id=700,
            )
            await session.commit()

        event = _join_event(user_id=919)
        fake_moderation = SimpleNamespace(
            check_rules=AsyncMock(return_value=(False, "", None))
        )
        async with self.session_factory() as session:
            async def restrict_chat_member(*_args, **_kwargs):
                self.assertFalse(session.in_transaction())
                return True

            event.bot.restrict_chat_member = AsyncMock(
                side_effect=restrict_chat_member
            )
            with (
                patch(
                    "bot.handlers.membership.ModerationService",
                    return_value=fake_moderation,
                ),
                patch("bot.handlers.membership._build_llm", return_value=object()),
            ):
                await membership.on_member_join(
                    event,
                    session=session,
                    settings=_settings(),
                )

        event.bot.restrict_chat_member.assert_awaited_once()

    async def test_duplicate_join_restrict_failure_preserves_old_challenge(self) -> None:
        old_deadline = now_shanghai_naive() + timedelta(minutes=5)
        async with self.session_factory() as session:
            await upsert_join_verification(
                session,
                group_id=-100,
                user_id=928,
                deadline_at=old_deadline,
                prompt_message_id=700,
            )
            await session.commit()

        event = _join_event(user_id=928)
        event.bot.restrict_chat_member = AsyncMock(
            side_effect=RuntimeError("missing rights")
        )
        await self._run_join(event, _settings())

        event.bot.send_message.assert_not_awaited()
        event.bot.delete_message.assert_not_awaited()
        async with self.session_factory() as session:
            row = await get_join_verification(session, -100, 928)
            self.assertIsNotNone(row)
            self.assertEqual(row.status, "pending")
            self.assertEqual(row.deadline_at, old_deadline)
            self.assertEqual(row.prompt_message_id, 700)

    async def test_duplicate_join_prompt_failure_preserves_old_challenge(self) -> None:
        old_deadline = now_shanghai_naive() + timedelta(minutes=5)
        async with self.session_factory() as session:
            await upsert_join_verification(
                session,
                group_id=-100,
                user_id=929,
                deadline_at=old_deadline,
                prompt_message_id=701,
            )
            await session.commit()

        event = _join_event(user_id=929)
        event.bot.send_message = AsyncMock(side_effect=RuntimeError("flood"))
        await self._run_join(event, _settings())

        # The pre-existing mute/work item remains authoritative; setup failure
        # must not restore permissions or delete its still-valid prompt.
        event.bot.restrict_chat_member.assert_awaited_once()
        event.bot.delete_message.assert_not_awaited()
        async with self.session_factory() as session:
            row = await get_join_verification(session, -100, 929)
            self.assertIsNotNone(row)
            self.assertEqual(row.status, "pending")
            self.assertEqual(row.deadline_at, old_deadline)
            self.assertEqual(row.prompt_message_id, 701)

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

    async def test_violating_join_is_muted_before_screening_then_banned(self) -> None:
        from bot.handlers import membership

        event = _join_event(user_id=914)
        fake_moderation = SimpleNamespace(
            check_rules=AsyncMock(return_value=(True, "昵称含广告", None))
        )

        # The mute must land before the screening LLM sees the profile: the
        # screening window previously left the member free to post.
        async def restrict_chat_member(*_args, **_kwargs):
            permissions = _kwargs.get("permissions")
            if permissions is not None and not permissions.can_send_messages:
                fake_moderation.check_rules.assert_not_awaited()
            return True

        event.bot.restrict_chat_member = AsyncMock(side_effect=restrict_chat_member)
        async with self.session_factory() as session:
            with (
                patch("bot.handlers.membership.ModerationService", return_value=fake_moderation),
                patch("bot.handlers.membership._build_llm", return_value=object()),
            ):
                await membership.on_member_join(event, session=session, settings=_settings())
            await session.commit()

            event.bot.ban_chat_member.assert_awaited_once_with(
                -100,
                914,
                revoke_messages=True,
            )
            first_restrict = event.bot.restrict_chat_member.await_args_list[0]
            self.assertFalse(first_restrict.kwargs["permissions"].can_send_messages)
            # The absorbed challenge's live prompt must not survive the ban.
            event.bot.delete_message.assert_awaited_once_with(-100, 777)
            self.assertIsNone(await get_join_verification(session, -100, 914))

    async def test_banned_rejoin_is_banned_without_verification(self) -> None:
        from bot.handlers import membership

        event = _join_event(user_id=915)
        async with self.session_factory() as session:
            await add_global_ban(session, 915, reason="旧账", created_by=1)
            await session.commit()

            await membership.on_member_join(event, session=session, settings=_settings())

            event.bot.ban_chat_member.assert_awaited_once_with(
                -100,
                915,
                revoke_messages=True,
            )
            event.bot.restrict_chat_member.assert_not_awaited()

    async def test_locally_banned_rejoin_is_durably_rebanned(self) -> None:
        event = _join_event(user_id=930)
        async with self.session_factory() as session:
            session.add(
                UserWarning(
                    group_id=-100,
                    user_id=930,
                    count=3,
                    is_banned=True,
                )
            )
            await session.commit()

            await membership.on_member_join(
                event,
                session=session,
                settings=_settings(),
            )

        event.bot.ban_chat_member.assert_awaited_once_with(
            -100,
            930,
            revoke_messages=True,
        )
        event.bot.get_chat.assert_not_awaited()
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

    async def test_cancelled_join_prompt_compensates_mute_and_preparation(self) -> None:
        event = _join_event(user_id=926)
        event.bot.send_message = AsyncMock(side_effect=asyncio.CancelledError())

        with self.assertRaises(asyncio.CancelledError):
            await self._run_join(event, _settings())

        self.assertEqual(event.bot.restrict_chat_member.await_count, 2)
        restore_call = event.bot.restrict_chat_member.await_args
        self.assertTrue(restore_call.kwargs["permissions"].can_send_messages)
        async with self.session_factory() as session:
            self.assertIsNone(await get_join_verification(session, -100, 926))


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

    async def test_manual_unban_between_generation_check_and_mute_is_restored(self) -> None:
        settings = _settings(join_verification_enabled=False)
        calls = 0

        async def restrict_side_effect(*_args, **_kwargs) -> bool:
            nonlocal calls
            calls += 1
            if calls == 1:
                # Simulate a newer /unban completing after the exact pre-check
                # but before Telegram returns from the stale mute.
                async with self.session_factory() as policy_session:
                    recovery = await lease_join_verification_for_unban(
                        policy_session,
                        -100,
                        928,
                    )
                    self.assertIsNotNone(recovery)
                    await policy_session.commit()
                    completed = await complete_leased_join_verification(
                        policy_session,
                        verification_id=int(recovery.verification_id),
                        lease_until=recovery.lease_until,
                        status=VERIFICATION_STATUS_UNBANNING,
                    )
                    self.assertTrue(completed)
                    await policy_session.commit()
            return True

        bot = SimpleNamespace(
            restrict_chat_member=AsyncMock(side_effect=restrict_side_effect),
            send_message=AsyncMock(return_value=SimpleNamespace(message_id=828)),
            get_chat_member=AsyncMock(),
        )

        async with self.session_factory() as session:
            started = await begin_moderation_challenge(
                bot=bot,
                session=session,
                session_factory=self.session_factory,
                settings=settings,
                group_id=-100,
                user_id=928,
                display_name="竞态用户",
                bot_username="my_bot",
                reason="竞态测试",
            )

        self.assertTrue(started)
        self.assertEqual(bot.restrict_chat_member.await_count, 2)
        restore_call = bot.restrict_chat_member.await_args
        self.assertTrue(restore_call.kwargs["permissions"].can_send_messages)
        bot.send_message.assert_not_awaited()
        async with self.session_factory() as session:
            self.assertIsNone(await get_join_verification(session, -100, 928))

    async def test_manual_unban_during_prompt_reconciles_lost_activation(self) -> None:
        settings = _settings(join_verification_enabled=False)

        async def send_side_effect(*_args, **_kwargs):
            # The stale worker has already muted the member. A newer /unban
            # removes its exact preparation before activation can commit.
            async with self.session_factory() as policy_session:
                recovery = await lease_join_verification_for_unban(
                    policy_session,
                    -100,
                    929,
                )
                self.assertIsNotNone(recovery)
                await policy_session.commit()
                completed = await complete_leased_join_verification(
                    policy_session,
                    verification_id=int(recovery.verification_id),
                    lease_until=recovery.lease_until,
                    status=VERIFICATION_STATUS_UNBANNING,
                )
                self.assertTrue(completed)
                await policy_session.commit()
            return SimpleNamespace(message_id=829)

        bot = SimpleNamespace(
            restrict_chat_member=AsyncMock(return_value=True),
            send_message=AsyncMock(side_effect=send_side_effect),
            delete_message=AsyncMock(return_value=True),
            edit_message_reply_markup=AsyncMock(return_value=True),
            get_chat_member=AsyncMock(),
        )

        async with self.session_factory() as session:
            started = await begin_moderation_challenge(
                bot=bot,
                session=session,
                session_factory=self.session_factory,
                settings=settings,
                group_id=-100,
                user_id=929,
                display_name="提示竞态用户",
                bot_username="my_bot",
                reason="提示竞态测试",
            )

        self.assertTrue(started)
        self.assertEqual(bot.restrict_chat_member.await_count, 2)
        restore_call = bot.restrict_chat_member.await_args
        self.assertTrue(restore_call.kwargs["permissions"].can_send_messages)
        bot.delete_message.assert_awaited_once_with(-100, 829)
        async with self.session_factory() as session:
            self.assertIsNone(await get_join_verification(session, -100, 929))

    async def test_cancelled_moderation_prompt_compensates_mute_and_preparation(self) -> None:
        settings = _settings(join_verification_enabled=False)
        bot = SimpleNamespace(
            restrict_chat_member=AsyncMock(return_value=True),
            get_chat_member=AsyncMock(
                return_value=SimpleNamespace(status="member", can_send_messages=True)
            ),
            send_message=AsyncMock(side_effect=asyncio.CancelledError()),
        )

        async with self.session_factory() as session:
            with self.assertRaises(asyncio.CancelledError):
                await begin_moderation_challenge(
                    bot=bot,
                    session=session,
                    settings=settings,
                    group_id=-100,
                    user_id=927,
                    display_name="取消用户",
                    bot_username="my_bot",
                    reason="测试取消",
                )

        self.assertEqual(bot.restrict_chat_member.await_count, 2)
        async with self.session_factory() as session:
            self.assertIsNone(await get_join_verification(session, -100, 927))

    async def test_expired_rejoin_keeps_enforcing_lease_when_group_ban_fails(self) -> None:
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
                ),
                bot=SimpleNamespace(
                    ban_chat_member=AsyncMock(return_value=False),
                    get_chat_member=AsyncMock(
                        return_value=SimpleNamespace(status="member")
                    ),
                    send_message=AsyncMock(),
                ),
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

        restrict.assert_not_awaited()
        async with self.session_factory() as session:
            retry = await get_join_verification(session, -100, 919)
            warning = await session.scalar(
                select(UserWarning).where(
                    UserWarning.group_id == -100,
                    UserWarning.user_id == 919,
                )
            )
            self.assertIsNotNone(retry)
            self.assertEqual(retry.status, "enforcing")
            self.assertIsNotNone(retry.lease_until)
            self.assertIsNotNone(warning)
            self.assertTrue(warning.is_banned)

    async def test_cancelled_expired_rejoin_keeps_enforcing_lease(self) -> None:
        async with self.session_factory() as session:
            await upsert_join_verification(
                session,
                group_id=-100,
                user_id=958,
                deadline_at=now_shanghai_naive()
                - CHALLENGE_SUBMIT_GRACE
                - timedelta(seconds=1),
                kind=VERIFICATION_KIND_PATROL,
                prompt_message_id=858,
            )
            await session.commit()
            record = await get_join_verification(session, -100, 958)
            event = SimpleNamespace(
                chat=SimpleNamespace(id=-100, ban=AsyncMock()),
                bot=SimpleNamespace(),
            )

            async def cancelled_kick(*_args, **_kwargs):
                async with self.session_factory() as check_session:
                    leased = await get_join_verification(check_session, -100, 958)
                    self.assertEqual(leased.status, "enforcing")
                    self.assertIsNotNone(leased.lease_until)
                raise asyncio.CancelledError()

            with patch(
                "bot.handlers.membership.kick_member",
                new=AsyncMock(side_effect=cancelled_kick),
            ):
                with self.assertRaises(asyncio.CancelledError):
                    await membership._enforce_pending_moderation_challenge(
                        event,
                        session,
                        _settings(),
                        record,
                        display_name="取消用户",
                    )

        async with self.session_factory() as session:
            leased = await get_join_verification(session, -100, 958)
            self.assertIsNotNone(leased)
            self.assertEqual(leased.status, "enforcing")

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
                    ban_chat_member=AsyncMock(return_value=True),
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

        event.bot.ban_chat_member.assert_not_awaited()
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
        unban_chat_member=AsyncMock(return_value=True),
        ban_chat_member=AsyncMock(return_value=True),
        restrict_chat_member=AsyncMock(return_value=True),
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

    async def test_preparing_prompt_cannot_be_used_before_activation(self) -> None:
        async with self.session_factory() as session:
            prepared = await prepare_join_verification(
                session,
                group_id=-100,
                user_id=953,
                deadline_at=now_shanghai_naive() + timedelta(minutes=5),
                prompt_message_id=853,
            )
            self.assertIsNotNone(prepared)
            await session.commit()

        callback = _verification_callback(
            action=VERIFICATION_CALLBACK_START,
            target_user_id=953,
            operator_id=953,
            message_id=853,
        )
        async with self.session_factory() as session:
            await membership.on_verification_callback(
                callback,
                session=session,
                settings=_settings(),
            )

        self.assertTrue(callback.answer.await_args.kwargs["show_alert"])
        self.assertIn("失效", callback.answer.await_args.args[0])
        callback.bot.me.assert_not_awaited()
        callback.bot.delete_message.assert_awaited_once_with(-100, 853)
        async with self.session_factory() as session:
            record = await get_join_verification(session, -100, 953)
            self.assertIsNotNone(record)
            self.assertEqual(record.status, "preparing")

    async def test_shared_challenge_rejects_preparing_record(self) -> None:
        async with self.session_factory() as session:
            prepared = await prepare_join_verification(
                session,
                group_id=-100,
                user_id=954,
                deadline_at=now_shanghai_naive() + timedelta(minutes=5),
                kind=VERIFICATION_KIND_PATROL,
            )
            self.assertIsNotNone(prepared)
            await session.commit()

        callback = _verification_callback(
            action=VERIFICATION_CALLBACK_START,
            target_user_id=954,
            operator_id=954,
            message_id=854,
        )
        async with self.session_factory() as session:
            await membership._handle_shared_challenge_callback(
                callback,
                session,
                kind=VERIFICATION_KIND_PATROL,
            )

        self.assertTrue(callback.answer.await_args.kwargs["show_alert"])
        self.assertIn("点名", callback.answer.await_args.args[0])
        callback.bot.me.assert_not_awaited()

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

    async def test_cancelled_admin_approve_leaves_releasing_recovery_lease(self) -> None:
        await self._add_record(user_id=956, message_id=856)
        callback = _verification_callback(
            action=VERIFICATION_CALLBACK_APPROVE,
            target_user_id=956,
            operator_id=11,
            message_id=856,
        )

        async def cancelled_restore(*_args, **_kwargs):
            async with self.session_factory() as check_session:
                row = await get_join_verification(check_session, -100, 956)
                self.assertEqual(row.status, VERIFICATION_STATUS_RELEASING)
                self.assertIsNotNone(row.lease_until)
            raise asyncio.CancelledError()

        with (
            patch(
                "bot.handlers.membership.is_group_admin_or_higher",
                new=AsyncMock(return_value=True),
            ),
            patch(
                "bot.handlers.membership.restore_member_permissions",
                new=AsyncMock(side_effect=cancelled_restore),
            ),
        ):
            async with self.session_factory() as session:
                with self.assertRaises(asyncio.CancelledError):
                    await membership.on_verification_callback(
                        callback,
                        session=session,
                        settings=_settings(),
                    )

        async with self.session_factory() as session:
            row = await get_join_verification(session, -100, 956)
            self.assertIsNotNone(row)
            self.assertEqual(row.status, VERIFICATION_STATUS_RELEASING)

    async def test_cancelled_admin_reject_leaves_enforcing_recovery_lease(self) -> None:
        await self._add_record(user_id=957, message_id=857)
        callback = _verification_callback(
            action=VERIFICATION_CALLBACK_REJECT,
            target_user_id=957,
            operator_id=11,
            message_id=857,
        )

        async def cancelled_ban(*_args, **_kwargs):
            async with self.session_factory() as check_session:
                row = await get_join_verification(check_session, -100, 957)
                self.assertEqual(row.status, "enforcing")
                self.assertIsNotNone(row.lease_until)
            raise asyncio.CancelledError()

        with (
            patch(
                "bot.handlers.membership.is_group_admin_or_higher",
                new=AsyncMock(return_value=True),
            ),
            patch(
                "bot.handlers.membership.ban_member",
                new=AsyncMock(side_effect=cancelled_ban),
            ),
        ):
            async with self.session_factory() as session:
                with self.assertRaises(asyncio.CancelledError):
                    await membership.on_verification_callback(
                        callback,
                        session=session,
                        settings=_settings(),
                    )

        async with self.session_factory() as session:
            row = await get_join_verification(session, -100, 957)
            self.assertIsNotNone(row)
            self.assertEqual(row.status, "enforcing")

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
                "bot.handlers.membership.schedule_message_auto_delete_durable",
                new=AsyncMock(return_value=True),
            ) as schedule_mock,
        ):
            async with self.session_factory() as session:
                await membership.on_verification_callback(
                    callback, session=session, settings=settings
                )

        callback.bot.edit_message_text.assert_awaited_once()
        schedule_mock.assert_awaited_once_with(edited, 15)

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

    async def test_admin_approve_restore_failure_keeps_releasing_lease(self) -> None:
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
            self.assertEqual(record.deadline_at, old_deadline)
            self.assertEqual(record.status, VERIFICATION_STATUS_RELEASING)
            self.assertIsNotNone(record.lease_until)
            self.assertEqual(record.prompt_message_id, 847)
        callback.bot.edit_message_text.assert_not_awaited()

    async def test_admin_reject_join_bans_in_group_without_global_ban(self) -> None:
        # An admin rejection is a ban decision: unlike the timeout path (kick =
        # temporary ban + unban), it must permanently ban in this group only.
        await self._add_record(user_id=948, message_id=848)
        callback = _verification_callback(
            action=VERIFICATION_CALLBACK_REJECT,
            target_user_id=948,
            operator_id=11,
            message_id=848,
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

        ban.assert_awaited_once_with(callback.bot, -100, 948)
        callback.bot.unban_chat_member.assert_not_awaited()
        # The rejected joiner never verified: the outcome notice hides the
        # display name behind a spoiler so a spam name gets no passive terminal
        # exposure, while the numeric ID keeps the account identifiable.
        rejected_text = callback.bot.edit_message_text.await_args.kwargs["text"]
        self.assertIn("<tg-spoiler>用户948</tg-spoiler>", rejected_text)
        self.assertIn("<code>948</code>", rejected_text)
        self.assertIn("已被管理员拒绝", rejected_text)
        async with self.session_factory() as session:
            self.assertIsNone(await get_join_verification(session, -100, 948))
            warning = await session.scalar(
                select(UserWarning).where(
                    UserWarning.group_id == -100,
                    UserWarning.user_id == 948,
                )
            )
            self.assertIsNotNone(warning)
            self.assertTrue(warning.is_banned)
            self.assertIsNone(await get_global_ban(session, 948))

    async def test_admin_reject_retracts_join_residue_despite_leave_race(self) -> None:
        # The rejected joiner's "xxx joined" service message and raced-in
        # residue stay visible after the permanent ban; the callback must
        # retract them even though the ban's own leave update clears the
        # live join marker first.
        from bot.services.recent_messages import (
            clear_member_join_marker,
            clear_recent_member_messages,
            mark_member_join,
            record_group_message,
        )

        clear_recent_member_messages()
        self.addCleanup(clear_recent_member_messages)
        mark_member_join(-100, 954)
        record_group_message(-100, 954, 621)

        await self._add_record(user_id=954, message_id=854)
        callback = _verification_callback(
            action=VERIFICATION_CALLBACK_REJECT,
            target_user_id=954,
            operator_id=11,
            message_id=854,
        )
        callback.bot.delete_messages = AsyncMock(return_value=True)

        async def ban_and_clear_marker(*args, **kwargs):
            clear_member_join_marker(-100, 954)
            return True

        with (
            patch(
                "bot.handlers.membership.is_group_admin_or_higher",
                new=AsyncMock(return_value=True),
            ),
            patch(
                "bot.handlers.membership.ban_member",
                new=AsyncMock(side_effect=ban_and_clear_marker),
            ),
        ):
            async with self.session_factory() as session:
                await membership.on_verification_callback(
                    callback,
                    session=session,
                    settings=_settings(),
                )

        callback.bot.delete_messages.assert_awaited_once_with(
            chat_id=-100,
            message_ids=[621],
        )
        async with self.session_factory() as session:
            self.assertIsNone(await get_join_verification(session, -100, 954))

    async def test_admin_reject_join_keeps_enforcing_when_ban_fails(self) -> None:
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
            record = await get_join_verification(session, -100, 949)
            self.assertIsNotNone(record)
            self.assertEqual(record.deadline_at, old_deadline)
            self.assertEqual(record.status, "enforcing")
            self.assertIsNotNone(record.lease_until)
            # The rejection's durable local ban survives the Telegram failure
            # so the sweeper's retry keeps enforcing the admin's decision.
            warning = await session.scalar(
                select(UserWarning).where(
                    UserWarning.group_id == -100,
                    UserWarning.user_id == 949,
                )
            )
            self.assertIsNotNone(warning)
            self.assertTrue(warning.is_banned)
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

    async def test_group_unban_wins_after_moderation_ban_heartbeat(self) -> None:
        await self._add_record(
            user_id=953,
            message_id=853,
            kind=VERIFICATION_KIND_MODERATION,
            reason="疑似广告",
        )
        callback = _verification_callback(
            action=VERIFICATION_CALLBACK_REJECT,
            target_user_id=953,
            operator_id=12,
            message_id=853,
        )

        async def ban_then_unban_policy(*_args, **_kwargs):
            async with self.session_factory() as concurrent:
                warning = await concurrent.scalar(
                    select(UserWarning).where(
                        UserWarning.group_id == -100,
                        UserWarning.user_id == 953,
                    )
                )
                if warning is not None:
                    await concurrent.delete(warning)
                await delete_join_verification(concurrent, -100, 953)
                await concurrent.commit()
            return True

        with (
            patch(
                "bot.handlers.membership.is_group_admin_or_higher",
                new=AsyncMock(return_value=True),
            ),
            patch(
                "bot.handlers.membership.ban_member",
                new=AsyncMock(side_effect=ban_then_unban_policy),
            ),
        ):
            async with self.session_factory() as session:
                await membership.on_verification_callback(
                    callback,
                    session=session,
                    settings=_settings(),
                )

        callback.bot.unban_chat_member.assert_awaited_once_with(
            -100,
            953,
            only_if_banned=True,
        )
        callback.bot.restrict_chat_member.assert_awaited_once()
        async with self.session_factory() as session:
            self.assertIsNone(await get_join_verification(session, -100, 953))
            self.assertIsNone(
                await session.scalar(
                    select(UserWarning).where(
                        UserWarning.group_id == -100,
                        UserWarning.user_id == 953,
                    )
                )
            )

    async def test_global_unban_wins_after_moderation_ban_heartbeat(self) -> None:
        await self._add_record(
            user_id=954,
            message_id=854,
            kind=VERIFICATION_KIND_MODERATION,
            reason="疑似广告",
        )
        async with self.session_factory() as session:
            await add_global_ban(session, 954, reason="测试", source="manual")
            await session.commit()
        callback = _verification_callback(
            action=VERIFICATION_CALLBACK_REJECT,
            target_user_id=954,
            operator_id=12,
            message_id=854,
        )

        async def ban_then_global_unban(*_args, **_kwargs):
            async with self.session_factory() as concurrent:
                await remove_global_ban(concurrent, 954, operator_id=12)
                warning = await concurrent.scalar(
                    select(UserWarning).where(
                        UserWarning.group_id == -100,
                        UserWarning.user_id == 954,
                    )
                )
                if warning is not None:
                    await concurrent.delete(warning)
                await delete_join_verification(concurrent, -100, 954)
                await concurrent.commit()
            return True

        with (
            patch(
                "bot.handlers.membership.is_group_admin_or_higher",
                new=AsyncMock(return_value=True),
            ),
            patch(
                "bot.handlers.membership.ban_member",
                new=AsyncMock(side_effect=ban_then_global_unban),
            ),
        ):
            async with self.session_factory() as session:
                await membership.on_verification_callback(
                    callback,
                    session=session,
                    settings=_settings(),
                )

        callback.bot.unban_chat_member.assert_awaited_once_with(
            -100,
            954,
            only_if_banned=True,
        )
        callback.bot.restrict_chat_member.assert_awaited_once()
        async with self.session_factory() as session:
            self.assertIsNone(await get_join_verification(session, -100, 954))
            self.assertIsNone(await get_global_ban(session, 954))

    async def test_admin_reject_moderation_keeps_enforcing_when_ban_fails(self) -> None:
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
            self.assertEqual(record.deadline_at, old_deadline)
            self.assertEqual(record.status, "enforcing")
            self.assertIsNotNone(record.lease_until)
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
            self.assertIn("<b>入群验证 · 待完成</b>", text)
            self.assertIn("<s>已加入群聊</s>", text)
            self.assertIn("<blockquote expandable>", text)
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
            self.assertIn("<b>消息审查验证 · 待完成</b>", text)
            self.assertIn("<s>已暂停发言</s>", text)
            self.assertIn("<blockquote expandable>", text)
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

    async def test_private_start_records_challenge_message_id(self) -> None:
        async with self.session_factory() as session:
            await upsert_join_verification(
                session,
                group_id=-100,
                user_id=930,
                deadline_at=now_shanghai_naive() + timedelta(minutes=5),
            )
            await session.commit()

            message = self._private_message(930)
            message.bot = SimpleNamespace(edit_message_text=AsyncMock())
            message.answer = AsyncMock(
                return_value=SimpleNamespace(message_id=4321)
            )
            handled = await maybe_send_private_verification(
                message, session, _settings()
            )

            self.assertTrue(handled)
            record = await get_join_verification(session, -100, 930)
            self.assertEqual(int(record.private_message_id), 4321)

    async def test_repeated_private_start_supersedes_previous_entry(self) -> None:
        async with self.session_factory() as session:
            await upsert_join_verification(
                session,
                group_id=-100,
                user_id=931,
                deadline_at=now_shanghai_naive() + timedelta(minutes=5),
            )
            await session.commit()

            edit = AsyncMock()
            message = self._private_message(931)
            message.bot = SimpleNamespace(edit_message_text=edit)
            message.answer = AsyncMock(
                return_value=SimpleNamespace(message_id=100)
            )
            self.assertTrue(
                await maybe_send_private_verification(message, session, _settings())
            )
            message.answer = AsyncMock(
                return_value=SimpleNamespace(message_id=101)
            )
            self.assertTrue(
                await maybe_send_private_verification(message, session, _settings())
            )

            record = await get_join_verification(session, -100, 931)
            self.assertEqual(int(record.private_message_id), 101)
        # The first entry (100) must have been rewritten to the superseded
        # notice so its WebApp button is gone.
        edit.assert_awaited_once()
        kwargs = edit.await_args.kwargs
        self.assertEqual(kwargs["chat_id"], 931)
        self.assertEqual(kwargs["message_id"], 100)
        self.assertIsNone(kwargs["reply_markup"])


class SweeperTests(_DbTestCase):
    async def test_unreachable_unban_is_single_attempt_and_persistently_isolated(self) -> None:
        async with self.session_factory() as session:
            recovery = await lease_join_verification_for_unban(
                session,
                -100,
                946,
                now=now_shanghai_naive() - timedelta(minutes=3),
            )
            self.assertIsNotNone(recovery)
            await session.commit()

        error = TelegramBadRequest(
            method=SimpleNamespace(),
            message="CHAT_NOT_FOUND",
        )
        bot = SimpleNamespace(
            unban_chat_member=AsyncMock(side_effect=error),
            ban_chat_member=AsyncMock(),
            restrict_chat_member=AsyncMock(),
        )
        sweeper = JoinVerificationSweeper(
            bot=bot,
            session_factory=self.session_factory,
        )

        self.assertEqual(await sweeper.sweep_once(), 1)
        self.assertEqual(bot.unban_chat_member.await_count, 1)
        async with self.session_factory() as session:
            record = await get_join_verification(session, -100, 946)
            authorization = await session.get(AuthorizedGroup, -100)
            self.assertIsNotNone(record)
            self.assertEqual(record.status, VERIFICATION_STATUS_UNBANNING)
            self.assertGreater(
                record.lease_until,
                now_shanghai_naive() + timedelta(hours=11),
            )
            self.assertFalse(authorization.bot_present)

    async def test_resume_only_shortens_long_isolation_lease_with_safety_barrier(self) -> None:
        now = now_shanghai_naive()
        leases = {
            947: now + timedelta(seconds=90),
            948: now + timedelta(minutes=10),
            949: now + timedelta(hours=12),
        }
        async with self.session_factory() as session:
            for user_id, lease_until in leases.items():
                session.add(
                    JoinVerification(
                        group_id=-100,
                        user_id=user_id,
                        kind=VERIFICATION_KIND_MODERATION,
                        provider="turnstile",
                        status=VERIFICATION_STATUS_UNBANNING,
                        lease_until=lease_until,
                        deadline_at=now,
                    )
                )
            await session.commit()
            self.assertEqual(
                await resume_group_verification_recovery(
                    session,
                    -100,
                    now=now,
                ),
                1,
            )
            await session.commit()

            rows = {
                row.user_id: row
                for row in (
                    await session.execute(
                        select(JoinVerification).where(
                            JoinVerification.group_id == -100
                        )
                    )
                ).scalars()
            }
            self.assertEqual(rows[947].lease_until, leases[947])
            self.assertEqual(rows[948].lease_until, leases[948])
            self.assertEqual(
                rows[949].lease_until,
                now + timedelta(seconds=105),
            )

    async def test_crashed_unban_journal_retries_unban_then_deletes(self) -> None:
        async with self.session_factory() as session:
            recovery = await lease_join_verification_for_unban(
                session,
                -100,
                944,
                now=now_shanghai_naive() - timedelta(minutes=3),
            )
            self.assertIsNotNone(recovery)
            await session.commit()

        bot = SimpleNamespace(
            restrict_chat_member=AsyncMock(return_value=True),
            delete_message=AsyncMock(return_value=True),
            ban_chat_member=AsyncMock(return_value=True),
            unban_chat_member=AsyncMock(return_value=True),
        )
        sweeper = JoinVerificationSweeper(
            bot=bot,
            session_factory=self.session_factory,
            settings=_settings(),
        )

        self.assertEqual(await sweeper.sweep_once(), 1)
        bot.unban_chat_member.assert_awaited_once_with(
            -100,
            944,
            only_if_banned=True,
        )
        bot.restrict_chat_member.assert_awaited_once()
        async with self.session_factory() as session:
            self.assertIsNone(await get_join_verification(session, -100, 944))

    async def test_expired_releasing_lease_restores_instead_of_punishing(self) -> None:
        now = now_shanghai_naive()
        async with self.session_factory() as session:
            await upsert_join_verification(
                session,
                group_id=-100,
                user_id=943,
                deadline_at=now + timedelta(minutes=5),
                kind=VERIFICATION_KIND_MODERATION,
                prompt_message_id=788,
            )
            await session.commit()
            record = await get_join_verification(session, -100, 943)
            self.assertTrue(
                await claim_join_verification(
                    session,
                    verification_id=record.id,
                    deadline_at=record.deadline_at,
                    kind=record.kind,
                    now=now,
                    expired=False,
                    lease_until=now - timedelta(seconds=1),
                    target_status=VERIFICATION_STATUS_RELEASING,
                )
            )
            await session.commit()

        bot = SimpleNamespace(
            restrict_chat_member=AsyncMock(return_value=True),
            delete_message=AsyncMock(return_value=True),
            ban_chat_member=AsyncMock(),
            unban_chat_member=AsyncMock(),
        )
        sweeper = JoinVerificationSweeper(
            bot=bot,
            session_factory=self.session_factory,
            settings=_settings(join_verification_turnstile_secret_key=""),
        )

        self.assertEqual(await sweeper.sweep_once(), 1)
        bot.restrict_chat_member.assert_awaited_once()
        bot.ban_chat_member.assert_not_awaited()
        async with self.session_factory() as session:
            self.assertIsNone(await get_join_verification(session, -100, 943))

    async def test_unauthorized_group_expiry_releases_member_without_enforcement(self) -> None:
        from bot.services.authz import deauthorize_group

        async with self.session_factory() as session:
            await upsert_join_verification(
                session,
                group_id=-100,
                user_id=942,
                deadline_at=now_shanghai_naive()
                - CHALLENGE_SUBMIT_GRACE
                - timedelta(seconds=1),
                kind=VERIFICATION_KIND_MODERATION,
                prompt_message_id=787,
            )
            await deauthorize_group(session, -100)
            await session.commit()

        bot = SimpleNamespace(
            restrict_chat_member=AsyncMock(return_value=True),
            delete_message=AsyncMock(return_value=True),
            ban_chat_member=AsyncMock(),
            unban_chat_member=AsyncMock(),
        )
        sweeper = JoinVerificationSweeper(
            bot=bot,
            session_factory=self.session_factory,
            settings=_settings(join_verification_turnstile_secret_key=""),
        )

        self.assertEqual(await sweeper.sweep_once(), 1)
        bot.restrict_chat_member.assert_awaited_once()
        bot.ban_chat_member.assert_not_awaited()
        async with self.session_factory() as session:
            self.assertIsNone(await get_join_verification(session, -100, 942))

    async def test_deauthorized_crashed_moderation_ban_is_unbanned_when_policy_clear(self) -> None:
        from bot.services.authz import deauthorize_group

        now = now_shanghai_naive()
        async with self.session_factory() as session:
            await upsert_join_verification(
                session,
                group_id=-100,
                user_id=941,
                deadline_at=now - CHALLENGE_SUBMIT_GRACE - timedelta(seconds=1),
                kind=VERIFICATION_KIND_MODERATION,
            )
            await session.commit()
            record = await get_join_verification(session, -100, 941)
            self.assertTrue(
                await claim_join_verification(
                    session,
                    verification_id=record.id,
                    deadline_at=record.deadline_at,
                    kind=record.kind,
                    now=now,
                    expired=True,
                    lease_until=now - timedelta(seconds=1),
                )
            )
            await deauthorize_group(session, -100)
            await session.commit()

        bot = SimpleNamespace(
            restrict_chat_member=AsyncMock(return_value=True),
            get_chat_member=AsyncMock(
                return_value=SimpleNamespace(status="member", can_send_messages=True)
            ),
            delete_message=AsyncMock(return_value=True),
            ban_chat_member=AsyncMock(),
            unban_chat_member=AsyncMock(return_value=True),
        )
        sweeper = JoinVerificationSweeper(
            bot=bot,
            session_factory=self.session_factory,
        )

        self.assertEqual(await sweeper.sweep_once(), 1)
        bot.unban_chat_member.assert_awaited_once_with(
            -100,
            941,
            only_if_banned=True,
        )
        bot.restrict_chat_member.assert_awaited_once()
        bot.ban_chat_member.assert_not_awaited()
        async with self.session_factory() as session:
            self.assertIsNone(await get_join_verification(session, -100, 941))

    async def test_expired_preparing_restores_permissions_and_deletes_record(self) -> None:
        expired_lease = now_shanghai_naive() - timedelta(seconds=1)
        async with self.session_factory() as session:
            prepared = await prepare_join_verification(
                session,
                group_id=-100,
                user_id=944,
                deadline_at=now_shanghai_naive() + timedelta(minutes=5),
                prompt_message_id=789,
                lease_until=expired_lease,
            )
            self.assertIsNotNone(prepared)
            await session.commit()
            expired = await list_expired_preparing_verifications(
                session,
                now=now_shanghai_naive(),
            )
            self.assertEqual([record.user_id for record in expired], [944])

        bot = SimpleNamespace(
            restrict_chat_member=AsyncMock(return_value=True),
            delete_message=AsyncMock(return_value=True),
            ban_chat_member=AsyncMock(),
            unban_chat_member=AsyncMock(),
        )
        sweeper = JoinVerificationSweeper(
            bot=bot,
            session_factory=self.session_factory,
        )

        self.assertEqual(await sweeper.sweep_once(), 1)

        bot.restrict_chat_member.assert_awaited_once()
        permissions = bot.restrict_chat_member.await_args.kwargs["permissions"]
        self.assertTrue(permissions.can_send_messages)
        bot.ban_chat_member.assert_not_awaited()
        async with self.session_factory() as session:
            self.assertIsNone(await get_join_verification(session, -100, 944))

    async def test_cancelled_enforcement_keeps_leased_record_for_restart_recovery(self) -> None:
        async with self.session_factory() as session:
            await upsert_join_verification(
                session,
                group_id=-100,
                user_id=945,
                deadline_at=now_shanghai_naive()
                - CHALLENGE_SUBMIT_GRACE
                - timedelta(seconds=1),
                prompt_message_id=790,
            )
            await session.commit()

        bot = SimpleNamespace(
            ban_chat_member=AsyncMock(return_value=True),
            unban_chat_member=AsyncMock(return_value=True),
            edit_message_text=AsyncMock(return_value=SimpleNamespace(message_id=790)),
        )
        sweeper = JoinVerificationSweeper(bot=bot, session_factory=self.session_factory)
        with patch(
            "bot.services.join_verification.kick_member",
            new=AsyncMock(side_effect=asyncio.CancelledError()),
        ):
            with self.assertRaises(asyncio.CancelledError):
                await sweeper.sweep_once()

        async with self.session_factory() as session:
            record = await get_join_verification(session, -100, 945)
            self.assertIsNotNone(record)
            self.assertEqual(record.status, "enforcing")
            self.assertIsNotNone(record.lease_until)
            record.lease_until = now_shanghai_naive() - timedelta(seconds=1)
            await session.commit()

        with patch(
            "bot.services.join_verification.kick_member",
            new=AsyncMock(return_value=True),
        ):
            self.assertEqual(await sweeper.sweep_once(), 1)

        async with self.session_factory() as session:
            self.assertIsNone(await get_join_verification(session, -100, 945))

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
        bot.unban_chat_member.assert_awaited_once_with(
            -100,
            930,
            only_if_banned=True,
        )
        self.assertCountEqual(
            bot.ban_chat_member.await_args_list,
            [
                call(-100, 930, revoke_messages=True),
                call(-100, 932, revoke_messages=True),
            ],
        )
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
            "bot.services.join_verification.schedule_message_auto_delete_durable",
            new=AsyncMock(return_value=True),
        ) as schedule_mock:
            self.assertEqual(await sweeper.sweep_once(), 1)

        bot.edit_message_text.assert_awaited_once()
        edit_kwargs = bot.edit_message_text.await_args.kwargs
        self.assertEqual(edit_kwargs["message_id"], 781)
        self.assertEqual(edit_kwargs["parse_mode"], "HTML")
        self.assertIsNone(edit_kwargs["reply_markup"])
        # The joiner never verified, so the timeout notice hides the name
        # behind a spoiler like the challenge prompt while the ID stays
        # identifiable; the outcome auto-deletes.
        self.assertIn("<b><tg-spoiler>张三</tg-spoiler></b>", edit_kwargs["text"])
        self.assertIn("<code>940</code>", edit_kwargs["text"])
        self.assertIn("<b>入群验证 · 已超时</b>", edit_kwargs["text"])
        schedule_mock.assert_awaited_once_with(edited, 30)

    async def test_failed_moderation_timeout_ban_keeps_enforcing_intent(self) -> None:
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
            self.assertEqual(record.status, "enforcing")
            self.assertIsNotNone(record.lease_until)
            self.assertIsNone(warning)
        bot.edit_message_text.assert_not_awaited()

    async def test_failed_join_timeout_kick_keeps_enforcing_intent(self) -> None:
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
            self.assertEqual(record.status, "enforcing")
            self.assertIsNotNone(record.lease_until)

    async def test_join_timeout_kick_retracts_join_residue_despite_leave_race(self) -> None:
        # The join service message ("xxx joined") and raced-in residue stay
        # visible to other members after the timeout kick; the sweep must
        # retract them even though the kick's own leave update clears the
        # live join marker before the sweep runs.
        from bot.services.recent_messages import (
            clear_member_join_marker,
            clear_recent_member_messages,
            consume_member_removal,
            mark_member_join,
            record_group_message,
        )

        clear_recent_member_messages()
        self.addCleanup(clear_recent_member_messages)
        mark_member_join(-100, 952)
        record_group_message(-100, 952, 601)
        record_group_message(-100, 952, 602)

        now = now_shanghai_naive()
        async with self.session_factory() as session:
            await upsert_join_verification(
                session,
                group_id=-100,
                user_id=952,
                deadline_at=now - CHALLENGE_SUBMIT_GRACE - timedelta(seconds=2),
                prompt_message_id=791,
            )
            await session.commit()

        async def ban_and_clear_marker(*args, **kwargs):
            # Telegram delivers the leave update caused by this very ban; its
            # handler clears the live marker before the post-kick sweep.
            clear_member_join_marker(-100, 952)
            return True

        bot = SimpleNamespace(
            ban_chat_member=AsyncMock(side_effect=ban_and_clear_marker),
            unban_chat_member=AsyncMock(return_value=True),
            edit_message_text=AsyncMock(),
            delete_messages=AsyncMock(return_value=True),
        )
        sweeper = JoinVerificationSweeper(bot=bot, session_factory=self.session_factory)

        self.assertEqual(await sweeper.sweep_once(), 1)
        bot.delete_messages.assert_awaited_once_with(
            chat_id=-100,
            message_ids=[601, 602],
        )
        # The pending "X was removed" service message is armed for the gate.
        self.assertTrue(consume_member_removal(-100, 952))
        async with self.session_factory() as session:
            self.assertIsNone(await get_join_verification(session, -100, 952))

    async def test_moderation_timeout_ban_retracts_join_residue(self) -> None:
        from bot.services.recent_messages import (
            clear_recent_member_messages,
            mark_member_join,
            record_group_message,
        )

        clear_recent_member_messages()
        self.addCleanup(clear_recent_member_messages)
        mark_member_join(-100, 953)
        record_group_message(-100, 953, 611)

        now = now_shanghai_naive()
        async with self.session_factory() as session:
            await upsert_join_verification(
                session,
                group_id=-100,
                user_id=953,
                deadline_at=now - CHALLENGE_SUBMIT_GRACE - timedelta(seconds=2),
                kind=VERIFICATION_KIND_MODERATION,
                reason="疑似违规",
                prompt_message_id=792,
            )
            await session.commit()

        bot = SimpleNamespace(
            ban_chat_member=AsyncMock(return_value=True),
            unban_chat_member=AsyncMock(return_value=True),
            edit_message_text=AsyncMock(),
            delete_messages=AsyncMock(return_value=True),
        )
        sweeper = JoinVerificationSweeper(bot=bot, session_factory=self.session_factory)

        self.assertEqual(await sweeper.sweep_once(), 1)
        bot.delete_messages.assert_awaited_once_with(
            chat_id=-100,
            message_ids=[611],
        )

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
        bot.unban_chat_member.assert_awaited_once_with(
            -100,
            934,
            only_if_banned=True,
        )
        bot.ban_chat_member.assert_awaited_once_with(
            -100,
            934,
            revoke_messages=True,
        )
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
    async def test_leave_retains_preparing_join_for_setup_or_sweeper_recovery(self) -> None:
        async with self.session_factory() as session:
            prepared = await prepare_join_verification(
                session,
                group_id=-100,
                user_id=939,
                deadline_at=now_shanghai_naive() + timedelta(minutes=5),
            )
            self.assertIsNotNone(prepared)
            await session.commit()

            event = SimpleNamespace(
                chat=SimpleNamespace(id=-100, type="supergroup"),
                new_chat_member=SimpleNamespace(
                    user=SimpleNamespace(id=939, is_bot=False)
                ),
                bot=SimpleNamespace(delete_message=AsyncMock(return_value=True)),
            )
            await membership.on_member_leave(
                event,
                session=session,
                settings=_settings(),
            )
            await session.commit()

            row = await get_join_verification(session, -100, 939)
            self.assertIsNotNone(row)
            self.assertEqual(row.status, "preparing")
            event.bot.delete_message.assert_not_awaited()

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

    async def test_duplicate_join_does_not_consume_or_enforce_preparing_record(self) -> None:
        async with self.session_factory() as session:
            prepared = await prepare_join_verification(
                session,
                group_id=-100,
                user_id=955,
                deadline_at=now_shanghai_naive() + timedelta(minutes=5),
                kind=VERIFICATION_KIND_MODERATION,
                reason="设置进行中",
            )
            self.assertIsNotNone(prepared)
            await session.commit()

            event = _join_event(user_id=955)
            with patch("bot.handlers.membership.get_raid_guard_service", return_value=None):
                await membership.on_member_join(
                    event,
                    session=session,
                    settings=_settings(),
                )

            event.bot.restrict_chat_member.assert_not_awaited()
            event.bot.get_chat.assert_not_awaited()
            row = await get_join_verification(session, -100, 955)
            self.assertIsNotNone(row)
            self.assertEqual(row.status, "preparing")
            self.assertEqual(row.kind, VERIFICATION_KIND_MODERATION)


if __name__ == "__main__":
    unittest.main()
