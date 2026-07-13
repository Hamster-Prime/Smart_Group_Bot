import asyncio
import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from bot.db.engine import init_db
from bot.db.models import ModerationRule, UserWarning
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from bot.services.authz import authorize_group
from bot.services.join_screening import (
    add_global_ban,
    build_join_profile_text,
    get_global_ban,
    get_profile_screen_hash,
    is_globally_banned,
    is_join_screening_exempt,
    list_global_bans,
    mark_profile_screened,
    profile_signature,
    remove_global_ban,
    screen_member_profile,
)


class GlobalBanServiceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        fd, self._db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.engine, self.session_factory = await init_db(f"sqlite+aiosqlite:///{self._db_path}")

    async def asyncTearDown(self) -> None:
        await self.engine.dispose()
        for suffix in ("", "-wal", "-shm"):
            try:
                os.remove(f"{self._db_path}{suffix}")
            except OSError:
                pass

    async def test_add_global_ban_marks_user_banned(self) -> None:
        async with self.session_factory() as session:
            added = await add_global_ban(
                session, 1001, reason="广告机", source="manual", created_by=1
            )
            await session.commit()
            self.assertTrue(added)
            self.assertTrue(await is_globally_banned(session, 1001))
            row = await get_global_ban(session, 1001)
            self.assertEqual(row.reason, "广告机")
            self.assertEqual(row.source, "manual")

    async def test_unban_adds_join_screening_exemption(self) -> None:
        async with self.session_factory() as session:
            await add_global_ban(session, 1002, reason="x", source="join_screening", created_by=1)
            await session.commit()

            removed = await remove_global_ban(session, 1002, operator_id=9)
            await session.commit()

            self.assertTrue(removed)
            self.assertFalse(await is_globally_banned(session, 1002))
            self.assertTrue(await is_join_screening_exempt(session, 1002))

    async def test_reban_revokes_exemption(self) -> None:
        async with self.session_factory() as session:
            await add_global_ban(session, 1003, reason="a", created_by=1)
            await session.commit()
            await remove_global_ban(session, 1003, operator_id=1)
            await session.commit()

            await add_global_ban(session, 1003, reason="b", created_by=1)
            await session.commit()

            self.assertTrue(await is_globally_banned(session, 1003))
            self.assertFalse(await is_join_screening_exempt(session, 1003))

    async def test_unban_unknown_user_still_records_exemption(self) -> None:
        async with self.session_factory() as session:
            removed = await remove_global_ban(session, 1004, operator_id=1)
            await session.commit()

            self.assertFalse(removed)
            self.assertTrue(await is_join_screening_exempt(session, 1004))

    async def test_list_global_bans_returns_rows(self) -> None:
        async with self.session_factory() as session:
            await add_global_ban(session, 1, reason="r1", created_by=1)
            await add_global_ban(session, 2, reason="r2", created_by=1)
            await session.commit()

            rows = await list_global_bans(session)
            self.assertEqual({row.user_id for row in rows}, {1, 2})

    async def test_concurrent_add_global_ban_is_idempotent(self) -> None:
        # Use plain AsyncSession instances here so the database upsert itself,
        # rather than the application's SQLite write mutex, resolves the race.
        concurrent_factory = async_sessionmaker(
            self.engine,
            class_=AsyncSession,
            expire_on_commit=False,
            autoflush=False,
        )

        async def add(reason: str) -> bool:
            async with concurrent_factory() as session:
                created = await add_global_ban(
                    session,
                    1005,
                    reason=reason,
                    source="join_screening",
                    created_by=1,
                )
                await session.commit()
                return created

        created = await asyncio.gather(add("first"), add("second"))

        self.assertEqual(sorted(created), [False, True])
        async with self.session_factory() as session:
            row = await get_global_ban(session, 1005)
        self.assertIsNotNone(row)
        self.assertIn(row.reason, {"first", "second"})


class ProfileScreenStateTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        fd, self._db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.engine, self.session_factory = await init_db(f"sqlite+aiosqlite:///{self._db_path}")

    async def asyncTearDown(self) -> None:
        await self.engine.dispose()
        for suffix in ("", "-wal", "-shm"):
            try:
                os.remove(f"{self._db_path}{suffix}")
            except OSError:
                pass

    def test_profile_signature_changes_with_profile(self) -> None:
        a = profile_signature(full_name="张三", username="zhang")
        b = profile_signature(full_name="张三改名", username="zhang")
        c = profile_signature(full_name="张三", username="zhang")
        self.assertNotEqual(a, b)
        self.assertEqual(a, c)

    async def test_mark_and_get_profile_screen_hash(self) -> None:
        async with self.session_factory() as session:
            self.assertEqual(await get_profile_screen_hash(session, 2001), "")
            await mark_profile_screened(session, 2001, profile_hash="h1")
            await session.commit()
            self.assertEqual(await get_profile_screen_hash(session, 2001), "h1")
            await mark_profile_screened(session, 2001, profile_hash="h2")
            await session.commit()
            self.assertEqual(await get_profile_screen_hash(session, 2001), "h2")


class JoinProfileScreeningTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        fd, self._db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.engine, self.session_factory = await init_db(f"sqlite+aiosqlite:///{self._db_path}")

    async def asyncTearDown(self) -> None:
        await self.engine.dispose()
        for suffix in ("", "-wal", "-shm"):
            try:
                os.remove(f"{self._db_path}{suffix}")
            except OSError:
                pass

    def test_build_join_profile_text_combines_name_username_bio(self) -> None:
        text = build_join_profile_text(
            full_name="出售 VPN 联系我",
            username="seller123",
            bio="加我飞机 @spam_channel 进群秒发",
        )

        self.assertIn("出售 VPN 联系我", text)
        self.assertIn("@seller123", text)
        self.assertIn("加我飞机 @spam_channel 进群秒发", text)

    async def test_screen_member_profile_flags_violating_profile(self) -> None:
        async with self.session_factory() as session:
            session.add(
                ModerationRule(
                    group_id=-100,
                    rule_type="llm",
                    pattern="禁止广告与拉人",
                    action="warn",
                    enabled=True,
                )
            )
            await session.commit()

            moderation = SimpleNamespace(
                check_rules=AsyncMock(return_value=(True, "昵称含广告", SimpleNamespace(id=1)))
            )
            violated, reason = await screen_member_profile(
                session,
                moderation,
                group_id=-100,
                user_id=555,
                profile_text="出售VPN | @seller | 广告simp",
            )

            self.assertTrue(violated)
            self.assertEqual(reason, "昵称含广告")
            moderation.check_rules.assert_awaited_once()
            called_text = moderation.check_rules.await_args.args[2]
            self.assertIn("成员资料审核", called_text)
            self.assertIn("出售VPN", called_text)

    async def test_screen_member_profile_skips_exempt_user(self) -> None:
        async with self.session_factory() as session:
            await remove_global_ban(session, 556, operator_id=1)  # records exemption
            await session.commit()

            moderation = SimpleNamespace(check_rules=AsyncMock())
            violated, reason = await screen_member_profile(
                session,
                moderation,
                group_id=-100,
                user_id=556,
                profile_text="whatever",
            )

            self.assertFalse(violated)
            self.assertEqual(reason, "")
            moderation.check_rules.assert_not_awaited()

    async def test_screen_member_profile_skips_empty_profile(self) -> None:
        async with self.session_factory() as session:
            moderation = SimpleNamespace(check_rules=AsyncMock())
            violated, _ = await screen_member_profile(
                session,
                moderation,
                group_id=-100,
                user_id=557,
                profile_text="   ",
            )

            self.assertFalse(violated)
            moderation.check_rules.assert_not_awaited()


