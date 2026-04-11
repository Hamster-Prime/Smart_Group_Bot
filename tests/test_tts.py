import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from aiogram.exceptions import TelegramBadRequest

from bot.config import Settings
from bot.handlers import admin
from bot.services.doubao_tts import DoubaoTTSService
from bot.services.doubao_tts import (
    TTS_MODE_ALWAYS,
    TTS_MODE_OFF,
    TTS_MODE_ON,
    build_tts_preference_context,
    build_tts_status_text,
    is_tts_always_enabled,
    is_tts_tool_enabled,
    normalize_tts_mode,
    set_tts_mode,
)
from bot.services.skills.base import SkillContext
from bot.services.skills.doubao_tts import DoubaoTTSSkill
from bot.services.skills.scheduled_task import ScheduledTaskSkill


def _settings() -> Settings:
    settings = Settings(_env_file=None)
    settings.bot.auto_delete_minutes = 0
    return settings


class TTSModeTests(unittest.IsolatedAsyncioTestCase):
    def test_normalize_tts_mode_supports_known_values(self) -> None:
        self.assertEqual(normalize_tts_mode("always"), TTS_MODE_ALWAYS)
        self.assertEqual(normalize_tts_mode({"tts_mode": "on"}), TTS_MODE_ON)
        self.assertEqual(normalize_tts_mode({"tts_mode": "disable"}), TTS_MODE_OFF)

    def test_tts_mode_helpers(self) -> None:
        settings_data = set_tts_mode({}, "always")
        self.assertTrue(is_tts_tool_enabled(settings_data))
        self.assertTrue(is_tts_always_enabled(settings_data))

    def test_build_tts_preference_context_for_enable_mode(self) -> None:
        context = build_tts_preference_context(TTS_MODE_ON, service_ready=True)

        self.assertIn("[GROUP_TTS_PREFERENCE]", context)
        self.assertIn("tts_mode: on", context)
        self.assertIn("mildly prefers voice replies only in selected cases", context)
        self.assertIn("Plain text is still the default", context)

    def test_build_tts_preference_context_for_off_mode_is_empty(self) -> None:
        context = build_tts_preference_context(TTS_MODE_OFF, service_ready=True)

        self.assertEqual(context, "")

    def test_doubao_payload_additions_is_json_string(self) -> None:
        settings = _settings()
        settings.doubao_tts_enabled = True
        settings.doubao_tts_app_id = "app-id"
        settings.doubao_tts_access_key = "access-key"
        settings.doubao_tts_resource_id = "seed-tts-2.0"
        settings.doubao_tts_speaker = "voice_1"
        service = DoubaoTTSService(settings)

        payload = service._build_payload("你好", uid="123")

        self.assertIsInstance(payload["req_params"]["additions"], str)
        self.assertIn("disable_markdown_filter", payload["req_params"]["additions"])
        self.assertEqual(payload["namespace"], "UnidirectionalTTS")

        additions = json.loads(payload["req_params"]["additions"])
        self.assertTrue(additions["cache_config"]["use_cache"])

    def test_doubao_payload_supports_context_and_emotion_scale(self) -> None:
        settings = _settings()
        settings.doubao_tts_enabled = True
        settings.doubao_tts_app_id = "app-id"
        settings.doubao_tts_access_key = "access-key"
        settings.doubao_tts_resource_id = "seed-tts-2.0"
        settings.doubao_tts_speaker = "voice_1"
        service = DoubaoTTSService(settings)

        payload = service._build_payload(
            "真的很抱歉，这次是我处理得不好。",
            uid="123",
            emotion="sad",
            emotion_scale=5,
            speech_rate=-10,
            context="用真诚克制的声音，像在认真道歉，语速稍慢。",
        )

        additions = json.loads(payload["req_params"]["additions"])
        audio_params = payload["req_params"]["audio_params"]

        self.assertEqual(audio_params["emotion"], "sad")
        self.assertEqual(audio_params["emotion_scale"], 5)
        self.assertEqual(audio_params["speech_rate"], -10)
        self.assertIn("context_texts", additions)
        self.assertIn("认真道歉", additions["context_texts"][0])

    def test_doubao_payload_auto_infers_apology_style(self) -> None:
        settings = _settings()
        settings.doubao_tts_enabled = True
        settings.doubao_tts_app_id = "app-id"
        settings.doubao_tts_access_key = "access-key"
        settings.doubao_tts_resource_id = "seed-tts-2.0"
        settings.doubao_tts_speaker = "voice_1"
        service = DoubaoTTSService(settings)

        payload = service._build_payload("真的很抱歉，这次是我们的问题。", uid="123")

        additions = json.loads(payload["req_params"]["additions"])
        audio_params = payload["req_params"]["audio_params"]

        self.assertEqual(audio_params["emotion"], "sad")
        self.assertEqual(audio_params["emotion_scale"], 3)
        self.assertLess(audio_params["speech_rate"], 0)
        self.assertIn("歉意", additions["context_texts"][0])

    def test_split_text_keeps_single_segment_when_under_max_length(self) -> None:
        settings = _settings()
        settings.doubao_tts_enabled = True
        settings.doubao_tts_app_id = "app-id"
        settings.doubao_tts_access_key = "access-key"
        settings.doubao_tts_resource_id = "seed-tts-2.0"
        settings.doubao_tts_speaker = "voice_1"
        service = DoubaoTTSService(settings)

        parts = service.split_text(
            "别着急呀 你可以先试试这几个办法：1. 检查下自己的网络是否正常，或者切换下节点试试；2. 看看对应服务的官方有没有发布维护通知；3. 也可以问问群里其他小伙伴。"
        )

        self.assertEqual(len(parts), 1)

    def test_normalize_text_replaces_semicolons_for_tts(self) -> None:
        settings = _settings()
        settings.doubao_tts_enabled = True
        settings.doubao_tts_app_id = "app-id"
        settings.doubao_tts_access_key = "access-key"
        settings.doubao_tts_resource_id = "seed-tts-2.0"
        settings.doubao_tts_speaker = "voice_1"
        service = DoubaoTTSService(settings)

        normalized = service.normalize_text("第一步；第二步;第三步")

        self.assertEqual(normalized, "第一步，第二步，第三步")

    def test_normalize_text_handles_markdown_lists_and_urls(self) -> None:
        settings = _settings()
        settings.doubao_tts_enabled = True
        settings.doubao_tts_app_id = "app-id"
        settings.doubao_tts_access_key = "access-key"
        settings.doubao_tts_resource_id = "seed-tts-2.0"
        settings.doubao_tts_speaker = "voice_1"
        service = DoubaoTTSService(settings)

        normalized = service.normalize_text(
            "先看这个链接：https://example.com/test\n"
            "1. **检查网络**\n"
            "2) `重启客户端`\n"
            "- 再试一次 / 或者切换节点"
        )

        self.assertEqual(
            normalized,
            "先看这个链接：链接，1、检查网络，2、重启客户端，再试一次，或者切换节点",
        )

    async def test_voice_payload_uses_mp3_source_then_transcodes_to_ogg(self) -> None:
        settings = _settings()
        settings.doubao_tts_enabled = True
        settings.doubao_tts_app_id = "app-id"
        settings.doubao_tts_access_key = "access-key"
        settings.doubao_tts_resource_id = "seed-tts-2.0"
        settings.doubao_tts_speaker = "voice_1"
        settings.doubao_tts_audio_format = "ogg_opus"
        service = DoubaoTTSService(settings)

        with (
            patch.object(
                service,
                "synthesize",
                new=AsyncMock(
                    return_value=SimpleNamespace(
                        ok=True,
                        text="test",
                        audio_bytes=b"mp3-data",
                        audio_format="mp3",
                        usage={},
                        logid="logid",
                    )
                ),
            ) as synth_mock,
            patch("bot.services.doubao_tts.asyncio.to_thread", new=AsyncMock(return_value=b"ogg-data")),
        ):
            result = await service.synthesize_voice_payload("test", uid="1")

        self.assertTrue(result.ok)
        self.assertEqual(result.audio_format, "ogg_opus")
        self.assertEqual(result.audio_bytes, b"ogg-data")
        self.assertEqual(synth_mock.await_args.kwargs["audio_format"], "mp3")


