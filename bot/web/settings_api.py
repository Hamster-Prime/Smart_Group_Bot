from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import replace
from functools import wraps
from typing import Annotated, Any, Literal

from aiohttp import web
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictFloat,
    StrictInt,
    ValidationError,
)
from sqlalchemy import delete, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from bot.config import Settings
from bot.db.models import (
    Admin,
    AuthorizedGroup,
    Group,
    GroupMember,
    GroupPermanentMemory,
    KeywordReply,
    ModerationExemption,
    ModerationRule,
    ReplyMute,
    ScheduledMessage,
    SpeechStyleSample,
    UserWarning,
)
from bot.services.at_reply import is_at_reply_enabled, set_at_reply_enabled
from bot.services.api_model_query import (
    clear_group_api_model_query_secret,
    get_api_model_query_config,
    group_api_model_query_secret_exists,
    normalize_api_model_query_api_key,
    normalize_api_model_query_base_url,
    replace_group_api_model_query_secret,
    set_api_model_query_config,
)
from bot.services.ban_audit import record_ban_event
from bot.services.doubao_tts import normalize_tts_mode, set_tts_mode
from bot.services.proactive import (
    get_cooldown_task_state,
    set_cooldown_task_enabled,
)
from bot.services.join_screening import add_global_ban, list_global_bans, remove_global_ban
from bot.services.join_screening import is_globally_banned
from bot.services.join_verification import (
    activate_manual_unban_recoveries,
    activate_manual_unban_recovery,
    ban_member as enforce_ban_member,
    close_private_challenge_messages,
    complete_leased_join_verification,
    delete_join_verification,
    delete_verification_prompts,
    get_join_verification,
    join_verification_policy,
    lease_join_verification_for_unban,
    lease_join_verifications_for_user_unban,
    reconcile_moderation_ban_after_lost_lease,
    release_moderation_restriction_after_exemption,
    restore_member_permissions,
    turnstile_verification_configured,
    unban_member as enforce_unban_member,
    verification_provider,
    verification_release_blocked_by_ban,
    verification_restriction_required,
    verification_service_ready,
)
from bot.services.update_delivery import (
    clear_privileged_operator_group,
    mark_privileged_operator,
    unmark_privileged_operator,
)
from bot.services.request_priority import privileged_request_scope
from bot.services.group_permissions import (
    GROUP_PERMISSIONS_SETTINGS_KEY,
    PERMISSION_FIELDS,
    fetch_telegram_default_permissions,
    get_group_permission_service,
    normalize_group_permission_config,
    permission_field_document,
    repair_group_permissions_config,
    resolve_group_permissions,
)
from bot.services.message_templates import normalize_template_buttons
from bot.services.member_identity import member_identity_document
from bot.services.patrol import get_patrol_service, parse_schedule_time, patrol_policy
from bot.services.runtime_config import (
    RuntimeConfigConflictError,
    RuntimeConfigEncryptionError,
    RuntimeConfigManager,
)
from bot.services.speech_style import (
    STYLE_SETTINGS_KEY,
    get_style_state,
    set_style_target,
)
from bot.utils.security import clean_multiline_text, clean_text
from bot.utils.timezone import now_shanghai_naive
from bot.web.auth import require_authenticated_user, require_super_admin

log = logging.getLogger(__name__)


async def _run_privileged_group_fanout(
    group_ids: list[int],
    operation: Callable[[int], Awaitable[bool]],
) -> list[bool]:
    semaphore = asyncio.Semaphore(4)

    async def run_one(group_id: int) -> bool:
        async with semaphore:
            try:
                with privileged_request_scope():
                    async with asyncio.timeout(45.0):
                        return bool(await operation(int(group_id)))
            except asyncio.CancelledError:
                raise
            except Exception:
                log.info(
                    "web privileged group operation failed | group=%s",
                    group_id,
                    exc_info=True,
                )
                return False

    return list(await asyncio.gather(*(run_one(group_id) for group_id in group_ids)))

_GROUP_SETTING_FIELDS = {
    "av_enabled",
    "mute_all_replies",
    "at_reply_mode",
    "tts_mode",
    "join_verification_enabled",
    "join_verification_provider",
    "welcome_message",
    "welcome_buttons",
    GROUP_PERMISSIONS_SETTINGS_KEY,
    "patrol_enabled",
    "raid_guard_enabled",
    "raid_guard_pin_message",
    "raid_guard_join_threshold",
    "raid_guard_window_seconds",
    "raid_guard_lockdown_seconds",
    "raid_guard_lookback_seconds",
    "raid_guard_challenge_timeout_seconds",
    "call_admin_enabled",
    "call_admin_pin_message",
    "call_admin_targets",
    "vote_ban_enabled",
    "vote_ban_pin_message",
    "vote_ban_threshold",
    "vote_ban_duration_seconds",
    "vote_ban_trigger_limit",
    "vote_ban_trigger_window_seconds",
    "proactive_enabled",
    "proactive_task_brief",
    "mimic_target_user_id",
    "mimic_target_user_name",
    "mimic_profile_text",
    "api_model_query_enabled",
    "api_model_query_base_url",
    "api_model_query_http_timeout_sec",
    "api_model_query_check_timeout_sec",
}
# Per-group raid-guard numeric overrides: key -> (min, max); null = inherit.
_RAID_GUARD_GROUP_INT_FIELDS = {
    "raid_guard_join_threshold": (2, 1000),
    "raid_guard_window_seconds": (5, 3600),
    "raid_guard_lockdown_seconds": (60, 86400),
    "raid_guard_lookback_seconds": (0, 86400),
    "raid_guard_challenge_timeout_seconds": (60, 86400),
}
# Per-group vote-ban numeric overrides, same null-inherits convention.
_VOTE_BAN_GROUP_INT_FIELDS = {
    "vote_ban_threshold": (2, 1000),
    "vote_ban_duration_seconds": (60, 86400),
    "vote_ban_trigger_limit": (1, 1000),
    "vote_ban_trigger_window_seconds": (60, 604800),
}
_SCHEDULED_TASKS_KEY = "scheduled_tasks"
_COOLDOWN_TASK_KEY = "cooldown_topic"
_GROUP_UPDATE_LOCKS: dict[int, asyncio.Lock] = {}
_JSON_BODY_TIMEOUT_SECONDS = 5.0
_JSON_BODY_CANCEL_GRACE_SECONDS = 0.1
_JSON_BODY_ORPHAN_LIMIT = 32
_JSON_BODY_ORPHANS: set[asyncio.Task[Any]] = set()
_JSON_BODY_TASKS: set[asyncio.Task[Any]] = set()
_MEMBER_IDENTITY_LOOKUP_TIMEOUT_SECONDS = 5.0
_MEMBER_IDENTITY_CANCEL_GRACE_SECONDS = 0.2
_MEMBER_IDENTITY_REQUEST_LOOKUP_LIMIT = 8
_MEMBER_IDENTITY_REQUEST_BUDGET_SECONDS = 4.0
_MEMBER_IDENTITY_CACHE_TTL_SECONDS = 300.0
_MEMBER_IDENTITY_NEGATIVE_CACHE_TTL_SECONDS = 30.0
_MEMBER_IDENTITY_CACHE_MAX_ENTRIES = 4096
_MEMBER_IDENTITY_LOOKUP_CONCURRENCY = 8
_MEMBER_IDENTITY_RPC_TASK_LIMIT = 32
_MEMBER_IDENTITY_ORPHAN_LIMIT = 8
_MEMBER_IDENTITY_CIRCUIT_COOLDOWN_SECONDS = 10.0
_MEMBER_IDENTITY_CIRCUIT_OPEN_UNTIL = 0.0
_MEMBER_IDENTITY_CACHE: dict[
    tuple[int, int, int],
    tuple[float, tuple[str, str, bool] | None],
] = {}
_MEMBER_IDENTITY_INFLIGHT: dict[
    tuple[int, int, int],
    asyncio.Task[tuple[str, str, bool] | None],
] = {}
_MEMBER_IDENTITY_ORPHANS: set[asyncio.Task[Any]] = set()
_MEMBER_IDENTITY_RPC_TASKS: set[asyncio.Task[Any]] = set()
_MEMBER_IDENTITY_LOOKUP_LOOP: asyncio.AbstractEventLoop | None = None
_MEMBER_IDENTITY_LOOKUP_SEMAPHORE: asyncio.Semaphore | None = None


class _RuntimeSettingsUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    revision: StrictInt = Field(ge=1)
    config: dict[str, Any]
    secret_changes: dict[str, dict[str, str]] = Field(default_factory=dict)


class _GroupSettingsUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    av_enabled: StrictBool | None = None
    mute_all_replies: StrictBool | None = None
    at_reply_mode: StrictBool | None = None
    tts_mode: Literal["off", "on", "always"] | None = None
    # None clears the group override and inherits the global default.
    join_verification_enabled: StrictBool | None = None
    join_verification_provider: (
        Literal["turnstile", "hcaptcha", "turnstile_hcaptcha"] | None
    ) = None
    welcome_message: str | None = Field(default=None, max_length=4000)
    welcome_buttons: list[dict[str, Any]] | None = Field(default=None, max_length=12)
    default_permissions: dict[str, Any] | None = None
    patrol_enabled: StrictBool | None = None
    raid_guard_enabled: StrictBool | None = None
    raid_guard_pin_message: StrictBool | None = None
    raid_guard_join_threshold: StrictInt | None = Field(default=None, ge=2, le=1000)
    raid_guard_window_seconds: StrictInt | None = Field(default=None, ge=5, le=3600)
    raid_guard_lockdown_seconds: StrictInt | None = Field(
        default=None, ge=60, le=86400
    )
    raid_guard_lookback_seconds: StrictInt | None = Field(
        default=None, ge=0, le=86400
    )
    raid_guard_challenge_timeout_seconds: StrictInt | None = Field(
        default=None, ge=60, le=86400
    )
    # None inherits the global default; targets [] or missing = all admins.
    call_admin_enabled: StrictBool | None = None
    call_admin_pin_message: StrictBool | None = None
    call_admin_targets: list[Annotated[StrictInt, Field(gt=0)]] | None = Field(
        default=None,
        max_length=100,
    )
    vote_ban_enabled: StrictBool | None = None
    vote_ban_pin_message: StrictBool | None = None
    vote_ban_threshold: StrictInt | None = Field(default=None, ge=2, le=1000)
    vote_ban_duration_seconds: StrictInt | None = Field(
        default=None, ge=60, le=86400
    )
    vote_ban_trigger_limit: StrictInt | None = Field(default=None, ge=1, le=1000)
    vote_ban_trigger_window_seconds: StrictInt | None = Field(
        default=None, ge=60, le=604800
    )
    proactive_enabled: StrictBool | None = None
    proactive_task_brief: str | None = Field(default=None, max_length=240)
    mimic_target_user_id: StrictInt | None = Field(default=None, ge=0)
    mimic_target_user_name: str | None = Field(default=None, max_length=80)
    mimic_profile_text: str | None = Field(default=None, max_length=1200)
    api_model_query_enabled: StrictBool | None = None
    api_model_query_base_url: str | None = Field(default=None, max_length=1000)
    api_model_query_http_timeout_sec: StrictFloat | StrictInt | None = Field(
        default=None, ge=1.0, le=300.0
    )
    api_model_query_check_timeout_sec: StrictFloat | StrictInt | None = Field(
        default=None, ge=1.0, le=600.0
    )


