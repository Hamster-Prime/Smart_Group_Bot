import importlib.util
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class MessageEntrypointTests(unittest.TestCase):
    def test_unified_entrypoint_has_no_silent_drop_throttle(self) -> None:
        launcher = (ROOT / "start.py").read_text(encoding="utf-8")
        entrypoint = (ROOT / "bot" / "__main__.py").read_text(encoding="utf-8")

        self.assertIn("from bot.__main__ import main", launcher)
        self.assertNotIn("ThrottleMiddleware", entrypoint)
        self.assertNotIn("bot.middlewares.throttle", entrypoint)
        self.assertIsNone(importlib.util.find_spec("bot.middlewares.throttle"))


if __name__ == "__main__":
    unittest.main()
