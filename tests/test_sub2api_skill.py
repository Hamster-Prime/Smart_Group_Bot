import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from bot.services.skills.sub2api_query import Sub2ApiQuerySkill


def _settings(**overrides: object) -> SimpleNamespace:
    config = {
        "sub2api_enabled": True,
        "sub2api_base_url": "https://codex.example.com",
        "sub2api_api_key": "sk-test-key-1234567890",
        "sub2api_http_timeout_sec": 15.0,
        "sub2api_check_timeout_sec": 45.0,
    }
    config.update(overrides)
    return SimpleNamespace(**config)


def _models_payload() -> dict:
    return {
        "object": "list",
        "data": [
            {"id": "gpt-5.4", "type": "model", "display_name": "gpt-5.4"},
            {"id": "gpt-5.3-codex", "type": "model", "display_name": "gpt-5.3-codex"},
        ],
    }


class Sub2ApiListModelsTests(unittest.IsolatedAsyncioTestCase):
    async def test_list_models_returns_model_ids(self) -> None:
        skill = Sub2ApiQuerySkill(_settings())

        with patch.object(
            skill,
            "_request_json",
            new=AsyncMock(return_value=(200, _models_payload(), "")),
        ) as request_mock:
            result = await skill.run({"action": "list_models"}, context=SimpleNamespace())

        self.assertTrue(result.ok)
        self.assertEqual(result.summary, "查到 2 个可用模型")
        self.assertEqual(result.payload["count"], 2)
        self.assertEqual(
            [m["id"] for m in result.payload["models"]],
            ["gpt-5.4", "gpt-5.3-codex"],
        )
        called_kwargs = request_mock.await_args.kwargs
        self.assertEqual(called_kwargs["url"], "https://codex.example.com/v1/models")
        self.assertEqual(
            called_kwargs["headers"]["Authorization"],
            "Bearer sk-test-key-1234567890",
        )


class Sub2ApiCheckModelTests(unittest.IsolatedAsyncioTestCase):
    async def test_check_model_reports_alive_on_valid_completion(self) -> None:
        skill = Sub2ApiQuerySkill(_settings())
        completion = {
            "id": "resp_1",
            "object": "chat.completion",
            "model": "gpt-5.4",
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": "pong"},
                    "finish_reason": "stop",
                }
            ],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
        }

        with patch.object(
            skill,
            "_request_json",
            new=AsyncMock(return_value=(200, completion, "")),
        ) as request_mock:
            result = await skill.run(
                {"action": "check_model", "model": "gpt-5.4"}, context=SimpleNamespace()
            )

        self.assertTrue(result.ok)
        self.assertTrue(result.payload["alive"])
        self.assertEqual(result.payload["model"], "gpt-5.4")
        self.assertIn("可用", result.summary)
        self.assertGreaterEqual(result.payload["latency_ms"], 0)
        called_kwargs = request_mock.await_args.kwargs
        self.assertEqual(
            called_kwargs["url"], "https://codex.example.com/v1/chat/completions"
        )
        self.assertEqual(called_kwargs["json_body"]["model"], "gpt-5.4")
        self.assertFalse(called_kwargs["json_body"]["stream"])


    async def test_check_model_reports_dead_on_upstream_error(self) -> None:
        skill = Sub2ApiQuerySkill(_settings())
        error_payload = {
            "error": {
                "message": "Upstream service temporarily unavailable",
                "type": "upstream_error",
            }
        }

        with patch.object(
            skill,
            "_request_json",
            new=AsyncMock(return_value=(502, error_payload, "")),
        ):
            result = await skill.run(
                {"action": "check_model", "model": "gpt-5.3-codex"},
                context=SimpleNamespace(),
            )

        self.assertTrue(result.ok)
        self.assertFalse(result.payload["alive"])
        self.assertEqual(result.payload["http_status"], 502)
        self.assertIn("不可用", result.summary)
        self.assertIn("Upstream service temporarily unavailable", result.payload["error_detail"])

    async def test_check_model_requires_model_id(self) -> None:
        skill = Sub2ApiQuerySkill(_settings())

        result = await skill.run({"action": "check_model"}, context=SimpleNamespace())

        self.assertFalse(result.ok)
        self.assertEqual(result.error, "missing_model")

    async def test_check_model_network_error_is_reported_not_raised(self) -> None:
        skill = Sub2ApiQuerySkill(_settings())

        with patch.object(
            skill,
            "_request_json",
            new=AsyncMock(side_effect=TimeoutError("connect timeout")),
        ):
            result = await skill.run(
                {"action": "check_model", "model": "gpt-5.4"}, context=SimpleNamespace()
            )

        self.assertTrue(result.ok)
        self.assertFalse(result.payload["alive"])
        self.assertIn("不可用", result.summary)


class Sub2ApiUsageRemovedTests(unittest.IsolatedAsyncioTestCase):
    async def test_usage_action_is_not_supported(self) -> None:
        skill = Sub2ApiQuerySkill(_settings())

        result = await skill.run({"action": "usage"}, context=SimpleNamespace())

        self.assertFalse(result.ok)
        self.assertEqual(result.error, "unknown_action")

    def test_schema_only_exposes_models_actions(self) -> None:
        skill = Sub2ApiQuerySkill(_settings())

        self.assertEqual(
            skill.parameters_schema["properties"]["action"]["enum"],
            ["list_models", "check_model"],
        )


