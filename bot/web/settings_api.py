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
from bot.db.models import (
    Admin,
    AuthorizedGroup,
    Group,
    GroupPermanentMemory,
    ModerationExemption,
    ModerationRule,
    ReplyMute,
    SpeechStyleSample,
    UserWarning,
)
from bot.services.at_reply import is_at_reply_enabled, set_at_reply_enabled
from bot.services.doubao_tts import normalize_tts_mode, set_tts_mode
from bot.services.proactive import (
    get_cooldown_task_state,
    set_cooldown_task_enabled,
)
from bot.services.join_screening import add_global_ban, list_global_bans, remove_global_ban
from bot.services.join_screening import is_globally_banned
from bot.services.join_verification import (
    delete_join_verification,
    delete_join_verifications_for_user,
    join_verification_policy,
    restore_member_permissions,
    turnstile_verification_configured,
    verification_provider,
    verification_service_ready,
)
from bot.services.patrol import get_patrol_service, patrol_policy
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
from bot.web.auth import require_authenticated_user, require_super_admin

log = logging.getLogger(__name__)

_GROUP_SETTING_FIELDS = {
    "av_enabled",
    "mute_all_replies",
    "at_reply_mode",
    "tts_mode",
    "join_verification_enabled",
    "join_verification_provider",
    "patrol_enabled",
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
    # None clears the group override and inherits the global default.
    join_verification_enabled: StrictBool | None = None
    join_verification_provider: (
        Literal["turnstile", "hcaptcha", "turnstile_hcaptcha"] | None
    ) = None
    patrol_enabled: StrictBool | None = None
    proactive_enabled: StrictBool | None = None
    proactive_task_brief: str | None = Field(default=None, max_length=240)
    mimic_target_user_id: StrictInt | None = Field(default=None, ge=0)
    mimic_target_user_name: str | None = Field(default=None, max_length=80)
    mimic_profile_text: str | None = Field(default=None, max_length=1200)


class _RuleCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    rule_type: Literal["keyword", "regex", "llm"]
    pattern: str = Field(min_length=1, max_length=1000)
    action: Literal["warn", "delete", "ban"] = "warn"
    enabled: StrictBool = True


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
        "patrol_enabled": (
            _setting_bool(settings_data, "patrol_enabled")
            if settings_data.get("patrol_enabled") is not None
            else None
        ),
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


def _rule_document(rule: ModerationRule) -> dict[str, Any]:
    return {
        "id": int(rule.id),
        "rule_type": str(rule.rule_type or "keyword"),
        "pattern": str(rule.pattern or ""),
        "action": str(rule.action or "warn"),
        "enabled": bool(rule.enabled),
    }


def _memory_document(memory: GroupPermanentMemory) -> dict[str, Any]:
    return {
        "id": int(memory.id),
        "content": str(memory.content or ""),
        "created_by": int(memory.created_by or 0),
        "created_at": memory.created_at.isoformat() if memory.created_at else "",
        "updated_at": memory.updated_at.isoformat() if memory.updated_at else "",
    }


def _user_policy_document(row: Any) -> dict[str, Any]:
    payload = {"user_id": int(row.user_id)}
    if isinstance(row, UserWarning):
        payload.update({"count": int(row.count or 0), "is_banned": bool(row.is_banned)})
    if hasattr(row, "created_by"):
        payload["created_by"] = int(row.created_by or 0)
    if hasattr(row, "created_at"):
        payload["created_at"] = row.created_at.isoformat() if row.created_at else ""
    return payload


def _ban_document(row: UserWarning) -> dict[str, Any]:
    """Expose group-ban state without leaking global moderation thresholds."""
    return {"user_id": int(row.user_id), "is_banned": bool(row.is_banned)}


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
            "patrol_enabled",
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
            .where(Admin.user_id == int(user_id))
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
        return _success_response({"deleted": row is not None})

    @authenticated
    async def list_group_admins_api(request: web.Request, _user: Any) -> web.Response:
        group_id = int(request.match_info["id"])
        async with session_factory() as session:
            rows = (await session.scalars(
                select(Admin).where(Admin.group_id == group_id).order_by(Admin.id)
            )).all()
        return _success_response({"admins": [
            {"user_id": int(row.user_id), "role": str(row.role or "admin")}
            for row in rows
        ]})

    @authenticated
    async def create_group_admin_api(request: web.Request, _user: Any) -> web.Response:
        group_id = int(request.match_info["id"])
        body = _AdminCreate.model_validate(await _json_object(request))
        async with session_factory() as session:
            if await session.get(AuthorizedGroup, group_id) is None:
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
            await session.commit()
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
        ban_member = getattr(bot, "ban_chat_member", None)
        if not callable(ban_member):
            raise _APIError(503, "telegram_unavailable", "当前无法调用 Telegram 封禁接口。")
        previous_warning: tuple[int, bool] | None = None
        async with session_factory() as session:
            await add_global_ban(
                session,
                body.user_id,
                reason=clean_multiline_text(body.reason, max_len=1000).strip() or "手动封禁",
                source="manual",
                created_by=int(user.id),
            )
            await delete_join_verifications_for_user(session, body.user_id)
            group_ids = [int(value) for value in (await session.scalars(select(AuthorizedGroup.group_id))).all()]
            await session.commit()
        succeeded = 0
        failures = 0
        for group_id in group_ids:
            try:
                await ban_member(group_id, body.user_id)
                succeeded += 1
            except Exception:
                failures += 1
                log.info("web ban failed | group=%s user=%s", group_id, body.user_id)
        if failures:
            raise _APIError(
                502,
                "telegram_ban_partial",
                f"已处理 {succeeded}/{len(group_ids)} 个群，但仍有群封禁失败，请重试。",
            )
        return _success_response({"banned_groups": succeeded, "total_groups": len(group_ids)})

    @authenticated
    async def delete_global_ban_api(request: web.Request, user: Any) -> web.Response:
        target_id = _path_int(request, "user_id")
        unban_member = getattr(bot, "unban_chat_member", None)
        if not callable(unban_member):
            raise _APIError(503, "telegram_unavailable", "当前无法调用 Telegram 解封接口。")
        async with session_factory() as session:
            group_ids = [int(value) for value in (await session.scalars(select(AuthorizedGroup.group_id))).all()]
        succeeded = 0
        restored = 0
        failures = 0
        for group_id in group_ids:
            try:
                await unban_member(group_id, target_id, only_if_banned=True)
                succeeded += 1
                if await restore_member_permissions(bot, group_id, target_id):
                    restored += 1
                else:
                    failures += 1
            except Exception:
                failures += 1
                log.info("web unban failed | group=%s user=%s", group_id, target_id)
        if failures:
            raise _APIError(
                502,
                "telegram_unban_partial",
                f"已处理 {succeeded}/{len(group_ids)} 个群，但仍有群解封失败，请重试。",
            )
        async with session_factory() as session:
            removed = await remove_global_ban(session, target_id, operator_id=int(user.id))
            await delete_join_verifications_for_user(session, target_id)
            await session.execute(delete(UserWarning).where(UserWarning.user_id == target_id))
            await session.commit()
        return _success_response({
            "removed": removed,
            "unbanned_groups": succeeded,
            "restored_groups": restored,
            "total_groups": len(group_ids),
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
        return _success_response({"groups": groups})

    @any_admin
    async def put_group_settings(request: web.Request, user: Any) -> web.Response:
        try:
            group_id = int(request.match_info["id"])
        except (KeyError, TypeError, ValueError) as exc:
            raise _APIError(400, "invalid_group_id", "群 ID 无效。") from exc
        await _require_group_access(group_id, int(user.id))

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
                stmt = stmt.where(UserWarning.count > 0)
            rows = (await session.scalars(stmt)).all()
        return _success_response({key: [_user_policy_document(row) for row in rows]})

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
        return _success_response({"bans": [_ban_document(row) for row in rows]})

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
        previous_warning: tuple[int, bool] | None = None
        async with session_factory() as session:
            if await is_globally_banned(session, body.user_id):
                raise _APIError(
                    409,
                    "group_ban_locked",
                    "该用户的封禁状态只能由最高管理员处理。",
                )
            row = await session.scalar(select(UserWarning).where(
                UserWarning.group_id == group_id,
                UserWarning.user_id == body.user_id,
            ))
            if row is not None:
                previous_warning = (int(row.count or 0), bool(row.is_banned))
            if row is None:
                row = UserWarning(
                    group_id=group_id,
                    user_id=body.user_id,
                    count=0,
                    is_banned=True,
                )
                session.add(row)
            else:
                row.is_banned = True
            await session.commit()
            await session.refresh(row)
        try:
            await ban_member(group_id, body.user_id)
        except Exception as exc:
            async with session_factory() as session:
                current = await session.scalar(select(UserWarning).where(
                    UserWarning.group_id == group_id,
                    UserWarning.user_id == body.user_id,
                ))
                if current is not None:
                    if previous_warning is None:
                        await session.delete(current)
                    else:
                        current.count, current.is_banned = previous_warning
                await session.commit()
            raise _APIError(502, "telegram_ban_failed", "Telegram 群内封禁失败，请稍后重试。") from exc
        async with session_factory() as session:
            await delete_join_verification(session, group_id, body.user_id)
            await session.commit()
        return _success_response({"ban": _ban_document(row)})

    @any_admin
    async def delete_group_ban(request: web.Request, user: Any) -> web.Response:
        group_id = _group_id(request)
        await _require_group_access(group_id, int(user.id))
        target_id = _path_int(request, "user_id")
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
            if row is None or not row.is_banned:
                return _success_response({"deleted": False, "restored": False})
        unban_member = getattr(bot, "unban_chat_member", None)
        if not callable(unban_member):
            raise _APIError(503, "telegram_unavailable", "当前无法调用 Telegram 解封接口。")
        try:
            await unban_member(group_id, target_id, only_if_banned=True)
        except Exception as exc:
            raise _APIError(502, "telegram_unban_failed", "Telegram 群内解封失败，请稍后重试。") from exc
        restored = await restore_member_permissions(bot, group_id, target_id)
        if not restored:
            raise _APIError(
                502,
                "telegram_restore_failed",
                "群内解封成功，但恢复发言权限失败；请重试解封操作。",
            )
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
            if row is not None:
                await session.delete(row)
            await session.commit()
        return _success_response({"deleted": row is not None, "restored": True})

    async def _create_user_row(
        request: web.Request,
        user: Any,
        model: type[Any],
        key: str,
    ) -> web.Response:
        group_id = _group_id(request)
        await _require_group_access(group_id, int(user.id))
        body = _UserIdCreate.model_validate(await _json_object(request))
        async with session_factory() as session:
            row = await session.scalar(select(model).where(
                model.group_id == group_id,
                model.user_id == body.user_id,
            ))
            if row is None:
                row = model(group_id=group_id, user_id=body.user_id, created_by=int(user.id))
                session.add(row)
                await session.commit()
                await session.refresh(row)
        return _success_response({key: _user_policy_document(row)})

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
    app.router.add_post("/api/v1/groups/{id}/admins", create_group_admin_api)
    app.router.add_delete("/api/v1/groups/{id}/admins/{user_id}", delete_group_admin_api)
    app.router.add_get("/api/v1/global-bans", list_global_bans_api)
    app.router.add_post("/api/v1/global-bans", create_global_ban_api)
    app.router.add_delete("/api/v1/global-bans/{user_id}", delete_global_ban_api)
    app.router.add_get("/api/v1/groups", get_groups)
    app.router.add_put("/api/v1/groups/{id}/settings", put_group_settings)
    app.router.add_get("/api/v1/groups/{id}/rules", list_rules)
    app.router.add_post("/api/v1/groups/{id}/rules", create_rule)
    app.router.add_patch("/api/v1/groups/{id}/rules/{rule_id}", update_rule)
    app.router.add_delete("/api/v1/groups/{id}/rules/{rule_id}", delete_rule)
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
