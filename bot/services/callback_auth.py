from __future__ import annotations

import logging

from aiogram import Bot
from aiogram.enums import ChatMemberStatus
from sqlalchemy.ext.asyncio import AsyncSession

from bot.config import Settings
from bot.services.authz import is_group_admin_authorized, is_super_admin_user_id
from bot.services.update_delivery import mark_privileged_operator

log = logging.getLogger(__name__)


async def is_group_admin_or_higher(
    *,
    bot: Bot,
    session: AsyncSession,
    settings: Settings,
    group_id: int,
    user_id: int,
) -> bool:
    """Authorize callback actions without producing user-visible side effects."""
    try:
        group_id = int(group_id)
        user_id = int(user_id)
    except (TypeError, ValueError):
        return False
    if not group_id or not user_id:
        return False

    if is_super_admin_user_id(user_id, settings):
        mark_privileged_operator(user_id)
        return True

    local_authorized = False
    try:
        local_authorized = await is_group_admin_authorized(
            session,
            group_id,
            user_id,
        )
    except Exception:
        log.warning(
            "local callback admin lookup failed | group=%s user=%s",
            group_id,
            user_id,
            exc_info=True,
        )
        try:
            await session.rollback()
        except Exception:
            log.debug(
                "callback authorization rollback failed | group=%s user=%s",
                group_id,
                user_id,
                exc_info=True,
            )
    else:
        # The fallback below calls Telegram over the network. End the local
        # authorization snapshot first so a slow Bot API request cannot pin a
        # pooled connection (or a SQLite read transaction) for its duration.
        try:
            await session.commit()
        except Exception:
            log.warning(
                "callback authorization transaction release failed | "
                "group=%s user=%s",
                group_id,
                user_id,
                exc_info=True,
            )
            try:
                await session.rollback()
            except Exception:
                pass
            return False

    if local_authorized:
        mark_privileged_operator(user_id, group_id=group_id)
        return True

    try:
        member = await bot.get_chat_member(group_id, user_id)
    except Exception:
        log.warning(
            "Telegram callback admin lookup failed | group=%s user=%s",
            group_id,
            user_id,
            exc_info=True,
        )
        return False

    status = getattr(member, "status", None)
    if status == ChatMemberStatus.CREATOR:
        mark_privileged_operator(user_id, group_id=group_id)
        return True
    if status == ChatMemberStatus.ADMINISTRATOR:
        allowed = bool(getattr(member, "can_restrict_members", False))
        if allowed:
            mark_privileged_operator(user_id, group_id=group_id)
        return allowed
    return False
