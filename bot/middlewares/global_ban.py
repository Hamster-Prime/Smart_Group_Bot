from __future__ import annotations

import logging
from typing import Any, Awaitable, Callable

from aiogram import BaseMiddleware
from aiogram.types import Message
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from bot.services.authz import is_group_authorized, is_super_admin_user_id
from bot.services.join_screening import is_globally_banned

log = logging.getLogger(__name__)


class GlobalBanEnforcementMiddleware(BaseMiddleware):
    """Blocks every group message from globally banned users before any handler.

    Registered as an OUTER middleware on the message observer, so it runs for
    every incoming group message — commands, media, polls, locations — even
    when no handler matches. It opens its own short-lived session because
    outer middlewares run before DbSessionMiddleware. Enforcement deletes the
    message and re-bans the user in that chat. Unauthorized groups are
    skipped entirely.
    """

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self.session_factory = session_factory

    async def __call__(
        self,
        handler: Callable[[Message, dict[str, Any]], Awaitable[Any]],
        event: Message,
        data: dict[str, Any],
    ) -> Any:
        chat = getattr(event, "chat", None)
        user = getattr(event, "from_user", None)
        if (
            chat is None
            or getattr(chat, "type", "") not in ("group", "supergroup")
            or user is None
            or getattr(event, "sender_chat", None) is not None
        ):
            return await handler(event, data)

        settings = data.get("settings")
        if settings is not None and is_super_admin_user_id(user.id, settings):
            return await handler(event, data)

        try:
            async with self.session_factory() as session:
                if not await is_group_authorized(session, chat.id):
                    return await handler(event, data)
                banned = await is_globally_banned(session, user.id)
                if not banned:
                    return await handler(event, data)
        except Exception:
            log.exception("global ban check failed | chat=%s user=%s", chat.id, user.id)
            return await handler(event, data)

        log.info("[%s] blocked message from globally banned user %s", chat.id, user.id)
        try:
            await event.delete()
        except Exception:
            pass
        try:
            await event.chat.ban(user.id)
        except Exception:
            log.warning("[%s] global ban enforcement failed | user=%s", chat.id, user.id)
        return None
