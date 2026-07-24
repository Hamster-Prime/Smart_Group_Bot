"""Proactive topic: when the group has been quiet, the bot opens a topic.

A lightweight standalone background loop (the old generic scheduled-task
engine was removed). Per-group state lives in Group.settings under the
legacy "scheduled_tasks.cooldown_topic" bucket so existing rows keep working.
"""
from __future__ import annotations

import asyncio
import logging
import random
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from aiogram import Bot
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from bot.config import BotConfig, Settings
from bot.services.background_health import record_background_failure
from bot.services.memory import MemoryService
from bot.utils.prompts import get_prompt, with_persona
from bot.utils.runtime_context import build_current_time_context
from bot.utils.security import build_defended_system, clean_text, sanitize_history_for_llm
from bot.utils.telegram import configured_auto_delete_seconds, send_chat_message

log = logging.getLogger(__name__)

_PROACTIVE_PASS_DEADLINE_SECONDS = 300.0

_TASKS_KEY = "scheduled_tasks"
_COOLDOWN_TOPIC_TASK = "cooldown_topic"
_DEFAULT_TASK_BRIEF = "群里已经安静了一阵，请结合群记忆，自然抛出一个大家可能感兴趣、容易接话的话题。"
_MUTE_ALL_REPLIES_KEY = "mute_all_replies"
_ACTIVITY_REVISION_KEY = "activity_revision"
_PROCESSED_ACTIVITY_REVISION_KEY = "processed_activity_revision"
_DELIVERY_CLAIM_KEY = "delivery_claim"
_DELIVERY_CLAIM_STALE_SECONDS = _PROACTIVE_PASS_DEADLINE_SECONDS + 60.0
_SKIP_MARKERS = {"SKIP_TASK", "SKIP_PROACTIVE"}
_LOCAL_ACTIVITY_REVISIONS: dict[int, int] = {}


@dataclass(frozen=True, slots=True)
class _TopicDeliveryResult:
    sent: bool
    memory_text: str = ""
    interrupted: bool = False

    def __bool__(self) -> bool:
        return self.sent


def _now_local() -> datetime:
    return datetime.now().astimezone()


def note_group_activity(group_id: int) -> None:
    """Record inbound activity immediately, before its DB transaction commits."""
    normalized = int(group_id)
    _LOCAL_ACTIVITY_REVISIONS[normalized] = _LOCAL_ACTIVITY_REVISIONS.get(normalized, 0) + 1


def _local_activity_revision(group_id: int) -> int:
    return _LOCAL_ACTIVITY_REVISIONS.get(int(group_id), 0)


def _parse_iso_datetime(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).astimezone()
    except Exception:
        return None


def _isoformat_local(value: datetime) -> str:
    return value.astimezone().replace(microsecond=0).isoformat()


def _is_quiet_hours(now: datetime, config: BotConfig) -> bool:
    start = int(config.proactive_quiet_hours_start or 0) % 24
    end = int(config.proactive_quiet_hours_end or 0) % 24
    hour = now.astimezone().hour
    if start == end:
        return False
    if start < end:
        return start <= hour < end
    return hour >= start or hour < end


def _move_out_of_quiet_hours(value: datetime, config: BotConfig) -> datetime:
    candidate = value.astimezone().replace(microsecond=0)
    if not _is_quiet_hours(candidate, config):
        return candidate

    resume = candidate.replace(hour=int(config.proactive_quiet_hours_end or 0) % 24)
    if resume <= candidate:
        resume += timedelta(days=1)
    return resume


def _task_bucket(group_settings: dict | None) -> dict[str, Any]:
    settings_data = dict(group_settings or {})
    raw_tasks = settings_data.get(_TASKS_KEY)
    tasks = dict(raw_tasks) if isinstance(raw_tasks, dict) else {}
    task_state = dict(tasks.get(_COOLDOWN_TOPIC_TASK) or {})
    if "task_brief" not in task_state or not str(task_state.get("task_brief", "")).strip():
        task_state["task_brief"] = _DEFAULT_TASK_BRIEF
    tasks[_COOLDOWN_TOPIC_TASK] = task_state
    settings_data[_TASKS_KEY] = tasks
    return settings_data


