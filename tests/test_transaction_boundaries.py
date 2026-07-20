from __future__ import annotations

import unittest
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from bot.handlers import admin, group, membership
from bot.services import join_verification, vote_ban
from bot.utils.timezone import now_shanghai_naive


class VoteBanTransactionBoundaryTests(unittest.IsolatedAsyncioTestCase):
    async def test_stale_recovery_releases_read_transaction_before_ban(self) -> None:
        events: list[str] = []
        fresh = SimpleNamespace(
            id=7,
            group_id=-100,
            target_user_id=42,
            status="enforcing",
            enforcing_started_at=now_shanghai_naive() - timedelta(minutes=5),
        )
        session = SimpleNamespace(
            execute=AsyncMock(return_value=SimpleNamespace(rowcount=1)),
            get=AsyncMock(return_value=fresh),
            rollback=AsyncMock(),
            commit=AsyncMock(side_effect=lambda: events.append("commit")),
        )

        async def apply_after_release(*_args, **_kwargs) -> bool:
            events.append("ban")
            self.assertEqual(events[:3], ["commit", "commit", "ban"])
            return False

        async def persist_outcome(_session, persisted_record, **_kwargs) -> bool:
            persisted_record.status = "failed"
            return True

        with (
            patch.object(vote_ban, "count_approvals", new=AsyncMock(return_value=2)),
            patch.object(vote_ban, "apply_vote_ban", new=AsyncMock(side_effect=apply_after_release)),
            patch.object(
                vote_ban,
                "record_vote_ban_outcome",
                new=AsyncMock(side_effect=persist_outcome),
            ),
            patch.object(vote_ban, "finalize_vote_message", new=AsyncMock()),
            patch.object(vote_ban, "cancel_vote_expiry"),
            patch.object(
                vote_ban,
                "lease_join_verification_for_unban",
                new=AsyncMock(
                    return_value=SimpleNamespace(
                        verification_id=99,
                        lease_until=now_shanghai_naive() + timedelta(minutes=5),
                    )
                ),
            ),
            patch.object(
                vote_ban,
                "join_verification_lease_is_current",
                new=AsyncMock(return_value=True),
            ),
        ):
            status = await vote_ban.recover_stale_vote_enforcement(
                bot=SimpleNamespace(),
                session=session,
                settings=SimpleNamespace(),
                record=fresh,
            )

        self.assertEqual(status, "failed")


