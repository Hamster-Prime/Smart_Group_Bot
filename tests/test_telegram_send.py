import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from aiogram.exceptions import TelegramBadRequest, TelegramNetworkError

from bot.utils import telegram
from bot.utils.telegram import answer_with_auto_delete, send_chat_message, send_reply, send_reply_messages


class ScheduledSendFallbackTests(unittest.IsolatedAsyncioTestCase):
    async def test_answer_with_auto_delete_can_retry_tls_record_error_once(self) -> None:
        sent_message = SimpleNamespace(message_id=87, chat=SimpleNamespace(id=-10001))
        message = SimpleNamespace(
            chat=SimpleNamespace(id=-10001),
            answer=AsyncMock(
                side_effect=[
                    TelegramNetworkError(
                        method=SimpleNamespace(),
                        message="ClientOSError: SSL bad record mac",
                    ),
                    sent_message,
                ]
            ),
        )

        with patch("bot.utils.telegram.asyncio.sleep", new=AsyncMock()) as sleep_mock:
            sent = await answer_with_auto_delete(
                message,
                "打开设置中心",
                auto_delete_seconds=0,
                retry_tls_record_error=True,
            )

        self.assertIs(sent, sent_message)
        self.assertEqual(message.answer.await_count, 2)
        sleep_mock.assert_awaited_once_with(telegram.TG_TLS_RECORD_RETRY_DELAY)

    async def test_answer_with_auto_delete_does_not_retry_tls_error_by_default(self) -> None:
        error = TelegramNetworkError(
            method=SimpleNamespace(),
            message="ClientOSError: SSL bad record mac",
        )
        message = SimpleNamespace(
            chat=SimpleNamespace(id=-10001),
            answer=AsyncMock(side_effect=error),
        )

        with patch("bot.utils.telegram.asyncio.sleep", new=AsyncMock()) as sleep_mock:
            with self.assertRaises(TelegramNetworkError):
                await answer_with_auto_delete(message, "打开设置中心", auto_delete_seconds=0)

        message.answer.assert_awaited_once()
        sleep_mock.assert_not_awaited()

    async def test_answer_with_auto_delete_does_not_retry_other_network_error(self) -> None:
        error = TelegramNetworkError(
            method=SimpleNamespace(),
            message="Request timeout error",
        )
        message = SimpleNamespace(
            chat=SimpleNamespace(id=-10001),
            answer=AsyncMock(side_effect=error),
        )

        with patch("bot.utils.telegram.asyncio.sleep", new=AsyncMock()) as sleep_mock:
            with self.assertRaises(TelegramNetworkError):
                await answer_with_auto_delete(
                    message,
                    "打开设置中心",
                    auto_delete_seconds=0,
                    retry_tls_record_error=True,
                )

        message.answer.assert_awaited_once()
        sleep_mock.assert_not_awaited()

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

        sent = await answer_with_auto_delete(message, "<内容>今天群聊总结</内容>", auto_delete_seconds=0)

        self.assertEqual(sent.message_id, 88)
        self.assertEqual(message.answer.await_count, 2)
        second_kwargs = message.answer.await_args_list[1].kwargs
        self.assertIsNone(second_kwargs["parse_mode"])
        self.assertEqual(message.answer.await_args_list[1].args[0], "&lt;内容&gt;今天群聊总结&lt;/内容&gt;")

    async def test_template_answer_keeps_body_when_keyboard_is_rejected(self) -> None:
        markup = object()
        message = SimpleNamespace(
            answer=AsyncMock(
                side_effect=[
                    TelegramBadRequest(
                        method=SimpleNamespace(),
                        message="Bad Request: BUTTON_URL_INVALID",
                    ),
                    SimpleNamespace(message_id=89, chat=SimpleNamespace(id=-10001)),
                ]
            )
        )

        sent = await answer_with_auto_delete(
            message,
            "<b>公告</b>",
            auto_delete_seconds=0,
            parse_mode="HTML",
            reply_markup=markup,
            plain_text_fallback="**公告**",
            drop_invalid_reply_markup=True,
        )

        self.assertEqual(sent.message_id, 89)
        self.assertEqual(message.answer.await_count, 2)
        self.assertNotIn("reply_markup", message.answer.await_args.kwargs)
        self.assertEqual(message.answer.await_args.args[0], "<b>公告</b>")

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
            auto_delete_seconds=0,
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

    async def test_send_reply_messages_sends_each_message_without_streaming_batch(self) -> None:
        message = SimpleNamespace()

        with patch.object(
            telegram,
            "send_reply",
            new=AsyncMock(side_effect=[True, False]),
        ) as mocked:
            results = await send_reply_messages(
                message,
                ["第一条", "第二条"],
                delivery_mode="reply",
                stream=True,
            )

        self.assertEqual(results, [True, False])
        self.assertEqual(mocked.await_count, 2)
        self.assertFalse(mocked.await_args_list[0].kwargs["stream"])
        self.assertFalse(mocked.await_args_list[1].kwargs["stream"])
        self.assertEqual(mocked.await_args_list[0].kwargs["delivery_mode"], "reply")
        self.assertEqual(mocked.await_args_list[1].kwargs["delivery_mode"], "message")

    async def test_send_reply_messages_keeps_streaming_for_single_message(self) -> None:
        message = SimpleNamespace()

        with patch.object(
            telegram,
            "send_reply",
            new=AsyncMock(return_value=True),
        ) as mocked:
            results = await send_reply_messages(
                message,
                ["只发一条"],
                delivery_mode="reply",
                stream=True,
            )

        self.assertEqual(results, [True])
        self.assertEqual(mocked.await_count, 1)
        self.assertTrue(mocked.await_args.kwargs["stream"])

    async def test_send_reply_can_use_explicit_reply_target(self) -> None:
        bot = SimpleNamespace(
            send_message=AsyncMock(return_value=SimpleNamespace(message_id=77, chat=SimpleNamespace(id=-10001)))
        )
        message = SimpleNamespace(
            chat=SimpleNamespace(id=-10001),
            message_id=999,
            bot=bot,
            reply=AsyncMock(),
            answer=AsyncMock(),
        )

        ok = await send_reply(
            message,
            "targeted reply",
            delivery_mode="reply",
            reply_to_message_id=321,
            stream=False,
            auto_delete_seconds=0,
        )

        self.assertTrue(ok)
        message.reply.assert_not_awaited()
        message.answer.assert_not_awaited()
        self.assertEqual(bot.send_message.await_args.kwargs["reply_to_message_id"], 321)

    async def test_send_reply_falls_back_to_standalone_when_explicit_target_missing(self) -> None:
        bot = SimpleNamespace(
            send_message=AsyncMock(
                side_effect=[
                    TelegramBadRequest(
                        method=SimpleNamespace(),
                        message="message to be replied not found",
                    ),
                ]
            )
        )
        message = SimpleNamespace(
            chat=SimpleNamespace(id=-10001),
            message_id=999,
            bot=bot,
            reply=AsyncMock(),
            answer=AsyncMock(return_value=SimpleNamespace(message_id=78, chat=SimpleNamespace(id=-10001))),
        )

        ok = await send_reply(
            message,
            "targeted reply",
            delivery_mode="reply",
            reply_to_message_id=321,
            stream=False,
            auto_delete_seconds=0,
        )

        self.assertTrue(ok)
        message.reply.assert_not_awaited()
        message.answer.assert_awaited_once()