def get_cooldown_task_state(group_settings: dict | None) -> dict[str, Any]:
    settings_data = _task_bucket(group_settings)
    tasks = settings_data.get(_TASKS_KEY) or {}
    return dict(tasks.get(_COOLDOWN_TOPIC_TASK) or {})


def _activity_revision(state: dict[str, Any]) -> int:
    try:
        return max(0, int(state.get(_ACTIVITY_REVISION_KEY, 0) or 0))
    except (TypeError, ValueError):
        return 0


def _bump_activity_revision(state: dict[str, Any]) -> None:
    state[_ACTIVITY_REVISION_KEY] = _activity_revision(state) + 1


def _processed_activity_revision(state: dict[str, Any]) -> int | None:
    raw = state.get(_PROCESSED_ACTIVITY_REVISION_KEY)
    if raw is None:
        return None
    try:
        return max(0, int(raw))
    except (TypeError, ValueError):
        return None


def _delivery_claim_for_revision(
    state: dict[str, Any],
    activity_revision: int,
) -> dict[str, Any] | None:
    raw = state.get(_DELIVERY_CLAIM_KEY)
    if not isinstance(raw, dict):
        return None
    try:
        claimed_revision = max(0, int(raw.get("activity_revision", -1)))
    except (TypeError, ValueError):
        return None
    token = str(raw.get("token") or "").strip()
    if claimed_revision != max(0, int(activity_revision)) or not token:
        return None
    return dict(raw)


def _claim_cooldown_delivery(
    group_settings: dict | None,
    *,
    activity_revision: int,
    default_enabled: bool,
    token: str,
    claimed_at: datetime,
) -> tuple[dict[str, Any], bool]:
    settings_data = _task_bucket(group_settings)
    tasks = dict(settings_data.get(_TASKS_KEY) or {})
    state = dict(tasks.get(_COOLDOWN_TOPIC_TASK) or {})
    raw_enabled = state.get("enabled")
    task_enabled = bool(default_enabled) if raw_enabled is None else bool(raw_enabled)
    if bool(settings_data.get(_MUTE_ALL_REPLIES_KEY, False)) or not task_enabled:
        return settings_data, False

    processed_revision = _processed_activity_revision(state)
    if (
        _activity_revision(state) != activity_revision
        or (
            processed_revision is not None
            and processed_revision >= activity_revision
        )
        or _delivery_claim_for_revision(state, activity_revision) is not None
    ):
        return settings_data, False

    state[_DELIVERY_CLAIM_KEY] = {
        "activity_revision": max(0, int(activity_revision)),
        "token": str(token),
        "claimed_at": _isoformat_local(claimed_at.astimezone()),
    }
    tasks[_COOLDOWN_TOPIC_TASK] = state
    settings_data[_TASKS_KEY] = tasks
    return settings_data, True


def _finalize_cooldown_delivery_claim(
    group_settings: dict | None,
    *,
    token: str,
    last_user_at: datetime,
    activity_revision: int,
    sent_at: datetime,
) -> tuple[dict[str, Any], bool]:
    settings_data = _task_bucket(group_settings)
    tasks = dict(settings_data.get(_TASKS_KEY) or {})
    state = dict(tasks.get(_COOLDOWN_TOPIC_TASK) or {})
    claim = _delivery_claim_for_revision(state, activity_revision)
    if claim is None or str(claim.get("token") or "") != str(token):
        return settings_data, False

    state.pop(_DELIVERY_CLAIM_KEY, None)
    state["last_processed_user_at"] = _isoformat_local(last_user_at.astimezone())
    state[_PROCESSED_ACTIVITY_REVISION_KEY] = max(0, int(activity_revision))
    state["last_sent_at"] = _isoformat_local(sent_at.astimezone())
    tasks[_COOLDOWN_TOPIC_TASK] = state
    settings_data[_TASKS_KEY] = tasks
    return settings_data, True


