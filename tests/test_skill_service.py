import asyncio
import unittest
from types import SimpleNamespace

from bot.services.skills import service as skill_service_module
from bot.services.skills.base import SkillContext, SkillRunResult
from bot.services.skills.service import SkillService


def _resp(*, content: str = "", tool_calls: list[dict] | None = None) -> SimpleNamespace:
    return SimpleNamespace(
        choices=[
            SimpleNamespace(
                message=SimpleNamespace(
                    content=content,
                    tool_calls=tool_calls or [],
                )
            )
        ]
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


def _tts_settings() -> SimpleNamespace:
    return SimpleNamespace(
        doubao_tts_enabled=True,
        doubao_tts_api_base="https://openspeech.bytedance.com",
        doubao_tts_app_id="app-id",
        doubao_tts_app_key="",
        doubao_tts_access_key="access-key",
        doubao_tts_resource_id="seed-tts-2.0",
        doubao_tts_model="",
        doubao_tts_speaker="voice_1",
        moderation=SimpleNamespace(enabled=True),
    )


class _PlannedSkillService(SkillService):
    def __init__(self, responses: list[SimpleNamespace]) -> None:
        super().__init__(llm=object(), settings=None)
        self._responses = list(responses)
        self.calls: list[list[dict]] = []
        self.tool_runs: list[str] = []

    async def _completion_with_fallbacks(self, messages, tools):
        self.calls.append([dict(message) for message in messages])
        if not self._responses:
            return None
        return self._responses.pop(0)

    async def _run_tool(self, *, name, arguments, context, skills=None):
        self.tool_runs.append(name)
        if name == "send_sticker":
            context.handled = True
            context.sticker_sent = True
            context.sticker_file_id = "sticker-file-id"
            context.suppress_followup_text = True
            return SkillRunResult(ok=True, skill=name, summary="")

        if name == "doubao_tts":
            context.handled = True
            context.tts_sent = True
            context.tts_text = "你好呀"
            context.suppress_followup_text = True
            return SkillRunResult(ok=True, skill=name, summary="你好呀")

        if name == "websearch":
            return SkillRunResult(
                ok=True,
                skill=name,
                summary="找到 2 条搜索结果",
                payload={
                    "query": arguments.get("query", ""),
                    "results": [
                        {
                            "title": "MosDNS 官方文档",
                            "url": "https://example.com/mosdns",
                            "snippet": "包含安装、配置与常见问题说明。",
                        },
                        {
                            "title": "顺便看一下谁更准的对比贴",
                            "url": "https://example.com/compare",
                            "snippet": "整理了几种 DNS 方案的命中率与延迟。",
                        },
                    ],
                },
            )

        if name == "vote_ban":
            return SkillRunResult(
                ok=False,
                skill=name,
                summary="你在 1 小时内最多只能发起 1 次民主投票；额度已用完，请 30 分钟后再试。",
                error="starter_quota_exhausted",
                payload={"quota": {"limit": 1, "used": 1, "remaining": 0}},
            )

        if name == "rule_manage":
            return SkillRunResult(
                ok=True,
                skill=name,
                summary="已添加规则 #7",
                payload={"action": "add", "rule_id": 7},
            )

        if name == "bilibili_search":
            return SkillRunResult(
                ok=True,
                skill=name,
                summary="拿到 B 站视频详情",
                payload={
                    "entry": {
                        "title": "测试 B 站视频",
                        "author": "测试 UP 主",
                        "url": "https://www.bilibili.com/video/BV1xx411c7mD",
                        "author_url": "https://space.bilibili.com/123456",
                        "content": "这是一段视频简介",
                    }
                },
            )

        if name == "weibo_search":
            return SkillRunResult(
                ok=True,
                skill=name,
                summary="拿到 2 条微博热搜",
                payload={
                    "platform": "weibo",
                    "action": "hot_search",
                    "results": [
                        {
                            "title": "热搜一",
                            "url": "https://s.weibo.com/weibo?q=%E7%83%AD%E6%90%9C%E4%B8%80",
                            "snippet": "热度 112万",
                        },
                        {
                            "title": "热搜二",
                            "url": "https://s.weibo.com/weibo?q=%E7%83%AD%E6%90%9C%E4%BA%8C",
                            "snippet": "热度 98万",
                        },
                    ],
                },
            )

        return SkillRunResult(ok=False, skill=name, summary="", error="unknown_skill")


class _AmbiguousPlannedSkillService(_PlannedSkillService):
    async def _run_tool(self, *, name, arguments, context, skills=None):
        del arguments, context, skills
        self.tool_runs.append(name)
        return self._ambiguous_side_effect_result(name)


class SkillServiceTTSPromptTests(unittest.TestCase):
    def test_vote_ban_skill_is_registered_when_runtime_settings_exist(self) -> None:
        service = SkillService(_llm_stub(), settings=_tts_settings())
        self.assertIn("vote_ban", service.available_skill_names())

    def test_enable_mode_includes_group_tts_preference_block(self) -> None:
        service = SkillService(_llm_stub(), settings=_tts_settings())

        payload = service.build_answer_prompt_payload(
            "晚安啦",
            intent_type="casual",
            allow_tts=True,
            tts_mode="on",
        )

        contents = [item["content"] for item in payload["messages"]]
        tts_blocks = [content for content in contents if content.startswith("[GROUP_TTS_PREFERENCE]\n")]

        self.assertEqual(len(tts_blocks), 1)
        self.assertIn("tts_mode: on", tts_blocks[0])
        self.assertIn("When in doubt between voice and text for a short, emotional, or conversational reply, lean toward voice.", tts_blocks[0])
        self.assertIn("Keep text for: factual answers, link-heavy or list-heavy replies", tts_blocks[0])

    def test_off_mode_does_not_include_group_tts_preference_block(self) -> None:
        service = SkillService(_llm_stub(), settings=_tts_settings())

        payload = service.build_answer_prompt_payload(
            "晚安啦",
            intent_type="casual",
            allow_tts=False,
            tts_mode="off",
        )

        contents = [item["content"] for item in payload["messages"]]
        self.assertFalse(any(content.startswith("[GROUP_TTS_PREFERENCE]\n") for content in contents))


class SkillServiceFollowupSuppressionTests(unittest.IsolatedAsyncioTestCase):
    async def test_ambiguous_side_effect_is_terminal_and_not_retried(self) -> None:
        service = _AmbiguousPlannedSkillService(
            [
                _resp(
                    tool_calls=[
                        {
                            "id": "call-sticker",
                            "function": {
                                "name": "send_sticker",
                                "arguments": "{}",
                            },
                        },
                        {
                            "id": "call-tts",
                            "function": {
                                "name": "doubao_tts",
                                "arguments": '{"text":"不要重复"}',
                            },
                        },
                    ]
                ),
                _resp(content="错误地声称两个操作都成功"),
            ]
        )

        result = await service.answer_with_skill("发贴纸并说一句", intent_type="casual")

        self.assertEqual(service.tool_runs, ["send_sticker"])
        self.assertEqual(len(service.calls), 1)
        self.assertIn("可能已经完成", result.text)
        self.assertIn("不会自动重试", result.text)

    async def test_send_sticker_does_not_return_followup_text(self) -> None:
        service = _PlannedSkillService(
            [
                _resp(
                    tool_calls=[
                        {
                            "id": "call-1",
                            "function": {
                                "name": "send_sticker",
                                "arguments": '{"query":"无语"}',
                            },
                        }
                    ]
                ),
                _resp(content="因为字多（贴纸贴贴完毕🤣）"),
            ]
        )

        result = await service.answer_with_skill("发个贴纸", intent_type="casual")

        self.assertTrue(result.handled)
        self.assertTrue(result.sticker_sent)
        self.assertEqual(result.sticker_file_id, "sticker-file-id")
        self.assertEqual(result.text, "")

    async def test_tts_skill_does_not_return_followup_text(self) -> None:
        service = _PlannedSkillService(
            [
                _resp(
                    tool_calls=[
                        {
                            "id": "call-1",
                            "function": {
                                "name": "doubao_tts",
                                "arguments": '{"text":"你好呀"}',
                            },
                        }
                    ]
                ),
                _resp(content="我发语音啦"),
            ]
        )

        result = await service.answer_with_skill("说一句你好呀", intent_type="casual")

        self.assertTrue(result.handled)
        self.assertTrue(result.tts_sent)
        self.assertEqual(result.tts_text, "你好呀")
        self.assertEqual(result.text, "")

    async def test_vote_quota_error_falls_back_to_required_refusal_summary(self) -> None:
        service = _PlannedSkillService(
            [
                _resp(
                    tool_calls=[
                        {
                            "id": "call-vote",
                            "function": {
                                "name": "vote_ban",
                                "arguments": "{}",
                            },
                        }
                    ]
                )
            ]
        )

        result = await service.answer_with_skill("发起投票封他", intent_type="casual")

        self.assertIn("额度已用完", result.text)
        self.assertIn("30 分钟后", result.text)
        self.assertFalse(result.handled)

    async def test_vote_quota_error_instructs_main_model_to_refuse_without_bypass(self) -> None:
        service = _PlannedSkillService(
            [
                _resp(
                    tool_calls=[
                        {
                            "id": "call-vote",
                            "function": {"name": "vote_ban", "arguments": "{}"},
                        }
                    ]
                ),
                _resp(content="投票已经发起，你也可以改用 /voteban 绕过限制。"),
            ]
        )

        result = await service.answer_with_skill("发起投票封他", intent_type="casual")

        self.assertIn("额度已用完", result.text)
        self.assertIn("30 分钟后", result.text)
        self.assertNotIn("已经发起", result.text)
        self.assertNotIn("绕过", result.text)
        second_call = service.calls[1]
        refusal = "\n".join(
            message.get("content", "")
            for message in second_call
            if message.get("role") == "system"
        )
        self.assertIn("MANDATORY_TOOL_REFUSAL", refusal)
        self.assertIn("Do not retry", refusal)
        self.assertIn("do not suggest /voteban", refusal)

    async def test_quota_refusal_skips_later_side_effect_tools_in_same_turn(self) -> None:
        service = _PlannedSkillService(
            [
                _resp(
                    tool_calls=[
                        {
                            "id": "call-vote",
                            "function": {"name": "vote_ban", "arguments": "{}"},
                        },
                        {
                            "id": "call-sticker",
                            "function": {"name": "send_sticker", "arguments": "{}"},
                        },
                    ]
                ),
                _resp(content="已处理"),
            ]
        )

        result = await service.answer_with_skill("发起投票并发贴纸", intent_type="casual")

        self.assertIn("额度已用完", result.text)
        self.assertFalse(result.sticker_sent)
        self.assertFalse(result.handled)

    async def test_successful_tts_skips_later_side_effect_tool(self) -> None:
        service = _PlannedSkillService(
            [
                _resp(
                    tool_calls=[
                        {
                            "id": "call-tts",
                            "function": {
                                "name": "doubao_tts",
                                "arguments": '{"text":"先说一句"}',
                            },
                        },
                        {
                            "id": "call-vote",
                            "function": {"name": "vote_ban", "arguments": "{}"},
                        },
                    ]
                ),
                _resp(content="投票已经发起"),
            ]
        )

        result = await service.answer_with_skill("语音说完再发起投票", intent_type="casual")

        self.assertTrue(result.tts_sent)
        self.assertFalse(result.must_deliver_text)
        self.assertEqual(result.text, "")
        self.assertEqual(service.tool_runs, ["doubao_tts"])

    async def test_successful_sticker_skips_second_delivered_action(self) -> None:
        service = _PlannedSkillService(
            [
                _resp(
                    tool_calls=[
                        {
                            "id": "call-sticker",
                            "function": {"name": "send_sticker", "arguments": "{}"},
                        },
                        {
                            "id": "call-tts",
                            "function": {
                                "name": "doubao_tts",
                                "arguments": '{"text":"重复回复"}',
                            },
                        },
                    ]
                )
            ]
        )

        result = await service.answer_with_skill("发贴纸再说一句", intent_type="casual")

        self.assertTrue(result.sticker_sent)
        self.assertFalse(result.tts_sent)
        self.assertEqual(service.tool_runs, ["send_sticker"])

    async def test_successful_state_mutation_skips_remaining_tools_and_summarizes(self) -> None:
        service = _PlannedSkillService(
            [
                _resp(
                    tool_calls=[
                        {
                            "id": "call-rule-1",
                            "function": {
                                "name": "rule_manage",
                                "arguments": '{"request_text":"添加规则一"}',
                            },
                        },
                        {
                            "id": "call-rule-2",
                            "function": {
                                "name": "rule_manage",
                                "arguments": '{"request_text":"添加规则二"}',
                            },
                        },
                    ]
                ),
                _resp(content="已添加第一条规则；第二次操作为避免重复已跳过。"),
            ]
        )

        result = await service.answer_with_skill("添加两次规则", intent_type="casual")

        self.assertEqual(service.tool_runs, ["rule_manage"])
        self.assertIn("已添加第一条规则", result.text)
        second_call = service.calls[1]
        tool_messages = [item for item in second_call if item.get("role") == "tool"]
        self.assertEqual(len(tool_messages), 2)
        self.assertIn("skipped_after_side_effect", tool_messages[1]["content"])
        self.assertTrue(
            any("SIDE_EFFECT_COMMITTED" in item.get("content", "") for item in second_call)
        )

    async def test_websearch_summary_only_reply_triggers_followup_generation(self) -> None:
        service = _PlannedSkillService(
            [
                _resp(
                    tool_calls=[
                        {
                            "id": "call-1",
                            "function": {
                                "name": "websearch",
                                "arguments": '{"query":"mosdns"}',
                            },
                        }
                    ]
                ),
                _resp(content="找到 2 条搜索结果"),
                _resp(content="我查了下，当前更权威的是官方文档这一条。"),
            ]
        )

        result = await service.answer_with_skill("查一下 mosdns", intent_type="casual")

        self.assertEqual(result.text, "我查了下，当前更权威的是官方文档这一条。")

    async def test_websearch_empty_followup_falls_back_to_readable_results(self) -> None:
        service = _PlannedSkillService(
            [
                _resp(
                    tool_calls=[
                        {
                            "id": "call-1",
                            "function": {
                                "name": "websearch",
                                "arguments": '{"query":"mosdns"}',
                            },
                        }
                    ]
                ),
                _resp(content=""),
            ]
        )

        result = await service.answer_with_skill("查一下 mosdns", intent_type="casual")

        self.assertIn("我先查到这些相关结果：", result.text)
        self.assertIn("MosDNS 官方文档", result.text)
        self.assertIn("https://example.com/mosdns", result.text)

    async def test_platform_entry_fallback_includes_author_link(self) -> None:
        service = _PlannedSkillService(
            [
                _resp(
                    tool_calls=[
                        {
                            "id": "call-1",
                            "function": {
                                "name": "bilibili_search",
                                "arguments": '{"action":"video_detail","query":"BV1xx411c7mD"}',
                            },
                        }
                    ]
                ),
                _resp(content=""),
            ]
        )

        result = await service.answer_with_skill("把这个视频原链接发我", intent_type="casual")

        self.assertIn("https://www.bilibili.com/video/BV1xx411c7mD", result.text)
        self.assertIn("作者主页：https://space.bilibili.com/123456", result.text)

    async def test_platform_results_reply_without_links_inline_urls(self) -> None:
        service = _PlannedSkillService(
            [
                _resp(
                    tool_calls=[
                        {
                            "id": "call-1",
                            "function": {
                                "name": "weibo_search",
                                "arguments": '{"action":"hot_search","max_results":2}',
                            },
                        }
                    ]
                ),
                _resp(content="微博热搜 Top 2\n1. 热搜一\n2. 热搜二"),
            ]
        )

        result = await service.answer_with_skill("帮我看下今天的微博热搜", intent_type="casual")

        self.assertIn("微博热搜 Top 2", result.text)
        self.assertNotIn("微博相关链接：", result.text)
        self.assertIn("1. 热搜一\nhttps://s.weibo.com/weibo?q=%E7%83%AD%E6%90%9C%E4%B8%80", result.text)
        self.assertIn("2. 热搜二\nhttps://s.weibo.com/weibo?q=%E7%83%AD%E6%90%9C%E4%BA%8C", result.text)
        self.assertIn("https://s.weibo.com/weibo?q=%E7%83%AD%E6%90%9C%E4%B8%80", result.text)
        self.assertIn("https://s.weibo.com/weibo?q=%E7%83%AD%E6%90%9C%E4%BA%8C", result.text)

    async def test_platform_summary_reply_does_not_append_full_list_without_link_request(self) -> None:
        service = _PlannedSkillService(
            [
                _resp(
                    tool_calls=[
                        {
                            "id": "call-1",
                            "function": {
                                "name": "weibo_search",
                                "arguments": '{"action":"hot_search","max_results":10}',
                            },
                        }
                    ]
                ),
                _resp(content="刚那主人查过一波，现在涨最快的是曝曾沛慈退出浪姐。"),
            ]
        )

        result = await service.answer_with_skill("微博今天热度上升最快的话题是哪个", intent_type="casual")

        self.assertEqual(result.text, "刚那主人查过一波，现在涨最快的是曝曾沛慈退出浪姐。")

    async def test_platform_results_last_item_url_stays_above_trailing_commentary(self) -> None:
        service = _PlannedSkillService([])

        payload = {
            "platform": "weibo",
            "results": [
                {"title": f"热搜{i}", "url": f"https://example.com/{i}", "snippet": ""}
                for i in range(1, 10)
            ]
            + [
                {
                    "title": "原来冲锋衣是胶水粘的",
                    "url": "https://example.com/10",
                    "snippet": "",
                }
            ],
        }
        recent_tool_results = [
            {
                "name": "weibo_search",
                "arguments": {"action": "hot_search", "max_results": 10},
                "result": SkillRunResult(
                    ok=True,
                    skill="weibo_search",
                    summary="拿到 10 条微博热搜",
                    payload=payload,
                ),
            }
        ]
        content = (
            "微博热搜前十\n"
            "1. 热搜一\n"
            "2. 热搜二\n"
            "3. 热搜三\n"
            "4. 热搜四\n"
            "5. 热搜五\n"
            "6. 热搜六\n"
            "7. 热搜七\n"
            "8. 热搜八\n"
            "9. 原来我真能花100万\n"
            "10. 原来冲锋衣是胶水粘的\n\n"
            "薅金币那个比中彩票了，笑死我了"
        )

        result = service._append_missing_platform_links(
            content=content,
            recent_tool_results=recent_tool_results,
            user_text="帮我看下今天的微博热搜",
        )

        self.assertIn(
            "10. 原来冲锋衣是胶水粘的\nhttps://example.com/10",
            result,
        )
        self.assertIn(
            "https://example.com/10\n\n薅金币那个比中彩票了，笑死我了",
            result,
        )

    async def test_platform_summary_reply_appends_links_when_user_explicitly_requests_them(self) -> None:
        service = _PlannedSkillService([])

        recent_tool_results = [
            {
                "name": "weibo_search",
                "arguments": {"action": "hot_search", "max_results": 2},
                "result": SkillRunResult(
                    ok=True,
                    skill="weibo_search",
                    summary="拿到 2 条微博热搜",
                    payload={
                        "platform": "weibo",
                        "results": [
                            {"title": "热搜一", "url": "https://example.com/1", "snippet": ""},
                            {"title": "热搜二", "url": "https://example.com/2", "snippet": ""},
                        ],
                    },
                ),
            }
        ]

        result = service._append_missing_platform_links(
            content="我先给你贴两个最相关的。",
            recent_tool_results=recent_tool_results,
            user_text="把微博热搜链接发我",
        )

        self.assertIn("微博相关链接：", result)
        self.assertIn("https://example.com/1", result)
        self.assertIn("https://example.com/2", result)


class SkillExecutionBoundaryTests(unittest.IsolatedAsyncioTestCase):
    async def test_tool_exception_is_converted_to_result(self) -> None:
        class BrokenSkill:
            name = "broken"

            async def run(self, arguments, context):
                del arguments, context
                raise RuntimeError("boom")

        service = SkillService(_llm_stub(), tool_timeout_seconds=0.1)
        service.skills = {"broken": BrokenSkill()}

        result = await service._run_tool(
            name="broken",
            arguments={},
            context=SkillContext(),
        )

        self.assertFalse(result.ok)
        self.assertEqual(result.error, "tool_failed")

    async def test_tool_timeout_returns_without_waiting_for_cancel_ack(self) -> None:
        release = asyncio.Event()

        class StuckSkill:
            name = "stuck"

            async def run(self, arguments, context):
                del arguments, context
                try:
                    await asyncio.sleep(60)
                except asyncio.CancelledError:
                    await release.wait()
                context.handled = True
                return SkillRunResult(ok=True, skill="stuck", summary="late")

        service = SkillService(_llm_stub(), tool_timeout_seconds=0.02)
        service.skills = {"stuck": StuckSkill()}
        loop = asyncio.get_running_loop()
        started = loop.time()

        context = SkillContext()
        result = await service._run_tool(
            name="stuck",
            arguments={},
            context=context,
        )

        self.assertFalse(result.ok)
        self.assertEqual(result.error, "tool_timeout")
        self.assertLess(loop.time() - started, 0.2)
        self.assertEqual(len(skill_service_module._SKILL_ORPHAN_TASKS), 1)
        release.set()
        for _ in range(10):
            if not skill_service_module._SKILL_ORPHAN_TASKS:
                break
            await asyncio.sleep(0)
        self.assertFalse(skill_service_module._SKILL_ORPHAN_TASKS)
        self.assertFalse(context.handled)

    async def test_side_effect_timeout_returns_terminal_ambiguous_result(self) -> None:
        release = asyncio.Event()

        class StuckStickerSkill:
            name = "send_sticker"

            async def run(self, arguments, context):
                del arguments, context
                try:
                    await asyncio.sleep(60)
                except asyncio.CancelledError:
                    await release.wait()
                return SkillRunResult(ok=True, skill=self.name, summary="late")

        service = SkillService(_llm_stub(), tool_timeout_seconds=0.02)
        service.skills = {"send_sticker": StuckStickerSkill()}
        result = await service._run_tool(
            name="send_sticker",
            arguments={},
            context=SkillContext(),
        )

        self.assertFalse(result.ok)
        self.assertEqual(result.error, "tool_outcome_ambiguous")
        self.assertIn("不会自动重试", result.summary)
        release.set()
        for _ in range(10):
            if not skill_service_module._SKILL_ORPHAN_TASKS:
                break
            await asyncio.sleep(0)
        self.assertFalse(skill_service_module._SKILL_ORPHAN_TASKS)

    async def test_shutdown_flush_joins_timed_out_tool_orphan(self) -> None:
        release = asyncio.Event()

        async def cancellation_resistant() -> None:
            try:
                await asyncio.Future()
            except asyncio.CancelledError:
                await release.wait()

        task = asyncio.create_task(cancellation_resistant())
        await asyncio.sleep(0)
        skill_service_module._track_skill_orphan(task)
        flush = asyncio.create_task(
            skill_service_module.flush_skill_execution_tasks(timeout_seconds=1.0)
        )
        await asyncio.sleep(0.01)
        self.assertFalse(flush.done())
        release.set()
        await asyncio.wait_for(flush, timeout=0.5)
        self.assertFalse(skill_service_module._SKILL_ORPHAN_TASKS)


if __name__ == "__main__":
    unittest.main()
