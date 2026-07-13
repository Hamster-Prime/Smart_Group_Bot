from __future__ import annotations

import json
import math
from datetime import datetime, timezone
from typing import Any

from aiohttp import web
from aiogram.utils.web_app import safe_parse_webapp_init_data

MAX_INIT_DATA_AGE_SECONDS = 10 * 60
MAX_INIT_DATA_FUTURE_SECONDS = 60


def _auth_error(
    *,
    status: int,
    code: str,
    message: str,
) -> web.HTTPException:
    payload = json.dumps(
        {"ok": False, "error": {"code": code, "message": message}},
        ensure_ascii=False,
    )
    exception_type: type[web.HTTPException]
    if status == 403:
        exception_type = web.HTTPForbidden
    else:
        exception_type = web.HTTPUnauthorized
    return exception_type(
        text=payload,
        content_type="application/json",
        headers={"Cache-Control": "no-store"},
    )


def _auth_timestamp(value: Any) -> float:
    timestamp: float
    if isinstance(value, datetime):
        parsed = value
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        timestamp = parsed.timestamp()
    elif isinstance(value, bool):
        raise ValueError("invalid auth_date")
    elif isinstance(value, (int, float)):
        timestamp = float(value)
    else:
        text = str(value or "").strip()
        if not text:
            raise ValueError("missing auth_date")
        timestamp = float(text)
    if not math.isfinite(timestamp):
        raise ValueError("invalid auth_date")
    return timestamp


async def require_super_admin(
    request: web.Request,
    bot_token: str,
    super_admin_id: int,
) -> Any:
    """Authenticate a fresh Telegram Mini App session and return its user.

    The custom ``tma`` authorization scheme carries Telegram's raw initData.
    Signature verification always happens before trusting its timestamp or user.
    """

    user = await require_authenticated_user(request, bot_token)
    try:
        user_id = int(user.id)
    except (AttributeError, TypeError, ValueError) as exc:
        raise _auth_error(
            status=401,
            code="invalid_user",
            message="Telegram 用户信息无效。",
        ) from exc
    if not super_admin_id or user_id != int(super_admin_id):
        raise _auth_error(
            status=403,
            code="super_admin_required",
            message="仅最高管理员可访问此页面。",
        )
    return user


async def require_authenticated_user(
    request: web.Request,
    bot_token: str,
) -> Any:
    """Authenticate a fresh Telegram Mini App session without assigning a role."""
    authorization = str(request.headers.get("Authorization") or "").strip()
    parts = authorization.split(None, 1)
    if len(parts) != 2 or parts[0].lower() != "tma" or not parts[1].strip():
        raise _auth_error(
            status=401,
            code="authorization_required",
            message="请从 Telegram Mini App 重新打开管理页面。",
        )

    try:
        parsed = safe_parse_webapp_init_data(bot_token, parts[1].strip())
        auth_timestamp = _auth_timestamp(parsed.auth_date)
    except (ValueError, AttributeError, TypeError, OverflowError, OSError) as exc:
        raise _auth_error(
            status=401,
            code="invalid_init_data",
            message="Telegram 会话签名或时间校验失败。",
        ) from exc

    age_seconds = datetime.now(timezone.utc).timestamp() - auth_timestamp
    if age_seconds > MAX_INIT_DATA_AGE_SECONDS:
        raise _auth_error(
            status=401,
            code="init_data_expired",
            message="Telegram 会话已过期，请重新打开管理页面。",
        )
    if age_seconds < -MAX_INIT_DATA_FUTURE_SECONDS:
        raise _auth_error(
            status=401,
            code="init_data_from_future",
            message="Telegram 会话时间异常，请重新打开管理页面。",
        )

    user = getattr(parsed, "user", None)
    try:
        int(user.id)
    except (AttributeError, TypeError, ValueError) as exc:
        raise _auth_error(
            status=401,
            code="invalid_user",
            message="Telegram 用户信息无效。",
        ) from exc
    return user