class PrivateVerificationTransactionBoundaryTests(unittest.IsolatedAsyncioTestCase):
    async def test_answer_happens_after_read_transaction_is_released(self) -> None:
        events: list[str] = []
        current = now_shanghai_naive()
        record = SimpleNamespace(
            id=11,
            group_id=-100,
            user_id=88,
            kind=join_verification.VERIFICATION_KIND_JOIN,
            provider="turnstile",
            reason="",
            created_at=current,
            deadline_at=current + timedelta(hours=1),
        )
        session = SimpleNamespace(
            commit=AsyncMock(side_effect=lambda: events.append("commit")),
            get=AsyncMock(),
            execute=AsyncMock(),
        )

        async def answer_after_release(*_args, **_kwargs) -> None:
            events.append("answer")
            self.assertEqual(events, ["commit", "answer"])

        message = SimpleNamespace(
            from_user=SimpleNamespace(id=88),
            answer=AsyncMock(side_effect=answer_after_release),
        )
        with (
            patch.object(
                join_verification,
                "get_pending_verification_for_user",
                new=AsyncMock(return_value=record),
            ),
            patch.object(join_verification, "verification_service_ready", return_value=True),
            patch.object(
                join_verification,
                "verification_timeout_seconds_for_kind",
                return_value=120,
            ),
            patch.object(join_verification, "build_private_challenge_text", return_value="challenge"),
            patch.object(join_verification, "build_private_challenge_keyboard", return_value=None),
        ):
            handled = await join_verification.maybe_send_private_verification(
                message,
                session,
                SimpleNamespace(),
            )

        self.assertTrue(handled)
        session.execute.assert_not_awaited()

    async def test_duplicate_moderation_challenge_releases_read_before_restrict(self) -> None:
        events: list[str] = []
        session = SimpleNamespace(
            commit=AsyncMock(side_effect=lambda: events.append("commit")),
        )
        current = SimpleNamespace(
            id=7,
            group_id=-100,
            user_id=88,
            kind=join_verification.VERIFICATION_KIND_MODERATION,
            status=join_verification.VERIFICATION_STATUS_PENDING,
            deadline_at=object(),
            lease_until=None,
        )

        async def restrict_after_release(*_args, **_kwargs) -> bool:
            events.append("restrict")
            self.assertEqual(
                events,
                ["commit", "commit", "check", "commit", "restrict"],
            )
            return True

        async def generation_check(*_args, **_kwargs) -> bool:
            events.append("check")
            return True

        with (
            patch.object(
                join_verification,
                "moderation_challenge_ready",
                return_value=True,
            ),
            patch.object(
                join_verification,
                "get_join_verification",
                new=AsyncMock(return_value=current),
            ),
            patch.object(
                join_verification,
                "restrict_new_member",
                new=AsyncMock(side_effect=restrict_after_release),
            ),
            patch.object(
                join_verification,
                "_join_verification_generation_is_current",
                new=AsyncMock(side_effect=generation_check),
            ),
        ):
            started = await join_verification._begin_moderation_challenge_locked(
                bot=SimpleNamespace(),
                session=session,
                settings=SimpleNamespace(),
                group_id=-100,
                user_id=88,
                display_name="member",
                bot_username="bot",
                reason="test",
            )

        self.assertTrue(started)
        self.assertEqual(
            events,
            [
                "commit",
                "commit",
                "check",
                "commit",
                "restrict",
                "check",
                "commit",
            ],
        )

    async def test_duplicate_moderation_mute_losing_generation_reconciles(self) -> None:
        current = SimpleNamespace(
            id=8,
            group_id=-100,
            user_id=88,
            kind=join_verification.VERIFICATION_KIND_MODERATION,
            status=join_verification.VERIFICATION_STATUS_PENDING,
            deadline_at=now_shanghai_naive() + timedelta(minutes=2),
            lease_until=None,
        )
        session = SimpleNamespace(commit=AsyncMock(), rollback=AsyncMock())
        reconcile = AsyncMock(return_value=True)

        with (
            patch.object(join_verification, "moderation_challenge_ready", return_value=True),
            patch.object(
                join_verification,
                "get_join_verification",
                new=AsyncMock(return_value=current),
            ),
            patch.object(
                join_verification,
                "_join_verification_generation_is_current",
                new=AsyncMock(side_effect=[True, False]),
            ) as generation_check,
            patch.object(
                join_verification,
                "restrict_new_member",
                new=AsyncMock(return_value=True),
            ) as restrict,
            patch.object(
                join_verification,
                "_reconcile_moderation_challenge_restriction",
                new=reconcile,
            ),
        ):
            handled = await join_verification._begin_moderation_challenge_locked(
                bot=SimpleNamespace(),
                session=session,
                settings=SimpleNamespace(),
                group_id=-100,
                user_id=88,
                display_name="member",
                bot_username="bot",
                reason="test",
            )

        self.assertTrue(handled)
        self.assertEqual(generation_check.await_count, 2)
        restrict.assert_awaited_once()
        reconcile.assert_awaited_once()

    async def test_moderation_mute_losing_generation_reconciles_before_prompt(self) -> None:
        now = now_shanghai_naive()
        prepared = join_verification.PreparedVerification(
            verification_id=17,
            group_id=-100,
            user_id=89,
            kind=join_verification.VERIFICATION_KIND_MODERATION,
            lease_until=now + timedelta(minutes=1),
            prompt_message_id=0,
        )
        session = SimpleNamespace(commit=AsyncMock(), rollback=AsyncMock())
        bot = SimpleNamespace(send_message=AsyncMock())
        reconcile = AsyncMock(return_value=True)

        with (
            patch.object(join_verification, "moderation_challenge_ready", return_value=True),
            patch.object(
                join_verification,
                "get_join_verification",
                new=AsyncMock(return_value=None),
            ),
            patch.object(
                join_verification,
                "prepare_join_verification",
                new=AsyncMock(return_value=prepared),
            ),
            patch.object(
                join_verification,
                "_join_verification_generation_is_current",
                new=AsyncMock(side_effect=[True, False]),
            ),
            patch.object(
                join_verification,
                "restrict_new_member",
                new=AsyncMock(return_value=True),
            ) as restrict,
            patch.object(
                join_verification,
                "abort_prepared_join_verification",
                new=AsyncMock(return_value=False),
            ),
            patch.object(
                join_verification,
                "_reconcile_moderation_challenge_restriction",
                new=reconcile,
            ),
            patch.object(join_verification, "verification_provider", return_value="turnstile"),
        ):
            handled = await join_verification._begin_moderation_challenge_locked(
                bot=bot,
                session=session,
                settings=SimpleNamespace(
                    moderation=SimpleNamespace(challenge_timeout_seconds=120)
                ),
                group_id=-100,
                user_id=89,
                display_name="member",
                bot_username="bot",
                reason="test",
            )

        self.assertTrue(handled)
        restrict.assert_awaited_once_with(bot, -100, 89)
        reconcile.assert_awaited_once()
        bot.send_message.assert_not_awaited()

    async def test_moderation_activation_lost_after_prompt_reconciles_and_cleans_prompt(self) -> None:
        now = now_shanghai_naive()
        prepared = join_verification.PreparedVerification(
            verification_id=18,
            group_id=-100,
            user_id=90,
            kind=join_verification.VERIFICATION_KIND_MODERATION,
            lease_until=now + timedelta(minutes=1),
            prompt_message_id=0,
        )
        session = SimpleNamespace(commit=AsyncMock(), rollback=AsyncMock())
        bot = SimpleNamespace(
            send_message=AsyncMock(return_value=SimpleNamespace(message_id=901))
        )
        reconcile = AsyncMock(return_value=True)
        cleanup_prompt = AsyncMock(return_value=True)

        with (
            patch.object(join_verification, "moderation_challenge_ready", return_value=True),
            patch.object(
                join_verification,
                "get_join_verification",
                new=AsyncMock(return_value=None),
            ),
            patch.object(
                join_verification,
                "prepare_join_verification",
                new=AsyncMock(return_value=prepared),
            ),
            patch.object(
                join_verification,
                "_join_verification_generation_is_current",
                new=AsyncMock(side_effect=[True, True]),
            ),
            patch.object(
                join_verification,
                "restrict_new_member",
                new=AsyncMock(return_value=True),
            ),
            patch.object(
                join_verification,
                "commit_prepared_join_verification",
                new=AsyncMock(return_value=False),
            ),
            patch.object(
                join_verification,
                "abort_prepared_join_verification",
                new=AsyncMock(return_value=False),
            ) as abort,
            patch.object(
                join_verification,
                "_reconcile_moderation_challenge_restriction",
                new=reconcile,
            ),
            patch.object(
                join_verification,
                "delete_verification_prompt",
                new=cleanup_prompt,
            ),
            patch.object(join_verification, "verification_provider", return_value="turnstile"),
        ):
            handled = await join_verification._begin_moderation_challenge_locked(
                bot=bot,
                session=session,
                settings=SimpleNamespace(
                    moderation=SimpleNamespace(challenge_timeout_seconds=120)
                ),
                group_id=-100,
                user_id=90,
                display_name="member",
                bot_username="bot",
                reason="test",
            )

        self.assertTrue(handled)
        abort.assert_awaited_once()
        cleanup_prompt.assert_awaited_once_with(bot, -100, 901)
        reconcile.assert_awaited_once()


