import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from aiogram.exceptions import TelegramBadRequest

from bot.services.reply_progress import (
    ProgressReference,
    ProgressUpdate,
    ReplyProgressTracker,
)


def _message_pair() -> tuple[SimpleNamespace, SimpleNamespace]:
    sent = SimpleNamespace(
        edit_text=AsyncMock(),
        delete=AsyncMock(),
        chat=SimpleNamespace(id=-1001),
        message_id=42,
    )
    incoming = SimpleNamespace(
        message_id=99,
        chat=SimpleNamespace(id=-1001),
        reply=AsyncMock(return_value=sent),
        answer=AsyncMock(return_value=sent),
    )
    return incoming, sent


class ReplyProgressTrackerTests(unittest.IsolatedAsyncioTestCase):
    async def test_fast_reply_finishes_without_sending_progress(self) -> None:
        message, _ = _message_pair()
        tracker = ReplyProgressTracker(message, True, reveal_after=0.05)

        await tracker.start()
        await tracker.finish()
        await asyncio.sleep(0.07)

        message.reply.assert_not_awaited()
        message.answer.assert_not_awaited()
        self.assertFalse(tracker.visible)
        await tracker.close()

    async def test_notice_is_revealed_after_delay(self) -> None:
        message, _ = _message_pair()
        tracker = ReplyProgressTracker(message, True, reveal_after=0.01)

        await tracker.start()
        self.assertFalse(tracker.visible)
        await asyncio.sleep(0.03)

        self.assertTrue(tracker.visible)
        message.reply.assert_awaited_once()
        body = message.reply.await_args.args[0]
        self.assertNotIn("正在处理</b>", body)
        self.assertEqual(
            body,
            "<blockquote><b>01　正在理解问题</b></blockquote>",
        )
        await tracker.close()

    async def test_same_key_updates_one_line_and_history_accumulates(self) -> None:
        message, sent = _message_pair()
        tracker = ReplyProgressTracker(
            message,
            True,
            reveal_after=0,
            edit_interval=0,
        )
        await tracker.start()
        await tracker.report(
            ProgressUpdate("search", "running", "正在搜索官方资料")
        )
        await tracker.report(
            ProgressUpdate("search", "completed", "已搜索官方资料")
        )
        await tracker.report(
            ProgressUpdate("read", "running", "正在读取 DNS 文档")
        )
        await asyncio.sleep(0.01)

        body = sent.edit_text.await_args.args[0]
        self.assertEqual(body.count("搜索官方资料"), 1)
        self.assertIn("01　已理解问题", body)
        self.assertIn("02　已搜索官方资料", body)
        self.assertIn("<b>03　正在读取 DNS 文档</b>", body)
        self.assertLess(body.index("已理解问题"), body.index("已搜索官方资料"))
        self.assertLess(body.index("已搜索官方资料"), body.index("正在读取 DNS"))
        await tracker.close()

    async def test_references_are_deduplicated_escaped_and_expandable(self) -> None:
        message, sent = _message_pair()
        tracker = ReplyProgressTracker(
            message,
            True,
            reveal_after=0,
            edit_interval=0,
        )
        await tracker.start()
        reference = ProgressReference(
            title="DNS <配置>",
            url="https://example.com/docs?a=1&b=2",
        )
        trusted_reference = ProgressReference(
            title="Mihomo DNS",
            url="https://wiki.metacubex.one/config/dns/?token=hidden",
            trusted_path=True,
        )
        await tracker.report(
            ProgressUpdate(
                "read",
                "completed",
                "已读取文档",
                references=(reference, reference, trusted_reference),
            )
        )
        await asyncio.sleep(0.01)

        body = sent.edit_text.await_args.args[0]
        self.assertIn("<blockquote expandable>", body)
        self.assertIn("<b>参考资料</b>", body)
        self.assertEqual(body.count("DNS &lt;配置&gt;"), 1)
        self.assertIn('href="https://example.com"', body)
        self.assertIn(
            'href="https://wiki.metacubex.one/config/dns/"',
            body,
        )
        self.assertNotIn('href="https://example.com/docs"', body)
        self.assertNotIn("a=1", body)
        self.assertNotIn("token=hidden", body)
        await tracker.close()

    async def test_invalid_reference_url_is_not_emitted_as_link(self) -> None:
        message, sent = _message_pair()
        tracker = ReplyProgressTracker(
            message,
            True,
            reveal_after=0,
            edit_interval=0,
        )
        await tracker.start()
        await tracker.report(
            ProgressUpdate(
                "read",
                "completed",
                "已读取内容",
                references=(
                    ProgressReference("不可信来源", 'javascript:alert("x")'),
                ),
            )
        )
        await asyncio.sleep(0.01)

        body = sent.edit_text.await_args.args[0]
        self.assertIn("不可信来源", body)
        self.assertNotIn("javascript:", body)
        self.assertNotIn("<a href=", body)
        await tracker.close()

    async def test_edits_are_throttled_and_latest_snapshot_wins(self) -> None:
        message, sent = _message_pair()
        tracker = ReplyProgressTracker(
            message,
            True,
            reveal_after=0,
            edit_interval=0.03,
        )
        await tracker.start()
        await tracker.report(ProgressUpdate("search", "running", "正在搜索"))
        await tracker.report(ProgressUpdate("search", "completed", "已搜索"))

        sent.edit_text.assert_not_awaited()
        await asyncio.sleep(0.05)

        sent.edit_text.assert_awaited_once()
        body = sent.edit_text.await_args.args[0]
        self.assertIn("已搜索", body)
        self.assertNotIn("正在搜索", body)
        await tracker.close()

    async def test_report_does_not_wait_for_slow_telegram_edit(self) -> None:
        message, sent = _message_pair()
        edit_started = asyncio.Event()
        release_edit = asyncio.Event()

        async def slow_edit(*args, **kwargs) -> None:
            del args, kwargs
            edit_started.set()
            await release_edit.wait()

        sent.edit_text.side_effect = slow_edit
        tracker = ReplyProgressTracker(
            message,
            True,
            reveal_after=0,
            edit_interval=0,
        )
        await tracker.start()

        await asyncio.wait_for(
            tracker.report(ProgressUpdate("search", "running", "正在搜索")),
            timeout=0.05,
        )
        await asyncio.wait_for(edit_started.wait(), timeout=0.05)

        release_edit.set()
        await tracker.finish()
        await tracker.close()

    async def test_fail_updates_visible_notice_with_only_failure_warning(self) -> None:
        message, sent = _message_pair()
        tracker = ReplyProgressTracker(
            message,
            True,
            reveal_after=0,
            edit_interval=30,
        )
        await tracker.start()
        await tracker.fail("读取资料失败")

        body = sent.edit_text.await_args.args[0]
        self.assertNotIn("处理未完成", body)
        self.assertIn("⚠️ 01　读取资料失败", body)
        self.assertNotIn("✅", body)
        self.assertNotIn("⏳", body)
        await tracker.close()

    async def test_missing_reply_target_falls_back_and_edit_failure_never_escapes(
        self,
    ) -> None:
        message, sent = _message_pair()
        message.reply.side_effect = TelegramBadRequest(
            method=SimpleNamespace(),
            message="Bad Request: reply message not found",
        )
        message.answer.return_value = sent
        sent.edit_text.side_effect = RuntimeError("edit unavailable")
        tracker = ReplyProgressTracker(
            message,
            True,
            reveal_after=0,
            edit_interval=0,
        )

        await tracker.start()
        await tracker.composing()
        await tracker.finish()

        message.answer.assert_awaited_once()
        self.assertTrue(tracker.visible)
        await tracker.close()

    async def test_ambiguous_send_timeout_does_not_create_second_card(self) -> None:
        message, _ = _message_pair()
        message.reply.side_effect = TimeoutError("outcome unknown")
        tracker = ReplyProgressTracker(message, True, reveal_after=0)

        await tracker.start()

        message.reply.assert_awaited_once()
        message.answer.assert_not_awaited()
        self.assertFalse(tracker.visible)
        await tracker.composing()
        await asyncio.sleep(0.01)
        message.reply.assert_awaited_once()
        await tracker.close()

    async def test_close_prevents_late_reveal(self) -> None:
        message, _ = _message_pair()
        tracker = ReplyProgressTracker(message, True, reveal_after=0.03)
        await tracker.start()

        await tracker.close()
        await asyncio.sleep(0.05)

        message.reply.assert_not_awaited()
        message.answer.assert_not_awaited()
        self.assertFalse(tracker.visible)

    async def test_dismiss_removes_visible_notice(self) -> None:
        message, sent = _message_pair()
        tracker = ReplyProgressTracker(message, True, reveal_after=0)
        await tracker.start()

        await tracker.dismiss()

        sent.delete.assert_awaited_once()
        self.assertFalse(tracker.visible)

    async def test_failed_dismiss_terminalizes_and_queues_cleanup(self) -> None:
        message, sent = _message_pair()
        sent.delete.side_effect = RuntimeError("delete unavailable")
        tracker = ReplyProgressTracker(
            message,
            True,
            reveal_after=0,
            auto_delete_seconds=30,
        )
        with patch(
            "bot.services.reply_progress.schedule_message_auto_delete_durable",
            new=AsyncMock(return_value=True),
        ) as schedule:
            await tracker.start()
            await tracker.dismiss()

        sent.edit_text.assert_awaited_once()
        self.assertIn("处理已结束", sent.edit_text.await_args.args[0])
        schedule.assert_awaited_once_with(sent, 30)
        self.assertFalse(tracker.visible)

    async def test_rejected_durable_cleanup_deletes_transient_card_now(self) -> None:
        message, sent = _message_pair()
        tracker = ReplyProgressTracker(
            message,
            True,
            reveal_after=0,
            auto_delete_seconds=30,
        )
        with patch(
            "bot.services.reply_progress.schedule_message_auto_delete_durable",
            new=AsyncMock(return_value=False),
        ):
            await tracker.start()
            await tracker.finish()

        sent.delete.assert_awaited_once()
        self.assertFalse(tracker.visible)
        await tracker.close()

    async def test_failed_warning_survives_rejected_cleanup_schedule(self) -> None:
        message, sent = _message_pair()
        tracker = ReplyProgressTracker(
            message,
            True,
            reveal_after=0,
            auto_delete_seconds=30,
        )
        with patch(
            "bot.services.reply_progress.schedule_message_auto_delete_durable",
            new=AsyncMock(return_value=False),
        ):
            await tracker.start()
            delivered = await tracker.fail("结果可能已生效，请先检查")

        self.assertTrue(delivered)
        self.assertTrue(tracker.visible)
        sent.delete.assert_not_awaited()
        self.assertIn("结果可能已生效", sent.edit_text.await_args.args[0])
        await tracker.close()

    async def test_handoff_freezes_status_for_same_message_final_reply(self) -> None:
        message, sent = _message_pair()
        tracker = ReplyProgressTracker(
            message,
            True,
            reveal_after=0,
            edit_interval=30,
        )
        await tracker.start()
        await tracker.composing()

        overlay = await tracker.handoff()

        self.assertIsNotNone(overlay)
        self.assertIs(overlay.message, sent)
        self.assertTrue(overlay.sent_as_reply)
        self.assertEqual(overlay.reply_to_message_id, 99)
        self.assertNotIn("正在处理", overlay.status_html)
        self.assertEqual(
            overlay.status_html,
            "<blockquote>01　已理解问题\n02　已整理并发送回答</blockquote>",
        )
        sent.delete.assert_not_awaited()
        await tracker.close()

    async def test_render_is_strictly_bounded_and_keeps_recent_history(self) -> None:
        message, _ = _message_pair()
        tracker = ReplyProgressTracker(message, True, reveal_after=60)
        await tracker.start()
        for index in range(80):
            await tracker.report(
                ProgressUpdate(
                    f"step-{index}",
                    "completed",
                    f"已完成第 {index:02d} 个很长的处理步骤 " + "说明" * 30,
                    references=(
                        ProgressReference(
                            f"来源 {index}",
                            "https://wiki.metacubex.one/"
                            + (f"section-{index}-" * 20),
                            trusted_path=True,
                        ),
                    ),
                )
            )
        await tracker.finish()

        body = tracker._render()

        self.assertLessEqual(len(body.encode("utf-16-le")) // 2, 3900)
        self.assertIn("较早步骤未展开", body)
        self.assertIn("已整理并发送回答", body)
        await tracker.close()

    async def test_finish_schedules_existing_auto_delete(self) -> None:
        message, sent = _message_pair()
        tracker = ReplyProgressTracker(
            message,
            True,
            reveal_after=0,
            edit_interval=0,
            auto_delete_seconds=30,
        )
        with patch(
            "bot.services.reply_progress.schedule_message_auto_delete_durable",
            new=AsyncMock(return_value=True),
        ) as schedule:
            await tracker.start()
            await tracker.finish()

        schedule.assert_awaited_once_with(sent, 30)
        await tracker.close()

    async def test_disabled_tracker_is_a_noop(self) -> None:
        message, _ = _message_pair()
        tracker = ReplyProgressTracker(message, False, reveal_after=0)

        await tracker.start()
        await tracker.report(ProgressUpdate("search", "running", "正在搜索"))
        await tracker.composing()
        await tracker.finish()
        await tracker.fail()
        await tracker.close()

        message.reply.assert_not_awaited()
        message.answer.assert_not_awaited()
        self.assertFalse(tracker.visible)


if __name__ == "__main__":
    unittest.main()
