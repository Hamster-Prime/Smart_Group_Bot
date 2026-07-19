from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from bot.services import background_health
from bot.services.group_permissions import GroupPermissionService
from bot.services.join_verification import JoinVerificationSweeper
from bot.services.patrol import PatrolService
from bot.services.proactive import ProactiveTopicService
from bot.services.scheduled_messages import ScheduledMessageService


class BackgroundRunnerHealthTests(unittest.IsolatedAsyncioTestCase):
    async def _assert_runner_surfaces_persistent_failure(
        self,
        *,
        runner: object,
        method: object,
        module: str,
        expected: str,
    ) -> None:
        with (
            patch.object(background_health, "MAX_CONSECUTIVE_BACKGROUND_FAILURES", 2),
            patch(f"{module}.asyncio.sleep", new=AsyncMock()),
        ):
            with self.assertRaisesRegex(RuntimeError, expected):
                await method(runner)

    async def test_all_permanent_runners_exit_after_repeated_failures(self) -> None:
        failing = lambda: AsyncMock(side_effect=ValueError("persistent failure"))

        await self._assert_runner_surfaces_persistent_failure(
            runner=SimpleNamespace(
                run_once=failing(),
                settings=SimpleNamespace(
                    bot=SimpleNamespace(proactive_check_interval_seconds=15.0)
                ),
            ),
            method=ProactiveTopicService.run_forever,
            module="bot.services.proactive",
            expected="proactive topic loop failed 2 consecutive passes",
        )
        await self._assert_runner_surfaces_persistent_failure(
            runner=SimpleNamespace(run_once=failing(), check_interval_seconds=5.0),
            method=ScheduledMessageService.run_forever,
            module="bot.services.scheduled_messages",
            expected="scheduled message service failed 2 consecutive passes",
        )
        await self._assert_runner_surfaces_persistent_failure(
            runner=SimpleNamespace(run_once=failing(), check_interval_seconds=1.0),
            method=GroupPermissionService.run_forever,
            module="bot.services.group_permissions",
            expected="group default permission service failed 2 consecutive passes",
        )
        await self._assert_runner_surfaces_persistent_failure(
            runner=SimpleNamespace(
                run_once=failing(),
                _reset_stale_running_flags=AsyncMock(),
                settings=SimpleNamespace(patrol_check_interval_seconds=15.0),
            ),
            method=PatrolService.run_forever,
            module="bot.services.patrol",
            expected="profile patrol service failed 2 consecutive passes",
        )
        await self._assert_runner_surfaces_persistent_failure(
            runner=SimpleNamespace(sweep_once=failing(), check_interval_seconds=5.0),
            method=JoinVerificationSweeper.run_forever,
            module="bot.services.join_verification",
            expected="join verification sweeper failed 2 consecutive passes",
        )


if __name__ == "__main__":
    unittest.main()
