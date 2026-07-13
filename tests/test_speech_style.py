import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from bot.db.engine import init_db
from bot.db.models import Group
from bot.services.speech_style import (
    SpeechStyleService,
    build_style_profile_context,
    get_style_state,
    set_style_target,
)


class _StubLLM(SimpleNamespace):
    pass


def _llm(profile_text: str = "语气懒散，爱用「捏」结尾。") -> SimpleNamespace:
    return SimpleNamespace(compress=AsyncMock(return_value=profile_text))


class StyleSettingsTests(unittest.TestCase):
    def test_set_style_target_writes_bucket(self) -> None:
        settings = set_style_target({}, user_id=555, user_name="老王")

        state = get_style_state(settings)
        self.assertEqual(state["target_user_id"], 555)
        self.assertEqual(state["target_user_name"], "老王")
        self.assertEqual(state["profile_text"], "")
        self.assertEqual(state["sample_count"], 0)

    def test_set_style_target_none_disables(self) -> None:
        settings = set_style_target({}, user_id=555, user_name="老王")
        settings = set_style_target(settings, user_id=0, user_name="")

        state = get_style_state(settings)
        self.assertEqual(state["target_user_id"], 0)

    def test_build_style_profile_context_renders_block(self) -> None:
        context = build_style_profile_context("语气懒散，爱用「捏」结尾。", target_name="老王")

        self.assertTrue(context.startswith("[SPEECH_STYLE_PROFILE]\n"))
        self.assertIn("语气懒散", context)
        self.assertIn("老王", context)

    def test_build_style_profile_context_empty_profile_returns_empty(self) -> None:
        self.assertEqual(build_style_profile_context("", target_name="x"), "")


class SpeechStyleServiceTests(unittest.IsolatedAsyncioTestCase):
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

    async def _make_group(self, group_id: int = -200, target_id: int = 555) -> None:
        async with self.session_factory() as session:
            group = Group(id=group_id, title="t")
            group.settings = set_style_target({}, user_id=target_id, user_name="老王")
            session.add(group)
            await session.commit()

    async def test_collect_ignores_non_target_user(self) -> None:
        await self._make_group()
        service = SpeechStyleService(_llm(), distill_every=50, max_samples=1000)

        async with self.session_factory() as session:
            collected = await service.collect_sample(
                session, group_id=-200, user_id=999, text="路人甲说话"
            )

        self.assertFalse(collected)

    async def test_collect_stores_target_sample(self) -> None:
        await self._make_group()
        service = SpeechStyleService(_llm(), distill_every=50, max_samples=1000)

        async with self.session_factory() as session:
            collected = await service.collect_sample(
                session, group_id=-200, user_id=555, text="今天摸鱼捏"
            )
            await session.commit()
            state = get_style_state((await session.get(Group, -200)).settings)

        self.assertTrue(collected)
        self.assertEqual(state["sample_count"], 1)

    async def test_distill_triggers_every_n_samples(self) -> None:
        await self._make_group()
        llm = _llm("新画像：爱用捏。")
        service = SpeechStyleService(llm, distill_every=3, max_samples=1000)

        async with self.session_factory() as session:
            for i in range(3):
                await service.collect_sample(
                    session, group_id=-200, user_id=555, text=f"消息{i}捏"
                )
            await session.commit()

            group = await session.get(Group, -200)
            state = get_style_state(group.settings)

        llm.compress.assert_awaited_once()
        corpus = llm.compress.await_args.args[1]
        self.assertIn("消息0捏", corpus)
        self.assertIn("消息2捏", corpus)
        self.assertEqual(state["profile_text"], "新画像：爱用捏。")
        self.assertEqual(state["distilled_at_count"], 3)

    async def test_distill_not_triggered_below_threshold(self) -> None:
        await self._make_group()
        llm = _llm()
        service = SpeechStyleService(llm, distill_every=3, max_samples=1000)

        async with self.session_factory() as session:
            for i in range(2):
                await service.collect_sample(
                    session, group_id=-200, user_id=555, text=f"消息{i}"
                )
            await session.commit()

        llm.compress.assert_not_awaited()

    async def test_samples_are_capped_at_max(self) -> None:
        await self._make_group()
        service = SpeechStyleService(_llm(), distill_every=1000, max_samples=5)

        async with self.session_factory() as session:
            for i in range(8):
                await service.collect_sample(
                    session, group_id=-200, user_id=555, text=f"消息{i}"
                )
            await session.commit()

            from sqlalchemy import select

            from bot.db.models import SpeechStyleSample

            rows = (
                await session.execute(
                    select(SpeechStyleSample.content)
                    .where(SpeechStyleSample.group_id == -200)
                    .order_by(SpeechStyleSample.id)
                )
            ).scalars().all()

        self.assertEqual(len(rows), 5)
        self.assertEqual(rows[0], "消息3")
        self.assertEqual(rows[-1], "消息7")

    async def test_sample_count_stops_growing_at_total_cap(self) -> None:
        await self._make_group()
        llm = _llm()
        service = SpeechStyleService(llm, distill_every=4, max_samples=5, total_cap=6)

        async with self.session_factory() as session:
            for i in range(10):
                await service.collect_sample(
                    session, group_id=-200, user_id=555, text=f"消息{i}"
                )
            await session.commit()

            state = get_style_state((await session.get(Group, -200)).settings)

        # collection stops at total_cap; only the distill at 4 fires
        self.assertEqual(state["sample_count"], 6)
        self.assertEqual(llm.compress.await_count, 1)

    async def test_distill_failure_keeps_old_profile(self) -> None:
        await self._make_group()
        llm = SimpleNamespace(compress=AsyncMock(return_value=""))
        service = SpeechStyleService(llm, distill_every=2, max_samples=100)

        async with self.session_factory() as session:
            group = await session.get(Group, -200)
            group.settings = {
                **group.settings,
                "speech_style": {
                    **get_style_state(group.settings),
                    "profile_text": "旧画像",
                },
            }
            await session.commit()

            for i in range(2):
                await service.collect_sample(
                    session, group_id=-200, user_id=555, text=f"m{i}"
                )
            await session.commit()

            state = get_style_state((await session.get(Group, -200)).settings)

        self.assertEqual(state["profile_text"], "旧画像")


