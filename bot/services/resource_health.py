"""Process-wide resource telemetry and a fail-fast recovery watchdog."""

from __future__ import annotations

import asyncio
import logging
import os
import resource
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

ResourceSnapshotProvider = Callable[[], dict[str, Any]]

_PROVIDERS: dict[str, ResourceSnapshotProvider] = {}
_LAST_LOOP_LAG_SECONDS = 0.0
_MAX_LOOP_LAG_SECONDS = 0.0
_WATCHDOG_CONSECUTIVE_FATAL_PASSES = 3
_WATCHDOG_CONSECUTIVE_UNHEALTHY_PASSES = 12
_WATCHDOG_INTERVAL_SECONDS = 5.0
_LOOP_LAG_DEGRADED_SECONDS = 2.0
_LOOP_LAG_FATAL_SECONDS = 10.0
_HARD_LOOP_STALL_SECONDS = 45.0
_HARD_LOOP_WATCH_INTERVAL_SECONDS = 1.0
_MIB = 1024 * 1024
_GIB = 1024 * _MIB
_SWAP_DEGRADED_BYTES = 128 * _MIB
_SWAP_FATAL_BYTES = 512 * _MIB
_RSS_FALLBACK_DEGRADED_BYTES = 1536 * _MIB
_RSS_FALLBACK_FATAL_BYTES = 2048 * _MIB
_FD_ABSOLUTE_DEGRADED = 2048
_FD_ABSOLUTE_FATAL = 4096
_THREADS_ABSOLUTE_DEGRADED = 128
_THREADS_ABSOLUTE_FATAL = 256
_PIDS_ABSOLUTE_DEGRADED = 256
_PIDS_ABSOLUTE_FATAL = 512
_DISK_FREE_DEGRADED_BYTES = 1 * _GIB
_DISK_FREE_FATAL_BYTES = 256 * _MIB
_DISK_FREE_DEGRADED_RATIO = 0.10
_DISK_FREE_FATAL_RATIO = 0.02


def register_resource_health_provider(
    name: str,
    provider: ResourceSnapshotProvider,
) -> None:
    _PROVIDERS[str(name)] = provider


def unregister_resource_health_provider(
    name: str,
    provider: ResourceSnapshotProvider | None = None,
) -> None:
    """Remove one provider without deleting a newer replacement by mistake."""

    normalized = str(name)
    current = _PROVIDERS.get(normalized)
    if current is None:
        return
    if provider is not None and current is not provider:
        return
    _PROVIDERS.pop(normalized, None)


def _read_int(path: str) -> int | None:
    try:
        raw = Path(path).read_text(encoding="utf-8").strip()
    except (OSError, ValueError):
        return None
    if not raw or raw == "max":
        return None
    try:
        return int(raw)
    except ValueError:
        return None


def _proc_status() -> dict[str, int]:
    values: dict[str, int] = {}
    try:
        lines = Path("/proc/self/status").read_text(encoding="utf-8").splitlines()
    except OSError:
        return values
    for line in lines:
        key, separator, raw = line.partition(":")
        if not separator or key not in {"VmRSS", "VmSwap", "Threads"}:
            continue
        token = raw.strip().split(maxsplit=1)[0]
        try:
            number = int(token)
        except ValueError:
            continue
        values[key] = number * 1024 if key != "Threads" else number
    return values


def _ratio(current: int | None, maximum: int | None) -> float | None:
    if current is None or maximum in (None, 0):
        return None
    return float(current) / float(maximum)


def _disk_snapshot(path: str = "data") -> dict[str, int | float | str | None]:
    try:
        stats = os.statvfs(path)
    except OSError:
        return {
            "path": path,
            "free_bytes": None,
            "total_bytes": None,
            "free_ratio": None,
        }
    fragment_size = int(stats.f_frsize or stats.f_bsize)
    free_bytes = int(stats.f_bavail) * fragment_size
    total_bytes = int(stats.f_blocks) * fragment_size
    free_ratio = (
        float(free_bytes) / float(total_bytes)
        if total_bytes > 0
        else None
    )
    return {
        "path": path,
        "free_bytes": free_bytes,
        "total_bytes": total_bytes,
        "free_ratio": round(free_ratio, 4) if free_ratio is not None else None,
    }


