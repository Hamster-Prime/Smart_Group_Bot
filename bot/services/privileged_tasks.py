"""Bounded, high-priority execution for security and permission operations.

Telegram update handlers must acknowledge interactive administration commands
quickly.  The actual work is therefore handed to this small in-process runner
instead of occupying a shared update worker for minutes.  Critical operator
commands and automatic security work use separate lanes so a burst of join
screening cannot starve ``/ban`` or ``/unban``.

The executor queue is in-process, but submissions made while processing a
durable Telegram update automatically defer that inbox receipt until the job
finishes. A crash therefore replays the original update instead of silently
losing a queued job. Callers must still keep operations idempotent and persist
domain recovery journals before external side effects; non-update callers do
not have the durable replay guarantee.
"""

from __future__ import annotations

import asyncio
import logging
import weakref
from collections.abc import Awaitable, Callable
from contextvars import Context
from dataclasses import dataclass, field
from typing import Any, Literal

from bot.services.request_priority import (
    ExecutionPriority,
    execution_priority_scope,
    privileged_request_scope,
)
from bot.services.resource_health import register_resource_health_provider
from bot.services.update_completion import (
    UpdateCompletionReceipt,
    current_update_completion,
)

log = logging.getLogger(__name__)

PrivilegedLane = Literal[
    "critical",
    "critical_bulk",
    "security",
    "policy",
    "profile",
]

_CRITICAL_WORKERS = 4
_CRITICAL_BULK_WORKERS = 2
_SECURITY_WORKERS = 4
_POLICY_WORKERS = 2
_PROFILE_WORKERS = 2
_CRITICAL_QUEUE_SIZE = 64
_CRITICAL_BULK_QUEUE_SIZE = 32
_SECURITY_QUEUE_SIZE = 128
_POLICY_QUEUE_SIZE = 32
_PROFILE_QUEUE_SIZE = 64
_DEFAULT_JOB_TIMEOUT_SECONDS = 180.0
_SHUTDOWN_CANCEL_GRACE_SECONDS = 1.0
_QUEUE_REAPER_INTERVAL_SECONDS = 0.25


@dataclass(frozen=True, slots=True)
class PrivilegedTaskSubmission:
    accepted: bool
    created: bool
    job_id: str
    lane: PrivilegedLane
    queue_depth: int
    reason: str = ""


@dataclass(slots=True)
class _PrivilegedJob:
    job_id: str
    key: str
    label: str
    lane: PrivilegedLane
    priority: int
    timeout_seconds: float
    enqueued_at: float
    operation: Callable[[], Awaitable[None]]
    completion: asyncio.Future[bool]
    receipts: list[UpdateCompletionReceipt] = field(default_factory=list)
    orphaned: bool = False


@dataclass(slots=True)
class _LaneState:
    queue: asyncio.PriorityQueue[tuple[int, int, _PrivilegedJob]]
    worker_count: int
    workers: set[asyncio.Task[None]] = field(default_factory=set)


@dataclass(slots=True)
class _RunnerState:
    lanes: dict[PrivilegedLane, _LaneState]
    jobs_by_key: dict[str, _PrivilegedJob] = field(default_factory=dict)
    active_tasks: dict[str, asyncio.Task[None]] = field(default_factory=dict)
    active_started_at: dict[str, float] = field(default_factory=dict)
    active_deadline_at: dict[str, float] = field(default_factory=dict)
    orphan_tasks: set[asyncio.Task[None]] = field(default_factory=set)
    orphan_started_at: dict[asyncio.Task[None], float] = field(default_factory=dict)
    reaper_task: asyncio.Task[None] | None = None
    sequence: int = 0
    draining: bool = False


_RUNNER_STATES: "weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, _RunnerState]" = (
    weakref.WeakKeyDictionary()
)


def _runner_state() -> _RunnerState:
    loop = asyncio.get_running_loop()
    state = _RUNNER_STATES.get(loop)
    if state is None:
        state = _RunnerState(
            lanes={
                "critical": _LaneState(
                    queue=asyncio.PriorityQueue(maxsize=_CRITICAL_QUEUE_SIZE),
                    worker_count=_CRITICAL_WORKERS,
                ),
                "critical_bulk": _LaneState(
                    queue=asyncio.PriorityQueue(maxsize=_CRITICAL_BULK_QUEUE_SIZE),
                    worker_count=_CRITICAL_BULK_WORKERS,
                ),
                "security": _LaneState(
                    queue=asyncio.PriorityQueue(maxsize=_SECURITY_QUEUE_SIZE),
                    worker_count=_SECURITY_WORKERS,
                ),
                "policy": _LaneState(
                    queue=asyncio.PriorityQueue(maxsize=_POLICY_QUEUE_SIZE),
                    worker_count=_POLICY_WORKERS,
                ),
                "profile": _LaneState(
                    queue=asyncio.PriorityQueue(maxsize=_PROFILE_QUEUE_SIZE),
                    worker_count=_PROFILE_WORKERS,
                ),
            }
        )
        _RUNNER_STATES[loop] = state
    return state