class _GroupApiModelQuerySecretChange(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action: Literal["replace", "clear"]
    value: str = Field(default="", max_length=1024)


class _RuleCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    rule_type: Literal["keyword", "regex", "llm"]
    pattern: str = Field(min_length=1, max_length=1000)
    action: Literal["warn", "delete", "ban"] = "warn"
    enabled: StrictBool = True


class _KeywordReplyCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    keyword: str = Field(min_length=1, max_length=255)
    match_type: Literal["contains", "exact", "regex"] = "contains"
    reply_text: str = Field(min_length=1, max_length=4000)
    buttons: list[dict[str, Any]] = Field(default_factory=list, max_length=12)
    pin_message: StrictBool = False
    auto_delete: StrictBool = True
    enabled: StrictBool = True


class _KeywordReplyUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    keyword: str | None = Field(default=None, min_length=1, max_length=255)
    match_type: Literal["contains", "exact", "regex"] | None = None
    reply_text: str | None = Field(default=None, min_length=1, max_length=4000)
    buttons: list[dict[str, Any]] | None = Field(default=None, max_length=12)
    pin_message: StrictBool | None = None
    auto_delete: StrictBool | None = None
    enabled: StrictBool | None = None


class _ScheduledMessageCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    text: str = Field(min_length=1, max_length=4000)
    buttons: list[dict[str, Any]] = Field(default_factory=list, max_length=12)
    schedule_type: Literal["daily", "interval"] = "daily"
    schedule_time: str = Field(default="09:00", max_length=5)
    interval_minutes: StrictInt = Field(default=60, ge=5, le=10080)
    pin_message: StrictBool = False
    unpin_previous: StrictBool = False
    auto_delete: StrictBool = False
    enabled: StrictBool = True


class _ScheduledMessageUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    text: str | None = Field(default=None, min_length=1, max_length=4000)
    buttons: list[dict[str, Any]] | None = Field(default=None, max_length=12)
    schedule_type: Literal["daily", "interval"] | None = None
    schedule_time: str | None = Field(default=None, max_length=5)
    interval_minutes: StrictInt | None = Field(default=None, ge=5, le=10080)
    pin_message: StrictBool | None = None
    unpin_previous: StrictBool | None = None
    auto_delete: StrictBool | None = None
    enabled: StrictBool | None = None


class _RuleUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    rule_type: Literal["keyword", "regex", "llm"] | None = None
    pattern: str | None = Field(default=None, min_length=1, max_length=1000)
    action: Literal["warn", "delete", "ban"] | None = None
    enabled: StrictBool | None = None


class _MemoryCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    content: str = Field(min_length=1, max_length=4000)


class _UserIdCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    user_id: StrictInt = Field(ge=1)


class _AuthorizedGroupCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    group_id: StrictInt
    title: str = Field(default="", max_length=255)


class _AdminCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    user_id: StrictInt
    role: str = Field(default="admin", min_length=1, max_length=32)


class _GlobalBanCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    user_id: StrictInt
    reason: str = Field(default="", max_length=1000)


class _APIError(Exception):
    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message


def _error_response(
    status: int,
    code: str,
    message: str,
    *,
    details: Any | None = None,
) -> web.Response:
    error: dict[str, Any] = {"code": code, "message": message}
    if details is not None:
        error["details"] = details
    return web.json_response(
        {"ok": False, "error": error},
        status=status,
        headers={"Cache-Control": "no-store"},
    )


def _success_response(payload: dict[str, Any]) -> web.Response:
    return web.json_response(
        {"ok": True, **payload},
        headers={"Cache-Control": "no-store"},
    )


def _validation_details(exc: ValidationError) -> list[dict[str, Any]]:
    return json.loads(exc.json(include_url=False))


def _consume_background_task(task: asyncio.Task[Any]) -> None:
    if task.cancelled():
        return
    try:
        task.exception()
    except (asyncio.CancelledError, Exception):
        pass


def _track_json_body_orphan(task: asyncio.Task[Any]) -> None:
    if task.done():
        _consume_background_task(task)
        return
    _JSON_BODY_ORPHANS.add(task)

    def _finished(done: asyncio.Task[Any]) -> None:
        _JSON_BODY_ORPHANS.discard(done)
        _consume_background_task(done)

    task.add_done_callback(_finished)


async def _read_request_json(request: web.Request) -> Any:
    """Read one JSON body with a hard caller-visible deadline.

    ``asyncio.wait_for`` can wait indefinitely for a coroutine that suppresses
    cancellation.  The request handler must still return 408 at the configured
    deadline; a misbehaving body reader is retired separately and bounded.
    """

    if len(_JSON_BODY_TASKS) >= _JSON_BODY_ORPHAN_LIMIT:
        raise _APIError(
            503,
            "request_body_overloaded",
            "请求体读取任务暂时过载，请稍后重试。",
        )
    task = asyncio.create_task(request.json(), name="settings-json-body")
    _JSON_BODY_TASKS.add(task)

    def _retire(done: asyncio.Task[Any]) -> None:
        _JSON_BODY_TASKS.discard(done)
        _JSON_BODY_ORPHANS.discard(done)
        _consume_background_task(done)

    task.add_done_callback(_retire)
    try:
        done, _pending = await asyncio.wait(
            {task},
            timeout=max(0.0, float(_JSON_BODY_TIMEOUT_SECONDS)),
        )
    except asyncio.CancelledError:
        task.cancel()
        done, _pending = await asyncio.wait(
            {task},
            timeout=max(0.0, float(_JSON_BODY_CANCEL_GRACE_SECONDS)),
        )
        if task not in done:
            _track_json_body_orphan(task)
        else:
            _consume_background_task(task)
        raise
    if task in done:
        return task.result()
    task.cancel()
    done, _pending = await asyncio.wait(
        {task},
        timeout=max(0.0, float(_JSON_BODY_CANCEL_GRACE_SECONDS)),
    )
    if task not in done:
        _track_json_body_orphan(task)
    else:
        _consume_background_task(task)
    raise _APIError(408, "request_timeout", "请求体读取超时，请重试。")


async def _json_object(request: web.Request) -> dict[str, Any]:
    try:
        body = await _read_request_json(request)
    except (json.JSONDecodeError, UnicodeDecodeError, ValueError) as exc:
        raise _APIError(400, "invalid_json", "请求体必须是有效的 JSON。") from exc
    if not isinstance(body, dict):
        raise _APIError(400, "invalid_body", "请求体必须是 JSON 对象。")
    return body


def _setting_bool(settings_data: dict[str, Any], key: str) -> bool:
    value = settings_data.get(key)
    if value is None:
        return False
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"1", "true", "yes", "on", "enable", "enabled"}:
            return True
        if normalized in {"0", "false", "no", "off", "disable", "disabled"}:
            return False
    return bool(value)


def _setting_int(settings_data: dict[str, Any], key: str) -> int | None:
    value = settings_data.get(key)
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _public_group_settings(settings_data: dict[str, Any]) -> dict[str, Any]:
    proactive_state = get_cooldown_task_state(settings_data)
    style_state = get_style_state(settings_data)
    api_model_query = get_api_model_query_config(settings_data)
    try:
        welcome_buttons = normalize_template_buttons(
            settings_data.get("welcome_buttons")
        )
    except ValueError:
        welcome_buttons = []
    try:
        default_permissions = (
            normalize_group_permission_config(
                settings_data.get(GROUP_PERMISSIONS_SETTINGS_KEY)
            )
            if settings_data.get(GROUP_PERMISSIONS_SETTINGS_KEY) is not None
            else None
        )
    except ValueError:
        default_permissions = None
    return {
        "av_enabled": _setting_bool(settings_data, "av_enabled"),
        "mute_all_replies": _setting_bool(settings_data, "mute_all_replies"),
        "at_reply_mode": is_at_reply_enabled(settings_data),
        "tts_mode": normalize_tts_mode(settings_data),
        "join_verification_enabled": (
            _setting_bool(settings_data, "join_verification_enabled")
            if settings_data.get("join_verification_enabled") is not None
            else None
        ),
        "join_verification_provider": (
            str(settings_data["join_verification_provider"]).strip().lower()
            if str(settings_data.get("join_verification_provider") or "").strip().lower()
            in {"turnstile", "hcaptcha", "turnstile_hcaptcha"}
            else None
        ),
        "welcome_message": str(settings_data.get("welcome_message") or ""),
        "welcome_buttons": welcome_buttons,
        GROUP_PERMISSIONS_SETTINGS_KEY: default_permissions,
        "patrol_enabled": (
            _setting_bool(settings_data, "patrol_enabled")
            if settings_data.get("patrol_enabled") is not None
            else None
        ),
        "raid_guard_enabled": (
            _setting_bool(settings_data, "raid_guard_enabled")
            if settings_data.get("raid_guard_enabled") is not None
            else None
        ),
        "raid_guard_pin_message": (
            _setting_bool(settings_data, "raid_guard_pin_message")
            if settings_data.get("raid_guard_pin_message") is not None
            else None
        ),
        **{
            key: _setting_int(settings_data, key)
            for key in _RAID_GUARD_GROUP_INT_FIELDS
        },
        "call_admin_enabled": (
            _setting_bool(settings_data, "call_admin_enabled")
            if settings_data.get("call_admin_enabled") is not None
            else None
        ),
        "call_admin_pin_message": (
            _setting_bool(settings_data, "call_admin_pin_message")
            if settings_data.get("call_admin_pin_message") is not None
            else None
        ),
        "call_admin_targets": sorted({
            int(item)
            for item in (settings_data.get("call_admin_targets") or [])
            if isinstance(item, int) and not isinstance(item, bool) and item > 0
        }),
        "vote_ban_enabled": (
            _setting_bool(settings_data, "vote_ban_enabled")
            if settings_data.get("vote_ban_enabled") is not None
            else None
        ),
        "vote_ban_pin_message": (
            _setting_bool(settings_data, "vote_ban_pin_message")
            if settings_data.get("vote_ban_pin_message") is not None
            else None
        ),
        **{
            key: _setting_int(settings_data, key)
            for key in _VOTE_BAN_GROUP_INT_FIELDS
        },
        # None means the group inherits the global default.
        "proactive_enabled": (
            bool(proactive_state.get("enabled"))
            if "enabled" in proactive_state
            else None
        ),
        "proactive_task_brief": str(proactive_state.get("task_brief") or "").strip(),
        "mimic_target_user_id": int(style_state.get("target_user_id") or 0),
        "mimic_target_user_name": str(style_state.get("target_user_name") or ""),
        "mimic_profile_text": str(style_state.get("profile_text") or ""),
        "mimic_sample_count": int(style_state.get("sample_count") or 0),
        "mimic_distilled_at_count": int(style_state.get("distilled_at_count") or 0),
        "api_model_query_enabled": api_model_query.enabled,
        "api_model_query_base_url": api_model_query.base_url,
        "api_model_query_http_timeout_sec": api_model_query.http_timeout_sec,
        "api_model_query_check_timeout_sec": api_model_query.check_timeout_sec,
        "api_model_query_api_key_configured": api_model_query.api_key_configured,
    }


