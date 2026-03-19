import unittest
from datetime import datetime, timedelta, timezone

from bot.config import BotConfig
from bot.services.scheduled_tasks import (
    compute_next_cooldown_run_at,
    get_cooldown_status_text,
    is_cooldown_task_enabled,
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


if __name__ == "__main__":
    unittest.main()
