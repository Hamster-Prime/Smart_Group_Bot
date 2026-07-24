import html
import re
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from bot.config import Settings
from bot.db.models import Admin, Base, GroupMember
from bot.services import call_admin
from bot.services.call_admin import (
    build_call_admin_text,
    call_admin_policy,
    call_admin_targets,
    handle_call_admin,
    is_call_admin_trigger,
)


def _settings(**overrides) -> Settings:
    settings = Settings(_env_file=None)
    settings.bot.auto_delete_seconds = 0
    settings.bot.auto_delete_categories = []
    settings.call_admin_enabled = True
    settings.call_admin_cooldown_seconds = 0
    for key, value in overrides.items():
        setattr(settings, key, value)
    return settings


class CallAdminTriggerTests(unittest.TestCase):
    def test_trigger_detection(self) -> None:
        self.assertTrue(is_call_admin_trigger("@admin"))
        self.assertTrue(is_call_admin_trigger("@Admin 有人发广告"))
        self.assertTrue(is_call_admin_trigger("@admins"))
        self.assertTrue(is_call_admin_trigger("求助 @admin"))
        self.assertFalse(is_call_admin_trigger("@administrator"))
        self.assertFalse(is_call_admin_trigger("test@admin.com"))
        self.assertFalse(is_call_admin_trigger("admin"))
        self.assertFalse(is_call_admin_trigger(""))

    def test_policy_inherits_global(self) -> None:
        settings = _settings()
        self.assertTrue(call_admin_policy(settings, None))
        self.assertTrue(call_admin_policy(settings, {}))
        self.assertFalse(call_admin_policy(settings, {"call_admin_enabled": False}))
        settings.call_admin_enabled = False
        self.assertFalse(call_admin_policy(settings, {}))
        self.assertTrue(call_admin_policy(settings, {"call_admin_enabled": True}))

    def test_targets_empty_means_all(self) -> None:
        self.assertEqual(call_admin_targets(None), set())
        self.assertEqual(call_admin_targets({}), set())
        self.assertEqual(
            call_admin_targets({"call_admin_targets": [7, "9", 0, "x"]}), {7, 9}
        )

    def test_notice_text_mentions_and_escaping(self) -> None:
        text = build_call_admin_text(
            ['<a href="tg://user?id=7">A</a>'],
            caller_id=5,
            caller_name="<u>",
            reason="有人<刷屏>",
            reported_text="买片加微信",
        )
        self.assertIn("<b>呼叫管理员 · 需要处理</b>", text)
        self.assertEqual(text.count("<blockquote>"), 2)
        self.assertIn("<blockquote expandable>", text)
        self.assertIn('<blockquote><a href="tg://user?id=7">A</a></blockquote>', text)
        self.assertIn('<blockquote><b>发起人</b>　<a href="tg://user?id=5">', text)
        self.assertNotIn("管理员通知批次", text)
        self.assertIn('tg://user?id=7', text)
        self.assertIn('tg://user?id=5', text)
        self.assertIn("&lt;u&gt;", text)
        self.assertIn("有人&lt;刷屏&gt;", text)
        self.assertIn("买片加微信", text)


class CallAdminSendTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        call_admin._last_call_at.clear()
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.session_factory = async_sessionmaker(self.engine, expire_on_commit=False)

    async def asyncTearDown(self) -> None:
        await self.engine.dispose()

    def _bot(self, admins: list[SimpleNamespace]) -> SimpleNamespace:
        return SimpleNamespace(
            me=AsyncMock(return_value=SimpleNamespace(id=999)),
            get_chat_administrators=AsyncMock(return_value=admins),
            send_message=AsyncMock(
                return_value=SimpleNamespace(
                    chat=SimpleNamespace(id=-100), message_id=1
                )
            ),
        )

    @staticmethod
    def _admin_member(
        user_id: int,
        *,
        username: str = "",
        full_name: str | None = None,
        is_bot: bool = False,
    ):
        return SimpleNamespace(
            user=SimpleNamespace(
                id=user_id,
                username=username,
                full_name=full_name if full_name is not None else f"user{user_id}",
                is_bot=is_bot,
            )
        )

    def _message(self, bot, *, reply=None) -> SimpleNamespace:
        return SimpleNamespace(
            chat=SimpleNamespace(id=-100, type="supergroup"),
            bot=bot,
            text="@admin 有广告",
            caption=None,
            reply_to_message=reply,
        )

    async def test_mentions_all_admins_excluding_bots_and_self(self) -> None:
        bot = self._bot([
            self._admin_member(7, username="alice"),
            self._admin_member(8),
            self._admin_member(999),  # the bot itself
            self._admin_member(12, is_bot=True),
        ])
        async with self.session_factory() as session:
            sent = await handle_call_admin(
                self._message(bot),
                session,
                _settings(),
                group_settings={},
                caller_id=5,
                caller_name="小明",
            )
        self.assertTrue(sent)
        text = bot.send_message.await_args.args[1]
        self.assertIn("tg://user?id=7", text)
        self.assertIn("tg://user?id=8", text)
        self.assertNotIn("tg://user?id=999", text)
        self.assertNotIn("tg://user?id=12", text)
        self.assertIn("@alice", text)

    async def test_target_selection_filters_mentions(self) -> None:
        bot = self._bot([self._admin_member(7), self._admin_member(8)])
        async with self.session_factory() as session:
            sent = await handle_call_admin(
                self._message(bot),
                session,
                _settings(),
                group_settings={"call_admin_targets": [8]},
                caller_id=5,
                caller_name="小明",
            )
        self.assertTrue(sent)
        text = bot.send_message.await_args.args[1]
        self.assertNotIn("tg://user?id=7", text)
        self.assertIn("tg://user?id=8", text)

    async def test_many_admins_are_sent_in_one_bounded_notice(self) -> None:
        bot = self._bot(
            [
                self._admin_member(user_id, full_name="😀" * 128)
                for user_id in range(1, 46)
            ]
        )
        async with self.session_factory() as session:
            sent = await handle_call_admin(
                self._message(bot),
                session,
                _settings(),
                group_settings={},
                caller_id=100,
                caller_name="小明",
            )
        self.assertTrue(sent)
        bot.send_message.assert_awaited_once()
        text = bot.send_message.await_args.args[1]
        for user_id in range(1, 46):
            self.assertEqual(text.count(f"tg://user?id={user_id}\""), 1)
        self.assertNotIn("管理员通知批次", text)
        visible = html.unescape(re.sub(r"<[^>]+>", "", text))
        self.assertLessEqual(len(visible.encode("utf-16-le")) // 2, 4096)

    async def test_disabled_group_sends_nothing(self) -> None:
        bot = self._bot([self._admin_member(7)])
        async with self.session_factory() as session:
            sent = await handle_call_admin(
                self._message(bot),
                session,
                _settings(),
                group_settings={"call_admin_enabled": False},
                caller_id=5,
                caller_name="小明",
            )
        self.assertFalse(sent)
        bot.send_message.assert_not_awaited()

    async def test_cooldown_suppresses_second_call(self) -> None:
        bot = self._bot([self._admin_member(7)])
        settings = _settings(call_admin_cooldown_seconds=300)
        async with self.session_factory() as session:
            first = await handle_call_admin(
                self._message(bot), session, settings,
                group_settings={}, caller_id=5, caller_name="a",
            )
            second = await handle_call_admin(
                self._message(bot), session, settings,
                group_settings={}, caller_id=6, caller_name="b",
            )
        self.assertTrue(first)
        self.assertFalse(second)
        self.assertEqual(bot.send_message.await_count, 1)

    async def test_telegram_failure_falls_back_to_admin_table(self) -> None:
        bot = self._bot([])
        bot.get_chat_administrators = AsyncMock(side_effect=RuntimeError("down"))
        async with self.session_factory() as session:
            session.add(Admin(group_id=-100, user_id=21, role="admin"))
            session.add(
                GroupMember(
                    group_id=-100, user_id=21, full_name="备用管理", username="backup"
                )
            )
            await session.commit()
            sent = await handle_call_admin(
                self._message(bot),
                session,
                _settings(),
                group_settings={},
                caller_id=5,
                caller_name="小明",
            )
        self.assertTrue(sent)
        text = bot.send_message.await_args.args[1]
        self.assertIn("tg://user?id=21", text)
        self.assertIn("@backup", text)

    async def test_database_transactions_are_released_before_telegram_calls(self) -> None:
        bot = self._bot([])
        bot.get_chat_administrators = AsyncMock(side_effect=RuntimeError("down"))
        async with self.session_factory() as session:
            session.add(Admin(group_id=-100, user_id=22, role="admin"))
            session.add(
                GroupMember(
                    group_id=-100,
                    user_id=22,
                    full_name="事务管理",
                    username="txadmin",
                )
            )
            await session.commit()
            # Reproduce the caller's already-open authorization snapshot.
            await session.execute(select(Admin.id).limit(1))

            async def me_after_release():
                self.assertFalse(session.in_transaction())
                return SimpleNamespace(id=999)

            async def send_after_fallback_release(*_args, **_kwargs):
                self.assertFalse(session.in_transaction())
                return SimpleNamespace(chat=SimpleNamespace(id=-100), message_id=2)

            bot.me = AsyncMock(side_effect=me_after_release)
            bot.send_message = AsyncMock(side_effect=send_after_fallback_release)
            sent = await handle_call_admin(
                self._message(bot),
                session,
                _settings(),
                group_settings={},
                caller_id=5,
                caller_name="小明",
            )

        self.assertTrue(sent)
        bot.send_message.assert_awaited_once()

    async def test_reply_context_included_and_anchored(self) -> None:
        bot = self._bot([self._admin_member(7)])
        reply = SimpleNamespace(message_id=42, text="卖片广告", caption=None)
        async with self.session_factory() as session:
            sent = await handle_call_admin(
                self._message(bot, reply=reply),
                session,
                _settings(),
                group_settings={},
                caller_id=5,
                caller_name="小明",
            )
        self.assertTrue(sent)
        self.assertIn("卖片广告", bot.send_message.await_args.args[1])
        self.assertEqual(
            bot.send_message.await_args.kwargs.get("reply_to_message_id"), 42
        )


if __name__ == "__main__":
    unittest.main()