def _join_event(*, user_id: int = 900, full_name: str = "新人", username: str = "newbie") -> SimpleNamespace:
    user = SimpleNamespace(
        id=user_id,
        is_bot=False,
        full_name=full_name,
        username=username,
    )
    return SimpleNamespace(
        chat=SimpleNamespace(id=-100, type="supergroup", ban=AsyncMock()),
        new_chat_member=SimpleNamespace(user=user),
        bot=SimpleNamespace(
            get_chat=AsyncMock(return_value=SimpleNamespace(bio="正经简介")),
            send_message=AsyncMock(),
        ),
    )


class MembershipHandlerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        fd, self._db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.engine, self.session_factory = await init_db(f"sqlite+aiosqlite:///{self._db_path}")
        async with self.session_factory() as session:
            await authorize_group(session, -100, 1)
            await session.commit()

    async def asyncTearDown(self) -> None:
        await self.engine.dispose()
        for suffix in ("", "-wal", "-shm"):
            try:
                os.remove(f"{self._db_path}{suffix}")
            except OSError:
                pass

    def _settings(self):
        from bot.config import Settings

        settings = Settings(_env_file=None)
        settings.moderation.enabled = True
        return settings

    async def test_violating_join_gets_banned_and_recorded(self) -> None:
        from unittest.mock import patch

        from bot.handlers import membership

        event = _join_event(user_id=901)
        async with self.session_factory() as session:
            fake_moderation = SimpleNamespace(
                check_rules=AsyncMock(return_value=(True, "昵称含广告", None))
            )
            with (
                patch("bot.handlers.membership.ModerationService", return_value=fake_moderation),
                patch("bot.handlers.membership._build_llm", return_value=object()),
            ):
                await membership.on_member_join(event, session=session, settings=self._settings())
            await session.commit()

            event.chat.ban.assert_awaited_once_with(901)
            event.bot.send_message.assert_awaited_once()
            self.assertTrue(await is_globally_banned(session, 901))
            row = await get_global_ban(session, 901)
            self.assertEqual(row.source, "join_screening")

    async def test_clean_join_is_not_banned_and_marks_profile_checked(self) -> None:
        from unittest.mock import patch

        from bot.handlers import membership

        event = _join_event(user_id=902)
        async with self.session_factory() as session:
            fake_moderation = SimpleNamespace(
                check_rules=AsyncMock(return_value=(False, "", None))
            )
            with (
                patch("bot.handlers.membership.ModerationService", return_value=fake_moderation),
                patch("bot.handlers.membership._build_llm", return_value=object()),
            ):
                await membership.on_member_join(event, session=session, settings=self._settings())
            await session.commit()

            event.chat.ban.assert_not_awaited()
            self.assertFalse(await is_globally_banned(session, 902))
            self.assertNotEqual(await get_profile_screen_hash(session, 902), "")

    async def test_globally_banned_user_is_rebanned_on_rejoin_without_screening(self) -> None:
        from unittest.mock import patch

        from bot.handlers import membership

        event = _join_event(user_id=903)
        async with self.session_factory() as session:
            await add_global_ban(session, 903, reason="之前违规", created_by=1)
            await session.commit()

            fake_moderation = SimpleNamespace(check_rules=AsyncMock())
            with (
                patch("bot.handlers.membership.ModerationService", return_value=fake_moderation),
                patch("bot.handlers.membership._build_llm", return_value=object()),
            ):
                await membership.on_member_join(event, session=session, settings=self._settings())

            event.chat.ban.assert_awaited_once_with(903)
            fake_moderation.check_rules.assert_not_awaited()
            event.bot.get_chat.assert_not_awaited()

    async def test_unbanned_user_rejoin_skips_profile_screening(self) -> None:
        from unittest.mock import patch

        from bot.handlers import membership

        event = _join_event(user_id=904)
        async with self.session_factory() as session:
            await add_global_ban(session, 904, reason="旧账", created_by=1)
            await remove_global_ban(session, 904, operator_id=1)
            await session.commit()

            fake_moderation = SimpleNamespace(check_rules=AsyncMock())
            with (
                patch("bot.handlers.membership.ModerationService", return_value=fake_moderation),
                patch("bot.handlers.membership._build_llm", return_value=object()),
            ):
                await membership.on_member_join(event, session=session, settings=self._settings())

            event.chat.ban.assert_not_awaited()
            fake_moderation.check_rules.assert_not_awaited()

    async def test_unauthorized_group_join_is_ignored(self) -> None:
        from unittest.mock import patch

        from bot.handlers import membership

        event = _join_event(user_id=905)
        event.chat = SimpleNamespace(id=-999, type="supergroup", ban=AsyncMock())
        async with self.session_factory() as session:
            fake_moderation = SimpleNamespace(check_rules=AsyncMock())
            with (
                patch("bot.handlers.membership.ModerationService", return_value=fake_moderation),
                patch("bot.handlers.membership._build_llm", return_value=object()),
            ):
                await membership.on_member_join(event, session=session, settings=self._settings())

            event.chat.ban.assert_not_awaited()
            fake_moderation.check_rules.assert_not_awaited()


class BanCommandTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        fd, self._db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.engine, self.session_factory = await init_db(f"sqlite+aiosqlite:///{self._db_path}")
        async with self.session_factory() as session:
            await authorize_group(session, -100, 1)
            await session.commit()

    async def asyncTearDown(self) -> None:
        await self.engine.dispose()
        for suffix in ("", "-wal", "-shm"):
            try:
                os.remove(f"{self._db_path}{suffix}")
            except OSError:
                pass

    def _settings(self):
        from bot.config import Settings

        settings = Settings(_env_file=None)
        settings.super_admin_id = 1
        return settings

    def _message(self, text: str) -> SimpleNamespace:
        return SimpleNamespace(
            chat=SimpleNamespace(id=-100, type="supergroup", ban=AsyncMock()),
            from_user=SimpleNamespace(id=1, username="root"),
            text=text,
            reply_to_message=None,
            bot=SimpleNamespace(
                ban_chat_member=AsyncMock(),
                unban_chat_member=AsyncMock(),
            ),
        )

    async def test_ban_command_bans_in_configured_group(self) -> None:
        from unittest.mock import patch

        from bot.handlers import admin as admin_handlers

        message = self._message("/ban 777 广告机")
        async with self.session_factory() as session:
            with patch("bot.handlers.admin._answer", new=AsyncMock()) as answer_mock:
                await admin_handlers.cmd_ban(message, session=session, settings=self._settings())
            await session.commit()

            self.assertTrue(await is_globally_banned(session, 777))
            message.bot.ban_chat_member.assert_awaited_once_with(-100, 777)
            self.assertIn("封禁结果", answer_mock.await_args.args[2])

    async def test_unban_command_unbans_and_records_exemption(self) -> None:
        from unittest.mock import patch

        from bot.handlers import admin as admin_handlers

        async with self.session_factory() as session:
            await add_global_ban(session, 778, reason="x", created_by=1)
            session.add(UserWarning(group_id=-100, user_id=778, count=3, is_banned=True))
            await session.commit()

            message = self._message("/unban 778")
            with patch("bot.handlers.admin._answer", new=AsyncMock()) as answer_mock:
                await admin_handlers.cmd_unban(message, session=session, settings=self._settings())
            await session.commit()

            self.assertFalse(await is_globally_banned(session, 778))
            self.assertTrue(await is_join_screening_exempt(session, 778))
            warning_result = await session.execute(
                select(UserWarning).where(
                    UserWarning.group_id == -100,
                    UserWarning.user_id == 778,
                )
            )
            self.assertIsNone(warning_result.scalar_one_or_none())
            message.bot.unban_chat_member.assert_awaited_once_with(-100, 778, only_if_banned=True)
            self.assertIn("解封结果", answer_mock.await_args.args[2])
            self.assertIn("累计 3 次", answer_mock.await_args.args[2])

    async def test_clearwarnings_command_clears_count_by_user_id(self) -> None:
        from unittest.mock import patch

        from bot.handlers import admin as admin_handlers

        async with self.session_factory() as session:
            session.add(UserWarning(group_id=-100, user_id=779, count=2, is_banned=False))
            await session.commit()

            message = self._message("/clearwarnings 779")
            with patch("bot.handlers.admin._answer", new=AsyncMock()) as answer_mock:
                await admin_handlers.cmd_clearwarnings(
                    message,
                    session=session,
                    settings=self._settings(),
                )
            await session.commit()

            warning_result = await session.execute(
                select(UserWarning).where(
                    UserWarning.group_id == -100,
                    UserWarning.user_id == 779,
                )
            )
            self.assertIsNone(warning_result.scalar_one_or_none())
            self.assertIn("原为 2 次", answer_mock.await_args.args[2])

    async def test_clearwarnings_command_accepts_reply_target(self) -> None:
        from unittest.mock import patch

        from bot.handlers import admin as admin_handlers

        async with self.session_factory() as session:
            session.add(UserWarning(group_id=-100, user_id=780, count=1, is_banned=True))
            await session.commit()

            message = self._message("/clearwarnings")
            message.reply_to_message = SimpleNamespace(from_user=SimpleNamespace(id=780))
            with patch("bot.handlers.admin._answer", new=AsyncMock()) as answer_mock:
                await admin_handlers.cmd_clearwarnings(
                    message,
                    session=session,
                    settings=self._settings(),
                )
            await session.commit()

            warning_result = await session.execute(
                select(UserWarning).where(
                    UserWarning.group_id == -100,
                    UserWarning.user_id == 780,
                )
            )
            self.assertIsNone(warning_result.scalar_one_or_none())
            self.assertIn("不会解封", answer_mock.await_args.args[2])

    async def test_ban_rejects_non_super_admin(self) -> None:
        from unittest.mock import patch

        from bot.handlers import admin as admin_handlers

        message = self._message("/ban 777")
        message.from_user = SimpleNamespace(id=42, username="mallory")
        message.answer = AsyncMock(return_value=None)

        async with self.session_factory() as session:
            with patch("bot.handlers.admin._answer", new=AsyncMock()):
                await admin_handlers.cmd_ban(message, session=session, settings=self._settings())

            self.assertFalse(await is_globally_banned(session, 777))
            message.bot.ban_chat_member.assert_not_awaited()


class GlobalBanMiddlewareTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        fd, self._db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.engine, self.session_factory = await init_db(f"sqlite+aiosqlite:///{self._db_path}")
        async with self.session_factory() as session:
            await authorize_group(session, -100, 1)
            await session.commit()

    async def asyncTearDown(self) -> None:
        await self.engine.dispose()
        for suffix in ("", "-wal", "-shm"):
            try:
                os.remove(f"{self._db_path}{suffix}")
            except OSError:
                pass

    def _settings(self):
        from bot.config import Settings

        settings = Settings(_env_file=None)
        settings.super_admin_id = 1
        return settings

    def _event(self, *, user_id: int, text: str = "/help") -> SimpleNamespace:
        return SimpleNamespace(
            chat=SimpleNamespace(id=-100, type="supergroup", ban=AsyncMock()),
            from_user=SimpleNamespace(id=user_id),
            sender_chat=None,
            text=text,
            delete=AsyncMock(),
        )

    async def test_banned_user_command_is_blocked_before_handler(self) -> None:
        from bot.middlewares.global_ban import GlobalBanEnforcementMiddleware

        middleware = GlobalBanEnforcementMiddleware(self.session_factory)
        handler = AsyncMock(return_value="handled")

        async with self.session_factory() as session:
            await add_global_ban(session, 666, reason="spam", created_by=1)
            await session.commit()

        event = self._event(user_id=666, text="/help")
        result = await middleware(handler, event, {"settings": self._settings()})

        handler.assert_not_awaited()
        self.assertIsNone(result)
        event.delete.assert_awaited_once()
        event.chat.ban.assert_awaited_once_with(666)

    async def test_normal_user_message_passes_through(self) -> None:
        from bot.middlewares.global_ban import GlobalBanEnforcementMiddleware

        middleware = GlobalBanEnforcementMiddleware(self.session_factory)
        handler = AsyncMock(return_value="handled")

        event = self._event(user_id=42)
        result = await middleware(handler, event, {"settings": self._settings()})

        handler.assert_awaited_once()
        self.assertEqual(result, "handled")
        event.delete.assert_not_awaited()

    async def test_super_admin_never_blocked_even_if_listed(self) -> None:
        from bot.middlewares.global_ban import GlobalBanEnforcementMiddleware

        middleware = GlobalBanEnforcementMiddleware(self.session_factory)
        handler = AsyncMock(return_value="handled")

        async with self.session_factory() as session:
            await add_global_ban(session, 1, reason="oops", created_by=1)
            await session.commit()

        event = self._event(user_id=1)
        result = await middleware(handler, event, {"settings": self._settings()})

        handler.assert_awaited_once()
        self.assertEqual(result, "handled")

    async def test_private_chat_is_ignored(self) -> None:
        from bot.middlewares.global_ban import GlobalBanEnforcementMiddleware

        middleware = GlobalBanEnforcementMiddleware(self.session_factory)
        handler = AsyncMock(return_value="handled")

        async with self.session_factory() as session:
            await add_global_ban(session, 666, reason="spam", created_by=1)
            await session.commit()

        event = self._event(user_id=666)
        event.chat = SimpleNamespace(id=666, type="private", ban=AsyncMock())
        result = await middleware(handler, event, {"settings": self._settings()})

        handler.assert_awaited_once()
        self.assertEqual(result, "handled")

    async def test_unauthorized_group_is_not_enforced(self) -> None:
        from bot.middlewares.global_ban import GlobalBanEnforcementMiddleware

        middleware = GlobalBanEnforcementMiddleware(self.session_factory)
        handler = AsyncMock(return_value="handled")

        async with self.session_factory() as session:
            await add_global_ban(session, 666, reason="spam", created_by=1)
            await session.commit()

        event = self._event(user_id=666)
        # group -999 is not in the authorized_groups table
        event.chat = SimpleNamespace(id=-999, type="supergroup", ban=AsyncMock())
        result = await middleware(handler, event, {"settings": self._settings()})

        handler.assert_awaited_once()
        self.assertEqual(result, "handled")
        event.chat.ban.assert_not_awaited()


class SuperAdminJoinProtectionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        fd, self._db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.engine, self.session_factory = await init_db(f"sqlite+aiosqlite:///{self._db_path}")
        async with self.session_factory() as session:
            await authorize_group(session, -100, 1)
            await session.commit()

    async def asyncTearDown(self) -> None:
        await self.engine.dispose()
        for suffix in ("", "-wal", "-shm"):
            try:
                os.remove(f"{self._db_path}{suffix}")
            except OSError:
                pass

    async def test_super_admin_join_skips_screening_and_ban(self) -> None:
        from unittest.mock import patch

        from bot.config import Settings
        from bot.handlers import membership

        settings = Settings(_env_file=None)
        settings.super_admin_id = 42
        settings.moderation.enabled = True

        event = _join_event(user_id=42, full_name="主人")
        async with self.session_factory() as session:
            # even if somehow listed, super admin must not be touched on join
            await add_global_ban(session, 42, reason="oops", created_by=1)
            await session.commit()

            fake_moderation = SimpleNamespace(check_rules=AsyncMock())
            with (
                patch("bot.handlers.membership.ModerationService", return_value=fake_moderation),
                patch("bot.handlers.membership._build_llm", return_value=object()),
            ):
                await membership.on_member_join(event, session=session, settings=settings)

            event.chat.ban.assert_not_awaited()
            fake_moderation.check_rules.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
