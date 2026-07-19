"""Per-update completion hand-off between delivery and detached handlers.

Aiogram considers an update handled when its router coroutine returns.  Some
handlers deliberately move expensive work to a bounded background worker. Both
webhook and polling persist the raw update before transport acknowledgement;
this context-local receipt keeps that shared inbox row unfinished until detached
work reaches a terminal outcome.
"""

from __future__ import annotations

import asyncio
from contextvars import ContextVar, Token


class UpdateCompletionReceipt:
    """One update's optional deferred-completion signal."""

    __slots__ = ("_deferred", "_future")

    def __init__(self) -> None:
        self._deferred = False
        self._future: asyncio.Future[bool] = asyncio.get_running_loop().create_future()

    @property
    def deferred(self) -> bool:
        return self._deferred

    def defer(self) -> None:
        self._deferred = True

    def finish(self, succeeded: bool) -> None:
        if not self._future.done():
            self._future.set_result(bool(succeeded))

    async def wait(self) -> bool:
        # A worker deadline or transport switch must not cancel the shared
        # receipt. The durable inbox finalizer continues to own it regardless
        # of whether Telegram delivered the update by webhook or polling.
        return await asyncio.shield(self._future)


_CURRENT_UPDATE_COMPLETION: ContextVar[UpdateCompletionReceipt | None] = ContextVar(
    "current_update_completion",
    default=None,
)


def current_update_completion() -> UpdateCompletionReceipt | None:
    return _CURRENT_UPDATE_COMPLETION.get()


def request_current_update_retry() -> bool:
    """Keep the current durable inbox row pending for a later replay.

    Handlers use this only after committing their idempotency markers and any
    user-visible failure notice.  Returning normally avoids aiogram error
    middleware swallowing a synthetic exception, while the receipt's terminal
    ``False`` result makes the shared webhook/polling inbox release its lease
    with backoff instead of marking the update completed.
    """

    receipt = current_update_completion()
    if receipt is None:
        return False
    receipt.defer()
    receipt.finish(False)
    return True


def bind_update_completion(
    receipt: UpdateCompletionReceipt,
) -> Token[UpdateCompletionReceipt | None]:
    return _CURRENT_UPDATE_COMPLETION.set(receipt)


def reset_update_completion(token: Token[UpdateCompletionReceipt | None]) -> None:
    _CURRENT_UPDATE_COMPLETION.reset(token)
