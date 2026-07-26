import asyncio
import html
import re
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from aiogram.exceptions import TelegramBadRequest, TelegramNetworkError

from bot.services.message_templates import render_data_brief
from bot.utils import telegram
from bot.utils.telegram import (
    ReplyMessageOverlay,
    answer_with_auto_delete,
    send_chat_message,
    send_reply,
    send_reply_messages,
)


class ScheduledSendFallbackTests(unittest.IsolatedAsyncioTestCase):
    async def test_sticker_cleanup_failure_does_not_undo_delivery(self) -> None:
        sent = SimpleNamespace(message_id=76, chat=SimpleNamespace(id=-10001))
        message = SimpleNamespace(reply_sticker=AsyncMock(return_value=sent))
        on_delivery = Mock()

        with patch(
            "bot.utils.telegram.schedule_message_auto_delete_durable",
            new=AsyncMock(side_effect=RuntimeError("cleanup unavailable")),
        ) as cleanup:
            result = await telegram.send_sticker_with_auto_delete(
                message,
                sticker="sticker-file-id",
                auto_delete_seconds=60,
                on_delivery=on_delivery,
            )

        self.assertIs(result, sent)
        message.reply_sticker.assert_awaited_once()
        on_delivery.assert_called_once_with()
        cleanup.assert_awaited_once_with(sent, 60)

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

    async def test_scheduled_markdown_keeps_mentions_and_fenced_code_valid(self) -> None:
        sent = SimpleNamespace(message_id=100, chat=SimpleNamespace(id=-10001))
        bot = SimpleNamespace(send_message=AsyncMock(return_value=sent))

        ok = await send_chat_message(
            bot,
            -10001,
            "给 @helper：\n\n```html\n<b>literal</b>\n```",
        )

        self.assertTrue(ok)
        body = bot.send_message.await_args.kwargs["text"]
        self.assertEqual(bot.send_message.await_args.kwargs["parse_mode"], "HTML")
        self.assertIn("@\u200bhelper", body)
        self.assertIn('<pre><code class="language-html">', body)
        self.assertIn("&lt;b&gt;literal&lt;/b&gt;", body)
        self.assertNotIn("&lt;code&gt;@", body)

    async def test_send_reply_cleanup_failure_does_not_retry_delivered_message(self) -> None:
        sent = SimpleNamespace(message_id=77, chat=SimpleNamespace(id=-10001))
        message = SimpleNamespace(
            chat=SimpleNamespace(id=-10001),
            answer=AsyncMock(return_value=sent),
        )
        on_delivery = Mock()

        with patch(
            "bot.utils.telegram.schedule_message_auto_delete_durable",
            new=AsyncMock(side_effect=RuntimeError("cleanup unavailable")),
        ) as cleanup:
            ok = await send_reply(
                message,
                "已经发出的内容",
                delivery_mode="message",
                auto_delete_seconds=60,
                on_delivery=on_delivery,
            )

        self.assertTrue(ok)
        message.answer.assert_awaited_once()
        on_delivery.assert_called_once_with()
        cleanup.assert_awaited_once_with(sent, 60)

    async def test_send_chat_cleanup_failure_does_not_retry_delivered_message(self) -> None:
        sent = SimpleNamespace(message_id=78, chat=SimpleNamespace(id=-10001))
        bot = SimpleNamespace(send_message=AsyncMock(return_value=sent))
        on_delivery = Mock()

        with patch(
            "bot.utils.telegram.schedule_message_auto_delete_durable",
            new=AsyncMock(side_effect=RuntimeError("cleanup unavailable")),
        ) as cleanup:
            ok = await send_chat_message(
                bot,
                -10001,
                "已经发出的主动消息",
                auto_delete_seconds=60,
                on_delivery=on_delivery,
            )

        self.assertTrue(ok)
        bot.send_message.assert_awaited_once()
        on_delivery.assert_called_once_with()
        cleanup.assert_awaited_once_with(sent, 60)

    async def test_multipart_reply_confirms_first_delivery_when_later_part_fails(self) -> None:
        sent = SimpleNamespace(message_id=79, chat=SimpleNamespace(id=-10001))
        parse_error = TelegramBadRequest(
            method=SimpleNamespace(),
            message="Bad Request: can't parse entities",
        )
        message = SimpleNamespace(
            chat=SimpleNamespace(id=-10001),
            answer=AsyncMock(
                side_effect=[sent, parse_error, parse_error, parse_error]
            ),
        )
        on_delivery = Mock()

        with (
            patch(
                "bot.utils.telegram._split_for_telegram",
                return_value=["第一段", "第二段"],
            ),
            patch(
                "bot.utils.telegram.schedule_message_auto_delete_durable",
                new=AsyncMock(return_value=True),
            ) as cleanup,
        ):
            ok = await send_reply(
                message,
                "第一段第二段",
                delivery_mode="message",
                on_delivery=on_delivery,
        )

        self.assertFalse(ok)
        self.assertEqual(message.answer.await_count, 3)
        on_delivery.assert_called_once_with()
        cleanup.assert_awaited_once_with(sent, 0)

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

    async def test_streaming_sends_pre_rendered_data_brief_directly_as_html(self) -> None:
        rendered = render_data_brief(
            "永久记忆",
            metadata={"总数": "<code>1</code> 条"},
            items=(
                '<b>1.</b> <a href="https://example.com/search?q=_alice_">'
                "群规优先</a>"
            ),
        )
        sent = SimpleNamespace(
            message_id=77,
            chat=SimpleNamespace(id=-10001),
            edit_text=AsyncMock(),
        )
        message = SimpleNamespace(
            chat=SimpleNamespace(id=-10001),
            bot=SimpleNamespace(send_message=AsyncMock()),
            reply=AsyncMock(return_value=sent),
            answer=AsyncMock(),
        )

        ok = await send_reply(
            message,
            rendered,
            delivery_mode="reply",
            stream=True,
            stream_interval=0,
            auto_delete_seconds=0,
        )

        self.assertTrue(ok)
        message.reply.assert_awaited_once_with(rendered, parse_mode="HTML")
        sent.edit_text.assert_not_awaited()

    async def test_pre_rendered_stream_uses_plain_text_fallback(self) -> None:
        rendered = render_data_brief(
            "搜索结果",
            items="<b>1.</b> result",
        )
        sent = SimpleNamespace(message_id=77, chat=SimpleNamespace(id=-10001))
        message = SimpleNamespace(
            chat=SimpleNamespace(id=-10001),
            bot=SimpleNamespace(send_message=AsyncMock()),
            reply=AsyncMock(
                side_effect=[
                    TelegramBadRequest(
                        method=SimpleNamespace(),
                        message="Bad Request: can't parse entities",
                    ),
                    sent,
                ]
            ),
            answer=AsyncMock(),
        )

        ok = await send_reply(
            message,
            rendered,
            delivery_mode="reply",
            stream=True,
            stream_interval=0,
            auto_delete_seconds=0,
        )

        self.assertTrue(ok)
        self.assertEqual(message.reply.await_count, 2)
        self.assertEqual(message.reply.await_args_list[0].kwargs["parse_mode"], "HTML")
        fallback_call = message.reply.await_args_list[1]
        self.assertIsNone(fallback_call.kwargs["parse_mode"])
        self.assertNotIn("<b>", fallback_call.args[0])

    async def test_html_entities_are_not_split_by_raw_source_length(self) -> None:
        rendered = render_data_brief(
            "搜索结果",
            items="&amp;" * 900,
        )
        self.assertGreater(len(rendered), telegram.TG_STREAM_SAFE_LIMIT)
        sent = SimpleNamespace(message_id=77, chat=SimpleNamespace(id=-10001))
        message = SimpleNamespace(
            chat=SimpleNamespace(id=-10001),
            bot=SimpleNamespace(send_message=AsyncMock()),
            reply=AsyncMock(return_value=sent),
            answer=AsyncMock(),
        )

        ok = await send_reply(message, rendered, stream=True)

        self.assertTrue(ok)
        message.reply.assert_awaited_once_with(rendered, parse_mode="HTML")

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


