from __future__ import annotations

import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from bot.web import settings_api


class MemberIdentityLookupTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.original_timeout = (
            settings_api._MEMBER_IDENTITY_LOOKUP_TIMEOUT_SECONDS
        )
        self.original_cancel_grace = (
            settings_api._MEMBER_IDENTITY_CANCEL_GRACE_SECONDS
        )
        self.original_concurrency = settings_api._MEMBER_IDENTITY_LOOKUP_CONCURRENCY
        self.original_orphan_limit = settings_api._MEMBER_IDENTITY_ORPHAN_LIMIT
        self.original_cooldown = (
            settings_api._MEMBER_IDENTITY_CIRCUIT_COOLDOWN_SECONDS
        )
        settings_api._MEMBER_IDENTITY_CACHE.clear()
        settings_api._MEMBER_IDENTITY_INFLIGHT.clear()
        settings_api._MEMBER_IDENTITY_ORPHANS.clear()
        settings_api._MEMBER_IDENTITY_RPC_TASKS.clear()
        settings_api._MEMBER_IDENTITY_LOOKUP_TIMEOUT_SECONDS = 0.05
        settings_api._MEMBER_IDENTITY_CANCEL_GRACE_SECONDS = 0.01
        settings_api._MEMBER_IDENTITY_CIRCUIT_OPEN_UNTIL = 0.0
        settings_api._MEMBER_IDENTITY_LOOKUP_LOOP = None
        settings_api._MEMBER_IDENTITY_LOOKUP_SEMAPHORE = None

    async def asyncTearDown(self) -> None:
        await settings_api.flush_member_identity_tasks(timeout_seconds=0.1)
        settings_api._MEMBER_IDENTITY_LOOKUP_TIMEOUT_SECONDS = self.original_timeout
        settings_api._MEMBER_IDENTITY_CANCEL_GRACE_SECONDS = self.original_cancel_grace
        settings_api._MEMBER_IDENTITY_LOOKUP_CONCURRENCY = self.original_concurrency
        settings_api._MEMBER_IDENTITY_ORPHAN_LIMIT = self.original_orphan_limit
        settings_api._MEMBER_IDENTITY_CIRCUIT_COOLDOWN_SECONDS = self.original_cooldown
        settings_api._MEMBER_IDENTITY_CIRCUIT_OPEN_UNTIL = 0.0
        settings_api._MEMBER_IDENTITY_CACHE.clear()
        settings_api._MEMBER_IDENTITY_INFLIGHT.clear()
        settings_api._MEMBER_IDENTITY_ORPHANS.clear()
        settings_api._MEMBER_IDENTITY_RPC_TASKS.clear()
        settings_api._MEMBER_IDENTITY_LOOKUP_LOOP = None
        settings_api._MEMBER_IDENTITY_LOOKUP_SEMAPHORE = None

    async def test_concurrent_requests_share_one_telegram_lookup(self) -> None:
        async def lookup(_group_id: int, _user_id: int):
            await asyncio.sleep(0)
            return SimpleNamespace(
                user=SimpleNamespace(
                    full_name="测试用户",
                    username="tester",
                    is_bot=False,
                )
            )

        bot = SimpleNamespace(get_chat_member=AsyncMock(side_effect=lookup))
        first, second = await asyncio.gather(
            settings_api._lookup_member_identity(bot, -100, 7),
            settings_api._lookup_member_identity(bot, -100, 7),
        )

        self.assertEqual(first, ("测试用户", "tester", False))
        self.assertEqual(second, first)
        bot.get_chat_member.assert_awaited_once_with(-100, 7)

    async def test_stalled_telegram_lookup_times_out_and_is_negative_cached(self) -> None:
        async def lookup(_group_id: int, _user_id: int):
            await asyncio.sleep(1)

        bot = SimpleNamespace(get_chat_member=AsyncMock(side_effect=lookup))
        self.assertIsNone(
            await settings_api._lookup_member_identity(bot, -101, 8)
        )
        self.assertIsNone(
            await settings_api._lookup_member_identity(bot, -101, 8)
        )
        bot.get_chat_member.assert_awaited_once_with(-101, 8)

    async def test_cancel_resistant_lookup_cannot_extend_hard_deadline(self) -> None:
        release = asyncio.Event()

        async def lookup(_group_id: int, _user_id: int):
            try:
                await asyncio.sleep(60)
            except asyncio.CancelledError:
                await release.wait()

        bot = SimpleNamespace(get_chat_member=AsyncMock(side_effect=lookup))
        started = asyncio.get_running_loop().time()
        self.assertIsNone(
            await settings_api._lookup_member_identity(bot, -102, 9)
        )
        self.assertLess(asyncio.get_running_loop().time() - started, 0.5)
        self.assertEqual(len(settings_api._MEMBER_IDENTITY_ORPHANS), 1)
        release.set()
        for _ in range(10):
            if not settings_api._MEMBER_IDENTITY_ORPHANS:
                break
            await asyncio.sleep(0)
        self.assertFalse(settings_api._MEMBER_IDENTITY_ORPHANS)

    async def test_cancel_resistant_child_keeps_real_concurrency_permit(self) -> None:
        release = asyncio.Event()
        calls: list[int] = []
        settings_api._MEMBER_IDENTITY_LOOKUP_CONCURRENCY = 1
        settings_api._MEMBER_IDENTITY_LOOKUP_LOOP = None
        settings_api._MEMBER_IDENTITY_LOOKUP_SEMAPHORE = None

        async def lookup(_group_id: int, user_id: int):
            calls.append(user_id)
            if user_id == 10:
                try:
                    await asyncio.sleep(60)
                except asyncio.CancelledError:
                    await release.wait()
            return SimpleNamespace(
                user=SimpleNamespace(full_name="下一位", username="next", is_bot=False)
            )

        bot = SimpleNamespace(get_chat_member=AsyncMock(side_effect=lookup))
        self.assertIsNone(await settings_api._lookup_member_identity(bot, -103, 10))
        self.assertEqual(calls, [10])

        # The first child ignored cancellation, so it still owns the only
        # permit.  The replacement times out while waiting and never invokes
        # Telegram.
        self.assertIsNone(await settings_api._lookup_member_identity(bot, -103, 11))
        self.assertEqual(calls, [10])

        release.set()
        for _ in range(20):
            if not settings_api._MEMBER_IDENTITY_ORPHANS:
                break
            await asyncio.sleep(0)
        self.assertFalse(settings_api._MEMBER_IDENTITY_ORPHANS)

    async def test_orphan_limit_opens_lookup_circuit(self) -> None:
        release = asyncio.Event()
        settings_api._MEMBER_IDENTITY_ORPHAN_LIMIT = 1
        settings_api._MEMBER_IDENTITY_CIRCUIT_COOLDOWN_SECONDS = 60.0

        async def lookup(_group_id: int, _user_id: int):
            try:
                await asyncio.sleep(60)
            except asyncio.CancelledError:
                await release.wait()

        bot = SimpleNamespace(get_chat_member=AsyncMock(side_effect=lookup))
        self.assertIsNone(await settings_api._lookup_member_identity(bot, -104, 12))
        self.assertEqual(bot.get_chat_member.await_count, 1)
        self.assertIsNone(await settings_api._lookup_member_identity(bot, -104, 13))
        self.assertEqual(bot.get_chat_member.await_count, 1)
        self.assertGreater(
            settings_api._MEMBER_IDENTITY_CIRCUIT_OPEN_UNTIL,
            asyncio.get_running_loop().time(),
        )
        release.set()
        await asyncio.sleep(0)

    async def test_shutdown_flush_bounds_cancel_resistant_admin_lookup(self) -> None:
        entered = asyncio.Event()
        release = asyncio.Event()

        async def get_chat_administrators(_group_id: int):
            entered.set()
            try:
                await asyncio.sleep(60)
            except asyncio.CancelledError:
                await release.wait()
            return []

        bot = SimpleNamespace(
            get_chat_administrators=AsyncMock(
                side_effect=get_chat_administrators
            )
        )
        waiter = asyncio.create_task(
            settings_api._await_member_identity_lookup(
                lambda: bot.get_chat_administrators(-105),
                timeout_seconds=60.0,
            )
        )
        await asyncio.wait_for(entered.wait(), timeout=0.2)
        started = asyncio.get_running_loop().time()
        await settings_api.flush_member_identity_tasks(timeout_seconds=0.02)
        self.assertLess(asyncio.get_running_loop().time() - started, 0.2)
        self.assertEqual(len(settings_api._MEMBER_IDENTITY_ORPHANS), 1)
        release.set()
        self.assertEqual(await asyncio.wait_for(waiter, timeout=0.2), [])
        self.assertFalse(settings_api._MEMBER_IDENTITY_ORPHANS)


