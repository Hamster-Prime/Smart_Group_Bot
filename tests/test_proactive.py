from __future__ import annotations

import asyncio
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from bot.config import BotConfig, Settings
from bot.handlers import admin
from bot.services import proactive as proactive_module
from bot.services.proactive import (
    ProactiveTopicService,
    get_cooldown_status_text,
    get_cooldown_task_state,
    mark_cooldown_task_processed,
    note_group_activity,
    postpone_cooldown_task,
    record_group_activity,
)


class _FreshSession:
    def __init__(self, settings: dict, *, rowcount: int = 1) -> None:
        self.get = AsyncMock(return_value=SimpleNamespace(settings=settings))
        self.execute = AsyncMock(return_value=SimpleNamespace(rowcount=rowcount))
        self.commit = AsyncMock()
        self.rollback = AsyncMock()

    async def __aenter__(self) -> _FreshSession:
        return self

    async def __aexit__(self, *_args: object) -> None:
        return None


class _SessionSequence:
    def __init__(self, *sessions: _FreshSession) -> None:
        self.sessions = list(sessions)

    def __call__(self) -> _FreshSession:
        return self.sessions.pop(0)


def _settings() -> Settings:
    return Settings(_env_file=None)


def _due_settings(now: datetime) -> dict:
    return {
        "scheduled_tasks": {
            "cooldown_topic": {
                "enabled": True,
                "activity_revision": 1,
                "last_user_at": (now - timedelta(hours=4)).isoformat(),
                "next_run_at": (now - timedelta(minutes=1)).isoformat(),
                "task_brief": "开个话题",
            }
        }
    }


class ProactiveStateTests(unittest.TestCase):
    def test_delivery_claim_is_single_owner_and_finalizes_processed_state(self) -> None:
        now = datetime(2026, 7, 11, 12, 0, 0, tzinfo=timezone.utc)
        settings = _due_settings(now)

        claimed, first_applied = proactive_module._claim_cooldown_delivery(
            settings,
            activity_revision=1,
            default_enabled=True,
            token="owner-1",
            claimed_at=now,
        )
        claimed_again, second_applied = proactive_module._claim_cooldown_delivery(
            claimed,
            activity_revision=1,
            default_enabled=True,
            token="owner-2",
            claimed_at=now,
        )
        finalized, did_finalize = (
            proactive_module._finalize_cooldown_delivery_claim(
                claimed_again,
                token="owner-1",
                last_user_at=now - timedelta(hours=4),
                activity_revision=1,
                sent_at=now,
            )
        )

        self.assertTrue(first_applied)
        self.assertFalse(second_applied)
        self.assertTrue(did_finalize)
        state = get_cooldown_task_state(finalized)
        self.assertNotIn("delivery_claim", state)
        self.assertEqual(state["processed_activity_revision"], 1)

    def test_delivery_claim_respects_latest_admin_state(self) -> None:
        now = datetime(2026, 7, 11, 12, 0, 0, tzinfo=timezone.utc)
        muted = {**_due_settings(now), "mute_all_replies": True}
        disabled = _due_settings(now)
        disabled["scheduled_tasks"]["cooldown_topic"]["enabled"] = False

        for settings in (muted, disabled):
            with self.subTest(settings=settings):
                updated, applied = proactive_module._claim_cooldown_delivery(
                    settings,
                    activity_revision=1,
                    default_enabled=True,
                    token="owner",
                    claimed_at=now,
                )

                self.assertFalse(applied)
                self.assertNotIn(
                    "delivery_claim",
                    get_cooldown_task_state(updated),
                )

    def test_status_uses_effective_runtime_config(self) -> None:
        config = BotConfig(
            proactive_idle_minutes=240,
            proactive_jitter_minutes=15,
            proactive_quiet_hours_start=22,
            proactive_quiet_hours_end=7,
        )

        text = get_cooldown_status_text({}, default_enabled=True, config=config)

        self.assertIn("22:00-07:00", text)
        self.assertIn("4 小时 + 随机抖动（最多 15 分钟）", text)
        self.assertNotIn("00:00-09:00", text)

    def test_activity_revision_changes_even_with_same_second_timestamp(self) -> None:
        config = BotConfig()
        at = datetime(2026, 7, 11, 12, 0, 0, tzinfo=timezone.utc)

        first = record_group_activity({}, config, at=at)
        second = record_group_activity(first, config, at=at)

        first_state = get_cooldown_task_state(first)
        second_state = get_cooldown_task_state(second)
        self.assertEqual(first_state["last_user_at"], second_state["last_user_at"])
        self.assertEqual(first_state["activity_revision"], 1)
        self.assertEqual(second_state["activity_revision"], 2)

    def test_failed_send_does_not_replace_new_activity_schedule(self) -> None:
        config = BotConfig(proactive_retry_minutes=30)
        at = datetime(2026, 7, 11, 12, 0, 0, tzinfo=timezone.utc)
        before_generation = record_group_activity({}, config, at=at)
        after_new_message = record_group_activity(before_generation, config, at=at)

        postponed = postpone_cooldown_task(
            after_new_message,
            config,
            at=at,
            expected_activity_revision=1,
        )

        self.assertEqual(
            get_cooldown_task_state(postponed)["next_run_at"],
            get_cooldown_task_state(after_new_message)["next_run_at"],
        )

    def test_processed_revision_does_not_consume_same_second_new_activity(self) -> None:
        config = BotConfig()
        at = datetime(2026, 7, 11, 12, 0, 0, tzinfo=timezone.utc)
        first = record_group_activity({}, config, at=at)
        second = record_group_activity(first, config, at=at)

        marked = mark_cooldown_task_processed(
            second,
            last_user_at=at,
            activity_revision=1,
        )
        state = get_cooldown_task_state(marked)

        self.assertEqual(state["activity_revision"], 2)
        self.assertEqual(state["processed_activity_revision"], 1)