def _system_snapshot() -> dict[str, Any]:
    status = _proc_status()
    try:
        fd_count = len(os.listdir("/proc/self/fd"))
    except OSError:
        fd_count = 0
    soft_fd_limit, _hard_fd_limit = resource.getrlimit(resource.RLIMIT_NOFILE)
    cgroup_current = _read_int("/sys/fs/cgroup/memory.current")
    cgroup_max = _read_int("/sys/fs/cgroup/memory.max")
    cgroup_swap_current = _read_int("/sys/fs/cgroup/memory.swap.current")
    cgroup_swap_max = _read_int("/sys/fs/cgroup/memory.swap.max")
    cgroup_pids_current = _read_int("/sys/fs/cgroup/pids.current")
    cgroup_pids_max = _read_int("/sys/fs/cgroup/pids.max")
    memory_ratio = _ratio(cgroup_current, cgroup_max)
    swap_ratio = _ratio(cgroup_swap_current, cgroup_swap_max)
    pids_ratio = _ratio(cgroup_pids_current, cgroup_pids_max)
    fd_ratio = (
        float(fd_count) / float(soft_fd_limit)
        if soft_fd_limit not in (0, resource.RLIM_INFINITY)
        else None
    )
    rss_bytes = status.get("VmRSS", 0)
    process_swap_bytes = status.get("VmSwap", 0)
    effective_swap_bytes = max(process_swap_bytes, cgroup_swap_current or 0)
    threads = status.get("Threads", 0)
    disk = _disk_snapshot()
    disk_free_bytes = disk["free_bytes"]
    disk_free_ratio = disk["free_ratio"]

    degraded_reasons: list[str] = []
    fatal_reasons: list[str] = []

    def record(
        name: str,
        *,
        degraded_when: bool,
        fatal_when: bool,
    ) -> None:
        if degraded_when:
            degraded_reasons.append(name)
        if fatal_when:
            fatal_reasons.append(name)

    record(
        "event_loop_lag",
        degraded_when=_LAST_LOOP_LAG_SECONDS >= _LOOP_LAG_DEGRADED_SECONDS,
        fatal_when=_LAST_LOOP_LAG_SECONDS >= _LOOP_LAG_FATAL_SECONDS,
    )
    record(
        "cgroup_memory",
        degraded_when=memory_ratio is not None and memory_ratio >= 0.85,
        fatal_when=memory_ratio is not None and memory_ratio >= 0.95,
    )
    # Bare-metal/manual deployments may not have a finite cgroup memory.max.
    # Retain an absolute RSS guard so a process cannot silently consume the host.
    record(
        "process_rss",
        degraded_when=(
            memory_ratio is None
            and rss_bytes >= _RSS_FALLBACK_DEGRADED_BYTES
        ),
        fatal_when=(
            memory_ratio is None
            and rss_bytes >= _RSS_FALLBACK_FATAL_BYTES
        ),
    )
    record(
        "swap",
        degraded_when=(
            effective_swap_bytes >= _SWAP_DEGRADED_BYTES
            or swap_ratio is not None
            and swap_ratio >= 0.50
        ),
        fatal_when=(
            effective_swap_bytes >= _SWAP_FATAL_BYTES
            or swap_ratio is not None
            and swap_ratio >= 0.90
        ),
    )
    record(
        "file_descriptors",
        degraded_when=(
            fd_count >= _FD_ABSOLUTE_DEGRADED
            or fd_ratio is not None
            and fd_ratio >= 0.80
        ),
        fatal_when=(
            fd_count >= _FD_ABSOLUTE_FATAL
            or fd_ratio is not None
            and fd_ratio >= 0.95
        ),
    )
    record(
        "threads",
        degraded_when=threads >= _THREADS_ABSOLUTE_DEGRADED,
        fatal_when=threads >= _THREADS_ABSOLUTE_FATAL,
    )
    record(
        "cgroup_pids",
        degraded_when=(
            cgroup_pids_current is not None
            and cgroup_pids_current >= _PIDS_ABSOLUTE_DEGRADED
            or pids_ratio is not None
            and pids_ratio >= 0.80
        ),
        fatal_when=(
            cgroup_pids_current is not None
            and cgroup_pids_current >= _PIDS_ABSOLUTE_FATAL
            or pids_ratio is not None
            and pids_ratio >= 0.95
        ),
    )
    record(
        "data_disk",
        degraded_when=(
            disk_free_bytes is not None
            and disk_free_bytes < _DISK_FREE_DEGRADED_BYTES
            or disk_free_ratio is not None
            and disk_free_ratio < _DISK_FREE_DEGRADED_RATIO
        ),
        fatal_when=(
            disk_free_bytes is not None
            and disk_free_bytes < _DISK_FREE_FATAL_BYTES
            or disk_free_ratio is not None
            and disk_free_ratio < _DISK_FREE_FATAL_RATIO
        ),
    )

    degraded = bool(degraded_reasons)
    fatal = bool(fatal_reasons)
    return {
        "ok": not degraded,
        "fatal": fatal,
        "degraded_reasons": degraded_reasons,
        "fatal_reasons": fatal_reasons,
        "event_loop_lag_seconds": round(_LAST_LOOP_LAG_SECONDS, 4),
        "max_event_loop_lag_seconds": round(_MAX_LOOP_LAG_SECONDS, 4),
        "rss_bytes": rss_bytes,
        "swap_bytes": process_swap_bytes,
        "effective_swap_bytes": effective_swap_bytes,
        "threads": threads,
        "fd_count": fd_count,
        "fd_soft_limit": soft_fd_limit,
        "cgroup_memory_current": cgroup_current,
        "cgroup_memory_max": cgroup_max,
        "cgroup_memory_ratio": round(memory_ratio, 4) if memory_ratio is not None else None,
        "cgroup_swap_current": cgroup_swap_current,
        "cgroup_swap_max": cgroup_swap_max,
        "cgroup_swap_ratio": round(swap_ratio, 4) if swap_ratio is not None else None,
        "cgroup_pids_current": cgroup_pids_current,
        "cgroup_pids_max": cgroup_pids_max,
        "cgroup_pids_ratio": round(pids_ratio, 4) if pids_ratio is not None else None,
        "data_disk": disk,
    }


