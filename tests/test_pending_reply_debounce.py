import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from bot.config import BotConfig
from bot.handlers import group
from bot.services.update_completion import UpdateCompletionReceipt


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
            patch("bot.handlers.group._is_user_admin_cached", new=admin_lookup),
            patch("bot.handlers.group._best_effort_commit", new=AsyncMock()),
        ):
            await group._process_pending_reply_batch([item], _processing_settings())

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


class PendingReplyWorkerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        async with group._PENDING_REPLY_LOCK:
            for state in group._PENDING_REPLY_BATCHES.values():
                if state.task and not state.task.done():
                    state.task.cancel()
            group._PENDING_REPLY_BATCHES.clear()

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

    async def test_cancelling_active_direct_batch_emits_visible_failure(self) -> None:
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

    async def test_failure_notifies_direct_item_even_if_later_item_is_indirect(self) -> None:
        direct_message = SimpleNamespace(message_id=10)
        later_message = SimpleNamespace(message_id=11)
        send = AsyncMock(return_value=True)

        with patch("bot.handlers.group.send_reply", new=send):
            await group._notify_pending_reply_failure(
                [
                    _item("direct", message=direct_message, mentioned=True),
                    _item("follow up", message=later_message),
                ],
                timed_out=True,
            )

        self.assertIs(send.await_args.args[0], direct_message)
        self.assertEqual(send.await_args.kwargs["reply_to_message_id"], 10)


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
    async def test_failed_always_tts_item_falls_back_to_text(self) -> None:
        tts_service = SimpleNamespace(
            available=True,
            send_message_tts=AsyncMock(side_effect=[False, True]),
        )
        send_text = AsyncMock(return_value=True)
        plans = [
            group._ReplyDeliveryPlan("first", "reply", 1),
            group._ReplyDeliveryPlan("second", "message", None),
        ]
        settings = _processing_settings()

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
            )

        self.assertTrue(sent)
        self.assertTrue(tts_sent)
        self.assertEqual(stored, ["first", "second"])
        send_text.assert_awaited_once()
        self.assertEqual(send_text.await_args.args[1], "first")

    async def test_mandatory_text_is_visible_after_earlier_tts(self) -> None:
        tts_service = SimpleNamespace(
            available=True,
            send_message_tts=AsyncMock(),
        )
        send_text = AsyncMock(return_value=True)
        settings = _processing_settings()

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
            )

        self.assertTrue(sent)
        self.assertTrue(tts_sent)
        self.assertEqual(stored, ["quota refusal"])
        tts_service.send_message_tts.assert_not_awaited()
        send_text.assert_awaited_once()


class GroupActivityCASTests(unittest.IsolatedAsyncioTestCase):
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


if __name__ == "__main__":
    unittest.main()
