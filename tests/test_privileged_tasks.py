from __future__ import annotations

import asyncio
import unittest
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from bot.config import Settings
from bot.handlers import admin, group, membership
from bot.services.join_verification import (
    UnbanRecovery,
    VERIFICATION_CALLBACK_APPROVE,
    build_verification_callback_data,
)
from bot.services.privileged_tasks import (
    PrivilegedTaskSubmission,
    flush_privileged_tasks,
    submit_privileged_task,
    wait_privileged_task,
)
from bot.services.request_priority import ExecutionPriority, current_execution_priority
from bot.services.update_completion import (
    UpdateCompletionReceipt,
    bind_update_completion,
    reset_update_completion,
)
from bot.web import settings_api


class _FakeSession:
    def __init__(self) -> None:
        self.commit = AsyncMock()
        self.rollback = AsyncMock()
        self.get = AsyncMock(return_value=None)

    def in_transaction(self) -> bool:
        return False

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        return None


class _FakeSessionFactory:
    def __call__(self) -> _FakeSession:
        return _FakeSession()


class PrivilegedTaskRunnerTests(unittest.IsolatedAsyncioTestCase):
    async def test_critical_lane_is_not_starved_by_security_jobs(self) -> None:
        release = asyncio.Event()
        all_security_started = asyncio.Event()
        security_started = 0

        async def security_job() -> None:
            nonlocal security_started
            security_started += 1
            if security_started == 4:
                all_security_started.set()
            await release.wait()

        for index in range(4):
            submission = submit_privileged_task(
                key=f"security-{index}",
                label="security blocker",
                operation=security_job,
                lane="security",
            )
            self.assertTrue(submission.accepted)

        await asyncio.wait_for(all_security_started.wait(), timeout=1.0)
        critical_started = asyncio.Event()
        observed_priority: list[ExecutionPriority] = []

        async def critical_job() -> None:
            observed_priority.append(current_execution_priority())
            critical_started.set()

        submission = submit_privileged_task(
            key="critical",
            label="critical command",
            operation=critical_job,
            lane="critical",
        )
        self.assertTrue(submission.accepted)
        await asyncio.wait_for(critical_started.wait(), timeout=0.25)
        self.assertEqual(observed_priority, [ExecutionPriority.CRITICAL])
        release.set()
        await flush_privileged_tasks(timeout_seconds=2.0)

    async def test_detached_job_defers_and_finishes_durable_update_receipt(self) -> None:
        receipt = UpdateCompletionReceipt()
        token = bind_update_completion(receipt)
        try:
            submission = submit_privileged_task(
                key="durable-receipt",
                label="durable receipt",
                operation=lambda: asyncio.sleep(0),
            )
        finally:
            reset_update_completion(token)

        self.assertTrue(receipt.deferred)
        self.assertTrue(
            await wait_privileged_task(submission.job_id, timeout_seconds=1.0)
        )
        self.assertTrue(await asyncio.wait_for(receipt.wait(), timeout=0.2))
        await flush_privileged_tasks(timeout_seconds=1.0)

    async def test_receipt_waits_for_every_detached_owner(self) -> None:
        receipt = UpdateCompletionReceipt()
        receipt.defer()
        receipt.defer()
        waiter = asyncio.create_task(receipt.wait())
        receipt.finish(True)
        await asyncio.sleep(0)
        self.assertFalse(waiter.done())
        receipt.finish(True)
        self.assertTrue(await asyncio.wait_for(waiter, timeout=0.2))

    async def test_receipt_does_not_finish_before_handler_registration_seals(self) -> None:
        receipt = UpdateCompletionReceipt()
        receipt.defer()
        receipt.finish(True)
        self.assertFalse(receipt._future.done())

        # A later detached owner may still register before the dispatcher
        # closes handler-time registration.
        receipt.defer()
        receipt.seal()
        waiter = asyncio.create_task(receipt.wait())
        await asyncio.sleep(0)
        self.assertFalse(waiter.done())
        receipt.finish(True)
        self.assertTrue(await asyncio.wait_for(waiter, timeout=0.2))

    async def test_orphan_keeps_receipt_and_dedup_key_until_child_exits(self) -> None:
        started = asyncio.Event()
        release = asyncio.Event()

        async def cancellation_resistant() -> None:
            started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                await release.wait()

        receipt = UpdateCompletionReceipt()
        token = bind_update_completion(receipt)
        try:
            submission = submit_privileged_task(
                key="orphan-receipt",
                label="orphan receipt",
                operation=cancellation_resistant,
                timeout_seconds=0.01,
            )
        finally:
            reset_update_completion(token)

        await asyncio.wait_for(started.wait(), timeout=0.2)
        await asyncio.sleep(0.03)
        waiter = asyncio.create_task(receipt.wait())
        await asyncio.sleep(0)
        self.assertFalse(waiter.done())

        duplicate_receipt = UpdateCompletionReceipt()
        duplicate_token = bind_update_completion(duplicate_receipt)
        try:
            duplicate = submit_privileged_task(
                key="orphan-receipt",
                label="duplicate orphan",
                operation=lambda: asyncio.sleep(0),
            )
        finally:
            reset_update_completion(duplicate_token)
        self.assertTrue(duplicate.accepted)
        self.assertFalse(duplicate.created)
        duplicate_waiter = asyncio.create_task(duplicate_receipt.wait())
        await asyncio.sleep(0)
        self.assertFalse(duplicate_waiter.done())

        release.set()
        self.assertTrue(await asyncio.wait_for(waiter, timeout=0.2))
        self.assertTrue(await asyncio.wait_for(duplicate_waiter, timeout=0.2))
        self.assertTrue(
            await wait_privileged_task(submission.job_id, timeout_seconds=0.2)
        )

    async def test_orphans_keep_real_lane_capacity_until_child_exits(self) -> None:
        release = asyncio.Event()
        all_started = asyncio.Event()
        fifth_started = asyncio.Event()
        started = 0

        async def cancellation_resistant() -> None:
            nonlocal started
            started += 1
            if started == 4:
                all_started.set()
            try:
                await release.wait()
            except asyncio.CancelledError:
                await release.wait()

        for index in range(4):
            submit_privileged_task(
                key=f"capacity-orphan-{index}",
                label="capacity orphan",
                operation=cancellation_resistant,
                lane="critical",
                timeout_seconds=0.01,
            )
        await asyncio.wait_for(all_started.wait(), timeout=0.5)
        await asyncio.sleep(0.03)

        async def fifth() -> None:
            fifth_started.set()

        submit_privileged_task(
            key="capacity-fifth",
            label="capacity fifth",
            operation=fifth,
            lane="critical",
            timeout_seconds=1.0,
        )
        await asyncio.sleep(0.05)
        self.assertFalse(fifth_started.is_set())

        release.set()
        await asyncio.wait_for(fifth_started.wait(), timeout=0.5)
        await flush_privileged_tasks(timeout_seconds=2.0)

    async def test_duplicate_key_is_accepted_without_second_execution(self) -> None:
        release = asyncio.Event()
        started = asyncio.Event()
        calls = 0

        async def operation() -> None:
            nonlocal calls
            calls += 1
            started.set()
            await release.wait()

        first = submit_privileged_task(
            key="same",
            label="first",
            operation=operation,
        )
        second = submit_privileged_task(
            key="same",
            label="second",
            operation=operation,
        )
        self.assertTrue(first.accepted)
        self.assertTrue(first.created)
        self.assertTrue(second.accepted)
        self.assertFalse(second.created)
        self.assertEqual(first.job_id, second.job_id)
        await asyncio.wait_for(started.wait(), timeout=0.25)
        self.assertEqual(calls, 1)
        release.set()
        await flush_privileged_tasks(timeout_seconds=2.0)

    async def test_queue_reaper_finishes_expired_receipt_without_worker_slot(self) -> None:
        release = asyncio.Event()
        all_started = asyncio.Event()
        started = 0

        async def blocker() -> None:
            nonlocal started
            started += 1
            if started == 4:
                all_started.set()
            await release.wait()

        for index in range(4):
            submit_privileged_task(
                key=f"reaper-blocker-{index}",
                label="reaper blocker",
                operation=blocker,
                lane="critical",
            )
        await asyncio.wait_for(all_started.wait(), timeout=0.5)

        receipt = UpdateCompletionReceipt()
        token = bind_update_completion(receipt)
        try:
            submission = submit_privileged_task(
                key="expires-before-worker",
                label="expires before worker",
                operation=lambda: asyncio.sleep(0),
                lane="critical",
                timeout_seconds=0.1,
            )
        finally:
            reset_update_completion(token)

        self.assertTrue(submission.accepted)
        self.assertFalse(await asyncio.wait_for(receipt.wait(), timeout=1.0))
        release.set()
        await flush_privileged_tasks(timeout_seconds=2.0)


