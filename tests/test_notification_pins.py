import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from aiogram.exceptions import TelegramBadRequest

from bot.services.notification_pins import unpin_notification_message


class NotificationPinTests(unittest.IsolatedAsyncioTestCase):
    async def test_missing_or_already_unpinned_message_is_released(self) -> None:
        for detail in (
            "Bad Request: message is not pinned",
            "Bad Request: message to unpin not found",
            "Bad Request: MESSAGE_ID_INVALID",
        ):
            with self.subTest(detail=detail):
                bot = SimpleNamespace(
                    unpin_chat_message=AsyncMock(
                        side_effect=TelegramBadRequest(
                            method=SimpleNamespace(),
                            message=detail,
                        )
                    )
                )

                released = await unpin_notification_message(
                    bot,
                    chat_id=-100,
                    message_id=77,
                    kind="vote_ban",
                )

                self.assertTrue(released)

    async def test_permission_error_remains_retryable(self) -> None:
        bot = SimpleNamespace(
            unpin_chat_message=AsyncMock(
                side_effect=TelegramBadRequest(
                    method=SimpleNamespace(),
                    message="Bad Request: not enough rights to unpin a message",
                )
            )
        )

        released = await unpin_notification_message(
            bot,
            chat_id=-100,
            message_id=77,
            kind="raid_guard",
        )

        self.assertFalse(released)


if __name__ == "__main__":
    unittest.main()