def _consume_task_result(task: asyncio.Task[Any]) -> None:
    if task.cancelled():
        return
    try:
        task.exception()
    except (asyncio.CancelledError, Exception):
        pass


def _finish_queued_job(
    state: _RunnerState,
    job: _PrivilegedJob,
    *,
    succeeded: bool,
) -> None:
    """Finish one job that will never be handed to a worker."""

    if not job.completion.done():
        job.completion.set_result(bool(succeeded))
    for receipt in job.receipts:
        receipt.finish(bool(succeeded))
    job.receipts.clear()
    if state.jobs_by_key.get(job.key) is job:
        state.jobs_by_key.pop(job.key, None)


def _expire_queued_jobs(state: _RunnerState) -> int:
    """Remove queue-expired jobs without making workers execute stale work.

    ``asyncio.Queue`` increments its unfinished counter on every ``put``.  A
    retained item is therefore balanced with ``task_done`` before it is put
    back, keeping ``join`` accounting exact while this synchronous event-loop
    section rebuilds the priority heap.
    """

    now = asyncio.get_running_loop().time()
    expired = 0
    for lane_name, lane in state.lanes.items():
        retained: list[tuple[int, int, _PrivilegedJob]] = []
        queued_count = lane.queue.qsize()
        for _index in range(queued_count):
            try:
                item = lane.queue.get_nowait()
            except asyncio.QueueEmpty:
                break
            _priority, _sequence, job = item
            deadline_at = job.enqueued_at + max(0.1, float(job.timeout_seconds))
            lane.queue.task_done()
            if deadline_at <= now:
                expired += 1
                _finish_queued_job(state, job, succeeded=False)
                log.error(
                    "expired queued privileged task discarded | "
                    "job=%s label=%s lane=%s age=%.3fs",
                    job.job_id,
                    job.label,
                    lane_name,
                    max(0.0, now - job.enqueued_at),
                )
            else:
                retained.append(item)
        for item in retained:
            lane.queue.put_nowait(item)
    return expired


async def _queue_reaper(state: _RunnerState) -> None:
    while not state.draining:
        await asyncio.sleep(_QUEUE_REAPER_INTERVAL_SECONDS)
        _expire_queued_jobs(state)
        # A worker must not remain silently dead after an unexpected exception.
        for lane_name in state.lanes:
            _ensure_workers(state, lane_name)


def _ensure_reaper(state: _RunnerState) -> None:
    task = state.reaper_task
    if task is not None and not task.done():
        return
    task = asyncio.create_task(
        _queue_reaper(state),
        name="privileged-queue-reaper",
        context=Context(),
    )
    state.reaper_task = task
    task.add_done_callback(_consume_task_result)


def _observe_orphan(
    task: asyncio.Task[None],
    state: _RunnerState,
    job: _PrivilegedJob,
) -> None:
    state.orphan_tasks.discard(task)
    state.orphan_started_at.pop(task, None)
    succeeded = False
    if not task.cancelled():
        try:
            task.result()
        except (asyncio.CancelledError, Exception):
            succeeded = False
        else:
            succeeded = True
    if not job.completion.done():
        job.completion.set_result(succeeded)
    for receipt in job.receipts:
        receipt.finish(succeeded)
    job.receipts.clear()
    if state.jobs_by_key.get(job.key) is job:
        state.jobs_by_key.pop(job.key, None)


def _attach_current_update_receipt(job: _PrivilegedJob) -> None:
    """Keep the durable inbox row pending until detached work is terminal."""

    receipt = current_update_completion()
    if receipt is None:
        return
    receipt.defer()
    if job.completion.done():
        try:
            succeeded = bool(job.completion.result())
        except (asyncio.CancelledError, Exception):
            succeeded = False
        receipt.finish(succeeded)
        return
    job.receipts.append(receipt)