def _group_revision(settings_data: dict[str, Any]) -> str:
    public = _public_group_settings(settings_data)
    editable = {
        key: public.get(key)
        for key in sorted(_GROUP_SETTING_FIELDS)
    }
    # The key itself is never returned, but replacing it must still invalidate
    # an already-open Mini App revision so concurrent saves cannot race.
    editable["_api_model_query_secret_version"] = get_api_model_query_config(
        settings_data
    ).secret_version
    encoded = json.dumps(
        editable,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()[:16]


def _group_document(
    group_id: int,
    title: str | None,
    settings_data: dict[str, Any] | None,
) -> dict[str, Any]:
    stored = settings_data if isinstance(settings_data, dict) else {}
    return {
        "id": int(group_id),
        "title": str(title or ""),
        "settings": _public_group_settings(stored),
        "revision": _group_revision(stored),
    }


def _rule_document(rule: ModerationRule) -> dict[str, Any]:
    return {
        "id": int(rule.id),
        "rule_type": str(rule.rule_type or "keyword"),
        "pattern": str(rule.pattern or ""),
        "action": str(rule.action or "warn"),
        "enabled": bool(rule.enabled),
    }


def _keyword_reply_document(row: KeywordReply) -> dict[str, Any]:
    try:
        buttons = normalize_template_buttons(getattr(row, "buttons", None))
    except ValueError:
        buttons = []
    return {
        "id": int(row.id),
        "keyword": str(row.keyword or ""),
        "match_type": str(row.match_type or "contains"),
        "reply_text": str(row.reply_text or ""),
        "buttons": buttons,
        "pin_message": bool(row.pin_message),
        "auto_delete": bool(row.auto_delete),
        "enabled": bool(row.enabled),
    }


def _scheduled_message_document(row: ScheduledMessage) -> dict[str, Any]:
    try:
        buttons = normalize_template_buttons(getattr(row, "buttons", None))
    except ValueError:
        buttons = []
    return {
        "id": int(row.id),
        "text": str(row.text or ""),
        "buttons": buttons,
        "schedule_type": str(row.schedule_type or "daily"),
        "schedule_time": str(row.schedule_time or "09:00"),
        "interval_minutes": int(row.interval_minutes or 60),
        "pin_message": bool(row.pin_message),
        "unpin_previous": bool(row.unpin_previous),
        "auto_delete": bool(row.auto_delete),
        "enabled": bool(row.enabled),
        "last_run_at": row.last_run_at.isoformat() if row.last_run_at else "",
    }


def _validated_keyword(keyword: str, match_type: str) -> str:
    cleaned = clean_text(keyword, max_len=255).strip()
    if not cleaned:
        raise _APIError(400, "empty_keyword", "关键词不能为空。")
    if match_type == "regex":
        try:
            re.compile(cleaned)
        except re.error as exc:
            raise _APIError(400, "invalid_keyword_regex", f"正则表达式无效：{exc}") from exc
    return cleaned


def _validated_schedule_time(raw: str) -> str:
    parsed = parse_schedule_time(raw)
    if parsed is None:
        raise _APIError(
            400,
            "invalid_schedule_time",
            "发送时间必须是 HH:MM 格式（例如 09:00）。",
        )
    return f"{parsed[0]:02d}:{parsed[1]:02d}"


def _validated_template_buttons(value: object) -> list[dict[str, Any]]:
    try:
        return normalize_template_buttons(value)
    except ValueError as exc:
        raise _APIError(400, "invalid_template_buttons", str(exc)) from exc


def _memory_document(memory: GroupPermanentMemory) -> dict[str, Any]:
    return {
        "id": int(memory.id),
        "content": str(memory.content or ""),
        "created_by": int(memory.created_by or 0),
        "created_at": memory.created_at.isoformat() if memory.created_at else "",
        "updated_at": memory.updated_at.isoformat() if memory.updated_at else "",
    }


def _member_identity(member: GroupMember | None, user_id: int) -> dict[str, Any]:
    full_name = str(getattr(member, "full_name", "") or "").strip()
    username = str(getattr(member, "username", "") or "").strip().lstrip("@")
    identity = member_identity_document(
        user_id,
        full_name=full_name,
        username=username,
    )
    # Keep an explicit human label even when Telegram no longer exposes the
    # profile; the stable ID remains a separate field for operations.
    if not full_name and not username:
        identity["display_name"] = f"用户 {user_id}"
    return identity


def _member_identity_lookup_semaphore() -> asyncio.Semaphore:
    """Return a bounded semaphore tied to the active aiohttp event loop."""
    global _MEMBER_IDENTITY_LOOKUP_LOOP, _MEMBER_IDENTITY_LOOKUP_SEMAPHORE
    loop = asyncio.get_running_loop()
    if (
        _MEMBER_IDENTITY_LOOKUP_LOOP is not loop
        or _MEMBER_IDENTITY_LOOKUP_SEMAPHORE is None
    ):
        # Tests and graceful restarts may create a new event loop in the same
        # process. Never reuse an asyncio primitive or in-flight Task across
        # loops; completed value-cache entries remain safe.
        _MEMBER_IDENTITY_LOOKUP_LOOP = loop
        _MEMBER_IDENTITY_LOOKUP_SEMAPHORE = asyncio.Semaphore(
            _MEMBER_IDENTITY_LOOKUP_CONCURRENCY
        )
        _MEMBER_IDENTITY_INFLIGHT.clear()
    return _MEMBER_IDENTITY_LOOKUP_SEMAPHORE


def _cache_member_identity(
    key: tuple[int, int, int],
    value: tuple[str, str, bool] | None,
) -> None:
    now = time.monotonic()
    ttl = (
        _MEMBER_IDENTITY_CACHE_TTL_SECONDS
        if value is not None
        else _MEMBER_IDENTITY_NEGATIVE_CACHE_TTL_SECONDS
    )
    _MEMBER_IDENTITY_CACHE[key] = (now + ttl, value)
    if len(_MEMBER_IDENTITY_CACHE) <= _MEMBER_IDENTITY_CACHE_MAX_ENTRIES:
        return
    for cached_key, (expires_at, _cached_value) in list(
        _MEMBER_IDENTITY_CACHE.items()
    ):
        if expires_at <= now:
            _MEMBER_IDENTITY_CACHE.pop(cached_key, None)
    while len(_MEMBER_IDENTITY_CACHE) > _MEMBER_IDENTITY_CACHE_MAX_ENTRIES:
        _MEMBER_IDENTITY_CACHE.pop(next(iter(_MEMBER_IDENTITY_CACHE)), None)


def _track_member_identity_orphan(task: asyncio.Task[Any]) -> None:
    global _MEMBER_IDENTITY_CIRCUIT_OPEN_UNTIL
    if task.done():
        _consume_background_task(task)
        return
    _MEMBER_IDENTITY_ORPHANS.add(task)
    if len(_MEMBER_IDENTITY_ORPHANS) >= _MEMBER_IDENTITY_ORPHAN_LIMIT:
        _MEMBER_IDENTITY_CIRCUIT_OPEN_UNTIL = max(
            _MEMBER_IDENTITY_CIRCUIT_OPEN_UNTIL,
            time.monotonic() + _MEMBER_IDENTITY_CIRCUIT_COOLDOWN_SECONDS,
        )
        log.error(
            "settings Telegram lookup circuit opened | orphan_count=%d",
            len(_MEMBER_IDENTITY_ORPHANS),
        )

    def _finished(done: asyncio.Task[Any]) -> None:
        _MEMBER_IDENTITY_ORPHANS.discard(done)
        _consume_background_task(done)

    task.add_done_callback(_finished)


class _TelegramLookupOverloaded(RuntimeError):
    pass


def _member_identity_lookup_capacity_available() -> bool:
    for task in tuple(_MEMBER_IDENTITY_RPC_TASKS):
        if task.done():
            _MEMBER_IDENTITY_RPC_TASKS.discard(task)
            _MEMBER_IDENTITY_ORPHANS.discard(task)
            _consume_background_task(task)
    if time.monotonic() < _MEMBER_IDENTITY_CIRCUIT_OPEN_UNTIL:
        return False
    if len(_MEMBER_IDENTITY_ORPHANS) >= _MEMBER_IDENTITY_ORPHAN_LIMIT:
        return False
    return len(_MEMBER_IDENTITY_RPC_TASKS) < _MEMBER_IDENTITY_RPC_TASK_LIMIT


async def _await_member_identity_lookup(
    operation: Callable[[], Awaitable[Any]],
    *,
    timeout_seconds: float | None = None,
) -> Any:
    """Run a Telegram lookup without releasing its permit before it stops."""

    if not _member_identity_lookup_capacity_available():
        raise _TelegramLookupOverloaded("settings Telegram lookup circuit is open")

    async def _run_with_permit() -> Any:
        # The permit belongs to the real Telegram operation.  If the SDK or a
        # test double suppresses cancellation, it stays held until that child
        # actually exits instead of admitting unbounded replacement calls.
        async with _member_identity_lookup_semaphore():
            return await operation()

    task = asyncio.create_task(_run_with_permit(), name="settings-telegram-lookup")
    _MEMBER_IDENTITY_RPC_TASKS.add(task)

    def _retire_rpc(done: asyncio.Task[Any]) -> None:
        _MEMBER_IDENTITY_RPC_TASKS.discard(done)
        _MEMBER_IDENTITY_ORPHANS.discard(done)
        _consume_background_task(done)

    task.add_done_callback(_retire_rpc)
    timeout = (
        _MEMBER_IDENTITY_LOOKUP_TIMEOUT_SECONDS
        if timeout_seconds is None
        else max(0.0, float(timeout_seconds))
    )
    try:
        done, _pending = await asyncio.wait(
            {task},
            timeout=timeout,
        )
    except asyncio.CancelledError:
        task.cancel()
        done, _pending = await asyncio.wait(
            {task},
            timeout=_MEMBER_IDENTITY_CANCEL_GRACE_SECONDS,
        )
        if task not in done:
            _track_member_identity_orphan(task)
        else:
            _consume_background_task(task)
        raise
    if task in done:
        return task.result()
    task.cancel()
    done, _pending = await asyncio.wait(
        {task},
        timeout=_MEMBER_IDENTITY_CANCEL_GRACE_SECONDS,
    )
    if task not in done:
        _track_member_identity_orphan(task)
    else:
        _consume_background_task(task)
    raise TimeoutError


async def flush_member_identity_tasks(*, timeout_seconds: float = 2.0) -> None:
    """Bound settings-page Telegram lookups before the Bot session closes."""

    deadline = asyncio.get_running_loop().time() + max(
        0.0, float(timeout_seconds)
    )
    pending: set[asyncio.Task[Any]] = set()
    while True:
        tasks = {
            task
            for task in (
                *_MEMBER_IDENTITY_INFLIGHT.values(),
                *_MEMBER_IDENTITY_RPC_TASKS,
                *_MEMBER_IDENTITY_ORPHANS,
                *_JSON_BODY_TASKS,
                *_JSON_BODY_ORPHANS,
            )
            if not task.done()
        }
        if not tasks:
            pending = set()
            break
        for task in tasks:
            task.cancel()
        remaining = max(0.0, deadline - asyncio.get_running_loop().time())
        if remaining <= 0.0:
            pending = tasks
            break
        done, pending = await asyncio.wait(tasks, timeout=remaining)
        for task in done:
            _consume_background_task(task)
        if pending or asyncio.get_running_loop().time() >= deadline:
            break
        # Cancelling an outer identity task can expose its cancellation-
        # resistant RPC child only after the first snapshot was taken.
        await asyncio.sleep(0)
    for task in pending:
        if task in _MEMBER_IDENTITY_RPC_TASKS:
            _track_member_identity_orphan(task)
        elif task in _JSON_BODY_ORPHANS:
            _track_json_body_orphan(task)
    if pending:
        log.error(
            "%d settings API background tasks ignored shutdown cancellation",
            len(pending),
        )


async def _lookup_member_identity(
    bot_obj: Any,
    group_id: int,
    user_id: int,
) -> tuple[str, str, bool] | None:
    """Deduplicate, bound and time-limit Telegram profile lookups."""
    key = (id(bot_obj), int(group_id), int(user_id))
    cached = _MEMBER_IDENTITY_CACHE.get(key)
    if cached is not None:
        if cached[0] > time.monotonic():
            return cached[1]
        _MEMBER_IDENTITY_CACHE.pop(key, None)

    task = _MEMBER_IDENTITY_INFLIGHT.get(key)
    if task is None:
        lookup = getattr(bot_obj, "get_chat_member", None)
        if not callable(lookup):
            return None
        if (
            len(_MEMBER_IDENTITY_INFLIGHT) >= _MEMBER_IDENTITY_RPC_TASK_LIMIT
            or not _member_identity_lookup_capacity_available()
        ):
            log.warning(
                "member identity lookup rejected by circuit | group=%s user=%s",
                group_id,
                user_id,
            )
            return None

        async def _fetch() -> tuple[str, str, bool] | None:
            profile_data: tuple[str, str, bool] | None = None
            try:
                chat_member = await _await_member_identity_lookup(
                    lambda: lookup(int(group_id), int(user_id)),
                )
                profile = getattr(chat_member, "user", None)
                if profile is not None:
                    full_name = str(getattr(profile, "full_name", "") or "")[:255]
                    username = str(getattr(profile, "username", "") or "")[:255]
                    if full_name or username:
                        profile_data = (
                            full_name,
                            username,
                            bool(getattr(profile, "is_bot", False)),
                        )
            except TimeoutError:
                log.debug(
                    "member identity lookup timed out | group=%s user=%s",
                    group_id,
                    user_id,
                )
            except _TelegramLookupOverloaded:
                log.warning(
                    "member identity lookup circuit open | group=%s user=%s",
                    group_id,
                    user_id,
                )
                return None
            except Exception:
                log.debug(
                    "member identity lookup failed | group=%s user=%s",
                    group_id,
                    user_id,
                    exc_info=True,
                )
            _cache_member_identity(key, profile_data)
            return profile_data

        task = asyncio.create_task(
            _fetch(),
            name=f"member-identity:{int(group_id)}:{int(user_id)}",
        )
        _MEMBER_IDENTITY_INFLIGHT[key] = task

        def _retire(done: asyncio.Task[tuple[str, str, bool] | None]) -> None:
            if _MEMBER_IDENTITY_INFLIGHT.get(key) is done:
                _MEMBER_IDENTITY_INFLIGHT.pop(key, None)

        task.add_done_callback(_retire)
    return await asyncio.shield(task)


async def _group_member_map(
    session_factory: async_sessionmaker[AsyncSession],
    group_id: int,
    user_ids: list[int],
    *,
    bot_obj: Any | None = None,
) -> dict[int, GroupMember]:
    ids = sorted({int(user_id) for user_id in user_ids if int(user_id) > 0})
    if not ids:
        return {}
    # Take a short DB snapshot first.  Live Telegram lookups can take seconds
    # and must never hold one of the application's scarce DB connections.
    async with session_factory() as session:
        rows = (
            await session.scalars(
                select(GroupMember).where(
                    GroupMember.group_id == int(group_id),
                    GroupMember.user_id.in_(ids),
                )
            )
        ).all()
    members = {int(row.user_id): row for row in rows}
    if bot_obj is None or not callable(getattr(bot_obj, "get_chat_member", None)):
        return members
    missing_ids = [
        user_id
        for user_id in ids
        if user_id not in members
        or not (
            str(getattr(members[user_id], "full_name", "") or "").strip()
            or str(getattr(members[user_id], "username", "") or "").strip()
        )
    ]
    if not missing_ids:
        return members

    # A policy table may contain thousands of historical users.  Only enrich a
    # small page per request and keep one absolute budget for the whole batch.
    lookup_ids = missing_ids[: max(0, int(_MEMBER_IDENTITY_REQUEST_LOOKUP_LIMIT))]
    lookup_tasks = {
        user_id: asyncio.create_task(
            _lookup_member_identity(bot_obj, int(group_id), user_id),
            name=f"settings-member-page:{int(group_id)}:{user_id}",
        )
        for user_id in lookup_ids
    }
    done: set[asyncio.Task[tuple[str, str, bool] | None]] = set()
    pending: set[asyncio.Task[tuple[str, str, bool] | None]] = set()
    if lookup_tasks:
        try:
            done, pending = await asyncio.wait(
                set(lookup_tasks.values()),
                timeout=max(0.0, float(_MEMBER_IDENTITY_REQUEST_BUDGET_SECONDS)),
            )
        except asyncio.CancelledError:
            for task in lookup_tasks.values():
                task.cancel()
            await asyncio.gather(*lookup_tasks.values(), return_exceptions=True)
            raise
    for task in pending:
        task.cancel()
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)
    profiles_by_id: dict[int, tuple[str, str, bool]] = {}
    for user_id, task in lookup_tasks.items():
        if task not in done or task.cancelled():
            continue
        try:
            profile_data = task.result()
        except Exception:
            continue
        if profile_data is not None:
            profiles_by_id[user_id] = profile_data
    if not profiles_by_id:
        return members

    # Open a fresh, short write transaction only after every network wait has
    # ended.  Re-read rows to merge safely with the roster middleware.
    async with session_factory() as session:
        current_rows = (
            await session.scalars(
                select(GroupMember).where(
                    GroupMember.group_id == int(group_id),
                    GroupMember.user_id.in_(ids),
                )
            )
        ).all()
        members = {int(row.user_id): row for row in current_rows}
        changed = False
        for user_id, profile_data in profiles_by_id.items():
            full_name, username, is_bot = profile_data
            row = members.get(user_id)
            if row is None:
                row = GroupMember(
                    group_id=int(group_id),
                    user_id=int(user_id),
                    full_name=full_name,
                    username=username,
                    is_bot=is_bot,
                    left=False,
                )
                session.add(row)
                members[user_id] = row
                changed = True
                continue
            if full_name and not str(row.full_name or "").strip():
                row.full_name = full_name
                changed = True
            if username and not str(row.username or "").strip():
                row.username = username
                changed = True
            if bool(row.is_bot) != is_bot:
                row.is_bot = is_bot
                changed = True
        if not changed:
            return members
        try:
            await session.commit()
        except Exception:
            # Roster middleware or another settings request may have inserted
            # the same member concurrently. Identity display is best-effort;
            # a conflict must not fail the whole policy-list response.
            await session.rollback()
            current_rows = (
                await session.scalars(
                    select(GroupMember).where(
                        GroupMember.group_id == int(group_id),
                        GroupMember.user_id.in_(ids),
                    )
                )
            ).all()
            members = {int(row.user_id): row for row in current_rows}
        return members


def _user_policy_document(
    row: Any,
    member: GroupMember | None = None,
) -> dict[str, Any]:
    user_id = int(row.user_id)
    payload = {"user_id": user_id, **_member_identity(member, user_id)}
    if isinstance(row, UserWarning):
        payload.update({"count": int(row.count or 0), "is_banned": bool(row.is_banned)})
    if hasattr(row, "created_by"):
        payload["created_by"] = int(row.created_by or 0)
    if hasattr(row, "created_at"):
        payload["created_at"] = row.created_at.isoformat() if row.created_at else ""
    return payload


def _ban_document(
    row: UserWarning,
    member: GroupMember | None = None,
) -> dict[str, Any]:
    """Expose group-ban state without leaking global moderation thresholds."""
    user_id = int(row.user_id)
    return {
        "user_id": user_id,
        "is_banned": bool(row.is_banned),
        **_member_identity(member, user_id),
    }


async def _set_group_banned_after_telegram(
    session: AsyncSession,
    *,
    group_id: int,
    user_id: int,
) -> UserWarning:
    """Upsert only the ban flag, preserving concurrent warning increments."""
    updated = await session.execute(
        update(UserWarning)
        .where(
            UserWarning.group_id == int(group_id),
            UserWarning.user_id == int(user_id),
        )
        .values(is_banned=True)
    )
    if int(updated.rowcount or 0) == 0:
        try:
            async with session.begin_nested():
                session.add(
                    UserWarning(
                        group_id=int(group_id),
                        user_id=int(user_id),
                        count=0,
                        is_banned=True,
                    )
                )
                await session.flush()
        except IntegrityError:
            updated = await session.execute(
                update(UserWarning)
                .where(
                    UserWarning.group_id == int(group_id),
                    UserWarning.user_id == int(user_id),
                )
                .values(is_banned=True)
            )
            if int(updated.rowcount or 0) != 1:
                raise
    row = await session.scalar(
        select(UserWarning).where(
            UserWarning.group_id == int(group_id),
            UserWarning.user_id == int(user_id),
        )
    )
    if row is None:
        raise RuntimeError("group ban row was not persisted")
    return row


def _set_proactive_task_brief(
    settings_data: dict[str, Any],
    task_brief: str,
) -> dict[str, Any]:
    updated = dict(settings_data)
    raw_tasks = updated.get(_SCHEDULED_TASKS_KEY)
    tasks = dict(raw_tasks) if isinstance(raw_tasks, dict) else {}
    raw_state = tasks.get(_COOLDOWN_TASK_KEY)
    state = dict(raw_state) if isinstance(raw_state, dict) else {}
    if task_brief:
        state["task_brief"] = task_brief
    else:
        state.pop("task_brief", None)
    tasks[_COOLDOWN_TASK_KEY] = state
    updated[_SCHEDULED_TASKS_KEY] = tasks
    return updated


