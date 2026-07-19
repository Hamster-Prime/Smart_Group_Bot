import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from bot.config import Settings
from bot.handlers import commands
from bot.services import authz
from bot.utils.telegram import configured_auto_delete_seconds


def _settings(auto_delete_seconds: int = 1) -> Settings:
    settings = Settings(_env_file=None)
    settings.bot.auto_delete_seconds = auto_delete_seconds
    settings.bot.auto_delete_categories = ["management"]
    return settings


class CommandMessageRetentionTests(unittest.IsolatedAsyncioTestCase):
    async def test_category_policy_uses_seconds(self) -> None:
        settings = _settings(auto_delete_seconds=7)
        settings.bot.auto_delete_categories = ["moderation"]
        self.assertEqual(configured_auto_delete_seconds(settings, "moderation"), 7)
        self.assertEqual(configured_auto_delete_seconds(settings, "reply"), 0)

    async def test_answer_uses_explicit_auto_delete_override(self) -> None:
        settings = _settings(auto_delete_seconds=3)

        with patch("bot.handlers.commands.answer_with_auto_delete", new=AsyncMock()) as answer_mock:
            await commands._answer(SimpleNamespace(), settings, "hello", auto_delete_seconds=0)

        self.assertEqual(answer_mock.await_args.kwargs["auto_delete_seconds"], 0)

    async def test_authz_notices_honor_per_category_seconds(self) -> None:
        settings = _settings(auto_delete_seconds=0)
        settings.bot.auto_delete_category_seconds = {"management": 45}
        sent = SimpleNamespace(chat=SimpleNamespace(id=-1), message_id=5)

        with patch(
            "bot.utils.telegram.schedule_message_auto_delete_durable",
            new=AsyncMock(return_value=True),
        ) as schedule_mock:
            await authz._schedule_auto_delete(sent, settings)

        schedule_mock.assert_awaited_once_with(sent, 45)

    async def test_lm_list_reply_is_persistent(self) -> None:
        message = SimpleNamespace(
            chat=SimpleNamespace(id=-10001, type="supergroup"),
            from_user=SimpleNamespace(id=123, username="tester"),
            text="/lm",
        )
        settings = _settings()
        session = SimpleNamespace(commit=AsyncMock())

        fake_memory = SimpleNamespace(list_permanent_memories=AsyncMock(return_value=[]))
        with (
            patch("bot.handlers.commands.ensure_group_authorized", new=AsyncMock(return_value=True)),
            patch("bot.handlers.commands.ensure_group_admin_permission", new=AsyncMock(return_value=True)),
            patch("bot.handlers.commands.memory_holder.get", return_value=fake_memory),
            patch("bot.handlers.commands._answer", new=AsyncMock()) as answer_mock,
        ):
            await commands.cmd_lm(
                message,
                session=session,
                settings=settings,
                session_factory=object(),
            )

        session.commit.assert_awaited_once()
        self.assertEqual(answer_mock.await_args.kwargs["auto_delete_seconds"], 0)


if __name__ == "__main__":
    unittest.main()