class JsonBodyDeadlineTests(unittest.IsolatedAsyncioTestCase):
    async def test_cancel_resistant_json_reader_returns_408_at_hard_deadline(self) -> None:
        original_timeout = settings_api._JSON_BODY_TIMEOUT_SECONDS
        original_grace = settings_api._JSON_BODY_CANCEL_GRACE_SECONDS
        release = asyncio.Event()

        async def read_json():
            try:
                await asyncio.sleep(60)
            except asyncio.CancelledError:
                await release.wait()
            return {}

        settings_api._JSON_BODY_TIMEOUT_SECONDS = 0.02
        settings_api._JSON_BODY_CANCEL_GRACE_SECONDS = 0.01
        request = SimpleNamespace(json=AsyncMock(side_effect=read_json))
        started = asyncio.get_running_loop().time()
        try:
            with self.assertRaises(settings_api._APIError) as caught:
                await settings_api._json_object(request)
            self.assertEqual(caught.exception.status, 408)
            self.assertEqual(caught.exception.code, "request_timeout")
            self.assertLess(asyncio.get_running_loop().time() - started, 0.2)
            self.assertEqual(len(settings_api._JSON_BODY_ORPHANS), 1)
        finally:
            release.set()
            for _ in range(20):
                if not settings_api._JSON_BODY_ORPHANS:
                    break
                await asyncio.sleep(0)
            settings_api._JSON_BODY_TIMEOUT_SECONDS = original_timeout
            settings_api._JSON_BODY_CANCEL_GRACE_SECONDS = original_grace
            settings_api._JSON_BODY_ORPHANS.clear()


class GroupMemberMapBudgetTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def _empty_session_factory(active: dict[str, int]):
        class _Rows:
            def all(self):
                return []

        class _Session:
            async def scalars(self, _statement):
                return _Rows()

        class _Context:
            async def __aenter__(self):
                active["count"] += 1
                return _Session()

            async def __aexit__(self, _exc_type, _exc, _tb):
                active["count"] -= 1

        return lambda: _Context()

    async def test_lookup_page_is_limited_and_runs_after_session_closes(self) -> None:
        active = {"count": 0}

        async def lookup(_bot, _group_id: int, _user_id: int):
            self.assertEqual(active["count"], 0)
            return None

        with patch.object(settings_api, "_lookup_member_identity", side_effect=lookup) as mocked:
            result = await settings_api._group_member_map(
                self._empty_session_factory(active),
                -200,
                list(range(1, 100)),
                bot_obj=SimpleNamespace(get_chat_member=AsyncMock()),
            )
        self.assertEqual(result, {})
        self.assertEqual(
            mocked.await_count,
            settings_api._MEMBER_IDENTITY_REQUEST_LOOKUP_LIMIT,
        )
        self.assertEqual(active["count"], 0)

    async def test_lookup_page_has_one_absolute_budget(self) -> None:
        active = {"count": 0}

        async def lookup(_bot, _group_id: int, _user_id: int):
            await asyncio.sleep(60)

        original_budget = settings_api._MEMBER_IDENTITY_REQUEST_BUDGET_SECONDS
        settings_api._MEMBER_IDENTITY_REQUEST_BUDGET_SECONDS = 0.02
        started = asyncio.get_running_loop().time()
        try:
            with patch.object(settings_api, "_lookup_member_identity", side_effect=lookup):
                result = await settings_api._group_member_map(
                    self._empty_session_factory(active),
                    -201,
                    list(range(1, 50)),
                    bot_obj=SimpleNamespace(get_chat_member=AsyncMock()),
                )
            self.assertEqual(result, {})
            self.assertLess(asyncio.get_running_loop().time() - started, 0.2)
        finally:
            settings_api._MEMBER_IDENTITY_REQUEST_BUDGET_SECONDS = original_budget


if __name__ == "__main__":
    unittest.main()
