from __future__ import annotations

import logging
from typing import Any, Awaitable, Callable

from aiogram import BaseMiddleware
from aiogram.types import Update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from bot.db.models import WebhookInboxUpdate
from bot.services.update_completion import current_update_completion

log = logging.getLogger(__name__)


class DurableInboxUpdateDedupMiddleware(BaseMiddleware):
    """Prevent a lost webhook ACK from being executed again by polling.

    The durable inbox binds an update-completion receipt while it deliberately
    feeds a stored update back through the dispatcher.  Only that path may
    bypass this lookup.  A polling delivery with the same update_id is skipped
    whether the inbox row is pending, completed, or dead-lettered: the inbox is
    the authoritative owner once persistence succeeded.
    """

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self.session_factory = session_factory

    async def __call__(
        self,
        handler: Callable[[Update, dict[str, Any]], Awaitable[Any]],
        event: Update,
        data: dict[str, Any],
    ) -> Any:
        if current_update_completion() is not None:
            return await handler(event, data)

        raw_update_id = getattr(event, "update_id", None)
        if isinstance(raw_update_id, bool) or not isinstance(raw_update_id, int):
            return await handler(event, data)

        try:
            async with self.session_factory() as session:
                durable = await session.get(WebhookInboxUpdate, int(raw_update_id))
        except Exception as exc:
            # Never fail open here. Executing while the durable ownership lookup
            # is unavailable can duplicate moderation, bans, replies, or memory
            # writes after a lost webhook ACK. The transport must retry once the
            # database is healthy.
            log.exception(
                "durable inbox polling dedup lookup failed | update=%s",
                raw_update_id,
            )
            raise RuntimeError(
                "durable Telegram update deduplication is unavailable"
            ) from exc

        if durable is not None:
            log.warning(
                "skipped polling duplicate already owned by durable inbox | update=%s",
                raw_update_id,
            )
            return None
        return await handler(event, data)
