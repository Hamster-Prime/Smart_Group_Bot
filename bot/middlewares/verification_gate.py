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
can retroactively clear residue sent before any record existed at all.
"""
from __future__ import annotations

import logging
from typing import Any, Awaitable, Callable

from aiogram import BaseMiddleware
from aiogram.types import Message
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from bot.services.authz import is_super_admin_user_id
from bot.services.join_verification import verification_restriction_required
from bot.services.recent_messages import record_group_message
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

    async def __call__(
        self,
        handler: Callable[[Message, dict[str, Any]], Awaitable[Any]],
        event: Message,
        data: dict[str, Any],
    ) -> Any:
        chat = getattr(event, "chat", None)
        user = getattr(event, "from_user", None)
        if (
            chat is None
            or getattr(chat, "type", "") not in ("group", "supergroup")
            or user is None
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
        try:
            await event.delete()
        except Exception:
            # Telegram refused the direct delete (transient outage or a
            # permissions hiccup). Hand the message to the durable deletion
            # queue so the residue still disappears once conditions recover.
            try:
                await schedule_message_auto_delete_durable(event, 1)
            except Exception:
                log.exception(
                    "verification gate fallback delete failed | chat=%s message=%s",
                    chat.id,
                    getattr(event, "message_id", 0),
                )
        return None