class ModerationNoticeTransactionBoundaryTests(unittest.IsolatedAsyncioTestCase):
    async def test_notice_send_releases_refresh_transaction_first(self) -> None:
        events: list[str] = []
        violation = SimpleNamespace(id=7, notice_sent_at=None)
        session = SimpleNamespace(
            refresh=AsyncMock(side_effect=lambda *_args, **_kwargs: events.append("refresh")),
            commit=AsyncMock(side_effect=lambda: events.append("commit")),
        )

        async def answer_after_release(*_args, **_kwargs) -> None:
            events.append("answer")
            self.assertEqual(events, ["refresh", "commit", "answer"])

        with patch.object(
            group,
            "answer_with_auto_delete",
            new=AsyncMock(side_effect=answer_after_release),
        ):
            sent = await group._send_moderation_notice_once_locked(
                session=session,
                violation=violation,
                message=SimpleNamespace(),
                notice="blocked",
                auto_delete_seconds=0,
            )

        self.assertTrue(sent)
        self.assertEqual(events, ["refresh", "commit", "answer", "commit"])


class AdminTransactionBoundaryTests(unittest.IsolatedAsyncioTestCase):
    async def test_rule_manager_denial_commits_before_callback_answer(self) -> None:
        events: list[str] = []
        session = SimpleNamespace(
            commit=AsyncMock(side_effect=lambda: events.append("commit")),
        )

        async def answer_after_release(*_args, **_kwargs) -> None:
            events.append("answer")
            self.assertEqual(events, ["commit", "answer"])

        callback = SimpleNamespace(
            from_user=SimpleNamespace(id=9),
            message=SimpleNamespace(
                chat=SimpleNamespace(id=-100, type="supergroup"),
            ),
            answer=AsyncMock(side_effect=answer_after_release),
        )
        with (
            patch.object(
                admin,
                "ensure_group_authorized",
                new=AsyncMock(return_value=True),
            ),
            patch.object(
                admin,
                "is_group_admin_authorized",
                new=AsyncMock(return_value=False),
            ),
        ):
            allowed = await admin._callback_user_can_manage_rules(
                callback,
                session,
                SimpleNamespace(super_admin_id=1),
            )

        self.assertFalse(allowed)

    async def test_rule_paging_commits_before_editing_telegram_message(self) -> None:
        events: list[str] = []
        session = SimpleNamespace(
            execute=AsyncMock(
                return_value=SimpleNamespace(
                    scalars=lambda: SimpleNamespace(all=lambda: [])
                )
            ),
            commit=AsyncMock(side_effect=lambda: events.append("commit")),
        )

        async def edit_after_release(*_args, **_kwargs) -> None:
            events.append("edit")
            self.assertEqual(events[:2], ["commit", "edit"])

        callback = SimpleNamespace(
            data="rul:0",
            from_user=SimpleNamespace(id=1),
            message=SimpleNamespace(
                chat=SimpleNamespace(id=-100, type="supergroup"),
                edit_text=AsyncMock(side_effect=edit_after_release),
            ),
            answer=AsyncMock(),
        )
        with patch.object(
            admin,
            "_callback_user_can_manage_rules",
            new=AsyncMock(return_value=True),
        ):
            await admin.on_rule_list_paging(
                callback,
                settings=SimpleNamespace(),
                session=session,
            )

        callback.message.edit_text.assert_awaited_once()

    async def test_warnings_command_commits_before_answer(self) -> None:
        events: list[str] = []
        session = SimpleNamespace(
            execute=AsyncMock(
                return_value=SimpleNamespace(
                    scalars=lambda: SimpleNamespace(all=lambda: [])
                )
            ),
            commit=AsyncMock(side_effect=lambda: events.append("commit")),
        )
        message = SimpleNamespace(
            chat=SimpleNamespace(id=-100, type="supergroup"),
        )

        async def answer_after_release(*_args, **_kwargs) -> None:
            events.append("answer")
            self.assertEqual(events, ["commit", "answer"])

        with (
            patch.object(
                admin,
                "ensure_group_authorized",
                new=AsyncMock(return_value=True),
            ),
            patch.object(
                admin,
                "ensure_group_admin_permission",
                new=AsyncMock(return_value=True),
            ),
            patch.object(admin, "_answer", new=AsyncMock(side_effect=answer_after_release)),
        ):
            await admin.cmd_warnings(
                message,
                session,
                SimpleNamespace(moderation=SimpleNamespace(warn_threshold=3)),
            )

    async def test_admin_paging_checks_missing_session_before_authorization(self) -> None:
        callback = SimpleNamespace(
            data="adl:-100:0",
            message=SimpleNamespace(chat=SimpleNamespace(id=-100, type="supergroup")),
            answer=AsyncMock(),
        )
        with patch.object(admin, "ensure_group_authorized", new=AsyncMock()) as authorized:
            await admin.on_adminlist_paging(
                callback,
                settings=SimpleNamespace(),
                session=None,
            )

        authorized.assert_not_awaited()
        self.assertIn("会话未就绪", callback.answer.await_args.args[0])


