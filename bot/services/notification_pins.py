"""Best-effort pin lifecycle helpers for group-wide notifications."""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Literal

from aiogram.exceptions import TelegramBadRequest

from bot.config import Settings

log = logging.getLogger(__name__)

NotificationPinKind = Literal["vote_ban", "raid_guard", "call_admin"]

_PIN_SETTING_KEYS: dict[NotificationPinKind, tuple[str, bool]] = {
    "vote_ban": ("vote_ban_pin_message", True),
    "raid_guard": ("raid_guard_pin_message", True),
    "call_admin": ("call_admin_pin_message", False),
}

_PIN_API_TIMEOUT_SECONDS = 10.0
_ALREADY_UNPINNED_MARKERS = (
    "message is not pinned",
    "message not found",
    "message to unpin not found",
    "message to be unpinned not found",
    "message_id_invalid",
)


def _already_unpinned(exc: TelegramBadRequest) -> bool:
    detail = str(exc or "").strip().lower()
    return any(marker in detail for marker in _ALREADY_UNPINNED_MARKERS)


def notification_pin_enabled(
    settings: Settings,
    group_settings: dict[str, Any] | None,
    kind: NotificationPinKind,
) -> bool:
    """Resolve a nullable per-group override against its global default."""

    key, fallback = _PIN_SETTING_KEYS[kind]
    default = bool(getattr(settings, key, fallback))
    if not isinstance(group_settings, dict) or group_settings.get(key) is None:
        return default
    raw = group_settings.get(key)
    if isinstance(raw, str):
        normalized = raw.strip().lower()
        if normalized in {"1", "true", "yes", "on", "enable", "enabled"}:
            return True
        if normalized in {"0", "false", "no", "off", "disable", "disabled"}:
            return False
        return default
    return bool(raw)


async def pin_notification_message(
    bot: Any,
    *,
    chat_id: int,
    message_id: int,
    kind: NotificationPinKind,
) -> bool:
    """Pin one exact message without making the primary workflow fail."""

    method = getattr(bot, "pin_chat_message", None)
    if not callable(method) or int(message_id or 0) <= 0:
        return False
    try:
        async with asyncio.timeout(_PIN_API_TIMEOUT_SECONDS):
            await method(
                chat_id=int(chat_id),
                message_id=int(message_id),
                disable_notification=True,
            )
        return True
    except asyncio.CancelledError:
        raise
    except Exception:
        log.warning(
            "notification pin failed | kind=%s chat=%s message=%s",
            kind,
            chat_id,
            message_id,
            exc_info=True,
        )
        return False


async def unpin_notification_message(
    bot: Any,
    *,
    chat_id: int,
    message_id: int,
    kind: NotificationPinKind,
) -> bool:
    """Unpin one exact message without disturbing other pinned messages."""

    method = getattr(bot, "unpin_chat_message", None)
    if not callable(method) or int(message_id or 0) <= 0:
        return False
    try:
        async with asyncio.timeout(_PIN_API_TIMEOUT_SECONDS):
            await method(chat_id=int(chat_id), message_id=int(message_id))
        return True
    except asyncio.CancelledError:
        raise
    except TelegramBadRequest as exc:
        if _already_unpinned(exc):
            log.debug(
                "notification already unpinned | kind=%s chat=%s message=%s",
                kind,
                chat_id,
                message_id,
            )
            return True
        log.warning(
            "notification unpin rejected | kind=%s chat=%s message=%s",
            kind,
            chat_id,
            message_id,
            exc_info=True,
        )
        return False
    except Exception:
        log.warning(
            "notification unpin failed | kind=%s chat=%s message=%s",
            kind,
            chat_id,
            message_id,
            exc_info=True,
        )
        return False


__all__ = [
    "NotificationPinKind",
    "notification_pin_enabled",
    "pin_notification_message",
    "unpin_notification_message",
]
