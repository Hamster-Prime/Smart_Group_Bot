import asyncio
import os
import tempfile
import unittest
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, call, patch

from sqlalchemy import select

from bot.db.engine import init_db
from bot.db.models import (
    Group,
    JoinVerification,
    ModerationExemption,
    UserWarning,
    Violation,
)
from bot.handlers import group
from bot.services.authz import authorize_group
from bot.services.join_screening import add_global_ban
from bot.services.moderation import ModerationService
from bot.services.update_completion import (
    UpdateCompletionReceipt,
    bind_update_completion,
    reset_update_completion,
)
from bot.utils.timezone import now_shanghai_naive


class ModerationActionCallbackTests(unittest.IsolatedAsyncioTestCase):
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

        self.settings = SimpleNamespace(
            super_admin_id=1,
            moderation=SimpleNamespace(warn_threshold=3),
            bot=SimpleNamespace(auto_delete_categories=[], auto_delete_seconds=0),
        )

    async def asyncTearDown(self) -> None:
        await self.engine.dispose()
        for suffix in ("", "-wal", "-shm"):
            try:
                os.remove(f"{self._db_path}{suffix}")
            except OSError:
                pass

    def _callback(
        self,
        action: str,
        violation_id: int,
        *,
        bot: SimpleNamespace | None = None,
        operator_id: int = 9,
        operator_username: str | None = None,
        operator_name: str | None = None,
        group_id: int = -100,
    ) -> SimpleNamespace:
        return SimpleNamespace(
            data=f"mact:{action}:{violation_id}",
            message=SimpleNamespace(
                chat=SimpleNamespace(id=group_id, type="supergroup"),
                message_id=555,
                html_text=(
                    "<b>内容审核 · 已警告</b>\n\n<blockquote>"
                    "<b>用户</b>　<code>@aLnTpu</code>\n"
                    "<b>处理结果</b>　已删除违规消息并发出警告</blockquote>\n\n"
                    "<blockquote expandable>"
                    "<b>警告次数</b>　<code>1/3</code>\n"
                    "<b>原因</b>　发布上门按摩服务广告信息\n"
                    "<b>依据规则</b>　#47（语义）</blockquote>"
                ),
                reply_markup=group._build_moderation_action_keyboard(violation_id),
            ),
            from_user=SimpleNamespace(
                id=operator_id,
                username=operator_username,
                full_name=operator_name,
            ),
            bot=bot
            or SimpleNamespace(
                ban_chat_member=AsyncMock(),
                unban_chat_member=AsyncMock(),
                edit_message_text=AsyncMock(),
            ),
            answer=AsyncMock(),
        )

    async def _seed_violation(
        self,
        action_taken: str,
        *,
        user_id: int = 42,
        warning: tuple[int, bool] | None = None,
        pending_verification: bool = False,
    ) -> int:
        async with self.session_factory() as session:
            if warning is not None and action_taken.startswith(("ban_warning", "ban_applied")):
                existing_states = list(
                    await session.scalars(
                        select(Violation.action_taken).where(
                            Violation.group_id == -100,
                            Violation.user_id == user_id,
                        )
                    )
                )
                existing_counted = sum(
                    state.startswith(("ban_warning", "ban_applied"))
                    and not state.endswith("_reverted")
                    for state in existing_states
                )
                missing_prior = max(0, warning[0] - existing_counted - 1)
                session.add_all(
                    Violation(
                        group_id=-100,
                        user_id=user_id,
                        message_text="prior",
                        action_taken="ban_warning",
                    )
                    for _ in range(missing_prior)
                )
            violation = Violation(
                group_id=-100,
                user_id=user_id,
                message_text="bad",
                action_taken=action_taken,
            )
            session.add(violation)
            if warning is not None:
                session.add(
                    UserWarning(
                        group_id=-100,
                        user_id=user_id,
                        count=warning[0],
                        is_banned=warning[1],
                    )
                )
            if pending_verification:
                session.add(
                    JoinVerification(
                        group_id=-100,
                        user_id=user_id,
                        kind="moderation",
                        provider="turnstile",
                        reason="review",
                        display_name="Member",
                        prompt_message_id=10,
                        deadline_at=now_shanghai_naive() + timedelta(minutes=5),
                    )
                )
            await session.commit()
            return int(violation.id)

    async def _invoke(
        self,
        callback: SimpleNamespace,
        *,
        allowed: bool = True,
        restore_result: bool = True,
    ) -> None:
        async with self.session_factory() as session:
            with (
                patch(
                    "bot.handlers.group.is_group_admin_or_higher",
                    new=AsyncMock(return_value=allowed),
                ),
                patch(
                    "bot.handlers.group.restore_member_permissions",
                    new=AsyncMock(return_value=restore_result),
                ),
            ):
                await group.on_moderation_action(
                    callback,
                    session=session,
                    settings=self.settings,
                )

    async def test_non_admin_cannot_use_moderation_actions(self) -> None:
        violation_id = await self._seed_violation("warn")
        callback = self._callback("ban", violation_id)

        await self._invoke(callback, allowed=False)

        callback.bot.ban_chat_member.assert_not_awaited()
        self.assertTrue(callback.answer.await_args.kwargs["show_alert"])
        async with self.session_factory() as session:
            violation = await session.get(Violation, violation_id)
            self.assertEqual(violation.action_taken, "warn")

    async def test_callback_rejects_event_from_another_group(self) -> None:
        violation_id = await self._seed_violation("warn")
        async with self.session_factory() as session:
            session.add(Group(id=-101, title="other", settings={}))
            await authorize_group(session, -101, 1)
            await session.commit()
        callback = self._callback("ban", violation_id, group_id=-101)

        await self._invoke(callback)

        callback.bot.ban_chat_member.assert_not_awaited()
        self.assertIn("不属于当前群", callback.answer.await_args.args[0])

    async def test_direct_ban_protects_super_admin_target(self) -> None:
        violation_id = await self._seed_violation("warn", user_id=1)
        callback = self._callback("ban", violation_id)

        await self._invoke(callback)

        callback.bot.ban_chat_member.assert_not_awaited()
        self.assertIn("最高管理员", callback.answer.await_args.args[0])

    async def test_direct_ban_persists_local_ban_and_cancels_verification(self) -> None:
        violation_id = await self._seed_violation(
            "warn",
            pending_verification=True,
        )
        callback = self._callback("ban", violation_id)

        await self._invoke(callback)

        callback.bot.ban_chat_member.assert_awaited_once_with(
            -100,
            42,
            revoke_messages=True,
        )
        # The rewritten notice is the only feedback: a successful direct ban
        # acks the callback silently instead of announcing a duplicate line.
        callback.answer.assert_awaited_once_with()
        async with self.session_factory() as session:
            violation = await session.get(Violation, violation_id)
            self.assertEqual(violation.action_taken, "warn_direct")
            warning = await session.scalar(
                select(UserWarning).where(
                    UserWarning.group_id == -100,
                    UserWarning.user_id == 42,
                )
            )
            self.assertEqual((warning.count, warning.is_banned), (0, True))
            pending = await session.scalar(
                select(JoinVerification).where(
                    JoinVerification.group_id == -100,
                    JoinVerification.user_id == 42,
                )
            )
            self.assertIsNone(pending)

    async def test_direct_ban_response_loss_confirms_remote_ban(self) -> None:
        violation_id = await self._seed_violation("warn", pending_verification=True)
        bot = SimpleNamespace(
            ban_chat_member=AsyncMock(side_effect=RuntimeError("response lost")),
            get_chat_member=AsyncMock(return_value=SimpleNamespace(status="kicked")),
            unban_chat_member=AsyncMock(),
        )
        callback = self._callback("ban", violation_id, bot=bot)

        await self._invoke(callback)

        bot.ban_chat_member.assert_awaited_once_with(
            -100,
            42,
            revoke_messages=True,
        )
        bot.get_chat_member.assert_awaited_once_with(-100, 42)
        async with self.session_factory() as session:
            warning = await session.scalar(
                select(UserWarning).where(
                    UserWarning.group_id == -100,
                    UserWarning.user_id == 42,
                )
            )
            pending = await session.scalar(
                select(JoinVerification).where(
                    JoinVerification.group_id == -100,
                    JoinVerification.user_id == 42,
                )
            )
            self.assertTrue(warning.is_banned)
            self.assertIsNone(pending)

    async def test_unconfirmed_direct_ban_keeps_durable_local_policy(self) -> None:
        violation_id = await self._seed_violation("ban_warning", warning=(2, False))
        bot = SimpleNamespace(
            ban_chat_member=AsyncMock(return_value=False),
            unban_chat_member=AsyncMock(),
        )
        callback = self._callback("ban", violation_id, bot=bot)

        await self._invoke(callback)

        bot.ban_chat_member.assert_awaited_once_with(
            -100,
            42,
            revoke_messages=True,
        )
        async with self.session_factory() as session:
            violation = await session.get(Violation, violation_id)
            self.assertEqual(violation.action_taken, "ban_warning_direct")
            warning = await session.scalar(
                select(UserWarning).where(
                    UserWarning.group_id == -100,
                    UserWarning.user_id == 42,
                )
            )
            self.assertEqual((warning.count, warning.is_banned), (2, True))
            self.assertFalse(violation.ban_enforced)
        self.assertIn("保留本群封禁状态", callback.answer.await_args.args[0])

    async def test_unconfirmed_direct_ban_retries_then_becomes_idempotent(self) -> None:
        violation_id = await self._seed_violation("ban_warning", warning=(2, False))
        bot = SimpleNamespace(
            ban_chat_member=AsyncMock(side_effect=[False, True]),
            unban_chat_member=AsyncMock(),
            edit_message_text=AsyncMock(),
        )

        first = self._callback("ban", violation_id, bot=bot)
        failed_receipt = UpdateCompletionReceipt()
        token = bind_update_completion(failed_receipt)
        try:
            await self._invoke(first)
        finally:
            reset_update_completion(token)
        self.assertTrue(failed_receipt.deferred)
        self.assertFalse(await failed_receipt.wait())

        second = self._callback("ban", violation_id, bot=bot)
        successful_receipt = UpdateCompletionReceipt()
        token = bind_update_completion(successful_receipt)
        try:
            await self._invoke(second)
        finally:
            reset_update_completion(token)
        self.assertFalse(successful_receipt.deferred)

        third = self._callback("ban", violation_id, bot=bot)
        await self._invoke(third)

        self.assertEqual(
            bot.ban_chat_member.await_args_list,
            [
                call(-100, 42, revoke_messages=True),
                call(-100, 42, revoke_messages=True),
            ],
        )
        self.assertIn("执行过", third.answer.await_args.args[0])
        async with self.session_factory() as session:
            violation = await session.get(Violation, violation_id)
            warning = await session.scalar(
                select(UserWarning).where(
                    UserWarning.group_id == -100,
                    UserWarning.user_id == 42,
                )
            )
        self.assertEqual(violation.action_taken, "ban_warning_direct")
        self.assertTrue(violation.ban_enforced)
        self.assertTrue(warning.is_banned)

    async def test_failed_direct_ban_does_not_overwrite_concurrent_warning_update(self) -> None:
        violation_id = await self._seed_violation("ban_warning", warning=(2, False))

        async def concurrent_update_then_fail(*_args, **_kwargs) -> None:
            async with self.session_factory() as session:
                warning = await session.scalar(
                    select(UserWarning).where(
                        UserWarning.group_id == -100,
                        UserWarning.user_id == 42,
                    )
                )
                warning.count = 3
                await session.commit()
            raise RuntimeError("no permission")

        bot = SimpleNamespace(
            ban_chat_member=AsyncMock(side_effect=concurrent_update_then_fail),
            unban_chat_member=AsyncMock(),
        )
        callback = self._callback("ban", violation_id, bot=bot)

        await self._invoke(callback)

        bot.ban_chat_member.assert_awaited_once_with(
            -100,
            42,
            revoke_messages=True,
        )
        async with self.session_factory() as session:
            violation = await session.get(Violation, violation_id)
            warning = await session.scalar(
                select(UserWarning).where(
                    UserWarning.group_id == -100,
                    UserWarning.user_id == 42,
                )
            )
            self.assertEqual(violation.action_taken, "ban_warning_direct")
            self.assertEqual((warning.count, warning.is_banned), (3, True))
        self.assertIn("保留本群封禁状态", callback.answer.await_args.args[0])

    async def test_atomic_warning_increment_handles_insert_race_and_threshold(self) -> None:
        service = ModerationService(
            SimpleNamespace(warn_threshold=3),
            SimpleNamespace(),
        )

        async def add_one() -> tuple[int, bool]:
            async with self.session_factory() as session:
                result = await service.add_warning(session, -100, 77)
                await session.commit()
                return result

        first, second = await asyncio.gather(add_one(), add_one())
        self.assertEqual({first[0], second[0]}, {1, 2})

        third = await add_one()
        self.assertEqual(third, (3, True))
        async with self.session_factory() as session:
            warning = await session.scalar(
                select(UserWarning).where(
                    UserWarning.group_id == -100,
                    UserWarning.user_id == 77,
                )
            )
            self.assertEqual((warning.count, warning.is_banned), (3, True))

    async def test_failed_auto_ban_database_rollback_is_atomic(self) -> None:
        clean_id = await self._seed_violation(
            "ban_applied",
            user_id=78,
            warning=(3, True),
        )
        async with self.session_factory() as session:
            clean = await group._rollback_failed_automatic_ban(
                session,
                group_id=-100,
                user_id=78,
                violation_id=clean_id,
                warning_count=3,
            )
        self.assertTrue(clean)

        stale_id = await self._seed_violation(
            "ban_applied",
            user_id=79,
            warning=(4, True),
        )
        async with self.session_factory() as session:
            stale = await group._rollback_failed_automatic_ban(
                session,
                group_id=-100,
                user_id=79,
                violation_id=stale_id,
                warning_count=3,
            )
        self.assertFalse(stale)

        async with self.session_factory() as session:
            clean_violation = await session.get(Violation, clean_id)
            stale_violation = await session.get(Violation, stale_id)
            clean_warning = await session.scalar(
                select(UserWarning).where(
                    UserWarning.group_id == -100,
                    UserWarning.user_id == 78,
                )
            )
            stale_warning = await session.scalar(
                select(UserWarning).where(
                    UserWarning.group_id == -100,
                    UserWarning.user_id == 79,
                )
            )
            self.assertEqual(clean_violation.action_taken, "ban_warning")
            self.assertEqual((clean_warning.count, clean_warning.is_banned), (3, False))
            self.assertEqual(stale_violation.action_taken, "ban_applied")
            self.assertEqual((stale_warning.count, stale_warning.is_banned), (4, True))

    async def test_counted_false_positive_decrements_exactly_once(self) -> None:
        violation_id = await self._seed_violation("ban_warning", warning=(2, False))
        first = self._callback("undo", violation_id)
        second = self._callback("undo", violation_id)

        await self._invoke(first)
        await self._invoke(second)

        async with self.session_factory() as session:
            violation = await session.get(Violation, violation_id)
            self.assertEqual(violation.action_taken, "ban_warning_reverted")
            warning = await session.scalar(
                select(UserWarning).where(
                    UserWarning.group_id == -100,
                    UserWarning.user_id == 42,
                )
            )
            self.assertEqual(warning.count, 1)
        self.assertIn("已撤销", second.answer.await_args.args[0])

    async def test_undo_aborts_when_warning_changes_before_lock(self) -> None:
        warning = SimpleNamespace(id=7, count=2, is_banned=True)
        session = SimpleNamespace(
            execute=AsyncMock(
                side_effect=[
                    SimpleNamespace(scalar_one_or_none=lambda: warning),
                    SimpleNamespace(rowcount=0),
                ]
            ),
            rollback=AsyncMock(),
        )
        callback = self._callback("undo", 99)
        violation = SimpleNamespace(id=99, group_id=-100, user_id=42)

        await group._moderation_undo_false_positive(
            callback,
            session,
            self.settings,
            violation,
            base_state="ban_applied",
            current_state="ban_applied",
            markers=set(),
        )

        session.rollback.assert_awaited_once()
        callback.bot.unban_chat_member.assert_not_awaited()
        self.assertIn("状态已变化", callback.answer.await_args.args[0])

    async def test_auto_ban_undo_unbans_when_count_drops_below_threshold(self) -> None:
        violation_id = await self._seed_violation("ban_applied", warning=(3, True))
        callback = self._callback("undo", violation_id)

        await self._invoke(callback)

        callback.bot.unban_chat_member.assert_awaited_once_with(
            -100,
            42,
            only_if_banned=True,
        )
        async with self.session_factory() as session:
            violation = await session.get(Violation, violation_id)
            self.assertEqual(violation.action_taken, "ban_applied_reverted")
            warning = await session.scalar(
                select(UserWarning).where(
                    UserWarning.group_id == -100,
                    UserWarning.user_id == 42,
                )
            )
            self.assertEqual((warning.count, warning.is_banned), (2, False))

    async def test_direct_marker_keeps_ban_when_auto_event_is_reverted(self) -> None:
        violation_id = await self._seed_violation(
            "ban_applied_direct",
            warning=(3, True),
        )
        callback = self._callback("undo", violation_id)

        await self._invoke(callback)

        callback.bot.unban_chat_member.assert_not_awaited()
        async with self.session_factory() as session:
            violation = await session.get(Violation, violation_id)
            self.assertEqual(violation.action_taken, "ban_applied_direct_reverted")
            warning = await session.scalar(
                select(UserWarning).where(
                    UserWarning.group_id == -100,
                    UserWarning.user_id == 42,
                )
            )
            self.assertEqual((warning.count, warning.is_banned), (2, True))

    async def test_earlier_direct_marker_keeps_ban_when_later_event_is_reverted(self) -> None:
        await self._seed_violation("ban_warning_direct")
        later_violation_id = await self._seed_violation(
            "ban_applied",
            warning=(3, True),
        )
        callback = self._callback("undo", later_violation_id)

        await self._invoke(callback)

        callback.bot.unban_chat_member.assert_not_awaited()
        async with self.session_factory() as session:
            violation = await session.get(Violation, later_violation_id)
            self.assertEqual(violation.action_taken, "ban_applied_reverted")
            warning = await session.scalar(
                select(UserWarning).where(
                    UserWarning.group_id == -100,
                    UserWarning.user_id == 42,
                )
            )
            self.assertEqual((warning.count, warning.is_banned), (2, True))
        self.assertIn("现有封禁保持不变", callback.answer.await_args.args[0])

    async def test_old_button_cannot_decrement_new_warning_generation(self) -> None:
        old_violation_id = await self._seed_violation(
            "ban_warning",
            warning=(1, False),
        )
        async with self.session_factory() as session:
            warning = await session.scalar(
                select(UserWarning).where(
                    UserWarning.group_id == -100,
                    UserWarning.user_id == 42,
                )
            )
            await session.delete(warning)
            await session.commit()
        new_violation_id = await self._seed_violation(
            "ban_warning",
            warning=(1, False),
        )

        await self._invoke(self._callback("undo", old_violation_id))

        async with self.session_factory() as session:
            warning = await session.scalar(
                select(UserWarning).where(
                    UserWarning.group_id == -100,
                    UserWarning.user_id == 42,
                )
            )
            new_violation = await session.get(Violation, new_violation_id)
            self.assertEqual(warning.count, 1)
            self.assertEqual(new_violation.action_taken, "ban_warning")

    async def test_old_direct_marker_does_not_block_new_generation_undo(self) -> None:
        await self._seed_violation(
            "ban_applied_direct",
            warning=(1, True),
        )
        async with self.session_factory() as session:
            warning = await session.scalar(
                select(UserWarning).where(
                    UserWarning.group_id == -100,
                    UserWarning.user_id == 42,
                )
            )
            await session.delete(warning)
            await session.commit()
        current_id = await self._seed_violation(
            "ban_applied",
            warning=(1, True),
        )
        callback = self._callback("undo", current_id)

        await self._invoke(callback)

        callback.bot.unban_chat_member.assert_awaited_once()
        async with self.session_factory() as session:
            warning = await session.scalar(
                select(UserWarning).where(
                    UserWarning.group_id == -100,
                    UserWarning.user_id == 42,
                )
            )
            self.assertIsNone(warning)

    async def test_undo_policy_check_failure_keeps_durable_unban_journal(self) -> None:
        violation_id = await self._seed_violation(
            "ban_applied",
            warning=(3, True),
        )
        callback = self._callback("undo", violation_id)

        async with self.session_factory() as session:
            original_commit = session.commit
            commit_calls = 0

            async def fail_final_commit() -> None:
                nonlocal commit_calls
                commit_calls += 1
                # 1) release the callback lookup snapshot; 2) durably commit
                # the warning rollback + unban journal. Fail the subsequent
                # fresh policy-check commit so the recovery semantics, rather
                # than the preparatory transaction, are exercised.
                if commit_calls < 3:
                    await original_commit()
                    return
                raise RuntimeError("commit failed")

            with (
                patch(
                    "bot.handlers.group.is_group_admin_or_higher",
                    new=AsyncMock(return_value=True),
                ),
                patch(
                    "bot.handlers.group.restore_member_permissions",
                    new=AsyncMock(return_value=True),
                ),
                patch.object(
                    session,
                    "commit",
                    new=AsyncMock(side_effect=fail_final_commit),
                ),
            ):
                await group.on_moderation_action(
                    callback,
                    session=session,
                    settings=self.settings,
                )

        callback.bot.unban_chat_member.assert_not_awaited()
        callback.bot.ban_chat_member.assert_not_awaited()
        self.assertIn("后台继续重试", callback.answer.await_args.args[0])
        async with self.session_factory() as session:
            warning = await session.scalar(
                select(UserWarning).where(
                    UserWarning.group_id == -100,
                    UserWarning.user_id == 42,
                )
            )
            recovery = await session.scalar(
                select(JoinVerification).where(
                    JoinVerification.group_id == -100,
                    JoinVerification.user_id == 42,
                )
            )
            self.assertEqual((warning.count, warning.is_banned), (2, False))
            self.assertEqual(recovery.status, "unbanning")

    async def test_standalone_warning_undo_only_marks_event_reverted(self) -> None:
        violation_id = await self._seed_violation("warn")
        callback = self._callback("undo", violation_id)

        await self._invoke(callback)

        async with self.session_factory() as session:
            violation = await session.get(Violation, violation_id)
            self.assertEqual(violation.action_taken, "warn_reverted")
            warning = await session.scalar(
                select(UserWarning).where(
                    UserWarning.group_id == -100,
                    UserWarning.user_id == 42,
                )
            )
            self.assertIsNone(warning)

    async def test_permanent_exemption_is_idempotent(self) -> None:
        violation_id = await self._seed_violation("warn")
        first = self._callback("exempt", violation_id, operator_id=9)
        second = self._callback("exempt", violation_id, operator_id=10)

        await self._invoke(first)
        await self._invoke(second)

        async with self.session_factory() as session:
            rows = list(
                (
                    await session.scalars(
                        select(ModerationExemption).where(
                            ModerationExemption.group_id == -100,
                            ModerationExemption.user_id == 42,
                        )
                    )
                ).all()
            )
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0].created_by, 9)
        self.assertIn("已在当前群", second.answer.await_args.args[0])

    async def test_global_ban_lock_blocks_direct_group_ban(self) -> None:
        violation_id = await self._seed_violation("warn")
        async with self.session_factory() as session:
            await add_global_ban(session, 42, reason="global", created_by=1)
            await session.commit()
        callback = self._callback("ban", violation_id)

        await self._invoke(callback)

        callback.bot.ban_chat_member.assert_not_awaited()
        async with self.session_factory() as session:
            violation = await session.get(Violation, violation_id)
            self.assertEqual(violation.action_taken, "warn")
        self.assertIn("全局封禁", callback.answer.await_args.args[0])

    async def test_direct_ban_rewrites_notice_and_removes_buttons(self) -> None:
        violation_id = await self._seed_violation("warn")
        callback = self._callback("ban", violation_id, operator_username="mod_alice")

        await self._invoke(callback)

        callback.bot.edit_message_text.assert_awaited_once()
        kwargs = callback.bot.edit_message_text.await_args.kwargs
        self.assertEqual(kwargs["chat_id"], -100)
        self.assertEqual(kwargs["message_id"], 555)
        self.assertEqual(kwargs["parse_mode"], "HTML")
        self.assertIsNone(kwargs["reply_markup"])
        text = kwargs["text"]
        self.assertIn("<b>处理结果</b>　已被", text)
        self.assertIn("mod_alice", text)
        self.assertIn("手动封禁", text)
        # The original automated outcome line must be gone, other lines kept.
        self.assertNotIn("已删除违规消息并发出警告", text)
        self.assertIn("<b>用户</b>　<code>@aLnTpu</code>", text)
        self.assertIn("<blockquote expandable>", text)

    async def test_false_positive_undo_rewrites_notice(self) -> None:
        violation_id = await self._seed_violation("ban_warning", warning=(2, False))
        callback = self._callback("undo", violation_id, operator_username="mod_bob")

        await self._invoke(callback)

        callback.bot.edit_message_text.assert_awaited_once()
        text = callback.bot.edit_message_text.await_args.kwargs["text"]
        self.assertIn("<b>处理结果</b>　已被", text)
        self.assertIn("mod_bob", text)
        self.assertIn("确认误封", text)

    async def test_permanent_exemption_rewrites_notice(self) -> None:
        violation_id = await self._seed_violation("warn")
        callback = self._callback("exempt", violation_id, operator_username="mod_eve")

        await self._invoke(callback)

        callback.bot.edit_message_text.assert_awaited_once()
        text = callback.bot.edit_message_text.await_args.kwargs["text"]
        self.assertIn("mod_eve", text)
        self.assertIn("永久豁免", text)

    async def test_operator_without_username_uses_display_name(self) -> None:
        violation_id = await self._seed_violation("warn")
        callback = self._callback("ban", violation_id, operator_name="张三管理")

        await self._invoke(callback)

        callback.bot.edit_message_text.assert_awaited_once()
        text = callback.bot.edit_message_text.await_args.kwargs["text"]
        # Same display logic as the user line: profile link is stripped on send,
        # leaving the plain display name.
        self.assertIn("张三管理", text)
        self.assertNotIn("tg://user", text)
        self.assertIn("手动封禁", text)

    async def test_failed_action_does_not_rewrite_notice(self) -> None:
        violation_id = await self._seed_violation("warn")
        callback = self._callback("ban", violation_id, operator_username="mod_alice")

        await self._invoke(callback, allowed=False)

        callback.bot.edit_message_text.assert_not_awaited()

    async def test_idempotent_exemption_second_click_does_not_rewrite(self) -> None:
        violation_id = await self._seed_violation("warn")
        first = self._callback("exempt", violation_id, operator_username="mod_a")
        second = self._callback("exempt", violation_id, operator_username="mod_b")

        await self._invoke(first)
        await self._invoke(second)

        first.bot.edit_message_text.assert_awaited_once()
        second.bot.edit_message_text.assert_not_awaited()

    async def test_missing_html_text_is_safe(self) -> None:
        violation_id = await self._seed_violation("warn")
        callback = self._callback("ban", violation_id, operator_username="mod_alice")
        callback.message.html_text = None

        await self._invoke(callback)

        # Action still applied; the notice rewrite is simply skipped.
        callback.bot.ban_chat_member.assert_awaited_once_with(
            -100,
            42,
            revoke_messages=True,
        )
        callback.bot.edit_message_text.assert_not_awaited()
        # Without the rewrite (>48h inaccessible or deleted notice) the silent
        # success ack would leave no feedback at all, so the explicit
        # confirmation is restored on this path only.
        self.assertIn(
            "已在当前群直接封禁该用户",
            callback.answer.await_args.args[0],
        )

    async def test_failed_notice_edit_falls_back_to_explicit_ban_answer(self) -> None:
        violation_id = await self._seed_violation("warn")
        callback = self._callback("ban", violation_id, operator_username="mod_alice")
        callback.bot.edit_message_text = AsyncMock(
            side_effect=RuntimeError("message to edit not found")
        )

        await self._invoke(callback)

        callback.bot.ban_chat_member.assert_awaited_once_with(
            -100,
            42,
            revoke_messages=True,
        )
        self.assertIn(
            "已在当前群直接封禁该用户",
            callback.answer.await_args.args[0],
        )

    async def test_successful_direct_ban_posts_no_group_message_via_proxy(self) -> None:
        # In production the action runs through _DetachedCallbackProxy, whose
        # answer() posts a group message for non-empty text. A successful ban
        # must stay silent there: the rewritten notice is the feedback.
        violation_id = await self._seed_violation("warn")
        inner = self._callback("ban", violation_id, operator_username="mod_alice")
        inner.message.answer = AsyncMock()
        proxy = group._DetachedCallbackProxy(inner)

        async with self.session_factory() as session:
            with patch(
                "bot.handlers.group.is_group_admin_or_higher",
                new=AsyncMock(return_value=True),
            ):
                await group.on_moderation_action(
                    proxy,  # type: ignore[arg-type]
                    session=session,
                    settings=self.settings,
                )

        inner.bot.ban_chat_member.assert_awaited_once_with(
            -100,
            42,
            revoke_messages=True,
        )
        inner.bot.edit_message_text.assert_awaited_once()
        inner.message.answer.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