def resource_health_snapshot() -> dict[str, Any]:
    providers: dict[str, dict[str, Any]] = {}
    ok = True
    fatal = False
    for name, provider in tuple(_PROVIDERS.items()):
        try:
            snapshot = dict(provider())
        except Exception as exc:
            log.exception("resource health provider failed | provider=%s", name)
            snapshot = {
                "ok": False,
                "fatal": False,
                "error": f"{type(exc).__name__}: {exc}",
            }
        providers[name] = snapshot
        ok = ok and bool(snapshot.get("ok", True))
        fatal = fatal or bool(snapshot.get("fatal", False))

    system = _system_snapshot()
    ok = ok and bool(system["ok"])
    fatal = fatal or bool(system["fatal"])
    return {
        "ok": ok,
        "fatal": fatal,
        "system": system,
        "resources": providers,
    }


def _hard_loop_stall_monitor(
    *,
    stop: threading.Event,
    heartbeat: list[float],
    stall_seconds: float,
    exit_process: Callable[[int], Any],
) -> None:
    """Watch the asyncio heartbeat from outside the event-loop thread.

    An asyncio watchdog cannot run while synchronous logging, tokenization or
    another accidental blocking call has frozen the loop.  This daemon thread
    is deliberately tiny and performs no routine I/O.  After a sustained hard
    stall it terminates the process so the configured service supervisor can
    rebuild every queue, connector and semaphore from a clean state.
    """

    deadline = max(1.0, float(stall_seconds))
    while not stop.wait(_HARD_LOOP_WATCH_INTERVAL_SECONDS):
        age = max(0.0, time.monotonic() - float(heartbeat[0]))
        if age < deadline:
            continue
        try:
            os.write(
                2,
                (
                    "resource watchdog: asyncio event loop stalled for "
                    f"{age:.1f}s; requesting supervised restart\n"
                ).encode("utf-8", errors="replace"),
            )
        except OSError:
            pass
        exit_process(75)
        return