async def _run_job_bounded(job: _PrivilegedJob, state: _RunnerState) -> bool:
    # Interactive permission work keeps CRITICAL priority through the actual
    # Telegram/DB side effects. Automatic join/raid enforcement uses HIGH so
    # it retains reserved capacity without taking the operator-only reserve.
    loop = asyncio.get_running_loop()
    deadline_at = job.enqueued_at + max(0.1, float(job.timeout_seconds))
    remaining = deadline_at - loop.time()
    if remaining <= 0.0:
        log.error(
            "privileged task expired in queue | job=%s label=%s lane=%s age=%.3fs",
            job.job_id,
            job.label,
            job.lane,
            max(0.0, loop.time() - job.enqueued_at),
        )
        return False

    priority_scope = (
        execution_priority_scope(ExecutionPriority.NORMAL)
        if job.lane in {"policy", "profile"}
        else privileged_request_scope(background=job.lane == "security")
    )
    with priority_scope:
        operation_task = asyncio.create_task(
            job.operation(),
            name=f"privileged-operation:{job.job_id}",
        )
    state.active_tasks[job.job_id] = operation_task
    state.active_started_at[job.job_id] = loop.time()
    state.active_deadline_at[job.job_id] = deadline_at
    try:
        done, _pending = await asyncio.wait(
            {operation_task},
            timeout=max(0.01, remaining),
        )
        if operation_task in done:
            await operation_task
            return True

        operation_task.cancel()
        job.orphaned = True
        state.orphan_tasks.add(operation_task)
        state.orphan_started_at[operation_task] = asyncio.get_running_loop().time()
        operation_task.add_done_callback(
            lambda task, owned_state=state, owned_job=job: _observe_orphan(
                task,
                owned_state,
                owned_job,
            )
        )
        log.error(
            "privileged task exceeded hard deadline | job=%s label=%s timeout=%.1fs",
            job.job_id,
            job.label,
            job.timeout_seconds,
        )
        return False
    except asyncio.CancelledError:
        operation_task.cancel()
        done, _pending = await asyncio.wait(
            {operation_task},
            timeout=_SHUTDOWN_CANCEL_GRACE_SECONDS,
        )
        if operation_task not in done:
            job.orphaned = True
            state.orphan_tasks.add(operation_task)
            state.orphan_started_at[operation_task] = asyncio.get_running_loop().time()
            operation_task.add_done_callback(
                lambda task, owned_state=state, owned_job=job: _observe_orphan(
                    task,
                    owned_state,
                    owned_job,
                )
            )
        raise
    finally:
        if state.active_tasks.get(job.job_id) is operation_task:
            state.active_tasks.pop(job.job_id, None)
            state.active_started_at.pop(job.job_id, None)
            state.active_deadline_at.pop(job.job_id, None)


async def _worker(state: _RunnerState, lane_name: PrivilegedLane) -> None:
    lane = state.lanes[lane_name]
    while True:
        _priority, _sequence, job = await lane.queue.get()
        succeeded = False
        try:
            succeeded = await _run_job_bounded(job, state)
            if job.orphaned:
                # The lane capacity describes real side effects, not timeout
                # wrappers. A child that ignores cancellation keeps this worker
                # occupied until it is terminal, preventing an upstream outage
                # from multiplying into worker_count active orphans plus another
                # worker_count fresh requests.
                succeeded = bool(await asyncio.shield(job.completion))
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception(
                "privileged task failed | job=%s label=%s lane=%s",
                job.job_id,
                job.label,
                lane_name,
            )
        finally:
            # A cancellation-resistant operation may still mutate Telegram or
            # the database. Keep its dedup key and durable receipts owned until
            # the actual child task reaches a terminal state; replaying sooner
            # would execute the same permission change concurrently.
            if not job.orphaned:
                if not job.completion.done():
                    job.completion.set_result(succeeded)
                for receipt in job.receipts:
                    receipt.finish(succeeded)
                job.receipts.clear()
                if state.jobs_by_key.get(job.key) is job:
                    state.jobs_by_key.pop(job.key, None)
            lane.queue.task_done()


def _ensure_workers(state: _RunnerState, lane_name: PrivilegedLane) -> None:
    lane = state.lanes[lane_name]
    lane.workers = {task for task in lane.workers if not task.done()}
    while len(lane.workers) < lane.worker_count:
        index = len(lane.workers) + 1
        task = asyncio.create_task(
            _worker(state, lane_name),
            name=f"privileged-{lane_name}-worker-{index}",
            context=Context(),
        )
        lane.workers.add(task)
        task.add_done_callback(lane.workers.discard)


