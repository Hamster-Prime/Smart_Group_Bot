import asyncio
import time
import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from bot.config import BotConfig
from bot.handlers import group
from bot.services.doubao_tts import TTSDeliveryResult
from bot.services.skills.base import SkillAnswerResult
from bot.services.update_completion import UpdateCompletionReceipt
from bot.utils.telegram import ReplyMessageOverlay, TelegramDeliveryResult


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

    def test_owner_uses_same_reply_delay_as_other_members(self) -> None:
        due_at = group._next_pending_reply_flush_at(
            item=_item("今天就先这样", sender_is_owner=True),
            batch_size=1,
            settings=_settings(5.0),
            now=100.0,
        )
        ordinary_due_at = group._next_pending_reply_flush_at(
            item=_item("今天就先这样"),
            batch_size=1,
            settings=_settings(5.0),
            now=100.0,
        )

        self.assertAlmostEqual(due_at, ordinary_due_at, places=3)
        self.assertFalse(group._is_strong_pending_reply_signal(
            _item("今天就先这样", sender_is_owner=True)
        ))

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
    def __init__(self) -> None:
        self.closed = False

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

    async def close(self) -> None:
        self.closed = True


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
        fake_progress = SimpleNamespace(
            visible=False,
            start=AsyncMock(),
            report=AsyncMock(),
            composing=AsyncMock(),
            handoff=AsyncMock(return_value=None),
            finish=AsyncMock(),
            fail=AsyncMock(),
            dismiss=AsyncMock(),
            close=AsyncMock(),
        )
        history_ready = False

        async def get_history_for_llm(
            group_id: int,
            *,
            prompt_payload_builder: object,
            recall_query: str = "",
            recall_exclude_message_keys: list[str] | None = None,
        ) -> list[dict[str, str]]:
            nonlocal history_ready
            self.assertEqual(recall_query, message.text)
            self.assertEqual(
                recall_exclude_message_keys,
                [f"{message.chat.id}:{message.message_id}"],
            )
            prompt_payload_builder([])
            history_ready = True
            return []

        async def lookup_after_history(message_arg: object) -> bool:
            self.assertTrue(history_ready, "admin lookup must follow history compaction/trim")
            self.assertTrue(session.closed, "DB connection must be released before LLM/tool work")
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
            patch(
                "bot.handlers.group.ReplyProgressTracker",
                return_value=fake_progress,
            ) as progress_factory,
            patch("bot.handlers.group._is_user_admin_cached", new=admin_lookup),
            patch("bot.handlers.group._best_effort_commit", new=AsyncMock()),
        ):
            processed = await group._process_pending_reply_batch(
                [item],
                _processing_settings(),
            )

        self.assertFalse(processed)
        admin_lookup.assert_awaited_once_with(message)
        self.assertFalse(
            fake_skill.build_answer_prompt_payload.call_args.kwargs["sender_is_tg_admin"]
        )
        self.assertFalse(fake_skill.answer_with_skill.await_args.kwargs["sender_is_tg_admin"])
        self.assertIs(
            fake_skill.answer_with_skill.await_args.kwargs["session_factory"],
            memory.session_factory,
        )
        self.assertTrue(
            fake_skill.answer_with_skill.await_args.kwargs["is_direct_request"]
        )
        self.assertIs(
            fake_skill.answer_with_skill.await_args.kwargs["progress_callback"],
            fake_progress.report,
        )
        progress_factory.assert_called_once_with(
            message,
            enabled=True,
            reveal_after=3.0,
            edit_interval=0.8,
            auto_delete_seconds=30,
            disable_link_preview=True,
        )
        fake_progress.start.assert_awaited_once_with()
        fake_progress.dismiss.assert_awaited_once_with()
        fake_progress.close.assert_awaited_once_with()
        self.assertNotIn(
            "is_direct_request",
            fake_skill.build_answer_prompt_payload.call_args.kwargs,
        )

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


class PendingReplyEmbeddedDeliveryTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def _message(text: str) -> SimpleNamespace:
        return SimpleNamespace(
            message_id=99,
            text=text,
            caption=None,
            from_user=SimpleNamespace(
                id=123,
                is_bot=False,
                username="tester",
                full_name="Tester",
            ),
            sender_chat=None,
            reply_to_message=None,
            chat=SimpleNamespace(id=-10001, type="supergroup"),
        )

    def _assert_archived_assistant_reply(
        self,
        memory: SimpleNamespace,
        item: group._PendingReplyItem,
        text: str,
    ) -> None:
        memory.add_message.assert_awaited_once()
        args = memory.add_message.await_args.args
        kwargs = memory.add_message.await_args.kwargs
        self.assertEqual(args, (item.group_id, "assistant", text))
        self.assertEqual(kwargs["message_type"], "assistant_reply")
        self.assertTrue(kwargs["defer_persistence"])
        self.assertEqual(kwargs["completions"], ())
        archive = kwargs["archive_metadata"]
        self.assertEqual(archive["direction"], "outbound")
        self.assertEqual(archive["sender_kind"], "bot")
        self.assertEqual(archive["reply_to_message_id"], item.message.message_id)
        self.assertIn(item.message.message_id, archive["extra_metadata"]["trigger_message_ids"])

    async def _run_guarded(
        self,
        skill_result: SkillAnswerResult | Exception,
        *,
        memory_error: Exception | None = None,
        progress_handoff: ReplyMessageOverlay | None = None,
        send_result: object = True,
    ) -> tuple[
        group._PendingReplyOutcome,
        group._PendingReplyItem,
        SimpleNamespace,
        AsyncMock,
        Mock,
    ]:
        message = self._message("@bot 执行动作")
        item = _item(
            message.text,
            message=message,
            explicit_mention=True,
            mentioned=True,
        )
        session = _PendingSession()
        memory = SimpleNamespace(
            session_factory=lambda: session,
            get_history=Mock(return_value=[]),
            get_history_for_llm=AsyncMock(return_value=[]),
            add_message=AsyncMock(side_effect=memory_error),
        )
        answer_with_skill = (
            AsyncMock(side_effect=skill_result)
            if isinstance(skill_result, Exception)
            else AsyncMock(return_value=skill_result)
        )
        fake_skill = SimpleNamespace(
            tts_service=SimpleNamespace(available=False),
            build_answer_prompt_payload=Mock(return_value={"messages": [], "tools": []}),
            answer_with_skill=answer_with_skill,
        )
        fake_progress = SimpleNamespace(
            visible=True,
            start=AsyncMock(),
            report=AsyncMock(),
            composing=AsyncMock(),
            handoff=AsyncMock(return_value=progress_handoff),
            finish=AsyncMock(return_value=True),
            fail=AsyncMock(return_value=True),
            dismiss=AsyncMock(),
            close=AsyncMock(),
        )
        notify_failure = AsyncMock(return_value=True)
        send_text_reply = AsyncMock(return_value=send_result)
        schedule_compaction = Mock()
        self._last_fake_progress = fake_progress
        self._last_send_text_reply = send_text_reply

        with (
            patch("bot.handlers.group.memory_holder.get", return_value=memory),
            patch("bot.handlers.group.LLMService", return_value=object()),
            patch("bot.handlers.group.SkillService", return_value=fake_skill),
            patch(
                "bot.handlers.group.ReplyProgressTracker",
                return_value=fake_progress,
            ),
            patch(
                "bot.handlers.group._is_user_admin_cached",
                new=AsyncMock(return_value=False),
            ),
            patch("bot.handlers.group._best_effort_commit", new=AsyncMock()),
            patch(
                "bot.handlers.group._schedule_memory_compaction",
                new=schedule_compaction,
            ),
            patch(
                "bot.handlers.group._notify_pending_reply_failure",
                new=notify_failure,
            ),
            patch(
                "bot.handlers.group.send_reply",
                new=send_text_reply,
            ),
        ):
            outcome = await group._process_pending_reply_batch_guarded(
                (item.group_id, item.user_id),
                [item],
                _processing_settings(),
            )

        return outcome, item, memory, notify_failure, schedule_compaction

    async def test_text_reply_archives_real_telegram_id_and_reply_relation(self) -> None:
        sent_at = datetime(2026, 7, 30, 8, 15, tzinfo=timezone.utc)
        telegram_message = SimpleNamespace(
            message_id=9001,
            date=sent_at,
            chat=SimpleNamespace(id=-10001),
        )

        outcome, item, memory, notify_failure, schedule_compaction = (
            await self._run_guarded(
                SkillAnswerResult(handled=True, text="带真实 ID 的回复"),
                send_result=TelegramDeliveryResult(
                    sent=True,
                    messages=(telegram_message,),
                ),
            )
        )

        self.assertTrue(outcome.succeeded)
        notify_failure.assert_not_awaited()
        schedule_compaction.assert_called_once_with(memory, item.group_id)
        kwargs = memory.add_message.await_args.kwargs
        self.assertEqual(kwargs["message_id"], "9001")
        self.assertEqual(kwargs["created_at"], sent_at)
        archive = kwargs["archive_metadata"]
        self.assertEqual(archive["telegram_message_id"], 9001)
        self.assertEqual(archive["reply_to_message_id"], item.message.message_id)
        self.assertTrue(archive["is_reply"])
        self.assertEqual(
            archive["extra_metadata"]["telegram_message_ids"],
            [9001],
        )
        self.assertNotIn(
            "telegram_message_id_unavailable",
            archive["extra_metadata"],
        )
        self.assertTrue(
            self._last_send_text_reply.await_args.kwargs["return_result"]
        )

    async def test_tts_skill_reply_archives_voice_message_id(self) -> None:
        outcome, item, memory, notify_failure, schedule_compaction = (
            await self._run_guarded(
                SkillAnswerResult(
                    handled=True,
                    tts_sent=True,
                    tts_text="语音回复",
                    tts_telegram_message_ids=(9010,),
                )
            )
        )

        self.assertTrue(outcome.succeeded)
        notify_failure.assert_not_awaited()
        schedule_compaction.assert_called_once_with(memory, item.group_id)
        kwargs = memory.add_message.await_args.kwargs
        self.assertEqual(kwargs["message_id"], "9010")
        archive = kwargs["archive_metadata"]
        self.assertEqual(archive["telegram_message_id"], 9010)
        self.assertEqual(archive["reply_to_message_id"], item.message.message_id)
        self.assertEqual(
            archive["extra_metadata"]["telegram_message_ids"],
            [9010],
        )

    async def test_embedded_music_and_vote_deliveries_do_not_emit_failure(self) -> None:
        for embedded_text in ("这首《稻香》给你。", "民主投票已经发起。"):
            with self.subTest(embedded_text=embedded_text):
                outcome, item, memory, notify_failure, schedule_compaction = (
                    await self._run_guarded(
                        SkillAnswerResult(
                            handled=True,
                            embedded_reply_sent=True,
                            embedded_reply_text=embedded_text,
                        )
                    )
                )

                self.assertTrue(outcome.succeeded)
                notify_failure.assert_not_awaited()
                self._assert_archived_assistant_reply(
                    memory,
                    item,
                    embedded_text,
                )
                schedule_compaction.assert_called_once_with(memory, item.group_id)

    async def test_handled_without_delivery_still_emits_failure(self) -> None:
        outcome, item, memory, notify_failure, schedule_compaction = (
            await self._run_guarded(SkillAnswerResult(handled=True))
        )

        self.assertTrue(outcome.succeeded)
        notify_failure.assert_awaited_once_with([item], timed_out=False)
        memory.add_message.assert_not_awaited()
        schedule_compaction.assert_not_called()

    async def test_post_delivery_memory_error_does_not_emit_failure(self) -> None:
        outcome, _item, memory, notify_failure, schedule_compaction = (
            await self._run_guarded(
                SkillAnswerResult(
                    handled=True,
                    embedded_reply_sent=True,
                    embedded_reply_text="这首《稻香》给你。",
                ),
                memory_error=RuntimeError("memory queue unavailable"),
            )
        )

        self.assertTrue(outcome.succeeded)
        memory.add_message.assert_awaited_once()
        notify_failure.assert_not_awaited()
        schedule_compaction.assert_not_called()

    async def test_visible_progress_does_not_suppress_pre_delivery_failure(self) -> None:
        outcome, item, memory, notify_failure, schedule_compaction = (
            await self._run_guarded(RuntimeError("provider failed"))
        )

        self.assertTrue(outcome.succeeded)
        notify_failure.assert_awaited_once_with([item], timed_out=False)
        memory.add_message.assert_not_awaited()
        schedule_compaction.assert_not_called()

    async def test_text_reply_adopts_progress_message_without_later_dismiss(self) -> None:
        overlay = ReplyMessageOverlay(
            message=SimpleNamespace(
                message_id=77,
                chat=SimpleNamespace(id=-10001),
            ),
            status_html=(
                "<blockquote><s>已理解问题</s>\n"
                "<b>当前</b>　已整理并发送回答</blockquote>"
            ),
            reply_to_message_id=99,
            sent_as_reply=True,
            outcome="attached",
        )

        outcome, item, memory, notify_failure, schedule_compaction = (
            await self._run_guarded(
                SkillAnswerResult(handled=True, text="最终正文"),
                progress_handoff=overlay,
            )
        )

        self.assertTrue(outcome.succeeded)
        notify_failure.assert_not_awaited()
        self._last_fake_progress.handoff.assert_awaited_once_with(
            "已整理并发送回答"
        )
        self.assertIs(
            self._last_send_text_reply.await_args.kwargs["overlay"],
            overlay,
        )
        self._last_fake_progress.dismiss.assert_not_awaited()
        self._last_fake_progress.finish.assert_not_awaited()
        self._assert_archived_assistant_reply(memory, item, "最终正文")
        schedule_compaction.assert_called_once_with(memory, item.group_id)

    async def test_mandatory_text_after_skill_tts_does_not_reorder_progress(
        self,
    ) -> None:
        outcome, item, memory, notify_failure, schedule_compaction = (
            await self._run_guarded(
                SkillAnswerResult(
                    handled=True,
                    text="必须显示的文字说明",
                    must_deliver_text=True,
                    tts_sent=True,
                    tts_text="已发送的语音",
                ),
            )
        )

        self.assertTrue(outcome.succeeded)
        notify_failure.assert_not_awaited()
        self._last_fake_progress.handoff.assert_not_awaited()
        self.assertIsNone(
            self._last_send_text_reply.await_args.kwargs["overlay"]
        )
        self._last_fake_progress.dismiss.assert_awaited_once_with()
        self._assert_archived_assistant_reply(
            memory,
            item,
            "必须显示的文字说明",
        )
        schedule_compaction.assert_called_once_with(memory, item.group_id)

    async def test_cancellation_keeps_visible_manual_check_warning(self) -> None:
        message = self._message("@bot 发一首歌")
        item = _item(
            message.text,
            message=message,
            explicit_mention=True,
            mentioned=True,
        )
        session = _PendingSession()
        memory = SimpleNamespace(
            session_factory=lambda: session,
            get_history=Mock(return_value=[]),
            get_history_for_llm=AsyncMock(return_value=[]),
        )
        fake_skill = SimpleNamespace(
            tts_service=SimpleNamespace(available=False),
            build_answer_prompt_payload=Mock(return_value={"messages": [], "tools": []}),
            answer_with_skill=AsyncMock(side_effect=asyncio.CancelledError()),
        )
        fake_progress = SimpleNamespace(
            visible=True,
            start=AsyncMock(),
            report=AsyncMock(),
            composing=AsyncMock(),
            handoff=AsyncMock(return_value=None),
            finish=AsyncMock(return_value=True),
            fail=AsyncMock(return_value=True),
            dismiss=AsyncMock(),
            close=AsyncMock(),
        )
        receipt = group._PendingReplyDeliveryReceipt()

        with (
            patch("bot.handlers.group.memory_holder.get", return_value=memory),
            patch("bot.handlers.group.LLMService", return_value=object()),
            patch("bot.handlers.group.SkillService", return_value=fake_skill),
            patch(
                "bot.handlers.group.ReplyProgressTracker",
                return_value=fake_progress,
            ),
            patch(
                "bot.handlers.group._is_user_admin_cached",
                new=AsyncMock(return_value=False),
            ),
        ):
            with self.assertRaises(asyncio.CancelledError):
                await group._process_pending_reply_batch(
                    [item],
                    _processing_settings(),
                    delivery_receipt=receipt,
                )

        self.assertTrue(receipt.delivered)
        fake_progress.fail.assert_awaited_once()
        warning = fake_progress.fail.await_args.args[0]
        self.assertIn("先检查群内状态", warning)
        self.assertIn("确认未执行后再重试", warning)
        fake_progress.dismiss.assert_not_awaited()
        fake_progress.close.assert_awaited_once_with()


class PendingReplyWorkerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        async with group._PENDING_REPLY_LOCK:
            for state in group._PENDING_REPLY_BATCHES.values():
                if state.task and not state.task.done():
                    state.task.cancel()
            group._PENDING_REPLY_BATCHES.clear()
        group._PENDING_REPLY_ORPHAN_STARTED.clear()

    async def asyncTearDown(self) -> None:
        async with group._PENDING_REPLY_LOCK:
            tasks = [
                state.task
                for state in group._PENDING_REPLY_BATCHES.values()
                if state.task and not state.task.done()
            ]
            group._PENDING_REPLY_BATCHES.clear()
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        orphan_tasks = [
            task for task in group._PENDING_REPLY_ORPHAN_TASKS if not task.done()
        ]
        for task in orphan_tasks:
            task.cancel()
        if orphan_tasks:
            await asyncio.gather(*orphan_tasks, return_exceptions=True)
        group._PENDING_REPLY_ORPHAN_TASKS.clear()
        group._PENDING_REPLY_ORPHAN_STARTED.clear()

    async def test_same_sender_batches_never_overlap(self) -> None:
        first_started = asyncio.Event()
        release_first = asyncio.Event()
        calls: list[list[str]] = []
        active = 0
        max_active = 0

        async def process(
            key: tuple[int, int],
            items: list[group._PendingReplyItem],
            settings: object,
            **_kwargs: object,
        ) -> group._PendingReplyOutcome:
            nonlocal active, max_active
            del key, settings
            active += 1
            max_active = max(max_active, active)
            calls.append([item.input_text for item in items])
            try:
                if len(calls) == 1:
                    first_started.set()
                    await release_first.wait()
            finally:
                active -= 1
            return group._PendingReplyOutcome(succeeded=True)

        settings = _settings(0.0)
        with patch(
            "bot.handlers.group._process_pending_reply_batch_guarded",
            new=process,
        ):
            await group._enqueue_pending_reply(_item("first"), settings)
            await asyncio.wait_for(first_started.wait(), timeout=1.0)
            await group._enqueue_pending_reply(_item("second"), settings)
            await asyncio.sleep(0.02)
            self.assertEqual(calls, [["first"]])
            release_first.set()

            async def queue_empty() -> None:
                while True:
                    async with group._PENDING_REPLY_LOCK:
                        if not group._PENDING_REPLY_BATCHES:
                            return
                    await asyncio.sleep(0)

            await asyncio.wait_for(queue_empty(), timeout=1.0)

        self.assertEqual(calls, [["first"], ["second"]])
        self.assertEqual(max_active, 1)

    async def test_webhook_receipt_finishes_only_after_batch_terminal_outcome(self) -> None:
        started = asyncio.Event()
        release = asyncio.Event()
        receipt = UpdateCompletionReceipt()
        receipt.defer()
        item = _item("direct", mentioned=True)
        item.update_completion = receipt

        async def process(*_args: object, **_kwargs: object) -> group._PendingReplyOutcome:
            started.set()
            await release.wait()
            return group._PendingReplyOutcome(succeeded=True)

        with patch(
            "bot.handlers.group._process_pending_reply_batch_guarded",
            new=process,
        ):
            await group._enqueue_pending_reply(item, _settings(0.0))
            await asyncio.wait_for(started.wait(), timeout=1.0)
            waiter = asyncio.create_task(receipt.wait())
            await asyncio.sleep(0)
            self.assertFalse(waiter.done())
            release.set()
            self.assertTrue(await asyncio.wait_for(waiter, timeout=1.0))

    async def test_failed_batch_marks_webhook_receipt_failed(self) -> None:
        receipt = UpdateCompletionReceipt()
        receipt.defer()
        item = _item("direct", mentioned=True)
        item.update_completion = receipt

        async def process(*_args: object, **_kwargs: object) -> group._PendingReplyOutcome:
            return group._PendingReplyOutcome(succeeded=False)

        with patch(
            "bot.handlers.group._process_pending_reply_batch_guarded",
            new=process,
        ):
            await group._enqueue_pending_reply(item, _settings(0.0))
            self.assertFalse(await asyncio.wait_for(receipt.wait(), timeout=1.0))

    async def test_global_sender_backpressure_rejects_new_key(self) -> None:
        existing = _item("existing")
        incoming = _item("incoming")
        incoming.group_id = -20002
        incoming.user_id = 456
        async with group._PENDING_REPLY_LOCK:
            group._PENDING_REPLY_BATCHES[(existing.group_id, existing.user_id)] = (
                group._PendingReplyBatch(items=[existing])
            )

        with (
            patch("bot.handlers.group._PENDING_REPLY_MAX_SENDERS", 1),
            self.assertRaises(group._PendingReplyQueueFull),
        ):
            await group._enqueue_pending_reply(incoming, _settings(5.0))

    async def test_per_sender_backpressure_rejects_unbounded_followups(self) -> None:
        existing = _item("existing")
        key = (existing.group_id, existing.user_id)
        async with group._PENDING_REPLY_LOCK:
            group._PENDING_REPLY_BATCHES[key] = group._PendingReplyBatch(
                items=[existing]
            )

        with (
            patch("bot.handlers.group._PENDING_REPLY_MAX_ITEMS_PER_SENDER", 1),
            self.assertRaises(group._PendingReplyQueueFull),
        ):
            await group._enqueue_pending_reply(_item("overflow"), _settings(5.0))

    async def test_timed_out_orphan_blocks_next_batch_for_same_sender(self) -> None:
        release_orphan = asyncio.Event()
        first_returned = asyncio.Event()
        second_started = asyncio.Event()
        calls = 0

        async def orphan_work() -> None:
            await release_orphan.wait()

        async def process(*_args: object, **_kwargs: object):
            nonlocal calls
            calls += 1
            if calls == 1:
                orphan = asyncio.create_task(orphan_work())
                first_returned.set()
                return group._PendingReplyOutcome(succeeded=True, orphan=orphan)
            second_started.set()
            return group._PendingReplyOutcome(succeeded=True)

        with patch(
            "bot.handlers.group._process_pending_reply_batch_guarded",
            new=process,
        ):
            await group._enqueue_pending_reply(_item("first"), _settings(0.0))
            await asyncio.wait_for(first_returned.wait(), timeout=1.0)
            await group._enqueue_pending_reply(_item("second"), _settings(0.0))
            await asyncio.sleep(0.02)
            self.assertFalse(second_started.is_set())
            release_orphan.set()
            await asyncio.wait_for(second_started.wait(), timeout=1.0)

    async def test_shutdown_flush_waits_for_active_worker(self) -> None:
        started = asyncio.Event()
        release = asyncio.Event()

        async def process(*args: object, **kwargs: object) -> group._PendingReplyOutcome:
            del args, kwargs
            started.set()
            await release.wait()
            return group._PendingReplyOutcome(succeeded=True)

        with patch(
            "bot.handlers.group._process_pending_reply_batch_guarded",
            new=process,
        ):
            await group._enqueue_pending_reply(_item("first"), _settings(0.0))
            await asyncio.wait_for(started.wait(), timeout=1.0)
            flush_task = asyncio.create_task(group.flush_pending_inbound_batches())
            await asyncio.sleep(0.02)
            self.assertFalse(flush_task.done())
            release.set()
            await asyncio.wait_for(flush_task, timeout=1.0)

    async def test_cancelling_active_worker_does_not_race_with_failure_notice(self) -> None:
        started = asyncio.Event()
        notify = AsyncMock()

        async def process(*args: object, **kwargs: object) -> None:
            del args, kwargs
            started.set()
            await asyncio.sleep(60)

        with (
            patch(
                "bot.handlers.group._process_pending_reply_batch_guarded",
                new=process,
            ),
            patch(
                "bot.handlers.group._notify_pending_reply_failure",
                new=notify,
            ),
        ):
            await group._enqueue_pending_reply(
                _item("direct", mentioned=True),
                _settings(0.0),
            )
            await asyncio.wait_for(started.wait(), timeout=1.0)
            async with group._PENDING_REPLY_LOCK:
                task = next(iter(group._PENDING_REPLY_BATCHES.values())).task
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

        notify.assert_not_awaited()

    async def test_late_delivery_from_timed_out_child_suppresses_timeout_notice(self) -> None:
        cancelled = asyncio.Event()
        release_delivery = asyncio.Event()
        notify = AsyncMock(return_value=True)

        async def deliver_after_cancel(
            items: object,
            settings: object,
            *,
            delivery_receipt: group._PendingReplyDeliveryReceipt,
        ) -> bool:
            del items, settings
            try:
                await asyncio.Future()
            except asyncio.CancelledError:
                cancelled.set()
                await release_delivery.wait()
                delivery_receipt.confirm()
                return True

        with (
            patch(
                "bot.handlers.group._process_pending_reply_batch",
                new=deliver_after_cancel,
            ),
            patch(
                "bot.handlers.group._pending_reply_timeout_seconds",
                return_value=0.02,
            ),
            patch(
                "bot.handlers.group._notify_pending_reply_failure",
                new=notify,
            ),
        ):
            await group._enqueue_pending_reply(
                _item("direct", mentioned=True),
                _settings(0.0),
            )
            await asyncio.wait_for(cancelled.wait(), timeout=1.0)
            await asyncio.sleep(0.02)
            notify.assert_not_awaited()
            release_delivery.set()

            async def queue_empty() -> None:
                while True:
                    async with group._PENDING_REPLY_LOCK:
                        if not group._PENDING_REPLY_BATCHES:
                            return
                    await asyncio.sleep(0)

            await asyncio.wait_for(queue_empty(), timeout=1.0)

        notify.assert_not_awaited()

    async def test_timeout_notice_waits_until_child_cannot_deliver(self) -> None:
        cancelled = asyncio.Event()
        release = asyncio.Event()
        notified = asyncio.Event()

        async def record_notification(*_args: object, **_kwargs: object) -> bool:
            notified.set()
            return True

        notify = AsyncMock(side_effect=record_notification)

        async def finish_without_delivery(
            items: object,
            settings: object,
            *,
            delivery_receipt: group._PendingReplyDeliveryReceipt,
        ) -> bool:
            del items, settings, delivery_receipt
            try:
                await asyncio.Future()
            except asyncio.CancelledError:
                cancelled.set()
                await release.wait()
                return False

        with (
            patch(
                "bot.handlers.group._process_pending_reply_batch",
                new=finish_without_delivery,
            ),
            patch(
                "bot.handlers.group._pending_reply_timeout_seconds",
                return_value=0.02,
            ),
            patch(
                "bot.handlers.group._notify_pending_reply_failure",
                new=notify,
            ),
        ):
            await group._enqueue_pending_reply(
                _item("direct", mentioned=True),
                _settings(0.0),
            )
            await asyncio.wait_for(cancelled.wait(), timeout=1.0)
            notify.assert_not_awaited()
            release.set()
            await asyncio.wait_for(notified.wait(), timeout=1.0)

        notify.assert_awaited_once()
        self.assertTrue(notify.await_args.kwargs["timed_out"])

    async def test_hard_deadline_does_not_wait_for_cancel_ack(self) -> None:
        release = asyncio.Event()

        async def cancellation_resistant() -> None:
            try:
                await asyncio.sleep(60)
            except asyncio.CancelledError:
                await release.wait()

        loop = asyncio.get_running_loop()
        started = loop.time()
        with self.assertRaises(asyncio.TimeoutError):
            await group._await_hard_deadline(
                cancellation_resistant(),
                timeout_seconds=0.02,
            )
        self.assertLess(loop.time() - started, 0.2)
        release.set()
        await asyncio.sleep(0)

    async def test_deadline_after_confirmed_delivery_suppresses_timeout_notice(self) -> None:
        release = asyncio.Event()
        notify_failure = AsyncMock(return_value=True)

        async def delivered_then_stuck(
            items: object,
            settings: object,
            *,
            delivery_receipt: group._PendingReplyDeliveryReceipt,
        ) -> bool:
            del items, settings
            delivery_receipt.confirm()
            try:
                await asyncio.Future()
            except asyncio.CancelledError:
                await release.wait()
            return True

        with (
            patch(
                "bot.handlers.group._process_pending_reply_batch",
                new=delivered_then_stuck,
            ),
            patch(
                "bot.handlers.group._pending_reply_timeout_seconds",
                return_value=0.02,
            ),
            patch(
                "bot.handlers.group._notify_pending_reply_failure",
                new=notify_failure,
            ),
        ):
            outcome = await group._process_pending_reply_batch_guarded(
                (-10001, 123),
                [_item("@bot 发一首歌", mentioned=True)],
                _processing_settings(),
            )

        self.assertTrue(outcome.succeeded)
        self.assertIsNotNone(outcome.orphan)
        notify_failure.assert_not_awaited()
        release.set()
        await asyncio.wait_for(outcome.orphan, timeout=1.0)

    async def test_exception_after_confirmed_delivery_suppresses_failure_notice(self) -> None:
        notify_failure = AsyncMock(return_value=True)

        async def delivered_then_failed(
            items: object,
            settings: object,
            *,
            delivery_receipt: group._PendingReplyDeliveryReceipt,
        ) -> bool:
            del items, settings
            delivery_receipt.confirm()
            raise RuntimeError("session exit failed after delivery")

        with (
            patch(
                "bot.handlers.group._process_pending_reply_batch",
                new=delivered_then_failed,
            ),
            patch(
                "bot.handlers.group._notify_pending_reply_failure",
                new=notify_failure,
            ),
        ):
            outcome = await group._process_pending_reply_batch_guarded(
                (-10001, 123),
                [_item("@bot 发一首歌", mentioned=True)],
                _processing_settings(),
            )

        self.assertTrue(outcome.succeeded)
        notify_failure.assert_not_awaited()

    async def test_ambiguous_external_attempt_consumes_batch_without_failure_notice(
        self,
    ) -> None:
        notify_failure = AsyncMock(return_value=False)

        async def ambiguous_then_failed(
            items: object,
            settings: object,
            *,
            delivery_receipt: group._PendingReplyDeliveryReceipt,
        ) -> bool:
            del items, settings
            delivery_receipt.mark_ambiguous()
            return False

        with (
            patch(
                "bot.handlers.group._process_pending_reply_batch",
                new=ambiguous_then_failed,
            ),
            patch(
                "bot.handlers.group._notify_pending_reply_failure",
                new=notify_failure,
            ),
        ):
            outcome = await group._process_pending_reply_batch_guarded(
                (-10001, 123),
                [_item("@bot 执行动作", mentioned=True)],
                _processing_settings(),
            )

        self.assertTrue(outcome.succeeded)
        notify_failure.assert_not_awaited()

    async def test_deadline_after_ambiguous_attempt_consumes_batch(self) -> None:
        notify_failure = AsyncMock(return_value=False)

        async def ambiguous_then_stuck(
            items: object,
            settings: object,
            *,
            delivery_receipt: group._PendingReplyDeliveryReceipt,
        ) -> bool:
            del items, settings
            delivery_receipt.mark_ambiguous()
            await asyncio.Future()
            return False

        with (
            patch(
                "bot.handlers.group._process_pending_reply_batch",
                new=ambiguous_then_stuck,
            ),
            patch(
                "bot.handlers.group._pending_reply_timeout_seconds",
                return_value=0.02,
            ),
            patch(
                "bot.handlers.group._notify_pending_reply_failure",
                new=notify_failure,
            ),
        ):
            outcome = await group._process_pending_reply_batch_guarded(
                (-10001, 123),
                [_item("@bot 执行动作", mentioned=True)],
                _processing_settings(),
            )

        self.assertTrue(outcome.succeeded)
        notify_failure.assert_not_awaited()

    async def test_outer_cancellation_tracks_child_until_shutdown_drain(self) -> None:
        started = asyncio.Event()
        first_cancel_seen = asyncio.Event()

        async def cancellation_resistant_once() -> None:
            started.set()
            try:
                await asyncio.Future()
            except asyncio.CancelledError:
                first_cancel_seen.set()
                # The outer deadline owner has already returned. Stay alive
                # until the common shutdown drain issues its own cancellation.
                await asyncio.Future()

        outer = asyncio.create_task(
            group._await_hard_deadline(
                cancellation_resistant_once(),
                timeout_seconds=60.0,
            )
        )
        await asyncio.wait_for(started.wait(), timeout=1.0)
        outer.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await outer
        await asyncio.wait_for(first_cancel_seen.wait(), timeout=1.0)
        self.assertEqual(len(group._PENDING_REPLY_ORPHAN_TASKS), 1)

        await asyncio.wait_for(group.flush_pending_inbound_batches(), timeout=1.0)
        await asyncio.sleep(0)
        self.assertFalse(group._PENDING_REPLY_ORPHAN_TASKS)

    async def test_pending_reply_health_marks_exhausted_or_stale_orphans_fatal(self) -> None:
        tasks = [asyncio.create_task(asyncio.sleep(60)) for _ in range(4)]
        try:
            started = time.monotonic() - 121.0
            group._PENDING_REPLY_ORPHAN_TASKS.update(tasks)
            group._PENDING_REPLY_ORPHAN_STARTED.update(
                {task: started for task in tasks}
            )
            group._PENDING_REPLY_BATCHES[(1, 2)] = group._PendingReplyBatch(
                items=[_item("queued")],
                processing=True,
            )

            snapshot = group.pending_reply_resource_health_snapshot()

            self.assertTrue(snapshot["fatal"])
            self.assertEqual(snapshot["capacity"], 4)
            self.assertEqual(snapshot["orphan_count"], 4)
            self.assertGreaterEqual(snapshot["oldest_orphan_seconds"], 120.0)
            self.assertEqual(snapshot["batch_count"], 1)
            self.assertEqual(snapshot["processing_batches"], 1)
            self.assertEqual(snapshot["queued_items"], 1)
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    async def test_failure_notifies_direct_item_even_if_later_item_is_indirect(self) -> None:
        direct_message = SimpleNamespace(message_id=10)
        later_message = SimpleNamespace(message_id=11)

        for timed_out, outcome in ((False, "失败"), (True, "超时")):
            with self.subTest(timed_out=timed_out):
                send = AsyncMock(return_value=True)
                with patch("bot.handlers.group.send_reply", new=send):
                    await group._notify_pending_reply_failure(
                        [
                            _item("direct", message=direct_message, mentioned=True),
                            _item("follow up", message=later_message),
                        ],
                        timed_out=timed_out,
                    )

                self.assertIs(send.await_args.args[0], direct_message)
                self.assertEqual(send.await_args.kwargs["reply_to_message_id"], 10)
                self.assertIn(outcome, send.await_args.args[1])
                self.assertIn("先检查群内状态", send.await_args.args[1])
                self.assertIn("确认未执行后再重试", send.await_args.args[1])