class StylePromptInjectionTests(unittest.TestCase):
    def _llm_stub(self) -> SimpleNamespace:
        return SimpleNamespace(
            main=SimpleNamespace(model="main-model", fallbacks=[]),
            decision_config=SimpleNamespace(model="decision-model", fallbacks=[]),
            vision_config=SimpleNamespace(model="vision-model", fallbacks=[]),
            moderation_config=SimpleNamespace(model="moderation-model", fallbacks=[]),
            compress_config=SimpleNamespace(model="compress-model", fallbacks=[]),
            embed_config=SimpleNamespace(model="embed-model", fallbacks=[]),
        )

    def test_casual_prompt_includes_style_block(self) -> None:
        from bot.services.casual import CasualService

        service = CasualService(self._llm_stub(), settings=None)
        payload = service.build_prompt_payload(
            "你好",
            style_profile_context=build_style_profile_context("爱用捏。", target_name="老王"),
        )

        contents = [m["content"] for m in payload["messages"]]
        blocks = [c for c in contents if c.startswith("[SPEECH_STYLE_PROFILE]\n")]
        self.assertEqual(len(blocks), 1)
        self.assertIn("爱用捏", blocks[0])

    def test_casual_prompt_without_profile_has_no_block(self) -> None:
        from bot.services.casual import CasualService

        service = CasualService(self._llm_stub(), settings=None)
        payload = service.build_prompt_payload("你好")

        contents = [m["content"] for m in payload["messages"]]
        self.assertFalse(any(c.startswith("[SPEECH_STYLE_PROFILE]\n") for c in contents))

    def test_skill_prompt_includes_style_block(self) -> None:
        from bot.services.skills.service import SkillService

        service = SkillService(self._llm_stub(), settings=None)
        payload = service.build_answer_prompt_payload(
            "你好",
            style_profile_context=build_style_profile_context("爱用捏。", target_name="老王"),
        )

        contents = [m["content"] for m in payload["messages"]]
        blocks = [c for c in contents if c.startswith("[SPEECH_STYLE_PROFILE]\n")]
        self.assertEqual(len(blocks), 1)


class MimicCommandTests(unittest.IsolatedAsyncioTestCase):
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

    def _settings(self):
        from bot.config import Settings

        settings = Settings(_env_file=None)
        settings.super_admin_id = 1
        return settings

    def _message(self, text: str, *, reply_user=None) -> SimpleNamespace:
        reply = None
        if reply_user is not None:
            reply = SimpleNamespace(from_user=reply_user)
        return SimpleNamespace(
            chat=SimpleNamespace(id=-300, type="supergroup", title="t"),
            from_user=SimpleNamespace(id=1, username="root"),
            text=text,
            reply_to_message=reply,
        )

    async def test_mimic_sets_target_from_reply(self) -> None:
        from unittest.mock import patch

        from bot.handlers import admin as admin_handlers

        target = SimpleNamespace(id=555, is_bot=False, full_name="老王", username="laowang")
        message = self._message("/mimic", reply_user=target)

        async with self.session_factory() as session:
            with (
                patch("bot.handlers.admin.ensure_group_authorized", new=AsyncMock(return_value=True)),
                patch("bot.handlers.admin.ensure_group_admin_permission", new=AsyncMock(return_value=True)),
                patch("bot.handlers.admin._answer", new=AsyncMock()) as answer_mock,
            ):
                await admin_handlers.cmd_mimic(message, session=session, settings=self._settings())
            await session.commit()

            group = await session.get(Group, -300)
            state = get_style_state(group.settings)

        self.assertEqual(state["target_user_id"], 555)
        self.assertEqual(state["target_user_name"], "老王")
        self.assertIn("已开始学习", answer_mock.await_args.args[2])

    async def test_mimic_off_clears_state(self) -> None:
        from unittest.mock import patch

        from bot.handlers import admin as admin_handlers

        async with self.session_factory() as session:
            group = Group(id=-300, title="t")
            group.settings = set_style_target({}, user_id=555, user_name="老王")
            session.add(group)
            await session.commit()

            message = self._message("/mimic off")
            with (
                patch("bot.handlers.admin.ensure_group_authorized", new=AsyncMock(return_value=True)),
                patch("bot.handlers.admin.ensure_group_admin_permission", new=AsyncMock(return_value=True)),
                patch("bot.handlers.admin._answer", new=AsyncMock()),
            ):
                await admin_handlers.cmd_mimic(message, session=session, settings=self._settings())
            await session.commit()

            state = get_style_state((await session.get(Group, -300)).settings)

        self.assertEqual(state["target_user_id"], 0)
        self.assertEqual(state["profile_text"], "")


if __name__ == "__main__":
    unittest.main()
