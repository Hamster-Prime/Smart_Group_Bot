from __future__ import annotations

import asyncio
import unittest
from unittest.mock import patch

from aiogram import Bot, Dispatcher

from bot.services.verify_web import _QueuedWebhookUpdate, _WebhookUpdateQueue


class WebhookHealthBookkeepingTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.bot = Bot(token="42:TEST_TOKEN")
        self.queue = _WebhookUpdateQueue(
            dispatcher=Dispatcher(),
            bot=self.bot,
            session_factory=None,
            secret_token="s" * 32,
            worker_count=1,
        )

    async def asyncTearDown(self) -> None:
        await self.queue.stop()
        await self.bot.session.close()

    async def test_same_poison_update_does_not_mark_transport_fatal(self) -> None:
        for _ in range(10):
            self.queue._record_business_failure(701, "poison")
        self.assertIsNone(self.queue.fatal_issue())
        self.assertEqual(
            self.queue.health_snapshot()["consecutive_distinct_failures"],
            1,
        )

    async def test_three_distinct_failures_degrade_health_until_success(self) -> None:
        for update_id in (701, 702, 703):
            self.queue._record_business_failure(update_id, "systemic")
        self.assertIsNotNone(self.queue.fatal_issue())
        self.assertFalse(self.queue.health_snapshot()["ok"])

        self.queue._record_durable_success()
        self.assertIsNone(self.queue.fatal_issue())
        self.assertTrue(self.queue.health_snapshot()["ok"])

    async def test_durable_completion_maps_are_pruned_on_finish(self) -> None:
        loop = asyncio.get_running_loop()
        self.queue._completed_update_ids.update(
            {index: loop.time() for index in range(10)}
        )
        self.queue._seen_update_ids.update(
            {index: loop.time() for index in range(10)}
        )
        result = loop.create_future()
        queued = _QueuedWebhookUpdate(
            update={"update_id": 999},
            update_id=999,
            enqueued_at=loop.time(),
            result=result,
        )
        self.queue._inflight_updates[999] = result

        with patch("bot.services.verify_web._WEBHOOK_DEDUP_MAX_UPDATES", 3):
            await self.queue._finish_queued_update(queued, succeeded=True)

        self.assertLessEqual(len(self.queue._completed_update_ids), 3)
        self.assertLessEqual(len(self.queue._seen_update_ids), 3)


if __name__ == "__main__":
    unittest.main()
