import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from bot.config import Settings
from bot.handlers import commands, group
from bot.services.manage_intent import GroupIntent


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

    async def test_tasks_reply_is_persistent(self) -> None:
        message = SimpleNamespace(
            chat=SimpleNamespace(id=-10001, type="supergroup"),
            from_user=SimpleNamespace(id=123),
            text="/tasks",
        )
        settings = _settings()

        with (
            patch("bot.handlers.commands.ensure_group_authorized", new=AsyncMock(return_value=True)),
            patch("bot.handlers.commands.list_group_scheduled_tasks", new=AsyncMock(return_value=[])),
            patch("bot.handlers.commands._answer", new=AsyncMock()) as answer_mock,
        ):
            await commands.cmd_tasks(message, session=object(), settings=settings)

        self.assertEqual(answer_mock.await_args.kwargs["auto_delete_minutes"], 0)

    async def test_canceltask_reply_is_persistent(self) -> None:
        message = SimpleNamespace(
            chat=SimpleNamespace(id=-10001, type="supergroup"),
            from_user=SimpleNamespace(id=123),
            text="/canceltask abc",
        )
        settings = _settings()

        with (
            patch("bot.handlers.commands.ensure_group_authorized", new=AsyncMock(return_value=True)),
            patch("bot.handlers.commands._answer", new=AsyncMock()) as answer_mock,
        ):
            await commands.cmd_canceltask(message, session=object(), settings=settings)

        self.assertEqual(answer_mock.await_args.kwargs["auto_delete_minutes"], 0)

    async def test_av_help_reply_is_persistent(self) -> None:
        message = SimpleNamespace(
            chat=SimpleNamespace(id=-10001, type="supergroup", title="test"),
            from_user=SimpleNamespace(id=123),
            text="/av",
        )
        settings = _settings()

        with (
            patch("bot.handlers.commands.ensure_group_authorized", new=AsyncMock(return_value=True)),
            patch("bot.handlers.commands._answer", new=AsyncMock()) as answer_mock,
        ):
            await commands.cmd_av(message, session=object(), settings=settings)

        self.assertEqual(answer_mock.await_args.kwargs["auto_delete_minutes"], 0)


class GroupMessageRetentionTests(unittest.TestCase):
    def test_memory_add_reply_is_persistent(self) -> None:
        settings = _settings(auto_delete_minutes=5)
        intent = GroupIntent(intent="memory_manage", memory_action="add")

        minutes = group._management_reply_auto_delete_minutes(intent, settings)

        self.assertEqual(minutes, 0)

    def test_non_add_management_reply_keeps_default_auto_delete(self) -> None:
        settings = _settings(auto_delete_minutes=5)
        intent = GroupIntent(intent="memory_manage", memory_action="delete")

        minutes = group._management_reply_auto_delete_minutes(intent, settings)

        self.assertEqual(minutes, 5)


if __name__ == "__main__":
    unittest.main()
