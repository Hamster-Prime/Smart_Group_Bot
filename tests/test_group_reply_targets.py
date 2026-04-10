import unittest
from types import SimpleNamespace

from bot.handlers import group


def _message(message_id: int, text: str, *, sender_name: str = "User", reply_to_message=None):
    return SimpleNamespace(
        message_id=message_id,
        text=text,
        caption=None,
        photo=None,
        video=None,
        animation=None,
        document=None,
        audio=None,
        contact=None,
        sticker=None,
        voice=None,
        video_note=None,
        location=None,
        from_user=SimpleNamespace(id=message_id, full_name=sender_name, username="user"),
        sender_chat=None,
        reply_to_message=reply_to_message,
        external_reply=None,
        quote=None,
    )


def _item(message, text: str) -> group._PendingReplyItem:
    return group._PendingReplyItem(
        message=message,
        group_id=-10001,
        user_id=123,
        input_text=text,
        msg_type="text",
        sender_username="tester",
        sender_is_owner=False,
        sender_is_tg_admin=False,
        user_tag="id:123",
        explicit_mention=False,
        mentioned=False,
        is_reply=bool(getattr(message, "reply_to_message", None)),
        reply_to_bot=False,
        reply_to_other=bool(getattr(message, "reply_to_message", None)),
        mention_other=False,
    )


class ReplyTargetResolutionTests(unittest.TestCase):
    def test_build_reply_targets_context_exposes_aliases(self) -> None:
        replied = _message(51, "anchor", sender_name="Other")
        first = _message(52, "first")
        latest = _message(53, "latest", reply_to_message=replied)
        context, alias_map = group._build_reply_targets_context(
            [_item(first, "first"), _item(latest, "latest")]
        )

        self.assertIn("[REPLY_TARGET_CANDIDATES]", context)
        self.assertEqual(alias_map["first_input"], 52)
        self.assertEqual(alias_map["latest_input"], 53)
        self.assertEqual(alias_map["input_1"], 52)
        self.assertEqual(alias_map["input_2"], 53)
        self.assertEqual(alias_map["latest_reply_target"], 51)

    def test_resolve_reply_target_supports_aliases_and_ids(self) -> None:
        alias_map = {
            "latest_input": 100,
            "first_input": 90,
            "latest_reply_target": 80,
            "input_2": 95,
        }

        self.assertEqual(
            group._resolve_reply_target_message_id("auto", alias_map=alias_map),
            100,
        )
        self.assertEqual(
            group._resolve_reply_target_message_id("first_input", alias_map=alias_map),
            90,
        )
        self.assertEqual(
            group._resolve_reply_target_message_id("latest_reply_target", alias_map=alias_map),
            80,
        )
        self.assertEqual(
            group._resolve_reply_target_message_id("message_id:77", alias_map=alias_map),
            77,
        )
        self.assertEqual(
            group._resolve_reply_target_message_id("none", alias_map=alias_map),
            None,
        )

    def test_normalize_multi_message_delivery_plans_only_first_same_target_keeps_reply(self) -> None:
        plans = [
            group._ReplyDeliveryPlan(text="第一条", delivery_mode="reply", reply_to_message_id=100),
            group._ReplyDeliveryPlan(text="第二条", delivery_mode="reply", reply_to_message_id=100),
            group._ReplyDeliveryPlan(text="第三条", delivery_mode="reply", reply_to_message_id=100),
        ]

        normalized = group._normalize_multi_message_delivery_plans(plans)

        self.assertEqual(
            [(plan.delivery_mode, plan.reply_to_message_id) for plan in normalized],
            [("reply", 100), ("message", None), ("message", None)],
        )

    def test_normalize_multi_message_delivery_plans_keeps_reply_when_target_changes(self) -> None:
        plans = [
            group._ReplyDeliveryPlan(text="第一条", delivery_mode="reply", reply_to_message_id=100),
            group._ReplyDeliveryPlan(text="第二条", delivery_mode="reply", reply_to_message_id=200),
            group._ReplyDeliveryPlan(text="第三条", delivery_mode="reply", reply_to_message_id=200),
        ]

        normalized = group._normalize_multi_message_delivery_plans(plans)

        self.assertEqual(
            [(plan.delivery_mode, plan.reply_to_message_id) for plan in normalized],
            [("reply", 100), ("reply", 200), ("message", None)],
        )


if __name__ == "__main__":
    unittest.main()
