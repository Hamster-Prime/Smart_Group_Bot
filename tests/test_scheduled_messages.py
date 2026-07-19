import asyncio
import os
import tempfile
import unittest
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

from aiogram.exceptions import TelegramBadRequest
from aiogram.methods import SendMessage
from sqlalchemy import select, update
from bot.config import Settings
from bot.db.engine import init_db
from bot.db.models import (
    AuthorizedGroup,
    ScheduledMessage,
    ScheduledMessageOccurrence,
)
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
        "buttons": [],
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
        explicit = self._updated.get(int(entry_id))
        if explicit is not None:
            return explicit
        return next(
            (row for row in self._rows if int(row.id) == int(entry_id)),
            None,
        )


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
        # Delivery-format unit tests keep a tiny in-memory occurrence adapter;
        # SQLite integration tests below exercise the real atomic claim/lease
        # implementation.
        pending: dict[tuple[int, datetime], object] = {}

        async def persist_occurrences(occurrences):
            for entry_id, occurrence_at in occurrences:
                pending.setdefault((int(entry_id), occurrence_at), object())

        async def claim_occurrence(*, authorized_groups, now):
            if not pending:
                return None
            entry_id, occurrence_at = sorted(pending)[0]
            pending.pop((entry_id, occurrence_at), None)
            entry = next(row for row in rows if int(row.id) == int(entry_id))
            return SimpleNamespace(
                occurrence_id=entry_id,
                occurrence_at=occurrence_at,
                lease_until=now + timedelta(minutes=5),
                attempts=1,
                entry=entry,
            )

        async def complete_occurrence(claimed, *, completed_at):
            claimed.entry.last_run_at = completed_at
            await session.commit()
            return True

        service._persist_due_occurrences = persist_occurrences
        service._claim_next_occurrence = claim_occurrence
        service._complete_occurrence = complete_occurrence
        service._retry_occurrence = AsyncMock()
        return service, bot, session

    async def test_due_entry_is_marked_only_after_successful_send(self) -> None:
        entry = _entry()
        service, bot, session = self._service([entry], authorized=[-10001])
        events: list[str] = []
        bot.send_message.side_effect = lambda **_kwargs: (
            events.append("send")
            or SimpleNamespace(message_id=901, chat=SimpleNamespace(id=-10001))
        )
        session.commit.side_effect = lambda: events.append("commit")

        delivered = await service.run_once(now=NOW)

        self.assertEqual(delivered, 1)
        self.assertEqual(entry.last_run_at, NOW)
        session.commit.assert_awaited()
        self.assertEqual(events, ["send", "commit"])
        bot.send_message.assert_awaited_once()
        self.assertEqual(bot.send_message.await_args.kwargs["chat_id"], -10001)
        self.assertEqual(bot.send_message.await_args.kwargs["parse_mode"], "HTML")
        bot.pin_chat_message.assert_not_awaited()

    async def test_markdown_and_inline_button_are_rendered(self) -> None:
        entry = _entry(
            text="**公告**\n第二行",
            buttons=[
                {
                    "text": "复制",
                    "action": "copy",
                    "value": "CODE",
                    "row": 0,
                }
            ],
        )
        service, bot, _session = self._service([entry], authorized=[-10001])

        await service.run_once(now=NOW)

        kwargs = bot.send_message.await_args.kwargs
        self.assertIn("<b>公告</b>\n第二行", kwargs["text"])
        self.assertEqual(
            kwargs["reply_markup"].inline_keyboard[0][0].copy_text.text,
            "CODE",
        )

    async def test_formatted_length_error_retries_plain_text(self) -> None:
        entry = _entry(text="**公告**\n第二行")
        service, bot, _session = self._service([entry], authorized=[-10001])
        bot.send_message.side_effect = [
            TelegramBadRequest(
                method=SendMessage(chat_id=-10001, text="x"),
                message="Bad Request: message is too long",
            ),
            SimpleNamespace(message_id=901, chat=SimpleNamespace(id=-10001)),
        ]

        delivered = await service.run_once(now=NOW)

        self.assertEqual(delivered, 1)
        self.assertEqual(bot.send_message.await_count, 2)
        self.assertEqual(bot.send_message.await_args.kwargs["text"], "**公告**\n第二行")
        self.assertIsNone(bot.send_message.await_args.kwargs["parse_mode"])

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
            "bot.services.scheduled_messages.schedule_message_auto_delete_durable",
            new=AsyncMock(return_value=True),
        ) as schedule_mock:
            await service.run_once(now=NOW)

        schedule_mock.assert_awaited_once()
        self.assertEqual(schedule_mock.call_args.args[1], 45)

    async def test_send_failure_does_not_crash_pass(self) -> None:
        entry = _entry()
        service, bot, _session = self._service([entry], authorized=[-10001])
        bot.send_message = AsyncMock(side_effect=RuntimeError("boom"))

        delivered = await service.run_once(now=NOW)

        self.assertEqual(delivered, 0)
        self.assertIsNone(entry.last_run_at)

    async def test_send_failure_is_retried_on_next_pass(self) -> None:
        entry = _entry()
        service, bot, _session = self._service([entry], authorized=[-10001])
        bot.send_message.side_effect = [
            RuntimeError("temporary"),
            SimpleNamespace(message_id=902, chat=SimpleNamespace(id=-10001)),
        ]

        self.assertEqual(await service.run_once(now=NOW), 0)
        self.assertIsNone(entry.last_run_at)
        self.assertEqual(await service.run_once(now=NOW), 1)
        self.assertEqual(entry.last_run_at, NOW)
        self.assertEqual(bot.send_message.await_count, 2)

    async def test_persistent_send_failure_surfaces_to_supervisor(self) -> None:
        entry = _entry()
        service, bot, _session = self._service([entry], authorized=[-10001])
        bot.send_message.side_effect = RuntimeError("persistent")

        with self.assertRaisesRegex(RuntimeError, "failed for all"):
            await service.run_once(now=NOW, raise_on_total_failure=True)
        self.assertIsNone(entry.last_run_at)

    async def test_overlapping_passes_do_not_send_same_occurrence_twice(self) -> None:
        entry = _entry()
        service, bot, _session = self._service([entry], authorized=[-10001])
        release = asyncio.Event()

        async def slow_send(**_kwargs):
            await release.wait()
            return SimpleNamespace(message_id=903, chat=SimpleNamespace(id=-10001))

        bot.send_message.side_effect = slow_send
        first = asyncio.create_task(service.run_once(now=NOW))
        await asyncio.sleep(0)
        second = asyncio.create_task(service.run_once(now=NOW))
        await asyncio.sleep(0)
        release.set()

        self.assertEqual(await first, 1)
        self.assertEqual(await second, 0)
        self.assertEqual(bot.send_message.await_count, 1)

    async def test_pin_failure_keeps_delivery_success(self) -> None:
        entry = _entry(pin_message=True)
        service, bot, _session = self._service(
            [entry], authorized=[-10001], updated_rows={1: entry}
        )
        bot.pin_chat_message = AsyncMock(side_effect=RuntimeError("no rights"))

        delivered = await service.run_once(now=NOW)

        self.assertEqual(delivered, 1)
        self.assertEqual(entry.last_message_id, 0)


class ScheduledMessageDurabilityTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        fd, self.path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.engine, self.session_factory = await init_db(
            f"sqlite+aiosqlite:///{self.path}"
        )
        async with self.session_factory() as session:
            session.add(AuthorizedGroup(group_id=-10001, authorized_by=1))
            session.add(_entry())
            await session.commit()

    async def asyncTearDown(self) -> None:
        await self.engine.dispose()
        for suffix in ("", "-wal", "-shm"):
            try:
                os.remove(self.path + suffix)
            except OSError:
                pass

    def _service(self, bot, **kwargs) -> ScheduledMessageService:
        return ScheduledMessageService(
            bot=bot,
            settings=_settings(),
            session_factory=self.session_factory,
            retry_base_seconds=0.05,
            retry_max_seconds=0.2,
            claim_lease_seconds=1.0,
            **kwargs,
        )

    async def _state(self):
        async with self.session_factory() as session:
            entry = await session.get(ScheduledMessage, 1)
            occurrences = (
                await session.scalars(select(ScheduledMessageOccurrence))
            ).all()
            return entry, occurrences

    async def test_success_advances_last_run_and_completes_occurrence(self) -> None:
        bot = SimpleNamespace(
            send_message=AsyncMock(
                return_value=SimpleNamespace(
                    message_id=901,
                    chat=SimpleNamespace(id=-10001),
                )
            ),
            pin_chat_message=AsyncMock(),
            unpin_chat_message=AsyncMock(),
        )
        service = self._service(bot)

        self.assertEqual(await service.run_once(now=NOW), 1)
        entry, occurrences = await self._state()
        assert entry is not None
        self.assertEqual(entry.last_run_at, NOW)
        self.assertEqual(occurrences, [])

    async def test_failure_keeps_occurrence_with_exponential_backoff(self) -> None:
        bot = SimpleNamespace(
            send_message=AsyncMock(side_effect=RuntimeError("offline")),
            pin_chat_message=AsyncMock(),
            unpin_chat_message=AsyncMock(),
        )
        service = self._service(bot)

        self.assertEqual(await service.run_once(now=NOW), 0)
        entry, occurrences = await self._state()
        assert entry is not None
        self.assertIsNone(entry.last_run_at)
        self.assertEqual(len(occurrences), 1)
        first = occurrences[0]
        self.assertEqual(first.attempts, 1)
        self.assertIsNone(first.lease_until)
        self.assertGreater(first.next_attempt_at, NOW)

        # Backoff prevents a hot-loop resend of the retained occurrence.
        self.assertEqual(await service.run_once(now=NOW), 0)
        self.assertEqual(bot.send_message.await_count, 1)

        bot.send_message.side_effect = None
        bot.send_message.return_value = SimpleNamespace(
            message_id=902,
            chat=SimpleNamespace(id=-10001),
        )
        self.assertEqual(
            await service.run_once(now=NOW + timedelta(seconds=1)),
            1,
        )
        entry, occurrences = await self._state()
        assert entry is not None
        self.assertEqual(entry.last_run_at, NOW + timedelta(seconds=1))
        self.assertEqual(occurrences, [])

    async def test_expired_lease_is_recovered(self) -> None:
        async with self.session_factory() as session:
            session.add(
                ScheduledMessageOccurrence(
                    scheduled_message_id=1,
                    occurrence_at=NOW.replace(hour=9, minute=0),
                    attempts=1,
                    lease_until=NOW - timedelta(seconds=1),
                )
            )
            await session.commit()
        bot = SimpleNamespace(
            send_message=AsyncMock(
                return_value=SimpleNamespace(
                    message_id=903,
                    chat=SimpleNamespace(id=-10001),
                )
            ),
            pin_chat_message=AsyncMock(),
            unpin_chat_message=AsyncMock(),
        )

        self.assertEqual(await self._service(bot).run_once(now=NOW), 1)
        entry, occurrences = await self._state()
        assert entry is not None
        self.assertEqual(entry.last_run_at, NOW)
        self.assertEqual(occurrences, [])

    async def test_two_workers_atomically_claim_one_occurrence(self) -> None:
        started = asyncio.Event()
        release = asyncio.Event()

        async def slow_send(**_kwargs):
            started.set()
            await release.wait()
            return SimpleNamespace(
                message_id=904,
                chat=SimpleNamespace(id=-10001),
            )

        bot = SimpleNamespace(
            send_message=AsyncMock(side_effect=slow_send),
            pin_chat_message=AsyncMock(),
            unpin_chat_message=AsyncMock(),
        )
        first = self._service(bot)
        second = self._service(bot)
        first_run = asyncio.create_task(first.run_once(now=NOW))
        await asyncio.wait_for(started.wait(), timeout=1.0)
        second_result = await second.run_once(now=NOW)
        release.set()

        self.assertEqual(await first_run, 1)
        self.assertEqual(second_result, 0)
        self.assertEqual(bot.send_message.await_count, 1)

    async def test_backoff_window_keeps_persistent_failure_unhealthy(self) -> None:
        async with self.session_factory() as session:
            session.add(
                ScheduledMessageOccurrence(
                    scheduled_message_id=1,
                    occurrence_at=NOW.replace(hour=9, minute=0),
                    attempts=3,
                    next_attempt_at=NOW + timedelta(hours=1),
                    last_error="persistent Telegram failure",
                )
            )
            await session.commit()
        bot = SimpleNamespace(
            send_message=AsyncMock(),
            pin_chat_message=AsyncMock(),
            unpin_chat_message=AsyncMock(),
        )

        with self.assertRaisesRegex(RuntimeError, "persistently failed"):
            await self._service(bot).run_once(
                now=NOW,
                raise_on_total_failure=True,
            )
        bot.send_message.assert_not_awaited()

    async def test_worker_that_loses_lease_cannot_complete_old_claim(self) -> None:
        bot = SimpleNamespace(
            send_message=AsyncMock(),
            pin_chat_message=AsyncMock(),
            unpin_chat_message=AsyncMock(),
        )
        old_worker = self._service(bot)
        new_worker = self._service(bot)
        occurrence_at = NOW.replace(hour=9, minute=0)
        await old_worker._persist_due_occurrences([(1, occurrence_at)])
        old_claim = await old_worker._claim_next_occurrence(
            authorized_groups={-10001},
            now=NOW,
        )
        self.assertIsNotNone(old_claim)
        assert old_claim is not None

        async with self.session_factory() as session:
            await session.execute(
                update(ScheduledMessageOccurrence)
                .where(ScheduledMessageOccurrence.id == old_claim.occurrence_id)
                .values(lease_until=NOW - timedelta(seconds=1))
            )
            await session.commit()
        new_claim = await new_worker._claim_next_occurrence(
            authorized_groups={-10001},
            now=NOW + timedelta(seconds=2),
        )
        self.assertIsNotNone(new_claim)
        assert new_claim is not None

        self.assertFalse(
            await old_worker._complete_occurrence(
                old_claim,
                completed_at=NOW,
            )
        )
        entry, occurrences = await self._state()
        assert entry is not None
        self.assertIsNone(entry.last_run_at)
        self.assertEqual(len(occurrences), 1)

        self.assertTrue(
            await new_worker._complete_occurrence(
                new_claim,
                completed_at=NOW + timedelta(seconds=2),
            )
        )
        entry, occurrences = await self._state()
        assert entry is not None
        self.assertEqual(entry.last_run_at, NOW + timedelta(seconds=2))
        self.assertEqual(occurrences, [])


if __name__ == "__main__":
    unittest.main()
