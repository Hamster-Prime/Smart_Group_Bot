from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from bot.handlers import group


def _user(
    user_id: int,
    name: str,
    *,
    username: str = "",
    is_bot: bool = False,
) -> SimpleNamespace:
    first, _, last = name.partition(" ")
    return SimpleNamespace(
        id=user_id,
        username=username or None,
        first_name=first,
        last_name=last or None,
        full_name=name,
        is_bot=is_bot,
        is_premium=True,
        language_code="zh-hans",
    )


def _message(
    message_id: int,
    text: str,
    *,
    sender: SimpleNamespace,
    reply_to: SimpleNamespace | None = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        message_id=message_id,
        text=text,
        caption=None,
        from_user=sender,
        sender_chat=None,
        reply_to_message=reply_to,
        chat=SimpleNamespace(
            id=-10001,
            type="supergroup",
            title="测试群",
            username="test_group",
        ),
        date=None,
        edit_date=None,
        message_thread_id=77,
        media_group_id=None,
        author_signature=None,
        has_protected_content=False,
        via_bot=None,
        quote=None,
        external_reply=None,
        forward_origin=None,
        entities=[],
        caption_entities=[],
        animation=None,
        audio=None,
        document=None,
        photo=None,
        sticker=None,
        video=None,
        video_note=None,
        voice=None,
        contact=None,
    )


class MessageArchiveMetadataTests(unittest.IsolatedAsyncioTestCase):
    def test_reply_sender_and_thread_are_structured(self) -> None:
        target = _message(41, "之前的部署方案", sender=_user(7, "Alice", username="alice"))
        message = _message(
            42,
            "就按这个做",
            sender=_user(8, "Bob Li", username="bob"),
            reply_to=target,
        )
        identity = group._resolve_sender_identity(message)

        metadata = group._message_archive_metadata(
            message,
            sender_identity=identity,
            raw_text=message.text,
        )

        self.assertEqual(metadata["telegram_message_id"], 42)
        self.assertEqual(metadata["sender_id"], 8)
        self.assertEqual(metadata["sender_username"], "bob")
        self.assertEqual(metadata["sender_first_name"], "Bob")
        self.assertEqual(metadata["sender_last_name"], "Li")
        self.assertEqual(metadata["sender_language_code"], "zh-hans")
        self.assertTrue(metadata["is_reply"])
        self.assertEqual(metadata["reply_to_message_id"], 41)
        self.assertEqual(metadata["reply_to_sender_id"], 7)
        self.assertEqual(metadata["reply_to_sender_name"], "Alice")
        self.assertEqual(metadata["reply_to_content"], "之前的部署方案")
        self.assertEqual(metadata["message_thread_id"], 77)
        self.assertEqual(metadata["extra_metadata"]["chat_id"], -10001)

    def test_sender_chat_is_recorded_as_anonymous_admin(self) -> None:
        sender_chat = SimpleNamespace(
            id=-10001,
            username=None,
            title="测试群",
            type="supergroup",
        )
        message = _message(
            50,
            "管理员通知",
            sender=_user(1087968824, "GroupAnonymousBot", is_bot=True),
        )
        message.sender_chat = sender_chat
        identity = group._resolve_sender_identity(message)

        metadata = group._message_archive_metadata(
            message,
            sender_identity=identity,
            raw_text=message.text,
        )

        self.assertEqual(metadata["sender_kind"], "anonymous_admin")
        self.assertEqual(metadata["sender_chat_id"], -10001)
        self.assertEqual(metadata["sender_chat_title"], "测试群")

    async def test_edited_message_updates_archive_without_reply_pipeline(self) -> None:
        message = _message(
            61,
            "编辑后的内容",
            sender=_user(9, "Carol", username="carol"),
        )
        message.edit_date = None
        archive = AsyncMock()
        memory = SimpleNamespace(archive_message=archive)

        with (
            patch(
                "bot.handlers.group.ensure_group_authorized",
                new=AsyncMock(return_value=True),
            ),
            patch(
                "bot.handlers.group._is_user_admin_cached",
                new=AsyncMock(return_value=False),
            ),
            patch("bot.handlers.group.memory_holder.get", return_value=memory),
        ):
            await group.on_group_message_edited(
                message,
                session=SimpleNamespace(),
                settings=SimpleNamespace(super_admin_id=1),
            )

        archive.assert_awaited_once()
        args = archive.await_args.args
        kwargs = archive.await_args.kwargs
        self.assertEqual(args, (-10001, "user", "编辑后的内容"))
        self.assertEqual(kwargs["message_id"], "61")
        self.assertEqual(kwargs["telegram_message_id"], 61)
        self.assertEqual(kwargs["sender_id"], 9)
        self.assertFalse(kwargs["extra_metadata"]["sender_is_tg_admin"])
        self.assertTrue(kwargs["defer_persistence"])


if __name__ == "__main__":
    unittest.main()
