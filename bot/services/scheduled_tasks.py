from __future__ import annotations

import asyncio
import html
import logging
import random
import re
from datetime import datetime, timedelta, timezone
from difflib import SequenceMatcher
from typing import Any

from aiogram import Bot
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from bot.config import BotConfig, Settings
from bot.db.models import AuthorizedGroup, Group, ScheduledTaskRecord
from bot.services.doubao_tts import DoubaoTTSService, is_tts_always_enabled, load_group_tts_mode
from bot.services.memory import MemoryService
from bot.services.skills import SkillService
from bot.utils.security import clean_multiline_text, clean_text
from bot.utils.telegram import send_chat_message

log = logging.getLogger(__name__)

_TASKS_KEY = "scheduled_tasks"
_COOLDOWN_TOPIC_TASK = "cooldown_topic"
_DEFAULT_TASK_BRIEF = "群里已经安静了一阵，请结合群记忆，自然抛出一个大家可能感兴趣、容易接话的话题。"
_MUTE_ALL_REPLIES_KEY = "mute_all_replies"
_MAX_TASK_RETRIES = 3


def _local_tz():
    return datetime.now().astimezone().tzinfo


def _now_local() -> datetime:
    return datetime.now().astimezone()


def _now_utc_naive() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None, microsecond=0)


def _to_utc_naive(value: datetime) -> datetime:
    if value.tzinfo is None:
        value = value.replace(tzinfo=_local_tz())
    return value.astimezone(timezone.utc).replace(tzinfo=None, microsecond=0)


def _from_utc_naive(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value.replace(tzinfo=timezone.utc).astimezone()


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


def format_due_at_local(value: datetime) -> str:
    return value.astimezone().strftime("%Y-%m-%d %H:%M:%S")


def _normalize_task_match_text(text: str) -> str:
    normalized = clean_text(text, max_len=300).lower()
    if not normalized:
        return ""
    normalized = re.sub(r"\s+", "", normalized)
    return re.sub(r"[^\w\u4e00-\u9fff]", "", normalized)


def _task_content_match_score(query: str, content: str) -> float:
    normalized_query = _normalize_task_match_text(query)
    normalized_content = _normalize_task_match_text(content)
    if not normalized_query or not normalized_content:
        return 0.0
    if normalized_query == normalized_content:
        return 1.0
    if normalized_query in normalized_content or normalized_content in normalized_query:
        return 0.92
    ratio = SequenceMatcher(None, normalized_query, normalized_content).ratio()
    return ratio if ratio >= 0.55 else 0.0


def _task_due_at_matches(candidate_due_at: datetime | None, expected_due_at: datetime) -> bool:
    due_local = _from_utc_naive(candidate_due_at)
    if due_local is None:
        return False
    delta_seconds = abs(int((due_local - expected_due_at.astimezone()).total_seconds()))
    return delta_seconds <= 120


def format_task_summary(row: ScheduledTaskRecord) -> str:
    due_local = _from_utc_naive(row.due_at)
    due_text = due_local.strftime("%Y-%m-%d %H:%M:%S") if due_local else "-"
    creator = html.escape(clean_text(row.creator_name or str(row.creator_user_id), max_len=60) or "-")
    task_type = html.escape(clean_text(row.task_type or "", max_len=32) or "unknown")
    content = html.escape(clean_text(row.content or "", max_len=80) or "-")
    return f"#{row.id} [{task_type}] {due_text}\n发起人: {creator}\n内容: {content}"


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
    sent_at: datetime | None = None,
) -> dict[str, Any]:
    settings_data = _task_bucket(group_settings)
    tasks = dict(settings_data.get(_TASKS_KEY) or {})
    state = dict(tasks.get(_COOLDOWN_TOPIC_TASK) or {})
    state["last_processed_user_at"] = _isoformat_local(last_user_at.astimezone())
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
) -> dict[str, Any]:
    now = (at or _now_local()).astimezone()
    settings_data = _task_bucket(group_settings)
    tasks = dict(settings_data.get(_TASKS_KEY) or {})
    state = dict(tasks.get(_COOLDOWN_TOPIC_TASK) or {})
    retry_at = _move_out_of_quiet_hours(
        now + timedelta(minutes=max(5, int(config.proactive_retry_minutes or 30))),
        config,
    )
    state["next_run_at"] = _isoformat_local(retry_at)
    tasks[_COOLDOWN_TOPIC_TASK] = state
    settings_data[_TASKS_KEY] = tasks
    return settings_data