def _apply_group_settings(
    stored_settings: dict[str, Any] | None,
    update: _GroupSettingsUpdate,
    settings: Settings,
) -> dict[str, Any]:
    updated = dict(stored_settings or {})
    fields = update.model_fields_set
    null_fields = sorted(
        name
        for name in fields
        if name not in {
            "proactive_enabled",
            "join_verification_enabled",
            "join_verification_provider",
            "welcome_message",
            "welcome_buttons",
            GROUP_PERMISSIONS_SETTINGS_KEY,
            "patrol_enabled",
            "raid_guard_enabled",
            "raid_guard_pin_message",
            *_RAID_GUARD_GROUP_INT_FIELDS,
            "call_admin_enabled",
            "call_admin_pin_message",
            "call_admin_targets",
            "vote_ban_enabled",
            "vote_ban_pin_message",
            *_VOTE_BAN_GROUP_INT_FIELDS,
        }
        and getattr(update, name) is None
    )
    if null_fields:
        raise _APIError(
            400,
            "invalid_group_settings",
            f"群设置不能为 null：{', '.join(null_fields)}",
        )

    if "av_enabled" in fields:
        updated["av_enabled"] = bool(update.av_enabled)
    if "mute_all_replies" in fields:
        if update.mute_all_replies:
            updated["mute_all_replies"] = True
        else:
            updated.pop("mute_all_replies", None)
    if "at_reply_mode" in fields:
        updated = set_at_reply_enabled(updated, bool(update.at_reply_mode))
    if "tts_mode" in fields:
        updated = set_tts_mode(updated, str(update.tts_mode))
    if "join_verification_enabled" in fields:
        if update.join_verification_enabled is None:
            updated.pop("join_verification_enabled", None)
        else:
            updated["join_verification_enabled"] = bool(update.join_verification_enabled)
    if "join_verification_provider" in fields:
        if update.join_verification_provider is None:
            updated.pop("join_verification_provider", None)
        else:
            updated["join_verification_provider"] = str(
                update.join_verification_provider
            )
    if "welcome_message" in fields:
        welcome_text = clean_multiline_text(
            str(update.welcome_message or ""), max_len=4000
        ).strip()
        if welcome_text:
            updated["welcome_message"] = welcome_text
        else:
            updated.pop("welcome_message", None)
    if "welcome_buttons" in fields:
        welcome_buttons = _validated_template_buttons(update.welcome_buttons or [])
        if welcome_buttons:
            updated["welcome_buttons"] = welcome_buttons
        else:
            updated.pop("welcome_buttons", None)
    if GROUP_PERMISSIONS_SETTINGS_KEY in fields:
        if update.default_permissions is None:
            updated.pop(GROUP_PERMISSIONS_SETTINGS_KEY, None)
        else:
            try:
                updated[GROUP_PERMISSIONS_SETTINGS_KEY] = (
                    normalize_group_permission_config(update.default_permissions)
                )
            except ValueError as exc:
                raise _APIError(
                    400,
                    "invalid_default_permissions",
                    str(exc),
                ) from exc
    if fields & {"join_verification_enabled", "join_verification_provider"}:
        verification_enabled, provider = join_verification_policy(settings, updated)
        provider_selected = (
            "join_verification_provider" in fields
            and update.join_verification_provider is not None
        )
        if (
            verification_enabled or provider_selected
        ) and not turnstile_verification_configured(settings, provider):
            raise _APIError(
                400,
                "verification_provider_unavailable",
                "该验证服务尚未由最高管理员完成配置，暂时不能为本群启用。",
            )
    if "patrol_enabled" in fields:
        if update.patrol_enabled is None:
            updated.pop("patrol_enabled", None)
        else:
            if update.patrol_enabled and not verification_service_ready(
                settings, verification_provider(settings)
            ):
                raise _APIError(
                    400,
                    "verification_provider_unavailable",
                    "真人质询验证服务未配置，暂时不能启用巡检。",
                )
            updated["patrol_enabled"] = bool(update.patrol_enabled)
    if "raid_guard_enabled" in fields:
        if update.raid_guard_enabled is None:
            updated.pop("raid_guard_enabled", None)
        else:
            if update.raid_guard_enabled and not verification_service_ready(
                settings, verification_provider(settings)
            ):
                raise _APIError(
                    400,
                    "verification_provider_unavailable",
                    "真人质询验证服务未配置，暂时不能启用爆破防护。",
                )
            updated["raid_guard_enabled"] = bool(update.raid_guard_enabled)
    if "raid_guard_pin_message" in fields:
        if update.raid_guard_pin_message is None:
            updated.pop("raid_guard_pin_message", None)
        else:
            updated["raid_guard_pin_message"] = bool(update.raid_guard_pin_message)
    for key in _RAID_GUARD_GROUP_INT_FIELDS:
        if key not in fields:
            continue
        value = getattr(update, key)
        if value is None:
            updated.pop(key, None)
        else:
            updated[key] = int(value)
    if "call_admin_enabled" in fields:
        if update.call_admin_enabled is None:
            updated.pop("call_admin_enabled", None)
        else:
            updated["call_admin_enabled"] = bool(update.call_admin_enabled)
    if "call_admin_pin_message" in fields:
        if update.call_admin_pin_message is None:
            updated.pop("call_admin_pin_message", None)
        else:
            updated["call_admin_pin_message"] = bool(update.call_admin_pin_message)
    if "call_admin_targets" in fields:
        targets = sorted(
            {int(item) for item in (update.call_admin_targets or []) if int(item) > 0}
        )
        # An empty selection means "mention all admins" (the default), so it
        # is not persisted.
        if targets:
            updated["call_admin_targets"] = targets
        else:
            updated.pop("call_admin_targets", None)
    if "vote_ban_enabled" in fields:
        if update.vote_ban_enabled is None:
            updated.pop("vote_ban_enabled", None)
        else:
            updated["vote_ban_enabled"] = bool(update.vote_ban_enabled)
    if "vote_ban_pin_message" in fields:
        if update.vote_ban_pin_message is None:
            updated.pop("vote_ban_pin_message", None)
        else:
            updated["vote_ban_pin_message"] = bool(update.vote_ban_pin_message)
    for key in _VOTE_BAN_GROUP_INT_FIELDS:
        if key not in fields:
            continue
        value = getattr(update, key)
        if value is None:
            updated.pop(key, None)
        else:
            updated[key] = int(value)
    if "proactive_enabled" in fields:
        inherited = update.proactive_enabled is None
        updated = set_cooldown_task_enabled(
            updated,
            enabled=(
                bool(settings.bot.proactive_default_enabled)
                if inherited
                else bool(update.proactive_enabled)
            ),
            config=settings.bot,
        )
        if inherited:
            tasks = dict(updated.get(_SCHEDULED_TASKS_KEY) or {})
            state = dict(tasks.get(_COOLDOWN_TASK_KEY) or {})
            state.pop("enabled", None)
            tasks[_COOLDOWN_TASK_KEY] = state
            updated[_SCHEDULED_TASKS_KEY] = tasks
    if "proactive_task_brief" in fields:
        task_brief = str(update.proactive_task_brief or "").strip()
        updated = _set_proactive_task_brief(updated, task_brief)

    api_model_query_fields = {
        "api_model_query_enabled",
        "api_model_query_base_url",
        "api_model_query_http_timeout_sec",
        "api_model_query_check_timeout_sec",
    }
    if fields & api_model_query_fields:
        api_config = get_api_model_query_config(updated)
        try:
            if "api_model_query_enabled" in fields:
                api_config = replace(
                    api_config, enabled=bool(update.api_model_query_enabled)
                )
            if "api_model_query_base_url" in fields:
                api_config = replace(
                    api_config,
                    base_url=normalize_api_model_query_base_url(
                        update.api_model_query_base_url
                    ),
                )
            if "api_model_query_http_timeout_sec" in fields:
                api_config = replace(
                    api_config,
                    http_timeout_sec=float(update.api_model_query_http_timeout_sec),
                )
            if "api_model_query_check_timeout_sec" in fields:
                api_config = replace(
                    api_config,
                    check_timeout_sec=float(update.api_model_query_check_timeout_sec),
                )
            updated = set_api_model_query_config(updated, api_config)
        except (TypeError, ValueError) as exc:
            raise _APIError(
                400,
                "invalid_api_model_query_settings",
                str(exc) or "模型 API 查询配置无效。",
            ) from exc

    style_fields = {
        "mimic_target_user_id",
        "mimic_target_user_name",
        "mimic_profile_text",
    }
    if fields & style_fields:
        state = get_style_state(updated)
        previous_target = int(state.get("target_user_id") or 0)
        target_id = (
            int(update.mimic_target_user_id or 0)
            if "mimic_target_user_id" in fields
            else previous_target
        )
        target_changed = target_id != previous_target
        target_name = (
            clean_text(str(update.mimic_target_user_name or ""), max_len=80)
            if "mimic_target_user_name" in fields
            else "" if target_changed else str(state.get("target_user_name") or "")
        )
        if target_changed:
            updated = set_style_target(
                updated,
                user_id=target_id,
                user_name=target_name,
            )
            state = get_style_state(updated)
        else:
            state["target_user_name"] = target_name
        if "mimic_profile_text" in fields:
            state["profile_text"] = clean_multiline_text(
                str(update.mimic_profile_text or ""),
                max_len=1200,
            ).strip()
        updated = dict(updated)
        updated[STYLE_SETTINGS_KEY] = state
    return updated


