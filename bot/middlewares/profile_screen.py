from __future__ import annotations

import asyncio
import html
import logging
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Awaitable, Callable

from aiogram import BaseMiddleware
from aiogram.types import Message
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from bot.services.admin_status import is_user_admin_cached
from bot.services.authz import is_group_authorized, is_super_admin_user_id
from bot.services.join_screening import (
    add_global_ban,
    build_join_profile_text,
    get_global_ban,
    get_profile_screen_hash,
    mark_profile_screened,
    moderation_rules_fingerprint,
    profile_screen_signature,
    screen_member_profile_verbose,
)
from bot.services.llm import LLMService
from bot.services.moderation import ModerationService
from bot.utils.telegram import answer_with_auto_delete

log = logging.getLogger(__name__)


@dataclass
class _ScreeningGate:
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    users: int = 0
    reviewed_signature: str | None = None
    violation_confirmed: bool = False
    reason: str = ""


class ProfileScreenEnforcementMiddleware(BaseMiddleware):
    """Re-screens a sender's visible profile on every group message.

    Registered as an OUTER middleware (after the global-ban one) so it also
    covers messages that never reach the group handler: matched commands like
    /help, videos, and empty-text media. A renamed violating user cannot dodge
    re-screening by only sending those.

    The stored signature includes a fingerprint of the group's enabled
    moderation rules, so adding or editing rules re-screens everyone on their
    next message even if their profile did not change. The screening LLM call
    only happens when the signature differs from the stored one.
    """

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self.session_factory = session_factory
        self._screening_gates: dict[tuple[int, int], _ScreeningGate] = {}

    @asynccontextmanager
    async def _screening_gate(
        self,
        group_id: int,
        user_id: int,
    ) -> AsyncIterator[_ScreeningGate]:
        """Serialize profile screening without retaining idle per-user locks."""
        key = (group_id, user_id)
        gate = self._screening_gates.get(key)
        if gate is None:
            gate = _ScreeningGate()
            self._screening_gates[key] = gate
        gate.users += 1

        acquired = False
        try:
            await gate.lock.acquire()
            acquired = True
            yield gate
        finally:
            if acquired:
                gate.lock.release()
            gate.users -= 1
            if gate.users == 0 and self._screening_gates.get(key) is gate:
                self._screening_gates.pop(key, None)

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
        if settings is None or not settings.moderation.enabled:
            return await handler(event, data)
        if is_super_admin_user_id(user.id, settings):
            return await handler(event, data)

        full_name = (getattr(user, "full_name", None) or "").strip()
        username = (getattr(user, "username", None) or "").strip()
        violation_confirmed = False
        reason = ""
        try:
            # Fast path for an unchanged, previously-passed profile. Always
            # query the ban after the signature read: a concurrent message may
            # already have passed the outer global-ban middleware before
            # another message committed both the signature and ban.
            async with self.session_factory() as session:
                if not await is_group_authorized(session, chat.id):
                    return await handler(event, data)
                rules_fp = await moderation_rules_fingerprint(session, chat.id)
                signature = profile_screen_signature(
                    full_name=full_name,
                    username=username,
                    rules_fingerprint=rules_fp,
                )
                signature_matches = (
                    await get_profile_screen_hash(session, user.id) == signature
                )
                ban = await get_global_ban(session, user.id)
                if ban is not None:
                    violation_confirmed = True
                    reason = str(ban.reason or "资料命中群规")
                elif signature_matches:
                    return await handler(event, data)

            if not violation_confirmed:
                async with self._screening_gate(chat.id, user.id) as gate:
                    if gate.violation_confirmed:
                        violation_confirmed = True
                        reason = gate.reason
                    else:
                        # Double-check in a fresh session after acquiring the
                        # gate. The preceding task may have committed either a
                        # passed signature or a global ban while we waited.
                        async with self.session_factory() as session:
                            rules_fp = await moderation_rules_fingerprint(session, chat.id)
                            signature = profile_screen_signature(
                                full_name=full_name,
                                username=username,
                                rules_fingerprint=rules_fp,
                            )
                            ban = await get_global_ban(session, user.id)
                            if ban is not None:
                                violation_confirmed = True
                                reason = str(ban.reason or "资料命中群规")
                                gate.violation_confirmed = True
                                gate.reason = reason
                            elif gate.reviewed_signature == signature:
                                pass
                            elif await get_profile_screen_hash(session, user.id) == signature:
                                gate.reviewed_signature = signature
                            else:
                                # Telegram admins are exempt from screening; do
                                # not mark them screened so a later demotion
                                # re-screens on the next message.
                                if await is_user_admin_cached(event):
                                    gate.reviewed_signature = signature
                                else:
                                    moderation = ModerationService(
                                        settings.moderation,
                                        _build_llm(settings),
                                    )
                                    if await moderation.is_user_exempt(
                                        session,
                                        chat.id,
                                        user.id,
                                    ):
                                        gate.reviewed_signature = signature
                                    else:
                                        profile_text = build_join_profile_text(
                                            full_name=full_name,
                                            username=username,
                                            bio="",
                                        )
                                        violated, reason, conclusive = (
                                            await screen_member_profile_verbose(
                                                session,
                                                moderation,
                                                group_id=chat.id,
                                                user_id=user.id,
                                                profile_text=profile_text,
                                            )
                                        )
                                        if violated:
                                            # Publish the verdict to all current
                                            # waiters before database I/O. If a
                                            # commit fails, an already-confirmed
                                            # violation still cannot fail open.
                                            violation_confirmed = True
                                            gate.violation_confirmed = True
                                            gate.reason = reason
                                            await add_global_ban(
                                                session,
                                                user.id,
                                                reason=f"资料命中群规: {reason}"[:500],
                                                source="join_screening",
                                                created_by=0,
                                            )
                                            await mark_profile_screened(
                                                session,
                                                user.id,
                                                profile_hash=signature,
                                            )
                                            # Persist the ban row before Telegram
                                            # calls so a failed notice/delete
                                            # cannot roll the registry entry back.
                                            await session.commit()
                                        elif conclusive:
                                            await mark_profile_screened(
                                                session,
                                                user.id,
                                                profile_hash=signature,
                                            )
                                            await session.commit()
                                            gate.reviewed_signature = signature
                                        else:
                                            # Share this fail-open result only
                                            # with messages already queued on
                                            # the gate. A later message creates
                                            # a fresh gate and retries screening.
                                            gate.reviewed_signature = signature
                                        # Inconclusive verdicts are not cached:
                                        # re-screen the next message.
        except Exception:
            log.exception("profile re-screening failed | chat=%s user=%s", chat.id, user.id)
            if not violation_confirmed:
                return await handler(event, data)

        if not violation_confirmed:
            return await handler(event, data)

        log.info("[%s] profile re-screening hit | user=%s reason=%s", chat.id, user.id, reason or "-")
        try:
            await event.delete()
        except Exception:
            pass
        try:
            await event.chat.ban(user.id)
        except Exception:
            log.exception("[%s] profile screening ban failed | user=%s", chat.id, user.id)

        shown = html.escape(full_name or (f"@{username}" if username else str(user.id)))
        notice = (
            f"🚫 已封禁成员 <b>{shown}</b>（ID: <code>{user.id}</code>）\n"
            f"原因：{html.escape(reason or '资料命中群规')}\n"
            "如需解封请管理员使用 /unban 命令。"
        )
        try:
            await answer_with_auto_delete(
                event,
                notice,
                auto_delete_minutes=settings.bot.auto_delete_minutes,
            )
        except Exception:
            log.exception("[%s] profile screening notice failed", chat.id)
        return None


def _build_llm(settings: Any) -> LLMService:
    return LLMService(
        settings.bot.main_model,
        settings.bot.decision_model,
        settings.bot.compress_model,
        moderation=settings.bot.moderation_model,
        vision=settings.bot.vision_model,
        embed=settings.bot.embed_model,
        max_context_tokens=settings.bot.max_context_tokens,
    )
