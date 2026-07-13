from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from collections.abc import Awaitable, Callable
from functools import wraps
from typing import Any, Literal

from aiohttp import web
from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt, ValidationError
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from bot.config import Settings
from bot.db.models import AuthorizedGroup, Group, SpeechStyleSample
from bot.services.at_reply import is_at_reply_enabled, set_at_reply_enabled
from bot.services.doubao_tts import normalize_tts_mode, set_tts_mode
from bot.services.proactive import (
    get_cooldown_task_state,
    set_cooldown_task_enabled,
)
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
from bot.web.auth import require_super_admin

log = logging.getLogger(__name__)

_GROUP_SETTING_FIELDS = {
    "av_enabled",
    "mute_all_replies",
    "at_reply_mode",
    "tts_mode",
    "proactive_enabled",
    "proactive_task_brief",
    "mimic_target_user_id",
    "mimic_target_user_name",
    "mimic_profile_text",
}
_SCHEDULED_TASKS_KEY = "scheduled_tasks"
_COOLDOWN_TASK_KEY = "cooldown_topic"
_GROUP_UPDATE_LOCKS: dict[int, asyncio.Lock] = {}


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
    proactive_enabled: StrictBool | None = None
    proactive_task_brief: str | None = Field(default=None, max_length=240)
    mimic_target_user_id: StrictInt | None = Field(default=None, ge=0)
    mimic_target_user_name: str | None = Field(default=None, max_length=80)
    mimic_profile_text: str | None = Field(default=None, max_length=1200)


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


async def _json_object(request: web.Request) -> dict[str, Any]:
    try:
        body = await request.json()
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


def _public_group_settings(settings_data: dict[str, Any]) -> dict[str, Any]:
    proactive_state = get_cooldown_task_state(settings_data)
    style_state = get_style_state(settings_data)
    return {
        "av_enabled": _setting_bool(settings_data, "av_enabled"),
        "mute_all_replies": _setting_bool(settings_data, "mute_all_replies"),
        "at_reply_mode": is_at_reply_enabled(settings_data),
        "tts_mode": normalize_tts_mode(settings_data),
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
    }


def _group_revision(settings_data: dict[str, Any]) -> str:
    public = _public_group_settings(settings_data)
    editable = {
        key: public.get(key)
        for key in sorted(_GROUP_SETTING_FIELDS)
    }
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
        if name != "proactive_enabled" and getattr(update, name) is None
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
    async def get_groups(_request: web.Request, _user: Any) -> web.Response:
        stmt = (
            select(
                AuthorizedGroup.group_id,
                Group.title,
                Group.settings,
            )
            .select_from(AuthorizedGroup)
            .outerjoin(Group, Group.id == AuthorizedGroup.group_id)
            .order_by(AuthorizedGroup.created_at.desc())
        )
        async with session_factory() as session:
            result = await session.execute(stmt)
            groups = [
                _group_document(group_id, title, group_settings)
                for group_id, title, group_settings in result.all()
            ]
        return _success_response({"groups": groups})

    @authenticated
    async def put_group_settings(request: web.Request, _user: Any) -> web.Response:
        try:
            group_id = int(request.match_info["id"])
        except (KeyError, TypeError, ValueError) as exc:
            raise _APIError(400, "invalid_group_id", "群 ID 无效。") from exc

        request_body = await _json_object(request)
        expected_revision = str(request_body.get("revision") or "").strip()
        body = request_body.get("settings")
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
        update = _GroupSettingsUpdate.model_validate(body)
        if not update.model_fields_set:
            raise _APIError(400, "empty_group_settings", "至少需要提供一个群设置。")

        lock = _GROUP_UPDATE_LOCKS.setdefault(group_id, asyncio.Lock())
        async with lock:
            async with session_factory() as session:
                authorized = await session.get(AuthorizedGroup, group_id)
                if authorized is None:
                    raise _APIError(404, "group_not_found", "该群未授权或不存在。")
                group = await session.get(Group, group_id)
                if group is None:
                    group = Group(id=group_id, title="", settings={})
                    session.add(group)
                actual_revision = _group_revision(group.settings or {})
                if actual_revision != expected_revision:
                    raise _APIError(
                        409,
                        "group_revision_conflict",
                        "群设置已被其他会话更新，请刷新后重试。",
                    )
                previous_style_target = int(
                    get_style_state(group.settings).get("target_user_id") or 0
                )
                group.settings = _apply_group_settings(group.settings, update, settings)
                current_style_target = int(
                    get_style_state(group.settings).get("target_user_id") or 0
                )
                if current_style_target != previous_style_target:
                    await session.execute(
                        delete(SpeechStyleSample).where(
                            SpeechStyleSample.group_id == group_id
                        )
                    )
                await session.commit()
                document = _group_document(group.id, group.title, group.settings)
        return _success_response({"group": document})

    app.router.add_get("/api/v1/settings", get_settings)
    app.router.add_put("/api/v1/settings", put_settings)
    app.router.add_get("/api/v1/groups", get_groups)
    app.router.add_put("/api/v1/groups/{id}/settings", put_group_settings)


__all__ = ["register_settings_routes"]