def is_cooldown_task_enabled(
    group_settings: dict | None,
    *,
    default_enabled: bool,
) -> bool:
    state = get_cooldown_task_state(group_settings)
    raw = state.get("enabled")
    if raw is None:
        return bool(default_enabled)
    return bool(raw)


def compute_next_cooldown_run_at(
    anchor_at: datetime,
    config: BotConfig,
    *,
    rand: random.Random | None = None,
) -> datetime:
    picker = rand or random
    jitter_seconds = picker.randint(0, max(0, int(config.proactive_jitter_minutes or 0) * 60))
    candidate = anchor_at.astimezone() + timedelta(
        minutes=max(180, int(config.proactive_idle_minutes or 180)),
        seconds=jitter_seconds,
    )
    return _move_out_of_quiet_hours(candidate, config)


def record_group_activity(
    group_settings: dict | None,
    config: BotConfig,
    *,
    at: datetime | None = None,
    rand: random.Random | None = None,
) -> dict[str, Any]:
    now = (at or _now_local()).astimezone()
    settings_data = _task_bucket(group_settings)
    tasks = dict(settings_data.get(_TASKS_KEY) or {})
    state = dict(tasks.get(_COOLDOWN_TOPIC_TASK) or {})
    state["task_brief"] = state.get("task_brief") or _DEFAULT_TASK_BRIEF
    _bump_activity_revision(state)
    state["last_user_at"] = _isoformat_local(now)
    state["next_run_at"] = _isoformat_local(compute_next_cooldown_run_at(now, config, rand=rand))
    tasks[_COOLDOWN_TOPIC_TASK] = state
    settings_data[_TASKS_KEY] = tasks
    return settings_data


def set_cooldown_task_enabled(
    group_settings: dict | None,
    *,
    enabled: bool,
    config: BotConfig,
    at: datetime | None = None,
    rand: random.Random | None = None,
) -> dict[str, Any]:
    now = (at or _now_local()).astimezone()
    settings_data = _task_bucket(group_settings)
    tasks = dict(settings_data.get(_TASKS_KEY) or {})
    state = dict(tasks.get(_COOLDOWN_TOPIC_TASK) or {})
    state["enabled"] = bool(enabled)
    state["task_brief"] = state.get("task_brief") or _DEFAULT_TASK_BRIEF
    if enabled:
        _bump_activity_revision(state)
        state["last_user_at"] = _isoformat_local(now)
        state["next_run_at"] = _isoformat_local(compute_next_cooldown_run_at(now, config, rand=rand))
        state.pop("last_processed_user_at", None)
    else:
        state.pop("next_run_at", None)
    tasks[_COOLDOWN_TOPIC_TASK] = state
    settings_data[_TASKS_KEY] = tasks
    return settings_data


def mark_cooldown_task_processed(
    group_settings: dict | None,
    *,
    last_user_at: datetime,
    activity_revision: int | None = None,
    sent_at: datetime | None = None,
) -> dict[str, Any]:
    settings_data = _task_bucket(group_settings)
    tasks = dict(settings_data.get(_TASKS_KEY) or {})
    state = dict(tasks.get(_COOLDOWN_TOPIC_TASK) or {})
    state["last_processed_user_at"] = _isoformat_local(last_user_at.astimezone())
    if activity_revision is not None:
        state[_PROCESSED_ACTIVITY_REVISION_KEY] = max(0, int(activity_revision))
    if sent_at is not None:
        state["last_sent_at"] = _isoformat_local(sent_at.astimezone())
    tasks[_COOLDOWN_TOPIC_TASK] = state
    settings_data[_TASKS_KEY] = tasks
    return settings_data


def postpone_cooldown_task(
    group_settings: dict | None,
    config: BotConfig,
    *,
    at: datetime | None = None,
    expected_activity_revision: int | None = None,
) -> dict[str, Any]:
    now = (at or _now_local()).astimezone()
    settings_data = _task_bucket(group_settings)
    tasks = dict(settings_data.get(_TASKS_KEY) or {})
    state = dict(tasks.get(_COOLDOWN_TOPIC_TASK) or {})
    if (
        expected_activity_revision is not None
        and _activity_revision(state) != expected_activity_revision
    ):
        return settings_data
    retry_at = _move_out_of_quiet_hours(
        now + timedelta(minutes=max(5, int(config.proactive_retry_minutes or 30))),
        config,
    )
    state["next_run_at"] = _isoformat_local(retry_at)
    tasks[_COOLDOWN_TOPIC_TASK] = state
    settings_data[_TASKS_KEY] = tasks
    return settings_data