def submit_privileged_task(
    *,
    key: str,
    label: str,
    operation: Callable[[], Awaitable[None]],
    lane: PrivilegedLane = "critical",
    priority: int = 0,
    timeout_seconds: float = _DEFAULT_JOB_TIMEOUT_SECONDS,
) -> PrivilegedTaskSubmission:
    """Accept one idempotent operation without waiting for queue capacity.

    ``key`` deduplicates queued and active work.  A duplicate submission is
    reported as accepted but not created, allowing callbacks to acknowledge a
    harmless retry without starting competing Telegram side effects.
    """

    state = _runner_state()
    _expire_queued_jobs(state)
    _ensure_reaper(state)
    selected_lane = state.lanes[lane]
    existing = state.jobs_by_key.get(str(key))
    if existing is not None:
        _attach_current_update_receipt(existing)
        return PrivilegedTaskSubmission(
            accepted=True,
            created=False,
            job_id=existing.job_id,
            lane=existing.lane,
            queue_depth=selected_lane.queue.qsize(),
            reason="duplicate",
        )
    if state.draining:
        return PrivilegedTaskSubmission(
            accepted=False,
            created=False,
            job_id="",
            lane=lane,
            queue_depth=selected_lane.queue.qsize(),
            reason="shutting_down",
        )
    if selected_lane.queue.full():
        return PrivilegedTaskSubmission(
            accepted=False,
            created=False,
            job_id="",
            lane=lane,
            queue_depth=selected_lane.queue.qsize(),
            reason="queue_full",
        )

    state.sequence += 1
    sequence = state.sequence
    job_id = f"{lane}-{sequence}"
    job = _PrivilegedJob(
        job_id=job_id,
        key=str(key),
        label=str(label),
        lane=lane,
        priority=int(priority),
        timeout_seconds=max(0.1, float(timeout_seconds)),
        enqueued_at=asyncio.get_running_loop().time(),
        operation=operation,
        completion=asyncio.get_running_loop().create_future(),
    )
    state.jobs_by_key[job.key] = job
    _attach_current_update_receipt(job)
    selected_lane.queue.put_nowait((job.priority, sequence, job))
    _ensure_workers(state, lane)
    return PrivilegedTaskSubmission(
        accepted=True,
        created=True,
        job_id=job_id,
        lane=lane,
        queue_depth=selected_lane.queue.qsize(),
    )


async def wait_privileged_task(job_id: str, *, timeout_seconds: float = 10.0) -> bool:
    """Test/diagnostic helper to await a queued or active job by id."""

    state = _runner_state()
    for job in state.jobs_by_key.values():
        if job.job_id == job_id:
            try:
                return bool(
                    await asyncio.wait_for(
                        asyncio.shield(job.completion),
                        timeout=max(0.1, float(timeout_seconds)),
                    )
                )
            except asyncio.TimeoutError:
                return False
    return True


