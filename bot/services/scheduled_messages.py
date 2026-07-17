"""Scheduled group messages: per-group timed announcements with optional pin.

Rows live in the scheduled_messages table and are managed from the Mini App.
A lightweight background loop (same shape as the profile patrol) wakes every
check interval, sends whatever is due, and optionally pins the sent message
(a "定时置顶"). Daily entries compare last_run_at against today's scheduled
instant so restarts never double-fire; interval entries fire when
last_run_at (or created_at for the first run) is older than the interval.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from bot.config import Settings
from bot.db.models import ScheduledMessage
from bot.services.authz import list_authorized_groups
from bot.services.patrol import parse_schedule_time
from bot.utils.telegram import (
    configured_auto_delete_seconds,
    schedule_message_auto_delete,
    sanitize_outgoing_mentions,
    sanitize_outgoing_text,
)
from bot.utils.timezone import now_shanghai_naive

log = logging.getLogger(__name__)

SCHEDULE_TYPES = ("daily", "interval")
_CHECK_INTERVAL_SECONDS = 30.0
_MIN_INTERVAL_MINUTES = 5


def scheduled_message_due(
    entry: ScheduledMessage,
    *,
    now: datetime | None = None,
) -> bool:
    """True when the entry should fire at `now` (Asia/Shanghai naive)."""
    if not entry.enabled or not str(entry.text or "").strip():
        return False
    current = now if now is not None else now_shanghai_naive()
    if str(entry.schedule_type or "daily") == "interval":
        minutes = max(_MIN_INTERVAL_MINUTES, int(entry.interval_minutes or 0))
        anchor = entry.last_run_at or entry.created_at
        if anchor is None:
            return True
        return current >= anchor + timedelta(minutes=minutes)
    parsed = parse_schedule_time(str(entry.schedule_time or ""))
    if parsed is None:
        return False
    due_at = current.replace(hour=parsed[0], minute=parsed[1], second=0, microsecond=0)
    if current < due_at:
        return False
    return entry.last_run_at is None or entry.last_run_at < due_at


class ScheduledMessageService:
    """Background loop delivering due scheduled messages per authorized group."""

    def __init__(
        self,
        *,
        bot: Bot,
        settings: Settings,
        session_factory: async_sessionmaker[AsyncSession],
        check_interval_seconds: float = _CHECK_INTERVAL_SECONDS,
    ) -> None:
        self.bot = bot
        self.settings = settings
        self.session_factory = session_factory
        self.check_interval_seconds = check_interval_seconds

    async def run_forever(self) -> None:
        log.info("scheduled message service started")
        while True:
            try:
                await self.run_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("scheduled message pass failed")
            await asyncio.sleep(max(5.0, float(self.check_interval_seconds)))

    async def run_once(self, *, now: datetime | None = None) -> int:
        """Send every due entry once; returns the number delivered."""
        current = now if now is not None else now_shanghai_naive()
        async with self.session_factory() as session:
            authorized = {
                int(row.group_id) for row in await list_authorized_groups(session)
            }
            rows = (
                await session.scalars(
                    select(ScheduledMessage)
                    .where(ScheduledMessage.enabled.is_(True))
                    .order_by(ScheduledMessage.id)
                )
            ).all()
            due = [
                row
                for row in rows
                if int(row.group_id) in authorized
                and scheduled_message_due(row, now=current)
            ]
            # Commit last_run_at before touching Telegram (project discipline:
            # DB first, then API). A send failure costs one occurrence instead
            # of risking a re-send loop against a flooding group.
            for entry in due:
                entry.last_run_at = current
            if due:
                await session.commit()

        delivered = 0
        for entry in due:
            if await self._deliver(entry):
                delivered += 1
        return delivered

    async def _deliver(self, entry: ScheduledMessage) -> bool:
        group_id = int(entry.group_id)
        payload = sanitize_outgoing_mentions(
            sanitize_outgoing_text(str(entry.text or "").strip())
        )
        if not payload:
            return False
        try:
            try:
                sent = await self.bot.send_message(chat_id=group_id, text=payload)
            except TelegramBadRequest as exc:
                if "can't parse entities" not in str(exc).lower():
                    raise
                sent = await self.bot.send_message(
                    chat_id=group_id, text=payload, parse_mode=None
                )
        except Exception:
            log.exception(
                "scheduled message send failed | group=%s entry=%s",
                group_id,
                entry.id,
            )
            return False
        if entry.auto_delete:
            schedule_message_auto_delete(
                sent,
                configured_auto_delete_seconds(self.settings, "scheduled"),
            )
        if entry.pin_message:
            await self._pin(entry, sent.message_id)
        log.info(
            "scheduled message sent | group=%s entry=%s pinned=%s",
            group_id,
            entry.id,
            bool(entry.pin_message),
        )
        return True

    async def _pin(self, entry: ScheduledMessage, message_id: int) -> None:
        group_id = int(entry.group_id)
        previous_id = int(entry.last_message_id or 0)
        if entry.unpin_previous and previous_id:
            try:
                await self.bot.unpin_chat_message(
                    chat_id=group_id, message_id=previous_id
                )
            except Exception:
                log.debug(
                    "scheduled message unpin skipped | group=%s message=%s",
                    group_id,
                    previous_id,
                )
        try:
            await self.bot.pin_chat_message(
                chat_id=group_id,
                message_id=message_id,
                disable_notification=True,
            )
        except Exception:
            log.warning(
                "scheduled message pin failed | group=%s entry=%s",
                group_id,
                entry.id,
            )
            return
        try:
            async with self.session_factory() as session:
                row = await session.get(ScheduledMessage, int(entry.id))
                if row is not None:
                    row.last_message_id = int(message_id)
                    await session.commit()
        except Exception:
            # Losing the unpin bookmark must not abort the remaining due
            # entries in this pass; their last_run_at is already committed.
            log.exception(
                "scheduled message pin bookkeeping failed | group=%s entry=%s",
                group_id,
                entry.id,
            )
