from __future__ import annotations

import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from bot.web import settings_api


class MemberIdentityLookupTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.original_timeout = (
            settings_api._MEMBER_IDENTITY_LOOKUP_TIMEOUT_SECONDS
        )
        settings_api._MEMBER_IDENTITY_CACHE.clear()
        settings_api._MEMBER_IDENTITY_INFLIGHT.clear()
        settings_api._MEMBER_IDENTITY_LOOKUP_TIMEOUT_SECONDS = 0.05

    async def asyncTearDown(self) -> None:
        settings_api._MEMBER_IDENTITY_LOOKUP_TIMEOUT_SECONDS = self.original_timeout
        settings_api._MEMBER_IDENTITY_CACHE.clear()
        settings_api._MEMBER_IDENTITY_INFLIGHT.clear()

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


if __name__ == "__main__":
    unittest.main()