def privileged_tasks_health_snapshot() -> dict[str, int | float | bool]:
    try:
        state = _runner_state()
    except RuntimeError:
        return {
            "ok": True,
            "fatal": False,
            "draining": False,
            "critical_queued": 0,
            "critical_bulk_queued": 0,
            "security_queued": 0,
            "policy_queued": 0,
            "profile_queued": 0,
            "active": 0,
            "tracked": 0,
            "critical_queue_full": False,
            "critical_bulk_queue_full": False,
            "security_queue_full": False,
            "policy_queue_full": False,
            "profile_queue_full": False,
            "orphans": 0,
            "oldest_active_seconds": 0.0,
            "oldest_critical_queued_seconds": 0.0,
            "oldest_critical_bulk_queued_seconds": 0.0,
            "oldest_security_queued_seconds": 0.0,
            "oldest_policy_queued_seconds": 0.0,
            "oldest_profile_queued_seconds": 0.0,
            "active_deadline_overrun_seconds": 0.0,
            "oldest_orphan_seconds": 0.0,
            "reaper_alive": False,
        }
    now = asyncio.get_running_loop().time()
    active_ages = [max(0.0, now - value) for value in state.active_started_at.values()]
    active_deadline_overrun = max(
        (max(0.0, now - value) for value in state.active_deadline_at.values()),
        default=0.0,
    )
    orphan_ages = [max(0.0, now - value) for value in state.orphan_started_at.values()]

    def oldest_queued_age(lane_name: PrivilegedLane) -> float:
        lane = state.lanes[lane_name]
        queued = getattr(lane.queue, "_queue", ())
        return max(
            (
                max(0.0, now - item[2].enqueued_at)
                for item in queued
                if len(item) >= 3
            ),
            default=0.0,
        )

    oldest_critical_queued = oldest_queued_age("critical")
    oldest_critical_bulk_queued = oldest_queued_age("critical_bulk")
    oldest_security_queued = oldest_queued_age("security")
    oldest_policy_queued = oldest_queued_age("policy")
    oldest_profile_queued = oldest_queued_age("profile")
    critical_full = state.lanes["critical"].queue.full()
    critical_bulk_full = state.lanes["critical_bulk"].queue.full()
    security_full = state.lanes["security"].queue.full()
    policy_full = state.lanes["policy"].queue.full()
    profile_full = state.lanes["profile"].queue.full()
    orphan_count = sum(not task.done() for task in state.orphan_tasks)
    oldest_orphan = max(orphan_ages, default=0.0)
    return {
        "ok": bool(
            not critical_full
            and not critical_bulk_full
            and not security_full
            and not policy_full
            and not profile_full
            and orphan_count == 0
            and oldest_critical_queued < 2.0
            and oldest_critical_bulk_queued < 10.0
            and oldest_security_queued < 10.0
            and oldest_policy_queued < 30.0
            and oldest_profile_queued < 30.0
            and active_deadline_overrun == 0.0
        ),
        "fatal": bool(
            orphan_count >= _CRITICAL_WORKERS
            or oldest_orphan >= 120.0
            or oldest_critical_queued >= 30.0
            or oldest_critical_bulk_queued >= 120.0
            or oldest_security_queued >= 60.0
            or oldest_policy_queued >= 180.0
            or oldest_profile_queued >= 180.0
            or active_deadline_overrun >= 30.0
        ),
        "draining": state.draining,
        "critical_queued": state.lanes["critical"].queue.qsize(),
        "critical_bulk_queued": state.lanes["critical_bulk"].queue.qsize(),
        "security_queued": state.lanes["security"].queue.qsize(),
        "policy_queued": state.lanes["policy"].queue.qsize(),
        "profile_queued": state.lanes["profile"].queue.qsize(),
        "active": len(state.active_tasks),
        "tracked": len(state.jobs_by_key),
        "critical_queue_full": critical_full,
        "critical_bulk_queue_full": critical_bulk_full,
        "security_queue_full": security_full,
        "policy_queue_full": policy_full,
        "profile_queue_full": profile_full,
        "orphans": orphan_count,
        "oldest_active_seconds": round(max(active_ages, default=0.0), 3),
        "oldest_critical_queued_seconds": round(oldest_critical_queued, 3),
        "oldest_critical_bulk_queued_seconds": round(
            oldest_critical_bulk_queued,
            3,
        ),
        "oldest_security_queued_seconds": round(oldest_security_queued, 3),
        "oldest_policy_queued_seconds": round(oldest_policy_queued, 3),
        "oldest_profile_queued_seconds": round(oldest_profile_queued, 3),
        "active_deadline_overrun_seconds": round(active_deadline_overrun, 3),
        "oldest_orphan_seconds": round(oldest_orphan, 3),
        "reaper_alive": bool(
            state.reaper_task is not None and not state.reaper_task.done()
        ),
    }


async def flush_privileged_tasks(*, timeout_seconds: float = 10.0) -> None:
    """Stop acceptance, drain briefly, then boundedly cancel owned work."""

    state = _runner_state()
    state.draining = True
    reaper = state.reaper_task
    if reaper is not None and not reaper.done():
        reaper.cancel()
        try:
            await reaper
        except asyncio.CancelledError:
            pass
    lane_joins = [lane.queue.join() for lane in state.lanes.values()]
    try:
        await asyncio.wait_for(
            asyncio.gather(*lane_joins),
            timeout=max(0.0, float(timeout_seconds)),
        )
    except asyncio.TimeoutError:
        pass

    workers = {
        task
        for lane in state.lanes.values()
        for task in lane.workers
        if not task.done()
    }
    active = {task for task in state.active_tasks.values() if not task.done()}
    orphans = {task for task in state.orphan_tasks if not task.done()}
    for task in {*workers, *active, *orphans}:
        task.cancel()
    pending = {*workers, *active, *orphans}
    if pending:
        _done, pending = await asyncio.wait(
            pending,
            timeout=_SHUTDOWN_CANCEL_GRACE_SECONDS,
        )
    if pending:
        log.error(
            "%d privileged task(s) ignored bounded shutdown cancellation",
            len(pending),
        )
        for task in pending:
            task.add_done_callback(_consume_task_result)

    # Retire queued jobs that never reached a worker.  Their callers already
    # received an in-process acceptance, so keep this count visible in logs.
    discarded = 0
    for lane in state.lanes.values():
        while True:
            try:
                _priority, _sequence, job = lane.queue.get_nowait()
            except asyncio.QueueEmpty:
                break
            discarded += 1
            _finish_queued_job(state, job, succeeded=False)
            lane.queue.task_done()
    if discarded:
        log.error("discarded %d queued privileged task(s) during shutdown", discarded)


register_resource_health_provider(
    "privileged_tasks",
    privileged_tasks_health_snapshot,
)
