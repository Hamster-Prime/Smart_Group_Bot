import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from bot.handlers import group


def _settings(delay: float = 5.0) -> SimpleNamespace:
    return SimpleNamespace(bot=SimpleNamespace(inbound_debounce_seconds=delay))


def _item(
    text: str,
    *,
    message: object | None = None,
    explicit_mention: bool = False,
    mentioned: bool = False,
    is_reply: bool = False,
    reply_to_bot: bool = False,
    sender_is_owner: bool = False,
    sender_is_tg_admin: bool = False,
) -> group._PendingReplyItem:
    return group._PendingReplyItem(
        message=message,
        group_id=-10001,
        user_id=123,
        input_text=text,
        msg_type="text",
        sender_username="tester",
        sender_is_owner=sender_is_owner,
        sender_is_tg_admin=sender_is_tg_admin,
        user_tag="id:123",
        explicit_mention=explicit_mention,
        mentioned=mentioned,
        is_reply=is_reply,
        reply_to_bot=reply_to_bot,
        reply_to_other=False,
        mention_other=False,
    )


class PendingReplyDebounceTests(unittest.TestCase):
    def test_question_message_flushes_well_before_config_ceiling(self) -> None:
        due_at = group._next_pending_reply_flush_at(
            item=_item("ios 上最好用的是啥啊"),
            batch_size=1,
            settings=_settings(5.0),
            now=100.0,
        )

        self.assertAlmostEqual(due_at, 101.4, places=3)

    def test_direct_trigger_flushes_fast(self) -> None:
        due_at = group._next_pending_reply_flush_at(
            item=_item("感思你在吗", mentioned=True),
            batch_size=1,
            settings=_settings(5.0),
            now=100.0,
        )

        self.assertAlmostEqual(due_at, 100.5, places=3)

    def test_new_followup_message_does_not_push_flush_later(self) -> None:
        first_due_at = group._next_pending_reply_flush_at(
            item=_item("在吗"),
            batch_size=1,
            settings=_settings(5.0),
            now=100.0,
        )
        second_due_at = group._next_pending_reply_flush_at(
            item=_item("我想问个事"),
            batch_size=2,
            settings=_settings(5.0),
            now=100.4,
            current_flush_at=first_due_at,
        )
        third_due_at = group._next_pending_reply_flush_at(
            item=_item("就是播放器那个"),
            batch_size=3,
            settings=_settings(5.0),
            now=100.9,
            current_flush_at=second_due_at,
        )

        self.assertLessEqual(second_due_at, first_due_at)
        self.assertLessEqual(third_due_at, second_due_at)


class _PendingSession:
    async def __aenter__(self) -> "_PendingSession":
        return self

    async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
        return None

    async def get(self, model: object, key: int) -> SimpleNamespace:
        return SimpleNamespace(settings={})

    async def execute(self, statement: object) -> SimpleNamespace:
        return SimpleNamespace(scalar_one_or_none=lambda: None)

    async def rollback(self) -> None:
        return None


def _processing_settings() -> SimpleNamespace:
    return SimpleNamespace(
        bot=SimpleNamespace(
            inbound_debounce_seconds=1.0,
            main_model="",
            decision_model="",
            compress_model="",
            moderation_model="",
            vision_model="",
            embed_model="",
            max_context_tokens=0,
            decision_context_items=0,
            enable_typing=False,
            enable_streaming=False,
            stream_chunk_size=100,
            stream_edit_interval_sec=0.0,
        ),
        skill_sticker_file_ids="",
    )


class PendingReplyAdminRevalidationTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def _message() -> SimpleNamespace:
        return SimpleNamespace(
            message_id=99,
            text="记住群昵称是测试群",
            caption=None,
            from_user=SimpleNamespace(id=123, is_bot=False, username="tester", full_name="Tester"),
            sender_chat=None,
            reply_to_message=None,
            chat=SimpleNamespace(id=-10001, type="supergroup"),
        )

    async def test_demoted_admin_snapshot_is_not_used_by_skill_execution(self) -> None:
        message = self._message()
        item = _item(
            message.text,
            message=message,
            explicit_mention=True,
            mentioned=True,
            sender_is_tg_admin=True,
        )
        session = _PendingSession()
        fake_skill = SimpleNamespace(
            tts_service=SimpleNamespace(available=False),
            build_answer_prompt_payload=Mock(return_value={"messages": [], "tools": []}),
            answer_with_skill=AsyncMock(
                return_value=SimpleNamespace(
                    text="",
                    handled=True,
                    sticker_sent=False,
                    tts_sent=False,
                    sticker_file_id="",
                    tts_text="",
                )
            ),
        )
        history_ready = False

        async def get_history_for_llm(
            group_id: int,
            *,
            prompt_payload_builder: object,
        ) -> list[dict[str, str]]:
            nonlocal history_ready
            prompt_payload_builder([])
            history_ready = True
            return []

        async def lookup_after_history(message_arg: object) -> bool:
            self.assertTrue(history_ready, "admin lookup must follow history compaction/trim")
            self.assertIs(message_arg, message)
            return False

        memory = SimpleNamespace(
            session_factory=lambda: session,
            get_history=Mock(return_value=[]),
            get_history_for_llm=AsyncMock(side_effect=get_history_for_llm),
        )
        admin_lookup = AsyncMock(side_effect=lookup_after_history)

        with (
            patch("bot.handlers.group.memory_holder.get", return_value=memory),
            patch("bot.handlers.group.LLMService", return_value=object()),
            patch("bot.handlers.group.SkillService", return_value=fake_skill),
            patch("bot.handlers.group._is_user_admin_cached", new=admin_lookup),
            patch("bot.handlers.group._best_effort_commit", new=AsyncMock()),
        ):
            await group._process_pending_reply_batch([item], _processing_settings())

        admin_lookup.assert_awaited_once_with(message)
        self.assertFalse(
            fake_skill.build_answer_prompt_payload.call_args.kwargs["sender_is_tg_admin"]
        )
        self.assertFalse(fake_skill.answer_with_skill.await_args.kwargs["sender_is_tg_admin"])

    async def test_admin_revalidation_error_fails_closed(self) -> None:
        item = _item(
            "记住这条信息",
            message=self._message(),
            sender_is_tg_admin=True,
        )

        with patch(
            "bot.handlers.group._is_user_admin_cached",
            new=AsyncMock(side_effect=RuntimeError("telegram unavailable")),
        ):
            self.assertFalse(await group._revalidate_pending_sender_admin(item))


if __name__ == "__main__":
    unittest.main()
