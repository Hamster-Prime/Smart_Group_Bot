"""Group member roster maintenance from message traffic.

The Bot API cannot enumerate chat members, so the profile patrol scans the
group_members table instead. This outer middleware upserts a roster row for
every group message sender (cheap: a process-level cache skips the write
unless the visible profile changed). Joins/leaves are tracked by the
membership handlers; retained dialogue history seeds the roster once per
group inside the patrol service.
"""
from __future__ import annotations

import logging
from typing import Any, Awaitable, Callable

from aiogram import BaseMiddleware
from aiogram.types import Message
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from bot.services.patrol import track_group_member_cached

log = logging.getLogger(__name__)


class MemberRosterMiddleware(BaseMiddleware):
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
            chat is not None
            and getattr(chat, "type", "") in ("group", "supergroup")
            and user is not None
            and getattr(event, "sender_chat", None) is None
        ):
            try:
                await track_group_member_cached(self.session_factory, chat.id, user)
            except Exception:
                log.debug(
                    "member roster tracking failed | chat=%s user=%s",
                    chat.id,
                    getattr(user, "id", "-"),
                    exc_info=True,
                )
        return await handler(event, data)
