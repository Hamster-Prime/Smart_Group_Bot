import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from aiogram.enums import ChatMemberStatus

from bot.handlers import membership
from bot.services import admin_status


def _message(get_member: AsyncMock) -> SimpleNamespace:
    return SimpleNamespace(
        chat=SimpleNamespace(
            id=-100,
            type="supergroup",
            get_member=get_member,
        ),
        from_user=SimpleNamespace(id=42),
    )


class AdminStatusCacheTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        admin_status._NON_ADMIN_STATUS_CACHE.clear()

    def tearDown(self) -> None:
        admin_status._NON_ADMIN_STATUS_CACHE.clear()

    async def test_positive_status_is_refreshed_and_demotion_takes_effect(self) -> None:
        get_member = AsyncMock(
            side_effect=[
                SimpleNamespace(status=ChatMemberStatus.ADMINISTRATOR),
                SimpleNamespace(status=ChatMemberStatus.MEMBER),
            ]
        )
        message = _message(get_member)

        self.assertTrue(await admin_status.is_user_admin_cached(message))
        self.assertFalse(await admin_status.is_user_admin_cached(message))
        self.assertEqual(get_member.await_count, 2)

    async def test_lookup_failure_after_admin_result_fails_closed(self) -> None:
        get_member = AsyncMock(
            side_effect=[
                SimpleNamespace(status=ChatMemberStatus.CREATOR),
                RuntimeError("telegram unavailable"),
            ]
        )
        message = _message(get_member)

        self.assertTrue(await admin_status.is_user_admin_cached(message))
        self.assertFalse(await admin_status.is_user_admin_cached(message))
        self.assertEqual(get_member.await_count, 2)

    async def test_non_admin_cache_is_invalidated_on_promotion_update(self) -> None:
        get_member = AsyncMock(
            side_effect=[
                SimpleNamespace(status=ChatMemberStatus.MEMBER),
                SimpleNamespace(status=ChatMemberStatus.ADMINISTRATOR),
            ]
        )
        message = _message(get_member)

        self.assertFalse(await admin_status.is_user_admin_cached(message))
        self.assertFalse(await admin_status.is_user_admin_cached(message))
        self.assertEqual(get_member.await_count, 1)

        event = SimpleNamespace(
            chat=SimpleNamespace(id=-100, type="supergroup"),
            new_chat_member=SimpleNamespace(user=SimpleNamespace(id=42)),
        )
        await membership.on_member_status_change(
            event,
            session=object(),
            settings=object(),
        )

        self.assertTrue(await admin_status.is_user_admin_cached(message))
        self.assertEqual(get_member.await_count, 2)


if __name__ == "__main__":
    unittest.main()
