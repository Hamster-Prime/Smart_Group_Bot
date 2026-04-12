import unittest
from unittest.mock import AsyncMock, patch

from bot.services.skills.bilibili_search import BilibiliSearchSkill


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


if __name__ == "__main__":
    unittest.main()