class AdminTTSCommandTests(unittest.IsolatedAsyncioTestCase):
    async def test_cmd_tts_without_args_shows_status(self) -> None:
        message = SimpleNamespace(
            chat=SimpleNamespace(id=-10001, type="supergroup", title="test"),
            text="/tts",
        )
        session = SimpleNamespace(flush=AsyncMock())
        group_row = SimpleNamespace(settings={"tts_mode": TTS_MODE_OFF})
        settings = _settings()

        with (
            patch("bot.handlers.admin.ensure_group_authorized", new=AsyncMock(return_value=True)),
            patch("bot.handlers.admin.ensure_super_admin", new=AsyncMock(return_value=True)),
            patch("bot.handlers.admin._ensure_group_row", new=AsyncMock(return_value=group_row)),
            patch("bot.handlers.admin._answer", new=AsyncMock()) as answer_mock,
        ):
            await admin.cmd_tts(message, session=session, settings=settings)

        self.assertEqual(
            answer_mock.await_args.args[2],
            build_tts_status_text(
                group_id=message.chat.id,
                group_settings=group_row.settings,
                service_ready=False,
            ),
        )
        session.flush.assert_not_awaited()

    async def test_cmd_tts_rejects_enable_when_service_not_configured(self) -> None:
        message = SimpleNamespace(
            chat=SimpleNamespace(id=-10001, type="supergroup", title="test"),
            text="/tts enable",
        )
        session = SimpleNamespace(flush=AsyncMock())
        group_row = SimpleNamespace(settings={})
        settings = _settings()

        with (
            patch("bot.handlers.admin.ensure_group_authorized", new=AsyncMock(return_value=True)),
            patch("bot.handlers.admin.ensure_super_admin", new=AsyncMock(return_value=True)),
            patch("bot.handlers.admin._ensure_group_row", new=AsyncMock(return_value=group_row)),
            patch("bot.handlers.admin._answer", new=AsyncMock()) as answer_mock,
        ):
            await admin.cmd_tts(message, session=session, settings=settings)

        self.assertEqual(group_row.settings, {})
        session.flush.assert_not_awaited()
        self.assertIn("尚未配置完成", answer_mock.await_args.args[2])

    async def test_cmd_tts_requires_super_admin(self) -> None:
        message = SimpleNamespace(
            chat=SimpleNamespace(id=-10001, type="supergroup", title="test"),
            text="/tts disable",
        )
        session = SimpleNamespace(flush=AsyncMock())
        settings = _settings()

        with (
            patch("bot.handlers.admin.ensure_group_authorized", new=AsyncMock(return_value=True)),
            patch("bot.handlers.admin.ensure_super_admin", new=AsyncMock(return_value=False)) as super_admin_mock,
            patch("bot.handlers.admin._ensure_group_row", new=AsyncMock()) as ensure_group_row_mock,
            patch("bot.handlers.admin._answer", new=AsyncMock()) as answer_mock,
        ):
            await admin.cmd_tts(message, session=session, settings=settings)

        super_admin_mock.assert_awaited_once()
        ensure_group_row_mock.assert_not_awaited()
        session.flush.assert_not_awaited()
        answer_mock.assert_not_awaited()

    async def test_cmd_tts_rejects_legacy_aliases(self) -> None:
        message = SimpleNamespace(
            chat=SimpleNamespace(id=-10001, type="supergroup", title="test"),
            text="/tts on",
        )
        session = SimpleNamespace(flush=AsyncMock())
        group_row = SimpleNamespace(settings={})
        settings = _settings()

        with (
            patch("bot.handlers.admin.ensure_group_authorized", new=AsyncMock(return_value=True)),
            patch("bot.handlers.admin.ensure_super_admin", new=AsyncMock(return_value=True)),
            patch("bot.handlers.admin._ensure_group_row", new=AsyncMock(return_value=group_row)),
            patch("bot.handlers.admin._answer", new=AsyncMock()) as answer_mock,
        ):
            await admin.cmd_tts(message, session=session, settings=settings)

        self.assertEqual(group_row.settings, {})
        session.flush.assert_not_awaited()
        self.assertEqual(answer_mock.await_args.args[2], admin._TTS_USAGE)

    async def test_cmd_tts_sets_always_mode_when_service_ready(self) -> None:
        message = SimpleNamespace(
            chat=SimpleNamespace(id=-10001, type="supergroup", title="test"),
            text="/tts always",
        )
        session = SimpleNamespace(flush=AsyncMock())
        group_row = SimpleNamespace(settings={})
        settings = _settings()
        settings.doubao_tts_enabled = True
        settings.doubao_tts_app_id = "app-id"
        settings.doubao_tts_access_key = "access-key"
        settings.doubao_tts_resource_id = "seed-tts-2.0"
        settings.doubao_tts_speaker = "voice_1"

        with (
            patch("bot.handlers.admin.ensure_group_authorized", new=AsyncMock(return_value=True)),
            patch("bot.handlers.admin.ensure_super_admin", new=AsyncMock(return_value=True)),
            patch("bot.handlers.admin._ensure_group_row", new=AsyncMock(return_value=group_row)),
            patch("bot.handlers.admin._answer", new=AsyncMock()) as answer_mock,
        ):
            await admin.cmd_tts(message, session=session, settings=settings)

        self.assertEqual(group_row.settings["tts_mode"], TTS_MODE_ALWAYS)
        session.flush.assert_awaited()
        self.assertIn("始终使用 TTS 输出", answer_mock.await_args.args[2])


