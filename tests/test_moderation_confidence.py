import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

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
            commit=AsyncMock(),
        )

    def _service(
        self,
        response: str | list[str] | Exception,
        *,
        threshold: float = 0.8,
    ) -> ModerationService:
        if isinstance(response, (list, Exception)):
            moderation = AsyncMock(side_effect=response)
        else:
            moderation = AsyncMock(return_value=response)
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
        self.assertEqual(verdict.rule.id, self.rule.id)
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
    async def test_keyword_rule_matches_locally_without_llm(self) -> None:
        keyword = ModerationRule(
            id=8,
            group_id=-100,
            rule_type="keyword",
            pattern="SpamOffer",
            action="delete",
            enabled=True,
        )
        llm_rule = ModerationRule(
            id=9,
            group_id=-100,
            rule_type="llm",
            pattern="No semantic advertising",
            action="ban",
            enabled=True,
        )
        self.session.execute.return_value = _RowsResult([keyword, llm_rule])
        service = self._service(RuntimeError("LLM must not run"))

        verdict = await service.evaluate(self.session, -100, "prefix spamoffer suffix")

        self.assertTrue(verdict.violated)
        self.assertTrue(verdict.conclusive)
        self.assertEqual(verdict.confidence, 1.0)
        self.assertEqual(verdict.rule.id, keyword.id)
        service.llm.moderation.assert_not_awaited()

    async def test_regex_rule_matches_locally_without_llm(self) -> None:
        regex_rule = ModerationRule(
            id=10,
            group_id=-100,
            rule_type="regex",
            pattern=r"\b(?:buy|sell)-\d{4}\b",
            action="warn",
            enabled=True,
        )
        self.session.execute.return_value = _RowsResult([regex_rule])
        service = self._service(RuntimeError("LLM must not run"))

        verdict = await service.evaluate(self.session, -100, "BUY-2026 now")

        self.assertTrue(verdict.violated)
        self.assertEqual(verdict.rule.id, regex_rule.id)
        service.llm.moderation.assert_not_awaited()

    async def test_regex_rule_matches_telegram_command_alias_locally(self) -> None:
        regex_rule = ModerationRule(
            id=14,
            group_id=-100,
            rule_type="regex",
            pattern=r"^(?:lucky_checkin|aq_lucky_bot)$",
            action="delete",
            enabled=True,
        )
        self.session.execute.return_value = _RowsResult([regex_rule])
        service = self._service(RuntimeError("LLM must not run"))

        verdict = await service.evaluate(
            self.session,
            -100,
            "/lucky_checkin@aq_lucky_bot",
        )

        self.assertTrue(verdict.violated)
        self.assertTrue(verdict.conclusive)
        self.assertEqual(verdict.confidence, 1.0)
        self.assertEqual(verdict.rule.id, regex_rule.id)
        service.llm.moderation.assert_not_awaited()

    async def test_keyword_rule_matches_telegram_command_locally(self) -> None:
        keyword = ModerationRule(
            id=15,
            group_id=-100,
            rule_type="keyword",
            pattern="lucky_checkin",
            action="delete",
            enabled=True,
        )
        self.session.execute.return_value = _RowsResult([keyword])
        service = self._service(RuntimeError("LLM must not run"))

        verdict = await service.evaluate(
            self.session,
            -100,
            "/lucky_checkin@aq_lucky_bot",
        )

        self.assertTrue(verdict.violated)
        self.assertTrue(verdict.conclusive)
        self.assertEqual(verdict.rule.id, keyword.id)
        service.llm.moderation.assert_not_awaited()

    async def test_anchored_regex_does_not_alias_inline_command_text(self) -> None:
        regex_rule = ModerationRule(
            id=16,
            group_id=-100,
            rule_type="regex",
            pattern=r"^lucky_checkin$",
            action="warn",
            enabled=True,
        )
        self.session.execute.return_value = _RowsResult([regex_rule])
        service = self._service(RuntimeError("LLM must not run"))

        verdict = await service.evaluate(
            self.session,
            -100,
            "prefix /lucky_checkin@aq_lucky_bot suffix",
        )

        self.assertFalse(verdict.violated)
        self.assertTrue(verdict.conclusive)
        service.llm.moderation.assert_not_awaited()

    async def test_invalid_regex_is_inconclusive_instead_of_crashing(self) -> None:
        invalid = ModerationRule(
            id=11,
            group_id=-100,
            rule_type="regex",
            pattern="([",
            action="warn",
            enabled=True,
        )
        self.session.execute.return_value = _RowsResult([invalid])
        service = self._service(RuntimeError("LLM must not run"))

        verdict = await service.evaluate(self.session, -100, "ordinary text")

        self.assertFalse(verdict.violated)
        self.assertFalse(verdict.conclusive)
        service.llm.moderation.assert_not_awaited()

    async def test_many_regex_rules_share_one_small_total_budget(self) -> None:
        regex_rules = [
            ModerationRule(
                id=20 + index,
                group_id=-100,
                rule_type="regex",
                pattern=f"never-{index}",
                action="warn",
                enabled=True,
            )
            for index in range(3)
        ]
        self.session.execute.return_value = _RowsResult(regex_rules)
        service = self._service(RuntimeError("LLM must not run"))

        with (
            patch(
                "bot.services.moderation.time.perf_counter",
                side_effect=[0.0, 0.05, 0.11, 0.12],
            ),
            patch(
                "bot.services.moderation.safe_regex.search",
                return_value=None,
            ) as search,
        ):
            verdict = await service.evaluate(self.session, -100, "ordinary text")

        self.assertFalse(verdict.violated)
        self.assertFalse(verdict.conclusive)
        self.assertEqual(search.call_count, 1)

    async def test_llm_failure_cannot_bypass_matching_deterministic_rule(self) -> None:
        keyword = ModerationRule(
            id=12,
            group_id=-100,
            rule_type="keyword",
            pattern="blocked-token",
            action="delete",
            enabled=True,
        )
        llm_rule = ModerationRule(
            id=13,
            group_id=-100,
            rule_type="llm",
            pattern="No scams",
            action="ban",
            enabled=True,
        )
        self.session.execute.return_value = _RowsResult([keyword, llm_rule])
        service = self._service(RuntimeError("provider down"))

        verdict = await service.evaluate(self.session, -100, "contains BLOCKED-TOKEN")

        self.assertTrue(verdict.violated)
        self.assertEqual(verdict.rule.id, keyword.id)
        service.llm.moderation.assert_not_awaited()

    async def test_llm_exception_returns_inconclusive_after_local_clean(self) -> None:
        self.session.execute.return_value = _RowsResult([self.rule])
        service = self._service(RuntimeError("provider down"))

        verdict = await service.evaluate(self.session, -100, "ordinary text")

        self.assertFalse(verdict.violated)
        self.assertFalse(verdict.conclusive)

    async def test_read_transaction_is_released_before_llm_await(self) -> None:
        transaction_open = True

        async def release_transaction() -> None:
            nonlocal transaction_open
            transaction_open = False

        self.session.commit.side_effect = release_transaction
        self.session.in_transaction = lambda: transaction_open

        async def assert_committed_first(_system: str, _user: str) -> str:
            self.session.commit.assert_awaited_once()
            self.assertFalse(self.session.in_transaction())
            # Simulate a provider that yields control while the assertion
            # remains true for the entire network wait.
            await asyncio.sleep(0.01)
            self.assertFalse(self.session.in_transaction())
            return '{"violated": false, "rule_id": null, "confidence": 1.0}'

        service = ModerationService(
            ModerationConfig(),
            SimpleNamespace(moderation=AsyncMock(side_effect=assert_committed_first)),
        )

        verdict = await service.evaluate(self.session, -100, "ordinary text")

        self.assertFalse(verdict.violated)
        self.assertTrue(verdict.conclusive)


if __name__ == "__main__":
    unittest.main()
