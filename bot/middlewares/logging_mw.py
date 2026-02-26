from __future__ import annotations

import logging
from typing import Any, Awaitable, Callable

from aiogram import BaseMiddleware
from aiogram.types import Message

log = logging.getLogger(__name__)


class LoggingMiddleware(BaseMiddleware):
    async def __call__(
        self,
        handler: Callable[[Message, dict[str, Any]], Awaitable[Any]],
        event: Message,
        data: dict[str, Any],
    ) -> Any:
        user = event.from_user
        name = user.username or user.full_name if user else "unknown"
        chat = event.chat.title or event.chat.id if event.chat else "?"
        log.info("[%s] %s: %s", chat, name, (event.text or "")[:80])
        return await handler(event, data)