class TelegramMarkdownOutputTests(unittest.TestCase):
    def test_sanitizer_preserves_fenced_code_whitespace(self) -> None:
        source = (
            "配置如下：\n\n"
            "```yaml\n"
            "dns:\n"
            "  default-nameserver:\n"
            "    - 223.5.5.5\n"
            "\n"
            "  fallback:\n"
            "    - https://example.com/dns-query\n"
            "```\n\n"
            "请检查。"
        )

        cleaned = telegram.sanitize_outgoing_text(source)

        self.assertIn(
            "```yaml\n"
            "dns:\n"
            "  default-nameserver:\n"
            "    - 223.5.5.5\n"
            "\n"
            "  fallback:\n"
            "    - https://example.com/dns-query\n"
            "```",
            cleaned,
        )

    def test_markdown_renderer_escapes_raw_html_and_code(self) -> None:
        rendered = telegram.md_to_html(
            "**配置** <b>raw</b> & value\n\n"
            "```yaml\n  item: <unsafe> & value\n\n    nested: true\n```"
        )

        self.assertIn("<b>配置</b>", rendered)
        self.assertIn("&lt;b&gt;raw&lt;/b&gt; &amp; value", rendered)
        self.assertIn('<pre><code class="language-yaml">', rendered)
        self.assertIn("  item: &lt;unsafe&gt; &amp; value", rendered)
        self.assertIn("\n\n    nested: true\n", rendered)
        self.assertIn("</code></pre>", rendered)

    async def test_rich_send_uses_send_rich_message_with_markdown(self) -> None:
        sent = SimpleNamespace(message_id=91, chat=SimpleNamespace(id=-10001))
        message = SimpleNamespace(
            chat=SimpleNamespace(id=-10001),
            message_id=42,
            bot=SimpleNamespace(send_rich_message=AsyncMock(return_value=sent)),
        )

        ok = await send_reply(
            message,
            "| A | B |\n|---|---|\n| 1 | 2 |",
            delivery_mode="reply",
            rich=True,
            stream=True,
        )

        self.assertTrue(ok)
        message.bot.send_rich_message.assert_awaited_once()
        kwargs = message.bot.send_rich_message.await_args.kwargs
        self.assertEqual(kwargs["chat_id"], -10001)
        self.assertIn("| A | B |", kwargs["rich_message"].markdown)
        self.assertTrue(kwargs["rich_message"].skip_entity_detection)
        self.assertEqual(kwargs["reply_parameters"].message_id, 42)

    async def test_rich_send_failure_falls_back_to_html(self) -> None:
        sent = SimpleNamespace(message_id=92, chat=SimpleNamespace(id=-10001))
        message = SimpleNamespace(
            chat=SimpleNamespace(id=-10001),
            message_id=43,
            bot=SimpleNamespace(
                send_rich_message=AsyncMock(
                    side_effect=TelegramBadRequest(
                        method=SimpleNamespace(),
                        message="Bad Request: method not available",
                    )
                )
            ),
            reply=AsyncMock(return_value=sent),
        )

        ok = await send_reply(
            message,
            "# 标题\n\n**加粗**",
            delivery_mode="reply",
            rich=True,
        )

        self.assertTrue(ok)
        message.reply.assert_awaited()
        body, kwargs = message.reply.await_args.args, message.reply.await_args.kwargs
        self.assertEqual(body[0], "<b>标题</b>\n\n<b>加粗</b>")
        self.assertEqual(kwargs["parse_mode"], "HTML")

    async def test_plain_chat_reply_skips_rich_message_path(self) -> None:
        sent = SimpleNamespace(message_id=93, chat=SimpleNamespace(id=-10001))
        rich_mock = AsyncMock()
        message = SimpleNamespace(
            chat=SimpleNamespace(id=-10001),
            message_id=44,
            bot=SimpleNamespace(send_rich_message=rich_mock),
            reply=AsyncMock(return_value=sent),
        )

        ok = await send_reply(
            message,
            "好呀好呀，**明天见**~",
            delivery_mode="reply",
            rich=True,
        )

        self.assertTrue(ok)
        rich_mock.assert_not_awaited()
        message.reply.assert_awaited()

    def test_normalize_block_layout_adds_breathing_room(self) -> None:
        source = (
            "结论如下：\n"
            "# 对比\n"
            "| A | B |\n"
            "|---|---|\n"
            "| 1 | 2 |\n"
            "总结一下\n"
            "- 第一点\n"
            "- 第二点\n"
            "结束"
        )

        result = telegram.normalize_block_layout(source)

        self.assertIn("结论如下：\n\n# 对比\n\n| A | B |", result)
        self.assertIn("| 1 | 2 |\n\n总结一下\n\n- 第一点\n- 第二点\n\n结束", result)
        self.assertNotIn("\n\n\n", result)

    def test_normalize_block_layout_leaves_code_untouched(self) -> None:
        source = "说明\n```yaml\nkey: 1\n# comment\n- item\n```\n结束"

        result = telegram.normalize_block_layout(source)

        self.assertIn("```yaml\nkey: 1\n# comment\n- item\n```", result)
        self.assertIn("说明\n\n```yaml", result)
        self.assertIn("```\n\n结束", result)

    def test_markdown_renderer_supports_rich_telegram_formats(self) -> None:
        rendered = telegram.md_to_html(
            "__也是加粗__ ~~删除~~ ||剧透|| **加粗** *斜体*\n"
            "> 第一行\n"
            "> 第二行\n"
            "普通行\n"
            ">! 折叠一\n"
            ">! 折叠二"
        )

        self.assertIn("<b>也是加粗</b>", rendered)
        self.assertIn("<s>删除</s>", rendered)
        self.assertIn("<tg-spoiler>剧透</tg-spoiler>", rendered)
        self.assertIn("<b>加粗</b>", rendered)
        self.assertIn("<i>斜体</i>", rendered)
        self.assertIn("<blockquote>第一行\n第二行</blockquote>", rendered)
        self.assertIn("<blockquote expandable>折叠一\n折叠二</blockquote>", rendered)
        self.assertNotIn("<blockquote>第一行</blockquote>", rendered)

    def test_spoiler_marker_inside_code_stays_literal(self) -> None:
        rendered = telegram.md_to_html("`a || b` 和 ||隐藏||")

        self.assertIn("<code>a || b</code>", rendered)
        self.assertIn("<tg-spoiler>隐藏</tg-spoiler>", rendered)

    def test_sanitizers_leave_code_literals_unchanged(self) -> None:
        source = (
            "示例：\n\n"
            "~~~~text\n"
            "@example  <think>literal</think>\n"
            "  <b>not html</b>\n"
            "  [literal](tg://user?id=42)\n"
            "~~~~"
        )

        cleaned = telegram.sanitize_outgoing_text(source)
        cleaned = telegram.sanitize_outgoing_mentions(cleaned, monospace=False)

        self.assertEqual(cleaned, source)

    def test_split_uses_utf16_units(self) -> None:
        parts = telegram._split_for_telegram("😀" * 11, limit=10)

        self.assertEqual("".join(parts), "😀" * 11)
        self.assertEqual([telegram._utf16_units(part) for part in parts], [10, 10, 2])

    def test_long_fenced_code_is_closed_and_reopened_per_part(self) -> None:
        code_lines = [f"  key_{index}: value_{index}\n" for index in range(12)]
        source = "```yaml\n" + "".join(code_lines) + "```"

        parts = telegram._split_for_telegram(source, limit=72)

        self.assertGreater(len(parts), 1)
        for part in parts:
            self.assertLessEqual(telegram._utf16_units(part), 72)
            self.assertTrue(part.startswith("```yaml\n"))
            self.assertTrue(part.endswith("```"))
            rendered = telegram.md_to_html(part)
            self.assertEqual(rendered.count("<pre>"), 1)
            self.assertEqual(rendered.count("</pre>"), 1)
        for line in code_lines:
            self.assertEqual(sum(line.rstrip("\n") in part for part in parts), 1)

    def test_long_single_line_code_delivery_does_not_insert_newlines(self) -> None:
        code = ("value<&>@literal" * 20) + "😀"
        source = f"```text\n{code}\n```"

        parts = telegram._split_for_telegram(source, limit=72)
        delivered_code: list[str] = []
        for part in parts:
            rendered = telegram.md_to_html(part)
            match = re.fullmatch(
                r'<pre><code class="language-text">(.*)</code></pre>',
                rendered,
                flags=re.DOTALL,
            )
            self.assertIsNotNone(match)
            delivered_code.append(html.unescape(match.group(1)))

        self.assertEqual("".join(delivered_code), code + "\n")
        self.assertNotIn("\n", "".join(delivered_code)[:-1])


class TelegramMarkdownDeliveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_progress_overlay_and_final_body_share_one_message_then_strip(
        self,
    ) -> None:
        progress_sent = SimpleNamespace(
            message_id=89,
            chat=SimpleNamespace(id=-10001),
            edit_text=AsyncMock(),
        )
        message = SimpleNamespace(
            message_id=321,
            chat=SimpleNamespace(id=-10001),
            bot=SimpleNamespace(send_message=AsyncMock()),
            reply=AsyncMock(),
            answer=AsyncMock(),
        )
        overlay = ReplyMessageOverlay(
            message=progress_sent,
            status_html=(
                "<blockquote>01　已理解问题\n"
                "02　已整理并发送回答</blockquote>"
            ),
            reply_to_message_id=321,
            sent_as_reply=True,
        )
        source = (
            "正文自己的引用：\n> 不应被状态清理误删\n\n"
            "```html\n<b>@literal</b>\n```"
        )

        ok = await send_reply(
            message,
            source,
            stream=True,
            overlay=overlay,
            overlay_remove_after=0.01,
        )
        await asyncio.sleep(0.04)

        self.assertTrue(ok)
        self.assertEqual(overlay.outcome, "attached")
        message.reply.assert_not_awaited()
        message.answer.assert_not_awaited()
        self.assertGreaterEqual(progress_sent.edit_text.await_count, 2)
        combined = progress_sent.edit_text.await_args_list[0].args[0]
        final_body = progress_sent.edit_text.await_args_list[-1].args[0]
        self.assertTrue(combined.startswith(overlay.status_html + "\n\n"))
        self.assertEqual(
            final_body,
            telegram.md_to_html(telegram.normalize_block_layout(source)),
        )
        self.assertIn("<blockquote>不应被状态清理误删</blockquote>", final_body)
        self.assertIn("&lt;b&gt;@literal&lt;/b&gt;", final_body)
        self.assertNotIn("已理解问题", final_body)

    async def test_incompatible_overlay_keeps_normal_delivery_path(self) -> None:
        progress_sent = SimpleNamespace(
            message_id=88,
            chat=SimpleNamespace(id=-10001),
            edit_text=AsyncMock(),
        )
        final_sent = SimpleNamespace(message_id=90, chat=SimpleNamespace(id=-10001))
        message = SimpleNamespace(
            message_id=321,
            chat=SimpleNamespace(id=-10001),
            bot=SimpleNamespace(send_message=AsyncMock()),
            reply=AsyncMock(),
            answer=AsyncMock(return_value=final_sent),
        )
        overlay = ReplyMessageOverlay(
            message=progress_sent,
            status_html="<blockquote>01　已理解问题</blockquote>",
            reply_to_message_id=321,
            sent_as_reply=True,
        )

        ok = await send_reply(
            message,
            "普通正文",
            delivery_mode="message",
            overlay=overlay,
        )

        self.assertTrue(ok)
        self.assertEqual(overlay.outcome, "pending")
        progress_sent.edit_text.assert_not_awaited()
        message.answer.assert_awaited_once()

    async def test_overlay_is_skipped_when_it_would_overflow_one_final_message(
        self,
    ) -> None:
        progress_sent = SimpleNamespace(
            message_id=86,
            chat=SimpleNamespace(id=-10001),
            edit_text=AsyncMock(),
        )
        final_sent = SimpleNamespace(message_id=91, chat=SimpleNamespace(id=-10001))
        message = SimpleNamespace(
            message_id=321,
            chat=SimpleNamespace(id=-10001),
            bot=SimpleNamespace(send_message=AsyncMock()),
            reply=AsyncMock(return_value=final_sent),
            answer=AsyncMock(),
        )
        overlay = ReplyMessageOverlay(
            message=progress_sent,
            status_html="<blockquote>" + ("状态" * 80) + "</blockquote>",
            reply_to_message_id=321,
            sent_as_reply=True,
        )
        body = "x" * 4000

        ok = await send_reply(message, body, overlay=overlay)

        self.assertTrue(ok)
        self.assertEqual(overlay.outcome, "pending")
        progress_sent.edit_text.assert_not_awaited()
        message.reply.assert_awaited_once()
        self.assertEqual(message.reply.await_args.args[0], body)

    async def test_ambiguous_overlay_edit_never_sends_duplicate_message(self) -> None:
        progress_sent = SimpleNamespace(
            message_id=87,
            chat=SimpleNamespace(id=-10001),
            edit_text=AsyncMock(side_effect=RuntimeError("network timeout")),
        )
        message = SimpleNamespace(
            message_id=321,
            chat=SimpleNamespace(id=-10001),
            bot=SimpleNamespace(send_message=AsyncMock()),
            reply=AsyncMock(),
            answer=AsyncMock(),
        )
        overlay = ReplyMessageOverlay(
            message=progress_sent,
            status_html="<blockquote>01　已理解问题</blockquote>",
            reply_to_message_id=321,
            sent_as_reply=True,
        )
        on_ambiguous = Mock()

        with patch("bot.utils.telegram.asyncio.sleep", new=AsyncMock()):
            ok = await send_reply(
                message,
                "最终正文",
                overlay=overlay,
                on_ambiguous=on_ambiguous,
            )
        await asyncio.sleep(0.01)

        self.assertFalse(ok)
        self.assertEqual(overlay.outcome, "ambiguous")
        on_ambiguous.assert_called_once_with()
        message.reply.assert_not_awaited()
        message.answer.assert_not_awaited()

    async def test_ambiguous_overlay_converges_on_same_message_id(self) -> None:
        attempts = 0

        async def edit_text(body: str, **_kwargs: object) -> None:
            nonlocal attempts
            attempts += 1
            if attempts <= 3:
                raise RuntimeError("network result unknown")
            self.assertEqual(body, telegram.md_to_html("**最终正文**"))

        progress_sent = SimpleNamespace(
            message_id=85,
            chat=SimpleNamespace(id=-10001),
            edit_text=AsyncMock(side_effect=edit_text),
        )
        message = SimpleNamespace(
            message_id=321,
            chat=SimpleNamespace(id=-10001),
            bot=SimpleNamespace(send_message=AsyncMock()),
            reply=AsyncMock(),
            answer=AsyncMock(),
        )
        overlay = ReplyMessageOverlay(
            message=progress_sent,
            status_html="<blockquote>01　已理解问题</blockquote>",
            reply_to_message_id=321,
            sent_as_reply=True,
        )
        delivered = Mock()
        ambiguous = Mock()

        ok = await send_reply(
            message,
            "**最终正文**",
            overlay=overlay,
            overlay_remove_after=0.01,
            on_delivery=delivered,
            on_ambiguous=ambiguous,
        )
        await asyncio.sleep(0.05)

        self.assertFalse(ok)
        self.assertEqual(overlay.outcome, "ambiguous")
        ambiguous.assert_called_once_with()
        delivered.assert_called_once_with()
        self.assertEqual(progress_sent.edit_text.await_count, 4)
        message.reply.assert_not_awaited()
        message.answer.assert_not_awaited()

    async def test_overlay_deadline_marks_ambiguous_and_reconciles_body(self) -> None:
        first_call = True

        async def edit_text(body: str, **_kwargs: object) -> None:
            nonlocal first_call
            if first_call:
                first_call = False
                await asyncio.sleep(0.1)
            self.assertEqual(body, telegram.md_to_html("最终正文"))

        progress_sent = SimpleNamespace(
            message_id=84,
            chat=SimpleNamespace(id=-10001),
            edit_text=AsyncMock(side_effect=edit_text),
        )
        message = SimpleNamespace(
            message_id=321,
            chat=SimpleNamespace(id=-10001),
            bot=SimpleNamespace(send_message=AsyncMock()),
            reply=AsyncMock(),
            answer=AsyncMock(),
        )
        overlay = ReplyMessageOverlay(
            message=progress_sent,
            status_html="<blockquote>01　已理解问题</blockquote>",
            reply_to_message_id=321,
            sent_as_reply=True,
        )
        ambiguous = Mock()

        with patch("bot.utils.telegram._send_total_deadline_seconds", return_value=0.01):
            ok = await send_reply(
                message,
                "最终正文",
                overlay=overlay,
                overlay_remove_after=0.01,
                on_ambiguous=ambiguous,
            )
        await asyncio.sleep(0.05)

        self.assertFalse(ok)
        self.assertEqual(overlay.outcome, "ambiguous")
        ambiguous.assert_called_once_with()
        self.assertEqual(progress_sent.edit_text.await_count, 2)
        self.assertEqual(
            progress_sent.edit_text.await_args_list[-1].args[0],
            telegram.md_to_html("最终正文"),
        )
        message.reply.assert_not_awaited()
        message.answer.assert_not_awaited()

    async def test_multipart_single_line_code_is_delivered_byte_exact(self) -> None:
        sent = SimpleNamespace(message_id=90, chat=SimpleNamespace(id=-10001))
        message = SimpleNamespace(
            chat=SimpleNamespace(id=-10001),
            bot=SimpleNamespace(send_message=AsyncMock()),
            reply=AsyncMock(return_value=sent),
            answer=AsyncMock(return_value=sent),
        )
        code = "A<&>@literal" * 20

        with patch.object(telegram, "TG_MESSAGE_LIMIT", 72):
            ok = await send_reply(
                message,
                f"```text\n{code}\n```",
                stream=True,
            )

        self.assertTrue(ok)
        delivered_code: list[str] = []
        for call in message.reply.await_args_list:
            self.assertEqual(call.kwargs["parse_mode"], "HTML")
            match = re.fullmatch(
                r'<pre><code class="language-text">(.*)</code></pre>',
                call.args[0],
                flags=re.DOTALL,
            )
            self.assertIsNotNone(match)
            delivered_code.append(html.unescape(match.group(1)))
        self.assertGreater(len(delivered_code), 1)
        self.assertEqual("".join(delivered_code), code + "\n")

    async def test_fenced_html_is_delivered_as_code_not_trusted_html(self) -> None:
        sent = SimpleNamespace(message_id=91, chat=SimpleNamespace(id=-10001))
        message = SimpleNamespace(
            chat=SimpleNamespace(id=-10001),
            bot=SimpleNamespace(send_message=AsyncMock()),
            reply=AsyncMock(return_value=sent),
            answer=AsyncMock(),
        )
        source = "示例：\n\n```html\n<b>hello</b>\n```"

        ok = await send_reply(message, source, stream=True)

        self.assertTrue(ok)
        message.reply.assert_awaited_once()
        body = message.reply.await_args.args[0]
        self.assertEqual(message.reply.await_args.kwargs["parse_mode"], "HTML")
        self.assertIn('<pre><code class="language-html">', body)
        self.assertIn("&lt;b&gt;hello&lt;/b&gt;", body)
        self.assertNotIn("```", body)

    async def test_mention_sanitizing_does_not_disable_fenced_code_rendering(self) -> None:
        sent = SimpleNamespace(message_id=92, chat=SimpleNamespace(id=-10001))
        message = SimpleNamespace(
            chat=SimpleNamespace(id=-10001),
            bot=SimpleNamespace(send_message=AsyncMock()),
            reply=AsyncMock(return_value=sent),
            answer=AsyncMock(),
        )
        source = "给 @helper：\n\n```yaml\nname: @literal\n```"

        ok = await send_reply(message, source, stream=False)

        self.assertTrue(ok)
        body = message.reply.await_args.args[0]
        self.assertIn("@\u200bhelper", body)
        self.assertIn("name: @literal", body)
        self.assertIn("<pre><code", body)
        self.assertNotIn("&lt;code&gt;", body)
