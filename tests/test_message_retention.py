import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from bot.config import Settings
from bot.handlers import commands


def _settings(auto_delete_minutes: int = 1) -> Settings:
    settings = Settings(_env_file=None)
    settings.bot.auto_delete_minutes = auto_delete_minutes
    return settings


class CommandMessageRetentionTests(unittest.IsolatedAsyncioTestCase):
    async def test_answer_uses_explicit_auto_delete_override(self) -> None:
        settings = _settings(auto_delete_minutes=3)

        with patch("bot.handlers.commands.answer_with_auto_delete", new=AsyncMock()) as answer_mock:
            await commands._answer(SimpleNamespace(), settings, "hello", auto_delete_minutes=0)

        self.assertEqual(answer_mock.await_args.kwargs["auto_delete_minutes"], 0)

    async def test_lm_list_reply_is_persistent(self) -> None:
        message = SimpleNamespace(
            chat=SimpleNamespace(id=-10001, type="supergroup"),
            from_user=SimpleNamespace(id=123, username="tester"),
            text="/lm",
        )
        settings = _settings()

        fake_memory = SimpleNamespace(list_permanent_memories=AsyncMock(return_value=[]))
        with (
            patch("bot.handlers.commands.ensure_group_authorized", new=AsyncMock(return_value=True)),
            patch("bot.handlers.commands.ensure_group_admin_permission", new=AsyncMock(return_value=True)),
            patch("bot.handlers.commands.memory_holder.get", return_value=fake_memory),
            patch("bot.handlers.commands._answer", new=AsyncMock()) as answer_mock,
        ):
            await commands.cmd_lm(message, session=object(), settings=settings)

        self.assertEqual(answer_mock.await_args.kwargs["auto_delete_minutes"], 0)


if __name__ == "__main__":
    unittest.main()
