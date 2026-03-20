import unittest
from types import SimpleNamespace

from bot.config import ChatEndpointConfig, EmbedConfig, EmbedEndpointConfig, ModelConfig
from bot.services.llm import LLMService
from bot.utils.runtime_context import build_bot_runtime_profile_context


class RuntimeContextTests(unittest.TestCase):
    def _make_llm(self) -> LLMService:
        return LLMService(
            ModelConfig(
                model="openai/gpt-4.1",
                fallbacks=[
                    ChatEndpointConfig(
                        model="gemini/gemini-2.0-flash",
                        temperature=0.7,
                        max_tokens=2048,
                    )
                ],
            ),
            ModelConfig(
                model="openai/gpt-4.1-mini",
                fallbacks=[
                    ChatEndpointConfig(
                        model="gemini/gemini-2.0-flash-lite",
                        temperature=0.1,
                        max_tokens=512,
                    )
                ],
                temperature=0.1,
                max_tokens=512,
            ),
            ModelConfig(
                model="openai/gpt-4.1-nano",
                temperature=0.3,
                max_tokens=1024,
            ),
            moderation=ModelConfig(
                model="openai/gpt-4.1-mini",
                temperature=0.1,
                max_tokens=1024,
            ),
            embed=EmbedConfig(
                model="gemini/text-embedding-004",
                fallbacks=[EmbedEndpointConfig(model="openai/text-embedding-3-small")],
            ),
        )

    def test_runtime_profile_includes_models_and_skills(self) -> None:
        text = build_bot_runtime_profile_context(
            self._make_llm(),
            settings=SimpleNamespace(
                moderation=SimpleNamespace(enabled=True),
                doubao_tts_model="seed-tts-2.0",
            ),
            skill_names=["websearch", "music_search", "webfetch", "doubao_tts", "websearch"],
        )

        self.assertIn("[BOT_RUNTIME_PROFILE]", text)
        self.assertIn("registered_skills: websearch, music_search, webfetch, doubao_tts", text)
        self.assertIn("联网搜索实时信息", text)
        self.assertIn("音乐搜索、播放链接、封面与歌词", text)
        self.assertIn("抓取网页正文", text)
        self.assertIn("文字转语音", text)
        self.assertIn("main_reply_model: openai/gpt-4.1", text)
        self.assertIn("decision_model: openai/gpt-4.1-mini", text)
        self.assertIn("moderation_model: openai/gpt-4.1-mini", text)
        self.assertIn("compress_model: openai/gpt-4.1-nano", text)
        self.assertIn("embed_model: gemini/text-embedding-004", text)
        self.assertIn("main_reply_fallbacks: gemini/gemini-2.0-flash", text)
        self.assertIn("embed_fallbacks: openai/text-embedding-3-small", text)
        self.assertIn("skill_planner_model: same_as_main_reply", text)
        self.assertIn("vision_model: same_as_main_reply", text)
        self.assertIn("tts_model: seed-tts-2.0", text)

    def test_runtime_profile_marks_disabled_moderation(self) -> None:
        text = build_bot_runtime_profile_context(
            self._make_llm(),
            settings=SimpleNamespace(
                moderation=SimpleNamespace(enabled=False),
                doubao_tts_model="",
            ),
            skill_names=[],
        )

        self.assertIn("user_visible_capabilities: 群聊闲聊与问答；图片/贴纸内容理解；永久记忆读写与上下文摘要；群规审核当前关闭", text)
        self.assertNotIn("tts_model:", text)
