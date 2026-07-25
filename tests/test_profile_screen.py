import asyncio
import os
import tempfile
import unittest
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from sqlalchemy import select

from bot.config import ModerationConfig, Settings
from bot.db.engine import init_db
from bot.db.models import ModerationRule, UserWarning
from bot.middlewares.profile_screen import ProfileScreenEnforcementMiddleware
from bot.services.join_screening import (
    get_global_ban,
    moderation_rules_fingerprint,
    profile_screen_signature,
)
from bot.services.moderation import ModerationService
from bot.services.request_priority import (
    ExecutionPriority,
    execution_priority_scope,
)
from bot.services.join_verification import BanEnforcementResult


class _NoAutoflush:
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False


class _SessionContext:
    def __init__(self, session: object) -> None:
        self.session = session

    async def __aenter__(self) -> object:
        return self.session

    async def __aexit__(self, exc_type, exc, traceback) -> bool:
        return False


class _RowsResult:
    def __init__(self, rows: list[object]) -> None:
        self.rows = rows

    def scalars(self) -> "_RowsResult":
        return self

    def all(self) -> list[object]:
        return self.rows


def _settings() -> Settings:
    settings = Settings(_env_file=None)
    settings.moderation.enabled = True
    return settings


def _event(**message_fields: object) -> SimpleNamespace:
    values = {
        "chat": SimpleNamespace(id=-100, type="supergroup", ban=AsyncMock()),
        "from_user": SimpleNamespace(
            id=42,
            is_bot=False,
            full_name="普通用户",
            username="member42",
        ),
        "sender_chat": None,
        "delete": AsyncMock(),
        "bot": SimpleNamespace(
            ban_chat_member=AsyncMock(return_value=True),
            get_chat_member=AsyncMock(return_value=SimpleNamespace(status="kicked")),
        ),
        "text": "hello",
    }
    values.update(message_fields)
    return SimpleNamespace(**values)


async def _wait_for_screening_start(
    testcase: unittest.IsolatedAsyncioTestCase,
    task: asyncio.Task[object],
    entered: asyncio.Event,
) -> None:
    waiter = asyncio.create_task(entered.wait())
    done, _pending = await asyncio.wait(
        {task, waiter},
        timeout=5,
        return_when=asyncio.FIRST_COMPLETED,
    )
    if waiter in done:
        return

    waiter.cancel()
    if task in done:
        testcase.fail(f"message completed before profile screening: {task.result()!r}")
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    testcase.fail("profile screening did not start within 5 seconds")


async def _gather_message_tasks(
    testcase: unittest.IsolatedAsyncioTestCase,
    tasks: list[asyncio.Task[object]],
) -> list[object]:
    done, pending = await asyncio.wait(tasks, timeout=5)
    if pending:
        locations = []
        for task in pending:
            stack = task.get_stack()
            if stack:
                frame = stack[-1]
                locations.append(f"{frame.f_code.co_filename}:{frame.f_lineno}")
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
        testcase.fail(f"concurrent messages did not finish: {locations}")
    return [task.result() for task in tasks]


async def _wait_for_gate_users(
    testcase: unittest.IsolatedAsyncioTestCase,
    middleware: ProfileScreenEnforcementMiddleware,
    key: tuple[int, int],
    expected_users: int,
    *,
    release: asyncio.Event,
    tasks: list[asyncio.Task[object]],
) -> None:
    async def wait_until_ready() -> None:
        while True:
            gate = middleware._screening_gates.get(key)
            if gate is not None and gate.users >= expected_users:
                return
            await asyncio.sleep(0.01)

    try:
        await asyncio.wait_for(wait_until_ready(), timeout=5)
    except TimeoutError:
        release.set()
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        testcase.fail("second message did not queue behind the profile screening gate")


