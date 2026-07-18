from __future__ import annotations

import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

from aiogram.types import ChatPermissions

from bot.db.models import AuthorizedGroup, Group
from bot.services.group_permissions import (
    GROUP_PERMISSIONS_SETTINGS_KEY,
    PERMISSION_FIELDS,
    GroupPermissionService,
    effective_group_permissions,
    fetch_telegram_default_permissions,
    next_group_permission_transition,
    normalize_group_permission_config,
    permission_field_document,
    repair_group_permissions_config,
    resolve_group_permissions,
    set_group_permissions_config,
    telegram_permissions_snapshot,
)


def _permissions(value: bool = True) -> dict[str, bool]:
    return {field: value for field in PERMISSION_FIELDS}


def _config(
    *,
    timezone_name: str = "Asia/Shanghai",
    schedule_enabled: bool = True,
    windows: list[dict] | None = None,
) -> dict:
    return {
        "version": 1,
        "timezone": timezone_name,
        "schedule_enabled": schedule_enabled,
        "base": _permissions(True),
        "windows": windows
        if windows is not None
        else [
            {
                "id": "night",
                "name": "夜间禁图",
                "enabled": True,
                "start": "23:00",
                "end": "07:00",
                "days": list(range(7)),
                "priority": 0,
                "overrides": {"can_send_photos": False},
            }
        ],
    }


