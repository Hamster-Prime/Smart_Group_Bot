"""Security-sensitive Telegram admin-status lookups.

Only non-admin results are cached. Positive admin status is always refreshed
from Telegram so a demoted administrator cannot retain moderation exemptions
or admin-only tool access for a local TTL window.
"""
from __future__ import annotations

import logging
import time

from aiogram.enums import ChatMemberStatus
from aiogram.types import Message

log = logging.getLogger(__name__)

_NON_ADMIN_STATUS_TTL_SECONDS = 60.0
_NON_ADMIN_STATUS_CACHE: dict[tuple[int, int], float] = {}


def invalidate_admin_status_cache(chat_id: int, user_id: int) -> None:
    """Drop a cached non-admin result when Telegram reports a member update."""
    _NON_ADMIN_STATUS_CACHE.pop((int(chat_id), int(user_id)), None)


async def is_user_admin_cached(message: Message) -> bool:
    """Return fresh positive admin status, caching only non-admin results.

    A cached positive result is an authorization grant, so even a short TTL
    creates a privilege-retention window after demotion. Telegram lookup
    failures therefore fail closed and never reuse a previous positive result.
    Non-admin results may be cached because stale denial cannot grant access;
    chat_member updates invalidate them eagerly on promotion.
    """
    chat = getattr(message, "chat", None)
    user = getattr(message, "from_user", None)
    if chat is None or user is None:
        return False
    if getattr(chat, "type", "") == "private":
        return True

    key = (int(chat.id), int(user.id))
    now = time.monotonic()
    cached_at = _NON_ADMIN_STATUS_CACHE.get(key)
    if cached_at is not None and now - cached_at < _NON_ADMIN_STATUS_TTL_SECONDS:
        return False

    try:
        member = await chat.get_member(user.id)
    except Exception:
        log.warning("[%s] admin status lookup failed | user=%s", chat.id, user.id)
        return False

    result = member.status in (ChatMemberStatus.CREATOR, ChatMemberStatus.ADMINISTRATOR)
    if result:
        _NON_ADMIN_STATUS_CACHE.pop(key, None)
        return True

    if len(_NON_ADMIN_STATUS_CACHE) > 2048:
        _NON_ADMIN_STATUS_CACHE.clear()
    _NON_ADMIN_STATUS_CACHE[key] = now
    return False