class ModerationRuleFingerprintTests(unittest.IsolatedAsyncioTestCase):
    async def test_new_enabled_rule_changes_profile_screen_signature(self) -> None:
        session = SimpleNamespace(
            no_autoflush=_NoAutoflush(),
            execute=AsyncMock(
                side_effect=[
                    _RowsResult([]),
                    _RowsResult([(7, "llm", "禁止广告", "ban")]),
                ]
            ),
        )

        before = await moderation_rules_fingerprint(session, -100)
        after = await moderation_rules_fingerprint(session, -100)

        self.assertNotEqual(before, after)
        self.assertNotEqual(
            profile_screen_signature(
                full_name="普通用户",
                username="member42",
                rules_fingerprint=before,
            ),
            profile_screen_signature(
                full_name="普通用户",
                username="member42",
                rules_fingerprint=after,
            ),
        )

    async def test_unparseable_moderation_result_is_inconclusive(self) -> None:
        rule = ModerationRule(
            id=7,
            group_id=-100,
            rule_type="llm",
            pattern="禁止广告",
            action="ban",
            enabled=True,
        )
        session = SimpleNamespace(
            no_autoflush=_NoAutoflush(),
            execute=AsyncMock(return_value=_RowsResult([rule])),
        )
        llm = SimpleNamespace(moderation=AsyncMock(return_value="not valid json"))
        service = ModerationService(ModerationConfig(), llm)

        violated, reason, hit_rule, conclusive = await service.check_rules_verbose(
            session,
            -100,
            "[成员资料审核] 普通用户",
        )

        self.assertFalse(violated)
        self.assertEqual(reason, "")
        self.assertIsNone(hit_rule)
        self.assertFalse(conclusive)


class ProfileScreenMiddlewareTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        # Other security suites intentionally publish a short-lived in-process
        # /unban generation for this same fixture user. Profile middleware
        # tests must not inherit that cross-test process state.
        unban_generation_patcher = patch(
            "bot.middlewares.profile_screen.manual_unban_generation_is_active",
            return_value=False,
        )
        unban_generation_patcher.start()
        self.addCleanup(unban_generation_patcher.stop)
        self.session = SimpleNamespace(
            commit=AsyncMock(),
            rollback=AsyncMock(),
            execute=AsyncMock(),
            scalar=AsyncMock(return_value=None),
            get=AsyncMock(return_value=None),
            no_autoflush=_NoAutoflush(),
        )
        self.middleware = ProfileScreenEnforcementMiddleware(
            lambda: _SessionContext(self.session)
        )
        self.handler = AsyncMock(return_value="handled")
        self.moderation = SimpleNamespace(is_user_exempt=AsyncMock(return_value=False))
        lease_patcher = patch(
            "bot.middlewares.profile_screen.lease_join_verification_for_unban",
            new=AsyncMock(
                return_value=SimpleNamespace(
                    verification_id=1,
                    lease_until=datetime(2026, 1, 1),
                )
            ),
        )
        completion_patcher = patch(
            "bot.middlewares.profile_screen.complete_leased_join_verification",
            new=AsyncMock(return_value=True),
        )
        local_ban_patcher = patch(
            "bot.middlewares.profile_screen.mark_profile_screening_group_ban",
            new=AsyncMock(return_value=None),
        )
        verification_state_patcher = patch(
            "bot.middlewares.profile_screen.get_join_verification",
            new=AsyncMock(return_value=None),
        )
        lease_patcher.start()
        completion_patcher.start()
        local_ban_patcher.start()
        verification_state_patcher.start()
        self.addCleanup(lease_patcher.stop)
        self.addCleanup(completion_patcher.stop)
        self.addCleanup(local_ban_patcher.stop)
        self.addCleanup(verification_state_patcher.stop)

    def _patch_screening(self, *, verdict: tuple[bool, str, bool] = (False, "", True)):
        return (
            patch(
                "bot.middlewares.profile_screen.is_group_authorized",
                new=AsyncMock(return_value=True),
            ),
            patch(
                "bot.middlewares.profile_screen.moderation_rules_fingerprint",
                new=AsyncMock(return_value="rules-v2"),
            ),
            patch(
                "bot.middlewares.profile_screen.get_global_ban",
                new=AsyncMock(return_value=None),
            ),
            patch(
                "bot.middlewares.profile_screen.get_profile_screen_hash",
                new=AsyncMock(return_value=""),
            ),
            patch(
                "bot.middlewares.profile_screen.is_user_admin_cached",
                new=AsyncMock(return_value=False),
            ),
            patch(
                "bot.middlewares.profile_screen.ModerationService",
                return_value=self.moderation,
            ),
            patch("bot.middlewares.profile_screen._build_llm", return_value=object()),
            patch(
                "bot.middlewares.profile_screen.screen_member_profile_verbose",
                new=AsyncMock(return_value=verdict),
            ),
            patch(
                "bot.middlewares.profile_screen.mark_profile_screened",
                new=AsyncMock(),
            ),
        )

    async def test_commands_videos_and_empty_text_all_reach_profile_screening(self) -> None:
        patches = self._patch_screening()
        with (
            patches[0],
            patches[1],
            patches[2],
            patches[3],
            patches[4],
            patches[5],
            patches[6],
            patches[7] as screen,
            patches[8],
        ):
            events = [
                _event(text="/help"),
                _event(text=None, video=SimpleNamespace(file_id="video")),
                _event(text=None, document=SimpleNamespace(file_id="document")),
            ]
            for event in events:
                result = await self.middleware(
                    self.handler,
                    event,
                    {"settings": _settings()},
                )
                self.assertEqual(result, "handled")

        self.assertEqual(screen.await_count, 3)
        self.assertEqual(self.handler.await_count, 3)

    async def test_privileged_execution_bypasses_profile_screening(self) -> None:
        screen = AsyncMock()
        with patch(
            "bot.middlewares.profile_screen.screen_member_profile_verbose",
            new=screen,
        ):
            with execution_priority_scope(ExecutionPriority.HIGH):
                first = await self.middleware(
                    self.handler,
                    _event(text="/ban 7", message_id=1),
                    {"settings": _settings()},
                )
                second = await self.middleware(
                    self.handler,
                    _event(text="/ban 7", message_id=2),
                    {"settings": _settings()},
                )

        self.assertEqual((first, second), ("handled", "handled"))
        screen.assert_not_awaited()
        self.assertEqual(self.handler.await_count, 2)

    async def test_raw_management_candidate_never_runs_profile_llm(self) -> None:
        screen = AsyncMock()
        with patch(
            "bot.middlewares.profile_screen.screen_member_profile_verbose",
            new=screen,
        ):
            result = await self.middleware(
                self.handler,
                _event(text="/ban 7", message_id=3),
                {"settings": _settings()},
            )

        self.assertEqual(result, "handled")
        screen.assert_not_awaited()

    async def test_inconclusive_screening_is_not_cached(self) -> None:
        patches = self._patch_screening(verdict=(False, "", False))
        with (
            patches[0],
            patches[1],
            patches[2],
            patches[3],
            patches[4],
            patches[5],
            patches[6],
            patches[7] as screen,
            patches[8] as mark_screened,
        ):
            event = _event(text="hello")
            await self.middleware(self.handler, event, {"settings": _settings()})
            await self.middleware(self.handler, event, {"settings": _settings()})

        self.assertEqual(screen.await_count, 2)
        mark_screened.assert_not_awaited()
        self.session.commit.assert_not_awaited()

    async def test_changed_rules_bypass_old_profile_cache(self) -> None:
        old_signature = profile_screen_signature(
            full_name="普通用户",
            username="member42",
            rules_fingerprint="rules-v1",
        )
        patches = self._patch_screening()
        patches = list(patches)
        patches[3] = patch(
            "bot.middlewares.profile_screen.get_profile_screen_hash",
            new=AsyncMock(return_value=old_signature),
        )

        with (
            patches[0],
            patches[1],
            patches[2],
            patches[3],
            patches[4],
            patches[5],
            patches[6],
            patches[7] as screen,
            patches[8] as mark_screened,
        ):
            result = await self.middleware(
                self.handler,
                _event(),
                {"settings": _settings()},
            )

        self.assertEqual(result, "handled")
        screen.assert_awaited_once()
        mark_screened.assert_awaited_once()

    async def test_local_group_ban_fast_path_blocks_without_rescreening(self) -> None:
        patches = self._patch_screening()
        self.session.scalar.return_value = 1
        event = _event()
        send_notice = AsyncMock()
        with (
            patches[0],
            patches[1],
            patches[2],
            patches[3],
            patches[4],
            patches[5],
            patches[6],
            patches[7] as screen,
            patches[8],
            patch(
                "bot.middlewares.profile_screen.enforce_ban_with_policy_reconciliation_result",
                new=AsyncMock(return_value=BanEnforcementResult(final_banned=True)),
            ),
            patch(
                "bot.middlewares.profile_screen.answer_with_auto_delete",
                new=send_notice,
            ),
        ):
            result = await self.middleware(
                self.handler,
                event,
                {"settings": _settings()},
            )

        self.assertIsNone(result)
        screen.assert_not_awaited()
        self.handler.assert_not_awaited()
        send_notice.assert_awaited_once()
        event.delete.assert_awaited_once()

    async def test_deauthorization_during_llm_discards_profile_ban_verdict(self) -> None:
        patches = list(self._patch_screening(verdict=(True, "广告昵称", True)))
        patches[0] = patch(
            "bot.middlewares.profile_screen.is_group_authorized",
            new=AsyncMock(side_effect=[True, True, False]),
        )
        add_ban = AsyncMock()
        with (
            patches[0],
            patches[1],
            patches[2],
            patches[3],
            patches[4],
            patches[5],
            patches[6],
            patches[7],
            patches[8],
            patch(
                "bot.middlewares.profile_screen.mark_profile_screening_group_ban",
                new=add_ban,
            ),
        ):
            result = await self.middleware(
                self.handler,
                _event(),
                {"settings": _settings()},
            )

        self.assertEqual(result, "handled")
        add_ban.assert_not_awaited()
        self.handler.assert_awaited_once()

    async def test_manual_unban_during_llm_discards_stale_profile_ban(self) -> None:
        patches = list(self._patch_screening(verdict=(True, "广告昵称", True)))
        add_ban = AsyncMock()
        with (
            patches[0],
            patches[1],
            patches[2],
            patches[3],
            patches[4],
            patches[5],
            patches[6],
            patches[7],
            patches[8],
            patch(
                "bot.middlewares.profile_screen.manual_unban_generation_is_active",
                return_value=True,
            ),
            patch(
                "bot.middlewares.profile_screen.mark_profile_screening_group_ban",
                new=add_ban,
            ),
        ):
            result = await self.middleware(
                self.handler,
                _event(),
                {"settings": _settings()},
            )

        self.assertEqual(result, "handled")
        add_ban.assert_not_awaited()
        self.handler.assert_awaited_once()

    async def test_persistent_unban_recovery_discards_stale_profile_ban(self) -> None:
        patches = list(self._patch_screening(verdict=(True, "广告昵称", True)))
        add_ban = AsyncMock()
        with (
            patches[0],
            patches[1],
            patches[2],
            patches[3],
            patches[4],
            patches[5],
            patches[6],
            patches[7],
            patches[8],
            patch(
                "bot.middlewares.profile_screen.get_join_verification",
                new=AsyncMock(return_value=SimpleNamespace(status="unbanning")),
            ),
            patch(
                "bot.middlewares.profile_screen.mark_profile_screening_group_ban",
                new=add_ban,
            ),
        ):
            result = await self.middleware(
                self.handler,
                _event(),
                {"settings": _settings()},
            )

        self.assertEqual(result, "handled")
        add_ban.assert_not_awaited()
        self.handler.assert_awaited_once()

    async def test_new_verification_restriction_keeps_message_blocked(self) -> None:
        patches = self._patch_screening(verdict=(True, "广告昵称", True))
        event = _event()
        with (
            patches[0],
            patches[1],
            patches[2],
            patches[3],
            patches[4],
            patches[5],
            patches[6],
            patches[7],
            patches[8],
            patch(
                "bot.middlewares.profile_screen.mark_profile_screening_group_ban",
                new=AsyncMock(),
            ),
            patch(
                "bot.middlewares.profile_screen.enforce_ban_with_policy_reconciliation_result",
                new=AsyncMock(
                    return_value=BanEnforcementResult(
                        final_banned=False,
                        final_restricted=True,
                    )
                ),
            ),
        ):
            result = await self.middleware(
                self.handler,
                event,
                {"settings": _settings()},
            )

        self.assertIsNone(result)
        self.handler.assert_not_awaited()
        event.delete.assert_awaited_once()

    async def test_profile_ban_notice_uses_expandable_audit_details(self) -> None:
        patches = self._patch_screening(verdict=(True, "广告昵称 <危险>", True))
        event = _event()
        send_notice = AsyncMock()
        with (
            patches[0],
            patches[1],
            patches[2],
            patches[3],
            patches[4],
            patches[5],
            patches[6],
            patches[7],
            patches[8],
            patch(
                "bot.middlewares.profile_screen.mark_profile_screening_group_ban",
                new=AsyncMock(),
            ),
            patch(
                "bot.middlewares.profile_screen.enforce_ban_with_policy_reconciliation_result",
                new=AsyncMock(return_value=BanEnforcementResult(final_banned=True)),
            ),
            patch(
                "bot.middlewares.profile_screen.answer_with_auto_delete",
                new=send_notice,
            ),
        ):
            result = await self.middleware(
                self.handler,
                event,
                {"settings": _settings()},
            )

        self.assertIsNone(result)
        send_notice.assert_awaited_once()
        rendered = send_notice.await_args.args[1]
        self.assertIn("<b>成员资料审核 · 已处理</b>", rendered)
        self.assertIn("<b>处理结果</b>", rendered)
        self.assertIn("<blockquote expandable>", rendered)
        self.assertIn("已在当前群封禁成员", rendered)
        self.assertIn("广告昵称 &lt;危险&gt;", rendered)
        self.assertIn("<code>/unban</code>", rendered)
        event.delete.assert_awaited_once()

    async def test_completion_loss_keeps_new_restriction_blocked(self) -> None:
        patches = self._patch_screening(verdict=(True, "广告昵称", True))
        event = _event()
        enforcement = AsyncMock(
            side_effect=[
                BanEnforcementResult(final_banned=True),
                BanEnforcementResult(
                    final_banned=False,
                    final_restricted=True,
                ),
            ]
        )
        with (
            patches[0],
            patches[1],
            patches[2],
            patches[3],
            patches[4],
            patches[5],
            patches[6],
            patches[7],
            patches[8],
            patch(
                "bot.middlewares.profile_screen.mark_profile_screening_group_ban",
                new=AsyncMock(),
            ),
            patch(
                "bot.middlewares.profile_screen.enforce_ban_with_policy_reconciliation_result",
                new=enforcement,
            ),
            patch(
                "bot.middlewares.profile_screen.complete_leased_join_verification",
                new=AsyncMock(return_value=False),
            ),
            patch(
                "bot.middlewares.profile_screen.answer_with_auto_delete",
                new=AsyncMock(),
            ),
        ):
            result = await self.middleware(
                self.handler,
                event,
                {"settings": _settings()},
            )

        self.assertIsNone(result)
        self.assertEqual(enforcement.await_count, 2)
        self.handler.assert_not_awaited()
        event.delete.assert_awaited_once()

    async def test_violation_never_bans_when_policy_persistence_fails(self) -> None:
        entered = asyncio.Event()
        release = asyncio.Event()

        async def slow_violation(*args: object, **kwargs: object):
            entered.set()
            await release.wait()
            return True, "广告昵称", True

        patches = list(self._patch_screening())
        patches[7] = patch(
            "bot.middlewares.profile_screen.screen_member_profile_verbose",
            new=AsyncMock(side_effect=slow_violation),
        )
        events = [_event(text="first"), _event(text="second")]

        with (
            patches[0],
            patches[1],
            patches[2],
            patches[3],
            patches[4],
            patches[5],
            patches[6],
            patches[7] as screen,
            patches[8],
            patch(
                "bot.middlewares.profile_screen.mark_profile_screening_group_ban",
                new=AsyncMock(side_effect=RuntimeError("write failed")),
            ) as add_ban,
            patch(
                "bot.middlewares.profile_screen.answer_with_auto_delete",
                new=AsyncMock(),
            ),
        ):
            first = asyncio.create_task(
                self.middleware(self.handler, events[0], {"settings": _settings()})
            )
            await _wait_for_screening_start(self, first, entered)
            second = asyncio.create_task(
                self.middleware(self.handler, events[1], {"settings": _settings()})
            )
            await _wait_for_gate_users(
                self,
                self.middleware,
                (-100, 42),
                2,
                release=release,
                tasks=[first, second],
            )
            release.set()
            results = await _gather_message_tasks(self, [first, second])

        self.assertEqual(results, ["handled", "handled"])
        self.assertEqual(self.handler.await_count, 2)
        self.assertEqual(screen.await_count, 2)
        self.assertEqual(add_ban.await_count, 2)
        for event in events:
            event.delete.assert_not_awaited()
            event.bot.ban_chat_member.assert_not_awaited()

    async def test_concurrent_different_profile_signature_is_reviewed_separately(self) -> None:
        entered = asyncio.Event()
        release = asyncio.Event()

        async def review_profile(
            _session: object,
            _moderation: object,
            **kwargs: object,
        ) -> tuple[bool, str, bool]:
            profile_text = str(kwargs.get("profile_text") or "")
            if "普通用户" in profile_text:
                entered.set()
                await release.wait()
                return False, "", True
            return True, "广告昵称", True

        patches = list(self._patch_screening())
        patches[7] = patch(
            "bot.middlewares.profile_screen.screen_member_profile_verbose",
            new=AsyncMock(side_effect=review_profile),
        )
        clean_event = _event(text="first")
        changed_event = _event(
            text="second",
            from_user=SimpleNamespace(
                id=42,
                is_bot=False,
                full_name="广告用户",
                username="member42",
            ),
        )

        with (
            patches[0],
            patches[1],
            patches[2],
            patches[3],
            patches[4],
            patches[5],
            patches[6],
            patches[7] as screen,
            patches[8],
            patch(
                "bot.middlewares.profile_screen.mark_profile_screening_group_ban",
                new=AsyncMock(),
            ),
            patch("bot.middlewares.profile_screen.answer_with_auto_delete", new=AsyncMock()),
            patch(
                "bot.middlewares.profile_screen.verification_release_blocked_by_ban",
                new=AsyncMock(return_value=True),
            ),
        ):
            first = asyncio.create_task(
                self.middleware(self.handler, clean_event, {"settings": _settings()})
            )
            await _wait_for_screening_start(self, first, entered)
            second = asyncio.create_task(
                self.middleware(self.handler, changed_event, {"settings": _settings()})
            )
            await _wait_for_gate_users(
                self,
                self.middleware,
                (-100, 42),
                2,
                release=release,
                tasks=[first, second],
            )
            release.set()
            results = await _gather_message_tasks(self, [first, second])

        self.assertEqual(results, ["handled", None])
        self.assertEqual(screen.await_count, 2)
        self.handler.assert_awaited_once_with(clean_event, {"settings": _settings()})
        changed_event.delete.assert_awaited_once()
        changed_event.bot.ban_chat_member.assert_awaited_once_with(
            -100,
            42,
            revoke_messages=True,
        )


class ProfileScreenConcurrencyTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        unban_generation_patcher = patch(
            "bot.middlewares.profile_screen.manual_unban_generation_is_active",
            return_value=False,
        )
        unban_generation_patcher.start()
        self.addCleanup(unban_generation_patcher.stop)
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

    async def test_concurrent_violations_are_screened_once_and_all_blocked(self) -> None:
        middleware = ProfileScreenEnforcementMiddleware(self.session_factory)
        handler = AsyncMock(return_value="handled")
        moderation = SimpleNamespace(is_user_exempt=AsyncMock(return_value=False))
        entered = asyncio.Event()
        release = asyncio.Event()

        async def slow_violation(*args: object, **kwargs: object):
            entered.set()
            await release.wait()
            return True, "广告昵称", True

        events = [_event(text="first"), _event(text="second")]
        with (
            patch(
                "bot.middlewares.profile_screen.moderation_rules_fingerprint",
                new=AsyncMock(return_value="rules-v2"),
            ),
            patch(
                "bot.middlewares.profile_screen.is_user_admin_cached",
                new=AsyncMock(return_value=False),
            ),
            patch(
                "bot.middlewares.profile_screen.ModerationService",
                return_value=moderation,
            ),
            patch("bot.middlewares.profile_screen._build_llm", return_value=object()),
            patch(
                "bot.middlewares.profile_screen.screen_member_profile_verbose",
                new=AsyncMock(side_effect=slow_violation),
            ) as screen,
            patch(
                "bot.middlewares.profile_screen.answer_with_auto_delete",
                new=AsyncMock(),
            ),
        ):
            first = asyncio.create_task(
                middleware(handler, events[0], {"settings": _settings()})
            )
            await _wait_for_screening_start(self, first, entered)
            second = asyncio.create_task(
                middleware(handler, events[1], {"settings": _settings()})
            )
            await _wait_for_gate_users(
                self,
                middleware,
                (-100, 42),
                2,
                release=release,
                tasks=[first, second],
            )
            release.set()
            results = await _gather_message_tasks(self, [first, second])

        self.assertEqual(results, [None, None])
        handler.assert_not_awaited()
        screen.assert_awaited_once()
        for event in events:
            event.delete.assert_awaited_once()
        ban_calls = sum(
            event.bot.ban_chat_member.await_count for event in events
        )
        self.assertEqual(ban_calls, 1)
        enforcing_event = next(
            event
            for event in events
            if event.bot.ban_chat_member.await_count
        )
        enforcing_event.bot.ban_chat_member.assert_awaited_once_with(
            -100,
            42,
            revoke_messages=True,
        )

        async with self.session_factory() as session:
            global_row = await get_global_ban(session, 42)
            local_row = await session.scalar(
                select(UserWarning).where(
                    UserWarning.group_id == -100,
                    UserWarning.user_id == 42,
                )
            )
        self.assertIsNone(global_row)
        self.assertIsNotNone(local_row)
        self.assertTrue(local_row.is_banned)


if __name__ == "__main__":
    unittest.main()