def register_settings_routes(
    app: web.Application,
    *,
    bot: Any,
    bot_token: str,
    settings: Settings,
    manager: RuntimeConfigManager,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Register super-admin-only runtime and per-group settings routes."""

    Handler = Callable[[web.Request, Any], Awaitable[web.StreamResponse]]

    def authenticated(
        handler: Handler,
    ) -> Callable[[web.Request], Awaitable[web.StreamResponse]]:
        @wraps(handler)
        async def wrapped(request: web.Request) -> web.StreamResponse:
            try:
                user = await require_super_admin(
                    request,
                    bot_token=bot_token,
                    super_admin_id=settings.super_admin_id,
                )
                with privileged_request_scope():
                    return await handler(request, user)
            except web.HTTPException:
                raise
            except _APIError as exc:
                return _error_response(exc.status, exc.code, exc.message)
            except ValidationError as exc:
                return _error_response(
                    400,
                    "validation_error",
                    "配置校验失败。",
                    details=_validation_details(exc),
                )
            except RuntimeConfigConflictError as exc:
                return _error_response(
                    409,
                    "revision_conflict",
                    str(exc) or "配置已被其他会话更新，请刷新后重试。",
                )
            except Exception:
                log.exception("Mini App settings API request failed")
                return _error_response(500, "internal_error", "服务器处理请求失败。")

        return wrapped

    def any_admin(
        handler: Handler,
    ) -> Callable[[web.Request], Awaitable[web.StreamResponse]]:
        @wraps(handler)
        async def wrapped(request: web.Request) -> web.StreamResponse:
            try:
                user = await require_authenticated_user(request, bot_token=bot_token)
                with privileged_request_scope():
                    return await handler(request, user)
            except web.HTTPException:
                raise
            except _APIError as exc:
                return _error_response(exc.status, exc.code, exc.message)
            except ValidationError as exc:
                return _error_response(
                    400,
                    "validation_error",
                    "配置校验失败。",
                    details=_validation_details(exc),
                )
            except RuntimeConfigEncryptionError as exc:
                return _error_response(400, "secret_storage_unavailable", str(exc))
            except Exception:
                log.exception("Mini App group API request failed")
                return _error_response(500, "internal_error", "服务器处理请求失败。")

        return wrapped

    async def _allowed_group_ids(user_id: int) -> set[int] | None:
        if settings.super_admin_id and int(user_id) == int(settings.super_admin_id):
            return None
        stmt = (
            select(Admin.group_id)
            .join(AuthorizedGroup, AuthorizedGroup.group_id == Admin.group_id)
            .where(
                Admin.user_id == int(user_id),
                AuthorizedGroup.bot_present.is_(True),
            )
        )
        async with session_factory() as session:
            return {int(value) for value in (await session.scalars(stmt)).all()}

    async def _require_group_access(group_id: int, user_id: int) -> None:
        allowed = await _allowed_group_ids(user_id)
        if allowed is not None and group_id not in allowed:
            raise _APIError(403, "group_access_denied", "你没有该群的管理权限。")

    async def _ensure_group_row(session: AsyncSession, group_id: int) -> Group:
        row = await session.get(Group, group_id)
        if row is None:
            row = Group(id=group_id, title="", settings={})
            session.add(row)
            await session.flush()
        return row

    @any_admin
    async def get_session(_request: web.Request, user: Any) -> web.Response:
        user_id = int(user.id)
        is_super = bool(
            settings.super_admin_id and user_id == int(settings.super_admin_id)
        )
        allowed = await _allowed_group_ids(user_id)
        group_ids = [] if allowed is None else sorted(allowed)
        if not is_super and not group_ids:
            raise _APIError(403, "admin_required", "你没有任何已授权群的管理权限。")
        return _success_response(
            {
                "session": {
                    "user_id": user_id,
                    "role": "super_admin" if is_super else "group_admin",
                    "can_manage_global": is_super,
                    "group_ids": group_ids,
                }
            }
        )

    @authenticated
    async def get_settings(_request: web.Request, _user: Any) -> web.Response:
        return _success_response(manager.api_document())

    @authenticated
    async def put_settings(request: web.Request, user: Any) -> web.Response:
        body = await _json_object(request)
        update = _RuntimeSettingsUpdate.model_validate(body)
        try:
            await manager.save(
                update.config,
                expected_revision=update.revision,
                updated_by=int(user.id),
                secret_changes=update.secret_changes,
            )
        except ValidationError:
            raise
        except RuntimeConfigConflictError:
            raise
        except RuntimeConfigEncryptionError as exc:
            raise _APIError(400, "secret_storage_unavailable", str(exc)) from exc
        except ValueError as exc:
            raise _APIError(400, "invalid_settings", str(exc)) from exc
        return _success_response(manager.api_document())

    @authenticated
    async def list_authorized_groups_api(_request: web.Request, _user: Any) -> web.Response:
        async with session_factory() as session:
            rows = (await session.execute(
                select(AuthorizedGroup, Group.title)
                .outerjoin(Group, Group.id == AuthorizedGroup.group_id)
                .order_by(AuthorizedGroup.created_at.desc())
            )).all()
        return _success_response({
            "authorized_groups": [
                {
                    "group_id": int(row.group_id),
                    "title": str(title or ""),
                    "authorized_by": int(row.authorized_by or 0),
                    "bot_present": bool(row.bot_present),
                    "created_at": row.created_at.isoformat() if row.created_at else "",
                }
                for row, title in rows
            ]
        })

    @authenticated
    async def create_authorized_group_api(request: web.Request, user: Any) -> web.Response:
        body = _AuthorizedGroupCreate.model_validate(await _json_object(request))
        async with session_factory() as session:
            row = await session.get(AuthorizedGroup, body.group_id)
            if row is None:
                row = AuthorizedGroup(group_id=body.group_id, authorized_by=int(user.id))
                session.add(row)
            elif not bool(row.bot_present):
                row.bot_present = True
                row.authorized_by = int(user.id)
            group = await session.get(Group, body.group_id)
            if group is None:
                session.add(Group(id=body.group_id, title=clean_text(body.title, max_len=255), settings={}))
            elif body.title:
                group.title = clean_text(body.title, max_len=255)
            await session.commit()
        return _success_response({"created": True})

    @authenticated
    async def delete_authorized_group_api(request: web.Request, _user: Any) -> web.Response:
        group_id = int(request.match_info["id"])
        async with session_factory() as session:
            row = await session.get(AuthorizedGroup, group_id)
            if row is not None:
                await session.delete(row)
            await session.execute(delete(Admin).where(Admin.group_id == group_id))
            await session.commit()
        clear_privileged_operator_group(group_id)
        return _success_response({"deleted": row is not None})

    @any_admin
    async def list_group_admins_api(request: web.Request, user: Any) -> web.Response:
        # Group admins may read this list too: the Mini App call-admin picker
        # renders it for every group they manage. Mutations stay super-admin.
        group_id = int(request.match_info["id"])
        await _require_group_access(group_id, int(user.id))
        async with session_factory() as session:
            rows = (await session.scalars(
                select(Admin)
                .where(Admin.group_id == group_id)
                .order_by(Admin.id)
            )).all()
        members = await _group_member_map(
            session_factory,
            group_id,
            [int(row.user_id) for row in rows],
            bot_obj=bot,
        )
        admins = [
            {
                "user_id": int(row.user_id),
                "role": str(row.role or "admin"),
                "display_name": _member_identity(
                    members.get(int(row.user_id)),
                    int(row.user_id),
                )["display_name"],
            }
            for row in rows
        ]
        return _success_response({"admins": admins})

    @any_admin
    async def list_telegram_admins_api(request: web.Request, user: Any) -> web.Response:
        """Live Telegram admins for the call-admin target picker.

        Falls back to the locally authorized admin list (with roster display
        names) when the Telegram lookup is unavailable.
        """
        group_id = int(request.match_info["id"])
        await _require_group_access(group_id, int(user.id))
        admins: list[dict[str, Any]] = []
        get_admins = getattr(bot, "get_chat_administrators", None)
        if callable(get_admins):
            try:
                members = await _await_member_identity_lookup(
                    lambda: get_admins(group_id),
                    timeout_seconds=5.0,
                )
                for member in members or []:
                    member_user = getattr(member, "user", None)
                    if member_user is None or getattr(member_user, "is_bot", False):
                        continue
                    user_id = int(member_user.id)
                    full_name = str(getattr(member_user, "full_name", "") or "").strip()
                    username = str(getattr(member_user, "username", "") or "").strip()
                    admins.append({
                        "user_id": user_id,
                        "display_name": full_name or (f"@{username}" if username else str(user_id)),
                        "username": username,
                    })
            except Exception:
                log.info("telegram admin list failed | group=%s", group_id)
                admins = []
        if not admins:
            async with session_factory() as session:
                rows = (await session.execute(
                    select(Admin.user_id, GroupMember.full_name, GroupMember.username)
                    .outerjoin(
                        GroupMember,
                        (GroupMember.group_id == Admin.group_id)
                        & (GroupMember.user_id == Admin.user_id),
                    )
                    .where(Admin.group_id == group_id)
                    .order_by(Admin.id)
                )).all()
            for user_id, full_name, username in rows:
                clean_username = str(username or "").strip()
                display = str(full_name or "").strip() or (
                    f"@{clean_username}" if clean_username else str(int(user_id))
                )
                admins.append({
                    "user_id": int(user_id),
                    "display_name": display,
                    "username": clean_username,
                })
        return _success_response({"admins": admins})

    @authenticated
    async def create_group_admin_api(request: web.Request, _user: Any) -> web.Response:
        group_id = int(request.match_info["id"])
        body = _AdminCreate.model_validate(await _json_object(request))
        async with session_factory() as session:
            authorized = await session.get(AuthorizedGroup, group_id)
            if authorized is None or not bool(authorized.bot_present):
                raise _APIError(404, "group_not_found", "该群尚未授权。")
            row = await session.scalar(select(Admin).where(
                Admin.group_id == group_id,
                Admin.user_id == body.user_id,
            ))
            if row is None:
                row = Admin(group_id=group_id, user_id=body.user_id, role=body.role)
                session.add(row)
            else:
                row.role = body.role
            try:
                await session.commit()
            except IntegrityError as exc:
                await session.rollback()
                authorized = await session.get(AuthorizedGroup, group_id)
                if authorized is None or not bool(authorized.bot_present):
                    raise _APIError(
                        404,
                        "group_not_found",
                        "该群授权状态刚刚发生变化。",
                    ) from exc
                raise _APIError(
                    409,
                    "admin_conflict",
                    "管理员授权发生并发冲突，请重试。",
                ) from exc
        mark_privileged_operator(body.user_id, group_id=group_id)
        return _success_response({"created": True})

    @authenticated
    async def delete_group_admin_api(request: web.Request, _user: Any) -> web.Response:
        group_id = int(request.match_info["id"])
        user_id = int(request.match_info["user_id"])
        async with session_factory() as session:
            row = await session.scalar(select(Admin).where(
                Admin.group_id == group_id,
                Admin.user_id == user_id,
            ))
            if row is not None:
                await session.delete(row)
            await session.commit()
        unmark_privileged_operator(user_id, group_id=group_id)
        return _success_response({"deleted": row is not None})

    @authenticated
    async def list_global_bans_api(request: web.Request, _user: Any) -> web.Response:
        try:
            limit = min(500, max(1, int(request.query.get("limit", "100"))))
            offset = max(0, int(request.query.get("offset", "0")))
        except (TypeError, ValueError) as exc:
            raise _APIError(400, "invalid_pagination", "分页参数无效。") from exc
        async with session_factory() as session:
            rows = await list_global_bans(session, limit=limit, offset=offset)
        return _success_response({"global_bans": [
            {
                "user_id": int(row.user_id),
                "reason": str(row.reason or ""),
                "source": str(row.source or "manual"),
                "created_by": int(row.created_by or 0),
                "created_at": row.created_at.isoformat() if row.created_at else "",
            }
            for row in rows
        ], "next_offset": offset + len(rows) if len(rows) == limit else None})

    @authenticated
    async def create_global_ban_api(request: web.Request, user: Any) -> web.Response:
        body = _GlobalBanCreate.model_validate(await _json_object(request))
        if settings.super_admin_id and body.user_id == int(settings.super_admin_id):
            raise _APIError(400, "cannot_ban_owner", "不能封禁最高管理员。")
        telegram_ban_member = getattr(bot, "ban_chat_member", None)
        if not callable(telegram_ban_member):
            raise _APIError(503, "telegram_unavailable", "当前无法调用 Telegram 封禁接口。")
        previous_warning: tuple[int, bool] | None = None
        async with session_factory() as session:
            authorization_rows = (
                await session.execute(
                    select(
                        AuthorizedGroup.group_id,
                        AuthorizedGroup.bot_present,
                    )
                )
            ).all()
            journal_group_ids = [int(group_id) for group_id, _ in authorization_rows]
            group_ids = [
                int(group_id)
                for group_id, bot_present in authorization_rows
                if bool(bot_present)
            ]
            active_group_set = set(group_ids)
            recoveries = await lease_join_verifications_for_user_unban(
                session,
                body.user_id,
                group_ids=journal_group_ids,
                manual_unban=False,
            )
            await add_global_ban(
                session,
                body.user_id,
                reason=clean_multiline_text(body.reason, max_len=1000).strip() or "手动封禁",
                source="manual",
                created_by=int(user.id),
            )
            prompts = tuple(
                (item.group_id, item.prompt_message_id)
                for item in recoveries
                if item.prompt_message_id > 0
                and int(item.group_id) in active_group_set
            )
            private_prompts = tuple(
                (item.user_id, item.private_message_id)
                for item in recoveries
                if item.private_message_id > 0
            )
            await session.commit()
        recovery_by_group = {int(item.group_id): item for item in recoveries}

        async def complete_recovery(group_id: int) -> bool:
            recovery = recovery_by_group.get(int(group_id))
            if recovery is None:
                return True
            async with session_factory() as completion_session:
                completed = await complete_leased_join_verification(
                    completion_session,
                    verification_id=int(recovery.verification_id),
                    lease_until=recovery.lease_until,
                    status="unbanning",
                )
                if completed:
                    await completion_session.commit()
                    return True
                await completion_session.rollback()
                return False

        async def ban_one(group_id: int) -> bool:
            async with session_factory() as policy_session:
                if not await is_globally_banned(policy_session, body.user_id):
                    if await enforce_unban_member(bot, group_id, body.user_id):
                        await restore_member_permissions(bot, group_id, body.user_id)
                        await complete_recovery(group_id)
                    return False
            enforced = await enforce_ban_member(bot, group_id, body.user_id)
            if not enforced:
                return False
            async with session_factory() as policy_session:
                still_banned = await is_globally_banned(
                    policy_session,
                    body.user_id,
                )
            if still_banned:
                await complete_recovery(group_id)
                return True
            if await enforce_unban_member(bot, group_id, body.user_id):
                await restore_member_permissions(bot, group_id, body.user_id)
                await complete_recovery(group_id)
            return False

        outcomes = await _run_privileged_group_fanout(group_ids, ban_one)
        await delete_verification_prompts(bot, prompts)
        await close_private_challenge_messages(bot, private_prompts)
        succeeded = sum(outcomes)
        failures = len(outcomes) - succeeded
        if failures:
            raise _APIError(
                502,
                "telegram_ban_partial",
                f"已处理 {succeeded}/{len(group_ids)} 个群，但仍有群封禁失败，请重试。",
            )
        return _success_response({
            "banned_groups": succeeded,
            "total_groups": len(group_ids),
            "deferred_groups": max(0, len(journal_group_ids) - len(group_ids)),
        })

    @authenticated
    async def delete_global_ban_api(request: web.Request, user: Any) -> web.Response:
        target_id = _path_int(request, "user_id")
        unban_member = getattr(bot, "unban_chat_member", None)
        if not callable(unban_member):
            raise _APIError(503, "telegram_unavailable", "当前无法调用 Telegram 解封接口。")
        async with session_factory() as session:
            authorization_rows = (
                await session.execute(
                    select(
                        AuthorizedGroup.group_id,
                        AuthorizedGroup.bot_present,
                    )
                )
            ).all()
            journal_group_ids = [int(group_id) for group_id, _ in authorization_rows]
            group_ids = [
                int(group_id)
                for group_id, bot_present in authorization_rows
                if bool(bot_present)
            ]
            active_group_set = set(group_ids)
            recoveries = await lease_join_verifications_for_user_unban(
                session,
                target_id,
                group_ids=journal_group_ids,
            )
            removed = await remove_global_ban(
                session,
                target_id,
                operator_id=int(user.id),
            )
            await session.execute(
                delete(UserWarning).where(
                    UserWarning.user_id == target_id,
                    UserWarning.group_id.in_(journal_group_ids),
                )
            )
            await session.commit()
        activate_manual_unban_recoveries(recoveries)
        prompts = tuple(
            (item.group_id, item.prompt_message_id)
            for item in recoveries
            if item.prompt_message_id > 0
            and int(item.group_id) in active_group_set
        )
        private_prompts = tuple(
            (item.user_id, item.private_message_id)
            for item in recoveries
            if item.private_message_id > 0
        )
        recovery_by_group = {int(item.group_id): item for item in recoveries}
        restored_group_ids: set[int] = set()

        async def unban_one(group_id: int) -> bool:
            async def preserve_ban() -> bool:
                async with session_factory() as session:
                    return await verification_release_blocked_by_ban(
                        session,
                        group_id=group_id,
                        user_id=target_id,
                    )

            if not await enforce_unban_member(
                bot,
                group_id,
                target_id,
                preserve_ban=preserve_ban,
            ):
                return False
            if not await restore_member_permissions(bot, group_id, target_id):
                # Keep the recovery journal for the verification sweeper.
                return True
            restored_group_ids.add(int(group_id))
            recovery = recovery_by_group.get(int(group_id))
            if recovery is not None:
                async with session_factory() as completion_session:
                    completed = await complete_leased_join_verification(
                        completion_session,
                        verification_id=int(recovery.verification_id),
                        lease_until=recovery.lease_until,
                        status="unbanning",
                    )
                    if completed:
                        await completion_session.commit()
                    else:
                        await completion_session.rollback()
            return True

        outcomes = await _run_privileged_group_fanout(group_ids, unban_one)
        await delete_verification_prompts(bot, prompts)
        await close_private_challenge_messages(bot, private_prompts)
        succeeded = sum(outcomes)
        restored = len(restored_group_ids)
        failures = (len(outcomes) - succeeded) + (succeeded - restored)
        if failures:
            raise _APIError(
                502,
                "telegram_unban_partial",
                f"已处理 {succeeded}/{len(group_ids)} 个群，但仍有群解封失败，请重试。",
            )
        return _success_response({
            "removed": removed,
            "unbanned_groups": succeeded,
            "restored_groups": restored,
            "total_groups": len(group_ids),
            "deferred_groups": max(0, len(journal_group_ids) - len(group_ids)),
        })

    @any_admin
    async def get_groups(_request: web.Request, user: Any) -> web.Response:
        allowed = await _allowed_group_ids(int(user.id))
        stmt = (
            select(
                AuthorizedGroup.group_id,
                Group.title,
                Group.settings,
            )
            .select_from(AuthorizedGroup)
            .outerjoin(Group, Group.id == AuthorizedGroup.group_id)
            .where(AuthorizedGroup.bot_present.is_(True))
            .order_by(AuthorizedGroup.created_at.desc())
        )
        if allowed is not None:
            if not allowed:
                return _success_response({"groups": []})
            stmt = stmt.where(AuthorizedGroup.group_id.in_(allowed))
        async with session_factory() as session:
            result = await session.execute(stmt)
            groups = [
                _group_document(group_id, title, group_settings)
                for group_id, title, group_settings in result.all()
            ]
        return _success_response(
            {"groups": groups, "permission_fields": permission_field_document()}
        )

    @any_admin
    async def get_group_default_permissions(
        request: web.Request,
        user: Any,
    ) -> web.Response:
        group_id = _group_id(request)
        await _require_group_access(group_id, int(user.id))
        async with session_factory() as session:
            group = await session.get(Group, group_id)
            stored = (
                (group.settings or {}).get(GROUP_PERMISSIONS_SETTINGS_KEY)
                if group is not None and isinstance(group.settings, dict)
                else None
            )
        configured = stored is not None
        repaired = False
        repair_reason = ""
        if stored is not None:
            try:
                config = normalize_group_permission_config(stored)
            except ValueError as exc:
                # A previously complete document can become incomplete when a
                # newer Bot API adds permission fields. Let the group admin
                # reopen and save it instead of trapping the Mini App behind a
                # 500 response. Current Telegram values fill newly introduced
                # fields while compatible base/window choices are retained.
                configured = False
                repaired = True
                repair_reason = str(exc)
                config = None
        else:
            config = None
        if config is None:
            try:
                base = await _await_member_identity_lookup(
                    lambda: fetch_telegram_default_permissions(bot, group_id),
                    timeout_seconds=5.0,
                )
            except Exception as exc:
                raise _APIError(
                    502,
                    "telegram_permissions_unavailable",
                    "无法读取当前 Telegram 群默认权限，请确认 bot 仍在群内。",
                ) from exc
            config = repair_group_permissions_config(
                stored,
                fallback_base=base,
            )
        resolved = resolve_group_permissions(config)
        service = get_group_permission_service()
        return _success_response(
            {
                "default_permissions": config,
                "permission_fields": permission_field_document(),
                "configured": configured,
                "repaired": repaired,
                "repair_reason": repair_reason,
                "effective": resolved.permissions,
                "active_window_ids": list(resolved.active_window_ids),
                "status": service.status(group_id) if service is not None else {},
            }
        )

    @any_admin
    async def put_group_settings(request: web.Request, user: Any) -> web.Response:
        try:
            group_id = int(request.match_info["id"])
        except (KeyError, TypeError, ValueError) as exc:
            raise _APIError(400, "invalid_group_id", "群 ID 无效。") from exc
        await _require_group_access(group_id, int(user.id))

        request_body = await _json_object(request)
        unknown_request = set(request_body) - {
            "revision",
            "settings",
            "api_model_query_secret_change",
        }
        if unknown_request:
            raise _APIError(
                400,
                "unknown_group_settings_request",
                f"包含不允许的群设置请求字段：{', '.join(sorted(unknown_request))}",
            )
        expected_revision = str(request_body.get("revision") or "").strip()
        body = request_body.get("settings")
        raw_secret_change = request_body.get("api_model_query_secret_change")
        secret_change: _GroupApiModelQuerySecretChange | None = None
        if raw_secret_change is not None:
            if not isinstance(raw_secret_change, dict):
                raise _APIError(
                    400,
                    "invalid_api_model_query_secret_change",
                    "模型 API Key 变更格式无效。",
                )
            secret_change = _GroupApiModelQuerySecretChange.model_validate(
                raw_secret_change
            )
            if secret_change.action == "replace":
                try:
                    normalized_api_key = normalize_api_model_query_api_key(
                        secret_change.value
                    )
                except ValueError as exc:
                    raise _APIError(
                        400,
                        "invalid_api_model_query_secret",
                        str(exc),
                    ) from exc
                if not normalized_api_key:
                    raise _APIError(
                        400,
                        "invalid_api_model_query_secret",
                        "模型 API Key 不能为空。",
                    )
        if not expected_revision or not isinstance(body, dict):
            raise _APIError(
                400,
                "group_revision_required",
                "群设置请求缺少 revision 或 settings。",
            )
        unknown = set(body) - _GROUP_SETTING_FIELDS
        if unknown:
            names = ", ".join(sorted(unknown))
            raise _APIError(
                400,
                "unknown_group_settings",
                f"包含不允许修改的群设置：{names}",
            )
        settings_update = _GroupSettingsUpdate.model_validate(body)
        if not settings_update.model_fields_set and secret_change is None:
            raise _APIError(400, "empty_group_settings", "至少需要提供一个群设置。")

        permissions_changed = (
            GROUP_PERMISSIONS_SETTINGS_KEY in settings_update.model_fields_set
        )
        lock = _GROUP_UPDATE_LOCKS.setdefault(group_id, asyncio.Lock())
        async with lock:
            async with session_factory() as session:
                authorized = await session.get(AuthorizedGroup, group_id)
                if authorized is None or not bool(authorized.bot_present):
                    raise _APIError(404, "group_not_found", "该群未授权或不存在。")
                group = await session.get(Group, group_id)
                created_group = group is None
                if group is None:
                    group = Group(id=group_id, title="", settings={})
                    session.add(group)
                stored_settings = dict(group.settings or {})
                actual_revision = _group_revision(stored_settings)
                if actual_revision != expected_revision:
                    raise _APIError(
                        409,
                        "group_revision_conflict",
                        "群设置已被其他会话更新，请刷新后重试。",
                    )
                previous_style_target = int(
                    get_style_state(stored_settings).get("target_user_id") or 0
                )
                updated_settings = _apply_group_settings(
                    stored_settings,
                    settings_update,
                    settings,
                )
                api_model_query_fields = {
                    "api_model_query_enabled",
                    "api_model_query_base_url",
                    "api_model_query_http_timeout_sec",
                    "api_model_query_check_timeout_sec",
                }
                api_model_query_change_requested = bool(
                    settings_update.model_fields_set & api_model_query_fields
                    or secret_change is not None
                )
                if api_model_query_change_requested:
                    stored_api_config = get_api_model_query_config(stored_settings)
                    api_key_configured = await group_api_model_query_secret_exists(
                        session, group_id
                    )
                    if secret_change is not None:
                        if secret_change.action == "replace":
                            await replace_group_api_model_query_secret(
                                session,
                                group_id=group_id,
                                api_key=secret_change.value,
                                master_key=settings.config_master_key,
                                updated_by=int(user.id),
                            )
                            api_key_configured = True
                        else:
                            await clear_group_api_model_query_secret(
                                session, group_id=group_id
                            )
                            api_key_configured = False
                        stored_api_config = replace(
                            stored_api_config,
                            secret_version=stored_api_config.secret_version + 1,
                        )
                    updated_api_config = replace(
                        get_api_model_query_config(updated_settings),
                        api_key_configured=api_key_configured,
                        secret_version=stored_api_config.secret_version,
                    )
                    if updated_api_config.enabled and not updated_api_config.base_url:
                        raise _APIError(
                            400,
                            "api_model_query_not_configured",
                            "开启模型 API 查询前请先填写 Base URL。",
                        )
                    if updated_api_config.enabled and not api_key_configured:
                        raise _APIError(
                            400,
                            "api_model_query_key_required",
                            "开启模型 API 查询前请先配置 API Key。",
                        )
                    updated_settings = set_api_model_query_config(
                        updated_settings, updated_api_config
                    )
                current_style_target = int(
                    get_style_state(updated_settings).get("target_user_id") or 0
                )
                if current_style_target != previous_style_target:
                    await session.execute(
                        delete(SpeechStyleSample).where(
                            SpeechStyleSample.group_id == group_id
                        )
                    )
                if created_group:
                    group.settings = updated_settings
                    try:
                        await session.commit()
                    except IntegrityError as exc:
                        await session.rollback()
                        raise _APIError(
                            409,
                            "group_revision_conflict",
                            "群设置已被其他会话更新，请刷新后重试。",
                        ) from exc
                else:
                    changed = await session.execute(
                        update(Group)
                        .where(
                            Group.id == group_id,
                            Group.settings == stored_settings,
                        )
                        .values(settings=updated_settings)
                        .execution_options(synchronize_session=False)
                    )
                    if int(changed.rowcount or 0) != 1:
                        await session.rollback()
                        raise _APIError(
                            409,
                            "group_revision_conflict",
                            "群设置已被其他会话更新，请刷新后重试。",
                        )
                    await session.commit()
                document = _group_document(group.id, group.title, updated_settings)
        permission_apply: dict[str, Any] | None = None
        if permissions_changed:
            service = get_group_permission_service()
            config = document["settings"].get(GROUP_PERMISSIONS_SETTINGS_KEY)
            if service is not None and config is not None:
                applied = await service.apply_group_now(group_id, config)
                permission_apply = {
                    **service.status(group_id),
                    "applied": bool(applied),
                }
            elif service is not None:
                service.forget_group(group_id)
                permission_apply = {"applied": False, "removed": True}
            else:
                permission_apply = {"applied": False, "service_unavailable": True}
        return _success_response(
            {"group": document, "permission_apply": permission_apply}
        )

    def _group_id(request: web.Request) -> int:
        try:
            return int(request.match_info["id"])
        except (KeyError, TypeError, ValueError) as exc:
            raise _APIError(400, "invalid_group_id", "群 ID 无效。") from exc

    def _path_int(
        request: web.Request,
        key: str,
        *,
        code: str = "invalid_user_id",
        message: str = "用户 ID 无效。",
    ) -> int:
        try:
            return int(request.match_info[key])
        except (KeyError, TypeError, ValueError) as exc:
            raise _APIError(400, code, message) from exc

    @any_admin
    async def list_rules(request: web.Request, user: Any) -> web.Response:
        group_id = _group_id(request)
        await _require_group_access(group_id, int(user.id))
        async with session_factory() as session:
            rows = (await session.scalars(
                select(ModerationRule)
                .where(ModerationRule.group_id == group_id)
                .order_by(ModerationRule.id)
            )).all()
        return _success_response({"rules": [_rule_document(row) for row in rows]})

    @any_admin
    async def create_rule(request: web.Request, user: Any) -> web.Response:
        group_id = _group_id(request)
        await _require_group_access(group_id, int(user.id))
        body = _RuleCreate.model_validate(await _json_object(request))
        row = ModerationRule(
            group_id=group_id,
            rule_type=body.rule_type,
            pattern=clean_multiline_text(body.pattern, max_len=1000).strip(),
            action=body.action,
            enabled=body.enabled,
        )
        if not row.pattern:
            raise _APIError(400, "empty_rule", "群规内容不能为空。")
        async with session_factory() as session:
            await _ensure_group_row(session, group_id)
            session.add(row)
            await session.commit()
            await session.refresh(row)
        return _success_response({"rule": _rule_document(row)})

    @any_admin
    async def update_rule(request: web.Request, user: Any) -> web.Response:
        group_id = _group_id(request)
        await _require_group_access(group_id, int(user.id))
        try:
            rule_id = int(request.match_info["rule_id"])
        except (KeyError, TypeError, ValueError) as exc:
            raise _APIError(400, "invalid_rule_id", "群规 ID 无效。") from exc
        body = _RuleUpdate.model_validate(await _json_object(request))
        if not body.model_fields_set:
            raise _APIError(400, "empty_rule_update", "至少需要修改一个字段。")
        async with session_factory() as session:
            row = await session.get(ModerationRule, rule_id)
            if row is None or int(row.group_id) != group_id:
                raise _APIError(404, "rule_not_found", "群规不存在。")
            if "rule_type" in body.model_fields_set:
                row.rule_type = str(body.rule_type)
            if "pattern" in body.model_fields_set:
                row.pattern = clean_multiline_text(str(body.pattern or ""), max_len=1000).strip()
                if not row.pattern:
                    raise _APIError(400, "empty_rule", "群规内容不能为空。")
            if "action" in body.model_fields_set:
                row.action = str(body.action)
            if "enabled" in body.model_fields_set:
                row.enabled = bool(body.enabled)
            await session.commit()
            document = _rule_document(row)
        return _success_response({"rule": document})

    @any_admin
    async def delete_rule(request: web.Request, user: Any) -> web.Response:
        group_id = _group_id(request)
        await _require_group_access(group_id, int(user.id))
        try:
            rule_id = int(request.match_info["rule_id"])
        except (KeyError, TypeError, ValueError) as exc:
            raise _APIError(400, "invalid_rule_id", "群规 ID 无效。") from exc
        async with session_factory() as session:
            row = await session.get(ModerationRule, rule_id)
            if row is None or int(row.group_id) != group_id:
                raise _APIError(404, "rule_not_found", "群规不存在。")
            await session.delete(row)
            await session.commit()
        return _success_response({"deleted": True})

    @any_admin
    async def list_keyword_replies(request: web.Request, user: Any) -> web.Response:
        group_id = _group_id(request)
        await _require_group_access(group_id, int(user.id))
        async with session_factory() as session:
            rows = (await session.scalars(
                select(KeywordReply)
                .where(KeywordReply.group_id == group_id)
                .order_by(KeywordReply.id)
            )).all()
        return _success_response(
            {"keyword_replies": [_keyword_reply_document(row) for row in rows]}
        )

    @any_admin
    async def create_keyword_reply(request: web.Request, user: Any) -> web.Response:
        group_id = _group_id(request)
        await _require_group_access(group_id, int(user.id))
        body = _KeywordReplyCreate.model_validate(await _json_object(request))
        reply_text = clean_multiline_text(body.reply_text, max_len=4000).strip()
        if not reply_text:
            raise _APIError(400, "empty_reply_text", "回复内容不能为空。")
        row = KeywordReply(
            group_id=group_id,
            keyword=_validated_keyword(body.keyword, body.match_type),
            match_type=body.match_type,
            reply_text=reply_text,
            buttons=_validated_template_buttons(body.buttons),
            pin_message=body.pin_message,
            auto_delete=body.auto_delete,
            enabled=body.enabled,
            created_by=int(user.id),
        )
        async with session_factory() as session:
            await _ensure_group_row(session, group_id)
            session.add(row)
            await session.commit()
            await session.refresh(row)
        return _success_response({"keyword_reply": _keyword_reply_document(row)})

    @any_admin
    async def update_keyword_reply(request: web.Request, user: Any) -> web.Response:
        group_id = _group_id(request)
        await _require_group_access(group_id, int(user.id))
        entry_id = _path_int(
            request, "entry_id", code="invalid_entry_id", message="记录 ID 无效。"
        )
        body = _KeywordReplyUpdate.model_validate(await _json_object(request))
        if not body.model_fields_set:
            raise _APIError(400, "empty_update", "至少需要修改一个字段。")
        async with session_factory() as session:
            row = await session.get(KeywordReply, entry_id)
            if row is None or int(row.group_id) != group_id:
                raise _APIError(404, "keyword_reply_not_found", "关键词回复不存在。")
            if "match_type" in body.model_fields_set:
                row.match_type = str(body.match_type)
            if "keyword" in body.model_fields_set:
                row.keyword = _validated_keyword(
                    str(body.keyword or ""), str(row.match_type)
                )
            elif "match_type" in body.model_fields_set:
                row.keyword = _validated_keyword(str(row.keyword), str(row.match_type))
            if "reply_text" in body.model_fields_set:
                row.reply_text = clean_multiline_text(
                    str(body.reply_text or ""), max_len=4000
                ).strip()
                if not row.reply_text:
                    raise _APIError(400, "empty_reply_text", "回复内容不能为空。")
            if "buttons" in body.model_fields_set:
                row.buttons = _validated_template_buttons(body.buttons or [])
            if "pin_message" in body.model_fields_set:
                row.pin_message = bool(body.pin_message)
            if "auto_delete" in body.model_fields_set:
                row.auto_delete = bool(body.auto_delete)
            if "enabled" in body.model_fields_set:
                row.enabled = bool(body.enabled)
            await session.commit()
            document = _keyword_reply_document(row)
        return _success_response({"keyword_reply": document})

    @any_admin
    async def delete_keyword_reply(request: web.Request, user: Any) -> web.Response:
        group_id = _group_id(request)
        await _require_group_access(group_id, int(user.id))
        entry_id = _path_int(
            request, "entry_id", code="invalid_entry_id", message="记录 ID 无效。"
        )
        async with session_factory() as session:
            row = await session.get(KeywordReply, entry_id)
            if row is None or int(row.group_id) != group_id:
                raise _APIError(404, "keyword_reply_not_found", "关键词回复不存在。")
            await session.delete(row)
            await session.commit()
        return _success_response({"deleted": True})

    @any_admin
    async def list_scheduled_messages(request: web.Request, user: Any) -> web.Response:
        group_id = _group_id(request)
        await _require_group_access(group_id, int(user.id))
        async with session_factory() as session:
            rows = (await session.scalars(
                select(ScheduledMessage)
                .where(ScheduledMessage.group_id == group_id)
                .order_by(ScheduledMessage.id)
            )).all()
        return _success_response(
            {"scheduled_messages": [_scheduled_message_document(row) for row in rows]}
        )

    @any_admin
    async def create_scheduled_message(request: web.Request, user: Any) -> web.Response:
        group_id = _group_id(request)
        await _require_group_access(group_id, int(user.id))
        body = _ScheduledMessageCreate.model_validate(await _json_object(request))
        text = clean_multiline_text(body.text, max_len=4000).strip()
        if not text:
            raise _APIError(400, "empty_message_text", "消息内容不能为空。")
        row = ScheduledMessage(
            group_id=group_id,
            text=text,
            buttons=_validated_template_buttons(body.buttons),
            schedule_type=body.schedule_type,
            schedule_time=_validated_schedule_time(body.schedule_time),
            interval_minutes=int(body.interval_minutes),
            pin_message=body.pin_message,
            unpin_previous=body.unpin_previous,
            auto_delete=body.auto_delete,
            enabled=body.enabled,
            # Anchor at creation so a daily entry created after today's HH:MM
            # starts tomorrow instead of firing immediately.
            last_run_at=now_shanghai_naive(),
            created_by=int(user.id),
        )
        async with session_factory() as session:
            await _ensure_group_row(session, group_id)
            session.add(row)
            await session.commit()
            await session.refresh(row)
        return _success_response(
            {"scheduled_message": _scheduled_message_document(row)}
        )

    @any_admin
    async def update_scheduled_message(request: web.Request, user: Any) -> web.Response:
        group_id = _group_id(request)
        await _require_group_access(group_id, int(user.id))
        entry_id = _path_int(
            request, "entry_id", code="invalid_entry_id", message="记录 ID 无效。"
        )
        body = _ScheduledMessageUpdate.model_validate(await _json_object(request))
        if not body.model_fields_set:
            raise _APIError(400, "empty_update", "至少需要修改一个字段。")
        async with session_factory() as session:
            row = await session.get(ScheduledMessage, entry_id)
            if row is None or int(row.group_id) != group_id:
                raise _APIError(404, "scheduled_message_not_found", "定时消息不存在。")
            if "text" in body.model_fields_set:
                row.text = clean_multiline_text(str(body.text or ""), max_len=4000).strip()
                if not row.text:
                    raise _APIError(400, "empty_message_text", "消息内容不能为空。")
            if "buttons" in body.model_fields_set:
                row.buttons = _validated_template_buttons(body.buttons or [])
            if "schedule_type" in body.model_fields_set:
                row.schedule_type = str(body.schedule_type)
            if "schedule_time" in body.model_fields_set:
                row.schedule_time = _validated_schedule_time(str(body.schedule_time or ""))
            if "interval_minutes" in body.model_fields_set:
                row.interval_minutes = int(body.interval_minutes or 60)
            if "pin_message" in body.model_fields_set:
                row.pin_message = bool(body.pin_message)
            if "unpin_previous" in body.model_fields_set:
                row.unpin_previous = bool(body.unpin_previous)
            if "auto_delete" in body.model_fields_set:
                row.auto_delete = bool(body.auto_delete)
            if "enabled" in body.model_fields_set:
                was_enabled = bool(row.enabled)
                row.enabled = bool(body.enabled)
                # Re-enabling anchors the next run to now instead of firing
                # immediately off a stale last_run_at; an edit that keeps the
                # entry enabled must not postpone the schedule.
                if row.enabled and not was_enabled:
                    row.last_run_at = now_shanghai_naive()
            await session.commit()
            document = _scheduled_message_document(row)
        return _success_response({"scheduled_message": document})

    @any_admin
    async def delete_scheduled_message(request: web.Request, user: Any) -> web.Response:
        group_id = _group_id(request)
        await _require_group_access(group_id, int(user.id))
        entry_id = _path_int(
            request, "entry_id", code="invalid_entry_id", message="记录 ID 无效。"
        )
        async with session_factory() as session:
            row = await session.get(ScheduledMessage, entry_id)
            if row is None or int(row.group_id) != group_id:
                raise _APIError(404, "scheduled_message_not_found", "定时消息不存在。")
            await session.delete(row)
            await session.commit()
        return _success_response({"deleted": True})

    @any_admin
    async def list_memories(request: web.Request, user: Any) -> web.Response:
        group_id = _group_id(request)
        await _require_group_access(group_id, int(user.id))
        async with session_factory() as session:
            rows = (await session.scalars(
                select(GroupPermanentMemory)
                .where(GroupPermanentMemory.group_id == group_id)
                .order_by(GroupPermanentMemory.id)
            )).all()
        return _success_response({"memories": [_memory_document(row) for row in rows]})

    @any_admin
    async def create_memory(request: web.Request, user: Any) -> web.Response:
        group_id = _group_id(request)
        await _require_group_access(group_id, int(user.id))
        body = _MemoryCreate.model_validate(await _json_object(request))
        content = clean_multiline_text(body.content, max_len=4000).strip()
        if not content:
            raise _APIError(400, "empty_memory", "永久记忆不能为空。")
        async with session_factory() as session:
            await _ensure_group_row(session, group_id)
            existing = await session.scalar(
                select(GroupPermanentMemory).where(
                    GroupPermanentMemory.group_id == group_id,
                    GroupPermanentMemory.content == content,
                )
            )
            if existing is not None:
                return _success_response({"memory": _memory_document(existing)})
            row = GroupPermanentMemory(
                group_id=group_id,
                content=content,
                created_by=int(user.id),
            )
            session.add(row)
            await session.commit()
            await session.refresh(row)
        return _success_response({"memory": _memory_document(row)})

    @any_admin
    async def update_memory(request: web.Request, user: Any) -> web.Response:
        group_id = _group_id(request)
        await _require_group_access(group_id, int(user.id))
        try:
            memory_id = int(request.match_info["memory_id"])
        except (KeyError, TypeError, ValueError) as exc:
            raise _APIError(400, "invalid_memory_id", "记忆 ID 无效。") from exc
        body = _MemoryCreate.model_validate(await _json_object(request))
        content = clean_multiline_text(body.content, max_len=4000).strip()
        if not content:
            raise _APIError(400, "empty_memory", "永久记忆不能为空。")
        async with session_factory() as session:
            row = await session.get(GroupPermanentMemory, memory_id)
            if row is None or int(row.group_id) != group_id:
                raise _APIError(404, "memory_not_found", "永久记忆不存在。")
            row.content = content
            await session.commit()
            document = _memory_document(row)
        return _success_response({"memory": document})

    @any_admin
    async def delete_memory(request: web.Request, user: Any) -> web.Response:
        group_id = _group_id(request)
        await _require_group_access(group_id, int(user.id))
        try:
            memory_id = int(request.match_info["memory_id"])
        except (KeyError, TypeError, ValueError) as exc:
            raise _APIError(400, "invalid_memory_id", "记忆 ID 无效。") from exc
        async with session_factory() as session:
            row = await session.get(GroupPermanentMemory, memory_id)
            if row is None or int(row.group_id) != group_id:
                raise _APIError(404, "memory_not_found", "永久记忆不存在。")
            await session.delete(row)
            await session.commit()
        return _success_response({"deleted": True})

    async def _list_user_rows(
        request: web.Request,
        user: Any,
        model: type[Any],
        key: str,
    ) -> web.Response:
        group_id = _group_id(request)
        await _require_group_access(group_id, int(user.id))
        async with session_factory() as session:
            stmt = select(model).where(model.group_id == group_id).order_by(model.user_id)
            if model is UserWarning:
                stmt = stmt.where(
                    or_(UserWarning.count > 0, UserWarning.is_banned.is_(True))
                )
            rows = (await session.scalars(stmt)).all()
        members = await _group_member_map(
            session_factory,
            group_id,
            [int(row.user_id) for row in rows],
            bot_obj=bot,
        )
        return _success_response(
            {
                key: [
                    _user_policy_document(row, members.get(int(row.user_id)))
                    for row in rows
                ]
            }
        )

    @any_admin
    async def list_warnings(request: web.Request, user: Any) -> web.Response:
        return await _list_user_rows(request, user, UserWarning, "warnings")

    @any_admin
    async def clear_warning(request: web.Request, user: Any) -> web.Response:
        group_id = _group_id(request)
        await _require_group_access(group_id, int(user.id))
        target_id = _path_int(request, "user_id")
        async with session_factory() as session:
            row = await session.scalar(select(UserWarning).where(
                UserWarning.group_id == group_id,
                UserWarning.user_id == target_id,
            ))
            if row is not None and row.is_banned:
                raise _APIError(
                    409,
                    "user_is_banned",
                    "该用户已被群内封禁，请先执行解封。",
                )
            if row is not None:
                await session.delete(row)
            await session.commit()
        return _success_response({"deleted": row is not None})

    @any_admin
    async def list_group_bans(request: web.Request, user: Any) -> web.Response:
        group_id = _group_id(request)
        await _require_group_access(group_id, int(user.id))
        async with session_factory() as session:
            rows = (await session.scalars(
                select(UserWarning)
                .where(
                    UserWarning.group_id == group_id,
                    UserWarning.is_banned == True,  # noqa: E712
                )
                .order_by(UserWarning.count.desc(), UserWarning.user_id.asc())
            )).all()
        members = await _group_member_map(
            session_factory,
            group_id,
            [int(row.user_id) for row in rows],
            bot_obj=bot,
        )
        return _success_response(
            {
                "bans": [
                    _ban_document(row, members.get(int(row.user_id)))
                    for row in rows
                ]
            }
        )

    @any_admin
    async def create_group_ban(request: web.Request, user: Any) -> web.Response:
        group_id = _group_id(request)
        await _require_group_access(group_id, int(user.id))
        body = _UserIdCreate.model_validate(await _json_object(request))
        if settings.super_admin_id and body.user_id == int(settings.super_admin_id):
            raise _APIError(400, "cannot_ban_owner", "不能封禁最高管理员。")
        ban_member = getattr(bot, "ban_chat_member", None)
        if not callable(ban_member):
            raise _APIError(503, "telegram_unavailable", "当前无法调用 Telegram 封禁接口。")
        prompts: set[tuple[int, int]] = set()
        private_prompts: set[tuple[int, int]] = set()
        async with session_factory() as session:
            if await is_globally_banned(session, body.user_id):
                raise _APIError(
                    409,
                    "group_ban_locked",
                    "该用户的封禁状态只能由最高管理员处理。",
                )
            verification = await get_join_verification(
                session, group_id, body.user_id
            )
            if verification is not None and int(verification.prompt_message_id or 0) > 0:
                prompts.add((group_id, int(verification.prompt_message_id)))
            if verification is not None and int(
                getattr(verification, "private_message_id", 0) or 0
            ) > 0:
                private_prompts.add(
                    (int(body.user_id), int(verification.private_message_id))
                )
            recovery = await lease_join_verification_for_unban(
                session,
                group_id,
                body.user_id,
                manual_unban=False,
            )
            if recovery is None:
                await session.rollback()
                raise _APIError(
                    503,
                    "group_ban_recovery_unavailable",
                    "无法建立封禁恢复工单，请立即重试。",
                )
            await session.commit()
        try:
            result = await enforce_ban_member(bot, group_id, body.user_id)
            if not result:
                raise RuntimeError("Telegram returned false")
        except Exception as exc:
            try:
                async with session_factory() as session:
                    await record_ban_event(
                        session,
                        group_id=group_id,
                        target_user_id=body.user_id,
                        action="ban",
                        source="miniapp_group",
                        outcome="failed",
                        reason="Mini App 管理员手动封禁",
                        actor_user_id=int(user.id),
                        details={"telegram_error": type(exc).__name__},
                    )
                    await session.commit()
            except Exception:
                log.exception(
                    "web group ban failure audit write failed | group=%s user=%s",
                    group_id,
                    body.user_id,
                )
            raise _APIError(502, "telegram_ban_failed", "Telegram 群内封禁失败，请稍后重试。") from exc
        async with session_factory() as session:
            claimed = await complete_leased_join_verification(
                session,
                verification_id=int(recovery.verification_id),
                lease_until=recovery.lease_until,
                status="unbanning",
            )
            if not claimed:
                await session.rollback()

                async def preserve_latest_ban() -> bool:
                    async with session_factory() as policy_session:
                        return bool(
                            await verification_release_blocked_by_ban(
                                policy_session,
                                group_id=group_id,
                                user_id=body.user_id,
                            )
                        )

                async def preserve_latest_restriction() -> bool:
                    async with session_factory() as policy_session:
                        return bool(
                            await verification_restriction_required(
                                policy_session,
                                group_id=group_id,
                                user_id=body.user_id,
                            )
                        )

                try:
                    await reconcile_moderation_ban_after_lost_lease(
                        bot,
                        group_id,
                        body.user_id,
                        preserve_latest_ban,
                        restriction_required=preserve_latest_restriction,
                    )
                except Exception:
                    log.exception(
                        "web group ban superseded-state reconciliation failed | "
                        "group=%s user=%s",
                        group_id,
                        body.user_id,
                    )
                raise _APIError(
                    409,
                    "group_ban_state_changed",
                    "封禁期间权限策略已被其他操作更新，已按最新策略校准。",
                )
            row = await _set_group_banned_after_telegram(
                session,
                group_id=group_id,
                user_id=body.user_id,
            )
            await record_ban_event(
                session,
                group_id=group_id,
                target_user_id=body.user_id,
                action="ban",
                source="miniapp_group",
                outcome="succeeded",
                reason="Mini App 管理员手动封禁",
                actor_user_id=int(user.id),
            )
            await session.commit()
        await delete_verification_prompts(bot, prompts)
        await close_private_challenge_messages(bot, private_prompts)
        return _success_response({"ban": _ban_document(row)})

    @any_admin
    async def delete_group_ban(request: web.Request, user: Any) -> web.Response:
        group_id = _group_id(request)
        await _require_group_access(group_id, int(user.id))
        target_id = _path_int(request, "user_id")
        prompts: set[tuple[int, int]] = set()
        private_prompts: set[tuple[int, int]] = set()
        async with session_factory() as session:
            if await is_globally_banned(session, target_id):
                raise _APIError(
                    409,
                    "group_ban_locked",
                    "该用户的封禁状态只能由最高管理员处理。",
                )
            row = await session.scalar(select(UserWarning).where(
                UserWarning.group_id == group_id,
                UserWarning.user_id == target_id,
            ))
            warning_snapshot = (
                (
                    int(row.id),
                    max(0, int(row.count or 0)),
                    bool(row.is_banned),
                )
                if row is not None and bool(row.is_banned)
                else None
            )
            verification = await get_join_verification(session, group_id, target_id)
            if verification is not None and int(verification.prompt_message_id or 0) > 0:
                prompts.add((group_id, int(verification.prompt_message_id)))
            if verification is not None and int(
                getattr(verification, "private_message_id", 0) or 0
            ) > 0:
                private_prompts.add(
                    (int(target_id), int(verification.private_message_id))
                )
            recovery = await lease_join_verification_for_unban(
                session,
                group_id,
                target_id,
            )
            await session.commit()
        activate_manual_unban_recovery(recovery)
        unban_member = getattr(bot, "unban_chat_member", None)
        if not callable(unban_member):
            raise _APIError(503, "telegram_unavailable", "当前无法调用 Telegram 解封接口。")
        try:
            result = await enforce_unban_member(bot, group_id, target_id)
            if not result:
                raise RuntimeError("Telegram returned false")
        except Exception as exc:
            raise _APIError(502, "telegram_unban_failed", "Telegram 群内解封失败，请稍后重试。") from exc
        async with session_factory() as session:
            if await is_globally_banned(session, target_id):
                await session.rollback()
                try:
                    await enforce_ban_member(bot, group_id, target_id)
                except Exception:
                    log.exception(
                        "web group unban global-race reban failed | group=%s user=%s",
                        group_id,
                        target_id,
                    )
                raise _APIError(
                    409,
                    "group_ban_locked",
                    "解封期间该用户被加入全局封禁名单，已保留封禁状态。",
                )
            if warning_snapshot is None:
                current = await session.scalar(
                    select(UserWarning).where(
                        UserWarning.group_id == group_id,
                        UserWarning.user_id == target_id,
                    )
                )
                claimed = current is None or not bool(current.is_banned)
            else:
                warning_id, previous_count, was_banned = warning_snapshot
                deleted = await session.execute(
                    delete(UserWarning).where(
                        UserWarning.id == warning_id,
                        UserWarning.group_id == group_id,
                        UserWarning.user_id == target_id,
                        UserWarning.count == previous_count,
                        UserWarning.is_banned.is_(was_banned),
                    )
                )
                claimed = int(deleted.rowcount or 0) == 1
            if not claimed:
                await session.rollback()
                current = await session.scalar(
                    select(UserWarning).where(
                        UserWarning.group_id == group_id,
                        UserWarning.user_id == target_id,
                    )
                )
                current_banned = current is not None and bool(current.is_banned)
                await session.rollback()
                if current_banned:
                    try:
                        await enforce_ban_member(bot, group_id, target_id)
                    except Exception:
                        log.exception(
                            "web group unban concurrent-state reban failed | group=%s user=%s",
                            group_id,
                            target_id,
                        )
                raise _APIError(
                    409,
                    "group_ban_state_changed",
                    "解封期间警告或封禁状态已被其他管理操作更新，请刷新后重试。",
                )
            current_verification = await get_join_verification(
                session, group_id, target_id
            )
            if (
                current_verification is not None
                and int(current_verification.prompt_message_id or 0) > 0
            ):
                prompts.add(
                    (group_id, int(current_verification.prompt_message_id))
                )
            if current_verification is not None and int(
                getattr(current_verification, "private_message_id", 0) or 0
            ) > 0:
                private_prompts.add(
                    (int(target_id), int(current_verification.private_message_id))
                )
            try:
                await session.commit()
            except Exception as exc:
                await session.rollback()
                try:
                    await enforce_ban_member(bot, group_id, target_id)
                except Exception:
                    log.exception(
                        "web group unban database-failure reban failed | group=%s user=%s",
                        group_id,
                        target_id,
                    )
                raise _APIError(
                    500,
                    "group_unban_state_failed",
                    "Telegram 已解封，但数据库更新失败；已尝试恢复封禁，请重试。",
                ) from exc
        await delete_verification_prompts(bot, prompts)
        await close_private_challenge_messages(bot, private_prompts)
        restored = await restore_member_permissions(bot, group_id, target_id)
        if restored and recovery is not None:
            async with session_factory() as completion_session:
                completed = await complete_leased_join_verification(
                    completion_session,
                    verification_id=int(recovery.verification_id),
                    lease_until=recovery.lease_until,
                    status="unbanning",
                )
                if completed:
                    await completion_session.commit()
                else:
                    await completion_session.rollback()
        try:
            async with session_factory() as session:
                await record_ban_event(
                    session,
                    group_id=group_id,
                    target_user_id=target_id,
                    action="unban",
                    source="miniapp_group",
                    outcome="succeeded",
                    reason="Mini App 管理员解除群内封禁",
                    actor_user_id=int(user.id),
                    details={"permissions_restored": bool(restored)},
                )
                await session.commit()
        except Exception:
            log.exception(
                "web group unban audit write failed | group=%s user=%s",
                group_id,
                target_id,
            )
        return _success_response({
            "deleted": warning_snapshot is not None,
            "restored": bool(restored),
        })

    async def _create_user_row(
        request: web.Request,
        user: Any,
        model: type[Any],
        key: str,
    ) -> web.Response:
        group_id = _group_id(request)
        await _require_group_access(group_id, int(user.id))
        body = _UserIdCreate.model_validate(await _json_object(request))
        recovery = None
        released = True
        async with session_factory() as session:
            row = await session.scalar(select(model).where(
                model.group_id == group_id,
                model.user_id == body.user_id,
            ))
            if row is None:
                row = model(group_id=group_id, user_id=body.user_id, created_by=int(user.id))
                session.add(row)
            if model is ModerationExemption:
                recovery = await lease_join_verification_for_unban(
                    session,
                    group_id,
                    body.user_id,
                    manual_unban=False,
                )
                if recovery is None:
                    await session.rollback()
                    raise _APIError(
                        503,
                        "exemption_recovery_unavailable",
                        "无法建立豁免恢复工单，请立即重试。",
                    )
            await session.commit()
            await session.refresh(row)
            if recovery is not None:
                activate_manual_unban_recovery(recovery)
                released = await release_moderation_restriction_after_exemption(
                    bot,
                    session,
                    recovery,
                )
        payload = {key: _user_policy_document(row)}
        if model is ModerationExemption:
            payload["restriction_reconciled"] = bool(released)
        return _success_response(payload)

    async def _delete_user_row(
        request: web.Request,
        user: Any,
        model: type[Any],
    ) -> web.Response:
        group_id = _group_id(request)
        await _require_group_access(group_id, int(user.id))
        target_id = _path_int(request, "user_id")
        async with session_factory() as session:
            row = await session.scalar(select(model).where(
                model.group_id == group_id,
                model.user_id == target_id,
            ))
            if row is not None:
                await session.delete(row)
            await session.commit()
        return _success_response({"deleted": row is not None})

    @any_admin
    async def list_exemptions(request: web.Request, user: Any) -> web.Response:
        return await _list_user_rows(request, user, ModerationExemption, "exemptions")

    @any_admin
    async def create_exemption(request: web.Request, user: Any) -> web.Response:
        return await _create_user_row(request, user, ModerationExemption, "exemption")

    @any_admin
    async def delete_exemption(request: web.Request, user: Any) -> web.Response:
        return await _delete_user_row(request, user, ModerationExemption)

    @any_admin
    async def list_reply_mutes(request: web.Request, user: Any) -> web.Response:
        return await _list_user_rows(request, user, ReplyMute, "reply_mutes")

    @any_admin
    async def create_reply_mute(request: web.Request, user: Any) -> web.Response:
        return await _create_user_row(request, user, ReplyMute, "reply_mute")

    @any_admin
    async def delete_reply_mute(request: web.Request, user: Any) -> web.Response:
        return await _delete_user_row(request, user, ReplyMute)

    @any_admin
    async def get_patrol_status(request: web.Request, user: Any) -> web.Response:
        group_id = _group_id(request)
        await _require_group_access(group_id, int(user.id))
        service = get_patrol_service()
        if service is None:
            raise _APIError(503, "patrol_unavailable", "巡检服务尚未启动。")
        status = await service.status(group_id)
        async with session_factory() as session:
            group = await session.get(Group, group_id)
            group_settings = dict(group.settings or {}) if group else {}
        status["enabled"] = patrol_policy(settings, group_settings)
        return _success_response({"patrol": status})

    @any_admin
    async def trigger_patrol(request: web.Request, user: Any) -> web.Response:
        group_id = _group_id(request)
        await _require_group_access(group_id, int(user.id))
        service = get_patrol_service()
        if service is None:
            raise _APIError(503, "patrol_unavailable", "巡检服务尚未启动。")
        if not settings.moderation.enabled:
            raise _APIError(400, "moderation_disabled", "内容审核已关闭，无法巡检。")
        if not verification_service_ready(settings, verification_provider(settings)):
            raise _APIError(
                400,
                "verification_provider_unavailable",
                "真人质询验证服务未配置，暂时不能巡检。",
            )
        # Fire-and-forget: a full scan takes far longer than the Mini App's
        # 10-minute initData window allows a request to wait.
        result = service.start_manual_patrol(group_id)
        if not result.get("started"):
            raise _APIError(409, "patrol_already_running", "该群巡检正在进行中。")
        log.info("manual patrol triggered | group=%s operator=%s", group_id, user.id)
        return _success_response({"patrol": result})

    app.router.add_get("/api/v1/session", get_session)
    app.router.add_get("/api/v1/settings", get_settings)
    app.router.add_put("/api/v1/settings", put_settings)
    app.router.add_get("/api/v1/authorized-groups", list_authorized_groups_api)
    app.router.add_post("/api/v1/authorized-groups", create_authorized_group_api)
    app.router.add_delete("/api/v1/authorized-groups/{id}", delete_authorized_group_api)
    app.router.add_get("/api/v1/groups/{id}/admins", list_group_admins_api)
    app.router.add_get("/api/v1/groups/{id}/telegram-admins", list_telegram_admins_api)
    app.router.add_post("/api/v1/groups/{id}/admins", create_group_admin_api)
    app.router.add_delete("/api/v1/groups/{id}/admins/{user_id}", delete_group_admin_api)
    app.router.add_get("/api/v1/global-bans", list_global_bans_api)
    app.router.add_post("/api/v1/global-bans", create_global_ban_api)
    app.router.add_delete("/api/v1/global-bans/{user_id}", delete_global_ban_api)
    app.router.add_get("/api/v1/groups", get_groups)
    app.router.add_get(
        "/api/v1/groups/{id}/default-permissions",
        get_group_default_permissions,
    )
    app.router.add_put("/api/v1/groups/{id}/settings", put_group_settings)
    app.router.add_get("/api/v1/groups/{id}/rules", list_rules)
    app.router.add_post("/api/v1/groups/{id}/rules", create_rule)
    app.router.add_patch("/api/v1/groups/{id}/rules/{rule_id}", update_rule)
    app.router.add_delete("/api/v1/groups/{id}/rules/{rule_id}", delete_rule)
    app.router.add_get("/api/v1/groups/{id}/keyword-replies", list_keyword_replies)
    app.router.add_post("/api/v1/groups/{id}/keyword-replies", create_keyword_reply)
    app.router.add_patch(
        "/api/v1/groups/{id}/keyword-replies/{entry_id}", update_keyword_reply
    )
    app.router.add_delete(
        "/api/v1/groups/{id}/keyword-replies/{entry_id}", delete_keyword_reply
    )
    app.router.add_get(
        "/api/v1/groups/{id}/scheduled-messages", list_scheduled_messages
    )
    app.router.add_post(
        "/api/v1/groups/{id}/scheduled-messages", create_scheduled_message
    )
    app.router.add_patch(
        "/api/v1/groups/{id}/scheduled-messages/{entry_id}", update_scheduled_message
    )
    app.router.add_delete(
        "/api/v1/groups/{id}/scheduled-messages/{entry_id}", delete_scheduled_message
    )
    app.router.add_get("/api/v1/groups/{id}/memories", list_memories)
    app.router.add_post("/api/v1/groups/{id}/memories", create_memory)
    app.router.add_patch("/api/v1/groups/{id}/memories/{memory_id}", update_memory)
    app.router.add_delete("/api/v1/groups/{id}/memories/{memory_id}", delete_memory)
    app.router.add_get("/api/v1/groups/{id}/warnings", list_warnings)
    app.router.add_delete("/api/v1/groups/{id}/warnings/{user_id}", clear_warning)
    app.router.add_get("/api/v1/groups/{id}/bans", list_group_bans)
    app.router.add_post("/api/v1/groups/{id}/bans", create_group_ban)
    app.router.add_delete("/api/v1/groups/{id}/bans/{user_id}", delete_group_ban)
    app.router.add_get("/api/v1/groups/{id}/moderation-exemptions", list_exemptions)
    app.router.add_post("/api/v1/groups/{id}/moderation-exemptions", create_exemption)
    app.router.add_delete("/api/v1/groups/{id}/moderation-exemptions/{user_id}", delete_exemption)
    app.router.add_get("/api/v1/groups/{id}/reply-mutes", list_reply_mutes)
    app.router.add_post("/api/v1/groups/{id}/reply-mutes", create_reply_mute)
    app.router.add_delete("/api/v1/groups/{id}/reply-mutes/{user_id}", delete_reply_mute)
    app.router.add_get("/api/v1/groups/{id}/patrol", get_patrol_status)
    app.router.add_post("/api/v1/groups/{id}/patrol", trigger_patrol)


__all__ = ["register_settings_routes"]
