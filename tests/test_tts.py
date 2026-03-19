import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from bot.config import Settings
from bot.handlers import admin
from bot.services.doubao_tts import DoubaoTTSService
from bot.services.doubao_tts import (
    TTS_MODE_ALWAYS,
    TTS_MODE_OFF,
    TTS_MODE_ON,
    is_tts_always_enabled,
    is_tts_tool_enabled,
    normalize_tts_mode,
    set_tts_mode,
)
from bot.services.skills.base import SkillContext
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
    async def test_cmd_tts_rejects_enable_when_service_not_configured(self) -> None:
        message = SimpleNamespace(
            chat=SimpleNamespace(id=-10001, type="supergroup", title="test"),
            text="/tts on",
        )
        session = SimpleNamespace(flush=AsyncMock())
        group_row = SimpleNamespace(settings={})
        settings = _settings()

        with (
            patch("bot.handlers.admin.ensure_group_authorized", new=AsyncMock(return_value=True)),
            patch("bot.handlers.admin.ensure_group_admin_permission", new=AsyncMock(return_value=True)),
            patch("bot.handlers.admin._ensure_group_row", new=AsyncMock(return_value=group_row)),
            patch("bot.handlers.admin._answer", new=AsyncMock()) as answer_mock,
        ):
            await admin.cmd_tts(message, session=session, settings=settings)

        self.assertEqual(group_row.settings, {})
        session.flush.assert_not_awaited()
        self.assertIn("尚未配置完成", answer_mock.await_args.args[2])

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
            patch("bot.handlers.admin.ensure_group_admin_permission", new=AsyncMock(return_value=True)),
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


if __name__ == "__main__":
    unittest.main()
