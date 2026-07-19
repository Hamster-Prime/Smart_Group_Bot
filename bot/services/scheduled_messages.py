"""Scheduled group Markdown templates with optional buttons and pinning.

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
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from aiogram import Bot
from sqlalchemy import delete, or_, select, update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from bot.config import Settings
from bot.db.models import ScheduledMessage, ScheduledMessageOccurrence
from bot.services.background_health import record_background_failure
from bot.services.message_templates import (
    build_template_keyboard,
    render_markdown_html,
    render_plain_template,
    send_template_with_fallback,
)
from bot.services.authz import list_authorized_groups
from bot.services.patrol import parse_schedule_time
from bot.utils.telegram import (
    configured_auto_delete_seconds,
    schedule_message_auto_delete_durable,
    sanitize_outgoing_text,
)
from bot.utils.timezone import now_shanghai_naive

log = logging.getLogger(__name__)

SCHEDULE_TYPES = ("daily", "interval")
_CHECK_INTERVAL_SECONDS = 30.0
_MIN_INTERVAL_MINUTES = 5
_CLAIM_LEASE_SECONDS = 180.0
_RETRY_BASE_SECONDS = 30.0
_RETRY_MAX_SECONDS = 1800.0
_PERSISTENT_FAILURE_ATTEMPTS = 3


@dataclass(frozen=True, slots=True)
class _ClaimedScheduledOccurrence:
    occurrence_id: int
    occurrence_at: datetime
    lease_until: datetime
    attempts: int
    entry: ScheduledMessage


def scheduled_message_due(
    entry: ScheduledMessage,
    *,
    now: datetime | None = None,
) -> bool:
    """True when the entry should fire at `now` (Asia/Shanghai naive)."""
    return scheduled_message_occurrence_at(entry, now=now) is not None


def scheduled_message_occurrence_at(
    entry: ScheduledMessage,
    *,
    now: datetime | None = None,
) -> datetime | None:
    """Return the stable identity of the due occurrence, if any."""

    if not entry.enabled or not str(entry.text or "").strip():
        return None
    current = now if now is not None else now_shanghai_naive()
    if str(entry.schedule_type or "daily") == "interval":
        minutes = max(_MIN_INTERVAL_MINUTES, int(entry.interval_minutes or 0))
        anchor = entry.last_run_at or entry.created_at
        if anchor is None:
            return current
        due_at = anchor + timedelta(minutes=minutes)
        return due_at if current >= due_at else None
    parsed = parse_schedule_time(str(entry.schedule_time or ""))
    if parsed is None:
        return None
    due_at = current.replace(hour=parsed[0], minute=parsed[1], second=0, microsecond=0)
    if current < due_at:
        return None
    if entry.last_run_at is not None and entry.last_run_at >= due_at:
        return None
    return due_at


class ScheduledMessageService:
    """Background loop delivering due scheduled messages per authorized group."""

    def __init__(
        self,
        *,
        bot: Bot,
        settings: Settings,
        session_factory: async_sessionmaker[AsyncSession],
        check_interval_seconds: float = _CHECK_INTERVAL_SECONDS,
        claim_lease_seconds: float = _CLAIM_LEASE_SECONDS,
        retry_base_seconds: float = _RETRY_BASE_SECONDS,
        retry_max_seconds: float = _RETRY_MAX_SECONDS,
        persistent_failure_attempts: int = _PERSISTENT_FAILURE_ATTEMPTS,
    ) -> None:
        self.bot = bot
        self.settings = settings
        self.session_factory = session_factory
        self.check_interval_seconds = check_interval_seconds
        self.claim_lease_seconds = max(1.0, float(claim_lease_seconds))
        self.retry_base_seconds = max(0.01, float(retry_base_seconds))
        self.retry_max_seconds = max(
            self.retry_base_seconds,
            float(retry_max_seconds),
        )
        self.persistent_failure_attempts = max(
            1,
            int(persistent_failure_attempts),
        )
        self._run_lock = asyncio.Lock()

    async def run_forever(self) -> None:
        log.info("scheduled message service started")
        consecutive_failures = 0
        while True:
            try:
                await self.run_once(raise_on_total_failure=True)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.exception("scheduled message pass failed")
                consecutive_failures = record_background_failure(
                    service="scheduled message service",
                    previous_failures=consecutive_failures,
                    error=exc,
                )
            else:
                consecutive_failures = 0
            await asyncio.sleep(max(5.0, float(self.check_interval_seconds)))

    async def run_once(
        self,
        *,
        now: datetime | None = None,
        raise_on_total_failure: bool = False,
    ) -> int:
        """Send every due entry once; returns the number delivered."""
        async with self._run_lock:
            return await self._run_once_locked(
                now=now,
                raise_on_total_failure=raise_on_total_failure,
            )

    async def _run_once_locked(
        self,
        *,
        now: datetime | None = None,
        raise_on_total_failure: bool = False,
    ) -> int:
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
        due_occurrences = [
            (int(row.id), occurrence_at)
            for row in rows
            if int(row.group_id) in authorized
            and (
                occurrence_at := scheduled_message_occurrence_at(
                    row,
                    now=current,
                )
            )
            is not None
        ]
        await self._persist_due_occurrences(due_occurrences)

        delivered = 0
        failures = 0
        attempted = 0
        while True:
            claimed = await self._claim_next_occurrence(
                authorized_groups=authorized,
                now=current,
            )
            if claimed is None:
                break
            attempted += 1
            if not await self._deliver(claimed.entry):
                failures += 1
                await self._retry_occurrence(
                    claimed,
                    error="Telegram scheduled message delivery failed",
                    now=current,
                )
                continue
            if not await self._complete_occurrence(
                claimed,
                completed_at=current,
            ):
                failures += 1
                log.error(
                    "scheduled message was sent but durable occurrence could "
                    "not be completed | group=%s entry=%s occurrence=%s",
                    claimed.entry.group_id,
                    claimed.entry.id,
                    claimed.occurrence_at,
                )
                continue
            delivered += 1
        if raise_on_total_failure:
            if attempted and failures and delivered == 0:
                raise RuntimeError(
                    "scheduled message delivery failed for all "
                    f"{attempted} claimed entries"
                )
            persistent_failures = await self._persistent_failure_count(
                authorized_groups=authorized,
            )
            if persistent_failures:
                # Backoff passes with no Telegram attempt must keep reporting a
                # poison occurrence. Otherwise run_forever would reset its
                # consecutive-failure counter during every quiet window and a
                # permanently broken destination would never reach supervisor.
                raise RuntimeError(
                    f"{persistent_failures} scheduled message occurrence(s) "
                    "remain persistently failed"
                )
        return delivered

    async def _persistent_failure_count(
        self,
        *,
        authorized_groups: set[int],
    ) -> int:
        if not authorized_groups:
            return 0
        async with self.session_factory() as session:
            rows = await session.scalars(
                select(ScheduledMessageOccurrence.id)
                .join(
                    ScheduledMessage,
                    ScheduledMessage.id
                    == ScheduledMessageOccurrence.scheduled_message_id,
                )
                .where(
                    ScheduledMessage.enabled.is_(True),
                    ScheduledMessage.group_id.in_(authorized_groups),
                    ScheduledMessageOccurrence.attempts
                    >= self.persistent_failure_attempts,
                    ScheduledMessageOccurrence.last_error != "",
                )
            )
            return len(rows.all())

    async def _persist_due_occurrences(
        self,
        occurrences: list[tuple[int, datetime]],
    ) -> None:
        if not occurrences:
            return
        async with self.session_factory() as session:
            dialect = getattr(getattr(session, "bind", None), "dialect", None)
            for entry_id, occurrence_at in occurrences:
                if getattr(dialect, "name", "") == "sqlite":
                    await session.execute(
                        sqlite_insert(ScheduledMessageOccurrence)
                        .values(
                            scheduled_message_id=entry_id,
                            occurrence_at=occurrence_at,
                        )
                        .on_conflict_do_nothing(
                            index_elements=[
                                ScheduledMessageOccurrence.scheduled_message_id,
                                ScheduledMessageOccurrence.occurrence_at,
                            ]
                        )
                    )
                else:
                    exists = await session.scalar(
                        select(ScheduledMessageOccurrence.id).where(
                            ScheduledMessageOccurrence.scheduled_message_id
                            == entry_id,
                            ScheduledMessageOccurrence.occurrence_at
                            == occurrence_at,
                        )
                    )
                    if exists is None:
                        session.add(
                            ScheduledMessageOccurrence(
                                scheduled_message_id=entry_id,
                                occurrence_at=occurrence_at,
                            )
                        )
            await session.commit()

    @staticmethod
    def _available_occurrence_filters(now: datetime) -> tuple[Any, Any]:
        return (
            or_(
                ScheduledMessageOccurrence.lease_until.is_(None),
                ScheduledMessageOccurrence.lease_until <= now,
            ),
            or_(
                ScheduledMessageOccurrence.next_attempt_at.is_(None),
                ScheduledMessageOccurrence.next_attempt_at <= now,
            ),
        )

    async def _claim_next_occurrence(
        self,
        *,
        authorized_groups: set[int],
        now: datetime,
    ) -> _ClaimedScheduledOccurrence | None:
        if not authorized_groups:
            return None
        available = self._available_occurrence_filters(now)
        lease_until = now + timedelta(seconds=self.claim_lease_seconds)
        for _attempt in range(8):
            async with self.session_factory() as session:
                candidate_id = await session.scalar(
                    select(ScheduledMessageOccurrence.id)
                    .join(
                        ScheduledMessage,
                        ScheduledMessage.id
                        == ScheduledMessageOccurrence.scheduled_message_id,
                    )
                    .where(
                        ScheduledMessage.enabled.is_(True),
                        ScheduledMessage.group_id.in_(authorized_groups),
                        *available,
                    )
                    .order_by(
                        ScheduledMessageOccurrence.occurrence_at,
                        ScheduledMessageOccurrence.id,
                    )
                    .limit(1)
                )
                if candidate_id is None:
                    return None
                result = await session.execute(
                    update(ScheduledMessageOccurrence)
                    .where(
                        ScheduledMessageOccurrence.id == int(candidate_id),
                        *available,
                    )
                    .values(
                        attempts=ScheduledMessageOccurrence.attempts + 1,
                        lease_until=lease_until,
                        next_attempt_at=None,
                        last_error="",
                        updated_at=now,
                    )
                    .returning(
                        ScheduledMessageOccurrence.id,
                        ScheduledMessageOccurrence.scheduled_message_id,
                        ScheduledMessageOccurrence.occurrence_at,
                        ScheduledMessageOccurrence.attempts,
                    )
                )
                claimed = result.first()
                if claimed is None:
                    await session.rollback()
                    continue
                entry = await session.get(ScheduledMessage, int(claimed[1]))
                if entry is None or not bool(entry.enabled):
                    await session.execute(
                        delete(ScheduledMessageOccurrence).where(
                            ScheduledMessageOccurrence.id == int(claimed[0])
                        )
                    )
                    await session.commit()
                    continue
                await session.commit()
                return _ClaimedScheduledOccurrence(
                    occurrence_id=int(claimed[0]),
                    occurrence_at=claimed[2],
                    lease_until=lease_until,
                    attempts=int(claimed[3]),
                    entry=entry,
                )
        return None

    async def _retry_occurrence(
        self,
        claimed: _ClaimedScheduledOccurrence,
        *,
        error: str,
        now: datetime,
    ) -> None:
        delay = min(
            self.retry_max_seconds,
            self.retry_base_seconds * (2 ** max(0, claimed.attempts - 1)),
        )
        async with self.session_factory() as session:
            await session.execute(
                update(ScheduledMessageOccurrence)
                .where(
                    ScheduledMessageOccurrence.id == claimed.occurrence_id,
                    ScheduledMessageOccurrence.lease_until == claimed.lease_until,
                )
                .values(
                    lease_until=None,
                    next_attempt_at=now + timedelta(seconds=delay),
                    last_error=str(error or "")[:1000],
                    updated_at=now,
                )
            )
            await session.commit()
        log.warning(
            "scheduled message retry scheduled | entry=%s occurrence=%s "
            "attempt=%s delay=%.1fs",
            claimed.entry.id,
            claimed.occurrence_at,
            claimed.attempts,
            delay,
        )

    async def _complete_occurrence(
        self,
        claimed: _ClaimedScheduledOccurrence,
        *,
        completed_at: datetime,
    ) -> bool:
        """Advance last_run and remove the owned occurrence in one commit."""

        try:
            async with self.session_factory() as session:
                # Consume ownership first, in the same write transaction that
                # advances last_run_at.  A preliminary SELECT is insufficient:
                # its lease can expire and be claimed by another worker before
                # this transaction reaches DELETE.
                deleted = await session.execute(
                    delete(ScheduledMessageOccurrence).where(
                        ScheduledMessageOccurrence.id == claimed.occurrence_id,
                        ScheduledMessageOccurrence.lease_until
                        == claimed.lease_until,
                    )
                )
                if int(deleted.rowcount or 0) != 1:
                    await session.rollback()
                    return False
                row = await session.get(ScheduledMessage, int(claimed.entry.id))
                if row is None:
                    await session.commit()
                    return False
                row.last_run_at = completed_at
                await session.commit()
                claimed.entry.last_run_at = completed_at
                return True
        except Exception:
            log.exception(
                "scheduled message occurrence completion failed | entry=%s",
                claimed.entry.id,
            )
            return False

    async def _deliver(self, entry: ScheduledMessage) -> bool:
        group_id = int(entry.group_id)
        source_text = sanitize_outgoing_text(str(entry.text or "").strip())
        if not source_text:
            return False
        payload = render_markdown_html(source_text)
        plain_fallback = render_plain_template(source_text)
        keyboard = build_template_keyboard(getattr(entry, "buttons", None))
        try:
            sent = await send_template_with_fallback(
                lambda text, **kwargs: self.bot.send_message(
                    chat_id=group_id,
                    text=text,
                    **kwargs,
                ),
                formatted_text=payload,
                plain_text=plain_fallback,
                reply_markup=keyboard,
            )
        except Exception:
            log.exception(
                "scheduled message send failed | group=%s entry=%s",
                group_id,
                entry.id,
            )
            return False
        if entry.auto_delete:
            await schedule_message_auto_delete_durable(
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
