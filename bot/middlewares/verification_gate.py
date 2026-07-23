"""Message gate for members with an unresolved verification requirement.

Join verification relies on ``restrictChatMember`` to silence a new member,
but the mute is not instant: queued join security work, profile screening and
the restrict call itself all run after the join update, so a fast spammer can
land messages first. Those messages passed no check — the member has not
proven anything yet — so this outer middleware deletes every group message
whose sender still has a verification record in a restriction-required state
(``preparing``/``pending``/``enforcing``) and swallows the update.

It also feeds the in-process recent-message buffer used by
``bot.services.recent_messages`` so verification start and ban enforcement
can retroactively clear residue sent before any record existed at all. Join
service messages (``new_chat_members``) are recorded under each announced
member — not their inviter — because the announcement reprints the member's
display name and must disappear with the member's other residue when a
verification timeout or screening ban removes them.

The mirror artifact is handled here too: when a terminal removal bans/kicks
an unverified member, Telegram posts a ``left_chat_member`` ("X was removed")
service message that reprints the same name. That message is created only
after the ban, so it cannot be pre-recorded; the removing site arms a
one-shot mark (``mark_member_removed``) and this gate deletes the
announcement when its update arrives.
"""
from __future__ import annotations

import logging
from typing import Any, Awaitable, Callable

from aiogram import BaseMiddleware
from aiogram.types import Message
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from bot.services.authz import is_super_admin_user_id
from bot.services.join_verification import verification_restriction_required
from bot.services.recent_messages import (
    consume_member_removal,
    record_group_message,
)
from bot.utils.telegram import schedule_message_auto_delete_durable

log = logging.getLogger(__name__)


class PendingVerificationGateMiddleware(BaseMiddleware):
    """Deletes group messages from senders who must still pass verification.

    Registered as an OUTER middleware after GlobalBanEnforcementMiddleware:
    banned users are already handled there, and this gate must run before the
    profile-screening middleware so an unverified sender cannot consume LLM
    screening capacity either.
    """

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self.session_factory = session_factory

    @staticmethod
    async def _delete_or_schedule(event: Message, chat_id: int) -> None:
        """Delete a message, falling back to the durable deletion queue.

        A transient Telegram outage or permissions hiccup must not leave the
        residue behind; the durable queue retries once conditions recover.
        """
        try:
            await event.delete()
        except Exception:
            try:
                await schedule_message_auto_delete_durable(event, 1)
            except Exception:
                log.exception(
                    "verification gate fallback delete failed | chat=%s message=%s",
                    chat_id,
                    getattr(event, "message_id", 0),
                )

    async def __call__(
        self,
        handler: Callable[[Message, dict[str, Any]], Awaitable[Any]],
        event: Message,
        data: dict[str, Any],
    ) -> Any:
        chat = getattr(event, "chat", None)
        user = getattr(event, "from_user", None)
        if chat is None or getattr(chat, "type", "") not in ("group", "supergroup"):
            return await handler(event, data)

        # A join service message announces each new member's display name, but
        # its from_user is the actor (the joiner on self-join, the inviter —
        # possibly an admin — otherwise). Record it under every new member
        # before any sender-based bypass below, so a verification timeout or
        # screening ban can retract the announcement with the member's other
        # residue instead of leaving the unverified name exposed.
        service_message_id = int(getattr(event, "message_id", 0) or 0)
        for member in getattr(event, "new_chat_members", None) or ():
            if member is None or bool(getattr(member, "is_bot", False)):
                continue
            member_id = int(getattr(member, "id", 0) or 0)
            if member_id > 0:
                record_group_message(int(chat.id), member_id, service_message_id)

        # A "X was removed" service message for a member we just removed
        # reprints their unverified name. Its from_user is the actor (bot or
        # admin), so key on the departed member and handle it before the
        # sender-based bypasses below.
        left_member = getattr(event, "left_chat_member", None)
        if left_member is not None and not bool(getattr(left_member, "is_bot", False)):
            left_id = int(getattr(left_member, "id", 0) or 0)
            if left_id > 0 and consume_member_removal(int(chat.id), left_id):
                log.info(
                    "[%s] deleting removal service message for member %s",
                    chat.id,
                    left_id,
                )
                await self._delete_or_schedule(event, chat.id)
                return None

        if (
            user is None
            or getattr(user, "is_bot", False)
            or getattr(event, "sender_chat", None) is not None
        ):
            return await handler(event, data)

        settings = data.get("settings")
        if settings is not None and is_super_admin_user_id(user.id, settings):
            return await handler(event, data)

        gated = False
        try:
            async with self.session_factory() as session:
                gated = await verification_restriction_required(
                    session,
                    group_id=int(chat.id),
                    user_id=int(user.id),
                )
        except Exception:
            log.exception(
                "verification gate check failed | chat=%s user=%s",
                chat.id,
                user.id,
            )
        if not gated:
            # No live challenge yet. Remember the message id: if this sender
            # is racing the join-verification mute, the restrict site sweeps
            # these ids right after the restriction lands.
            record_group_message(
                int(chat.id),
                int(user.id),
                int(getattr(event, "message_id", 0) or 0),
            )
            return await handler(event, data)

        log.info(
            "[%s] deleted message from unverified member %s",
            chat.id,
            user.id,
        )
        await self._delete_or_schedule(event, chat.id)
        return None