def get_cooldown_status_text(group_settings: dict | None, *, default_enabled: bool) -> str:
    state = get_cooldown_task_state(group_settings)
    enabled = is_cooldown_task_enabled(group_settings, default_enabled=default_enabled)
    last_user_at = _parse_iso_datetime(state.get("last_user_at"))
    next_run_at = _parse_iso_datetime(state.get("next_run_at"))
    last_sent_at = _parse_iso_datetime(state.get("last_sent_at"))

    lines = [
        "<b>主动话题定时任务</b>",
        f"<b>状态</b>: {'已开启' if enabled else '已关闭'}",
        "<b>静默期</b>: 00:00-09:00",
        "<b>最短间隔</b>: 3 小时 + 随机抖动",
    ]
    if last_user_at:
        lines.append(f"<b>最近活跃</b>: {last_user_at.strftime('%Y-%m-%d %H:%M:%S')}")
    if next_run_at and enabled:
        lines.append(f"<b>最早下次执行</b>: {next_run_at.strftime('%Y-%m-%d %H:%M:%S')}")
    if last_sent_at:
        lines.append(f"<b>最近主动发送</b>: {last_sent_at.strftime('%Y-%m-%d %H:%M:%S')}")
    return "\n".join(lines)


async def create_scheduled_task(
    session: AsyncSession,
    *,
    group_id: int,
    creator_user_id: int,
    creator_name: str,
    due_at: datetime,
    task_type: str,
    content: str,
    reply_to_message_id: int | None = None,
    target_user_id: int = 0,
    target_user_name: str = "",
    source: str = "natural_language",
) -> ScheduledTaskRecord:
    row = ScheduledTaskRecord(
        group_id=group_id,
        creator_user_id=creator_user_id,
        creator_name=clean_text(creator_name, max_len=120),
        task_type=clean_text(task_type, max_len=32) or "agent_task",
        content=clean_text(content, max_len=300),
        payload={
            "creator_name": clean_text(creator_name, max_len=120),
            "task_content": clean_text(content, max_len=300),
            "reply_to_message_id": int(reply_to_message_id or 0),
            "target_user_id": int(target_user_id or 0),
            "target_user_name": clean_text(target_user_name, max_len=120),
            "retry_count": 0,
        },
        status="pending",
        due_at=_to_utc_naive(due_at),
        source=source,
    )
    session.add(row)
    await session.flush()
    return row


async def create_reminder_task(
    session: AsyncSession,
    *,
    group_id: int,
    creator_user_id: int,
    creator_name: str,
    due_at: datetime,
    content: str,
    reply_to_message_id: int | None = None,
    target_user_id: int = 0,
    target_user_name: str = "",
    source: str = "natural_language",
) -> ScheduledTaskRecord:
    return await create_scheduled_task(
        session,
        group_id=group_id,
        creator_user_id=creator_user_id,
        creator_name=creator_name,
        due_at=due_at,
        task_type="reminder",
        content=content,
        reply_to_message_id=reply_to_message_id,
        target_user_id=target_user_id,
        target_user_name=target_user_name,
        source=source,
    )


async def list_group_scheduled_tasks(
    session: AsyncSession,
    *,
    group_id: int,
    limit: int = 20,
) -> list[ScheduledTaskRecord]:
    stmt = (
        select(ScheduledTaskRecord)
        .where(
            ScheduledTaskRecord.group_id == group_id,
            ScheduledTaskRecord.status.in_(("pending", "failed")),
        )
        .order_by(ScheduledTaskRecord.due_at.asc(), ScheduledTaskRecord.id.asc())
        .limit(max(1, limit))
    )
    result = await session.execute(stmt)
    return list(result.scalars().all())


