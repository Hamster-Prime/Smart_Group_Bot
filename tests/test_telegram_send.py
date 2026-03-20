import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from aiogram.exceptions import TelegramBadRequest

from bot.utils.telegram import send_chat_message


class ScheduledSendFallbackTests(unittest.IsolatedAsyncioTestCase):
    async def test_missing_reply_target_falls_back_to_direct_mention(self) -> None:
        bot = SimpleNamespace(
            send_message=AsyncMock(
                side_effect=[
                    TelegramBadRequest(
                        method=SimpleNamespace(),
                        message="message to be replied not found",
                    ),
                    SimpleNamespace(message_id=99, chat=SimpleNamespace(id=-10001)),
                ]
            )
        )

        ok = await send_chat_message(
            bot,
            -10001,
            "提醒你该吃饭了",
            reply_to_message_id=321,
            fallback_mention_user_id=42,
            fallback_mention_name="Alice",
            auto_delete_minutes=0,
        )

        self.assertTrue(ok)
        self.assertEqual(bot.send_message.await_count, 2)

        first_kwargs = bot.send_message.await_args_list[0].kwargs
        second_kwargs = bot.send_message.await_args_list[1].kwargs

        self.assertEqual(first_kwargs["reply_to_message_id"], 321)
        self.assertEqual(second_kwargs["reply_to_message_id"], None)
        self.assertEqual(second_kwargs["parse_mode"], "HTML")
        self.assertIn('tg://user?id=42', second_kwargs["text"])
        self.assertIn("@Alice", second_kwargs["text"])