class GroupPermissionConfigTests(unittest.TestCase):
    def test_exports_every_aiogram_chat_permission_field(self) -> None:
        self.assertEqual(PERMISSION_FIELDS, tuple(ChatPermissions.model_fields))
        metadata = permission_field_document()
        self.assertEqual([item["key"] for item in metadata], list(PERMISSION_FIELDS))
        self.assertIn("can_react_to_messages", PERMISSION_FIELDS)
        self.assertIn("can_edit_tag", PERMISSION_FIELDS)

    def test_normalization_requires_complete_strict_boolean_base(self) -> None:
        normalized = normalize_group_permission_config(
            {"base": _permissions(False)}
        )
        self.assertEqual(normalized["version"], 1)
        self.assertEqual(normalized["timezone"], "Asia/Shanghai")
        self.assertFalse(normalized["schedule_enabled"])
        self.assertEqual(normalized["windows"], [])

        missing = _permissions()
        missing.pop("can_send_photos")
        with self.assertRaisesRegex(ValueError, "missing permissions"):
            normalize_group_permission_config({"base": missing})

        non_boolean = _permissions()
        non_boolean["can_send_photos"] = 1  # type: ignore[assignment]
        with self.assertRaisesRegex(ValueError, "must be a boolean"):
            normalize_group_permission_config({"base": non_boolean})

    def test_rejects_bad_timezone_schedule_and_duplicate_ids(self) -> None:
        with self.assertRaisesRegex(ValueError, "unknown timezone"):
            normalize_group_permission_config(
                {**_config(), "timezone": "Mars/Olympus_Mons"}
            )

        bad_time = _config()
        bad_time["windows"][0]["start"] = "24:00"
        with self.assertRaisesRegex(ValueError, "HH:MM"):
            normalize_group_permission_config(bad_time)

        duplicate = _config()
        duplicate["windows"].append(dict(duplicate["windows"][0]))
        with self.assertRaisesRegex(ValueError, "duplicate"):
            normalize_group_permission_config(duplicate)

    def test_night_window_crosses_midnight_and_restores_base(self) -> None:
        config = _config()
        friday_night = resolve_group_permissions(
            config,
            at=datetime(2026, 7, 17, 23, 30),
        )
        saturday_morning = resolve_group_permissions(
            config,
            at=datetime(2026, 7, 18, 6, 59),
        )
        saturday_day = resolve_group_permissions(
            config,
            at=datetime(2026, 7, 18, 7, 0),
        )

        self.assertEqual(friday_night.active_window_ids, ("night",))
        self.assertFalse(friday_night.permissions["can_send_photos"])
        self.assertFalse(saturday_morning.permissions["can_send_photos"])
        self.assertEqual(saturday_day.active_window_ids, ())
        self.assertTrue(saturday_day.permissions["can_send_photos"])

    def test_weekdays_refer_to_window_start_day(self) -> None:
        config = _config()
        config["windows"][0]["days"] = [4]  # Friday starts only.
        self.assertTrue(
            effective_group_permissions(
                config,
                at=datetime(2026, 7, 17, 22, 59),
            )["can_send_photos"]
        )
        self.assertFalse(
            effective_group_permissions(
                config,
                at=datetime(2026, 7, 18, 6, 0),
            )["can_send_photos"]
        )
        self.assertTrue(
            effective_group_permissions(
                config,
                at=datetime(2026, 7, 19, 6, 0),
            )["can_send_photos"]
        )

    def test_groups_resolve_the_same_instant_in_their_own_timezone(self) -> None:
        instant = datetime(2026, 7, 17, 15, 30, tzinfo=timezone.utc)
        shanghai = resolve_group_permissions(_config(), at=instant)
        london = resolve_group_permissions(
            _config(timezone_name="Europe/London"),
            at=instant,
        )
        self.assertEqual(shanghai.local_datetime.hour, 23)
        self.assertFalse(shanghai.permissions["can_send_photos"])
        self.assertEqual(london.local_datetime.hour, 16)
        self.assertTrue(london.permissions["can_send_photos"])

    def test_overlapping_window_priority_is_deterministic(self) -> None:
        config = _config(
            windows=[
                {
                    "id": "quiet",
                    "name": "Quiet",
                    "start": "22:00",
                    "end": "08:00",
                    "overrides": {"can_send_photos": False},
                    "priority": 0,
                },
                {
                    "id": "event",
                    "name": "Event",
                    "start": "23:00",
                    "end": "01:00",
                    "overrides": {"can_send_photos": True},
                    "priority": 10,
                },
            ]
        )
        resolved = resolve_group_permissions(
            config,
            at=datetime(2026, 7, 17, 23, 30),
        )
        self.assertEqual(resolved.active_window_ids, ("quiet", "event"))
        self.assertTrue(resolved.permissions["can_send_photos"])

    def test_schedule_disable_applies_base_and_next_transition_is_reported(self) -> None:
        config = _config(schedule_enabled=False)
        self.assertTrue(
            effective_group_permissions(
                config,
                at=datetime(2026, 7, 17, 23, 30),
            )["can_send_photos"]
        )
        self.assertIsNone(next_group_permission_transition(config))

        enabled = _config()
        transition = next_group_permission_transition(
            enabled,
            at=datetime(2026, 7, 17, 23, 30),
        )
        self.assertIsNotNone(transition)
        self.assertEqual(transition.isoformat(), "2026-07-18T07:00:00+08:00")

    def test_equal_start_end_is_a_24_hour_window_without_fake_transitions(self) -> None:
        config = _config()
        window = config["windows"][0]
        window["start"] = "08:00"
        window["end"] = "08:00"

        for at in (
            datetime(2026, 7, 17, 7, 59),
            datetime(2026, 7, 17, 8, 0),
            datetime(2026, 7, 17, 23, 59),
            datetime(2026, 7, 18, 7, 59),
        ):
            self.assertFalse(
                effective_group_permissions(config, at=at)["can_send_photos"]
            )
        # Every day hands off to the next identical 24-hour window at 08:00,
        # so there is no effective permission transition to report.
        self.assertIsNone(
            next_group_permission_transition(
                config,
                at=datetime(2026, 7, 17, 7, 0),
            )
        )

    def test_equal_start_end_on_one_weekday_ends_exactly_next_day(self) -> None:
        config = _config()
        window = config["windows"][0]
        window.update({"start": "08:00", "end": "08:00", "days": [0]})

        # 2026-07-20 is Monday: the window starts Monday 08:00 and remains
        # active for a full 24 hours, ending Tuesday 08:00.
        self.assertTrue(
            effective_group_permissions(
                config,
                at=datetime(2026, 7, 20, 7, 59),
            )["can_send_photos"]
        )
        self.assertFalse(
            effective_group_permissions(
                config,
                at=datetime(2026, 7, 20, 8, 0),
            )["can_send_photos"]
        )
        self.assertFalse(
            effective_group_permissions(
                config,
                at=datetime(2026, 7, 21, 7, 59),
            )["can_send_photos"]
        )
        self.assertTrue(
            effective_group_permissions(
                config,
                at=datetime(2026, 7, 21, 8, 0),
            )["can_send_photos"]
        )
        transition = next_group_permission_transition(
            config,
            at=datetime(2026, 7, 20, 9, 0),
        )
        self.assertIsNotNone(transition)
        self.assertEqual(transition.isoformat(), "2026-07-21T08:00:00+08:00")

    def test_settings_copy_preserves_unrelated_group_values(self) -> None:
        updated = set_group_permissions_config(
            {"welcome_message": "hello", "raid_guard_enabled": True},
            _config(),
        )
        self.assertEqual(updated["welcome_message"], "hello")
        self.assertTrue(updated["raid_guard_enabled"])
        self.assertIn(GROUP_PERMISSIONS_SETTINGS_KEY, updated)

    def test_repairs_legacy_config_when_bot_api_adds_permission_fields(self) -> None:
        legacy = _config()
        legacy["base"].pop("can_edit_tag")
        legacy["base"]["can_send_photos"] = False
        legacy["windows"][0]["overrides"]["removed_legacy_field"] = True
        live = _permissions(True)
        live["can_edit_tag"] = False

        repaired = repair_group_permissions_config(
            legacy,
            fallback_base=live,
        )

        self.assertFalse(repaired["base"]["can_edit_tag"])
        self.assertFalse(repaired["base"]["can_send_photos"])
        self.assertEqual(repaired["windows"][0]["id"], "night")
        self.assertNotIn(
            "removed_legacy_field",
            repaired["windows"][0]["overrides"],
        )

    def test_repair_falls_back_from_invalid_timezone_and_windows(self) -> None:
        legacy = _config(timezone_name="Mars/Olympus")
        legacy["windows"][0]["start"] = "25:00"

        repaired = repair_group_permissions_config(
            legacy,
            fallback_base=_permissions(False),
        )

        self.assertEqual(repaired["timezone"], "Asia/Shanghai")
        self.assertEqual(repaired["windows"], [])

    def test_telegram_snapshot_is_complete_and_conservative(self) -> None:
        snapshot = telegram_permissions_snapshot(
            ChatPermissions(can_send_messages=True, can_send_photos=True)
        )
        self.assertEqual(set(snapshot), set(PERMISSION_FIELDS))
        self.assertTrue(snapshot["can_send_messages"])
        self.assertTrue(snapshot["can_send_photos"])
        self.assertFalse(snapshot["can_pin_messages"])


class GroupPermissionServiceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.group_rows: dict[int, dict] = {}
        self.authorized_group_ids: set[int] = set()

        class _Result:
            def __init__(inner_self, rows: list[tuple[int]]) -> None:
                inner_self._rows = rows

            def all(inner_self) -> list[tuple[int]]:
                return list(inner_self._rows)

        class _Session:
            async def __aenter__(inner_self):
                return inner_self

            async def __aexit__(inner_self, _exc_type, _exc, _traceback) -> None:
                return None

            async def execute(inner_self, _statement):
                return _Result(
                    [(group_id,) for group_id in sorted(self.authorized_group_ids)]
                )

            async def get(inner_self, model, group_id: int):
                if model is AuthorizedGroup:
                    return (
                        SimpleNamespace(group_id=group_id)
                        if group_id in self.authorized_group_ids
                        else None
                    )
                if model is Group and group_id in self.group_rows:
                    return SimpleNamespace(
                        id=group_id,
                        settings=self.group_rows[group_id],
                    )
                return None

        self.session_factory = lambda: _Session()
        self.bot = SimpleNamespace(set_chat_permissions=AsyncMock(return_value=True))

    async def _add_group(
        self,
        group_id: int,
        config: dict,
        *,
        authorized: bool = True,
    ) -> None:
        self.group_rows[group_id] = {
            GROUP_PERMISSIONS_SETTINGS_KEY: config,
        }
        if authorized:
            self.authorized_group_ids.add(group_id)

    async def test_per_group_scan_sends_complete_independent_permissions(self) -> None:
        await self._add_group(-1001, _config())
        await self._add_group(-1002, _config(schedule_enabled=False))
        await self._add_group(-1003, _config(), authorized=False)
        service = GroupPermissionService(
            bot=self.bot,
            session_factory=self.session_factory,
            reconcile_interval_seconds=3600,
        )

        applied = await service.run_once(at=datetime(2026, 7, 17, 23, 30))
        self.assertEqual(applied, 2)
        self.assertEqual(self.bot.set_chat_permissions.await_count, 2)
        calls = {
            call.kwargs["chat_id"]: call.kwargs
            for call in self.bot.set_chat_permissions.await_args_list
        }
        self.assertNotIn(-1003, calls)
        self.assertTrue(calls[-1001]["use_independent_chat_permissions"])
        self.assertFalse(calls[-1001]["permissions"].can_send_photos)
        self.assertTrue(calls[-1002]["permissions"].can_send_photos)
        for group_call in calls.values():
            dumped = group_call["permissions"].model_dump()
            self.assertTrue(all(dumped[field] is not None for field in PERMISSION_FIELDS))

    async def test_transition_applies_once_and_new_process_reconciles_on_restart(self) -> None:
        await self._add_group(-1101, _config())
        service = GroupPermissionService(
            bot=self.bot,
            session_factory=self.session_factory,
            reconcile_interval_seconds=3600,
        )
        self.assertEqual(
            await service.run_once(at=datetime(2026, 7, 17, 23, 30)),
            1,
        )
        self.assertEqual(
            await service.run_once(at=datetime(2026, 7, 18, 1, 0)),
            0,
        )
        self.assertEqual(
            await service.run_once(at=datetime(2026, 7, 18, 7, 0)),
            1,
        )
        self.assertEqual(self.bot.set_chat_permissions.await_count, 2)

        restarted = GroupPermissionService(
            bot=self.bot,
            session_factory=self.session_factory,
            reconcile_interval_seconds=3600,
        )
        self.assertEqual(
            await restarted.run_once(at=datetime(2026, 7, 18, 8, 0)),
            1,
        )
        self.assertEqual(self.bot.set_chat_permissions.await_count, 3)

    async def test_failed_apply_is_retried_and_only_success_updates_status(self) -> None:
        await self._add_group(-1201, _config())
        self.bot.set_chat_permissions.side_effect = [RuntimeError("temporary"), True]
        service = GroupPermissionService(
            bot=self.bot,
            session_factory=self.session_factory,
            reconcile_interval_seconds=3600,
        )
        self.assertEqual(
            await service.run_once(at=datetime(2026, 7, 17, 23, 30)),
            0,
        )
        self.assertFalse(service.status(-1201)["applied"])
        self.assertIn("temporary", service.status(-1201)["last_error"])

        self.assertEqual(
            await service.run_once(at=datetime(2026, 7, 17, 23, 31)),
            1,
        )
        self.assertTrue(service.status(-1201)["applied"])
        self.assertEqual(service.status(-1201)["last_error"], "")

    async def test_new_failure_does_not_report_an_older_state_as_applied(self) -> None:
        service = GroupPermissionService(
            bot=self.bot,
            session_factory=self.session_factory,
            reconcile_interval_seconds=3600,
        )
        self.assertTrue(
            await service.apply_group_now(
                -1251,
                _config(schedule_enabled=False),
                at=datetime(2026, 7, 17, 23, 30),
            )
        )
        self.bot.set_chat_permissions.side_effect = RuntimeError("new state failed")
        self.assertFalse(
            await service.apply_group_now(
                -1251,
                _config(),
                at=datetime(2026, 7, 17, 23, 30),
            )
        )
        self.assertFalse(service.status(-1251)["applied"])
        self.assertIn("new state failed", service.status(-1251)["last_error"])

    async def test_apply_group_now_supports_save_then_immediate_apply(self) -> None:
        service = GroupPermissionService(
            bot=self.bot,
            session_factory=self.session_factory,
            reconcile_interval_seconds=3600,
        )
        applied = await service.apply_group_now(
            -1301,
            _config(),
            at=datetime(2026, 7, 17, 23, 30),
        )
        self.assertTrue(applied)
        call = self.bot.set_chat_permissions.await_args
        self.assertEqual(call.kwargs["chat_id"], -1301)
        self.assertFalse(call.kwargs["permissions"].can_send_photos)

    async def test_fetches_live_defaults_for_initial_miniapp_form(self) -> None:
        live = ChatPermissions(
            **{
                field: field not in {"can_send_photos", "can_pin_messages"}
                for field in PERMISSION_FIELDS
            }
        )
        bot = SimpleNamespace(
            get_chat=AsyncMock(return_value=SimpleNamespace(permissions=live))
        )
        snapshot = await fetch_telegram_default_permissions(bot, -1401)
        self.assertFalse(snapshot["can_send_photos"])
        self.assertTrue(snapshot["can_send_videos"])
        bot.get_chat.assert_awaited_once_with(chat_id=-1401)

    async def test_scheduler_repairs_legacy_missing_fields_without_stopping(self) -> None:
        legacy = _config()
        legacy["base"].pop("can_edit_tag")
        await self._add_group(-1501, legacy)
        live = ChatPermissions(**_permissions(True))
        self.bot.get_chat = AsyncMock(
            return_value=SimpleNamespace(permissions=live)
        )
        service = GroupPermissionService(
            bot=self.bot,
            session_factory=self.session_factory,
            reconcile_interval_seconds=3600,
        )

        self.assertEqual(
            await service.run_once(at=datetime(2026, 7, 17, 23, 30)),
            1,
        )
        self.assertEqual(
            await service.run_once(at=datetime(2026, 7, 17, 23, 31)),
            0,
        )
        self.bot.get_chat.assert_awaited_once_with(chat_id=-1501)
        self.assertTrue(service.status(-1501)["applied"])


if __name__ == "__main__":
    unittest.main()