class Sub2ApiAvailabilityTests(unittest.IsolatedAsyncioTestCase):
    async def test_disabled_skill_reports_disabled(self) -> None:
        skill = Sub2ApiQuerySkill(_settings(sub2api_enabled=False))

        result = await skill.run({"action": "list_models"}, context=SimpleNamespace())

        self.assertFalse(result.ok)
        self.assertEqual(result.error, "disabled")
        self.assertFalse(skill.available)

    async def test_missing_key_reports_not_configured(self) -> None:
        skill = Sub2ApiQuerySkill(_settings(sub2api_api_key=""))

        result = await skill.run({"action": "list_models"}, context=SimpleNamespace())

        self.assertFalse(result.ok)
        self.assertEqual(result.error, "not_configured")
        self.assertFalse(skill.available)

    async def test_list_models_network_error_is_reported_not_raised(self) -> None:
        skill = Sub2ApiQuerySkill(_settings())

        with patch.object(
            skill,
            "_request_json",
            new=AsyncMock(side_effect=OSError("dns failure")),
        ):
            result = await skill.run({"action": "list_models"}, context=SimpleNamespace())

        self.assertFalse(result.ok)
        self.assertIn("dns failure", result.error)


class Sub2ApiSkillRegistrationTests(unittest.TestCase):
    def _service_settings(self, **overrides: object) -> SimpleNamespace:
        config = {
            "sub2api_enabled": True,
            "sub2api_base_url": "https://codex.example.com",
            "sub2api_api_key": "sk-test-key",
            "sub2api_http_timeout_sec": 15.0,
            "sub2api_check_timeout_sec": 45.0,
            "doubao_tts_enabled": False,
            "moderation": SimpleNamespace(enabled=True),
        }
        config.update(overrides)
        return SimpleNamespace(**config)

    def _llm_stub(self) -> SimpleNamespace:
        return SimpleNamespace(
            main=SimpleNamespace(model="main-model", fallbacks=[]),
            decision_config=SimpleNamespace(model="decision-model", fallbacks=[]),
            vision_config=SimpleNamespace(model="vision-model", fallbacks=[]),
            moderation_config=SimpleNamespace(model="moderation-model", fallbacks=[]),
            compress_config=SimpleNamespace(model="compress-model", fallbacks=[]),
            embed_config=SimpleNamespace(model="embed-model", fallbacks=[]),
        )

    def test_configured_settings_register_skill(self) -> None:
        from bot.services.skills.service import SkillService

        service = SkillService(self._llm_stub(), settings=self._service_settings())

        self.assertIn("sub2api_query", service.available_skill_names())

    def test_unconfigured_settings_skip_registration(self) -> None:
        from bot.services.skills.service import SkillService

        service = SkillService(
            self._llm_stub(),
            settings=self._service_settings(sub2api_api_key=""),
        )

        self.assertNotIn("sub2api_query", service.available_skill_names())


class Sub2ApiFollowupTests(unittest.IsolatedAsyncioTestCase):
    async def test_summary_only_reply_triggers_followup_generation(self) -> None:
        from bot.services.skills.base import SkillRunResult
        from bot.services.skills.service import SkillService

        class _PlannedService(SkillService):
            def __init__(self, responses: list[SimpleNamespace]) -> None:
                super().__init__(llm=object(), settings=None)
                self._responses = list(responses)

            async def _completion_with_fallbacks(self, messages, tools):
                if not self._responses:
                    return None
                return self._responses.pop(0)

            async def _run_tool(self, *, name, arguments, context, skills=None):
                return SkillRunResult(
                    ok=True,
                    skill="sub2api_query",
                    summary="查到 2 个可用模型",
                    payload={
                        "action": "list_models",
                        "count": 2,
                        "models": [
                            {"id": "gpt-5.4", "display_name": "gpt-5.4"},
                            {"id": "gpt-5.3-codex", "display_name": "gpt-5.3-codex"},
                        ],
                    },
                )

        def _resp(*, content: str = "", tool_calls: list[dict] | None = None) -> SimpleNamespace:
            return SimpleNamespace(
                choices=[
                    SimpleNamespace(
                        message=SimpleNamespace(content=content, tool_calls=tool_calls or [])
                    )
                ]
            )

        service = _PlannedService(
            [
                _resp(
                    tool_calls=[
                        {
                            "id": "call-1",
                            "function": {
                                "name": "sub2api_query",
                                "arguments": '{"action":"list_models"}',
                            },
                        }
                    ]
                ),
                _resp(content="查到 2 个可用模型"),
                _resp(content="当前站点有 gpt-5.4 和 gpt-5.3-codex 两个模型可以用。"),
            ]
        )

        result = await service.answer_with_skill("看看中转站有哪些模型", intent_type="casual")

        self.assertEqual(result.text, "当前站点有 gpt-5.4 和 gpt-5.3-codex 两个模型可以用。")


if __name__ == "__main__":
    unittest.main()
