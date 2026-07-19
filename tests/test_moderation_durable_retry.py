from __future__ import annotations

import asyncio
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, call

from aiogram import Bot, Dispatcher
from sqlalchemy import func, select, update

from bot.db.engine import init_db
from bot.db.models import (
    BanAuditEvent,
    Group,
    UserWarning,
    Violation,
    WebhookInboxUpdate,
)
from bot.middlewares.global_ban import GlobalBanEnforcementMiddleware
from bot.services.authz import authorize_group
from bot.services.verify_web import _WebhookUpdateQueue
from bot.utils.timezone import now_shanghai_naive


_WEBHOOK_SECRET = "r" * 32


def _request(payload: dict) -> SimpleNamespace:
    return SimpleNamespace(
        headers={"X-Telegram-Bot-Api-Secret-Token": _WEBHOOK_SECRET},
        json=AsyncMock(return_value=payload),
    )


class ModerationDurableRetryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        db_path = Path(self.tempdir.name) / "retry.db"
        self.engine, self.session_factory = await init_db(
            f"sqlite+aiosqlite:///{db_path}"
        )
        async with self.session_factory() as session:
            session.add(Group(id=-100, title="test", settings={}))
            await authorize_group(session, -100, 1)
            session.add(
                UserWarning(
                    group_id=-100,
                    user_id=42,
                    count=3,
                    is_banned=True,
                )
            )
            session.add(
                Violation(
                    group_id=-100,
                    user_id=42,
                    message_text="retry ban",
                    action_taken="ban_applied",
                    source_message_id=7001,
                    warning_count=3,
                    ban_enforced=False,
                )
            )
            await session.commit()
        self.bot = Bot(token="42:TEST_TOKEN")

    async def asyncTearDown(self) -> None:
        await self.bot.session.close()
        await self.engine.dispose()
        self.tempdir.cleanup()

    async def _wait_until(self, predicate, *, timeout: float = 1.5) -> None:
        deadline = asyncio.get_running_loop().time() + timeout
        while not predicate():
            if asyncio.get_running_loop().time() >= deadline:
                self.fail("condition was not reached before timeout")
            await asyncio.sleep(0.005)

    async def test_inbox_replays_local_ban_until_telegram_confirms(self) -> None:
        middleware = GlobalBanEnforcementMiddleware(self.session_factory)
        downstream = AsyncMock(return_value="handled")
        telegram = SimpleNamespace(
            ban_chat_member=AsyncMock(side_effect=[False, True]),
            get_chat_member=AsyncMock(return_value=SimpleNamespace(status="member")),
        )

        async def dispatch(**_kwargs: object) -> None:
            event = SimpleNamespace(
                chat=SimpleNamespace(id=-100, type="supergroup"),
                bot=telegram,
                from_user=SimpleNamespace(id=42),
                sender_chat=None,
                delete=AsyncMock(),
            )
            await middleware(
                downstream,
                event,
                {"settings": SimpleNamespace(super_admin_id=1)},
            )

        queue = _WebhookUpdateQueue(
            dispatcher=Dispatcher(),
            bot=self.bot,
            session_factory=self.session_factory,
            secret_token=_WEBHOOK_SECRET,
            worker_count=1,
        )
        queue.dispatcher.feed_raw_update = AsyncMock(side_effect=dispatch)
        update_id = 88001
        response = await queue.handle_verified(
            _request({"update_id": update_id, "message": {"text": "retry"}})
        )
        self.assertEqual(response.status, 200)

        await self._wait_until(
            lambda: (
                queue.health_snapshot()["retry_scheduled_updates"] == 1
                and update_id not in queue._completed_update_ids
            )
        )
        async with self.session_factory() as session:
            pending = await session.get(WebhookInboxUpdate, update_id)
            self.assertIsNotNone(pending)
            assert pending is not None
            self.assertIsNone(pending.completed_at)
            self.assertIsNotNone(pending.next_attempt_at)
            await session.execute(
                update(WebhookInboxUpdate)
                .where(WebhookInboxUpdate.update_id == update_id)
                .values(
                    next_attempt_at=now_shanghai_naive() - timedelta(seconds=1)
                )
            )
            await session.commit()

        self.assertEqual(await queue._recover_durable_once(), 1)
        await self._wait_until(
            lambda: queue.health_snapshot()["completed_updates"] == 1
        )
        # A later message from the still-locally-banned user re-enforces the
        # policy but must not append another reconciliation audit.
        telegram.ban_chat_member.side_effect = None
        telegram.ban_chat_member.return_value = True
        await dispatch()
        async with self.session_factory() as session:
            completed = await session.get(WebhookInboxUpdate, update_id)
            assert completed is not None
            self.assertIsNotNone(completed.completed_at)
            violation = await session.scalar(
                select(Violation).where(Violation.source_message_id == 7001)
            )
            audit_count = await session.scalar(
                select(func.count(BanAuditEvent.id)).where(
                    BanAuditEvent.reference_type == "violation",
                    BanAuditEvent.reference_id == violation.id,
                    BanAuditEvent.outcome == "succeeded",
                )
            )
            self.assertTrue(violation.ban_enforced)
            self.assertEqual(audit_count, 1)

        self.assertEqual(
            telegram.ban_chat_member.await_args_list,
            [
                call(-100, 42, revoke_messages=True),
                call(-100, 42, revoke_messages=True),
                call(-100, 42, revoke_messages=True),
            ],
        )
        downstream.assert_not_awaited()
        await queue.stop()


if __name__ == "__main__":
    unittest.main()