def get_cooldown_status_text(
    group_settings: dict | None,
    *,
    default_enabled: bool,
    config: BotConfig | None = None,
) -> str:
    state = get_cooldown_task_state(group_settings)
    enabled = is_cooldown_task_enabled(group_settings, default_enabled=default_enabled)
    last_user_at = _parse_iso_datetime(state.get("last_user_at"))
    next_run_at = _parse_iso_datetime(state.get("next_run_at"))
    last_sent_at = _parse_iso_datetime(state.get("last_sent_at"))

    cfg = config or BotConfig()
    quiet_start = int(cfg.proactive_quiet_hours_start or 0) % 24
    quiet_end = int(cfg.proactive_quiet_hours_end or 0) % 24
    quiet_label = (
        f"{quiet_start:02d}:00-{quiet_end:02d}:00" if quiet_start != quiet_end else "无"
    )
    idle_minutes = max(180, int(cfg.proactive_idle_minutes or 180))
    if idle_minutes % 60 == 0:
        idle_label = f"{idle_minutes // 60} 小时"
    else:
        idle_label = f"{idle_minutes} 分钟"
    jitter_minutes = max(0, int(cfg.proactive_jitter_minutes or 0))
    interval_label = (
        f"{idle_label} + 随机抖动（最多 {jitter_minutes} 分钟）" if jitter_minutes else idle_label
    )

    lines = [
        "<b>主动话题</b>",
        f"<b>状态</b>: {'已开启' if enabled else '已关闭'}",
        f"<b>静默期</b>: {quiet_label}",
        f"<b>最短间隔</b>: {interval_label}",
    ]
    if last_user_at:
        lines.append(f"<b>最近活跃</b>: {last_user_at.strftime('%Y-%m-%d %H:%M:%S')}")
    if next_run_at and enabled:
        lines.append(f"<b>最早下次执行</b>: {next_run_at.strftime('%Y-%m-%d %H:%M:%S')}")
    if last_sent_at:
        lines.append(f"<b>最近主动发送</b>: {last_sent_at.strftime('%Y-%m-%d %H:%M:%S')}")
    return "\n".join(lines)


def build_proactive_prompt_payload(
    *,
    task_brief: str,
    history: list[dict[str, str]] | None = None,
) -> dict[str, list[dict[str, str]]]:
    normalized_brief = clean_text(task_brief, max_len=500)
    messages: list[dict[str, str]] = [
        {
            "role": "system",
            "content": build_defended_system(with_persona(get_prompt("proactive_topic"))),
        },
        {"role": "system", "content": build_current_time_context()},
    ]
    if history:
        messages.extend(sanitize_history_for_llm(history, max_items=len(history)))
    messages.append({"role": "user", "content": normalized_brief})
    return {"messages": messages}


