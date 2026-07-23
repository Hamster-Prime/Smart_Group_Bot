"""Pre-verification message gate and join-residue cleanup tests."""
from __future__ import annotations

import os
import tempfile
import unittest
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from bot.db.models import UserWarning
from bot.db.engine import init_db
from bot.middlewares.verification_gate import PendingVerificationGateMiddleware
from bot.services.authz import authorize_group
from bot.services.join_verification import upsert_join_verification
from bot.services.recent_messages import (
    clear_member_join_marker,
    clear_recent_member_messages,
    consume_member_removal,
    delete_messages_since_join,
    drain_recent_member_messages,
    mark_member_join,
    mark_member_removed,
    member_join_marker,
    record_group_message,
    retract_removed_member_residue,
)
from bot.utils.timezone import now_shanghai_naive

GROUP_ID = -100


def _message_event(
    *,
    user_id: int,
    message_id: int = 41,
    is_bot: bool = False,
    sender_chat: object | None = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        chat=SimpleNamespace(id=GROUP_ID, type="supergroup"),
        from_user=SimpleNamespace(id=user_id, is_bot=is_bot),
        sender_chat=sender_chat,
        message_id=message_id,
        bot=SimpleNamespace(
            delete_messages=AsyncMock(return_value=True),
            delete_message=AsyncMock(return_value=True),
        ),
        delete=AsyncMock(),
    )


def _settings():
    from bot.config import Settings

    settings = Settings(_env_file=None)
    settings.super_admin_id = 1
    return settings


class RecentMessageBufferTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        clear_recent_member_messages()

    def tearDown(self) -> None:
        clear_recent_member_messages()

    def test_drain_returns_and_clears_tracked_ids(self) -> None:
        record_group_message(GROUP_ID, 900, 11)
        record_group_message(GROUP_ID, 900, 12)
        record_group_message(GROUP_ID, 901, 13)

        drained = drain_recent_member_messages(GROUP_ID, 900)
        self.assertEqual(drained, [11, 12])
        self.assertEqual(drain_recent_member_messages(GROUP_ID, 900), [])
        self.assertEqual(drain_recent_member_messages(GROUP_ID, 901), [13])

    def test_since_bound_keeps_older_entries_tracked(self) -> None:
        old = now_shanghai_naive() - timedelta(minutes=30)
        record_group_message(GROUP_ID, 902, 21, at=old)
        record_group_message(GROUP_ID, 902, 22)

        drained = drain_recent_member_messages(
            GROUP_ID,
            902,
            since=now_shanghai_naive() - timedelta(minutes=5),
        )
        self.assertEqual(drained, [22])
        # The pre-join entry is retained for a later full-window sweep.
        self.assertEqual(drain_recent_member_messages(GROUP_ID, 902), [21])

    def test_join_marker_is_kept_on_replay_and_cleared_on_leave(self) -> None:
        mark_member_join(GROUP_ID, 903)
        first = member_join_marker(GROUP_ID, 903)
        mark_member_join(
            GROUP_ID,
            903,
            at=now_shanghai_naive() + timedelta(seconds=60),
        )
        self.assertEqual(member_join_marker(GROUP_ID, 903), first)
        clear_member_join_marker(GROUP_ID, 903)
        self.assertIsNone(member_join_marker(GROUP_ID, 903))

    async def test_delete_since_join_requires_fresh_marker(self) -> None:
        bot = SimpleNamespace(delete_messages=AsyncMock(return_value=True))
        record_group_message(GROUP_ID, 904, 31)

        deleted = await delete_messages_since_join(bot, GROUP_ID, 904)
        self.assertEqual(deleted, 0)
        bot.delete_messages.assert_not_awaited()

        mark_member_join(
            GROUP_ID,
            904,
            at=now_shanghai_naive() - timedelta(hours=2),
        )
        deleted = await delete_messages_since_join(bot, GROUP_ID, 904)
        self.assertEqual(deleted, 0)
        bot.delete_messages.assert_not_awaited()

    async def test_delete_since_join_bulk_deletes_raced_messages(self) -> None:
        bot = SimpleNamespace(delete_messages=AsyncMock(return_value=True))
        mark_member_join(GROUP_ID, 905)
        record_group_message(GROUP_ID, 905, 41)
        record_group_message(GROUP_ID, 905, 42)

        deleted = await delete_messages_since_join(bot, GROUP_ID, 905)
        self.assertEqual(deleted, 2)
        bot.delete_messages.assert_awaited_once_with(
            chat_id=GROUP_ID,
            message_ids=[41, 42],
        )

    async def test_delete_falls_back_to_per_message_without_bulk_api(self) -> None:
        bot = SimpleNamespace(delete_message=AsyncMock(return_value=True))
        mark_member_join(GROUP_ID, 906)
        record_group_message(GROUP_ID, 906, 51)
        record_group_message(GROUP_ID, 906, 52)

        deleted = await delete_messages_since_join(bot, GROUP_ID, 906)
        self.assertEqual(deleted, 2)
        self.assertEqual(bot.delete_message.await_count, 2)

    async def test_explicit_marker_snapshot_survives_concurrent_leave_clear(self) -> None:
        # Terminal enforcement kicks/bans the member, and the resulting leave
        # update clears the live join marker before the post-removal sweep
        # runs. A marker snapshot taken before the removal must keep working.
        bot = SimpleNamespace(delete_messages=AsyncMock(return_value=True))
        mark_member_join(GROUP_ID, 907)
        record_group_message(GROUP_ID, 907, 55)
        marker = member_join_marker(GROUP_ID, 907)
        clear_member_join_marker(GROUP_ID, 907)

        deleted = await delete_messages_since_join(bot, GROUP_ID, 907)
        self.assertEqual(deleted, 0)
        bot.delete_messages.assert_not_awaited()

        deleted = await delete_messages_since_join(
            bot, GROUP_ID, 907, marker=marker
        )
        self.assertEqual(deleted, 1)
        bot.delete_messages.assert_awaited_once_with(
            chat_id=GROUP_ID,
            message_ids=[55],
        )

    async def test_explicit_stale_marker_does_not_sweep(self) -> None:
        bot = SimpleNamespace(delete_messages=AsyncMock(return_value=True))
        record_group_message(GROUP_ID, 908, 56)

        deleted = await delete_messages_since_join(
            bot,
            GROUP_ID,
            908,
            marker=now_shanghai_naive() - timedelta(hours=2),
        )
        self.assertEqual(deleted, 0)
        bot.delete_messages.assert_not_awaited()

    def test_member_removal_mark_is_one_shot(self) -> None:
        mark_member_removed(GROUP_ID, 970)
        self.assertTrue(consume_member_removal(GROUP_ID, 970))
        # A single removal yields exactly one service message; the mark is
        # cleared so a later unrelated leave is not deleted.
        self.assertFalse(consume_member_removal(GROUP_ID, 970))

    def test_member_removal_mark_expires(self) -> None:
        mark_member_removed(
            GROUP_ID,
            971,
            at=now_shanghai_naive() - timedelta(minutes=20),
        )
        self.assertFalse(consume_member_removal(GROUP_ID, 971))

    def test_unmarked_member_removal_is_not_consumed(self) -> None:
        self.assertFalse(consume_member_removal(GROUP_ID, 972))

    async def test_retract_arms_removal_notice_for_fresh_join(self) -> None:
        bot = SimpleNamespace(delete_messages=AsyncMock(return_value=True))
        mark_member_join(GROUP_ID, 973)
        record_group_message(GROUP_ID, 973, 57)

        deleted = await retract_removed_member_residue(bot, GROUP_ID, 973)
        self.assertEqual(deleted, 1)
        # The join residue was swept AND the pending "X removed" notice armed.
        self.assertTrue(consume_member_removal(GROUP_ID, 973))

    async def test_retract_does_not_arm_removal_for_stale_join(self) -> None:
        # An established member's ban must not delete their removal notice.
        bot = SimpleNamespace(delete_messages=AsyncMock(return_value=True))
        deleted = await retract_removed_member_residue(
            bot,
            GROUP_ID,
            974,
            marker=now_shanghai_naive() - timedelta(hours=2),
        )
        self.assertEqual(deleted, 0)
        self.assertFalse(consume_member_removal(GROUP_ID, 974))


class PendingVerificationGateTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        clear_recent_member_messages()
        fd, self._db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.engine, self.session_factory = await init_db(
            f"sqlite+aiosqlite:///{self._db_path}"
        )
        async with self.session_factory() as session:
            await authorize_group(session, GROUP_ID, 1)
            await session.commit()
        self.middleware = PendingVerificationGateMiddleware(self.session_factory)

    async def asyncTearDown(self) -> None:
        clear_recent_member_messages()
        await self.engine.dispose()
        for suffix in ("", "-wal", "-shm"):
            try:
                os.remove(f"{self._db_path}{suffix}")
            except OSError:
                pass

    async def _add_pending_verification(self, user_id: int) -> None:
        async with self.session_factory() as session:
            await upsert_join_verification(
                session,
                group_id=GROUP_ID,
                user_id=user_id,
                deadline_at=now_shanghai_naive() + timedelta(minutes=5),
                prompt_message_id=700,
            )
            await session.commit()

    async def test_pending_sender_message_is_deleted_and_swallowed(self) -> None:
        await self._add_pending_verification(910)
        handler = AsyncMock(return_value="handled")
        event = _message_event(user_id=910)

        result = await self.middleware(handler, event, {"settings": _settings()})

        self.assertIsNone(result)
        handler.assert_not_awaited()
        event.delete.assert_awaited_once()

    async def test_clean_sender_passes_and_is_tracked_for_sweeps(self) -> None:
        handler = AsyncMock(return_value="handled")
        event = _message_event(user_id=911, message_id=61)

        result = await self.middleware(handler, event, {"settings": _settings()})

        self.assertEqual(result, "handled")
        event.delete.assert_not_awaited()
        self.assertEqual(drain_recent_member_messages(GROUP_ID, 911), [61])

    async def test_super_admin_bypasses_gate(self) -> None:
        await self._add_pending_verification(1)
        handler = AsyncMock(return_value="handled")
        event = _message_event(user_id=1)

        result = await self.middleware(handler, event, {"settings": _settings()})

        self.assertEqual(result, "handled")
        event.delete.assert_not_awaited()

    async def test_gate_failure_fails_open(self) -> None:
        handler = AsyncMock(return_value="handled")
        event = _message_event(user_id=912)
        with patch(
            "bot.middlewares.verification_gate.verification_restriction_required",
            side_effect=RuntimeError("db down"),
        ):
            result = await self.middleware(handler, event, {"settings": _settings()})

        self.assertEqual(result, "handled")
        event.delete.assert_not_awaited()

    async def test_delete_failure_schedules_durable_cleanup(self) -> None:
        await self._add_pending_verification(913)
        handler = AsyncMock(return_value="handled")
        event = _message_event(user_id=913)
        event.delete = AsyncMock(side_effect=RuntimeError("flood"))

        with patch(
            "bot.middlewares.verification_gate.schedule_message_auto_delete_durable",
            new=AsyncMock(return_value=True),
        ) as durable:
            result = await self.middleware(handler, event, {"settings": _settings()})

        self.assertIsNone(result)
        handler.assert_not_awaited()
        durable.assert_awaited_once()

    async def test_join_service_message_is_tracked_under_each_new_member(self) -> None:
        # The "xxx joined" service message reprints the member's display name;
        # it must be recorded under the announced member (not the inviter) so
        # a later timeout kick / screening ban retracts it with the member's
        # other residue.
        handler = AsyncMock(return_value="handled")
        event = _message_event(user_id=915, message_id=71)
        event.new_chat_members = [
            SimpleNamespace(id=916, is_bot=False),
            SimpleNamespace(id=917, is_bot=False),
            SimpleNamespace(id=918, is_bot=True),
        ]

        result = await self.middleware(handler, event, {"settings": _settings()})

        self.assertEqual(result, "handled")
        self.assertEqual(drain_recent_member_messages(GROUP_ID, 916), [71])
        self.assertEqual(drain_recent_member_messages(GROUP_ID, 917), [71])
        self.assertEqual(drain_recent_member_messages(GROUP_ID, 918), [])

    async def test_admin_invited_join_service_message_is_still_tracked(self) -> None:
        # An admin inviting a member is exempt from the gate as the sender,
        # but the join announcement still belongs to the invited member.
        handler = AsyncMock(return_value="handled")
        event = _message_event(user_id=1, message_id=72)
        event.new_chat_members = [SimpleNamespace(id=919, is_bot=False)]

        result = await self.middleware(handler, event, {"settings": _settings()})

        self.assertEqual(result, "handled")
        self.assertEqual(drain_recent_member_messages(GROUP_ID, 919), [72])
        # The exempt inviter's own buffer must not absorb the announcement.
        self.assertEqual(drain_recent_member_messages(GROUP_ID, 1), [])

    async def test_removed_member_leave_service_message_is_deleted(self) -> None:
        # After a terminal removal the "X was removed" service message reprints
        # the unverified name; when the member is marked removed, the gate
        # deletes that announcement and swallows the update. Its from_user is
        # the acting bot, so the gate must key on left_chat_member instead.
        mark_member_removed(GROUP_ID, 920)
        handler = AsyncMock(return_value="handled")
        event = _message_event(user_id=99, message_id=81, is_bot=True)
        event.left_chat_member = SimpleNamespace(id=920, is_bot=False)

        result = await self.middleware(handler, event, {"settings": _settings()})

        self.assertIsNone(result)
        handler.assert_not_awaited()
        event.delete.assert_awaited_once()
        # One-shot: a subsequent leave message for the same user is left alone.
        self.assertFalse(consume_member_removal(GROUP_ID, 920))

    async def test_unmarked_member_leave_service_message_passes_through(self) -> None:
        handler = AsyncMock(return_value="handled")
        event = _message_event(user_id=99, message_id=82, is_bot=True)
        event.left_chat_member = SimpleNamespace(id=921, is_bot=False)

        result = await self.middleware(handler, event, {"settings": _settings()})

        self.assertEqual(result, "handled")
        event.delete.assert_not_awaited()

    async def test_removed_member_leave_delete_failure_uses_durable_queue(self) -> None:
        mark_member_removed(GROUP_ID, 922)
        handler = AsyncMock(return_value="handled")
        event = _message_event(user_id=99, message_id=83, is_bot=True)
        event.left_chat_member = SimpleNamespace(id=922, is_bot=False)
        event.delete = AsyncMock(side_effect=RuntimeError("flood"))

        with patch(
            "bot.middlewares.verification_gate.schedule_message_auto_delete_durable",
            new=AsyncMock(return_value=True),
        ) as durable:
            result = await self.middleware(handler, event, {"settings": _settings()})

        self.assertIsNone(result)
        handler.assert_not_awaited()
        durable.assert_awaited_once()

    async def test_banned_sender_is_not_gated_here(self) -> None:
        # Local bans are the GlobalBanEnforcementMiddleware's job; without a
        # verification record this gate must stay out of the way.
        async with self.session_factory() as session:
            session.add(
                UserWarning(
                    group_id=GROUP_ID,
                    user_id=914,
                    count=3,
                    is_banned=True,
                )
            )
            await session.commit()
        handler = AsyncMock(return_value="handled")
        event = _message_event(user_id=914)

        result = await self.middleware(handler, event, {"settings": _settings()})

        self.assertEqual(result, "handled")
        event.delete.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
