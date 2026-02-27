from __future__ import annotations

import logging
from typing import Any, Awaitable, Callable

from aiogram import BaseMiddleware
from aiogram.types import Message
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

log = logging.getLogger(__name__)


class DbSessionMiddleware(BaseMiddleware):
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self.session_factory = session_factory

    async def __call__(
        self,
        handler: Callable[[Message, dict[str, Any]], Awaitable[Any]],
        event: Message,
        data: dict[str, Any],
    ) -> Any:
        async with self.session_factory() as session:
            data["session"] = session
            result = await handler(event, data)
            try:
                await session.commit()
            except IntegrityError as exc:
                await session.rollback()
                detail = str(getattr(exc, "orig", exc))
                # Allow benign race when two fresh updates concurrently create same group row.
                if "UNIQUE constraint failed: groups.id" in detail:
                    log.warning("ignored duplicate groups.id race during commit")
                else:
                    raise
            return result
