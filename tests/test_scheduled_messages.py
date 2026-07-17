import unittest
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

from bot.config import Settings
from bot.db.models import ScheduledMessage
from bot.services.scheduled_messages import (
    ScheduledMessageService,
    scheduled_message_due,
)

NOW = datetime(2026, 7, 17, 9, 30, 0)


def _entry(**overrides) -> ScheduledMessage:
    values = {
        "id": 1,
        "group_id": -10001,
        "text": "每日播报",
        "schedule_type": "daily",
        "schedule_time": "09:00",
        "interval_minutes": 60,
        "pin_message": False,
        "unpin_previous": False,
        "auto_delete": False,
        "enabled": True,
        "last_run_at": None,
        "last_message_id": 0,
        "created_at": NOW - timedelta(days=1),
    }
    values.update(overrides)
    return ScheduledMessage(**values)


def _settings() -> Settings:
    settings = Settings(_env_file=None)
    settings.bot.auto_delete_seconds = 45
    settings.bot.auto_delete_categories = ["scheduled"]
    return settings


class ScheduledMessageDueTests(unittest.TestCase):
    def test_daily_due_after_schedule_time_without_prior_run(self) -> None:
        self.assertTrue(scheduled_message_due(_entry(), now=NOW))

    def test_daily_not_due_before_schedule_time(self) -> None:
        entry = _entry(schedule_time="10:00")
        self.assertFalse(scheduled_message_due(entry, now=NOW))

    def test_daily_not_due_twice_same_day(self) -> None:
        entry = _entry(last_run_at=NOW - timedelta(minutes=10))
        self.assertFalse(scheduled_message_due(entry, now=NOW))

    def test_daily_due_again_next_day(self) -> None:
        entry = _entry(last_run_at=NOW - timedelta(days=1))
        self.assertTrue(scheduled_message_due(entry, now=NOW))

    def test_interval_due_when_interval_elapsed(self) -> None:
        entry = _entry(
            schedule_type="interval",
            interval_minutes=30,
            last_run_at=NOW - timedelta(minutes=31),
        )
        self.assertTrue(scheduled_message_due(entry, now=NOW))

    def test_interval_not_due_before_interval(self) -> None:
        entry = _entry(
            schedule_type="interval",
            interval_minutes=30,
            last_run_at=NOW - timedelta(minutes=5),
        )
        self.assertFalse(scheduled_message_due(entry, now=NOW))

    def test_interval_first_run_anchors_on_created_at(self) -> None:
        entry = _entry(
            schedule_type="interval",
            interval_minutes=30,
            created_at=NOW - timedelta(minutes=10),
        )
        self.assertFalse(scheduled_message_due(entry, now=NOW))
        entry.created_at = NOW - timedelta(minutes=31)
        self.assertTrue(scheduled_message_due(entry, now=NOW))

    def test_disabled_or_empty_text_never_due(self) -> None:
        self.assertFalse(scheduled_message_due(_entry(enabled=False), now=NOW))
        self.assertFalse(scheduled_message_due(_entry(text="  "), now=NOW))

    def test_invalid_schedule_time_never_due(self) -> None:
        self.assertFalse(scheduled_message_due(_entry(schedule_time="25:99"), now=NOW))


class _FakeSession:
    def __init__(self, rows, authorized, updated_rows=None):
        self._rows = rows
        self._authorized = authorized
        self._updated = updated_rows if updated_rows is not None else {}
        self.commit = AsyncMock()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def scalars(self, _stmt):
        return SimpleNamespace(all=lambda: list(self._rows))

    async def execute(self, _stmt):
        rows = [SimpleNamespace(group_id=g) for g in self._authorized]
        return SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: rows))

    async def get(self, _model, entry_id):
        return self._updated.get(int(entry_id))


class ScheduledMessageServiceTests(unittest.IsolatedAsyncioTestCase):
    def _service(self, rows, authorized, updated_rows=None):
        bot = SimpleNamespace(
            send_message=AsyncMock(
                return_value=SimpleNamespace(
                    message_id=901, chat=SimpleNamespace(id=-10001)
                )
            ),
            pin_chat_message=AsyncMock(),
            unpin_chat_message=AsyncMock(),
        )
        session = _FakeSession(rows, authorized, updated_rows)
        service = ScheduledMessageService(
            bot=bot,
            settings=_settings(),
            session_factory=lambda: session,
        )
        return service, bot, session

    async def test_due_entry_is_sent_and_last_run_committed_first(self) -> None:
        entry = _entry()
        service, bot, session = self._service([entry], authorized=[-10001])

        delivered = await service.run_once(now=NOW)

        self.assertEqual(delivered, 1)
        self.assertEqual(entry.last_run_at, NOW)
        session.commit.assert_awaited()
        bot.send_message.assert_awaited_once()
        self.assertEqual(bot.send_message.await_args.kwargs["chat_id"], -10001)
        bot.pin_chat_message.assert_not_awaited()

    async def test_unauthorized_group_is_skipped(self) -> None:
        entry = _entry()
        service, bot, _session = self._service([entry], authorized=[])

        delivered = await service.run_once(now=NOW)

        self.assertEqual(delivered, 0)
        bot.send_message.assert_not_awaited()
        self.assertIsNone(entry.last_run_at)

    async def test_pin_message_pins_and_records_message_id(self) -> None:
        entry = _entry(pin_message=True)
        service, bot, _session = self._service(
            [entry], authorized=[-10001], updated_rows={1: entry}
        )

        await service.run_once(now=NOW)

        bot.pin_chat_message.assert_awaited_once()
        kwargs = bot.pin_chat_message.await_args.kwargs
        self.assertEqual(kwargs["chat_id"], -10001)
        self.assertEqual(kwargs["message_id"], 901)
        self.assertEqual(entry.last_message_id, 901)

    async def test_unpin_previous_unpins_old_message(self) -> None:
        entry = _entry(pin_message=True, unpin_previous=True, last_message_id=800)
        service, bot, _session = self._service(
            [entry], authorized=[-10001], updated_rows={1: entry}
        )

        await service.run_once(now=NOW)

        bot.unpin_chat_message.assert_awaited_once()
        self.assertEqual(
            bot.unpin_chat_message.await_args.kwargs["message_id"], 800
        )

    async def test_auto_delete_uses_scheduled_category(self) -> None:
        entry = _entry(auto_delete=True)
        service, bot, _session = self._service([entry], authorized=[-10001])

        with unittest.mock.patch(
            "bot.services.scheduled_messages.schedule_message_auto_delete"
        ) as schedule_mock:
            await service.run_once(now=NOW)

        schedule_mock.assert_called_once()
        self.assertEqual(schedule_mock.call_args.args[1], 45)

    async def test_send_failure_does_not_crash_pass(self) -> None:
        entry = _entry()
        service, bot, _session = self._service([entry], authorized=[-10001])
        bot.send_message = AsyncMock(side_effect=RuntimeError("boom"))

        delivered = await service.run_once(now=NOW)

        self.assertEqual(delivered, 0)
        # last_run_at was still advanced: one missed occurrence, no retry loop.
        self.assertEqual(entry.last_run_at, NOW)

    async def test_pin_failure_keeps_delivery_success(self) -> None:
        entry = _entry(pin_message=True)
        service, bot, _session = self._service(
            [entry], authorized=[-10001], updated_rows={1: entry}
        )
        bot.pin_chat_message = AsyncMock(side_effect=RuntimeError("no rights"))

        delivered = await service.run_once(now=NOW)

        self.assertEqual(delivered, 1)
        self.assertEqual(entry.last_message_id, 0)


if __name__ == "__main__":
    unittest.main()
