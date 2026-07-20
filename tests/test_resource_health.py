from __future__ import annotations

import unittest
import threading
import time
from unittest.mock import patch

from bot.services import resource_health


class ResourceHealthTests(unittest.IsolatedAsyncioTestCase):
    def test_hard_loop_watchdog_schedules_heartbeat_before_async_startup(self) -> None:
        callbacks: list[object] = []

        class FakeLoop:
            def call_soon(self, callback: object) -> None:
                callbacks.append(callback)

            def call_later(self, _delay: float, callback: object) -> None:
                callbacks.append(callback)

            def is_closed(self) -> bool:
                return False

        stop = resource_health.start_hard_loop_watchdog(
            FakeLoop(),  # type: ignore[arg-type]
            stall_seconds=60.0,
            exit_process=lambda _code: None,
        )
        try:
            self.assertEqual(len(callbacks), 1)
            callback = callbacks.pop()
            self.assertTrue(callable(callback))
            callback()  # type: ignore[operator]
            self.assertEqual(len(callbacks), 1)
        finally:
            stop.set()

    def test_hard_loop_monitor_requests_exit_after_stale_heartbeat(self) -> None:
        stop = threading.Event()
        exit_codes: list[int] = []
        with patch.object(
            resource_health,
            "_HARD_LOOP_WATCH_INTERVAL_SECONDS",
            0.001,
        ):
            resource_health._hard_loop_stall_monitor(
                stop=stop,
                heartbeat=[time.monotonic() - 10.0],
                stall_seconds=0.01,
                exit_process=exit_codes.append,
            )
        self.assertEqual(exit_codes, [75])

    def test_provider_failure_is_visible_without_crashing_health_endpoint(self) -> None:
        with (
            patch.dict(resource_health._PROVIDERS, {}, clear=True),
            patch.object(
                resource_health,
                "_system_snapshot",
                return_value={"ok": True, "fatal": False},
            ),
        ):
            resource_health.register_resource_health_provider(
                "broken",
                lambda: (_ for _ in ()).throw(RuntimeError("boom")),
            )
            snapshot = resource_health.resource_health_snapshot()
        self.assertFalse(snapshot["ok"])
        self.assertFalse(snapshot["fatal"])
        self.assertIn("boom", snapshot["resources"]["broken"]["error"])

    def test_system_snapshot_detects_absolute_host_pressure(self) -> None:
        cgroup_values = {
            "/sys/fs/cgroup/memory.current": None,
            "/sys/fs/cgroup/memory.max": None,
            "/sys/fs/cgroup/memory.swap.current": 600 * resource_health._MIB,
            "/sys/fs/cgroup/memory.swap.max": 1024 * resource_health._MIB,
            "/sys/fs/cgroup/pids.current": 98,
            "/sys/fs/cgroup/pids.max": 100,
        }
        disk = {
            "path": "data",
            "free_bytes": 128 * resource_health._MIB,
            "total_bytes": 10 * resource_health._GIB,
            "free_ratio": 0.0125,
        }
        with (
            patch.object(
                resource_health,
                "_proc_status",
                return_value={
                    "VmRSS": 3 * resource_health._GIB,
                    "VmSwap": 600 * resource_health._MIB,
                    "Threads": 300,
                },
            ),
            patch.object(
                resource_health,
                "_read_int",
                side_effect=cgroup_values.get,
            ),
            patch.object(resource_health, "_disk_snapshot", return_value=disk),
            patch.object(resource_health.os, "listdir", return_value=["fd"] * 4100),
            patch.object(
                resource_health.resource,
                "getrlimit",
                return_value=(8192, 8192),
            ),
        ):
            snapshot = resource_health._system_snapshot()

        self.assertFalse(snapshot["ok"])
        self.assertTrue(snapshot["fatal"])
        self.assertIn("process_rss", snapshot["fatal_reasons"])
        self.assertIn("swap", snapshot["fatal_reasons"])
        self.assertIn("file_descriptors", snapshot["fatal_reasons"])
        self.assertIn("threads", snapshot["fatal_reasons"])
        self.assertIn("cgroup_pids", snapshot["fatal_reasons"])
        self.assertIn("data_disk", snapshot["fatal_reasons"])

    async def test_watchdog_raises_after_persistent_fatal_state(self) -> None:
        with (
            patch.dict(resource_health._PROVIDERS, {}, clear=True),
            patch.object(
                resource_health,
                "_system_snapshot",
                return_value={"ok": True, "fatal": False},
            ),
        ):
            resource_health.register_resource_health_provider(
                "stuck",
                lambda: {"ok": False, "fatal": True},
            )
            with self.assertRaisesRegex(RuntimeError, "supervised restart"):
                await resource_health.run_resource_watchdog(
                    interval_seconds=0.01,
                    consecutive_fatal_passes=2,
                )

    async def test_watchdog_raises_after_persistent_nonfatal_degradation(self) -> None:
        with (
            patch.dict(resource_health._PROVIDERS, {}, clear=True),
            patch.object(
                resource_health,
                "_system_snapshot",
                return_value={"ok": True, "fatal": False},
            ),
        ):
            resource_health.register_resource_health_provider(
                "degraded",
                lambda: {"ok": False, "fatal": False},
            )
            with self.assertRaisesRegex(RuntimeError, "remained unhealthy"):
                await resource_health.run_resource_watchdog(
                    interval_seconds=0.01,
                    consecutive_fatal_passes=99,
                    consecutive_unhealthy_passes=2,
                )


if __name__ == "__main__":
    unittest.main()