class MembershipTransactionBoundaryTests(unittest.IsolatedAsyncioTestCase):
    async def test_stale_verification_prompt_commits_before_telegram_cleanup(self) -> None:
        events: list[str] = []
        session = SimpleNamespace(
            commit=AsyncMock(side_effect=lambda: events.append("commit")),
        )

        async def delete_after_release(*_args, **_kwargs) -> bool:
            events.append("delete")
            self.assertEqual(events[:2], ["commit", "delete"])
            return True

        callback = SimpleNamespace(
            bot=SimpleNamespace(),
            message=SimpleNamespace(
                chat=SimpleNamespace(id=-100, type="supergroup"),
                message_id=55,
            ),
            answer=AsyncMock(),
        )
        with (
            patch.object(
                membership,
                "get_join_verification",
                new=AsyncMock(return_value=None),
            ),
            patch.object(
                membership,
                "delete_verification_prompt",
                new=AsyncMock(side_effect=delete_after_release),
            ),
        ):
            result = await membership._verification_callback_record(
                callback,
                session,
                target_user_id=88,
            )

        self.assertIsNone(result)

    async def test_shared_challenge_releases_read_before_bot_lookup(self) -> None:
        events: list[str] = []
        current = now_shanghai_naive()
        record = SimpleNamespace(
            group_id=-100,
            kind=join_verification.VERIFICATION_KIND_PATROL,
            status=join_verification.VERIFICATION_STATUS_PENDING,
            deadline_at=current + timedelta(minutes=5),
        )
        session = SimpleNamespace(
            commit=AsyncMock(side_effect=lambda: events.append("commit")),
        )
        callback = SimpleNamespace(
            from_user=SimpleNamespace(id=88),
            message=SimpleNamespace(
                chat=SimpleNamespace(id=-100, type="supergroup"),
            ),
            answer=AsyncMock(),
        )

        async def username_after_release(_callback) -> str:
            events.append("bot_lookup")
            self.assertEqual(events, ["commit", "bot_lookup"])
            return "test_bot"

        with (
            patch.object(
                membership,
                "get_join_verification",
                new=AsyncMock(return_value=record),
            ),
            patch.object(
                membership,
                "_callback_bot_username",
                new=AsyncMock(side_effect=username_after_release),
            ),
        ):
            await membership._handle_shared_challenge_callback(
                callback,
                session,
                kind=join_verification.VERIFICATION_KIND_PATROL,
            )

        callback.answer.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
