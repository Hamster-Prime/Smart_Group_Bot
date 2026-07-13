import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from bot.config import ModerationConfig
from bot.db.models import ModerationRule
from bot.services.join_screening import screen_member_profile_verbose
from bot.services.moderation import ModerationService


class _NoAutoflush:
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False


class _RowsResult:
    def __init__(self, rows: list[object]) -> None:
        self.rows = rows

    def scalars(self) -> "_RowsResult":
        return self

    def all(self) -> list[object]:
        return self.rows


class ModerationConfidenceTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.rule = ModerationRule(
            id=7,
            group_id=-100,
            rule_type="llm",
            pattern="No advertising",
            action="ban",
            enabled=True,
        )
        self.session = SimpleNamespace(
            no_autoflush=_NoAutoflush(),
            execute=AsyncMock(return_value=_RowsResult([self.rule])),
            get=AsyncMock(return_value=None),
        )

    def _service(
        self,
        response: str | list[str],
        *,
        threshold: float = 0.8,
    ) -> ModerationService:
        moderation = (
            AsyncMock(side_effect=response)
            if isinstance(response, list)
            else AsyncMock(return_value=response)
        )
        llm = SimpleNamespace(moderation=moderation)
        config = ModerationConfig(high_confidence_threshold=threshold)
        return ModerationService(config, llm)

    async def test_valid_confidence_respects_threshold_inclusively(self) -> None:
        cases = (
            (0.81, True),
            (0.79, False),
            (0.8, True),
        )

        for confidence, expected_high in cases:
            with self.subTest(confidence=confidence):
                service = self._service(
                    '{"violated": true, "rule_id": 7, '
                    f'"reason": "matched", "confidence": {confidence}'
                    "}"
                )

                verdict = await service.evaluate(self.session, -100, "message")

                self.assertTrue(verdict.violated)
                self.assertTrue(verdict.conclusive)
                self.assertEqual(verdict.confidence, confidence)
                self.assertEqual(service.is_high_confidence(verdict), expected_high)

    async def test_invalid_confidence_can_never_be_high_confidence(self) -> None:
        responses = {
            "missing": '{"violated": true, "rule_id": 7, "reason": "matched"}',
            "string": (
                '{"violated": true, "rule_id": 7, "reason": "matched", '
                '"confidence": "0.99"}'
            ),
            "nan": (
                '{"violated": true, "rule_id": 7, "reason": "matched", '
                '"confidence": NaN}'
            ),
            "infinity": (
                '{"violated": true, "rule_id": 7, "reason": "matched", '
                '"confidence": Infinity}'
            ),
            "negative_infinity": (
                '{"violated": true, "rule_id": 7, "reason": "matched", '
                '"confidence": -Infinity}'
            ),
            "below_range": (
                '{"violated": true, "rule_id": 7, "reason": "matched", '
                '"confidence": -0.01}'
            ),
            "above_range": (
                '{"violated": true, "rule_id": 7, "reason": "matched", '
                '"confidence": 1.01}'
            ),
        }

        for label, response in responses.items():
            with self.subTest(confidence=label):
                service = self._service(response, threshold=0.0)

                verdict = await service.evaluate(self.session, -100, "message")

                self.assertTrue(verdict.violated)
                self.assertFalse(verdict.conclusive)
                self.assertEqual(verdict.confidence, 0.0)
                self.assertFalse(service.is_high_confidence(verdict))

    async def test_string_false_violated_is_inconclusive_clean(self) -> None:
        service = self._service(
            '{"violated": "false", "rule_id": null, '
            '"reason": "clean", "confidence": 0.99}'
        )

        verdict = await service.evaluate(self.session, -100, "message")

        self.assertFalse(verdict.violated)
        self.assertFalse(verdict.conclusive)
        self.assertEqual(verdict.reason, "")
        self.assertIsNone(verdict.rule)
        self.assertEqual(verdict.confidence, 0.0)
        self.assertFalse(service.is_high_confidence(verdict))

    async def test_truncated_json_salvages_scientific_confidence(self) -> None:
        service = self._service(
            '{"violated": true, "rule_id": 7, "reason": "matched", '
            '"confidence": 1e-1,'
        )

        verdict = await service.evaluate(self.session, -100, "message")

        self.assertTrue(verdict.violated)
        self.assertTrue(verdict.conclusive)
        self.assertIs(verdict.rule, self.rule)
        self.assertAlmostEqual(verdict.confidence, 0.1)
        self.assertFalse(service.is_high_confidence(verdict))

    async def test_profile_screen_ignores_low_confidence_but_keeps_high_match(self) -> None:
        service = self._service(
            [
                '{"violated": true, "rule_id": 7, "reason": "profile ad", '
                '"confidence": 0.79}',
                '{"violated": true, "rule_id": 7, "reason": "profile ad", '
                '"confidence": 0.8}',
            ]
        )

        low = await screen_member_profile_verbose(
            self.session,
            service,
            group_id=-100,
            user_id=42,
            profile_text="advertising profile",
        )
        high = await screen_member_profile_verbose(
            self.session,
            service,
            group_id=-100,
            user_id=42,
            profile_text="advertising profile",
        )

        self.assertEqual(low, (False, "", True))
        self.assertEqual(high, (True, "profile ad", True))
        self.assertEqual(service.llm.moderation.await_count, 2)
        for call in service.llm.moderation.await_args_list:
            self.assertIn("[\u6210\u5458\u8d44\u6599\u5ba1\u6838]", call.args[1])


if __name__ == "__main__":
    unittest.main()
