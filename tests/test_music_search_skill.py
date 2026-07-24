import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from bot.services.skills.music_search import MusicSearchSkill


class MusicSearchSkillTests(unittest.IsolatedAsyncioTestCase):
    def _skill(self, **kwargs: object) -> MusicSearchSkill:
        config = {
            "music_api_enabled": True,
            "music_api_http_timeout_sec": 15.0,
            "music_api_base_url": "https://music-api.gdstudio.xyz/api.php",
            "music_api_default_source": "kuwo",
            "music_api_stable_sources": "kuwo,netease,joox,bilibili",
        }
        config.update(kwargs)
        settings = SimpleNamespace(**config)
        return MusicSearchSkill(settings)

    async def test_search_auto_tries_stable_sources_until_match(self) -> None:
        skill = self._skill(music_api_stable_sources="kuwo,netease")

        with patch.object(
            skill,
            "_request_json",
            new=AsyncMock(
                side_effect=[
                    [],
                    [
                        {
                            "id": "1",
                            "name": "稻香",
                            "artist": ["周杰伦"],
                            "album": "魔杰座",
                            "pic_id": "p1",
                            "lyric_id": "l1",
                            "source": "netease",
                        }
                    ],
                ]
            ),
        ) as request_mock:
            result = await skill.run({"action": "search", "keyword": "稻香"}, context=SimpleNamespace())

        self.assertTrue(result.ok)
        self.assertEqual(result.payload["source"], "netease")
        self.assertEqual(result.payload["attempted_sources"], ["kuwo", "netease"])
        self.assertEqual(result.payload["results"][0]["track_id"], "1")
        self.assertEqual(result.payload["results"][0]["artist_text"], "周杰伦")
        self.assertEqual(request_mock.await_count, 2)

    async def test_get_url_returns_link_payload(self) -> None:
        skill = self._skill()

        with patch.object(
            skill,
            "_request_json",
            new=AsyncMock(return_value={"url": "https://example.com/song.mp3", "br": 320, "size": 12345}),
        ) as request_mock:
            result = await skill.run(
                {"action": "get_url", "source": "netease", "track_id": "5257138", "quality": 320},
                context=SimpleNamespace(),
            )

        self.assertTrue(result.ok)
        self.assertEqual(result.payload["url"], "https://example.com/song.mp3")
        self.assertEqual(result.payload["bitrate"], 320)
        self.assertEqual(result.payload["size_kb"], 12345)
        self.assertEqual(request_mock.await_args.args[0]["types"], "url")
        self.assertEqual(request_mock.await_args.args[0]["id"], "5257138")

    async def test_send_audio_uses_remote_url_without_local_download(self) -> None:
        skill = self._skill()
        message = SimpleNamespace(
            reply_audio=AsyncMock(return_value=SimpleNamespace(chat=SimpleNamespace(id=-10001), message_id=10)),
            answer_audio=AsyncMock(return_value=SimpleNamespace(chat=SimpleNamespace(id=-10001), message_id=11)),
        )

        with patch.object(
            skill,
            "_request_json",
            new=AsyncMock(return_value={"url": "https://example.com/song.flac", "br": 999, "size": 54321}),
        ):
            result = await skill.run(
                {
                    "action": "send_audio",
                    "source": "netease",
                    "track_id": "5257138",
                    "title": "稻香",
                    "performer": "周杰伦",
                },
                context=SimpleNamespace(message=message, bot=None, chat_id=0, handled=False),
            )

        self.assertTrue(result.ok)
        self.assertEqual(result.payload["transport"], "telegram_remote_url")
        self.assertEqual(result.payload["url"], "https://example.com/song.flac")
        message.reply_audio.assert_awaited_once()
        self.assertEqual(message.reply_audio.await_args.kwargs["audio"], "https://example.com/song.flac")
        self.assertEqual(message.reply_audio.await_args.kwargs["title"], "稻香")
        self.assertEqual(message.reply_audio.await_args.kwargs["performer"], "周杰伦")
        self.assertEqual(message.reply_audio.await_args.kwargs["parse_mode"], "HTML")
        self.assertIn("这首", message.reply_audio.await_args.kwargs["caption"])

    async def test_post_delivery_cleanup_failure_does_not_resend_audio(self) -> None:
        skill = self._skill()
        sent = SimpleNamespace(chat=SimpleNamespace(id=-10001), message_id=10)
        message = SimpleNamespace(
            reply_audio=AsyncMock(return_value=sent),
            answer_audio=AsyncMock(),
        )
        receipt = Mock()

        with patch(
            "bot.services.skills.music_search.schedule_message_auto_delete_durable",
            new=AsyncMock(side_effect=RuntimeError("cleanup unavailable")),
        ):
            ok = await skill._send_audio_to_message(
                message,
                audio_url="https://example.com/song.mp3",
                title="稻香",
                performer="周杰伦",
                caption_text="这首《稻香》给你。",
                delivery_mode="reply",
                auto_delete_seconds=60,
                on_delivery=receipt,
            )

        self.assertTrue(ok)
        message.reply_audio.assert_awaited_once()
        receipt.assert_called_once_with()

    async def test_send_audio_can_search_first_then_send_top_result(self) -> None:
        skill = self._skill(music_api_stable_sources="kuwo")
        message = SimpleNamespace(
            reply_audio=AsyncMock(return_value=SimpleNamespace(chat=SimpleNamespace(id=-10001), message_id=10)),
            answer_audio=AsyncMock(return_value=SimpleNamespace(chat=SimpleNamespace(id=-10001), message_id=11)),
        )

        with patch.object(
            skill,
            "_request_json",
            new=AsyncMock(
                side_effect=[
                    [
                        {
                            "id": "1",
                            "name": "稻香",
                            "artist": ["周杰伦"],
                            "album": "魔杰座",
                            "pic_id": "p1",
                            "lyric_id": "l1",
                            "source": "netease",
                        }
                    ],
                    {"url": "https://example.com/song.mp3", "br": 320, "size": 12345},
                ]
            ),
        ) as request_mock:
            result = await skill.run(
                {"action": "send_audio", "keyword": "稻香"},
                context=SimpleNamespace(message=message, bot=None, chat_id=0, handled=False),
            )

        self.assertTrue(result.ok)
        self.assertEqual(result.payload["track_id"], "1")
        self.assertEqual(result.payload["title"], "稻香")
        self.assertEqual(result.payload["performer"], "周杰伦")
        self.assertEqual(result.payload["requested_quality"], 320)
        self.assertEqual(request_mock.await_count, 2)
        self.assertEqual(request_mock.await_args_list[1].args[0]["br"], 320)

    async def test_send_audio_ignores_lossless_request_and_still_uses_320(self) -> None:
        skill = self._skill()
        message = SimpleNamespace(
            reply_audio=AsyncMock(return_value=SimpleNamespace(chat=SimpleNamespace(id=-10001), message_id=10)),
            answer_audio=AsyncMock(return_value=SimpleNamespace(chat=SimpleNamespace(id=-10001), message_id=11)),
        )

        with patch.object(
            skill,
            "_request_json",
            new=AsyncMock(return_value={"url": "https://example.com/song.flac", "br": 999, "size": 54321}),
        ) as request_mock:
            result = await skill.run(
                {"action": "send_audio", "source": "netease", "track_id": "5257138"},
                context=SimpleNamespace(
                    message=message,
                    bot=None,
                    chat_id=0,
                    handled=False,
                    current_user_text="来一首无损的一次就好",
                ),
            )

        self.assertTrue(result.ok)
        self.assertEqual(result.payload["requested_quality"], 320)
        self.assertEqual(request_mock.await_args.args[0]["br"], 320)

    async def test_send_audio_uses_caption_text_in_same_message(self) -> None:
        skill = self._skill()
        message = SimpleNamespace(
            reply_audio=AsyncMock(return_value=SimpleNamespace(chat=SimpleNamespace(id=-10001), message_id=10)),
            answer_audio=AsyncMock(return_value=SimpleNamespace(chat=SimpleNamespace(id=-10001), message_id=11)),
        )

        with patch.object(
            skill,
            "_request_json",
            new=AsyncMock(return_value={"url": "https://example.com/song.mp3", "br": 320, "size": 12345}),
        ):
            context = SimpleNamespace(
                message=message,
                bot=None,
                chat_id=0,
                handled=False,
                embedded_reply_sent=False,
                embedded_reply_text="",
            )
            result = await skill.run(
                {
                    "action": "send_audio",
                    "source": "netease",
                    "track_id": "5257138",
                    "title": "一次就好",
                    "performer": "杨宗纬",
                    "caption_text": "这首《一次就好》给你，慢慢听。",
                },
                context=context,
            )

        self.assertTrue(result.ok)
        self.assertTrue(context.embedded_reply_sent)
        self.assertEqual(context.embedded_reply_text, "这首《一次就好》给你，慢慢听。")
        self.assertIn("一次就好", message.reply_audio.await_args.kwargs["caption"])

    async def test_get_lyric_can_return_plain_text(self) -> None:
        skill = self._skill()

        with patch.object(
            skill,
            "_request_json",
            new=AsyncMock(
                return_value={
                    "lyric": "[00:00.00]作词 : 周杰伦\n[00:02.00]半夜睡不着觉",
                    "tlyric": "",
                }
            ),
        ):
            result = await skill.run(
                {"action": "get_lyric", "source": "netease", "lyric_id": "5257138", "plain_text": True},
                context=SimpleNamespace(),
            )

        self.assertTrue(result.ok)
        self.assertIn("作词 : 周杰伦", result.payload["plain_lyric"])
        self.assertIn("半夜睡不着觉", result.payload["plain_lyric"])
        self.assertNotIn("[00:00.00]", result.payload["plain_lyric"])

    async def test_disabled_skill_returns_error(self) -> None:
        skill = self._skill(music_api_enabled=False)

        result = await skill.run({"action": "search", "keyword": "稻香"}, context=SimpleNamespace())

        self.assertFalse(result.ok)
        self.assertEqual(result.error, "disabled")


if __name__ == "__main__":
    unittest.main()
