from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from bot.middlewares.update_dedup import DurableInboxUpdateDedupMiddleware
from bot.services.update_completion import (
    UpdateCompletionReceipt,
    bind_update_completion,
    reset_update_completion,
)


class _SessionContext:
    def __init__(self, session: object) -> None:
        self.session = session

    async def __aenter__(self) -> object:
        return self.session

    async def __aexit__(self, *_args: object) -> None:
        return None


class DurableInboxUpdateDedupTests(unittest.IsolatedAsyncioTestCase):
    def _middleware(self, durable: object | None) -> tuple[DurableInboxUpdateDedupMiddleware, AsyncMock]:
        session = SimpleNamespace(get=AsyncMock(return_value=durable))
        middleware = DurableInboxUpdateDedupMiddleware(
            lambda: _SessionContext(session)  # type: ignore[arg-type]
        )
        return middleware, session.get

    async def test_polling_duplicate_owned_by_inbox_is_skipped(self) -> None:
        middleware, lookup = self._middleware(SimpleNamespace(update_id=101))
        handler = AsyncMock(return_value="handled")

        result = await middleware(handler, SimpleNamespace(update_id=101), {})

        self.assertIsNone(result)
        handler.assert_not_awaited()
        lookup.assert_awaited_once()

    async def test_polling_update_without_inbox_row_runs_normally(self) -> None:
        middleware, _lookup = self._middleware(None)
        handler = AsyncMock(return_value="handled")

        result = await middleware(handler, SimpleNamespace(update_id=102), {})

        self.assertEqual(result, "handled")
        handler.assert_awaited_once()

    async def test_inbox_dispatch_context_bypasses_its_own_dedup_row(self) -> None:
        middleware, lookup = self._middleware(SimpleNamespace(update_id=103))
        handler = AsyncMock(return_value="handled")
        receipt = UpdateCompletionReceipt()
        token = bind_update_completion(receipt)
        try:
            result = await middleware(handler, SimpleNamespace(update_id=103), {})
        finally:
            reset_update_completion(token)

        self.assertEqual(result, "handled")
        handler.assert_awaited_once()
        lookup.assert_not_awaited()

    async def test_lookup_failure_is_fail_closed(self) -> None:
        session = SimpleNamespace(get=AsyncMock(side_effect=RuntimeError("db offline")))
        middleware = DurableInboxUpdateDedupMiddleware(
            lambda: _SessionContext(session)  # type: ignore[arg-type]
        )
        handler = AsyncMock(return_value="unsafe duplicate")

        with self.assertRaisesRegex(
            RuntimeError,
            "deduplication is unavailable",
        ):
            await middleware(handler, SimpleNamespace(update_id=104), {})

        handler.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
