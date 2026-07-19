import unittest
from unittest.mock import AsyncMock, patch

from bot.services.skills.platform_common import InvalidJsonResponseError, fetch_json
from bot.services.skills.weibo_search import WeiboSearchSkill


class PlatformCommonJsonTests(unittest.IsolatedAsyncioTestCase):
    async def test_fetch_json_raises_invalid_json_response_error_with_context(self) -> None:
        with patch(
            "bot.services.skills.platform_common.fetch_text",
            new=AsyncMock(return_value=(200, "<html>blocked</html>", "https://m.weibo.cn/api/x", "text/html")),
        ):
            with self.assertRaises(InvalidJsonResponseError) as ctx:
                await fetch_json("https://m.weibo.cn/api/x")

        self.assertEqual(ctx.exception.status, 200)
        self.assertEqual(ctx.exception.content_type, "text/html")
        self.assertIn("blocked", ctx.exception.body_preview)
        self.assertIn("invalid_json_response", str(ctx.exception))


class WeiboSearchSkillTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def _invalid_json_error(url: str) -> InvalidJsonResponseError:
        return InvalidJsonResponseError(
            url=url,
            status=200,
            content_type="text/html",
            body_preview="<html>risk control</html>",
        )

    async def test_search_posts_falls_back_to_web_search_when_api_returns_non_json(self) -> None:
        skill = WeiboSearchSkill()
        invalid = self._invalid_json_error("https://m.weibo.cn/api/container/getIndex")

        with (
            patch("bot.services.skills.weibo_search.fetch_json", new=AsyncMock(side_effect=invalid)),
            patch(
                "bot.services.skills.weibo_search.site_search",
                new=AsyncMock(
                    return_value=(
                        [
                            {
                                "title": "测试微博",
                                "url": "https://weibo.com/123/AbCdEf",
                                "snippet": "这是一条网页搜索兜底结果",
                            }
                        ],
                        "测试关键词 site:weibo.com",
                    )
                ),
            ),
        ):
            result = await skill._search_posts("测试关键词", page=1, max_results=5)

        self.assertTrue(result.ok)
        self.assertEqual(result.payload["action"], "search_posts")
        self.assertEqual(result.payload["fallback"], "web_search")
        self.assertEqual(result.payload["effective_query"], "测试关键词 site:weibo.com")
        self.assertEqual(result.payload["results"][0]["url"], "https://weibo.com/123/AbCdEf")

    async def test_hot_search_falls_back_to_web_search_when_api_returns_non_json(self) -> None:
        skill = WeiboSearchSkill()
        invalid = self._invalid_json_error("https://weibo.com/ajax/side/hotSearch")

        with (
            patch("bot.services.skills.weibo_search.fetch_json", new=AsyncMock(side_effect=invalid)),
            patch(
                "bot.services.skills.weibo_search.site_search",
                new=AsyncMock(
                    return_value=(
                        [
                            {
                                "title": "微博热搜榜",
                                "url": "https://s.weibo.com/top/summary",
                                "snippet": "热搜相关网页结果",
                            }
                        ],
                        "微博热搜 site:s.weibo.com",
                    )
                ),
            ),
        ):
            result = await skill._hot_search(max_results=5)

        self.assertTrue(result.ok)
        self.assertEqual(result.payload["action"], "hot_search")
        self.assertEqual(result.payload["fallback"], "web_search")
        self.assertEqual(result.payload["effective_query"], "微博热搜 site:s.weibo.com")

    async def test_hot_feed_falls_back_to_web_search_when_api_returns_non_json(self) -> None:
        skill = WeiboSearchSkill()
        invalid = self._invalid_json_error("https://weibo.com/ajax/feed/hottimeline")

        with (
            patch("bot.services.skills.weibo_search.fetch_json", new=AsyncMock(side_effect=invalid)),
            patch(
                "bot.services.skills.weibo_search.site_search",
                new=AsyncMock(
                    return_value=(
                        [
                            {
                                "title": "一条热门微博",
                                "url": "https://weibo.com/123/HotFeed",
                                "snippet": "热门内容网页兜底结果",
                            }
                        ],
                        "微博热门 site:weibo.com",
                    )
                ),
            ),
        ):
            result = await skill._get_hot_feed(max_results=5)

        self.assertTrue(result.ok)
        self.assertEqual(result.payload["action"], "get_hot_feed")
        self.assertEqual(result.payload["fallback"], "web_search")
        self.assertEqual(result.payload["effective_query"], "微博热门 site:weibo.com")
        self.assertEqual(result.payload["results"][0]["url"], "https://weibo.com/123/HotFeed")

    def test_weibo_url_filter_rejects_host_suffix_confusion(self) -> None:
        self.assertFalse(
            WeiboSearchSkill._is_weibo_post_url(
                "https://weibo.com.attacker.example/status/123"
            )
        )
        self.assertTrue(
            WeiboSearchSkill._is_weibo_post_url("https://m.weibo.cn/detail/123")
        )


if __name__ == "__main__":
    unittest.main()