async def cancel_scheduled_task(
    session: AsyncSession,
    *,
    group_id: int,
    task_id: int,
    operator_user_id: int,
    allow_any: bool = False,
) -> ScheduledTaskRecord | None:
    row = await session.get(ScheduledTaskRecord, task_id)
    if row is None or row.group_id != group_id:
        return None
    if row.status not in {"pending", "failed"}:
        return None
    if not allow_any and row.creator_user_id != operator_user_id:
        return None
    row.status = "cancelled"
    row.cancelled_at = _now_utc_naive()
    return row


async def match_scheduled_task_for_cancel(
    session: AsyncSession,
    *,
    group_id: int,
    operator_user_id: int,
    allow_any: bool = False,
    task_id: int = 0,
    task_type: str = "",
    due_at: datetime | None = None,
    task_content: str = "",
) -> tuple[ScheduledTaskRecord | None, list[ScheduledTaskRecord]]:
    if task_id > 0:
        row = await session.get(ScheduledTaskRecord, task_id)
        if row is None or row.group_id != group_id:
            return None, []
        if row.status not in {"pending", "failed"}:
            return None, []
        if not allow_any and row.creator_user_id != operator_user_id:
            return None, []
        return row, []

    rows = await list_group_scheduled_tasks(session, group_id=group_id, limit=100)
    if not allow_any:
        rows = [row for row in rows if row.creator_user_id == operator_user_id]
    if not rows:
        return None, []

    task_type_norm = clean_text(task_type, max_len=32).lower()
    if task_type_norm not in {"reminder", "agent_task"}:
        task_type_norm = ""
    task_content_norm = clean_text(task_content, max_len=300)
    has_due_at = due_at is not None
    has_task_content = bool(task_content_norm)

    if not task_type_norm and not has_due_at and not has_task_content:
        if len(rows) == 1:
            return rows[0], []
        return None, rows[:5]

    scored: list[tuple[int, ScheduledTaskRecord]] = []
    for row in rows:
        if task_type_norm and row.task_type != task_type_norm:
            continue
        if has_due_at and not _task_due_at_matches(row.due_at, due_at):
            continue

        score = 0
        if task_type_norm and row.task_type == task_type_norm:
            score += 15
        if has_due_at:
            score += 120
        if has_task_content:
            content_score = _task_content_match_score(task_content_norm, row.content or "")
            min_score = 0.48 if has_due_at else 0.58
            if content_score < min_score:
                continue
            score += int(content_score * 100)

        scored.append((score, row))

    if not scored:
        return None, []

    scored.sort(key=lambda item: (-item[0], item[1].due_at, item[1].id))
    if len(scored) == 1:
        return scored[0][1], []

    top_score = scored[0][0]
    second_score = scored[1][0]
    if top_score >= second_score + 25:
        return scored[0][1], []

    return None, [row for _, row in scored[:5]]


def render_task_list_text(rows: list[ScheduledTaskRecord]) -> str:
    if not rows:
        return "<b>定时任务列表</b>\n当前没有待执行任务。"
    lines = [
        "<b>定时任务列表</b>",
        f"共 {len(rows)} 条",
        "",
    ]
    for row in rows:
        lines.append(format_task_summary(row))
        lines.append("")
    return "\n".join(lines).rstrip()


