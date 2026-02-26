from __future__ import annotations

import time
from typing import Any, Awaitable, Callable

from aiogram import BaseMiddleware
from aiogram.types import Message


class ThrottleMiddleware(BaseMiddleware):
    def __init__(self, rate_limit: float = 1.0) -> None:
        self.rate_limit = rate_limit
        self._cache: dict[int, float] = {}

    async def __call__(
        self,
        handler: Callable[[Message, dict[str, Any]], Awaitable[Any]],
        event: Message,
        data: dict[str, Any],
    ) -> Any:
        user_id = event.from_user.id if event.from_user else 0
        now = time.monotonic()
        last = self._cache.get(user_id, 0.0)

        if now - last < self.rate_limit:
            return  # silently drop

        self._cache[user_id] = now
        return await handler(event, data)