class PrivilegedAdminTests(unittest.IsolatedAsyncioTestCase):
    def test_ban_result_only_adds_collapsed_details_for_errors(self) -> None:
        success = admin._render_ban_result("本群解封完成")
        failed = admin._render_ban_result(
            "本群解封未完成",
            errors=["Telegram <拒绝>"],
        )

        self.assertEqual(success, "<b>本群解封完成</b>")
        self.assertNotIn("<blockquote", success)
        self.assertIn("<blockquote expandable>", failed)
        self.assertIn("Telegram &lt;拒绝&gt;", failed)

    async def test_compact_privileged_result_omits_generic_task_status(self) -> None:
        message = SimpleNamespace(
            edit_text=AsyncMock(),
            answer=AsyncMock(),
        )

        await admin._publish_privileged_result(
            message,
            "<b>本群封禁完成</b>",
            status="completed",
            default_title="本群封禁",
            compact=True,
        )

        text = message.edit_text.await_args.args[0]
        self.assertEqual(text, "<b>本群封禁完成</b>")
        self.assertNotIn("处理结果已返回", text)

    def test_task_result_status_does_not_depend_on_body_words(self) -> None:
        completed = admin._render_task_result(
            "<b>规则添加成功</b>\n<b>规则内容</b>: 禁止伪造支付失败截图",
            status="completed",
        )
        partial = admin._render_task_result(
            "<b>全局封禁完成</b>\n<b>未完成分类</b>: 网络错误 1\n失败群可重新提交",
            status="completed",
        )
        failed = admin._render_task_result(
            "<b>规则任务已取消</b>\n操作者权限已变化。",
            status="failed",
        )

        self.assertIn("处理结果已返回", completed)
        self.assertNotIn("后台处理未完成", completed)
        self.assertIn("请查看下方处理结果", partial)
        self.assertNotIn("无需进一步操作", partial)
        self.assertIn("后台处理未完成", failed)
        self.assertIn("请按下方说明重试", failed)

    async def test_group_fanout_never_exceeds_four_calls(self) -> None:
        active = 0
        maximum = 0

        async def operation(_group_id: int) -> bool:
            nonlocal active, maximum
            active += 1
            maximum = max(maximum, active)
            await asyncio.sleep(0.01)
            active -= 1
            return True

        outcomes = await admin._run_group_actions_bounded(
            list(range(20)),
            operation,
        )
        self.assertEqual(maximum, 4)
        self.assertTrue(all(item.succeeded for item in outcomes))

    async def test_global_ban_uses_authorized_groups_only(self) -> None:
        calls: list[int] = []
        recoveries = tuple(
            UnbanRecovery(
                verification_id=index,
                group_id=group_id,
                user_id=77,
                lease_until=datetime(2026, 1, 1),
            )
            for index, group_id in enumerate((-101, -102, -103), start=1)
        )

        async def ban_member(_bot, group_id: int, _target_id: int) -> bool:
            calls.append(group_id)
            return True

        message = SimpleNamespace(
            bot=SimpleNamespace(),
            edit_text=AsyncMock(),
            answer=AsyncMock(),
        )
        with (
            patch("bot.handlers.admin.add_global_ban", new=AsyncMock(return_value=True)),
            patch(
                "bot.handlers.admin.lease_join_verifications_for_user_unban",
                new=AsyncMock(return_value=recoveries),
            ),
            patch(
                "bot.handlers.admin._authorized_group_ids",
                new=AsyncMock(return_value=[-101, -102, -103]),
            ),
            patch("bot.handlers.admin.delete_verification_prompts", new=AsyncMock()),
            patch(
                "bot.handlers.admin.is_globally_banned",
                new=AsyncMock(return_value=True),
            ),
            patch("bot.handlers.admin.ban_member", new=ban_member),
            patch(
                "bot.handlers.admin.complete_leased_join_verification",
                new=AsyncMock(return_value=True),
            ),
        ):
            text = await admin._perform_global_ban(
                message,
                _FakeSessionFactory(),
                target_id=77,
                reason="test",
                operator_id=1,
            )

        self.assertEqual(calls, [-101, -102, -103])
        self.assertEqual(text, "<b>全局封禁完成</b>")
        self.assertNotIn("<blockquote", text)

    async def test_global_unban_completes_successful_recovery_journals(self) -> None:
        recoveries = tuple(
            UnbanRecovery(
                verification_id=index,
                group_id=group_id,
                user_id=77,
                lease_until=datetime(2026, 1, 1),
            )
            for index, group_id in enumerate((-101, -102), start=1)
        )
        unbanned: list[int] = []
        completed: list[int] = []

        async def unban_member(_bot, group_id: int, _target_id: int, **_kwargs) -> bool:
            unbanned.append(group_id)
            return True

        async def complete(_session, *, verification_id: int, **_kwargs) -> bool:
            completed.append(verification_id)
            return True

        message = SimpleNamespace(
            bot=SimpleNamespace(),
            edit_text=AsyncMock(),
            answer=AsyncMock(),
        )
        with (
            patch(
                "bot.handlers.admin._authorized_group_ids",
                new=AsyncMock(return_value=[-101, -102]),
            ),
            patch(
                "bot.handlers.admin.lease_join_verifications_for_user_unban",
                new=AsyncMock(return_value=recoveries),
            ),
            patch("bot.handlers.admin.remove_global_ban", new=AsyncMock(return_value=True)),
            patch("bot.handlers.admin._clear_user_warning", new=AsyncMock(return_value=(0, False))),
            patch("bot.handlers.admin.delete_verification_prompts", new=AsyncMock()),
            patch("bot.handlers.admin.unban_member", new=unban_member),
            patch(
                "bot.handlers.admin.restore_member_permissions",
                new=AsyncMock(return_value=True),
            ),
            patch(
                "bot.handlers.admin.verification_release_blocked_by_ban",
                new=AsyncMock(return_value=False),
            ),
            patch("bot.handlers.admin.complete_leased_join_verification", new=complete),
        ):
            text = await admin._perform_global_unban(
                message,
                _FakeSessionFactory(),
                target_id=77,
                operator_id=1,
            )

        self.assertEqual(unbanned, [-101, -102])
        self.assertEqual(completed, [1, 2])
        self.assertEqual(text, "<b>全局解封完成</b>")
        self.assertNotIn("<blockquote", text)

    async def test_global_unban_collapses_permission_restore_errors(self) -> None:
        recoveries = (
            UnbanRecovery(
                verification_id=1,
                group_id=-101,
                user_id=77,
                lease_until=datetime(2026, 1, 1),
            ),
        )
        message = SimpleNamespace(
            bot=SimpleNamespace(),
            edit_text=AsyncMock(),
            answer=AsyncMock(),
        )
        with (
            patch(
                "bot.handlers.admin._authorized_group_ids",
                new=AsyncMock(return_value=[-101]),
            ),
            patch(
                "bot.handlers.admin.lease_join_verifications_for_user_unban",
                new=AsyncMock(return_value=recoveries),
            ),
            patch("bot.handlers.admin.remove_global_ban", new=AsyncMock(return_value=True)),
            patch("bot.handlers.admin._clear_user_warning", new=AsyncMock(return_value=(0, False))),
            patch("bot.handlers.admin.delete_verification_prompts", new=AsyncMock()),
            patch("bot.handlers.admin.unban_member", new=AsyncMock(return_value=True)),
            patch(
                "bot.handlers.admin.restore_member_permissions",
                new=AsyncMock(return_value=False),
            ),
            patch(
                "bot.handlers.admin.verification_release_blocked_by_ban",
                new=AsyncMock(return_value=False),
            ),
            patch(
                "bot.handlers.admin.complete_leased_join_verification",
                new=AsyncMock(),
            ),
        ):
            text = await admin._perform_global_unban(
                message,
                _FakeSessionFactory(),
                target_id=77,
                operator_id=1,
            )

        self.assertIn("<b>全局解封完成</b>", text)
        self.assertIn("<blockquote expandable>", text)
        self.assertIn("未确认发言权限恢复", text)
        self.assertNotIn("<b>发言权限恢复</b>", text)

    async def test_global_ban_reconciles_when_unban_policy_wins_race(self) -> None:
        message = SimpleNamespace(
            bot=SimpleNamespace(),
            edit_text=AsyncMock(),
            answer=AsyncMock(),
        )
        policy = AsyncMock(side_effect=[True, False])
        unban = AsyncMock(return_value=True)
        restore = AsyncMock(return_value=True)
        recoveries = (
            UnbanRecovery(
                verification_id=1,
                group_id=-101,
                user_id=77,
                lease_until=datetime(2026, 1, 1),
            ),
        )
        with (
            patch("bot.handlers.admin.add_global_ban", new=AsyncMock(return_value=True)),
            patch(
                "bot.handlers.admin.lease_join_verifications_for_user_unban",
                new=AsyncMock(return_value=recoveries),
            ),
            patch(
                "bot.handlers.admin._authorized_group_ids",
                new=AsyncMock(return_value=[-101]),
            ),
            patch("bot.handlers.admin.delete_verification_prompts", new=AsyncMock()),
            patch("bot.handlers.admin.is_globally_banned", new=policy),
            patch("bot.handlers.admin.ban_member", new=AsyncMock(return_value=True)),
            patch("bot.handlers.admin.unban_member", new=unban),
            patch("bot.handlers.admin.restore_member_permissions", new=restore),
            patch(
                "bot.handlers.admin.complete_leased_join_verification",
                new=AsyncMock(return_value=True),
            ),
        ):
            text = await admin._perform_global_ban(
                message,
                _FakeSessionFactory(),
                target_id=77,
                reason="test",
                operator_id=1,
            )

        unban.assert_awaited_once()
        restore.assert_awaited_once()
        self.assertIn("<b>全局封禁完成</b>", text)
        self.assertIn("<blockquote expandable>", text)
        self.assertIn("结果未确认 1", text)

    async def test_global_ban_deletes_target_only_when_origin_group_succeeds(
        self,
    ) -> None:
        async def run_case(
            outcomes: list[admin._GroupActionOutcome],
        ) -> AsyncMock:
            delete_message = AsyncMock(return_value=True)
            message = SimpleNamespace(
                chat=SimpleNamespace(id=-101, type="supergroup"),
                bot=SimpleNamespace(delete_message=delete_message),
            )
            with (
                patch(
                    "bot.handlers.admin.add_global_ban",
                    new=AsyncMock(return_value=True),
                ),
                patch(
                    "bot.handlers.admin.lease_join_verifications_for_user_unban",
                    new=AsyncMock(return_value=()),
                ),
                patch(
                    "bot.handlers.admin._authorized_group_ids",
                    new=AsyncMock(return_value=[-101, -102]),
                ),
                patch(
                    "bot.handlers.admin._run_group_actions_bounded",
                    new=AsyncMock(return_value=outcomes),
                ),
                patch(
                    "bot.handlers.admin._publish_privileged_progress",
                    new=AsyncMock(),
                ),
                patch(
                    "bot.handlers.admin.delete_verification_prompts",
                    new=AsyncMock(),
                ),
                patch(
                    "bot.handlers.admin.close_private_challenge_messages",
                    new=AsyncMock(),
                ),
            ):
                await admin._perform_global_ban_locked(
                    message,
                    _FakeSessionFactory(),
                    target_id=77,
                    reason="spam",
                    operator_id=1,
                    origin_group_id=-101,
                    target_message_id=654,
                )
            return delete_message

        origin_succeeded = await run_case(
            [
                admin._GroupActionOutcome(-101, True),
                admin._GroupActionOutcome(-102, False, "unconfirmed"),
            ]
        )
        origin_succeeded.assert_awaited_once_with(
            chat_id=-101,
            message_id=654,
        )

        origin_failed = await run_case(
            [
                admin._GroupActionOutcome(-101, False, "unconfirmed"),
                admin._GroupActionOutcome(-102, True),
            ]
        )
        origin_failed.assert_not_awaited()

    async def test_scope_token_is_retained_when_queue_rejects(self) -> None:
        settings = Settings(_env_file=None)
        settings.super_admin_id = 1
        message = SimpleNamespace(
            message_id=900,
            chat=SimpleNamespace(id=-100, type="supergroup"),
            edit_reply_markup=AsyncMock(),
        )
        callback = SimpleNamespace(
            data="bsc:b:g:77",
            from_user=SimpleNamespace(id=1, full_name="Root"),
            message=message,
            answer=AsyncMock(),
        )
        session = _FakeSession()
        key = (-100, 900)
        request = admin._BanScopeRequest(
            action="ban",
            target_id=77,
            reason="test",
            created_at=asyncio.get_running_loop().time(),
        )
        admin._BAN_SCOPE_REQUESTS[key] = request
        rejected = PrivilegedTaskSubmission(
            accepted=False,
            created=False,
            job_id="",
            lane="critical",
            queue_depth=64,
            reason="queue_full",
        )
        try:
            with patch("bot.handlers.admin.submit_privileged_task", return_value=rejected):
                await admin.on_ban_scope_choice(
                    callback,
                    session=session,
                    settings=settings,
                    session_factory=_FakeSessionFactory(),
                )
            self.assertIs(admin._BAN_SCOPE_REQUESTS.get(key), request)
            self.assertIn("队列正忙", callback.answer.await_args.args[0])
        finally:
            admin._BAN_SCOPE_REQUESTS.pop(key, None)


