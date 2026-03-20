import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from bot.services.skills.music_search import MusicSearchSkill


class MusicSearchSkillTests(unittest.IsolatedAsyncioTestCase):
    def _skill(self, **kwargs: object) -> MusicSearchSkill:
        config = {
            "music_api_enabled": True,
            "music_api_http_timeout_sec": 15.0,
            "music_api_base_url": "https://music-api.gdstudio.xyz/api.php",
            "music_api_default_source": "netease",
            "music_api_stable_sources": "netease,kuwo,joox,bilibili",
        }
        config.update(kwargs)
        settings = SimpleNamespace(**config)
        return MusicSearchSkill(settings)

    async def test_search_auto_tries_stable_sources_until_match(self) -> None:
        skill = self._skill(music_api_stable_sources="netease,kuwo")

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
                            "source": "kuwo",
                        }
                    ],
                ]
            ),
        ) as request_mock:
            result = await skill.run({"action": "search", "keyword": "稻香"}, context=SimpleNamespace())

        self.assertTrue(result.ok)
        self.assertEqual(result.payload["source"], "kuwo")
        self.assertEqual(result.payload["attempted_sources"], ["netease", "kuwo"])
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