class ScheduledTaskTTSModeTests(unittest.IsolatedAsyncioTestCase):
    async def test_always_tts_does_not_fallback_to_text(self) -> None:
        settings = _settings()
        settings.doubao_tts_enabled = True
        settings.doubao_tts_app_id = "app-id"
        settings.doubao_tts_access_key = "access-key"
        settings.doubao_tts_resource_id = "seed-tts-2.0"
        settings.doubao_tts_speaker = "voice_1"
        skill = ScheduledTaskSkill(settings)
        skill.tts_service.send_chat_tts = AsyncMock(return_value=False)  # type: ignore[method-assign]

        context = SkillContext(
            session=object(),
            bot=object(),
            chat_id=-10001,
            llm=SimpleNamespace(chat=AsyncMock(return_value="测试语音")),
            sender_user_id=123,
        )

        with (
            patch("bot.services.skills.scheduled_task.load_group_tts_mode", new=AsyncMock(return_value="always")),
            patch("bot.services.skills.scheduled_task.send_chat_message", new=AsyncMock(return_value=True)) as send_text,
        ):
            result = await skill.run(
                {"task_name": "reminder", "task_brief": "提醒一下", "send_message": True},
                context,
            )

        self.assertFalse(result.ok)
        send_text.assert_not_awaited()


