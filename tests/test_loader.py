import unittest

from bot.config import Settings
from bot.loader import _TELEGRAM_HTTP_TIMEOUT_SECONDS, create_bot


class BotLoaderTests(unittest.IsolatedAsyncioTestCase):
    async def test_telegram_http_session_has_a_bounded_timeout(self) -> None:
        settings = Settings(_env_file=None)
        settings.bot.token = "42:TEST_TOKEN"
        bot = create_bot(settings)
        try:
            self.assertEqual(bot.session.timeout, _TELEGRAM_HTTP_TIMEOUT_SECONDS)
            self.assertTrue(bot.default.link_preview_is_disabled)
            self.assertIsNone(bot.default.link_preview)
        finally:
            await bot.session.close()

    async def test_link_preview_default_can_be_enabled_without_conflicting_options(
        self,
    ) -> None:
        settings = Settings(_env_file=None)
        settings.bot.token = "42:TEST_TOKEN"
        settings.bot.disable_link_preview = False
        bot = create_bot(settings)
        try:
            self.assertFalse(bot.default.link_preview_is_disabled)
            self.assertIsNone(bot.default.link_preview)
        finally:
            await bot.session.close()


if __name__ == "__main__":
    unittest.main()