def start_hard_loop_watchdog(
    loop: asyncio.AbstractEventLoop,
    *,
    stall_seconds: float = _HARD_LOOP_STALL_SECONDS,
    exit_process: Callable[[int], Any] | None = None,
) -> threading.Event:
    """Start a daemon watchdog that covers the loop's entire lifetime.

    This is deliberately started by the synchronous entrypoint before
    ``main()`` begins.  It therefore covers async startup and ordered cleanup,
    not only the steady-state background services.
    """

    stop = threading.Event()
    heartbeat = [time.monotonic()]

    def pulse() -> None:
        if stop.is_set() or loop.is_closed():
            return
        heartbeat[0] = time.monotonic()
        loop.call_later(_HARD_LOOP_WATCH_INTERVAL_SECONDS, pulse)

    loop.call_soon(pulse)
    thread = threading.Thread(
        target=_hard_loop_stall_monitor,
        kwargs={
            "stop": stop,
            "heartbeat": heartbeat,
            "stall_seconds": stall_seconds,
            "exit_process": exit_process or os._exit,
        },
        name="asyncio-hard-stall-watchdog",
        daemon=True,
    )
    thread.start()
    return stop


async def run_resource_watchdog(
    *,
    interval_seconds: float = _WATCHDOG_INTERVAL_SECONDS,
    consecutive_fatal_passes: int = _WATCHDOG_CONSECUTIVE_FATAL_PASSES,
    consecutive_unhealthy_passes: int = _WATCHDOG_CONSECUTIVE_UNHEALTHY_PASSES,
) -> None:
    """Raise when a process-local resource cannot recover without restart.

    The main supervisor treats this like any other permanent background service
    failure and exits cleanly.  With Compose/systemd restart policy this turns a
    silently wedged process into a short, observable recovery.
    """

    global _LAST_LOOP_LAG_SECONDS, _MAX_LOOP_LAG_SECONDS
    interval = max(0.25, float(interval_seconds))
    fatal_passes = 0
    unhealthy_passes = 0
    loop = asyncio.get_running_loop()
    expected = loop.time() + interval
    while True:
        await asyncio.sleep(interval)
        now = loop.time()
        _LAST_LOOP_LAG_SECONDS = max(0.0, now - expected)
        _MAX_LOOP_LAG_SECONDS = max(
            _MAX_LOOP_LAG_SECONDS,
            _LAST_LOOP_LAG_SECONDS,
        )
        expected = now + interval
        snapshot = resource_health_snapshot()
        if snapshot["fatal"]:
            fatal_passes += 1
            log.error(
                "resource watchdog detected fatal pressure | pass=%d/%d snapshot=%s",
                fatal_passes,
                consecutive_fatal_passes,
                snapshot,
            )
        else:
            fatal_passes = 0

        if snapshot["ok"]:
            unhealthy_passes = 0
        else:
            unhealthy_passes += 1
            if not snapshot["fatal"]:
                log.warning(
                    "resource watchdog detected persistent degradation | "
                    "pass=%d/%d snapshot=%s",
                    unhealthy_passes,
                    consecutive_unhealthy_passes,
                    snapshot,
                )

        if fatal_passes >= max(1, int(consecutive_fatal_passes)):
            raise RuntimeError(
                "process resources remained unrecoverable; requesting supervised restart"
            )
        if unhealthy_passes >= max(1, int(consecutive_unhealthy_passes)):
            raise RuntimeError(
                "process resources remained unhealthy; requesting supervised restart"
            )
