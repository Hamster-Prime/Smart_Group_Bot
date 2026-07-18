import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from bot.config import Settings
from bot.services.runtime_config import RuntimeConfig
from bot.utils.telegram import (
    AUTO_DELETE_BUTTON_SENTINEL,
    AUTO_DELETE_CATEGORIES,
    DELETE_BUTTON_CALLBACK_DATA,
    build_delete_button_markup,
    configured_auto_delete_mode,
    configured_auto_delete_seconds,
    schedule_message_auto_delete,
)


def _settings(**bot_overrides) -> Settings:
    settings = Settings(_env_file=None)
    settings.bot.auto_delete_seconds = 30
    settings.bot.auto_delete_categories = ["moderation", "vote", "call_admin"]
    settings.bot.auto_delete_category_seconds = {}
    settings.bot.auto_delete_category_mode = {}
    for key, value in bot_overrides.items():
        setattr(settings.bot, key, value)
    return settings


class AutoDeleteModeTests(unittest.TestCase):
    def test_new_categories_are_known(self) -> None:
        self.assertIn("call_admin", AUTO_DELETE_CATEGORIES)
        self.assertIn("vote", AUTO_DELETE_CATEGORIES)

    def test_mode_resolution(self) -> None:
        settings = _settings(
            auto_delete_category_mode={"moderation": "button"}
        )
        self.assertEqual(configured_auto_delete_mode(settings, "moderation"), "button")
        self.assertEqual(configured_auto_delete_mode(settings, "vote"), "timer")
        self.assertEqual(configured_auto_delete_mode(settings, "reply"), "off")
        self.assertEqual(configured_auto_delete_mode(settings, "unknown"), "off")

    def test_button_mode_returns_sentinel_not_seconds(self) -> None:
        settings = _settings(
            auto_delete_category_mode={"moderation": "button"},
            auto_delete_category_seconds={"moderation": 120},
        )
        self.assertEqual(
            configured_auto_delete_seconds(settings, "moderation"),
            AUTO_DELETE_BUTTON_SENTINEL,
        )
        # Timer categories keep their normal resolution.
        self.assertEqual(configured_auto_delete_seconds(settings, "vote"), 30)

    def test_timer_mode_unchanged(self) -> None:
        settings = _settings(auto_delete_category_seconds={"vote": 45})
        self.assertEqual(configured_auto_delete_seconds(settings, "vote"), 45)
        self.assertEqual(configured_auto_delete_seconds(settings, "call_admin"), 30)
        self.assertEqual(configured_auto_delete_seconds(settings, "reply"), 0)

    def test_delete_button_markup_appends_once(self) -> None:
        markup = build_delete_button_markup(None)
        self.assertEqual(len(markup.inline_keyboard), 1)
        self.assertEqual(
            markup.inline_keyboard[0][0].callback_data, DELETE_BUTTON_CALLBACK_DATA
        )
        stacked = build_delete_button_markup(markup)
        self.assertEqual(len(stacked.inline_keyboard), 1)


class ScheduleDispatchTests(unittest.IsolatedAsyncioTestCase):
    async def test_sentinel_attaches_button_instead_of_timer(self) -> None:
        sent = SimpleNamespace(
            chat=SimpleNamespace(id=-1),
            message_id=5,
            reply_markup=None,
            edit_reply_markup=AsyncMock(),
            delete=AsyncMock(),
        )
        schedule_message_auto_delete(sent, AUTO_DELETE_BUTTON_SENTINEL)
        await asyncio.sleep(0.05)
        sent.edit_reply_markup.assert_awaited_once()
        markup = sent.edit_reply_markup.await_args.kwargs["reply_markup"]
        self.assertEqual(
            markup.inline_keyboard[-1][0].callback_data, DELETE_BUTTON_CALLBACK_DATA
        )
        sent.delete.assert_not_awaited()

    async def test_sentinel_preserves_existing_keyboard(self) -> None:
        from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

        base = InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="旧按钮", callback_data="x:1")]
            ]
        )
        sent = SimpleNamespace(
            chat=SimpleNamespace(id=-1),
            message_id=5,
            reply_markup=base,
            edit_reply_markup=AsyncMock(),
        )
        schedule_message_auto_delete(sent, AUTO_DELETE_BUTTON_SENTINEL)
        await asyncio.sleep(0.05)
        markup = sent.edit_reply_markup.await_args.kwargs["reply_markup"]
        self.assertEqual(len(markup.inline_keyboard), 2)

    async def test_zero_and_none_are_still_noops(self) -> None:
        sent = SimpleNamespace(
            chat=SimpleNamespace(id=-1),
            message_id=5,
            edit_reply_markup=AsyncMock(),
            delete=AsyncMock(),
        )
        schedule_message_auto_delete(sent, 0)
        schedule_message_auto_delete(None, AUTO_DELETE_BUTTON_SENTINEL)
        await asyncio.sleep(0.02)
        sent.edit_reply_markup.assert_not_awaited()
        sent.delete.assert_not_awaited()