class PrivilegedMembershipTests(unittest.IsolatedAsyncioTestCase):
    async def test_missing_reconciliation_row_creates_durable_owner(self) -> None:
        session = _FakeSession()
        recovery = UnbanRecovery(
            verification_id=91,
            group_id=-100,
            user_id=77,
            lease_until=datetime(2026, 1, 1),
        )
        with (
            patch(
                "bot.handlers.membership.get_join_verification",
                new=AsyncMock(return_value=None),
            ),
            patch(
                "bot.handlers.membership.lease_join_verification_for_unban",
                new=AsyncMock(return_value=recovery),
            ) as lease,
        ):
            owned = await membership._ensure_moderation_recovery_owner(
                session,
                group_id=-100,
                user_id=77,
            )

        self.assertTrue(owned)
        lease.assert_awaited_once_with(
            session,
            -100,
            77,
            manual_unban=False,
        )
        session.commit.assert_awaited_once()

    async def test_nonretryable_reconciliation_uses_durable_owner(self) -> None:
        session = _FakeSession()
        with (
            patch(
                "bot.handlers.membership._complete_terminal_verification",
                new=AsyncMock(return_value=False),
            ),
            patch(
                "bot.handlers.membership.reconcile_moderation_ban_after_lost_lease_result",
                new=AsyncMock(
                    return_value=SimpleNamespace(ok=False, retryable=False)
                ),
            ),
            patch(
                "bot.handlers.membership._ensure_moderation_recovery_owner",
                new=AsyncMock(return_value=True),
            ) as ensure_owner,
        ):
            completed = (
                await membership._complete_moderation_enforcement_or_reconcile(
                    SimpleNamespace(),
                    session,
                    group_id=-100,
                    user_id=77,
                    verification_id=91,
                    lease_until=datetime(2026, 1, 1),
                )
            )

        self.assertFalse(completed)
        ensure_owner.assert_awaited_once_with(
            session,
            group_id=-100,
            user_id=77,
        )

    async def test_verification_admin_callback_acks_then_uses_critical_job(self) -> None:
        callback = SimpleNamespace(
            data=build_verification_callback_data(
                VERIFICATION_CALLBACK_APPROVE,
                77,
            ),
            from_user=SimpleNamespace(id=1),
            message=SimpleNamespace(
                message_id=701,
                chat=SimpleNamespace(id=-100, type="supergroup"),
                answer=AsyncMock(),
            ),
            bot=SimpleNamespace(),
            answer=AsyncMock(),
        )
        accepted = PrivilegedTaskSubmission(
            accepted=True,
            created=True,
            job_id="critical-jv-1",
            lane="critical",
            queue_depth=1,
        )
        session_factory = _FakeSessionFactory()
        handler = AsyncMock()
        session = _FakeSession()
        with (
            patch(
                "bot.handlers.membership.is_group_authorized",
                new=AsyncMock(return_value=True),
            ),
            patch(
                "bot.handlers.membership.is_group_admin_or_higher",
                new=AsyncMock(return_value=True),
            ),
            patch(
                "bot.handlers.membership._handle_verification_admin_callback",
                new=handler,
            ),
            patch(
                "bot.handlers.membership.submit_privileged_task",
                return_value=accepted,
            ) as submit,
        ):
            await membership.on_verification_callback(
                callback,
                session=session,
                settings=Settings(_env_file=None),
                session_factory=session_factory,
            )

            self.assertIn("正在验证权限", callback.answer.await_args_list[0].args[0])
            handler.assert_not_awaited()
            self.assertEqual(submit.call_args.kwargs["lane"], "critical")
            self.assertEqual(
                submit.call_args.kwargs["key"],
                "verification-admin:-100:701:77",
            )
            await submit.call_args.kwargs["operation"]()

        handler.assert_awaited_once()
        self.assertIs(
            handler.await_args.kwargs["session_factory"],
            session_factory,
        )

    async def test_verification_admin_denial_is_private_callback_alert(self) -> None:
        callback = SimpleNamespace(
            data=build_verification_callback_data(
                VERIFICATION_CALLBACK_APPROVE,
                77,
            ),
            from_user=SimpleNamespace(id=9),
            message=SimpleNamespace(
                message_id=701,
                chat=SimpleNamespace(id=-100, type="supergroup"),
                answer=AsyncMock(),
            ),
            bot=SimpleNamespace(send_message=AsyncMock()),
            answer=AsyncMock(),
        )
        handler = AsyncMock()
        with (
            patch(
                "bot.handlers.membership.is_group_authorized",
                new=AsyncMock(return_value=True),
            ),
            patch(
                "bot.handlers.membership.is_group_admin_or_higher",
                new=AsyncMock(return_value=False),
            ),
            patch(
                "bot.handlers.membership._handle_verification_admin_callback",
                new=handler,
            ),
            patch("bot.handlers.membership.submit_privileged_task") as submit,
        ):
            await membership.on_verification_callback(
                callback,
                session=_FakeSession(),
                settings=Settings(_env_file=None),
                session_factory=_FakeSessionFactory(),
            )

        callback.answer.assert_awaited_once_with(
            "仅群管理员及以上权限可操作",
            show_alert=True,
        )
        callback.message.answer.assert_not_awaited()
        callback.bot.send_message.assert_not_awaited()
        submit.assert_not_called()
        handler.assert_not_awaited()

    async def test_raid_remove_callback_acks_before_background_submission(self) -> None:
        callback = SimpleNamespace(
            from_user=SimpleNamespace(id=1),
            message=SimpleNamespace(
                message_id=700,
                chat=SimpleNamespace(id=-100, type="supergroup"),
                answer=AsyncMock(),
            ),
            bot=SimpleNamespace(),
            answer=AsyncMock(),
        )
        accepted = PrivilegedTaskSubmission(
            accepted=True,
            created=True,
            job_id="critical-1",
            lane="critical",
            queue_depth=1,
        )
        session = _FakeSession()
        with (
            patch(
                "bot.handlers.membership.is_group_authorized",
                new=AsyncMock(return_value=True),
            ),
            patch(
                "bot.handlers.membership.is_group_admin_or_higher",
                new=AsyncMock(return_value=True),
            ),
            patch(
                "bot.handlers.membership.remove_raid_challenged_users",
                new=AsyncMock(),
            ) as remove,
            patch(
                "bot.handlers.membership.submit_privileged_task",
                return_value=accepted,
            ) as submit,
        ):
            await membership.on_raid_remove_callback(
                callback,
                session=session,
                settings=Settings(_env_file=None),
                session_factory=_FakeSessionFactory(),
            )

        self.assertIn("正在验证权限", callback.answer.await_args_list[0].args[0])
        remove.assert_not_awaited()
        self.assertEqual(submit.call_args.kwargs["lane"], "critical_bulk")

    async def test_raid_remove_denial_is_private_callback_alert(self) -> None:
        callback = SimpleNamespace(
            from_user=SimpleNamespace(id=9),
            message=SimpleNamespace(
                message_id=700,
                chat=SimpleNamespace(id=-100, type="supergroup"),
                answer=AsyncMock(),
            ),
            bot=SimpleNamespace(send_message=AsyncMock()),
            answer=AsyncMock(),
        )
        with (
            patch(
                "bot.handlers.membership.is_group_authorized",
                new=AsyncMock(return_value=True),
            ),
            patch(
                "bot.handlers.membership.is_group_admin_or_higher",
                new=AsyncMock(return_value=False),
            ),
            patch(
                "bot.handlers.membership.remove_raid_challenged_users",
                new=AsyncMock(),
            ) as remove,
            patch("bot.handlers.membership.submit_privileged_task") as submit,
        ):
            await membership.on_raid_remove_callback(
                callback,
                session=_FakeSession(),
                settings=Settings(_env_file=None),
                session_factory=_FakeSessionFactory(),
            )

        callback.answer.assert_awaited_once_with(
            "仅群管理员可一键移除追溯用户",
            show_alert=True,
        )
        callback.message.answer.assert_not_awaited()
        callback.bot.send_message.assert_not_awaited()
        submit.assert_not_called()
        remove.assert_not_awaited()

    async def test_queued_raid_remove_revalidates_operator_before_execution(self) -> None:
        callback = SimpleNamespace(
            from_user=SimpleNamespace(id=1),
            message=SimpleNamespace(
                message_id=702,
                chat=SimpleNamespace(id=-100, type="supergroup"),
                answer=AsyncMock(),
            ),
            bot=SimpleNamespace(),
            answer=AsyncMock(),
        )
        accepted = PrivilegedTaskSubmission(
            accepted=True,
            created=True,
            job_id="critical-raid-2",
            lane="critical",
            queue_depth=1,
        )
        authorization = AsyncMock(side_effect=[True, False])
        operator_auth = AsyncMock(return_value=True)
        remove = AsyncMock()
        with (
            patch(
                "bot.handlers.membership.is_group_authorized",
                new=authorization,
            ),
            patch(
                "bot.handlers.membership.is_group_admin_or_higher",
                new=operator_auth,
            ),
            patch(
                "bot.handlers.membership.remove_raid_challenged_users",
                new=remove,
            ),
            patch(
                "bot.handlers.membership.submit_privileged_task",
                return_value=accepted,
            ) as submit,
        ):
            await membership.on_raid_remove_callback(
                callback,
                session=_FakeSession(),
                settings=Settings(_env_file=None),
                session_factory=_FakeSessionFactory(),
            )
            await submit.call_args.kwargs["operation"]()

        remove.assert_not_awaited()
        operator_auth.assert_awaited_once()
        callback.message.answer.assert_not_awaited()

    async def test_member_join_uses_reserved_security_lane(self) -> None:
        event = SimpleNamespace(
            chat=SimpleNamespace(id=-100, type="supergroup"),
            new_chat_member=SimpleNamespace(
                user=SimpleNamespace(id=77, is_bot=False),
            ),
        )
        accepted = PrivilegedTaskSubmission(
            accepted=True,
            created=True,
            job_id="security-1",
            lane="security",
            queue_depth=1,
        )
        session = _FakeSession()
        with (
            patch("bot.handlers.membership._process_member_join", new=AsyncMock()) as process,
            patch(
                "bot.handlers.membership.submit_privileged_task",
                return_value=accepted,
            ) as submit,
        ):
            await membership.on_member_join(
                event,
                session=session,
                settings=Settings(_env_file=None),
                session_factory=_FakeSessionFactory(),
            )

            process.assert_not_awaited()
            self.assertEqual(submit.call_args.kwargs["lane"], "security")
            await submit.call_args.kwargs["operation"]()

        process.assert_awaited_once()
        self.assertTrue(process.await_args.kwargs["require_current_membership"])

    async def test_new_join_update_reruns_latest_generation_before_completion(self) -> None:
        first_started = asyncio.Event()
        release_first = asyncio.Event()
        second_finished = asyncio.Event()
        processed: list[int] = []

        async def process(event, *_args, **_kwargs) -> None:
            processed.append(int(event.generation))
            if int(event.generation) == 1:
                first_started.set()
                await release_first.wait()
            else:
                second_finished.set()

        def join_event(generation: int) -> SimpleNamespace:
            return SimpleNamespace(
                generation=generation,
                chat=SimpleNamespace(id=-100, type="supergroup"),
                new_chat_member=SimpleNamespace(
                    user=SimpleNamespace(id=77, is_bot=False),
                ),
            )

        factory = _FakeSessionFactory()
        settings = Settings(_env_file=None)
        try:
            with patch(
                "bot.handlers.membership._process_member_join",
                new=process,
            ):
                await membership.on_member_join(
                    join_event(1),
                    session=_FakeSession(),
                    settings=settings,
                    session_factory=factory,
                    event_update=SimpleNamespace(update_id=1001),
                )
                await asyncio.wait_for(first_started.wait(), timeout=0.5)
                await membership.on_member_join(
                    join_event(2),
                    session=_FakeSession(),
                    settings=settings,
                    session_factory=factory,
                    event_update=SimpleNamespace(update_id=1002),
                )
                release_first.set()
                await asyncio.wait_for(second_finished.wait(), timeout=0.5)
            self.assertEqual(processed, [1, 2])
        finally:
            release_first.set()
            await flush_privileged_tasks(timeout_seconds=2.0)
            membership._PENDING_MEMBER_JOIN_SECURITY.clear()


class PrivilegedModerationTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def _callback() -> SimpleNamespace:
        return SimpleNamespace(
            data="mact:ban:12",
            from_user=SimpleNamespace(id=9),
            message=SimpleNamespace(
                chat=SimpleNamespace(id=-100, type="supergroup"),
                answer=AsyncMock(),
            ),
            bot=SimpleNamespace(send_message=AsyncMock()),
            answer=AsyncMock(),
        )

    async def test_untrusted_moderation_callback_cannot_allocate_critical_job(self) -> None:
        callback = self._callback()
        with (
            patch(
                "bot.handlers.group.is_group_authorized",
                new=AsyncMock(return_value=True),
            ),
            patch(
                "bot.handlers.group.is_group_admin_or_higher",
                new=AsyncMock(return_value=False),
            ),
            patch("bot.handlers.group.submit_privileged_task") as submit,
        ):
            await group.on_moderation_action(
                callback,
                settings=Settings(_env_file=None),
                session=_FakeSession(),
                session_factory=_FakeSessionFactory(),
            )

        submit.assert_not_called()
        callback.answer.assert_awaited_once_with(
            "仅群管理员及以上权限可执行该操作",
            show_alert=True,
        )
        callback.message.answer.assert_not_awaited()
        callback.bot.send_message.assert_not_awaited()

    async def test_authorized_moderation_callback_uses_critical_job(self) -> None:
        callback = self._callback()
        session = _FakeSession()
        session.get = AsyncMock(
            return_value=SimpleNamespace(group_id=-100, user_id=77)
        )
        accepted = PrivilegedTaskSubmission(
            accepted=True,
            created=True,
            job_id="critical-moderation-1",
            lane="critical",
            queue_depth=1,
        )
        with (
            patch(
                "bot.handlers.group.is_group_authorized",
                new=AsyncMock(return_value=True),
            ),
            patch(
                "bot.handlers.group.is_group_admin_or_higher",
                new=AsyncMock(return_value=True),
            ),
            patch(
                "bot.handlers.group.submit_privileged_task",
                return_value=accepted,
            ) as submit,
        ):
            await group.on_moderation_action(
                callback,
                settings=Settings(_env_file=None),
                session=session,
                session_factory=_FakeSessionFactory(),
            )

        self.assertEqual(submit.call_args.kwargs["lane"], "critical")
        self.assertEqual(
            submit.call_args.kwargs["key"],
            "moderation-action:-100:77",
        )


class PrivilegedSettingsAPITests(unittest.IsolatedAsyncioTestCase):
    async def test_web_group_fanout_is_bounded_and_marked_critical(self) -> None:
        active = 0
        maximum = 0
        priorities: list[ExecutionPriority] = []

        async def operation(_group_id: int) -> bool:
            nonlocal active, maximum
            active += 1
            maximum = max(maximum, active)
            priorities.append(current_execution_priority())
            await asyncio.sleep(0.01)
            active -= 1
            return True

        outcomes = await settings_api._run_privileged_group_fanout(
            list(range(18)),
            operation,
        )
        self.assertEqual(maximum, 4)
        self.assertTrue(all(outcomes))
        self.assertEqual(set(priorities), {ExecutionPriority.CRITICAL})


if __name__ == "__main__":
    unittest.main()