class ProactiveTopicService:
    """Background loop: post a topic when the group has gone quiet."""

    def __init__(
        self,
        *,
        settings: Settings,
        bot: Bot,
        memory: MemoryService,
        session_factory: async_sessionmaker[AsyncSession],
        llm: Any,
    ) -> None:
        self.settings = settings
        self.bot = bot
        self.memory = memory
        self.session_factory = session_factory
        self.llm = llm

    async def run_forever(self) -> None:
        consecutive_failures = 0
        while True:
            try:
                async with asyncio.timeout(_PROACTIVE_PASS_DEADLINE_SECONDS):
                    await self.run_once(raise_on_total_failure=True)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.exception("proactive topic loop failed")
                consecutive_failures = record_background_failure(
                    service="proactive topic loop",
                    previous_failures=consecutive_failures,
                    error=exc,
                )
            else:
                consecutive_failures = 0
            interval = max(
                15.0,
                float(self.settings.bot.proactive_check_interval_seconds or 60.0),
            )
            await asyncio.sleep(interval)

    async def run_once(self, *, raise_on_total_failure: bool = False) -> None:
        if _is_quiet_hours(_now_local(), self.settings.bot):
            return
        from bot.services.authz import list_authorized_groups

        async with self.session_factory() as session:
            rows = await list_authorized_groups(session)
            group_ids = [int(row.group_id) for row in rows]
        attempted = 0
        succeeded = 0
        failed = 0
        for group_id in group_ids:
            try:
                did_attempt = await self._run_group_cooldown_task(
                    group_id,
                    now=_now_local(),
                )
                if did_attempt:
                    attempted += 1
                    succeeded += 1
            except asyncio.CancelledError:
                raise
            except Exception:
                attempted += 1
                failed += 1
                log.exception("[%s] proactive topic run failed", group_id)
        if raise_on_total_failure and attempted and failed and succeeded == 0:
            raise RuntimeError(
                f"proactive topic failed for all {failed} attempted groups"
            )

    async def _load_group_settings(self, group_id: int) -> dict[str, Any] | None:
        """Snapshot one group without retaining its DB session during AI work."""
        from bot.db.models import Group

        async with self.session_factory() as session:
            row = await session.get(Group, int(group_id))
            if row is None:
                return None
            return dict(row.settings or {})

    async def _run_group_cooldown_task(
        self, group_id: int, *, now: datetime
    ) -> bool:
        group_settings = await self._load_group_settings(group_id)
        if group_settings is None:
            return False
        if bool(group_settings.get(_MUTE_ALL_REPLIES_KEY, False)):
            return False
        if not is_cooldown_task_enabled(
            group_settings,
            default_enabled=self.settings.bot.proactive_default_enabled,
        ):
            return False

        state = get_cooldown_task_state(group_settings)
        activity_revision = _activity_revision(state)
        last_user_at = _parse_iso_datetime(state.get("last_user_at"))
        next_run_at = _parse_iso_datetime(state.get("next_run_at"))
        last_processed_user_at = _parse_iso_datetime(state.get("last_processed_user_at"))
        processed_activity_revision = _processed_activity_revision(state)
        last_sent_at = _parse_iso_datetime(state.get("last_sent_at"))

        if last_user_at is None or next_run_at is None or now < next_run_at:
            return False
        if processed_activity_revision is not None:
            if processed_activity_revision >= activity_revision:
                return False
        elif last_processed_user_at is not None and last_processed_user_at >= last_user_at:
            # Legacy state did not track revisions; keep the timestamp check
            # until the next processed marker upgrades the stored state.
            return False
        if last_sent_at is not None:
            min_gap = timedelta(minutes=max(180, int(self.settings.bot.proactive_idle_minutes or 180)))
            if now < last_sent_at + min_gap:
                return False

        existing_claim = _delivery_claim_for_revision(state, activity_revision)
        if existing_claim is not None:
            claimed_at = _parse_iso_datetime(existing_claim.get("claimed_at"))
            claim_age = (
                (now - claimed_at).total_seconds()
                if claimed_at is not None
                else _DELIVERY_CLAIM_STALE_SECONDS
            )
            if claim_age < _DELIVERY_CLAIM_STALE_SECONDS:
                log.info(
                    "[%s] proactive topic skipped | reason=delivery_claim_active "
                    "revision=%s age=%.1fs",
                    group_id,
                    activity_revision,
                    max(0.0, claim_age),
                )
                return False

            claim_resolved = False

            def _resolve_stale_claim(fresh: dict[str, Any]) -> dict[str, Any]:
                nonlocal claim_resolved
                updated, claim_resolved = _finalize_cooldown_delivery_claim(
                    fresh,
                    token=str(existing_claim.get("token") or ""),
                    last_user_at=last_user_at,
                    activity_revision=activity_revision,
                    sent_at=claimed_at or now,
                )
                return updated

            committed = await self._commit_fresh_settings(
                group_id,
                _resolve_stale_claim,
            )
            if not committed or not claim_resolved:
                raise RuntimeError("failed to resolve stale proactive delivery claim")
            log.warning(
                "[%s] stale proactive delivery claim finalized without resend | "
                "revision=%s",
                group_id,
                activity_revision,
            )
            return True

        task_brief = clean_text(str(state.get("task_brief") or _DEFAULT_TASK_BRIEF), max_len=240)
        local_activity_revision = _local_activity_revision(group_id)
        history = await self.memory.get_history_for_llm(
            group_id,
            reserve_tokens=768,
            prompt_payload_builder=lambda candidate_history: build_proactive_prompt_payload(
                task_brief=task_brief,
                history=candidate_history,
            ),
        )
        messages = build_proactive_prompt_payload(task_brief=task_brief, history=history)["messages"]
        generated = (await self.llm.chat(messages)).strip()
        reply = clean_text(generated, max_len=240)

        finished_at = _now_local()
        if not reply or reply.upper() in _SKIP_MARKERS:
            committed = await self._commit_fresh_settings(
                group_id,
                lambda fresh: mark_cooldown_task_processed(
                    fresh,
                    last_user_at=last_user_at,
                    activity_revision=activity_revision,
                ),
            )
            if not committed:
                raise RuntimeError("failed to persist proactive skip marker")
            return True

        # The LLM call takes seconds; re-check fresh state before sending so a
        # concurrent /proactive off, /mute all, or new user activity during
        # generation drops this topic instead of posting anyway.
        fresh_settings = await self._load_fresh_settings(group_id)
        if not self._may_send_now(
            fresh_settings,
            snapshot_last_user_at=last_user_at,
            snapshot_activity_revision=activity_revision,
            snapshot_local_activity_revision=local_activity_revision,
            group_id=group_id,
            now=_now_local(),
        ):
            log.info("[%s] proactive topic dropped | reason=state_changed_during_generation", group_id)
            return True

        delivery_token = uuid.uuid4().hex
        claim_applied = False

        def _claim_delivery(fresh: dict[str, Any]) -> dict[str, Any]:
            nonlocal claim_applied
            updated, claim_applied = _claim_cooldown_delivery(
                fresh,
                activity_revision=activity_revision,
                default_enabled=self.settings.bot.proactive_default_enabled,
                token=delivery_token,
                claimed_at=_now_local(),
            )
            return updated

        claim_committed = await self._commit_fresh_settings(
            group_id,
            _claim_delivery,
        )
        if not claim_committed:
            raise RuntimeError("failed to persist proactive delivery claim")
        if not claim_applied:
            log.info(
                "[%s] proactive topic dropped | reason=delivery_claim_lost "
                "revision=%s",
                group_id,
                activity_revision,
            )
            return True

        # The claim was atomic with the latest persisted settings, but activity
        # or admin state may change immediately afterward. Refresh once more so
        # delivery never continues with the pre-claim snapshot.
        fresh_settings = await self._load_fresh_settings(group_id)
        if not self._may_send_now(
            fresh_settings,
            snapshot_last_user_at=last_user_at,
            snapshot_activity_revision=activity_revision,
            snapshot_local_activity_revision=local_activity_revision,
            group_id=group_id,
            now=_now_local(),
        ):
            log.info(
                "[%s] proactive topic dropped | reason=state_changed_after_delivery_claim",
                group_id,
            )
            return True

        raw_delivery = await self._deliver_topic(group_id, reply, fresh_settings)
        delivery = (
            raw_delivery
            if isinstance(raw_delivery, _TopicDeliveryResult)
            else _TopicDeliveryResult(bool(raw_delivery), reply if raw_delivery else "")
        )
        if not delivery:
            # Keep the durable claim. A transport failure can be ambiguous, so
            # another pass must not send the same topic again. Once the claim is
            # stale it is finalized as processed without a resend.
            raise RuntimeError("proactive topic delivery failed")

        delivery_finalized = False

        def _finalize_delivery(fresh: dict[str, Any]) -> dict[str, Any]:
            nonlocal delivery_finalized
            updated, delivery_finalized = _finalize_cooldown_delivery_claim(
                fresh,
                token=delivery_token,
                last_user_at=last_user_at,
                activity_revision=activity_revision,
                sent_at=finished_at,
            )
            return updated

        committed = await self._commit_fresh_settings(
            group_id,
            _finalize_delivery,
        )
        if not committed or not delivery_finalized:
            # The durable pre-send claim remains in place when persistence is
            # unavailable, preventing the next pass or restart from resending.
            raise RuntimeError("failed to persist proactive delivery outcome")

        if delivery.interrupted:
            log.warning(
                "[%s] proactive delivery was cancelled after Telegram acceptance; "
                "processed marker persisted",
                group_id,
            )
            raise asyncio.CancelledError

        if delivery.memory_text:
            await self.memory.add_message(
                group_id,
                "assistant",
                delivery.memory_text,
                message_type="assistant_proactive",
            )
            await self.memory.compact_if_needed(group_id)
        log.info("[%s] proactive topic sent | %s", group_id, reply[:80])
        return True

    async def _load_fresh_settings(self, group_id: int) -> dict[str, Any]:
        from bot.db.models import Group

        # Use a new transaction here so the post-generation decision can never
        # reuse the pre-generation settings snapshot.
        async with self.session_factory() as fresh_session:
            row = await fresh_session.get(Group, group_id)
            return dict(row.settings or {}) if row is not None else {}

    def _may_send_now(
        self,
        fresh_settings: dict[str, Any],
        *,
        snapshot_last_user_at: datetime,
        snapshot_activity_revision: int,
        snapshot_local_activity_revision: int,
        group_id: int,
        now: datetime,
    ) -> bool:
        if _local_activity_revision(group_id) != snapshot_local_activity_revision:
            return False
        if bool(fresh_settings.get(_MUTE_ALL_REPLIES_KEY, False)):
            return False
        if not is_cooldown_task_enabled(
            fresh_settings,
            default_enabled=self.settings.bot.proactive_default_enabled,
        ):
            return False
        if _is_quiet_hours(now, self.settings.bot):
            return False
        fresh_state = get_cooldown_task_state(fresh_settings)
        if _activity_revision(fresh_state) != snapshot_activity_revision:
            return False
        fresh_last_user_at = _parse_iso_datetime(fresh_state.get("last_user_at"))
        # Someone spoke while we were generating: the group is no longer quiet.
        if fresh_last_user_at is not None and fresh_last_user_at > snapshot_last_user_at:
            return False
        return True

    async def _deliver_topic(
        self,
        group_id: int,
        reply: str,
        fresh_settings: dict[str, Any],
    ) -> _TopicDeliveryResult:
        """Send the topic, honoring the group's TTS mode like normal replies."""
        from bot.services.doubao_tts import (
            DoubaoTTSService,
            TTSDeliveryResult,
            is_tts_always_enabled,
            normalize_tts_mode,
            tts_sender_accepts_delivery_callback,
        )

        delivery_confirmed = False

        def _confirm_delivery() -> None:
            nonlocal delivery_confirmed
            delivery_confirmed = True

        tts_mode = normalize_tts_mode(fresh_settings)
        if is_tts_always_enabled(tts_mode):
            tts_service = DoubaoTTSService(self.settings)
            if not tts_service.available:
                log.warning("[%s] proactive topic always-tts unavailable", group_id)
                return _TopicDeliveryResult(False)
            detailed_sender = getattr(tts_service, "send_chat_tts_result", None)
            try:
                if callable(detailed_sender):
                    delivery = await detailed_sender(
                        self.bot,
                        group_id,
                        reply,
                        auto_delete_seconds=configured_auto_delete_seconds(
                            self.settings, "media"
                        ),
                        uid=str(group_id),
                        on_delivery=_confirm_delivery,
                    )
                else:
                    legacy_sender = tts_service.send_chat_tts
                    legacy_kwargs = {
                        "auto_delete_seconds": configured_auto_delete_seconds(
                            self.settings,
                            "media",
                        ),
                        "uid": str(group_id),
                    }
                    if tts_sender_accepts_delivery_callback(legacy_sender):
                        complete = await legacy_sender(
                            self.bot,
                            group_id,
                            reply,
                            **legacy_kwargs,
                            on_delivery=_confirm_delivery,
                        )
                    else:
                        complete = await legacy_sender(
                            self.bot,
                            group_id,
                            reply,
                            **legacy_kwargs,
                        )
                    delivery = TTSDeliveryResult(
                        requested_segments=(reply,),
                        sent_segment_count=1 if complete else 0,
                        error="" if complete else "legacy_send_failed",
                    )
            except asyncio.CancelledError:
                if delivery_confirmed:
                    return _TopicDeliveryResult(True, interrupted=True)
                raise
            except Exception:
                if delivery_confirmed:
                    log.exception(
                        "[%s] proactive TTS failed after confirmed delivery",
                        group_id,
                    )
                    return _TopicDeliveryResult(True)
                raise
            if delivery.complete:
                return _TopicDeliveryResult(True, reply)
            if delivery.any_sent:
                remaining_sent = False
                if delivery.remaining_text:
                    try:
                        remaining_sent = await send_chat_message(
                            self.bot,
                            group_id,
                            delivery.remaining_text,
                            auto_delete_seconds=configured_auto_delete_seconds(
                                self.settings,
                                "proactive",
                            ),
                            on_delivery=_confirm_delivery,
                        )
                    except asyncio.CancelledError:
                        return _TopicDeliveryResult(
                            True,
                            delivery.delivered_text,
                            interrupted=True,
                        )
                log.warning(
                    "[%s] proactive topic partially delivered by TTS | "
                    "sent=%d total=%d text_fallback=%s",
                    group_id,
                    delivery.sent_segment_count,
                    len(delivery.requested_segments),
                    remaining_sent,
                )
                # Some content is already externally visible. Retrying the full
                # topic would duplicate the delivered prefix.
                return _TopicDeliveryResult(
                    True,
                    reply if remaining_sent else delivery.delivered_text,
                )
            if delivery_confirmed:
                return _TopicDeliveryResult(True)
            if not delivery.complete:
                # In always mode a TTS failure suppresses the text fallback;
                # the postpone path retries later.
                log.warning("[%s] proactive topic always-tts send failed", group_id)
            return _TopicDeliveryResult(False)
        try:
            complete = await send_chat_message(
                self.bot,
                group_id,
                reply,
                auto_delete_seconds=configured_auto_delete_seconds(
                    self.settings, "proactive"
                ),
                on_delivery=_confirm_delivery,
            )
        except asyncio.CancelledError:
            if delivery_confirmed:
                return _TopicDeliveryResult(True, interrupted=True)
            raise
        except Exception:
            if delivery_confirmed:
                log.exception(
                    "[%s] proactive text failed after confirmed delivery",
                    group_id,
                )
                return _TopicDeliveryResult(True)
            raise
        return _TopicDeliveryResult(
            bool(complete or delivery_confirmed),
            reply if complete else "",
        )

    async def _commit_fresh_settings(
        self,
        group_id: int,
        mutate: Any,
    ) -> bool:
        """Apply a settings mutation without losing concurrent JSON updates.

        Each attempt uses a new transaction and compares the full JSON value
        read with the value still stored by the conditional UPDATE. If another
        handler wins the race, retry from its committed settings instead of
        replacing them with a stale snapshot.
        """
        from bot.db.models import Group

        for _attempt in range(5):
            async with self.session_factory() as fresh_session:
                row = await fresh_session.get(Group, group_id)
                if row is None:
                    return False
                stored_settings = row.settings
                updated_settings = mutate(dict(stored_settings or {}))
                stmt = (
                    update(Group)
                    .where(
                        Group.id == group_id,
                        Group.settings == stored_settings,
                    )
                    .values(settings=updated_settings)
                    .execution_options(synchronize_session=False)
                )
                result = await fresh_session.execute(stmt)
                if int(result.rowcount or 0) == 1:
                    await fresh_session.commit()
                    return True
                await fresh_session.rollback()

        log.warning("[%s] proactive settings update lost repeated concurrent races", group_id)
        return False
