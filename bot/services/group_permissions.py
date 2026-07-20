"""Per-group default chat permissions with timezone-aware daily windows.

The Bot API exposes default member permissions through
``setChatPermissions``.  This module keeps the user-facing configuration in
``Group.settings`` and deliberately keeps delivery state in memory:

* JSON configuration remains independent per group and needs no migration.
* A process always reconciles every configured group on its first pass, so a
  restart repairs missed transitions and ambiguous previous API responses.
* Successful permission fingerprints suppress duplicate Telegram calls while
  the process is running.  Failures are retried on the next pass.
* A periodic reconciliation also repairs changes made directly in Telegram.

Windows are start-inclusive and end-exclusive.  ``days`` contains the local
weekdays on which a window *starts* (Monday=0).  This makes a 23:00-07:00
window on Friday naturally continue into Saturday morning.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, time as datetime_time, timedelta
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from aiogram import Bot
from aiogram.types import ChatPermissions
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from bot.db.models import AuthorizedGroup, Group
from bot.services.background_health import record_background_failure

log = logging.getLogger(__name__)

_PERMISSION_PASS_DEADLINE_SECONDS = 180.0

GROUP_PERMISSIONS_SETTINGS_KEY = "default_permissions"
GROUP_PERMISSIONS_SCHEMA_VERSION = 1
DEFAULT_GROUP_TIMEZONE = "Asia/Shanghai"
ALL_WEEKDAYS = tuple(range(7))

# Derive this list from the installed aiogram Bot API model.  Besides keeping
# the current sixteen fields complete (including reactions and member tags),
# this automatically makes validation and Telegram payloads include new
# boolean permission fields when aiogram adds them.
CHAT_PERMISSION_FIELDS = tuple(ChatPermissions.model_fields)
# Public short name used by the settings API / Mini App contract.
PERMISSION_FIELDS = CHAT_PERMISSION_FIELDS

_TOP_LEVEL_FIELDS = {
    "version",
    "timezone",
    "schedule_enabled",
    "base",
    "windows",
}
_WINDOW_FIELDS = {
    "id",
    "name",
    "enabled",
    "start",
    "end",
    "days",
    "priority",
    "overrides",
}
_WINDOW_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_CLOCK_RE = re.compile(r"^(?:[01]\d|2[0-3]):[0-5]\d$")
_MAX_WINDOWS = 32

_PERMISSION_LABELS = {
    "can_send_messages": "发送文字消息",
    "can_send_audios": "发送音频",
    "can_send_documents": "发送文件",
    "can_send_photos": "发送图片",
    "can_send_videos": "发送视频",
    "can_send_video_notes": "发送视频消息",
    "can_send_voice_notes": "发送语音消息",
    "can_send_polls": "发送投票",
    "can_send_other_messages": "发送贴纸、动画和游戏",
    "can_add_web_page_previews": "添加链接预览",
    "can_react_to_messages": "添加消息反应",
    "can_edit_tag": "编辑成员标签",
    "can_change_info": "修改群组信息",
    "can_invite_users": "邀请用户",
    "can_pin_messages": "置顶消息",
    "can_manage_topics": "管理话题",
}


@dataclass(frozen=True, slots=True)
class ResolvedGroupPermissions:
    """Effective default permissions at one instant."""

    timezone: str
    local_datetime: datetime
    permissions: dict[str, bool]
    active_window_ids: tuple[str, ...]

    def as_chat_permissions(self) -> ChatPermissions:
        return ChatPermissions(**self.permissions)


def permission_field_document() -> list[dict[str, str]]:
    """Metadata used by the Mini App to render every Bot API field."""
    return [
        {"key": field, "label": _PERMISSION_LABELS.get(field, field)}
        for field in CHAT_PERMISSION_FIELDS
    ]


def _ensure_object(value: Any, *, field: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{field} must be an object")
    return value


def _ensure_bool(value: Any, *, field: str) -> bool:
    if type(value) is not bool:
        raise ValueError(f"{field} must be a boolean")
    return value


def _validate_timezone(value: Any) -> str:
    timezone_name = str(value or DEFAULT_GROUP_TIMEZONE).strip()
    if not timezone_name or len(timezone_name) > 64:
        raise ValueError("timezone must be a valid IANA timezone")
    try:
        ZoneInfo(timezone_name)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ValueError(f"unknown timezone: {timezone_name}") from exc
    return timezone_name


def _validate_clock(value: Any, *, field: str) -> str:
    normalized = str(value or "").strip()
    if not _CLOCK_RE.fullmatch(normalized):
        raise ValueError(f"{field} must use HH:MM (00:00-23:59)")
    return normalized


def _validate_permission_map(
    value: Any,
    *,
    field: str,
    partial: bool,
) -> dict[str, bool]:
    raw = _ensure_object(value, field=field)
    known = set(CHAT_PERMISSION_FIELDS)
    unknown = set(raw) - known
    if unknown:
        raise ValueError(
            f"{field} contains unsupported permissions: {', '.join(sorted(unknown))}"
        )
    if not partial:
        missing = known - set(raw)
        if missing:
            raise ValueError(
                f"{field} is missing permissions: {', '.join(sorted(missing))}"
            )
    if partial and not raw:
        raise ValueError(f"{field} must contain at least one permission")
    return {
        name: _ensure_bool(raw[name], field=f"{field}.{name}")
        for name in CHAT_PERMISSION_FIELDS
        if name in raw
    }


def _validate_days(value: Any, *, field: str) -> list[int]:
    if value is None:
        return list(ALL_WEEKDAYS)
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise ValueError(f"{field} must be an array of weekdays")
    days: set[int] = set()
    for item in value:
        if isinstance(item, bool) or not isinstance(item, int) or not 0 <= item <= 6:
            raise ValueError(f"{field} entries must be integers from 0 to 6")
        days.add(item)
    if not days:
        raise ValueError(f"{field} must contain at least one weekday")
    return sorted(days)


def validate_group_permissions_config(value: Any) -> dict[str, Any]:
    """Validate and canonicalize a ``Group.settings`` permission block.

    ``base`` must explicitly contain every field supported by the installed
    aiogram ``ChatPermissions`` model.  Window overrides may contain any
    non-empty subset.  Requiring a complete base prevents a library default
    or Telegram implication rule from silently enabling a permission.
    """
    raw = _ensure_object(value, field=GROUP_PERMISSIONS_SETTINGS_KEY)
    unknown = set(raw) - _TOP_LEVEL_FIELDS
    if unknown:
        raise ValueError(
            "default_permissions contains unsupported fields: "
            + ", ".join(sorted(unknown))
        )

    version = raw.get("version", GROUP_PERMISSIONS_SCHEMA_VERSION)
    if type(version) is not int or version != GROUP_PERMISSIONS_SCHEMA_VERSION:
        raise ValueError(
            f"default_permissions.version must be {GROUP_PERMISSIONS_SCHEMA_VERSION}"
        )
    if "base" not in raw:
        raise ValueError("default_permissions.base is required")

    schedule_enabled = _ensure_bool(
        raw.get("schedule_enabled", False),
        field="default_permissions.schedule_enabled",
    )
    timezone_name = _validate_timezone(raw.get("timezone"))
    base = _validate_permission_map(
        raw["base"],
        field="default_permissions.base",
        partial=False,
    )

    raw_windows = raw.get("windows", [])
    if isinstance(raw_windows, (str, bytes)) or not isinstance(raw_windows, Sequence):
        raise ValueError("default_permissions.windows must be an array")
    if len(raw_windows) > _MAX_WINDOWS:
        raise ValueError(f"default_permissions.windows supports at most {_MAX_WINDOWS} entries")

    windows: list[dict[str, Any]] = []
    ids: set[str] = set()
    for index, item in enumerate(raw_windows):
        prefix = f"default_permissions.windows[{index}]"
        window = _ensure_object(item, field=prefix)
        extra = set(window) - _WINDOW_FIELDS
        if extra:
            raise ValueError(
                f"{prefix} contains unsupported fields: {', '.join(sorted(extra))}"
            )
        window_id = str(window.get("id") or "").strip()
        if not _WINDOW_ID_RE.fullmatch(window_id):
            raise ValueError(f"{prefix}.id must match {_WINDOW_ID_RE.pattern}")
        if window_id in ids:
            raise ValueError(f"duplicate permission window id: {window_id}")
        ids.add(window_id)

        name = str(window.get("name") or window_id).strip()
        if not name or len(name) > 80:
            raise ValueError(f"{prefix}.name must contain 1-80 characters")
        enabled = _ensure_bool(window.get("enabled", True), field=f"{prefix}.enabled")
        priority = window.get("priority", 0)
        if (
            isinstance(priority, bool)
            or not isinstance(priority, int)
            or not -1000 <= priority <= 1000
        ):
            raise ValueError(f"{prefix}.priority must be an integer from -1000 to 1000")
        windows.append(
            {
                "id": window_id,
                "name": name,
                "enabled": enabled,
                "start": _validate_clock(window.get("start"), field=f"{prefix}.start"),
                "end": _validate_clock(window.get("end"), field=f"{prefix}.end"),
                "days": _validate_days(window.get("days"), field=f"{prefix}.days"),
                "priority": priority,
                "overrides": _validate_permission_map(
                    window.get("overrides"),
                    field=f"{prefix}.overrides",
                    partial=True,
                ),
            }
        )

    return {
        "version": GROUP_PERMISSIONS_SCHEMA_VERSION,
        "timezone": timezone_name,
        "schedule_enabled": schedule_enabled,
        "base": base,
        "windows": windows,
    }


def normalize_group_permission_config(value: Any) -> dict[str, Any]:
    """Canonical Mini App/API helper (validation is part of normalization)."""
    return validate_group_permissions_config(value)


def repair_group_permissions_config(
    value: Any,
    *,
    fallback_base: Mapping[str, bool],
) -> dict[str, Any]:
    """Best-effort compatibility repair using a live Telegram baseline.

    A newer Bot API may add permission fields after an older complete config
    was saved. Strict validation must still reject incomplete writes, while a
    stored document needs a path back into the editor. Existing recognized
    booleans and valid windows are retained; newly introduced base fields are
    filled from Telegram and malformed legacy fragments are dropped.
    """
    live_base = _validate_permission_map(
        fallback_base,
        field="telegram_permissions",
        partial=False,
    )
    baseline = {
        "version": GROUP_PERMISSIONS_SCHEMA_VERSION,
        "timezone": DEFAULT_GROUP_TIMEZONE,
        "schedule_enabled": False,
        "base": live_base,
        "windows": [],
    }
    if not isinstance(value, Mapping):
        return validate_group_permissions_config(baseline)

    repaired_base = dict(live_base)
    raw_base = value.get("base")
    if isinstance(raw_base, Mapping):
        for field in CHAT_PERMISSION_FIELDS:
            candidate = raw_base.get(field)
            if type(candidate) is bool:
                repaired_base[field] = candidate

    timezone_name = str(value.get("timezone") or DEFAULT_GROUP_TIMEZONE).strip()
    seed = {
        "version": GROUP_PERMISSIONS_SCHEMA_VERSION,
        "timezone": timezone_name,
        "schedule_enabled": value.get("schedule_enabled") is True,
        "base": repaired_base,
        "windows": [],
    }
    try:
        seed = validate_group_permissions_config(seed)
    except ValueError:
        seed["timezone"] = DEFAULT_GROUP_TIMEZONE
        seed = validate_group_permissions_config(seed)

    raw_windows = value.get("windows")
    if isinstance(raw_windows, (str, bytes)) or not isinstance(raw_windows, Sequence):
        return seed
    repaired_windows: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for raw_window in raw_windows[:_MAX_WINDOWS]:
        if not isinstance(raw_window, Mapping):
            continue
        raw_overrides = raw_window.get("overrides")
        if not isinstance(raw_overrides, Mapping):
            continue
        overrides = {
            field: raw_overrides[field]
            for field in CHAT_PERMISSION_FIELDS
            if type(raw_overrides.get(field)) is bool
        }
        if not overrides:
            continue
        candidate = {
            "id": raw_window.get("id"),
            "name": raw_window.get("name"),
            "enabled": (
                raw_window.get("enabled")
                if type(raw_window.get("enabled")) is bool
                else True
            ),
            "start": raw_window.get("start"),
            "end": raw_window.get("end"),
            "days": raw_window.get("days"),
            "priority": raw_window.get("priority", 0),
            "overrides": overrides,
        }
        try:
            normalized_window = validate_group_permissions_config(
                {**seed, "windows": [candidate]}
            )["windows"][0]
        except (IndexError, ValueError):
            continue
        window_id = str(normalized_window["id"])
        if window_id in seen_ids:
            continue
        seen_ids.add(window_id)
        repaired_windows.append(normalized_window)
    return validate_group_permissions_config({**seed, "windows": repaired_windows})


def get_group_permissions_config(
    group_settings: Mapping[str, Any] | None,
) -> dict[str, Any] | None:
    """Return a canonical configured block, or ``None`` when absent."""
    if not isinstance(group_settings, Mapping):
        return None
    raw = group_settings.get(GROUP_PERMISSIONS_SETTINGS_KEY)
    if raw is None:
        return None
    return validate_group_permissions_config(raw)


def set_group_permissions_config(
    group_settings: Mapping[str, Any] | None,
    config: Any,
) -> dict[str, Any]:
    """Copy group settings and replace only the permission configuration."""
    updated = dict(group_settings or {})
    updated[GROUP_PERMISSIONS_SETTINGS_KEY] = validate_group_permissions_config(config)
    return updated


def remove_group_permissions_config(
    group_settings: Mapping[str, Any] | None,
) -> dict[str, Any]:
    updated = dict(group_settings or {})
    updated.pop(GROUP_PERMISSIONS_SETTINGS_KEY, None)
    return updated


def _clock_minutes(value: str) -> int:
    hour, minute = value.split(":", 1)
    return int(hour) * 60 + int(minute)


def _window_is_active(window: Mapping[str, Any], local_at: datetime) -> bool:
    if not window["enabled"]:
        return False
    start = _clock_minutes(str(window["start"]))
    end = _clock_minutes(str(window["end"]))
    current = local_at.hour * 60 + local_at.minute
    days = set(window["days"])
    weekday = local_at.weekday()

    if start < end:
        return weekday in days and start <= current < end
    # Cross-midnight windows (and equal times, which intentionally mean a
    # 24-hour window) are keyed to the weekday on which they started.
    if current >= start:
        return weekday in days
    return ((weekday - 1) % 7) in days and current < end


def _local_datetime(at: datetime | None, timezone_name: str) -> datetime:
    timezone = ZoneInfo(timezone_name)
    if at is None:
        return datetime.now(timezone).replace(microsecond=0)
    if at.tzinfo is None:
        return at.replace(tzinfo=timezone, microsecond=0)
    return at.astimezone(timezone).replace(microsecond=0)


def _resolve_canonical(
    config: Mapping[str, Any],
    *,
    at: datetime | None,
) -> ResolvedGroupPermissions:
    local_at = _local_datetime(at, str(config["timezone"]))
    permissions = dict(config["base"])
    active: list[tuple[int, int, Mapping[str, Any]]] = []
    if config["schedule_enabled"]:
        for index, window in enumerate(config["windows"]):
            if _window_is_active(window, local_at):
                active.append((int(window["priority"]), index, window))
    # Low priority first; later/high-priority windows deterministically win
    # when two active windows override the same permission.
    active.sort(key=lambda item: (item[0], item[1]))
    for _priority, _index, window in active:
        permissions.update(window["overrides"])
    return ResolvedGroupPermissions(
        timezone=str(config["timezone"]),
        local_datetime=local_at,
        permissions=permissions,
        active_window_ids=tuple(str(item[2]["id"]) for item in active),
    )


def resolve_group_permissions(
    config: Any,
    *,
    at: datetime | None = None,
) -> ResolvedGroupPermissions:
    """Resolve the complete Bot API permission set for one instant."""
    return _resolve_canonical(validate_group_permissions_config(config), at=at)


def effective_group_permissions(
    config: Any,
    *,
    at: datetime | None = None,
) -> dict[str, bool]:
    """Return only the complete effective permission map."""
    return resolve_group_permissions(config, at=at).permissions


def _combine_local(day: date, clock: str, timezone: ZoneInfo) -> datetime:
    hour, minute = (int(part) for part in clock.split(":", 1))
    return datetime.combine(day, datetime_time(hour, minute), tzinfo=timezone)


def next_group_permission_transition(
    config: Any,
    *,
    at: datetime | None = None,
) -> datetime | None:
    """Return the next local instant at which effective permissions change."""
    canonical = validate_group_permissions_config(config)
    if not canonical["schedule_enabled"]:
        return None
    timezone = ZoneInfo(str(canonical["timezone"]))
    local_at = _local_datetime(at, str(canonical["timezone"]))
    candidates: set[datetime] = set()
    first_day = local_at.date() - timedelta(days=1)
    for offset in range(10):
        start_day = first_day + timedelta(days=offset)
        for window in canonical["windows"]:
            if not window["enabled"] or start_day.weekday() not in window["days"]:
                continue
            start_at = _combine_local(start_day, str(window["start"]), timezone)
            start_minutes = _clock_minutes(str(window["start"]))
            end_minutes = _clock_minutes(str(window["end"]))
            end_day = start_day + timedelta(days=1) if start_minutes >= end_minutes else start_day
            end_at = _combine_local(end_day, str(window["end"]), timezone)
            if start_at > local_at:
                candidates.add(start_at)
            if end_at > local_at:
                candidates.add(end_at)

    for candidate in sorted(candidates):
        before = _resolve_canonical(
            canonical,
            at=candidate - timedelta(microseconds=1),
        ).permissions
        after = _resolve_canonical(canonical, at=candidate).permissions
        if before != after:
            return candidate
    return None


def permissions_fingerprint(permissions: Mapping[str, bool]) -> str:
    encoded = json.dumps(
        {field: bool(permissions[field]) for field in CHAT_PERMISSION_FIELDS},
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def telegram_permissions_snapshot(value: Any) -> dict[str, bool]:
    """Expand a Telegram/aiogram permission object into every known field."""
    if isinstance(value, Mapping):
        raw = value
    elif hasattr(value, "model_dump"):
        raw = value.model_dump()
    else:
        raw = {
            field: getattr(value, field, None)
            for field in CHAT_PERMISSION_FIELDS
        }
    # getChat normally returns explicit booleans.  Treat an omitted optional
    # field as false rather than enabling a capability while taking a snapshot.
    return {field: raw.get(field) is True for field in CHAT_PERMISSION_FIELDS}


async def fetch_telegram_default_permissions(bot: Bot, group_id: int) -> dict[str, bool]:
    """Fetch a complete baseline suitable for the first Mini App save."""
    chat = await bot.get_chat(chat_id=int(group_id))
    permissions = getattr(chat, "permissions", None)
    if permissions is None:
        raise RuntimeError("Telegram did not return default chat permissions")
    return telegram_permissions_snapshot(permissions)


class GroupPermissionService:
    """Reconcile configured per-group default permissions with Telegram."""

    def __init__(
        self,
        *,
        bot: Bot,
        session_factory: async_sessionmaker[AsyncSession],
        check_interval_seconds: float = 15.0,
        reconcile_interval_seconds: float = 900.0,
    ) -> None:
        self.bot = bot
        self.session_factory = session_factory
        self.check_interval_seconds = max(1.0, float(check_interval_seconds))
        self.reconcile_interval_seconds = max(0.0, float(reconcile_interval_seconds))
        self._applied_fingerprints: dict[int, str] = {}
        self._last_success_monotonic: dict[int, float] = {}
        self._last_applied_at: dict[int, datetime] = {}
        self._last_errors: dict[int, str] = {}
        self._locks: dict[int, asyncio.Lock] = {}
        self._compat_configs: dict[int, tuple[str, dict[str, Any]]] = {}

    async def run_forever(self) -> None:
        log.info("group default permission service started")
        consecutive_failures = 0
        while True:
            try:
                async with asyncio.timeout(_PERMISSION_PASS_DEADLINE_SECONDS):
                    await self.run_once(raise_on_total_failure=True)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.exception("group default permission pass failed")
                consecutive_failures = record_background_failure(
                    service="group default permission service",
                    previous_failures=consecutive_failures,
                    error=exc,
                )
            else:
                consecutive_failures = 0
            await asyncio.sleep(self.check_interval_seconds)

    async def run_once(
        self,
        *,
        at: datetime | None = None,
        raise_on_total_failure: bool = False,
    ) -> int:
        """Reconcile all authorized configured groups; return API successes."""
        async with self.session_factory() as session:
            rows = (
                await session.execute(
                    select(Group.id)
                    .join(AuthorizedGroup, AuthorizedGroup.group_id == Group.id)
                    .order_by(Group.id)
                )
            ).all()
        applied = 0
        failed = 0
        for (group_id,) in rows:
            if await self.apply_group(int(group_id), at=at):
                applied += 1
            elif int(group_id) in self._last_errors:
                failed += 1
        if raise_on_total_failure and failed and applied == 0:
            raise RuntimeError(
                f"group permission apply failed for all {failed} attempted groups"
            )
        return applied

    async def apply_group(
        self,
        group_id: int,
        *,
        at: datetime | None = None,
        force: bool = False,
    ) -> bool:
        """Load and immediately reconcile one authorized group after an edit."""
        group_id = int(group_id)
        lock = self._locks.setdefault(group_id, asyncio.Lock())
        async with lock:
            # Load after acquiring the same lock used by apply_group_now().
            # If an API save races a scheduler pass, the last lock holder sees
            # the committed config instead of re-applying a stale snapshot.
            async with self.session_factory() as session:
                authorized = await session.get(AuthorizedGroup, group_id)
                group = await session.get(Group, group_id)
            if authorized is None or group is None:
                self.forget_group(group_id)
                return False
            settings_data = group.settings if isinstance(group.settings, Mapping) else {}
            raw = settings_data.get(GROUP_PERMISSIONS_SETTINGS_KEY)
            if raw is None:
                self.forget_group(group_id)
                return False
            return await self._apply_config_locked(
                group_id,
                raw,
                at=at,
                force=force,
            )

    async def apply_group_now(
        self,
        group_id: int,
        config: Any,
        *,
        at: datetime | None = None,
    ) -> bool:
        """Immediately apply a just-validated API payload after it is saved."""
        canonical = normalize_group_permission_config(config)
        group_id = int(group_id)
        lock = self._locks.setdefault(group_id, asyncio.Lock())
        async with lock:
            return await self._apply_config_locked(
                group_id,
                canonical,
                at=at,
                force=True,
            )

    async def apply_settings(
        self,
        group_id: int,
        group_settings: Mapping[str, Any],
        *,
        at: datetime | None = None,
        force: bool = False,
    ) -> bool:
        """Apply one already-loaded settings document when needed."""
        group_id = int(group_id)
        raw = group_settings.get(GROUP_PERMISSIONS_SETTINGS_KEY)
        if raw is None:
            self.forget_group(group_id)
            return False
        lock = self._locks.setdefault(group_id, asyncio.Lock())
        async with lock:
            return await self._apply_config_locked(
                group_id,
                raw,
                at=at,
                force=force,
            )

    async def _apply_config_locked(
        self,
        group_id: int,
        raw_config: Any,
        *,
        at: datetime | None,
        force: bool,
    ) -> bool:
        """Validate and apply while the caller holds this group's lock."""
        try:
            config = validate_group_permissions_config(raw_config)
            self._compat_configs.pop(group_id, None)
        except ValueError as exc:
            try:
                raw_digest = hashlib.sha256(
                    json.dumps(
                        raw_config,
                        sort_keys=True,
                        separators=(",", ":"),
                        default=str,
                    ).encode("utf-8")
                ).hexdigest()
                cached = self._compat_configs.get(group_id)
                if cached is not None and cached[0] == raw_digest:
                    config = cached[1]
                else:
                    live_base = await fetch_telegram_default_permissions(
                        self.bot,
                        group_id,
                    )
                    config = repair_group_permissions_config(
                        raw_config,
                        fallback_base=live_base,
                    )
                    self._compat_configs[group_id] = (raw_digest, config)
                    log.warning(
                        "legacy group permission config repaired in memory | group=%s error=%s",
                        group_id,
                        exc,
                    )
            except Exception as repair_exc:
                self._last_errors[group_id] = str(exc)
                log.warning(
                    "invalid group permission config | group=%s error=%s repair_error=%s",
                    group_id,
                    exc,
                    repair_exc,
                )
                return False
        resolved = _resolve_canonical(config, at=at)

        fingerprint = permissions_fingerprint(resolved.permissions)
        elapsed = time.monotonic() - self._last_success_monotonic.get(group_id, 0.0)
        if (
            not force
            and self._applied_fingerprints.get(group_id) == fingerprint
            and elapsed < self.reconcile_interval_seconds
        ):
            return False
        try:
            result = await self.bot.set_chat_permissions(
                chat_id=group_id,
                permissions=resolved.as_chat_permissions(),
                use_independent_chat_permissions=True,
            )
            if result is False:
                raise RuntimeError("Telegram returned false")
        except Exception as exc:
            self._last_errors[group_id] = str(exc) or type(exc).__name__
            log.exception(
                "group default permission apply failed | group=%s active_windows=%s",
                group_id,
                ",".join(resolved.active_window_ids) or "none",
            )
            return False

        self._applied_fingerprints[group_id] = fingerprint
        self._last_success_monotonic[group_id] = time.monotonic()
        self._last_applied_at[group_id] = resolved.local_datetime
        self._last_errors.pop(group_id, None)
        log.info(
            "group default permissions applied | group=%s active_windows=%s",
            group_id,
            ",".join(resolved.active_window_ids) or "none",
        )
        return True

    def forget_group(self, group_id: int) -> None:
        group_id = int(group_id)
        self._applied_fingerprints.pop(group_id, None)
        self._last_success_monotonic.pop(group_id, None)
        self._last_applied_at.pop(group_id, None)
        self._last_errors.pop(group_id, None)
        self._compat_configs.pop(group_id, None)

    def status(self, group_id: int) -> dict[str, Any]:
        """In-process delivery status for a Mini App status hint."""
        group_id = int(group_id)
        applied_at = self._last_applied_at.get(group_id)
        return {
            # Conservative: after a newer apply failed, an older fingerprint
            # may still be recorded but the requested state is not confirmed.
            "applied": (
                group_id in self._applied_fingerprints
                and group_id not in self._last_errors
            ),
            "last_applied_at": applied_at.isoformat() if applied_at else "",
            "last_error": self._last_errors.get(group_id, ""),
        }


_group_permission_service: GroupPermissionService | None = None


def init_group_permission_service(service: GroupPermissionService) -> None:
    global _group_permission_service
    _group_permission_service = service


def get_group_permission_service() -> GroupPermissionService | None:
    return _group_permission_service


__all__ = [
    "ALL_WEEKDAYS",
    "CHAT_PERMISSION_FIELDS",
    "DEFAULT_GROUP_TIMEZONE",
    "GROUP_PERMISSIONS_SCHEMA_VERSION",
    "GROUP_PERMISSIONS_SETTINGS_KEY",
    "PERMISSION_FIELDS",
    "GroupPermissionService",
    "ResolvedGroupPermissions",
    "effective_group_permissions",
    "fetch_telegram_default_permissions",
    "get_group_permission_service",
    "get_group_permissions_config",
    "init_group_permission_service",
    "next_group_permission_transition",
    "normalize_group_permission_config",
    "permission_field_document",
    "permissions_fingerprint",
    "repair_group_permissions_config",
    "remove_group_permissions_config",
    "resolve_group_permissions",
    "set_group_permissions_config",
    "telegram_permissions_snapshot",
    "validate_group_permissions_config",
]
