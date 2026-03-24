import unittest
from types import SimpleNamespace

from bot.handlers import group


def _settings(delay: float = 5.0) -> SimpleNamespace:
    return SimpleNamespace(bot=SimpleNamespace(inbound_debounce_seconds=delay))


def _item(
    text: str,
    *,
    explicit_mention: bool = False,
    mentioned: bool = False,
    is_reply: bool = False,
    reply_to_bot: bool = False,
    sender_is_owner: bool = False,
) -> group._PendingReplyItem:
    return group._PendingReplyItem(
        message=None,
        group_id=-10001,
        user_id=123,
        input_text=text,
        msg_type="text",
        sender_username="tester",
        sender_is_owner=sender_is_owner,
        sender_is_tg_admin=False,
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

        self.assertAlmostEqual(due_at, 100.8, places=3)

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


if __name__ == "__main__":
    unittest.main()