class MemoryCompactionSchedulingTests(unittest.IsolatedAsyncioTestCase):
    async def asyncTearDown(self) -> None:
        tasks = [task for task in group._MEMORY_COMPACT_TASKS.values() if not task.done()]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        group._MEMORY_COMPACT_TASKS.clear()
        group._MEMORY_COMPACT_RERUN.clear()

    async def test_compaction_is_single_flight_per_group_with_one_rerun(self) -> None:
        first_started = asyncio.Event()
        release_first = asyncio.Event()
        calls = 0

        async def compact_if_needed(group_id: int) -> None:
            nonlocal calls
            self.assertEqual(group_id, -10001)
            calls += 1
            if calls == 1:
                first_started.set()
                await release_first.wait()

        memory = SimpleNamespace(compact_if_needed=compact_if_needed)
        group._schedule_memory_compaction(memory, -10001)
        await asyncio.wait_for(first_started.wait(), timeout=1.0)
        first_task = group._MEMORY_COMPACT_TASKS[-10001]

        group._schedule_memory_compaction(memory, -10001)
        self.assertIs(group._MEMORY_COMPACT_TASKS[-10001], first_task)
        release_first.set()
        await asyncio.wait_for(first_task, timeout=1.0)

        self.assertEqual(calls, 2)
        self.assertNotIn(-10001, group._MEMORY_COMPACT_TASKS)