class DeleteButtonCallbackTests(unittest.IsolatedAsyncioTestCase):
    def _callback(self, *, chat_type: str = "supergroup") -> SimpleNamespace:
        return SimpleNamespace(
            data=DELETE_BUTTON_CALLBACK_DATA,
            message=SimpleNamespace(
                chat=SimpleNamespace(id=-100, type=chat_type),
                message_id=9,
                delete=AsyncMock(),
            ),
            from_user=SimpleNamespace(id=7),
            bot=SimpleNamespace(),
            answer=AsyncMock(),
        )

    async def test_admin_can_delete(self) -> None:
        from bot.handlers import group

        callback = self._callback()
        settings = _settings()
        with patch(
            "bot.handlers.group.is_group_admin_or_higher",
            new=AsyncMock(return_value=True),
        ):
            await group.on_delete_button(callback, settings, session=object())
        callback.message.delete.assert_awaited_once()
        callback.answer.assert_awaited_with("已删除")

    async def test_non_admin_refused(self) -> None:
        from bot.handlers import group

        callback = self._callback()
        settings = _settings()
        with patch(
            "bot.handlers.group.is_group_admin_or_higher",
            new=AsyncMock(return_value=False),
        ):
            await group.on_delete_button(callback, settings, session=object())
        callback.message.delete.assert_not_awaited()
        self.assertIn("管理员", callback.answer.await_args.args[0])


class RuntimeConfigModeTests(unittest.TestCase):
    def test_mode_map_accepts_and_persists_button_only(self) -> None:
        config = RuntimeConfig.model_validate(
            {
                "bot": {
                    "auto_delete_categories": ["moderation", "vote"],
                    "auto_delete_category_mode": {
                        "moderation": "button",
                        "vote": "timer",
                    },
                }
            }
        )
        self.assertEqual(
            config.bot.auto_delete_category_mode, {"moderation": "button"}
        )

    def test_new_categories_validate(self) -> None:
        config = RuntimeConfig.model_validate(
            {
                "bot": {
                    "auto_delete_categories": ["call_admin", "vote"],
                    "auto_delete_category_seconds": {"call_admin": 60, "vote": 90},
                }
            }
        )
        self.assertEqual(
            config.bot.auto_delete_category_seconds, {"call_admin": 60, "vote": 90}
        )

    def test_unknown_mode_category_rejected(self) -> None:
        with self.assertRaises(Exception):
            RuntimeConfig.model_validate(
                {"bot": {"auto_delete_category_mode": {"bogus": "button"}}}
            )

    def test_apply_to_settings_copies_mode_map(self) -> None:
        settings = Settings(_env_file=None)
        config = RuntimeConfig.model_validate(
            {
                "bot": {
                    "auto_delete_categories": ["moderation"],
                    "auto_delete_category_mode": {"moderation": "button"},
                }
            }
        )
        config.apply_to_settings(settings, apply_prompts=False)
        self.assertEqual(
            settings.bot.auto_delete_category_mode, {"moderation": "button"}
        )


if __name__ == "__main__":
    unittest.main()