class ProactiveDeliveryTests(unittest.IsolatedAsyncioTestCase):
    def _service(
        self,
        *,
        llm: object | None = None,
        fresh_settings: dict | None = None,
    ) -> ProactiveTopicService:
        return ProactiveTopicService(
            settings=_settings(),
            bot=SimpleNamespace(),
            memory=SimpleNamespace(
                get_history_for_llm=AsyncMock(return_value=[]),
                add_message=AsyncMock(),
                compact_if_needed=AsyncMock(),
            ),
            session_factory=lambda: _FreshSession(fresh_settings or {}),
            llm=llm or SimpleNamespace(chat=AsyncMock(return_value="新话题")),
        )

    async def test_group_session_is_released_before_history_and_llm(self) -> None:
        now = datetime(2026, 7, 11, 12, 0, 0, tzinfo=timezone.utc).astimezone()
        initial_settings = _due_settings(now)
        exited = False

        class SnapshotSession:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args: object) -> None:
                nonlocal exited
                exited = True

            async def get(self, _model: object, _group_id: int) -> object:
                return SimpleNamespace(settings=initial_settings)

        async def history_after_close(*_args: object, **_kwargs: object) -> list:
            self.assertTrue(exited)
            return []

        async def llm_after_close(_messages: object) -> str:
            self.assertTrue(exited)
            return ""

        service = self._service(
            llm=SimpleNamespace(chat=AsyncMock(side_effect=llm_after_close))
        )
        service.session_factory = SnapshotSession
        service.memory.get_history_for_llm = AsyncMock(side_effect=history_after_close)

        with patch.object(
            service,
            "_commit_fresh_settings",
            new=AsyncMock(return_value=True),
        ):
            await service._run_group_cooldown_task(-100123, now=now)

        self.assertTrue(exited)

    async def test_disabled_group_does_not_mask_another_group_failure(self) -> None:
        service = self._service()
        service._run_group_cooldown_task = AsyncMock(
            side_effect=[False, RuntimeError("broken group")]
        )

        with (
            patch(
                "bot.services.authz.list_authorized_groups",
                new=AsyncMock(
                    return_value=[
                        SimpleNamespace(group_id=-1001),
                        SimpleNamespace(group_id=-1002),
                    ]
                ),
            ),
            patch("bot.services.proactive._is_quiet_hours", return_value=False),
        ):
            with self.assertRaisesRegex(RuntimeError, "failed for all"):
                await service.run_once(raise_on_total_failure=True)

    async def test_delivery_failure_remains_due_and_is_surfaced(self) -> None:
        now = datetime(2026, 7, 11, 12, 0, 0, tzinfo=timezone.utc).astimezone()
        initial_settings = _due_settings(now)
        service = self._service(fresh_settings=initial_settings)
        persisted_settings = initial_settings

        async def commit_settings(_group_id: int, mutate: object) -> bool:
            nonlocal persisted_settings
            persisted_settings = mutate(persisted_settings)
            return True

        commit = AsyncMock(side_effect=commit_settings)

        with (
            patch.object(
                service,
                "_load_group_settings",
                new=AsyncMock(return_value=initial_settings),
            ),
            patch.object(
                service,
                "_deliver_topic",
                new=AsyncMock(return_value=False),
            ),
            patch.object(
                service,
                "_commit_fresh_settings",
                new=commit,
            ),
            patch("bot.services.proactive._now_local", return_value=now),
        ):
            with self.assertRaisesRegex(RuntimeError, "delivery failed"):
                await service._run_group_cooldown_task(-100123, now=now)

        commit.assert_awaited_once()
        claim = get_cooldown_task_state(persisted_settings)["delivery_claim"]
        self.assertEqual(claim["activity_revision"], 1)

    async def test_active_delivery_claim_prevents_restart_resend(self) -> None:
        now = datetime(2026, 7, 11, 12, 0, 0, tzinfo=timezone.utc).astimezone()
        claimed_settings, applied = proactive_module._claim_cooldown_delivery(
            _due_settings(now),
            activity_revision=1,
            default_enabled=True,
            token="prior-process",
            claimed_at=now,
        )
        self.assertTrue(applied)
        service = self._service(fresh_settings=claimed_settings)
        deliver = AsyncMock(return_value=True)

        with (
            patch.object(
                service,
                "_load_group_settings",
                new=AsyncMock(return_value=claimed_settings),
            ),
            patch.object(service, "_deliver_topic", new=deliver),
        ):
            attempted = await service._run_group_cooldown_task(-100123, now=now)

        self.assertFalse(attempted)
        deliver.assert_not_awaited()

    async def test_sent_topic_requires_persisted_outcome(self) -> None:
        now = datetime(2026, 7, 11, 12, 0, 0, tzinfo=timezone.utc).astimezone()
        initial_settings = _due_settings(now)
        service = self._service(fresh_settings=initial_settings)
        persisted_settings = initial_settings
        commit_count = 0

        async def fail_outcome_commit(_group_id: int, mutate: object) -> bool:
            nonlocal commit_count, persisted_settings
            commit_count += 1
            if commit_count == 1:
                persisted_settings = mutate(persisted_settings)
                return True
            return False

        with (
            patch.object(
                service,
                "_load_group_settings",
                new=AsyncMock(return_value=initial_settings),
            ),
            patch.object(
                service,
                "_deliver_topic",
                new=AsyncMock(return_value=True),
            ),
            patch.object(
                service,
                "_commit_fresh_settings",
                new=AsyncMock(side_effect=fail_outcome_commit),
            ),
            patch("bot.services.proactive._now_local", return_value=now),
        ):
            with self.assertRaisesRegex(RuntimeError, "persist"):
                await service._run_group_cooldown_task(-100123, now=now)

        service.memory.add_message.assert_not_awaited()
        self.assertIn("delivery_claim", get_cooldown_task_state(persisted_settings))

    async def test_cancelled_after_delivery_persists_marker_before_propagating(self) -> None:
        now = datetime(2026, 7, 11, 12, 0, 0, tzinfo=timezone.utc).astimezone()
        initial_settings = _due_settings(now)
        service = self._service(fresh_settings=initial_settings)
        persisted_settings = initial_settings

        async def commit_settings(_group_id: int, mutate: object) -> bool:
            nonlocal persisted_settings
            persisted_settings = mutate(persisted_settings)
            return True

        commit = AsyncMock(side_effect=commit_settings)

        with (
            patch.object(
                service,
                "_load_group_settings",
                new=AsyncMock(return_value=initial_settings),
            ),
            patch.object(
                service,
                "_deliver_topic",
                new=AsyncMock(
                    return_value=proactive_module._TopicDeliveryResult(
                        True,
                        interrupted=True,
                    )
                ),
            ),
            patch.object(service, "_commit_fresh_settings", new=commit),
            patch("bot.services.proactive._now_local", return_value=now),
        ):
            with self.assertRaises(asyncio.CancelledError):
                await service._run_group_cooldown_task(-100123, now=now)

        self.assertEqual(commit.await_count, 2)
        state = get_cooldown_task_state(persisted_settings)
        self.assertNotIn("delivery_claim", state)
        self.assertEqual(state["processed_activity_revision"], 1)
        service.memory.add_message.assert_not_awaited()

    async def test_new_activity_during_generation_suppresses_topic(self) -> None:
        now = datetime(2026, 7, 11, 12, 0, 0, tzinfo=timezone.utc).astimezone()
        initial_settings = _due_settings(now)
        fresh_settings = record_group_activity(
            initial_settings,
            BotConfig(),
            at=now - timedelta(hours=4),
        )
        service = self._service(fresh_settings=fresh_settings)

        with (
            patch.object(
                service,
                "_load_group_settings",
                new=AsyncMock(return_value=initial_settings),
            ),
            patch.object(service, "_deliver_topic", new=AsyncMock()) as deliver,
        ):
            await service._run_group_cooldown_task(-100123, now=now)

        deliver.assert_not_awaited()

    async def test_uncommitted_local_activity_during_generation_suppresses_topic(self) -> None:
        now = datetime(2026, 7, 11, 12, 0, 0, tzinfo=timezone.utc).astimezone()
        initial_settings = _due_settings(now)
        async def generate_after_message(_messages: object) -> str:
            note_group_activity(-100123)
            return "新话题"

        service = self._service(
            llm=SimpleNamespace(chat=AsyncMock(side_effect=generate_after_message)),
            fresh_settings=initial_settings,
        )

        with (
            patch.object(
                service,
                "_load_group_settings",
                new=AsyncMock(return_value=initial_settings),
            ),
            patch.object(service, "_deliver_topic", new=AsyncMock()) as deliver,
        ):
            await service._run_group_cooldown_task(-100123, now=now)

        deliver.assert_not_awaited()

    async def test_admin_state_change_during_generation_suppresses_topic(self) -> None:
        now = datetime(2026, 7, 11, 12, 0, 0, tzinfo=timezone.utc).astimezone()
        initial_settings = _due_settings(now)

        for fresh_settings in (
            {**initial_settings, "mute_all_replies": True},
            {
                "scheduled_tasks": {
                    "cooldown_topic": {
                        **get_cooldown_task_state(initial_settings),
                        "enabled": False,
                    }
                }
            },
        ):
            with self.subTest(fresh_settings=fresh_settings):
                service = self._service(fresh_settings=fresh_settings)
                with (
                    patch.object(
                        service,
                        "_load_group_settings",
                        new=AsyncMock(return_value=initial_settings),
                    ),
                    patch.object(service, "_deliver_topic", new=AsyncMock()) as deliver,
                ):
                    await service._run_group_cooldown_task(-100123, now=now)
                deliver.assert_not_awaited()

    async def test_admin_state_change_after_claim_suppresses_topic(self) -> None:
        now = datetime(2026, 7, 11, 12, 0, 0, tzinfo=timezone.utc).astimezone()

        for mode in ("muted", "disabled"):
            with self.subTest(mode=mode):
                initial_settings = _due_settings(now)
                persisted_settings = initial_settings
                fresh_read_count = 0
                service = self._service(fresh_settings=initial_settings)

                def apply_admin_change(settings: dict) -> dict:
                    updated = dict(settings)
                    if mode == "muted":
                        updated["mute_all_replies"] = True
                        return updated
                    tasks = dict(updated.get("scheduled_tasks") or {})
                    state = dict(tasks.get("cooldown_topic") or {})
                    state["enabled"] = False
                    tasks["cooldown_topic"] = state
                    updated["scheduled_tasks"] = tasks
                    return updated

                async def load_fresh(_group_id: int) -> dict:
                    nonlocal fresh_read_count, persisted_settings
                    fresh_read_count += 1
                    if fresh_read_count == 2:
                        persisted_settings = apply_admin_change(persisted_settings)
                    return persisted_settings

                async def commit_settings(_group_id: int, mutate: object) -> bool:
                    nonlocal persisted_settings
                    persisted_settings = mutate(persisted_settings)
                    return True

                with (
                    patch.object(
                        service,
                        "_load_group_settings",
                        new=AsyncMock(return_value=initial_settings),
                    ),
                    patch.object(
                        service,
                        "_load_fresh_settings",
                        new=AsyncMock(side_effect=load_fresh),
                    ),
                    patch.object(service, "_deliver_topic", new=AsyncMock()) as deliver,
                    patch.object(
                        service,
                        "_commit_fresh_settings",
                        new=AsyncMock(side_effect=commit_settings),
                    ),
                    patch("bot.services.proactive._now_local", return_value=now),
                ):
                    attempted = await service._run_group_cooldown_task(
                        -100123,
                        now=now,
                    )

                self.assertTrue(attempted)
                self.assertEqual(fresh_read_count, 2)
                self.assertIn(
                    "delivery_claim",
                    get_cooldown_task_state(persisted_settings),
                )
                deliver.assert_not_awaited()

    async def test_older_processed_revision_does_not_block_same_second_activity(self) -> None:
        now = datetime(2026, 7, 11, 12, 0, 0, tzinfo=timezone.utc).astimezone()
        group_settings = _due_settings(now)
        state = group_settings["scheduled_tasks"]["cooldown_topic"]
        state["activity_revision"] = 2
        state["processed_activity_revision"] = 1
        state["last_processed_user_at"] = state["last_user_at"]
        service = self._service(fresh_settings=group_settings)
        persisted_settings = group_settings

        async def commit_settings(_group_id: int, mutate: object) -> bool:
            nonlocal persisted_settings
            persisted_settings = mutate(persisted_settings)
            return True

        with (
            patch.object(
                service,
                "_load_group_settings",
                new=AsyncMock(return_value=group_settings),
            ),
            patch.object(
                service,
                "_deliver_topic",
                new=AsyncMock(return_value=True),
            ) as deliver,
            patch.object(
                service,
                "_commit_fresh_settings",
                new=AsyncMock(side_effect=commit_settings),
            ),
            # _may_send_now re-checks quiet hours against the real clock;
            # pin it to the test's `now` so runs between 0-9 点 don't flake.
            patch("bot.services.proactive._now_local", return_value=now),
        ):
            await service._run_group_cooldown_task(-100123, now=now)

        deliver.assert_awaited_once()

    async def test_settings_commit_retries_from_concurrent_json_value(self) -> None:
        first = _FreshSession({"mute_all_replies": False}, rowcount=0)
        second = _FreshSession({"mute_all_replies": True}, rowcount=1)
        service = self._service()
        service.session_factory = _SessionSequence(first, second)
        seen: list[dict] = []

        def mutate(fresh: dict) -> dict:
            seen.append(dict(fresh))
            fresh["proactive_marker"] = "done"
            return fresh

        updated = await service._commit_fresh_settings(-100123, mutate)

        self.assertTrue(updated)
        self.assertEqual(
            seen,
            [
                {"mute_all_replies": False},
                {"mute_all_replies": True},
            ],
        )
        first.rollback.assert_awaited_once()
        second.commit.assert_awaited_once()

    async def test_always_mode_delivers_voice_without_text_fallback(self) -> None:
        service = self._service()
        fake_tts = SimpleNamespace(
            available=True,
            send_chat_tts=AsyncMock(return_value=True),
        )

        with (
            patch("bot.services.doubao_tts.DoubaoTTSService", return_value=fake_tts),
            patch("bot.services.proactive.send_chat_message", new=AsyncMock()) as send_text,
        ):
            sent = await service._deliver_topic(-100123, "新话题", {"tts_mode": "always"})

        self.assertTrue(sent)
        fake_tts.send_chat_tts.assert_awaited_once()
        send_text.assert_not_awaited()

    async def test_legacy_tts_sender_without_delivery_callback_is_supported(self) -> None:
        service = self._service()

        class LegacyTTS:
            available = True

            async def send_chat_tts(
                self,
                bot: object,
                group_id: int,
                reply: str,
                *,
                auto_delete_seconds: int,
                uid: str,
            ) -> bool:
                del bot, group_id, reply, auto_delete_seconds, uid
                return True

        with patch(
            "bot.services.doubao_tts.DoubaoTTSService",
            return_value=LegacyTTS(),
        ):
            result = await service._deliver_topic(
                -100123,
                "新话题",
                {"tts_mode": "always"},
            )

        self.assertTrue(result)
        self.assertEqual(result.memory_text, "新话题")

    async def test_text_delivery_receipt_survives_cancellation(self) -> None:
        service = self._service()

        async def delivered_then_cancelled(
            _bot: object,
            _group_id: int,
            _reply: str,
            **kwargs: object,
        ) -> bool:
            callback = kwargs["on_delivery"]
            self.assertTrue(callable(callback))
            callback()
            raise asyncio.CancelledError

        with patch(
            "bot.services.proactive.send_chat_message",
            new=delivered_then_cancelled,
        ):
            result = await service._deliver_topic(-100123, "新话题", {})

        self.assertTrue(result)
        self.assertTrue(result.interrupted)
        self.assertEqual(result.memory_text, "")

    async def test_partial_always_tts_sends_only_remaining_text(self) -> None:
        from bot.services.doubao_tts import TTSDeliveryResult

        service = self._service()
        fake_tts = SimpleNamespace(
            available=True,
            send_chat_tts_result=AsyncMock(
                return_value=TTSDeliveryResult(
                    requested_segments=("第一段。", "第二段。"),
                    sent_segment_count=1,
                    error="synthesis_failed",
                )
            ),
        )

        with (
            patch("bot.services.doubao_tts.DoubaoTTSService", return_value=fake_tts),
            patch(
                "bot.services.proactive.send_chat_message",
                new=AsyncMock(return_value=True),
            ) as send_text,
        ):
            sent = await service._deliver_topic(
                -100123,
                "第一段。第二段。",
                {"tts_mode": "always"},
            )

        self.assertTrue(sent)
        self.assertEqual(send_text.await_args.args[2], "第二段。")
        self.assertEqual(sent.memory_text, "第一段。第二段。")

    async def test_partial_always_tts_records_only_delivered_prefix_when_fallback_fails(
        self,
    ) -> None:
        from bot.services.doubao_tts import TTSDeliveryResult

        service = self._service()
        fake_tts = SimpleNamespace(
            available=True,
            send_chat_tts_result=AsyncMock(
                return_value=TTSDeliveryResult(
                    requested_segments=("第一段。", "第二段。"),
                    sent_segment_count=1,
                    error="synthesis_failed",
                )
            ),
        )

        with (
            patch("bot.services.doubao_tts.DoubaoTTSService", return_value=fake_tts),
            patch(
                "bot.services.proactive.send_chat_message",
                new=AsyncMock(return_value=False),
            ),
        ):
            sent = await service._deliver_topic(
                -100123,
                "第一段。第二段。",
                {"tts_mode": "always"},
            )

        self.assertTrue(sent)
        self.assertEqual(sent.memory_text, "第一段。")

    async def test_always_mode_does_not_fall_back_to_text_when_tts_unavailable(self) -> None:
        service = self._service()
        fake_tts = SimpleNamespace(available=False, send_chat_tts=AsyncMock())

        with (
            patch("bot.services.doubao_tts.DoubaoTTSService", return_value=fake_tts),
            patch("bot.services.proactive.send_chat_message", new=AsyncMock()) as send_text,
        ):
            sent = await service._deliver_topic(-100123, "新话题", {"tts_mode": "always"})

        self.assertFalse(sent)
        fake_tts.send_chat_tts.assert_not_awaited()
        send_text.assert_not_awaited()


class ProactiveAdminCommandTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def _message(text: str) -> SimpleNamespace:
        return SimpleNamespace(
            text=text,
            chat=SimpleNamespace(id=-100123, type="supergroup", title="group"),
        )

    async def _assert_commit_precedes_answer(self, command: object, text: str) -> None:
        events: list[str] = []
        session = SimpleNamespace(
            commit=AsyncMock(side_effect=lambda: events.append("commit")),
        )
        group_row = SimpleNamespace(settings=_due_settings(datetime.now().astimezone()))

        async def record_answer(*_args: object, **_kwargs: object) -> None:
            events.append("answer")

        with (
            patch("bot.handlers.admin.ensure_group_authorized", new=AsyncMock(return_value=True)),
            patch(
                "bot.handlers.admin.ensure_group_admin_permission",
                new=AsyncMock(return_value=True),
            ),
            patch("bot.handlers.admin._ensure_group_row", new=AsyncMock(return_value=group_row)),
            patch("bot.handlers.admin._answer", new=AsyncMock(side_effect=record_answer)),
        ):
            await command(self._message(text), session, _settings())

        self.assertEqual(events, ["commit", "answer"])

    async def test_proactive_off_is_committed_before_confirmation(self) -> None:
        await self._assert_commit_precedes_answer(admin.cmd_proactive, "/proactive off")

    async def test_mute_all_is_committed_before_confirmation(self) -> None:
        await self._assert_commit_precedes_answer(admin.cmd_mute, "/mute all")
