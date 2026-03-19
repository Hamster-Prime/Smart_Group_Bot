import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

from bot.config import BotConfig
from bot.db.engine import init_db
from bot.services.scheduled_tasks import (
    create_scheduled_task,
    compute_next_cooldown_run_at,
    format_task_summary,
    get_cooldown_status_text,
    is_cooldown_task_enabled,
    match_scheduled_task_for_cancel,
    mark_cooldown_task_processed,
    record_group_activity,
    set_cooldown_task_enabled,
)


class ScheduledTaskTests(unittest.TestCase):
    def _config(self) -> BotConfig:
        cfg = BotConfig()
        cfg.proactive_default_enabled = False
        cfg.proactive_idle_minutes = 180
        cfg.proactive_jitter_minutes = 0
        cfg.proactive_quiet_hours_start = 0
        cfg.proactive_quiet_hours_end = 9
        cfg.proactive_retry_minutes = 30
        return cfg

    def test_compute_next_run_shifts_out_of_quiet_hours(self) -> None:
        cfg = self._config()
        tz = timezone(timedelta(hours=8))
        anchor = datetime(2026, 3, 19, 22, 30, tzinfo=tz)

        next_run = compute_next_cooldown_run_at(anchor, cfg)

        self.assertEqual(next_run, datetime(2026, 3, 20, 9, 30, tzinfo=tz))

    def test_enable_task_sets_independent_group_schedule(self) -> None:
        cfg = self._config()
        tz = timezone(timedelta(hours=8))
        now = datetime(2026, 3, 19, 10, 0, tzinfo=tz)

        settings_data = set_cooldown_task_enabled({}, enabled=True, config=cfg, at=now)

        self.assertTrue(is_cooldown_task_enabled(settings_data, default_enabled=False))
        status = get_cooldown_status_text(settings_data, default_enabled=False)
        self.assertIn("已开启", status)
        self.assertIn("最早下次执行", status)

    def test_record_activity_allows_next_cycle_after_processed(self) -> None:
        cfg = self._config()
        tz = timezone(timedelta(hours=8))
        first = datetime(2026, 3, 19, 12, 0, tzinfo=tz)
        second = datetime(2026, 3, 19, 13, 0, tzinfo=tz)

        settings_data = record_group_activity({}, cfg, at=first)
        settings_data = mark_cooldown_task_processed(
            settings_data,
            last_user_at=first,
            sent_at=first + timedelta(hours=3),
        )
        settings_data = record_group_activity(settings_data, cfg, at=second)

        status = get_cooldown_status_text(settings_data, default_enabled=False)
        self.assertIn("最近活跃", status)
        self.assertIn("最近主动发送", status)

class SemanticCancelMatchTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        fd, self._db_path = tempfile.mkstemp(
            prefix="scheduled-task-tests-",
            suffix=".db",
            dir=os.getcwd(),
        )
        os.close(fd)
        self.engine, self.session_factory = await init_db(f"sqlite+aiosqlite:///./{os.path.basename(self._db_path)}")

    async def asyncTearDown(self) -> None:
        await self.engine.dispose()
        for suffix in ("", "-wal", "-shm"):
            try:
                os.remove(self._db_path + suffix)
            except FileNotFoundError:
                pass

    async def test_match_semantic_cancel_by_due_at_and_content(self) -> None:
        tz = timezone(timedelta(hours=8))
        due_at = datetime(2026, 3, 19, 21, 0, tzinfo=tz)

        async with self.session_factory() as session:
            target = await create_scheduled_task(
                session,
                group_id=1001,
                creator_user_id=7,
                creator_name="Alice",
                due_at=due_at,
                task_type="reminder",
                content="吃饭",
            )
            await create_scheduled_task(
                session,
                group_id=1001,
                creator_user_id=7,
                creator_name="Alice",
                due_at=due_at + timedelta(hours=1),
                task_type="reminder",
                content="洗衣服",
            )
            await session.commit()

            matched, candidates = await match_scheduled_task_for_cancel(
                session,
                group_id=1001,
                operator_user_id=7,
                task_type="reminder",
                due_at=due_at,
                task_content="吃饭",
            )

        self.assertIsNotNone(matched)
        self.assertEqual(matched.id, target.id)
        self.assertEqual(candidates, [])

    async def test_match_semantic_cancel_reports_ambiguous_content_only(self) -> None:
        tz = timezone(timedelta(hours=8))

        async with self.session_factory() as session:
            await create_scheduled_task(
                session,
                group_id=2002,
                creator_user_id=9,
                creator_name="Bob",
                due_at=datetime(2026, 3, 19, 21, 0, tzinfo=tz),
                task_type="reminder",
                content="吃饭",
            )
            await create_scheduled_task(
                session,
                group_id=2002,
                creator_user_id=9,
                creator_name="Bob",
                due_at=datetime(2026, 3, 20, 21, 0, tzinfo=tz),
                task_type="reminder",
                content="吃饭",
            )
            await session.commit()

            matched, candidates = await match_scheduled_task_for_cancel(
                session,
                group_id=2002,
                operator_user_id=9,
                task_content="吃饭",
            )

        self.assertIsNone(matched)
        self.assertEqual(len(candidates), 2)
        self.assertIn("吃饭", format_task_summary(candidates[0]))

    async def test_match_semantic_cancel_without_query_uses_single_accessible_task(self) -> None:
        tz = timezone(timedelta(hours=8))

        async with self.session_factory() as session:
            target = await create_scheduled_task(
                session,
                group_id=3003,
                creator_user_id=11,
                creator_name="Carol",
                due_at=datetime(2026, 3, 19, 9, 0, tzinfo=tz),
                task_type="agent_task",
                content="查询今天的科技新闻并概述",
            )
            await create_scheduled_task(
                session,
                group_id=3003,
                creator_user_id=12,
                creator_name="Dave",
                due_at=datetime(2026, 3, 19, 10, 0, tzinfo=tz),
                task_type="reminder",
                content="喝水",
            )
            await session.commit()

            matched, candidates = await match_scheduled_task_for_cancel(
                session,
                group_id=3003,
                operator_user_id=11,
            )

        self.assertIsNotNone(matched)
        self.assertEqual(matched.id, target.id)
        self.assertEqual(candidates, [])


if __name__ == "__main__":
    unittest.main()