class DoubaoTTSReplyFallbackTests(unittest.IsolatedAsyncioTestCase):
    async def test_send_chat_tts_mentions_target_when_reply_target_is_missing(self) -> None:
        settings = _settings()
        settings.doubao_tts_enabled = True
        settings.doubao_tts_app_id = "app-id"
        settings.doubao_tts_access_key = "access-key"
        settings.doubao_tts_resource_id = "seed-tts-2.0"
        settings.doubao_tts_speaker = "voice_1"
        service = DoubaoTTSService(settings)

        bot = SimpleNamespace(
            send_voice=AsyncMock(
                side_effect=[
                    TelegramBadRequest(
                        method=SimpleNamespace(),
                        message="message to be replied not found",
                    ),
                    SimpleNamespace(message_id=88, chat=SimpleNamespace(id=-10001)),
                ]
            )
        )

        with patch.object(
            service,
            "synthesize_voice_payload",
            new=AsyncMock(
                return_value=SimpleNamespace(
                    ok=True,
                    text="测试语音",
                    audio_bytes=b"ogg-data",
                    audio_format="ogg_opus",
                    usage={},
                    logid="logid",
                )
            ),
        ):
            ok = await service.send_chat_tts(
                bot,
                -10001,
                "测试语音",
                reply_to_message_id=123,
                fallback_mention_user_id=42,
                fallback_mention_name="Alice",
                auto_delete_minutes=0,
                uid="1",
            )

        self.assertTrue(ok)
        self.assertEqual(bot.send_voice.await_count, 2)

        first_kwargs = bot.send_voice.await_args_list[0].kwargs
        second_kwargs = bot.send_voice.await_args_list[1].kwargs

        self.assertEqual(first_kwargs["reply_to_message_id"], 123)
        self.assertEqual(second_kwargs["reply_to_message_id"], None)
        self.assertEqual(second_kwargs["parse_mode"], "HTML")
        self.assertIn('tg://user?id=42', second_kwargs["caption"])
        self.assertIn("@Alice", second_kwargs["caption"])


class DoubaoTTSSkillTests(unittest.IsolatedAsyncioTestCase):
    async def test_skill_passes_context_and_emotion_scale_to_service(self) -> None:
        tts_service = SimpleNamespace(
            send_message_tts=AsyncMock(return_value=True),
            send_chat_tts=AsyncMock(return_value=True),
        )
        skill = DoubaoTTSSkill(tts_service)
        context = SkillContext(
            message=SimpleNamespace(),
            chat_id=-10001,
            sender_user_id=42,
            current_user_text="原始文本",
        )

        result = await skill.run(
            {
                "text": "你别急，我在。",
                "emotion": "calm",
                "emotion_scale": 5,
                "context": "像在认真安慰朋友，声音放轻一点，语速慢一点。",
                "speech_rate": -12,
            },
            context,
        )

        self.assertTrue(result.ok)
        self.assertEqual(result.payload["emotion_scale"], 5)
        kwargs = tts_service.send_message_tts.await_args.kwargs
        self.assertEqual(kwargs["emotion"], "calm")
        self.assertEqual(kwargs["emotion_scale"], 5)
        self.assertEqual(kwargs["speech_rate"], -12)
        self.assertIn("认真安慰朋友", kwargs["context"])


if __name__ == "__main__":
    unittest.main()
