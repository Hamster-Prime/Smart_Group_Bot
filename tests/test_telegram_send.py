import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from aiogram.exceptions import TelegramBadRequest

from bot.utils.telegram import answer_with_auto_delete, send_chat_message


class ScheduledSendFallbackTests(unittest.IsolatedAsyncioTestCase):
    async def test_answer_with_auto_delete_retries_plain_text_on_entity_parse_error(self) -> None:
        message = SimpleNamespace(
            answer=AsyncMock(
                side_effect=[
                    TelegramBadRequest(
                        method=SimpleNamespace(),
                        message='Bad Request: can\'t parse entities: Unsupported start tag "内容"',
                    ),
                    SimpleNamespace(message_id=88, chat=SimpleNamespace(id=-10001)),
                ]
            )
        )

        sent = await answer_with_auto_delete(message, "<内容>今天群聊总结</内容>", auto_delete_minutes=0)

        self.assertEqual(sent.message_id, 88)
        self.assertEqual(message.answer.await_count, 2)
        second_kwargs = message.answer.await_args_list[1].kwargs
        self.assertIsNone(second_kwargs["parse_mode"])
        self.assertEqual(message.answer.await_args_list[1].args[0], "&lt;内容&gt;今天群聊总结&lt;/内容&gt;")

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