class ReplyDeliveryFallbackTests(unittest.IsolatedAsyncioTestCase):
    async def test_only_first_text_plan_receives_progress_overlay(self) -> None:
        overlay = ReplyMessageOverlay(
            message=SimpleNamespace(
                message_id=77,
                chat=SimpleNamespace(id=-10001),
            ),
            status_html=(
                "<blockquote><s>已理解问题</s>\n"
                "<b>当前</b>　已整理并发送回答</blockquote>"
            ),
            reply_to_message_id=99,
            sent_as_reply=True,
        )
        send = AsyncMock(return_value=True)

        with patch("bot.handlers.group.send_reply", new=send):
            delivered, tts_sent, stored = await group._deliver_reply_plans(
                message=SimpleNamespace(),
                delivery_plans=[
                    group._ReplyDeliveryPlan("第一条", "reply", 99),
                    group._ReplyDeliveryPlan("第二条", "message", None),
                ],
                settings=_processing_settings(),
                tts_mode="off",
                tts_service=SimpleNamespace(available=False),
                user_id=123,
                group_id=-10001,
                tts_already_sent=False,
                progress_overlay=overlay,
            )

        self.assertTrue(delivered)
        self.assertFalse(tts_sent)
        self.assertEqual(stored, ["第一条", "第二条"])
        self.assertIs(send.await_args_list[0].kwargs["overlay"], overlay)
        self.assertIsNone(send.await_args_list[1].kwargs["overlay"])

    async def test_legacy_tts_sender_without_delivery_callback_remains_compatible(
        self,
    ) -> None:
        class LegacyTTS:
            available = True

            async def send_message_tts(
                self,
                message: object,
                text: str,
                *,
                delivery_mode: str,
                reply_to_message_id: int | None,
                auto_delete_seconds: int,
                uid: str,
            ) -> bool:
                del (
                    message,
                    text,
                    delivery_mode,
                    reply_to_message_id,
                    auto_delete_seconds,
                    uid,
                )
                return True

        receipt = Mock()
        with (
            patch("bot.handlers.group.is_tts_always_enabled", return_value=True),
            patch("bot.handlers.group.configured_auto_delete_seconds", return_value=0),
        ):
            sent, tts_sent, stored = await group._deliver_reply_plans(
                message=SimpleNamespace(),
                delivery_plans=[group._ReplyDeliveryPlan("语音内容", "reply", 1)],
                settings=_processing_settings(),
                tts_mode="always",
                tts_service=LegacyTTS(),
                user_id=123,
                group_id=-10001,
                tts_already_sent=False,
                on_delivery=receipt,
            )

        self.assertTrue(sent)
        self.assertTrue(tts_sent)
        self.assertEqual(stored, ["语音内容"])
        receipt.assert_called_once_with()

    async def test_partial_text_delivery_confirms_receipt_without_full_memory(self) -> None:
        receipt = Mock()

        async def send_part_then_fail(
            _message: object,
            _text: str,
            **kwargs: object,
        ) -> bool:
            callback = kwargs["on_delivery"]
            self.assertTrue(callable(callback))
            callback()
            return False

        with patch("bot.handlers.group.send_reply", new=send_part_then_fail):
            sent, tts_sent, stored = await group._deliver_reply_plans(
                message=SimpleNamespace(),
                delivery_plans=[group._ReplyDeliveryPlan("很长的回复", "message", None)],
                settings=_processing_settings(),
                tts_mode="off",
                tts_service=SimpleNamespace(available=False),
                user_id=123,
                group_id=-10001,
                tts_already_sent=False,
                on_delivery=receipt,
            )

        self.assertTrue(sent)
        self.assertFalse(tts_sent)
        self.assertEqual(stored, [])
        receipt.assert_called_once_with()

    async def test_partial_always_tts_falls_back_with_remaining_text_only(self) -> None:
        tts_service = SimpleNamespace(
            available=True,
            send_message_tts_result=AsyncMock(
                return_value=TTSDeliveryResult(
                    requested_segments=("第一段。", "第二段。"),
                    sent_segment_count=1,
                    error="synthesis_failed",
                )
            ),
        )
        send_text = AsyncMock(return_value=True)
        plan = group._ReplyDeliveryPlan("第一段。第二段。", "reply", 1)
        settings = _processing_settings()
        overlay_factory = AsyncMock()

        with (
            patch("bot.handlers.group.is_tts_always_enabled", return_value=True),
            patch("bot.handlers.group.configured_auto_delete_seconds", return_value=0),
            patch("bot.handlers.group.send_reply", new=send_text),
        ):
            sent, tts_sent, stored = await group._deliver_reply_plans(
                message=SimpleNamespace(),
                delivery_plans=[plan],
                settings=settings,
                tts_mode="always",
                tts_service=tts_service,
                user_id=123,
                group_id=-10001,
                tts_already_sent=False,
                progress_overlay_factory=overlay_factory,
            )

        self.assertTrue(sent)
        self.assertTrue(tts_sent)
        self.assertEqual(stored, [plan.text])
        self.assertEqual(send_text.await_args.args[1], "第二段。")
        self.assertIsNone(send_text.await_args.kwargs["overlay"])
        overlay_factory.assert_not_awaited()

    async def test_failed_always_tts_item_falls_back_to_text(self) -> None:
        delivery_order: list[str] = []

        class SequencedTTS:
            available = True

            async def send_message_tts(
                self,
                _message: object,
                text: str,
                **_kwargs: object,
            ) -> bool:
                delivery_order.append(f"voice:{text}")
                return text == "second"

        async def send_text_fallback(
            _message: object,
            text: str,
            **_kwargs: object,
        ) -> bool:
            delivery_order.append(f"text:{text}")
            return True

        tts_service = SequencedTTS()
        send_text = AsyncMock(side_effect=send_text_fallback)
        plans = [
            group._ReplyDeliveryPlan("first", "reply", 1),
            group._ReplyDeliveryPlan("second", "message", None),
        ]
        settings = _processing_settings()
        overlay = ReplyMessageOverlay(
            message=SimpleNamespace(
                message_id=77,
                chat=SimpleNamespace(id=-10001),
            ),
            status_html=(
                "<blockquote><s>已理解问题</s>\n"
                "<b>当前</b>　已整理并发送回答</blockquote>"
            ),
            reply_to_message_id=1,
            sent_as_reply=True,
        )
        overlay_factory = AsyncMock(return_value=overlay)

        with (
            patch("bot.handlers.group.is_tts_always_enabled", return_value=True),
            patch("bot.handlers.group.configured_auto_delete_seconds", return_value=0),
            patch("bot.handlers.group.send_reply", new=send_text),
        ):
            sent, tts_sent, stored = await group._deliver_reply_plans(
                message=SimpleNamespace(),
                delivery_plans=plans,
                settings=settings,
                tts_mode="always",
                tts_service=tts_service,
                user_id=123,
                group_id=-10001,
                tts_already_sent=False,
                progress_overlay_factory=overlay_factory,
            )

        self.assertTrue(sent)
        self.assertTrue(tts_sent)
        self.assertEqual(stored, ["first", "second"])
        send_text.assert_awaited_once()
        self.assertEqual(send_text.await_args.args[1], "first")
        self.assertIs(send_text.await_args.kwargs["overlay"], overlay)
        overlay_factory.assert_awaited_once_with()
        self.assertEqual(
            delivery_order,
            ["voice:first", "text:first", "voice:second"],
        )

    async def test_all_failed_tts_reuses_progress_for_first_text_fallback(
        self,
    ) -> None:
        tts_service = SimpleNamespace(
            available=True,
            send_message_tts=AsyncMock(return_value=False),
        )
        send_text = AsyncMock(return_value=True)
        overlay = ReplyMessageOverlay(
            message=SimpleNamespace(
                message_id=77,
                chat=SimpleNamespace(id=-10001),
            ),
            status_html=(
                "<blockquote><s>已理解问题</s>\n"
                "<b>当前</b>　已整理并发送回答</blockquote>"
            ),
            reply_to_message_id=1,
            sent_as_reply=True,
        )
        overlay_factory = AsyncMock(return_value=overlay)

        with (
            patch("bot.handlers.group.is_tts_always_enabled", return_value=True),
            patch("bot.handlers.group.configured_auto_delete_seconds", return_value=0),
            patch("bot.handlers.group.send_reply", new=send_text),
        ):
            sent, tts_sent, stored = await group._deliver_reply_plans(
                message=SimpleNamespace(),
                delivery_plans=[
                    group._ReplyDeliveryPlan("文字降级", "reply", 1),
                ],
                settings=_processing_settings(),
                tts_mode="always",
                tts_service=tts_service,
                user_id=123,
                group_id=-10001,
                tts_already_sent=False,
                progress_overlay_factory=overlay_factory,
            )

        self.assertTrue(sent)
        self.assertFalse(tts_sent)
        self.assertEqual(stored, ["文字降级"])
        self.assertIs(send_text.await_args.kwargs["overlay"], overlay)
        overlay_factory.assert_awaited_once_with()

    async def test_mandatory_text_is_visible_after_earlier_tts(self) -> None:
        tts_service = SimpleNamespace(
            available=True,
            send_message_tts=AsyncMock(),
        )
        send_text = AsyncMock(return_value=True)
        settings = _processing_settings()
        overlay_factory = AsyncMock()

        with (
            patch("bot.handlers.group.is_tts_always_enabled", return_value=True),
            patch("bot.handlers.group.configured_auto_delete_seconds", return_value=0),
            patch("bot.handlers.group.send_reply", new=send_text),
        ):
            sent, tts_sent, stored = await group._deliver_reply_plans(
                message=SimpleNamespace(),
                delivery_plans=[group._ReplyDeliveryPlan("quota refusal", "reply", 1)],
                settings=settings,
                tts_mode="always",
                tts_service=tts_service,
                user_id=123,
                group_id=-10001,
                tts_already_sent=True,
                force_text=True,
                progress_overlay_factory=overlay_factory,
            )

        self.assertTrue(sent)
        self.assertTrue(tts_sent)
        self.assertEqual(stored, ["quota refusal"])
        tts_service.send_message_tts.assert_not_awaited()
        send_text.assert_awaited_once()
        self.assertIsNone(send_text.await_args.kwargs["overlay"])
        overlay_factory.assert_not_awaited()


