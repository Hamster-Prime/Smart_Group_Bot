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
        finally:
            await bot.session.close()


if __name__ == "__main__":
    unittest.main()
