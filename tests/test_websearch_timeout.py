import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from bot.services.skills import platform_common, websearch
from bot.services.skills.platform_common import site_search
from bot.services.skills.websearch import WebSearchSkill


class WebSearchTimeoutTests(unittest.IsolatedAsyncioTestCase):
    async def test_websearch_converts_thread_timeouts_to_bounded_failure(self) -> None:
        runner = AsyncMock(side_effect=TimeoutError)
        with patch.object(websearch, "run_search_thread", new=runner):
            result = await WebSearchSkill().run(
                {"query": "test query", "max_results": 3},
                SimpleNamespace(),
            )

        self.assertFalse(result.ok)
        self.assertEqual(result.error, "search_timeout")
        self.assertLessEqual(runner.await_count, 8)

    async def test_site_search_converts_thread_timeouts_to_empty_result(self) -> None:
        runner = AsyncMock(side_effect=TimeoutError)
        with patch.object(platform_common, "run_search_thread", new=runner):
            rows, effective = await site_search(
                "test query",
                query_candidates=["test query"],
                max_results=3,
            )

        self.assertEqual(rows, [])
        self.assertEqual(effective, "test query")
        self.assertLessEqual(runner.await_count, 2)


if __name__ == "__main__":
    unittest.main()
