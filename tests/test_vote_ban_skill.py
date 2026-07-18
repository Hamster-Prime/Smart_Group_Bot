import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from sqlalchemy import select

from bot.db.engine import init_db
from bot.db.models import Group, VoteBanSession
from bot.services.authz import authorize_group
from bot.services.skills.base import SkillContext
from bot.services.skills.vote_ban import VoteBanSkill


def _settings(**overrides) -> SimpleNamespace:
    values = {
        "super_admin_id": 1,
        "vote_ban_enabled": True,
        "vote_ban_threshold": 3,
        "vote_ban_duration_seconds": 600,
        "vote_ban_trigger_limit": 1,
        "vote_ban_trigger_window_seconds": 3600,
        "bot": SimpleNamespace(
            auto_delete_categories=[],
            auto_delete_seconds=0,
            auto_delete_category_seconds={},
            auto_delete_category_mode={},
        ),
    }
    values.update(overrides)
    return SimpleNamespace(**values)


class VoteBanSkillTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        fd, self._db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.engine, self.session_factory = await init_db(
            f"sqlite+aiosqlite:///{self._db_path}"
        )
        async with self.session_factory() as session:
            session.add(Group(id=-100, title="test", settings={}))
            await authorize_group(session, -100, 1)
            await session.commit()
        self.settings = _settings()
        self.skill = VoteBanSkill(self.settings)

    async def asyncTearDown(self) -> None:
        await self.engine.dispose()
        for suffix in ("", "-wal", "-shm"):
            try:
                os.remove(f"{self._db_path}{suffix}")
            except OSError:
                pass

    @staticmethod
    def _message(target_id: int = 555) -> SimpleNamespace:
        bot = SimpleNamespace(
            get_chat_member=AsyncMock(return_value=SimpleNamespace(status="member")),
            send_message=AsyncMock(return_value=SimpleNamespace(message_id=888)),
        )
        return SimpleNamespace(
            chat=SimpleNamespace(id=-100, type="supergroup"),
            from_user=SimpleNamespace(
                id=10,
                full_name="发起人",
                username="starter",
            ),
            sender_chat=None,
            text="请发起民主投票封他",
            bot=bot,
            reply_to_message=SimpleNamespace(
                message_id=42,
                from_user=SimpleNamespace(
                    id=target_id,
                    full_name=f"目标{target_id}",
                    username=f"user{target_id}",
                    is_bot=False,
                ),
                sender_chat=None,
                text="持续骚扰内容",
                caption=None,
            ),
        )

    def _context(self, session, message, *, text: str = "请发起民主投票封他") -> SkillContext:
        return SkillContext(
            session=session,
            session_factory=None,
            message=message,
            bot=message.bot,
            chat_id=-100,
            sender_user_id=10,
            sender_username="starter",
            current_user_text=text,
            is_direct_request=True,
        )

    async def test_explicit_reply_request_starts_poll_and_suppresses_followup(self) -> None:
        message = self._message()
        async with self.session_factory() as session:
            context = self._context(session, message)
            result = await self.skill.run({"reason": "持续骚扰"}, context)
        self.assertTrue(result.ok)
        self.assertTrue(context.handled)
        self.assertTrue(context.suppress_followup_text)
        message.bot.send_message.assert_awaited_once()
        async with self.session_factory() as session:
            record = await session.scalar(select(VoteBanSession))
            self.assertEqual(record.source, "skill")
            self.assertEqual(record.reason, "持续骚扰")
            self.assertEqual(record.evidence, "持续骚扰内容")

    async def test_quota_exhaustion_returns_structured_error_for_main_model(self) -> None:
        first = self._message(555)
        async with self.session_factory() as session:
            first_context = self._context(session, first)
            self.assertTrue((await self.skill.run({}, first_context)).ok)

        second = self._message(556)
        async with self.session_factory() as session:
            second_context = self._context(session, second)
            result = await self.skill.run({}, second_context)
        self.assertFalse(result.ok)
        self.assertEqual(result.error, "starter_quota_exhausted")
        self.assertEqual(result.payload["quota"]["limit"], 1)
        self.assertEqual(result.payload["quota"]["remaining"], 0)
        self.assertIn("额度已用完", result.summary)
        self.assertFalse(second_context.handled)
        second.bot.send_message.assert_not_awaited()

    async def test_non_explicit_chat_does_not_start_high_impact_action(self) -> None:
        message = self._message()
        message.text = "这人怎么这么烦"
        async with self.session_factory() as session:
            context = self._context(session, message, text="这人怎么这么烦")
            result = await self.skill.run({}, context)
        self.assertFalse(result.ok)
        self.assertEqual(result.error, "not_explicit_request")
        message.bot.send_message.assert_not_awaited()

    async def test_merged_earlier_intent_cannot_authorize_latest_reply_target(self) -> None:
        message = self._message()
        message.text = "这只是后续补充"
        async with self.session_factory() as session:
            context = self._context(
                session,
                message,
                text="请发起民主投票封他\n这只是后续补充",
            )
            result = await self.skill.run({}, context)
        self.assertFalse(result.ok)
        self.assertEqual(result.error, "not_explicit_request")
        message.bot.send_message.assert_not_awaited()

    async def test_autonomous_non_direct_turn_cannot_start_vote(self) -> None:
        message = self._message()
        async with self.session_factory() as session:
            context = self._context(session, message)
            context.is_direct_request = False
            result = await self.skill.run({}, context)
        self.assertFalse(result.ok)
        self.assertEqual(result.error, "not_direct_request")
        message.bot.send_message.assert_not_awaited()

    async def test_missing_reply_target_is_reported(self) -> None:
        message = self._message()
        message.reply_to_message = None
        async with self.session_factory() as session:
            context = self._context(session, message)
            result = await self.skill.run({}, context)
        self.assertFalse(result.ok)
        self.assertEqual(result.error, "missing_reply_target")


if __name__ == "__main__":
    unittest.main()