class GroupActivityCASTests(unittest.IsolatedAsyncioTestCase):
    async def asyncTearDown(self) -> None:
        tasks = [
            state.task
            for state in group._GROUP_ACTIVITY_PENDING.values()
            if state.task is not None and not state.task.done()
        ]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        group._GROUP_ACTIVITY_PENDING.clear()

    async def test_retry_merges_activity_into_concurrently_changed_settings(self) -> None:
        rows = [
            SimpleNamespace(settings={"private": "old"}),
            SimpleNamespace(settings={"private": "new", "tts_mode": "always"}),
        ]
        session = SimpleNamespace(
            in_transaction=Mock(return_value=False),
            get=AsyncMock(side_effect=rows),
            execute=AsyncMock(
                side_effect=[
                    SimpleNamespace(rowcount=0),
                    SimpleNamespace(rowcount=1),
                ]
            ),
            commit=AsyncMock(),
            rollback=AsyncMock(),
        )

        result = await group._record_group_activity_cas(
            session,
            group_id=-10001,
            title="group",
            settings=SimpleNamespace(bot=BotConfig()),
        )

        self.assertEqual(result["private"], "new")
        self.assertEqual(result["tts_mode"], "always")
        task_state = result["scheduled_tasks"]["cooldown_topic"]
        self.assertEqual(task_state["activity_revision"], 1)
        self.assertEqual(session.execute.await_count, 2)
        session.rollback.assert_awaited_once()
        session.commit.assert_awaited_once()

    async def test_production_activity_writes_are_coalesced_off_hot_path(self) -> None:
        read_session = SimpleNamespace(
            in_transaction=Mock(side_effect=[True, True, False, True, True, False]),
            get=AsyncMock(
                return_value=SimpleNamespace(settings={"mute_all_replies": True})
            ),
            commit=AsyncMock(),
        )

        class _WriteContext:
            async def __aenter__(self):
                return SimpleNamespace()

            async def __aexit__(self, exc_type, exc, tb):
                return False

        factory = Mock(side_effect=lambda: _WriteContext())
        persist = AsyncMock(return_value={})
        settings = SimpleNamespace(bot=BotConfig())

        with (
            patch.object(group, "_GROUP_ACTIVITY_DEBOUNCE_SECONDS", 0.01),
            patch.object(group, "_persist_group_activity_cas", new=persist),
        ):
            first = await group._record_group_activity_cas(
                read_session,
                group_id=-10001,
                title="group",
                settings=settings,
                session_factory=factory,
            )
            second = await group._record_group_activity_cas(
                read_session,
                group_id=-10001,
                title="group-renamed",
                settings=settings,
                session_factory=factory,
            )
            task = group._GROUP_ACTIVITY_PENDING[-10001].task
            self.assertIsNotNone(task)
            await asyncio.wait_for(task, timeout=1.0)

        self.assertTrue(first["mute_all_replies"])
        self.assertTrue(second["mute_all_replies"])
        persist.assert_awaited_once()
        self.assertEqual(persist.await_args.kwargs["title"], "group-renamed")


if __name__ == "__main__":
    unittest.main()
