import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from aiogram.enums import ChatMemberStatus

from bot.services.callback_auth import is_group_admin_or_higher


def _session(local_admin_id: int | None = None) -> SimpleNamespace:
    result = SimpleNamespace(scalar_one_or_none=lambda: local_admin_id)
    return SimpleNamespace(execute=AsyncMock(return_value=result))


def _settings(super_admin_id: int = 1) -> SimpleNamespace:
    return SimpleNamespace(super_admin_id=super_admin_id)


class CallbackAuthorizationTests(unittest.IsolatedAsyncioTestCase):
    async def test_super_admin_short_circuits_other_lookups(self) -> None:
        session = _session()
        bot = SimpleNamespace(get_chat_member=AsyncMock())

        allowed = await is_group_admin_or_higher(
            bot=bot,
            session=session,
            settings=_settings(),
            group_id=-100,
            user_id=1,
        )

        self.assertTrue(allowed)
        session.execute.assert_not_awaited()
        bot.get_chat_member.assert_not_awaited()

    async def test_locally_authorized_admin_skips_telegram_lookup(self) -> None:
        session = _session(local_admin_id=7)
        bot = SimpleNamespace(get_chat_member=AsyncMock())

        allowed = await is_group_admin_or_higher(
            bot=bot,
            session=session,
            settings=_settings(),
            group_id=-100,
            user_id=42,
        )

        self.assertTrue(allowed)
        session.execute.assert_awaited_once()
        bot.get_chat_member.assert_not_awaited()

    async def test_telegram_creator_is_allowed(self) -> None:
        session = _session()
        bot = SimpleNamespace(
            get_chat_member=AsyncMock(
                return_value=SimpleNamespace(status=ChatMemberStatus.CREATOR)
            )
        )

        allowed = await is_group_admin_or_higher(
            bot=bot,
            session=session,
            settings=_settings(),
            group_id=-100,
            user_id=42,
        )

        self.assertTrue(allowed)
        bot.get_chat_member.assert_awaited_once_with(-100, 42)

    async def test_telegram_admin_requires_restrict_members_permission(self) -> None:
        for can_restrict_members, expected in ((True, True), (False, False)):
            with self.subTest(can_restrict_members=can_restrict_members):
                session = _session()
                bot = SimpleNamespace(
                    get_chat_member=AsyncMock(
                        return_value=SimpleNamespace(
                            status=ChatMemberStatus.ADMINISTRATOR,
                            can_restrict_members=can_restrict_members,
                        )
                    )
                )

                allowed = await is_group_admin_or_higher(
                    bot=bot,
                    session=session,
                    settings=_settings(),
                    group_id=-100,
                    user_id=42,
                )

                self.assertEqual(allowed, expected)
                bot.get_chat_member.assert_awaited_once_with(-100, 42)

    async def test_telegram_status_is_fetched_fresh_each_time(self) -> None:
        session = _session()
        bot = SimpleNamespace(
            get_chat_member=AsyncMock(
                side_effect=[
                    SimpleNamespace(
                        status=ChatMemberStatus.ADMINISTRATOR,
                        can_restrict_members=True,
                    ),
                    SimpleNamespace(status=ChatMemberStatus.MEMBER),
                ]
            )
        )

        first = await is_group_admin_or_higher(
            bot=bot,
            session=session,
            settings=_settings(),
            group_id=-100,
            user_id=42,
        )
        second = await is_group_admin_or_higher(
            bot=bot,
            session=session,
            settings=_settings(),
            group_id=-100,
            user_id=42,
        )

        self.assertTrue(first)
        self.assertFalse(second)
        self.assertEqual(bot.get_chat_member.await_count, 2)

    async def test_lookup_failures_fail_closed(self) -> None:
        session = SimpleNamespace(execute=AsyncMock(side_effect=RuntimeError("db unavailable")))
        bot = SimpleNamespace(
            get_chat_member=AsyncMock(side_effect=RuntimeError("telegram unavailable"))
        )

        allowed = await is_group_admin_or_higher(
            bot=bot,
            session=session,
            settings=_settings(),
            group_id=-100,
            user_id=42,
        )

        self.assertFalse(allowed)
        bot.get_chat_member.assert_awaited_once_with(-100, 42)

    async def test_invalid_identity_fails_closed_without_lookups(self) -> None:
        session = _session()
        bot = SimpleNamespace(get_chat_member=AsyncMock())

        allowed = await is_group_admin_or_higher(
            bot=bot,
            session=session,
            settings=_settings(),
            group_id=-100,
            user_id="invalid",  # type: ignore[arg-type]
        )

        self.assertFalse(allowed)
        session.execute.assert_not_awaited()
        bot.get_chat_member.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
