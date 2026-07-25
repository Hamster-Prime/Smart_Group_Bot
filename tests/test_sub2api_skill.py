import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from bot.db.models import Base, Group, GroupApiModelQuerySecret
from bot.services.api_model_query import (
    ApiModelQueryConfig,
    normalize_api_model_query_base_url,
    replace_group_api_model_query_secret,
    set_api_model_query_config,
)
from bot.services.skills.api_model_query import ApiModelQuerySkill
from bot.services.skills.base import SkillContext, SkillRunResult
from bot.services.skills.service import SkillService


_MASTER_KEY = "api-model-query-test-master-key"


def _skill_settings() -> SimpleNamespace:
    return SimpleNamespace(config_master_key=_MASTER_KEY)


def _service_settings() -> SimpleNamespace:
    return SimpleNamespace(
        config_master_key=_MASTER_KEY,
        doubao_tts_enabled=False,
        moderation=SimpleNamespace(enabled=True),
    )


def _llm_stub() -> SimpleNamespace:
    return SimpleNamespace(
        main=SimpleNamespace(model="main-model", fallbacks=[]),
        decision_config=SimpleNamespace(model="decision-model", fallbacks=[]),
        vision_config=SimpleNamespace(model="vision-model", fallbacks=[]),
        moderation_config=SimpleNamespace(model="moderation-model", fallbacks=[]),
        compress_config=SimpleNamespace(model="compress-model", fallbacks=[]),
        embed_config=SimpleNamespace(model="embed-model", fallbacks=[]),
    )


def _models_payload(*model_ids: str) -> dict:
    return {
        "object": "list",
        "data": [
            {"id": model_id, "type": "model", "display_name": model_id}
            for model_id in model_ids
        ],
    }


def _completion_payload(model: str) -> dict:
    return {
        "id": "resp_1",
        "object": "chat.completion",
        "model": model,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": "pong"},
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
    }


def _resp(*, content: str = "", tool_calls: list[dict] | None = None) -> SimpleNamespace:
    return SimpleNamespace(
        choices=[
            SimpleNamespace(
                message=SimpleNamespace(content=content, tool_calls=tool_calls or [])
            )
        ]
    )


class ApiModelQuerySkillTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with self.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        self.session_factory = async_sessionmaker(self.engine, expire_on_commit=False)
        self.skill = ApiModelQuerySkill(_skill_settings())

    async def asyncTearDown(self) -> None:
        await self.engine.dispose()

    async def _add_group(
        self,
        group_id: int,
        *,
        enabled: bool = True,
        base_url: str = "https://models.example.com",
        api_key: str | None = "sk-group-key",
        api_key_configured: bool | None = None,
        http_timeout_sec: float = 12.0,
        check_timeout_sec: float = 34.0,
    ) -> None:
        key_configured = (
            bool(api_key) if api_key_configured is None else api_key_configured
        )
        settings_data = set_api_model_query_config(
            {},
            ApiModelQueryConfig(
                enabled=enabled,
                base_url=base_url,
                http_timeout_sec=http_timeout_sec,
                check_timeout_sec=check_timeout_sec,
                api_key_configured=key_configured,
                secret_version=1 if key_configured else 0,
            ),
        )
        async with self.session_factory() as session:
            session.add(Group(id=group_id, title=f"group-{group_id}", settings=settings_data))
            await session.flush()
            if api_key is not None:
                await replace_group_api_model_query_secret(
                    session,
                    group_id=group_id,
                    api_key=api_key,
                    master_key=_MASTER_KEY,
                    updated_by=42,
                )
            await session.commit()

    def _context(self, group_id: int) -> SkillContext:
        return SkillContext(session_factory=self.session_factory, chat_id=group_id)

    async def test_groups_use_independent_urls_and_encrypted_api_keys(self) -> None:
        first_group = -10001
        second_group = -10002
        await self._add_group(
            first_group,
            base_url="https://first.example.com",
            api_key="sk-first-group",
        )
        await self._add_group(
            second_group,
            base_url="https://second.example.com/gateway/v1",
            api_key="sk-second-group",
        )

        async with self.session_factory() as session:
            first_secret = await session.get(GroupApiModelQuerySecret, first_group)
            second_secret = await session.get(GroupApiModelQuerySecret, second_group)
        self.assertIsNotNone(first_secret)
        self.assertIsNotNone(second_secret)
        self.assertNotEqual(first_secret.ciphertext, "sk-first-group")
        self.assertNotEqual(second_secret.ciphertext, "sk-second-group")

        async def request_side_effect(url: str, **kwargs):
            authorization = kwargs["headers"]["Authorization"]
            if authorization == "Bearer sk-first-group":
                return 200, _models_payload("first-model"), url, ""
            if authorization == "Bearer sk-second-group":
                return 200, _models_payload("second-model"), url, ""
            raise AssertionError(f"unexpected authorization header: {authorization}")

        with patch(
            "bot.services.skills.api_model_query.request_json",
            new=AsyncMock(side_effect=request_side_effect),
        ) as request_mock:
            first_result = await self.skill.run(
                {"action": "list_models"}, self._context(first_group)
            )
            second_result = await self.skill.run(
                {"action": "list_models"}, self._context(second_group)
            )

        self.assertTrue(first_result.ok)
        self.assertTrue(second_result.ok)
        self.assertEqual(first_result.summary, "查到 1 个可用模型")
        self.assertEqual(first_result.payload["models"][0]["id"], "first-model")
        self.assertEqual(second_result.payload["models"][0]["id"], "second-model")
        self.assertEqual(request_mock.await_count, 2)
        self.assertEqual(
            request_mock.await_args_list[0].args[0],
            "https://first.example.com/v1/models",
        )
        self.assertEqual(
            request_mock.await_args_list[1].args[0],
            "https://second.example.com/gateway/v1/models",
        )

    async def test_disabled_group_is_rejected_without_network_request(self) -> None:
        group_id = -10003
        await self._add_group(group_id, enabled=False)

        with patch(
            "bot.services.skills.api_model_query.request_json", new=AsyncMock()
        ) as request_mock:
            result = await self.skill.run(
                {"action": "list_models"}, self._context(group_id)
            )

        self.assertFalse(result.ok)
        self.assertEqual(result.error, "disabled_for_group")
        request_mock.assert_not_awaited()

    async def test_group_without_stored_secret_is_not_configured(self) -> None:
        group_id = -10004
        await self._add_group(
            group_id,
            api_key=None,
            api_key_configured=True,
        )

        with patch(
            "bot.services.skills.api_model_query.request_json", new=AsyncMock()
        ) as request_mock:
            result = await self.skill.run(
                {"action": "list_models"}, self._context(group_id)
            )

        self.assertFalse(result.ok)
        self.assertEqual(result.error, "not_configured_for_group")
        request_mock.assert_not_awaited()

    async def test_list_models_reports_http_error(self) -> None:
        group_id = -10005
        await self._add_group(group_id)

        with patch(
            "bot.services.skills.api_model_query.request_json",
            new=AsyncMock(
                return_value=(
                    401,
                    {"error": {"message": "invalid group API key"}},
                    "https://models.example.com/v1/models",
                    "",
                )
            ),
        ):
            result = await self.skill.run(
                {"action": "list_models"}, self._context(group_id)
            )

        self.assertFalse(result.ok)
        self.assertEqual(result.summary, "模型列表查询失败（HTTP 401）")
        self.assertEqual(result.error, "invalid group API key")

    async def test_list_models_reports_network_error(self) -> None:
        group_id = -10006
        await self._add_group(group_id)

        with patch(
            "bot.services.skills.api_model_query.request_json",
            new=AsyncMock(side_effect=OSError("dns failure")),
        ):
            result = await self.skill.run(
                {"action": "list_models"}, self._context(group_id)
            )

        self.assertFalse(result.ok)
        self.assertEqual(result.summary, "模型列表查询失败（网络错误）")
        self.assertIn("dns failure", result.error)

    async def test_upstream_model_text_cannot_echo_group_api_key(self) -> None:
        group_id = -10012
        api_key = "sk-never-send-this-to-the-main-model"
        await self._add_group(group_id, api_key=api_key)

        payload = {
            "data": [
                {
                    "id": "safe-model",
                    "display_name": f"display leaked {api_key}",
                },
                {
                    "id": f"credential-{api_key}",
                    "display_name": "must be omitted",
                },
            ]
        }
        with patch(
            "bot.services.skills.api_model_query.request_json",
            new=AsyncMock(
                return_value=(
                    200,
                    payload,
                    "https://models.example.com/v1/models",
                    "",
                )
            ),
        ):
            result = await self.skill.run(
                {"action": "list_models"}, self._context(group_id)
            )

        self.assertTrue(result.ok)
        self.assertEqual(result.payload["count"], 1)
        self.assertEqual(result.payload["models"][0]["id"], "safe-model")
        self.assertIn(
            "[API_KEY_REDACTED]",
            result.payload["models"][0]["display_name"],
        )
        self.assertNotIn(api_key, str(result.payload))

    async def test_upstream_errors_and_completion_content_cannot_echo_api_key(
        self,
    ) -> None:
        group_id = -10013
        api_key = "sk-private-group-credential"
        await self._add_group(group_id, api_key=api_key)

        with patch(
            "bot.services.skills.api_model_query.request_json",
            new=AsyncMock(
                return_value=(
                    401,
                    {"error": {"message": f"Authorization: Bearer {api_key}"}},
                    "https://models.example.com/v1/models",
                    "",
                )
            ),
        ):
            failed = await self.skill.run(
                {"action": "list_models"}, self._context(group_id)
            )

        self.assertFalse(failed.ok)
        self.assertIn("[API_KEY_REDACTED]", failed.error)
        self.assertNotIn(api_key, failed.error)

        completion = _completion_payload("safe-model")
        completion["choices"][0]["message"]["content"] = f"pong {api_key}"
        with patch(
            "bot.services.skills.api_model_query.request_json",
            new=AsyncMock(
                side_effect=[
                    (
                        200,
                        _models_payload("safe-model"),
                        "https://models.example.com/v1/models",
                        "",
                    ),
                    (
                        200,
                        completion,
                        "https://models.example.com/v1/chat/completions",
                        "",
                    ),
                ]
            ),
        ):
            checked = await self.skill.run(
                {"action": "check_model", "model": "safe-model"},
                self._context(group_id),
            )

        self.assertTrue(checked.ok)
        self.assertTrue(checked.payload["alive"])
        self.assertNotIn("reply_preview", checked.payload)
        self.assertNotIn(api_key, str(checked.payload))

    async def test_check_model_fetches_list_before_posting_completion(self) -> None:
        group_id = -10007
        await self._add_group(group_id, api_key="sk-check-group")

        with patch(
            "bot.services.skills.api_model_query.request_json",
            new=AsyncMock(
                side_effect=[
                    (
                        200,
                        _models_payload("gpt-5.4", "gpt-5.3-codex"),
                        "https://models.example.com/v1/models",
                        "",
                    ),
                    (
                        200,
                        _completion_payload("gpt-5.4"),
                        "https://models.example.com/v1/chat/completions",
                        "",
                    ),
                ]
            ),
        ) as request_mock:
            result = await self.skill.run(
                {"action": "check_model", "model": "gpt-5.4"},
                self._context(group_id),
            )

        self.assertTrue(result.ok)
        self.assertTrue(result.payload["alive"])
        self.assertEqual(result.payload["model"], "gpt-5.4")
        self.assertEqual(request_mock.await_count, 2)
        list_call, check_call = request_mock.await_args_list
        self.assertEqual(list_call.kwargs["method"], "GET")
        self.assertEqual(list_call.args[0], "https://models.example.com/v1/models")
        self.assertEqual(check_call.kwargs["method"], "POST")
        self.assertEqual(
            check_call.args[0], "https://models.example.com/v1/chat/completions"
        )
        self.assertEqual(check_call.kwargs["json_body"]["model"], "gpt-5.4")
        self.assertFalse(check_call.kwargs["json_body"]["stream"])
        self.assertEqual(
            check_call.kwargs["headers"]["Authorization"], "Bearer sk-check-group"
        )

    async def test_check_model_rejects_unlisted_model_without_post(self) -> None:
        group_id = -10008
        await self._add_group(group_id)

        with patch(
            "bot.services.skills.api_model_query.request_json",
            new=AsyncMock(
                return_value=(
                    200,
                    _models_payload("listed-model"),
                    "https://models.example.com/v1/models",
                    "",
                )
            ),
        ) as request_mock:
            result = await self.skill.run(
                {"action": "check_model", "model": "not-listed-model"},
                self._context(group_id),
            )

        self.assertFalse(result.ok)
        self.assertEqual(result.error, "model_not_listed")
        self.assertEqual(result.payload["available_model_ids"], ["listed-model"])
        self.assertEqual(request_mock.await_count, 1)
        self.assertEqual(request_mock.await_args.kwargs["method"], "GET")

    async def test_check_model_reports_upstream_http_error_as_dead(self) -> None:
        group_id = -10009
        await self._add_group(group_id)

        with patch(
            "bot.services.skills.api_model_query.request_json",
            new=AsyncMock(
                side_effect=[
                    (
                        200,
                        _models_payload("gpt-5.4"),
                        "https://models.example.com/v1/models",
                        "",
                    ),
                    (
                        503,
                        {"error": {"message": "upstream unavailable"}},
                        "https://models.example.com/v1/chat/completions",
                        "",
                    ),
                ]
            ),
        ):
            result = await self.skill.run(
                {"action": "check_model", "model": "gpt-5.4"},
                self._context(group_id),
            )

        self.assertTrue(result.ok)
        self.assertFalse(result.payload["alive"])
        self.assertEqual(result.payload["http_status"], 503)
        self.assertEqual(result.payload["error_detail"], "upstream unavailable")

    async def test_check_model_reports_network_error_as_dead(self) -> None:
        group_id = -10010
        await self._add_group(group_id)

        with patch(
            "bot.services.skills.api_model_query.request_json",
            new=AsyncMock(
                side_effect=[
                    (
                        200,
                        _models_payload("gpt-5.4"),
                        "https://models.example.com/v1/models",
                        "",
                    ),
                    TimeoutError("connect timeout"),
                ]
            ),
        ):
            result = await self.skill.run(
                {"action": "check_model", "model": "gpt-5.4"},
                self._context(group_id),
            )

        self.assertTrue(result.ok)
        self.assertFalse(result.payload["alive"])
        self.assertEqual(result.payload["http_status"], 0)
        self.assertIn("connect timeout", result.payload["error_detail"])

    async def test_missing_model_and_unknown_action_are_rejected(self) -> None:
        group_id = -10011
        await self._add_group(group_id)

        with patch(
            "bot.services.skills.api_model_query.request_json", new=AsyncMock()
        ) as request_mock:
            missing = await self.skill.run(
                {"action": "check_model"}, self._context(group_id)
            )
            unknown = await self.skill.run(
                {"action": "usage"}, self._context(group_id)
            )

        self.assertFalse(missing.ok)
        self.assertEqual(missing.error, "missing_model")
        self.assertFalse(unknown.ok)
        self.assertEqual(unknown.error, "unknown_action")
        request_mock.assert_not_awaited()

    def test_schema_only_exposes_model_list_and_check_actions(self) -> None:
        self.assertEqual(
            self.skill.parameters_schema["properties"]["action"]["enum"],
            ["list_models", "check_model"],
        )

    def test_plain_http_base_url_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "HTTPS"):
            normalize_api_model_query_base_url("http://models.example.com")