class ScheduledTaskService:
    def __init__(
        self,
        *,
        settings: Settings,
        bot: Bot,
        memory: MemoryService,
        session_factory: async_sessionmaker[AsyncSession],
        skill_service: SkillService,
    ) -> None:
        self.settings = settings
        self.bot = bot
        self.memory = memory
        self.session_factory = session_factory
        self.skill_service = skill_service
        self.tts_service = DoubaoTTSService(settings)

    async def run_forever(self) -> None:
        interval = float(self.settings.bot.proactive_check_interval_seconds or 60.0)
        while True:
            try:
                await self.run_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("scheduled task loop failed")
            await asyncio.sleep(interval)

    async def run_once(self) -> None:
        async with self.session_factory() as session:
            await self._run_due_scheduled_tasks(session)
            if not _is_quiet_hours(_now_local(), self.settings.bot):
                await self._run_cooldown_topic_tasks(session)

    async def _run_due_scheduled_tasks(self, session: AsyncSession) -> None:
        now_utc = _now_utc_naive()
        stmt = (
            select(ScheduledTaskRecord)
            .where(
                ScheduledTaskRecord.status == "pending",
                ScheduledTaskRecord.due_at <= now_utc,
            )
            .order_by(ScheduledTaskRecord.due_at.asc(), ScheduledTaskRecord.id.asc())
            .limit(50)
        )
        rows = list((await session.execute(stmt)).scalars().all())
        for row in rows:
            row_group_id = int(row.group_id)
            row_id = int(row.id)
            try:
                await self._execute_due_task(session, row)
            except asyncio.CancelledError:
                raise
            except Exception:
                await session.rollback()
                log.exception("[%s] scheduled task execution failed | task_id=%s", row_group_id, row_id)

    async def _execute_due_task(self, session: AsyncSession, row: ScheduledTaskRecord) -> None:
        if row.status != "pending":
            return

        history = await self.memory.get_history_for_llm(row.group_id, reserve_tokens=768)
        due_local = _from_utc_naive(row.due_at)
        payload = row.payload if isinstance(row.payload, dict) else {}
        creator_name = clean_text(row.creator_name or str(row.creator_user_id), max_len=80)
        target_user_name = clean_text(
            str(payload.get("target_user_name", "")).strip() or creator_name,
            max_len=80,
        )
        reply_to_message_id = int(payload.get("reply_to_message_id", 0) or 0) or None
        task_content = clean_text(row.content or "", max_len=300)

        finished_at = _now_utc_naive()
        retry_count = int(payload.get("retry_count", 0) or 0)
        sent_text = ""
        ok = False
        error = ""

        if row.task_type == "reminder":
            task_brief = (
                f"这是一条到期提醒任务。\n"
                f"被提醒人: {target_user_name}\n"
                f"到期时间: {format_due_at_local(due_local or _now_local())}\n"
                f"提醒内容: {task_content}"
            )
            result = await self.skill_service.run_skill(
                "scheduled_task",
                {
                    "task_name": row.task_type,
                    "task_brief": task_brief,
                    "send_message": True,
                    "reply_to_message_id": reply_to_message_id,
                    "fallback_mention_user_id": int(payload.get("target_user_id", 0) or 0),
                    "fallback_mention_name": target_user_name,
                },
                session=session,
                history=history,
                bot=self.bot,
                chat_id=row.group_id,
                current_user_text=task_brief,
            )
            ok = result.ok
            error = result.error or ""
            sent_text = (
                clean_text(str((result.payload or {}).get("text", "")), max_len=240)
                if result.payload
                else ""
            )
        else:
            execution_text = task_content
            if target_user_name:
                execution_text = (
                    f"这是一个已到时间的群聊定时任务，现在请执行下面这件事，"
                    f"完成后把结果自然回复给 {target_user_name}：\n{task_content}"
                )
            answer = await self.skill_service.answer_with_skill(
                execution_text,
                session=session,
                history=history,
                sender_user_id=row.creator_user_id,
                sender_username="",
                sender_is_owner=False,
                sender_is_tg_admin=False,
                message=None,
                intent_type="scheduled_task",
            )
            sent_text = clean_multiline_text(answer.text or "", max_len=2000)
            if sent_text:
                ok = False
                tts_mode = await load_group_tts_mode(session, row.group_id)
                always_tts = is_tts_always_enabled(tts_mode)
                if always_tts and self.tts_service.available:
                    ok = await self.tts_service.send_chat_tts(
                        self.bot,
                        row.group_id,
                        sent_text,
                        reply_to_message_id=reply_to_message_id,
                        fallback_mention_user_id=int(payload.get("target_user_id", 0) or 0),
                        fallback_mention_name=target_user_name,
                        auto_delete_minutes=0,
                        uid=str(row.creator_user_id or row.group_id),
                    )
                if not ok and not always_tts:
                    ok = await send_chat_message(
                        self.bot,
                        row.group_id,
                        sent_text,
                        reply_to_message_id=reply_to_message_id,
                        fallback_mention_user_id=int(payload.get("target_user_id", 0) or 0),
                        fallback_mention_name=target_user_name,
                        auto_delete_minutes=0,
                    )
                if not ok:
                    error = "send_failed"
            else:
                ok = False
                error = "empty_result"

        if ok:
            row.status = "done"
            row.executed_at = finished_at
            row.last_error = ""
            await session.commit()
            if sent_text:
                await self.memory.add_message(
                    row.group_id,
                    "assistant",
                    sent_text,
                    message_type="assistant_scheduled_task",
                    message_id=f"scheduled:{row.id}",
                )
                await self.memory.compact_if_needed(row.group_id)
            log.info("[%s] scheduled task done | task_id=%s type=%s", row.group_id, row.id, row.task_type)
            return

        retry_count += 1
        payload["retry_count"] = retry_count
        row.payload = payload
        row.last_error = clean_text(error or "task_failed", max_len=300)
        if retry_count >= _MAX_TASK_RETRIES:
            row.status = "failed"
        else:
            row.due_at = _now_utc_naive() + timedelta(
                minutes=max(5, int(self.settings.bot.proactive_retry_minutes or 30))
            )
            row.status = "pending"
        await session.commit()

    async def _run_cooldown_topic_tasks(self, session: AsyncSession) -> None:
        now = _now_local()
        stmt = (
            select(Group)
            .join(AuthorizedGroup, AuthorizedGroup.group_id == Group.id)
            .order_by(Group.created_at.asc())
        )
        rows = list((await session.execute(stmt)).scalars().all())
        for row in rows:
            row_id = int(row.id)
            try:
                await self._run_group_cooldown_task(session, row, now=now)
            except asyncio.CancelledError:
                raise
            except Exception:
                await session.rollback()
                log.exception("[%s] scheduled cooldown task failed", row_id)

    async def _run_group_cooldown_task(self, session: AsyncSession, row: Group, *, now: datetime) -> None:
        group_settings = dict(row.settings or {})
        if bool(group_settings.get(_MUTE_ALL_REPLIES_KEY, False)):
            return
        if not is_cooldown_task_enabled(
            group_settings,
            default_enabled=self.settings.bot.proactive_default_enabled,
        ):
            return

        state = get_cooldown_task_state(group_settings)
        last_user_at = _parse_iso_datetime(state.get("last_user_at"))
        next_run_at = _parse_iso_datetime(state.get("next_run_at"))
        last_processed_user_at = _parse_iso_datetime(state.get("last_processed_user_at"))
        last_sent_at = _parse_iso_datetime(state.get("last_sent_at"))

        if last_user_at is None or next_run_at is None or now < next_run_at:
            return
        if last_processed_user_at is not None and last_processed_user_at >= last_user_at:
            return
        if last_sent_at is not None:
            min_gap = timedelta(minutes=max(180, int(self.settings.bot.proactive_idle_minutes or 180)))
            if now < last_sent_at + min_gap:
                return

        history = await self.memory.get_history_for_llm(row.id, reserve_tokens=768)
        task_brief = clean_text(str(state.get("task_brief") or _DEFAULT_TASK_BRIEF), max_len=240)
        result = await self.skill_service.run_skill(
            "scheduled_task",
            {
                "task_name": _COOLDOWN_TOPIC_TASK,
                "task_brief": task_brief,
                "send_message": True,
            },
            session=session,
            history=history,
            bot=self.bot,
            chat_id=row.id,
            current_user_text=task_brief,
        )

        finished_at = _now_local()
        if not result.ok:
            row.settings = postpone_cooldown_task(group_settings, self.settings.bot, at=finished_at)
            await session.commit()
            return

        payload = result.payload if isinstance(result.payload, dict) else {}
        sent_ok = bool(payload.get("sent"))
        sent_text = clean_text(str(payload.get("text", "")), max_len=240)
        row.settings = mark_cooldown_task_processed(
            group_settings,
            last_user_at=last_user_at,
            sent_at=finished_at if sent_ok else None,
        )
        await session.commit()

        if sent_ok and sent_text:
            await self.memory.add_message(row.id, "assistant", sent_text, message_type="assistant_proactive")
            await self.memory.compact_if_needed(row.id)
            log.info("[%s] scheduled cooldown topic sent | %s", row.id, sent_text[:80])
