from __future__ import annotations

import asyncio
import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from sqlalchemy import func, select

from bot.config import ModerationConfig
from bot.db.engine import init_db
from bot.db.models import (
    BanAuditEvent,
    Group,
    ModerationRule,
    UserWarning,
    Violation,
)
from bot.handlers import group
from bot.services.join_verification import BanEnforcementResult
from bot.services.moderation import ModerationService


class ModerationIdempotencyTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        fd, self.path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.engine, self.session_factory = await init_db(
            f"sqlite+aiosqlite:///{self.path}"
        )
        async with self.session_factory() as session:
            session.add(Group(id=-100, title="test", settings={}))
            await session.commit()
        self.service = ModerationService(
            ModerationConfig(warn_threshold=2),
            SimpleNamespace(),
        )

    async def asyncTearDown(self) -> None:
        await self.engine.dispose()
        for suffix in ("", "-wal", "-shm"):
            try:
                os.remove(self.path + suffix)
            except OSError:
                pass

    async def test_concurrent_record_reuses_one_source_event(self) -> None:
        async def record_once(user_id: int) -> tuple[int, bool]:
            async with self.session_factory() as session:
                violation = await self.service.record_violation(
                    session,
                    -100,
                    user_id,
                    "same update",
                    "warn",
                    source_message_id=9001,
                )
                await session.commit()
                return int(violation.id), bool(
                    getattr(violation, "_source_event_created", False)
                )

        first, second = await asyncio.gather(record_once(11), record_once(11))

        self.assertEqual(first[0], second[0])
        self.assertEqual(sorted((first[1], second[1])), [False, True])
        async with self.session_factory() as session:
            count = await session.scalar(
                select(func.count(Violation.id)).where(
                    Violation.group_id == -100,
                    Violation.source_message_id == 9001,
                )
            )
        self.assertEqual(count, 1)

    async def test_slow_llm_holds_no_session_transaction_or_pool_connection(self) -> None:
        async with self.session_factory() as session:
            session.add(
                ModerationRule(
                    group_id=-100,
                    rule_type="llm",
                    pattern="semantic spam",
                    action="warn",
                    enabled=True,
                )
            )
            await session.commit()

        checked_during_wait = asyncio.Event()

        async def slow_moderation(_system: str, _user: str) -> str:
            self.assertFalse(session.in_transaction())
            self.assertEqual(self.engine.sync_engine.pool.checkedout(), 0)
            checked_during_wait.set()
            await asyncio.sleep(0.05)
            self.assertFalse(session.in_transaction())
            self.assertEqual(self.engine.sync_engine.pool.checkedout(), 0)
            return '{"violated": false, "confidence": 1.0}'

        service = ModerationService(
            ModerationConfig(),
            SimpleNamespace(moderation=slow_moderation),
        )
        async with self.session_factory() as session:
            verdict = await service.evaluate(session, -100, "ordinary text")
        self.assertTrue(checked_during_wait.is_set())
        self.assertFalse(verdict.violated)
        self.assertTrue(verdict.conclusive)

    async def test_counted_ban_retry_does_not_increment_or_audit_twice(self) -> None:
        async with self.session_factory() as session:
            session.add(
                UserWarning(
                    group_id=-100,
                    user_id=42,
                    count=1,
                    is_banned=False,
                )
            )
            await session.commit()

        message = SimpleNamespace(
            message_id=77,
            bot=SimpleNamespace(),
            delete=AsyncMock(),
        )
        ban = AsyncMock(return_value=BanEnforcementResult(final_banned=True))
        results: list[tuple[int, int, bool]] = []
        with patch(
            "bot.handlers.group.enforce_ban_with_policy_reconciliation_result",
            new=ban,
        ):
            for _ in range(2):
                async with self.session_factory() as session:
                    count, violation, enforced, _, _retryable = (
                        await group._apply_counted_moderation_ban(
                            moderation=self.service,
                            session=session,
                            message=message,
                            group_id=-100,
                            user_id=42,
                            input_text="spam",
                            rule=None,
                            message_deleted=False,
                        )
                    )
                    results.append((count, int(violation.id), enforced))

        self.assertEqual(results[0], results[1])
        self.assertEqual(results[0][0], 2)
        self.assertTrue(results[0][2])
        ban.assert_awaited_once()

        async with self.session_factory() as session:
            warning = await session.scalar(
                select(UserWarning).where(
                    UserWarning.group_id == -100,
                    UserWarning.user_id == 42,
                )
            )
            violations = await session.scalar(select(func.count(Violation.id)))
            audits = await session.scalar(select(func.count(BanAuditEvent.id)))
            violation = await session.scalar(select(Violation))
        self.assertEqual((warning.count, warning.is_banned), (2, True))
        self.assertEqual(violations, 1)
        self.assertEqual(audits, 1)
        self.assertEqual(violation.warning_count, 2)
        self.assertTrue(violation.ban_enforced)

    async def test_failed_counted_ban_retries_until_success_then_stops(self) -> None:
        async with self.session_factory() as session:
            session.add(
                UserWarning(
                    group_id=-100,
                    user_id=43,
                    count=1,
                    is_banned=False,
                )
            )
            await session.commit()

        message = SimpleNamespace(
            message_id=78,
            bot=SimpleNamespace(),
            delete=AsyncMock(),
        )
        ban = AsyncMock(
            side_effect=[
                BanEnforcementResult(final_banned=None, retryable=True),
                BanEnforcementResult(final_banned=True),
            ]
        )
        results: list[bool] = []
        with patch(
            "bot.handlers.group.enforce_ban_with_policy_reconciliation_result",
            new=ban,
        ):
            for _ in range(3):
                async with self.session_factory() as session:
                    _count, _violation, enforced, _note, _retryable = (
                        await group._apply_counted_moderation_ban(
                            moderation=self.service,
                            session=session,
                            message=message,
                            group_id=-100,
                            user_id=43,
                            input_text="retry spam",
                            rule=None,
                            message_deleted=False,
                        )
                    )
                    results.append(enforced)

        self.assertEqual(results, [False, True, True])
        self.assertEqual(ban.await_count, 2)
        async with self.session_factory() as session:
            warning = await session.scalar(
                select(UserWarning).where(
                    UserWarning.group_id == -100,
                    UserWarning.user_id == 43,
                )
            )
            violation = await session.scalar(
                select(Violation).where(
                    Violation.group_id == -100,
                    Violation.source_message_id == 78,
                )
            )
            audits = await session.scalar(
                select(func.count(BanAuditEvent.id)).where(
                    BanAuditEvent.reference_type == "violation",
                    BanAuditEvent.reference_id == violation.id,
                )
            )
        self.assertEqual((warning.count, warning.is_banned), (2, True))
        self.assertTrue(violation.ban_enforced)
        self.assertEqual(audits, 2)

    async def test_failed_notice_is_retried_then_durably_suppressed(self) -> None:
        async with self.session_factory() as session:
            violation = await self.service.record_violation(
                session,
                -100,
                42,
                "spam",
                "warn",
                source_message_id=88,
            )
            await session.commit()
            violation_id = int(violation.id)

        answer = AsyncMock(
            side_effect=[RuntimeError("telegram down"), SimpleNamespace(message_id=1)]
        )
        message = SimpleNamespace(message_id=88)
        with patch("bot.handlers.group.answer_with_auto_delete", new=answer):
            async with self.session_factory() as session:
                violation = await session.get(Violation, violation_id)
                with self.assertRaisesRegex(RuntimeError, "telegram down"):
                    await group._send_moderation_notice_once_locked(
                        session=session,
                        violation=violation,
                        message=message,
                        notice="blocked",
                        auto_delete_seconds=0,
                    )

            async with self.session_factory() as session:
                violation = await session.get(Violation, violation_id)
                sent = await group._send_moderation_notice_once_locked(
                    session=session,
                    violation=violation,
                    message=message,
                    notice="blocked",
                    auto_delete_seconds=0,
                )
                self.assertTrue(sent)

            async with self.session_factory() as session:
                violation = await session.get(Violation, violation_id)
                sent = await group._send_moderation_notice_once_locked(
                    session=session,
                    violation=violation,
                    message=message,
                    notice="blocked",
                    auto_delete_seconds=0,
                )
                self.assertFalse(sent)

        self.assertEqual(answer.await_count, 2)
        async with self.session_factory() as session:
            violation = await session.get(Violation, violation_id)
            self.assertIsNotNone(violation.notice_sent_at)


if __name__ == "__main__":
    unittest.main()
