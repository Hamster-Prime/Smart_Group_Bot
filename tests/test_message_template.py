import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from aiogram.exceptions import TelegramBadRequest
from aiogram.methods import SendMessage
from bot.services.member_identity import (
    member_display_name,
    member_identity_document,
)
from bot.services.message_templates import (
    build_template_keyboard,
    normalize_template_buttons,
    render_markdown_html,
    render_plain_template,
    send_template_with_fallback,
)
from bot.utils.telegram import DELETE_BUTTON_CALLBACK_DATA


class TemplateRenderTests(unittest.TestCase):
    def test_common_markdown_is_rendered_and_raw_html_is_escaped(self) -> None:
        rendered = render_markdown_html(
            "# 标题\n**粗体**、*斜体*、[官网](https://example.com) <b>raw</b>"
        )
        self.assertIn("<b>标题</b>", rendered)
        self.assertIn("<b>粗体</b>", rendered)
        self.assertIn("<i>斜体</i>", rendered)
        self.assertIn('<a href="https://example.com">官网</a>', rendered)
        self.assertIn("&lt;b&gt;raw&lt;/b&gt;", rendered)

    def test_replacements_are_safe_html_only_in_markdown_renderer(self) -> None:
        rendered = render_markdown_html(
            "欢迎 {mention}",
            replacements={"{mention}": '<a href="tg://user?id=7">Alice</a>'},
        )
        self.assertIn('<a href="tg://user?id=7">Alice</a>', rendered)
        self.assertEqual(
            render_plain_template("欢迎 {mention}", replacements={"{mention}": "Alice"}),
            "欢迎 Alice",
        )


class TemplateKeyboardTests(unittest.TestCase):
    def test_canonicalizes_button_documents(self) -> None:
        self.assertEqual(
            normalize_template_buttons(
                [
                    {
                        "text": " 官网 ",
                        "action": "url",
                        "value": " https://example.com/a ",
                        "row": 0,
                    }
                ]
            ),
            [
                {
                    "text": "官网",
                    "action": "url",
                    "value": "https://example.com/a",
                    "row": 0,
                }
            ],
        )

    def test_builds_link_copy_and_admin_delete_buttons(self) -> None:
        markup = build_template_keyboard(
            [
                {
                    "text": "打开",
                    "action": "url",
                    "value": "https://example.com",
                    "row": 0,
                },
                {"text": "复制", "action": "copy", "value": "ABC-123", "row": 0},
                {"text": "删除", "action": "dismiss", "row": 1},
            ]
        )
        self.assertIsNotNone(markup)
        self.assertEqual(markup.inline_keyboard[0][0].url, "https://example.com")
        self.assertEqual(markup.inline_keyboard[0][1].copy_text.text, "ABC-123")
        self.assertEqual(
            markup.inline_keyboard[1][0].callback_data,
            DELETE_BUTTON_CALLBACK_DATA,
        )

    def test_rejects_unsafe_url_and_arbitrary_action(self) -> None:
        with self.assertRaises(ValueError):
            normalize_template_buttons(
                [{"text": "bad", "action": "url", "value": "javascript:alert(1)"}]
            )
        with self.assertRaises(ValueError):
            normalize_template_buttons(
                [{"text": "bad", "action": "arbitrary", "value": "owned"}]
            )

    def test_rejects_oversized_layout(self) -> None:
        with self.assertRaises(ValueError):
            normalize_template_buttons(
                [{"text": str(i), "action": "dismiss"} for i in range(13)]
            )


class MemberIdentityTests(unittest.TestCase):
    def test_display_name_fallback_order(self) -> None:
        self.assertEqual(
            member_display_name(42, full_name=" 张三 ", username="zhang"),
            "张三",
        )
        self.assertEqual(member_display_name(42, username="@zhang"), "@zhang")
        self.assertEqual(member_display_name(42), "42")

    def test_identity_document_contains_render_ready_fields(self) -> None:
        self.assertEqual(
            member_identity_document(7, username="@alice"),
            {
                "user_id": 7,
                "full_name": "",
                "username": "alice",
                "display_name": "@alice",
            },
        )


class TemplateSendFallbackTests(unittest.IsolatedAsyncioTestCase):
    async def test_keyboard_rejection_retries_body_without_buttons(self) -> None:
        send = AsyncMock(
            side_effect=[
                TelegramBadRequest(
                    method=SendMessage(chat_id=-100, text="x"),
                    message="Bad Request: BUTTON_URL_INVALID",
                ),
                SimpleNamespace(message_id=9),
            ]
        )
        keyboard = build_template_keyboard(
            [{"text": "打开", "action": "url", "value": "https://example.com"}]
        )

        sent = await send_template_with_fallback(
            send,
            formatted_text="<b>正文</b>",
            plain_text="**正文**",
            reply_markup=keyboard,
        )

        self.assertEqual(sent.message_id, 9)
        self.assertEqual(send.await_count, 2)
        self.assertEqual(send.await_args.kwargs["reply_markup"], None)
        self.assertEqual(send.await_args.kwargs["parse_mode"], "HTML")


if __name__ == "__main__":
    unittest.main()
