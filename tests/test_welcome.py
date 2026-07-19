import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from aiogram.exceptions import TelegramBadRequest
from aiogram.methods import SendMessage
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from bot.config import Settings
from bot.db.models import Base, Group
from bot.services.welcome import (
    group_welcome_template,
    render_welcome_text,
    send_group_welcome,
)
from bot.utils.telegram import configured_auto_delete_seconds


def _settings() -> Settings:
    settings = Settings(_env_file=None)
    settings.bot.auto_delete_seconds = 0
    settings.bot.auto_delete_categories = []
    return settings


class WelcomeTemplateTests(unittest.TestCase):
    def test_template_reads_group_settings(self) -> None:
        self.assertEqual(group_welcome_template({"welcome_message": " hi "}), "hi")
        self.assertEqual(group_welcome_template({}), "")
        self.assertEqual(group_welcome_template(None), "")

    def test_placeholders_and_html_escaping(self) -> None:
        text = render_welcome_text(
            "欢迎 {name} & {mention} <b>加入</b>",
            user_id=42,
            display_name="<Ada>",
        )
        self.assertIn("欢迎 &lt;Ada&gt;", text)
        self.assertIn('<a href="tg://user?id=42">&lt;Ada&gt;</a>', text)
        # Template HTML must be escaped, not interpreted.
        self.assertIn("&lt;b&gt;加入&lt;/b&gt;", text)
        self.assertIn("&amp;", text)

    def test_missing_display_name_falls_back_to_user_id(self) -> None:
        text = render_welcome_text("hi {name}", user_id=7, display_name="")
        self.assertIn("hi 7", text)

    def test_markdown_is_rendered_after_placeholder_substitution(self) -> None:
        text = render_welcome_text(
            "**欢迎 {name}**\n[群规](https://example.com/rules)",
            user_id=7,
            display_name="Alice",
        )
        self.assertIn("<b>欢迎 Alice</b>", text)
        self.assertIn('<a href="https://example.com/rules">群规</a>', text)


class WelcomeSendTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.session_factory = async_sessionmaker(self.engine, expire_on_commit=False)

    async def asyncTearDown(self) -> None:
        await self.engine.dispose()

    async def _add_group(self, group_id: int, settings_data: dict) -> None:
        async with self.session_factory() as session:
            session.add(Group(id=group_id, title="t", settings=settings_data))
            await session.commit()

    async def test_sends_welcome_with_category_retention(self) -> None:
        await self._add_group(
            -100,
            {
                "welcome_message": "欢迎 {name}",
                "welcome_buttons": [
                    {
                        "text": "群规",
                        "action": "url",
                        "value": "https://example.com/rules",
                        "row": 0,
                    }
                ],
            },
        )
        settings = _settings()
        settings.bot.auto_delete_categories = ["welcome"]
        settings.bot.auto_delete_category_seconds = {"welcome": 45}
        bot = SimpleNamespace(send_message=AsyncMock(return_value=SimpleNamespace()))
        with patch(
            "bot.services.welcome.schedule_message_auto_delete_durable",
            new=AsyncMock(return_value=True),
        ) as schedule:
            async with self.session_factory() as session:
                sent = await send_group_welcome(
                    bot,
                    session,
                    settings,
                    group_id=-100,
                    user_id=9,
                    display_name="Ada",
                )
        self.assertTrue(sent)
        schedule.assert_awaited_once()
        self.assertIn("欢迎 Ada", bot.send_message.await_args.args[1])
        keyboard = bot.send_message.await_args.kwargs["reply_markup"]
        self.assertEqual(keyboard.inline_keyboard[0][0].text, "群规")
        self.assertEqual(schedule.call_args.args[1], 45)

    async def test_formatted_length_error_falls_back_to_plain_markdown(self) -> None:
        await self._add_group(-100, {"welcome_message": "**欢迎**\n第二行"})
        bot = SimpleNamespace(
            send_message=AsyncMock(
                side_effect=[
                    TelegramBadRequest(
                        method=SendMessage(chat_id=-100, text="x"),
                        message="Bad Request: message is too long",
                    ),
                    SimpleNamespace(),
                ]
            )
        )
        async with self.session_factory() as session:
            sent = await send_group_welcome(
                bot,
                session,
                _settings(),
                group_id=-100,
                user_id=9,
                display_name="Ada",
            )
        self.assertTrue(sent)
        self.assertEqual(bot.send_message.await_count, 2)
        fallback = bot.send_message.await_args
        self.assertEqual(fallback.args[1], "**欢迎**\n第二行")
        self.assertIsNone(fallback.kwargs["parse_mode"])

    async def test_empty_template_sends_nothing(self) -> None:
        await self._add_group(-100, {})
        bot = SimpleNamespace(send_message=AsyncMock())
        async with self.session_factory() as session:
            sent = await send_group_welcome(
                bot,
                session,
                _settings(),
                group_id=-100,
                user_id=9,
                display_name="Ada",
            )
        self.assertFalse(sent)
        bot.send_message.assert_not_awaited()

    async def test_send_failure_returns_false(self) -> None:
        await self._add_group(-100, {"welcome_message": "hi"})
        bot = SimpleNamespace(send_message=AsyncMock(side_effect=RuntimeError("x")))
        async with self.session_factory() as session:
            sent = await send_group_welcome(
                bot,
                session,
                _settings(),
                group_id=-100,
                user_id=9,
                display_name="Ada",
            )
        self.assertFalse(sent)


class WelcomeRetentionCategoryTests(unittest.TestCase):
    def test_welcome_category_recognized(self) -> None:
        settings = _settings()
        settings.bot.auto_delete_seconds = 30
        settings.bot.auto_delete_categories = ["welcome"]
        self.assertEqual(configured_auto_delete_seconds(settings, "welcome"), 30)

    def test_per_category_seconds_override_global(self) -> None:
        settings = _settings()
        settings.bot.auto_delete_seconds = 30
        settings.bot.auto_delete_categories = ["welcome", "keyword"]
        settings.bot.auto_delete_category_seconds = {"welcome": 120}
        self.assertEqual(configured_auto_delete_seconds(settings, "welcome"), 120)
        # Categories without an override inherit the global seconds.
        self.assertEqual(configured_auto_delete_seconds(settings, "keyword"), 30)

    def test_disabled_category_ignores_override(self) -> None:
        settings = _settings()
        settings.bot.auto_delete_seconds = 30
        settings.bot.auto_delete_categories = ["keyword"]
        settings.bot.auto_delete_category_seconds = {"welcome": 120}
        self.assertEqual(configured_auto_delete_seconds(settings, "welcome"), 0)


if __name__ == "__main__":
    unittest.main()