class ApiModelQuerySkillExposureTests(unittest.TestCase):
    def test_skill_service_only_exposes_group_api_tool_when_allowed(self) -> None:
        service = SkillService(_llm_stub(), settings=_service_settings())

        self.assertNotIn("api_model_query", service.available_skill_names())
        self.assertIn(
            "api_model_query",
            service.available_skill_names(allow_api_model_query=True),
        )
        self.assertNotIn(
            "sub2api_query",
            service.available_skill_names(allow_api_model_query=True),
        )


class ApiModelQueryFollowupTests(unittest.IsolatedAsyncioTestCase):
    async def test_summary_only_reply_triggers_payload_followup(self) -> None:
        class _PlannedService(SkillService):
            def __init__(self, responses: list[SimpleNamespace]) -> None:
                super().__init__(llm=object(), settings=None)
                self._register(ApiModelQuerySkill(_skill_settings()))
                self._responses = list(responses)
                self.calls: list[list[dict]] = []
                self.tool_names_by_round: list[list[str]] = []
                self.tool_runs: list[str] = []

            async def _completion_with_fallbacks(self, messages, tools):
                self.calls.append([dict(message) for message in messages])
                self.tool_names_by_round.append(
                    [tool["function"]["name"] for tool in tools]
                )
                if not self._responses:
                    return None
                return self._responses.pop(0)

            async def _run_tool(self, *, name, arguments, context, skills=None):
                del arguments, context, skills
                self.tool_runs.append(name)
                return SkillRunResult(
                    ok=True,
                    skill="api_model_query",
                    summary="查到 2 个可用模型",
                    payload={
                        "action": "list_models",
                        "count": 2,
                        "models": [
                            {"id": "gpt-5.4", "display_name": "gpt-5.4"},
                            {
                                "id": "gpt-5.3-codex",
                                "display_name": "gpt-5.3-codex",
                            },
                        ],
                    },
                )

        service = _PlannedService(
            [
                _resp(
                    tool_calls=[
                        {
                            "id": "call-1",
                            "function": {
                                "name": "api_model_query",
                                "arguments": '{"action":"list_models"}',
                            },
                        }
                    ]
                ),
                _resp(content="查到 2 个可用模型"),
                _resp(content="本群 API 当前提供 gpt-5.4 和 gpt-5.3-codex。"),
            ]
        )

        result = await service.answer_with_skill(
            "看看本群 API 有哪些模型",
            intent_type="casual",
            allow_api_model_query=True,
        )

        self.assertEqual(result.text, "本群 API 当前提供 gpt-5.4 和 gpt-5.3-codex。")
        self.assertEqual(service.tool_runs, ["api_model_query"])
        self.assertIn("api_model_query", service.tool_names_by_round[0])
        followup_blocks = [
            message["content"]
            for message in service.calls[-1]
            if message.get("role") == "system"
            and str(message.get("content", "")).startswith("[TOOL_FOLLOWUP]")
        ]
        self.assertEqual(len(followup_blocks), 1)
        self.assertIn("models[].id", followup_blocks[0])


if __name__ == "__main__":
    unittest.main()
