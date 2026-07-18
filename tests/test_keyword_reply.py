import time
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from bot.config import Settings
from bot.db.models import KeywordReply
from bot.services.keyword_reply import (
    find_keyword_reply,
    keyword_rule_matches,
    send_keyword_reply,
)


def _rule(**overrides) -> KeywordReply:
    values = {
        "id": 1,
        "group_id": -10001,
        "keyword": "签到",
        "match_type": "contains",
        "reply_text": "欢迎签到！",
        "buttons": [],
        "pin_message": False,
        "auto_delete": True,
        "enabled": True,
    }
    values.update(overrides)
    return KeywordReply(**values)


def _settings() -> Settings:
    settings = Settings(_env_file=None)
    settings.bot.auto_delete_seconds = 30
    settings.bot.auto_delete_categories = ["keyword"]
    return settings


def _message() -> SimpleNamespace:
    return SimpleNamespace(
        chat=SimpleNamespace(id=-10001, type="supergroup"),
        message_id=555,
        bot=SimpleNamespace(pin_chat_message=AsyncMock()),
    )


class KeywordMatchTests(unittest.TestCase):
    def test_contains_match_is_case_insensitive(self) -> None:
        rule = _rule(keyword="Hello")
        self.assertTrue(keyword_rule_matches(rule, "说一句 hello 吧"))
        self.assertFalse(keyword_rule_matches(rule, "没有关键词"))

    def test_exact_match_requires_whole_message(self) -> None:
        rule = _rule(match_type="exact")
        self.assertTrue(keyword_rule_matches(rule, " 签到 "))
        self.assertFalse(keyword_rule_matches(rule, "我要签到"))

    def test_regex_match(self) -> None:
        rule = _rule(match_type="regex", keyword=r"^/?签到\d*$")
        self.assertTrue(keyword_rule_matches(rule, "签到123"))
        self.assertFalse(keyword_rule_matches(rule, "补签到"))

    def test_invalid_regex_never_matches(self) -> None:
        rule = _rule(match_type="regex", keyword="([")
        self.assertFalse(keyword_rule_matches(rule, "(["))

    def test_catastrophic_regex_times_out_instead_of_hanging(self) -> None:
        rule = _rule(match_type="regex", keyword=r"(a+)+$")
        started = time.monotonic()
        self.assertFalse(keyword_rule_matches(rule, "a" * 64 + "b"))
        self.assertLess(time.monotonic() - started, 2.0)

    def test_regex_matches_long_messages(self) -> None:
        rule = _rule(match_type="regex", keyword=r"尾部关键词")
        self.assertTrue(keyword_rule_matches(rule, "x" * 5000 + "尾部关键词"))

    def test_empty_keyword_or_text_never_matches(self) -> None:
        self.assertFalse(keyword_rule_matches(_rule(keyword="  "), "签到"))
        self.assertFalse(keyword_rule_matches(_rule(), ""))


class FindKeywordReplyTests(unittest.IsolatedAsyncioTestCase):
    async def test_returns_first_matching_enabled_rule(self) -> None:
        rules = [
            _rule(id=1, keyword="不匹配"),
            _rule(id=2, keyword="签到"),
            _rule(id=3, keyword="签"),
        ]
        scalars_result = SimpleNamespace(all=lambda: rules)
        session = SimpleNamespace(scalars=AsyncMock(return_value=scalars_result))

        found = await find_keyword_reply(session, -10001, "今日签到")

        self.assertIsNotNone(found)
        self.assertEqual(found.id, 2)

    async def test_returns_none_without_match(self) -> None:
        scalars_result = SimpleNamespace(all=lambda: [_rule(keyword="午饭")])
        session = SimpleNamespace(scalars=AsyncMock(return_value=scalars_result))

        self.assertIsNone(await find_keyword_reply(session, -10001, "今日签到"))


class SendKeywordReplyTests(unittest.IsolatedAsyncioTestCase):
    async def test_send_uses_keyword_auto_delete_category(self) -> None:
        message = _message()
        sent = SimpleNamespace(message_id=777)
        with patch(
            "bot.services.keyword_reply.answer_with_auto_delete",
            new=AsyncMock(return_value=sent),
        ) as answer_mock:
            ok = await send_keyword_reply(message, _rule(), _settings())

        self.assertTrue(ok)
        kwargs = answer_mock.await_args.kwargs
        self.assertEqual(kwargs["auto_delete_seconds"], 30)
        self.assertEqual(kwargs["reply_to_message_id"], 555)
        self.assertEqual(kwargs["parse_mode"], "HTML")
        message.bot.pin_chat_message.assert_not_awaited()

    async def test_markdown_and_inline_buttons_share_template_renderer(self) -> None:
        message = _message()
        rule = _rule(
            reply_text="**欢迎**\n第二行",
            buttons=[
                {
                    "text": "官网",
                    "action": "url",
                    "value": "https://example.com",
                    "row": 0,
                }
            ],
        )
        with patch(
            "bot.services.keyword_reply.answer_with_auto_delete",
            new=AsyncMock(return_value=SimpleNamespace(message_id=1)),
        ) as answer_mock:
            await send_keyword_reply(message, rule, _settings())

        self.assertIn("<b>欢迎</b>\n第二行", answer_mock.await_args.args[1])
        keyboard = answer_mock.await_args.kwargs["reply_markup"]
        self.assertEqual(keyboard.inline_keyboard[0][0].url, "https://example.com")

    async def test_auto_delete_opt_out_sends_persistent_reply(self) -> None:
        message = _message()
        with patch(
            "bot.services.keyword_reply.answer_with_auto_delete",
            new=AsyncMock(return_value=SimpleNamespace(message_id=1)),
        ) as answer_mock:
            await send_keyword_reply(message, _rule(auto_delete=False), _settings())

        self.assertEqual(answer_mock.await_args.kwargs["auto_delete_seconds"], 0)

    async def test_pin_message_pins_sent_reply(self) -> None:
        message = _message()
        sent = SimpleNamespace(message_id=777)
        with patch(
            "bot.services.keyword_reply.answer_with_auto_delete",
            new=AsyncMock(return_value=sent),
        ):
            await send_keyword_reply(message, _rule(pin_message=True), _settings())

        message.bot.pin_chat_message.assert_awaited_once()
        kwargs = message.bot.pin_chat_message.await_args.kwargs
        self.assertEqual(kwargs["chat_id"], -10001)
        self.assertEqual(kwargs["message_id"], 777)

    async def test_pin_failure_does_not_fail_send(self) -> None:
        message = _message()
        message.bot.pin_chat_message = AsyncMock(side_effect=RuntimeError("no rights"))
        with patch(
            "bot.services.keyword_reply.answer_with_auto_delete",
            new=AsyncMock(return_value=SimpleNamespace(message_id=1)),
        ):
            ok = await send_keyword_reply(message, _rule(pin_message=True), _settings())

        self.assertTrue(ok)

    async def test_send_failure_returns_false(self) -> None:
        message = _message()
        with patch(
            "bot.services.keyword_reply.answer_with_auto_delete",
            new=AsyncMock(side_effect=RuntimeError("telegram down")),
        ):
            ok = await send_keyword_reply(message, _rule(), _settings())

        self.assertFalse(ok)


if __name__ == "__main__":
    unittest.main()
