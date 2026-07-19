from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Literal

from aiogram import Bot
from aiogram.exceptions import (
    TelegramBadRequest,
    TelegramForbiddenError,
    TelegramNetworkError,
    TelegramNotFound,
    TelegramRetryAfter,
    TelegramServerError,
    TelegramUnauthorizedError,
)
from sqlalchemy import delete, func, or_, select, update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from bot.db.models import TelegramDeleteJob
from bot.utils.timezone import now_shanghai_naive

log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class _DeleteRequest:
    chat_id: int
    message_id: int
    due_at: datetime


@dataclass(frozen=True, slots=True)
class _ClaimedDeleteJob:
    id: int
    chat_id: int
    message_id: int
    attempts: int
    lease_until: datetime


@dataclass(frozen=True, slots=True)
class _DeleteOutcome:
    kind: Literal["success", "retry", "permanent", "orphaned"]
    error: BaseException | None = None
    retry_after: float | None = None


class TelegramCleanupScheduler:
    """Single-worker, durable scheduler for Telegram message deletion.

    Production send sites use :meth:`enqueue_durable`, which commits the job
    before returning. The bounded synchronous queue remains only for legacy
    callers and isolated compatibility tests. Claimed rows are committed before
    any Telegram request, so restarts can recover them.
    """

    def __init__(
        self,
        *,
        bot: Bot,
        session_factory: Any,
        queue_size: int = 2048,
        persist_batch_size: int = 128,
        startup_timeout_seconds: float = 10.0,
        poll_interval_seconds: float = 1.0,
        delete_timeout_seconds: float = 5.0,
        cancel_grace_seconds: float = 0.25,
        lease_seconds: float = 30.0,
        retry_base_seconds: float = 5.0,
        retry_max_seconds: float = 300.0,
        max_attempts: int = 6,
        max_orphans: int = 32,
    ) -> None:
        self.bot = bot
        self.session_factory = session_factory
        self.queue_size = max(1, int(queue_size))
        self.persist_batch_size = max(1, int(persist_batch_size))
        self.startup_timeout_seconds = max(0.1, float(startup_timeout_seconds))
        self.poll_interval_seconds = max(0.01, float(poll_interval_seconds))
        self.delete_timeout_seconds = max(0.01, float(delete_timeout_seconds))
        self.cancel_grace_seconds = max(0.0, float(cancel_grace_seconds))
        self.lease_seconds = max(
            self.delete_timeout_seconds + self.cancel_grace_seconds + 0.05,
            float(lease_seconds),
        )
        self.retry_base_seconds = max(0.01, float(retry_base_seconds))
        self.retry_max_seconds = max(
            self.retry_base_seconds,
            float(retry_max_seconds),
        )
        self.max_attempts = max(1, int(max_attempts))
        self.max_orphans = max(1, int(max_orphans))

        self._intake: asyncio.Queue[_DeleteRequest] = asyncio.Queue(
            maxsize=self.queue_size
        )
        self._unpersisted: list[_DeleteRequest] = []
        self._worker_task: asyncio.Task[None] | None = None
        self._accepting = False
        self._stopping = False
        self._worker_error: BaseException | None = None
        self._auxiliary_error: BaseException | None = None
        self._fatal_event = asyncio.Event()
        self._ready_event = asyncio.Event()
        self._dropped_requests = 0
        self._orphan_tasks: dict[int, asyncio.Task[Any]] = {}
        self._orphan_finalizers: set[asyncio.Task[None]] = set()

    @property
    def worker_task(self) -> asyncio.Task[None] | None:
        return self._worker_task

    @property
    def failure(self) -> BaseException | None:
        return self._worker_error or self._auxiliary_error

    @property
    def dropped_requests(self) -> int:
        return self._dropped_requests

    @property
    def pending_intake(self) -> int:
        return self._intake.qsize() + len(self._unpersisted)

    @property
    def orphan_count(self) -> int:
        return len(self._orphan_tasks)

    async def start(self) -> None:
        if self._worker_task is not None and not self._worker_task.done():
            return
        if self._worker_task is not None and self._worker_task.done():
            try:
                error = self._worker_task.exception()
            except asyncio.CancelledError as exc:
                error = exc
            if error is not None:
                raise RuntimeError("Telegram cleanup worker previously failed") from error
        self._accepting = False
        self._stopping = False
        self._worker_error = None
        self._auxiliary_error = None
        self._fatal_event.clear()
        self._ready_event.clear()
        self._worker_task = asyncio.create_task(
            self._run(),
            name="telegram-cleanup-worker",
        )
        # Do not accept updates until the worker has successfully touched the
        # durable table.  Otherwise a schema/DB failure could surface only
        # after outgoing messages had already requested timer cleanup.
        ready_wait = asyncio.create_task(
            self._ready_event.wait(),
            name="telegram-cleanup-ready-wait",
        )
        try:
            done, _pending = await asyncio.wait(
                {self._worker_task, ready_wait},
                timeout=self.startup_timeout_seconds,
                return_when=asyncio.FIRST_COMPLETED,
            )
            if self._worker_task in done:
                await self._worker_task
                raise RuntimeError("Telegram cleanup worker exited during startup")
            if ready_wait not in done:
                raise TimeoutError(
                    "Telegram cleanup worker did not become ready before timeout"
                )
            self._accepting = True
        finally:
            ready_wait.cancel()
            await asyncio.gather(ready_wait, return_exceptions=True)

    def enqueue(self, *, chat_id: int, message_id: int, due_at: datetime) -> bool:
        """Compatibility-only non-durable intake.

        New asynchronous send paths must use :meth:`enqueue_durable`; this
        method can lose a just-sent message's timer if the process dies before
        the worker flushes its in-memory queue.
        """

        if not self._accepting or self._stopping:
            log.error(
                "Telegram cleanup scheduler is not accepting jobs | chat_id=%s "
                "message_id=%s",
                chat_id,
                message_id,
            )
            return False
        request = _DeleteRequest(
            chat_id=int(chat_id),
            message_id=int(message_id),
            due_at=due_at.replace(tzinfo=None),
        )
        try:
            self._intake.put_nowait(request)
            return True
        except asyncio.QueueFull:
            self._dropped_requests += 1
            failure = RuntimeError(
                "Telegram cleanup persistence queue is full; a deletion job "
                f"could not be accepted ({chat_id}:{message_id})"
            )
            self._set_auxiliary_failure(failure)
            log.critical("%s", failure)
            return False

    async def enqueue_durable(
        self,
        *,
        chat_id: int,
        message_id: int,
        due_at: datetime,
    ) -> bool:
        """Atomically persist a deletion job before acknowledging acceptance.

        Concurrent scheduling of the same Telegram message coalesces to one
        row and can only shorten, never extend, its retention deadline.
        Persistence failures make the permanent-service monitor unhealthy and
        return ``False`` without encouraging the send caller to retry an
        already-delivered Telegram message.
        """

        normalized_chat_id = int(chat_id)
        normalized_message_id = int(message_id)
        normalized_due_at = due_at.replace(tzinfo=None)
        if (
            not self._accepting
            or self._stopping
            or normalized_chat_id == 0
            or normalized_message_id <= 0
        ):
            failure = RuntimeError(
                "Telegram cleanup scheduler rejected durable intake "
                f"({normalized_chat_id}:{normalized_message_id})"
            )
            self._set_auxiliary_failure(failure)
            log.critical("%s", failure)
            return False

        now = now_shanghai_naive()
        try:
            async with self.session_factory() as session:
                dialect = getattr(getattr(session, "bind", None), "dialect", None)
                if getattr(dialect, "name", "") == "sqlite":
                    statement = sqlite_insert(TelegramDeleteJob).values(
                        chat_id=normalized_chat_id,
                        message_id=normalized_message_id,
                        due_at=normalized_due_at,
                        attempts=0,
                        lease_until=None,
                        last_error="",
                        updated_at=now,
                    )
                    await session.execute(
                        statement.on_conflict_do_update(
                            index_elements=[
                                TelegramDeleteJob.chat_id,
                                TelegramDeleteJob.message_id,
                            ],
                            set_={
                                "due_at": func.min(
                                    TelegramDeleteJob.due_at,
                                    statement.excluded.due_at,
                                ),
                                "updated_at": now,
                            },
                        )
                    )
                else:
                    existing = (
                        await session.execute(
                            select(TelegramDeleteJob).where(
                                TelegramDeleteJob.chat_id == normalized_chat_id,
                                TelegramDeleteJob.message_id
                                == normalized_message_id,
                            )
                        )
                    ).scalar_one_or_none()
                    if existing is None:
                        session.add(
                            TelegramDeleteJob(
                                chat_id=normalized_chat_id,
                                message_id=normalized_message_id,
                                due_at=normalized_due_at,
                            )
                        )
                    elif normalized_due_at < existing.due_at:
                        existing.due_at = normalized_due_at
                        existing.updated_at = now
                await session.commit()
        except BaseException as exc:
            if isinstance(exc, asyncio.CancelledError):
                raise
            failure = RuntimeError(
                "Telegram cleanup durable persistence failed for "
                f"{normalized_chat_id}:{normalized_message_id}"
            )
            self._set_auxiliary_failure(failure)
            log.critical("%s", failure, exc_info=True)
            return False
        return True

    async def monitor(self) -> None:
        """Permanent-service monitor used by the process supervisor.

        Cancelling this monitor deliberately does not cancel the worker; the
        ordered main shutdown stops the scheduler only after pending replies
        have had their final deletion jobs persisted.
        """

        worker = self._worker_task
        if worker is None:
            raise RuntimeError("Telegram cleanup scheduler was not started")
        fatal_wait = asyncio.create_task(
            self._fatal_event.wait(),
            name="telegram-cleanup-fatal-wait",
        )
        try:
            done, _pending = await asyncio.wait(
                {worker, fatal_wait},
                return_when=asyncio.FIRST_COMPLETED,
            )
            if fatal_wait in done and self.failure is not None:
                raise RuntimeError("Telegram cleanup scheduler became unhealthy") from self.failure
            await asyncio.shield(worker)
            if not self._stopping:
                raise RuntimeError("Telegram cleanup worker exited unexpectedly")
        finally:
            fatal_wait.cancel()
            await asyncio.gather(fatal_wait, return_exceptions=True)

    async def stop(self, *, timeout_seconds: float = 10.0) -> None:
        """Persist queued jobs and stop before the Bot/DB shared resources."""

        self._accepting = False
        self._stopping = True
        worker = self._worker_task
        worker_error: BaseException | None = None
        if worker is not None and not worker.done():
            done, _pending = await asyncio.wait(
                {worker},
                timeout=max(0.0, float(timeout_seconds)),
            )
            if worker not in done:
                log.error(
                    "Telegram cleanup worker did not stop in %.1fs; cancelling",
                    timeout_seconds,
                )
                worker.cancel()
                done, _pending = await asyncio.wait(
                    {worker},
                    timeout=self.cancel_grace_seconds,
                )
                if worker not in done:
                    worker.add_done_callback(self._observe_detached_task)
            if worker.done():
                try:
                    await worker
                except asyncio.CancelledError:
                    pass
                except BaseException as exc:  # preserve queue, then report below
                    worker_error = exc
        elif worker is not None:
            try:
                await worker
            except asyncio.CancelledError:
                pass
            except BaseException as exc:
                worker_error = exc

        # A failed worker keeps its last batch in ``_unpersisted``.  Retry the
        # short DB write while the engine is still live.  If a cancellation-
        # resistant worker is still active, it may own that batch, so avoid a
        # concurrent duplicate flush and leave recovery to the next process.
        if worker is None or worker.done():
            await self._persist_available_intake()

        await self._stop_orphans(timeout_seconds=min(2.0, timeout_seconds))
        if self.pending_intake:
            raise RuntimeError(
                f"Telegram cleanup stopped with {self.pending_intake} unpersisted job(s)"
            )
        if worker_error is not None:
            raise RuntimeError("Telegram cleanup worker failed") from worker_error

    async def _run(self) -> None:
        try:
            await self._delete_exhausted_legacy_jobs()
            self._ready_event.set()
            while True:
                await self._persist_available_intake()
                if self._stopping:
                    return

                claimed = await self._claim_due_job()
                if claimed is not None:
                    await self._process_claimed_job(claimed)
                    continue

                timeout = await self._seconds_until_next_job()
                try:
                    request = await asyncio.wait_for(
                        self._intake.get(),
                        timeout=timeout,
                    )
                except TimeoutError:
                    continue
                self._unpersisted.append(request)
        except asyncio.CancelledError:
            raise
        except BaseException as exc:
            self._worker_error = exc
            self._fatal_event.set()
            log.exception("Telegram cleanup worker failed")
            raise

    async def _persist_available_intake(self) -> None:
        batch = self._unpersisted
        self._unpersisted = []
        while len(batch) < self.persist_batch_size:
            try:
                batch.append(self._intake.get_nowait())
            except asyncio.QueueEmpty:
                break
        if not batch:
            return
        coalesced: dict[tuple[int, int], _DeleteRequest] = {}
        for request in batch:
            key = (request.chat_id, request.message_id)
            existing_request = coalesced.get(key)
            if existing_request is None or request.due_at < existing_request.due_at:
                coalesced[key] = request
        try:
            async with self.session_factory() as session:
                for request in coalesced.values():
                    existing = (
                        await session.execute(
                            select(TelegramDeleteJob).where(
                                TelegramDeleteJob.chat_id == request.chat_id,
                                TelegramDeleteJob.message_id == request.message_id,
                            )
                        )
                    ).scalar_one_or_none()
                    if existing is None:
                        session.add(
                            TelegramDeleteJob(
                                chat_id=request.chat_id,
                                message_id=request.message_id,
                                due_at=request.due_at,
                            )
                        )
                    elif existing.due_at is None or request.due_at < existing.due_at:
                        # Repeated scheduling must never extend retention.
                        existing.due_at = request.due_at
                        existing.updated_at = now_shanghai_naive()
                await session.commit()
        except BaseException:
            # Every item in this list still owns one unfinished queue slot.
            self._unpersisted = [*batch, *self._unpersisted]
            raise
        for _request in batch:
            self._intake.task_done()

    async def _claim_due_job(self) -> _ClaimedDeleteJob | None:
        if len(self._orphan_tasks) >= self.max_orphans:
            # Do not burn retry attempts for unrelated rows while no more
            # cancellation-resistant Telegram calls can be tracked safely.
            return None
        now = now_shanghai_naive()
        lease_until = now + timedelta(seconds=self.lease_seconds)
        orphan_ids = tuple(self._orphan_tasks)
        available = (
            TelegramDeleteJob.due_at <= now,
            or_(
                TelegramDeleteJob.lease_until.is_(None),
                TelegramDeleteJob.lease_until <= now,
            ),
        )
        if orphan_ids:
            available = (*available, TelegramDeleteJob.id.not_in(orphan_ids))

        async with self.session_factory() as session:
            candidate_id = (
                await session.execute(
                    select(TelegramDeleteJob.id)
                    .where(*available)
                    .order_by(TelegramDeleteJob.due_at, TelegramDeleteJob.id)
                    .limit(1)
                )
            ).scalar_one_or_none()
            if candidate_id is None:
                return None
            result = await session.execute(
                update(TelegramDeleteJob)
                .where(
                    TelegramDeleteJob.id == candidate_id,
                    *available,
                )
                .values(
                    attempts=TelegramDeleteJob.attempts + 1,
                    lease_until=lease_until,
                    updated_at=now,
                )
                .returning(
                    TelegramDeleteJob.id,
                    TelegramDeleteJob.chat_id,
                    TelegramDeleteJob.message_id,
                    TelegramDeleteJob.attempts,
                )
            )
            row = result.first()
            await session.commit()
        if row is None:
            return None
        return _ClaimedDeleteJob(
            id=int(row[0]),
            chat_id=int(row[1]),
            message_id=int(row[2]),
            attempts=int(row[3]),
            lease_until=lease_until,
        )

    async def _seconds_until_next_job(self) -> float:
        if len(self._orphan_tasks) >= self.max_orphans:
            return self.poll_interval_seconds
        now = now_shanghai_naive()
        orphan_ids = tuple(self._orphan_tasks)
        available_filters: list[Any] = [
            or_(
                TelegramDeleteJob.lease_until.is_(None),
                TelegramDeleteJob.lease_until <= now,
            )
        ]
        lease_filters: list[Any] = [TelegramDeleteJob.lease_until > now]
        if orphan_ids:
            available_filters.append(TelegramDeleteJob.id.not_in(orphan_ids))
            lease_filters.append(TelegramDeleteJob.id.not_in(orphan_ids))
        async with self.session_factory() as session:
            next_due = (
                await session.execute(
                    select(func.min(TelegramDeleteJob.due_at)).where(
                        *available_filters
                    )
                )
            ).scalar_one_or_none()
            next_lease = (
                await session.execute(
                    select(func.min(TelegramDeleteJob.lease_until)).where(
                        *lease_filters
                    )
                )
            ).scalar_one_or_none()
        candidates = [value for value in (next_due, next_lease) if value is not None]
        if not candidates:
            return self.poll_interval_seconds
        delay = min((value - now).total_seconds() for value in candidates)
        return max(0.01, min(self.poll_interval_seconds, delay))

    async def _process_claimed_job(self, job: _ClaimedDeleteJob) -> None:
        if len(self._orphan_tasks) >= self.max_orphans:
            await self._retry_or_drop(
                job,
                RuntimeError("Telegram delete orphan capacity is saturated"),
            )
            return
        outcome = await self._delete_with_hard_deadline(job)
        if outcome.kind == "orphaned":
            return
        if outcome.kind in {"success", "permanent"}:
            if outcome.kind == "permanent":
                log.info(
                    "discarding permanent Telegram delete failure | chat_id=%s "
                    "message_id=%s error=%s",
                    job.chat_id,
                    job.message_id,
                    outcome.error,
                )
            await self._remove_job(job)
            return
        await self._retry_or_drop(
            job,
            outcome.error or RuntimeError("unknown Telegram delete failure"),
            retry_after=outcome.retry_after,
        )

    async def _delete_with_hard_deadline(
        self,
        job: _ClaimedDeleteJob,
    ) -> _DeleteOutcome:
        task = asyncio.create_task(
            self.bot.delete_message(
                chat_id=job.chat_id,
                message_id=job.message_id,
            ),
            name=f"telegram-delete:{job.chat_id}:{job.message_id}",
        )
        try:
            done, _pending = await asyncio.wait(
                {task},
                timeout=self.delete_timeout_seconds,
            )
        except asyncio.CancelledError:
            await self._retire_cancelled_delete(job, task)
            raise
        if task in done:
            return self._outcome_from_done_task(task)

        task.cancel()
        try:
            done, _pending = await asyncio.wait(
                {task},
                timeout=self.cancel_grace_seconds,
            )
        except asyncio.CancelledError:
            await self._retire_cancelled_delete(job, task)
            raise
        if task in done:
            if task.cancelled():
                return _DeleteOutcome(
                    "retry",
                    TimeoutError("Telegram delete exceeded its hard deadline"),
                )
            return self._outcome_from_done_task(task)

        self._track_orphaned_delete(job, task)
        log.error(
            "Telegram delete ignored cancellation; retaining leased job | "
            "chat_id=%s message_id=%s",
            job.chat_id,
            job.message_id,
        )
        return _DeleteOutcome("orphaned")

    async def _retire_cancelled_delete(
        self,
        job: _ClaimedDeleteJob,
        task: asyncio.Task[Any],
    ) -> None:
        """Transfer an in-flight delete to orphan ownership during shutdown.

        The worker cancellation must still propagate, but the child Telegram
        request may suppress cancellation.  Register it before yielding so a
        second cancellation of the worker cannot leave that request detached
        from both bounded shutdown and the lease-CAS finalizer.
        """

        self._track_orphaned_delete(job, task)
        task.cancel()
        try:
            await asyncio.wait(
                {task},
                timeout=self.cancel_grace_seconds,
            )
        except asyncio.CancelledError:
            # Ownership was transferred before the wait, so repeated shutdown
            # cancellation is safe to propagate immediately.
            task.cancel()
            raise

    def _track_orphaned_delete(
        self,
        job: _ClaimedDeleteJob,
        task: asyncio.Task[Any],
    ) -> None:
        if self._orphan_tasks.get(job.id) is task:
            return
        self._orphan_tasks[job.id] = task
        if len(self._orphan_tasks) >= self.max_orphans:
            self._set_auxiliary_failure(
                RuntimeError(
                    "Telegram delete orphan capacity is saturated; "
                    "restart required to recover safely"
                )
            )
        task.add_done_callback(
            lambda finished, claimed=job: self._schedule_orphan_finalizer(
                claimed,
                finished,
            )
        )

    def _outcome_from_done_task(self, task: asyncio.Task[Any]) -> _DeleteOutcome:
        if task.cancelled():
            return _DeleteOutcome("retry", asyncio.CancelledError())
        try:
            task.result()
        except TelegramRetryAfter as exc:
            return _DeleteOutcome(
                "retry",
                exc,
                retry_after=max(0.0, float(exc.retry_after)),
            )
        except (
            TelegramBadRequest,
            TelegramForbiddenError,
            TelegramNotFound,
            TelegramUnauthorizedError,
        ) as exc:
            return _DeleteOutcome("permanent", exc)
        except (TelegramNetworkError, TelegramServerError) as exc:
            return _DeleteOutcome("retry", exc)
        except BaseException as exc:
            return _DeleteOutcome("retry", exc)
        return _DeleteOutcome("success")

    def _schedule_orphan_finalizer(
        self,
        job: _ClaimedDeleteJob,
        task: asyncio.Task[Any],
    ) -> None:
        try:
            finalizer = asyncio.create_task(
                self._finalize_orphan(job, task),
                name=f"telegram-delete-finalize:{job.id}",
            )
        except RuntimeError:
            return
        self._orphan_finalizers.add(finalizer)
        finalizer.add_done_callback(self._observe_orphan_finalizer)

    async def _finalize_orphan(
        self,
        job: _ClaimedDeleteJob,
        task: asyncio.Task[Any],
    ) -> None:
        try:
            outcome = self._outcome_from_done_task(task)
            if outcome.kind in {"success", "permanent"}:
                await self._remove_job(job)
            else:
                await self._retry_or_drop(
                    job,
                    outcome.error or RuntimeError("orphaned Telegram delete failed"),
                    retry_after=outcome.retry_after,
                )
        except BaseException as exc:
            self._set_auxiliary_failure(exc)
            log.exception(
                "failed to finalize cancellation-resistant Telegram delete | job=%s",
                job.id,
            )
            raise
        else:
            if self._orphan_tasks.get(job.id) is task:
                self._orphan_tasks.pop(job.id, None)

    async def _retry_or_drop(
        self,
        job: _ClaimedDeleteJob,
        error: BaseException,
        *,
        retry_after: float | None = None,
    ) -> None:
        detail = str(error or type(error).__name__)[:1000]
        if job.attempts >= self.max_attempts:
            removed = await self._remove_job(job)
            if removed:
                log.error(
                    "Telegram delete abandoned after %d attempt(s) | chat_id=%s "
                    "message_id=%s error=%s",
                    job.attempts,
                    job.chat_id,
                    job.message_id,
                    detail,
                )
            return
        exponential = min(
            self.retry_max_seconds,
            self.retry_base_seconds * (2 ** max(0, job.attempts - 1)),
        )
        delay = max(exponential, float(retry_after or 0.0))
        now = now_shanghai_naive()
        async with self.session_factory() as session:
            result = await session.execute(
                update(TelegramDeleteJob)
                .where(
                    TelegramDeleteJob.id == job.id,
                    TelegramDeleteJob.lease_until == job.lease_until,
                )
                .values(
                    due_at=now + timedelta(seconds=delay),
                    lease_until=None,
                    last_error=detail,
                    updated_at=now,
                )
            )
            await session.commit()
        if not int(result.rowcount or 0):
            log.info(
                "ignored stale Telegram delete retry result | job=%s lease=%s",
                job.id,
                job.lease_until,
            )
            return
        log.warning(
            "Telegram delete retry scheduled | chat_id=%s message_id=%s "
            "attempt=%s delay=%.1fs error=%s",
            job.chat_id,
            job.message_id,
            job.attempts,
            delay,
            detail,
        )

    async def _remove_job(self, job: _ClaimedDeleteJob) -> bool:
        async with self.session_factory() as session:
            result = await session.execute(
                delete(TelegramDeleteJob).where(
                    TelegramDeleteJob.id == job.id,
                    TelegramDeleteJob.lease_until == job.lease_until,
                )
            )
            await session.commit()
        removed = bool(int(result.rowcount or 0))
        if not removed:
            log.info(
                "ignored stale Telegram delete completion | job=%s lease=%s",
                job.id,
                job.lease_until,
            )
        return removed

    async def _delete_exhausted_legacy_jobs(self) -> None:
        async with self.session_factory() as session:
            result = await session.execute(
                delete(TelegramDeleteJob).where(
                    TelegramDeleteJob.attempts >= self.max_attempts
                )
            )
            await session.commit()
        removed = max(0, int(result.rowcount or 0))
        if removed:
            log.warning(
                "discarded %d exhausted legacy Telegram cleanup job(s)",
                removed,
            )

    async def _stop_orphans(self, *, timeout_seconds: float) -> None:
        tasks = {
            *self._orphan_tasks.values(),
            *self._orphan_finalizers,
        }
        for task in self._orphan_tasks.values():
            if not task.done():
                task.cancel()
        if not tasks:
            return
        done, pending = await asyncio.wait(
            tasks,
            timeout=max(0.0, float(timeout_seconds)),
        )
        for task in done:
            self._observe_detached_task(task)
        if pending:
            log.error(
                "%d Telegram cleanup task(s) ignored bounded shutdown; "
                "leased rows will recover after restart",
                len(pending),
            )
            for task in pending:
                task.add_done_callback(self._observe_detached_task)
        # Completion callbacks may have created short DB finalizers while the
        # first wait was yielding.  Drain that second generation as well before
        # the engine is disposed.
        await asyncio.sleep(0)
        finalizers = {task for task in self._orphan_finalizers if not task.done()}
        if finalizers:
            done, pending = await asyncio.wait(
                finalizers,
                timeout=max(0.0, float(timeout_seconds)),
            )
            for task in done:
                self._observe_detached_task(task)
            for task in pending:
                task.add_done_callback(self._observe_detached_task)

    def _set_auxiliary_failure(self, error: BaseException) -> None:
        if self._auxiliary_error is None:
            self._auxiliary_error = error
        self._fatal_event.set()

    def _observe_orphan_finalizer(self, task: asyncio.Task[None]) -> None:
        self._orphan_finalizers.discard(task)
        self._observe_detached_task(task)

    @staticmethod
    def _observe_detached_task(task: asyncio.Task[Any]) -> None:
        if task.cancelled():
            return
        try:
            task.exception()
        except (asyncio.CancelledError, Exception):
            pass
