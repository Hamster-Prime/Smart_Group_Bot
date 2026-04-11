import json
import unittest
from unittest.mock import AsyncMock, patch

from bot.services.skills.bilibili_search import BilibiliSearchSkill
from bot.services.skills.douyin_search import DouyinSearchSkill
from bot.services.skills.twitter_x_search import TwitterXSearchSkill


class BilibiliSearchSkillTests(unittest.TestCase):
    def test_extract_bvid_from_url(self) -> None:
        skill = BilibiliSearchSkill()
        self.assertEqual(skill._extract_bvid("https://www.bilibili.com/video/BV1xx411c7mD"), "BV1xx411c7mD")
        self.assertEqual(skill._extract_bvid("BV1ab411c7mD"), "BV1ab411c7mD")
        self.assertEqual(skill._extract_bvid("bv1ab411c7mD"), "BV1ab411c7mD")
        self.assertEqual(skill._extract_bvid("not-a-bvid"), "")

    def test_comment_intent_detection_from_user_text(self) -> None:
        skill = BilibiliSearchSkill()
        self.assertTrue(skill._wants_comments_from_text("总结一下评论"))
        self.assertTrue(skill._wants_comments_from_text("帮我看看热评"))
        self.assertFalse(skill._wants_comments_from_text("总结一下视频内容"))


class BilibiliSearchFallbackTests(unittest.IsolatedAsyncioTestCase):
    async def test_search_users_falls_back_when_api_hits_412(self) -> None:
        skill = BilibiliSearchSkill()
        with (
            patch.object(skill, "_api_get", new=AsyncMock(side_effect=ValueError("http_412"))),
            patch(
                "bot.services.skills.bilibili_search.site_search",
                new=AsyncMock(
                    return_value=(
                        [
                            {
                                "title": "某UP主的个人空间",
                                "url": "https://space.bilibili.com/123456",
                                "snippet": "粉丝很多，做硬件改装视频。",
                            }
                        ],
                        "测试UP site:space.bilibili.com",
                    )
                ),
            ),
        ):
            result = await skill._search_users("测试UP", page=1, max_results=5)

        self.assertTrue(result.ok)
        self.assertEqual(result.payload["effective_query"], "测试UP site:space.bilibili.com")
        self.assertEqual(result.payload["results"][0]["url"], "https://space.bilibili.com/123456")


class TwitterXSearchSkillTests(unittest.TestCase):
    def test_extract_status_id(self) -> None:
        skill = TwitterXSearchSkill()
        self.assertEqual(skill._extract_status_id("https://x.com/openai/status/1234567890123456789"), "1234567890123456789")
        self.assertEqual(skill._extract_status_id("https://twitter.com/openai/status/987654321"), "987654321")
        self.assertEqual(skill._extract_status_id("https://x.com/openai"), "")


class DouyinSearchSkillTests(unittest.TestCase):
    def test_parse_video_id_from_final_url(self) -> None:
        skill = DouyinSearchSkill()
        self.assertEqual(
            skill._parse_video_id_from_final_url("https://www.iesdouyin.com/share/video/7445842287652441376"),
            "7445842287652441376",
        )
        self.assertEqual(
            skill._parse_video_id_from_final_url("https://www.douyin.com/video/7445842287652441376"),
            "7445842287652441376",
        )

    def test_extract_router_data_json(self) -> None:
        skill = DouyinSearchSkill()
        router = {"loaderData": {"video_(id)/page": {"videoInfoRes": {"item_list": []}}}}
        html = f"<script>window._ROUTER_DATA = {router!r}</script>"
        with self.assertRaises(json.JSONDecodeError):
            skill._extract_router_data_json(html)


if __name__ == "__main__":
    unittest.main()
