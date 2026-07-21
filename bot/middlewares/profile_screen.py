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
from bot.services.group_settings import acquire_group_settings_write_intent
from bot.services.join_screening import (
    add_global_ban,
    build_join_profile_text,
    get_global_ban,
    get_profile_screen_hash,
    is_join_screening_exempt,
    mark_profile_screened,
    moderation_rules_fingerprint,
    profile_screen_signature,
    screen_member_profile_verbose,
)
from bot.services.join_verification import (
    complete_leased_join_verification,
    enforce_ban_with_policy_reconciliation_result,
    lease_join_verification_for_unban,
    manual_unban_generation_is_active,
    verification_release_blocked_by_ban,
    verification_restriction_required,
)
from bot.services.llm import LLMService
from bot.services.moderation import ModerationService
from bot.services.request_priority import is_privileged_execution
from bot.services.update_delivery import telegram_message_is_privileged_candidate
from bot.services.update_completion import request_current_update_retry
from bot.utils.telegram import answer_with_auto_delete, configured_auto_delete_seconds

log = logging.getLogger(__name__)


@dataclass
class _ScreeningGate:
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    users: int = 0
    reviewed_signature: str | None = None
    violation_confirmed: bool = False
    reason: str = ""
    enforcement_outcome: str = ""


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

    async def _read_screening_state(
        self,
        *,
        group_id: int,
        user_id: int,
        full_name: str,
        username: str,
    ) -> tuple[bool, str, bool, str | None]:
        """Read all fast-path state without leaking a session downstream."""
        async with self.session_factory() as session:
            if not await is_group_authorized(session, group_id):
                return False, "", False, None
            rules_fp = await moderation_rules_fingerprint(session, group_id)
            signature = profile_screen_signature(
                full_name=full_name,
                username=username,
                rules_fingerprint=rules_fp,
            )
            signature_matches = (
                await get_profile_screen_hash(session, group_id, user_id)
                == signature
            )
            ban = await get_global_ban(session, user_id)
            ban_reason = (
                str(ban.reason or "资料命中群规") if ban is not None else None
            )
            return True, signature, signature_matches, ban_reason

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
        # Permission handlers perform authoritative authorization. Never spend
        # LLM/DB profile capacity on raw management-command candidates: an
        # attacker can forge command text but cannot pass the handler check.
        # A real administrator is screened on later ordinary messages instead.
        if is_privileged_execution() or telegram_message_is_privileged_candidate(event):
            return await handler(event, data)

        full_name = (getattr(user, "full_name", None) or "").strip()
        username = (getattr(user, "username", None) or "").strip()
        violation_confirmed = False
        reason = ""
        ban_recovery = None
        try:
            # Fast path for an unchanged, previously-passed profile. Always
            # query the ban after the signature read: a concurrent message may
            # already have passed the outer global-ban middleware before
            # another message committed both the signature and ban.
            authorized, signature, signature_matches, ban_reason = (
                await self._read_screening_state(
                    group_id=chat.id,
                    user_id=user.id,
                    full_name=full_name,
                    username=username,
                )
            )
            if not authorized:
                return await handler(event, data)
            if ban_reason is not None:
                violation_confirmed = True
                reason = ban_reason
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
                        authorized, signature, signature_matches, ban_reason = (
                            await self._read_screening_state(
                                group_id=chat.id,
                                user_id=user.id,
                                full_name=full_name,
                                username=username,
                            )
                        )
                        if not authorized:
                            return await handler(event, data)
                        if ban_reason is not None:
                            violation_confirmed = True
                            reason = ban_reason
                            gate.violation_confirmed = True
                            gate.reason = reason
                        elif gate.reviewed_signature == signature:
                            pass
                        elif signature_matches:
                            gate.reviewed_signature = signature
                        else:
                            # Telegram admins are exempt from screening; do not
                            # persist a pass so a later demotion re-screens.
                            if await is_user_admin_cached(event):
                                gate.reviewed_signature = signature
                            else:
                                moderation = ModerationService(
                                    settings.moderation,
                                    _build_llm(settings),
                                )
                                async with self.session_factory() as session:
                                    exempt = await moderation.is_user_exempt(
                                        session,
                                        chat.id,
                                        user.id,
                                    )
                                if exempt:
                                    gate.reviewed_signature = signature
                                else:
                                    profile_text = build_join_profile_text(
                                        full_name=full_name,
                                        username=username,
                                        bio="",
                                    )
                                    async with self.session_factory() as session:
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
                                        async with self.session_factory() as session:
                                            # Serialize the final policy decision
                                            # with /unban and exemption writes. An
                                            # older LLM verdict must never delete a
                                            # newer administrator exemption through
                                            # add_global_ban().
                                            await acquire_group_settings_write_intent(
                                                session,
                                                chat.id,
                                            )
                                            if not await is_group_authorized(
                                                session,
                                                chat.id,
                                            ):
                                                # Authorization was revoked while
                                                # the LLM was running. Discard the
                                                # verdict; a removed group's rules
                                                # must never create a cross-group
                                                # global ban.
                                                violation_confirmed = False
                                                gate.violation_confirmed = False
                                                gate.reason = ""
                                                return await handler(event, data)
                                            if (
                                                manual_unban_generation_is_active(
                                                    chat.id,
                                                    user.id,
                                                )
                                                or await is_join_screening_exempt(
                                                    session,
                                                    user.id,
                                                )
                                                or await moderation.is_user_exempt(
                                                    session,
                                                    chat.id,
                                                    user.id,
                                                )
                                            ):
                                                await session.commit()
                                                violation_confirmed = False
                                                gate.violation_confirmed = False
                                                gate.reason = ""
                                                gate.reviewed_signature = signature
                                                log.info(
                                                    "profile verdict discarded | "
                                                    "reason=newer_admin_exemption "
                                                    "chat=%s user=%s",
                                                    chat.id,
                                                    user.id,
                                                )
                                                return await handler(event, data)
                                            ban_recovery = (
                                                await lease_join_verification_for_unban(
                                                    session,
                                                    chat.id,
                                                    user.id,
                                                    manual_unban=False,
                                                )
                                            )
                                            if ban_recovery is None:
                                                await session.rollback()
                                                log.error(
                                                    "profile ban recovery journal could not be created | "
                                                    "chat=%s user=%s",
                                                    chat.id,
                                                    user.id,
                                                )
                                                return await handler(event, data)
                                            await add_global_ban(
                                                session,
                                                user.id,
                                                reason=f"资料命中群规: {reason}"[:500],
                                                source="join_screening",
                                                created_by=0,
                                            )
                                            await mark_profile_screened(
                                                session,
                                                chat.id,
                                                user.id,
                                                profile_hash=signature,
                                            )
                                            await session.commit()
                                        # Telegram enforcement is authorized only
                                        # after the global policy commit succeeds.
                                        # A DB outage retries screening next message
                                        # instead of creating an untracked ban.
                                        violation_confirmed = True
                                        gate.violation_confirmed = True
                                        gate.reason = reason
                                    elif conclusive:
                                        async with self.session_factory() as session:
                                            await mark_profile_screened(
                                                session,
                                                chat.id,
                                                user.id,
                                                profile_hash=signature,
                                            )
                                            await session.commit()
                                        gate.reviewed_signature = signature
                                    else:
                                        # Share an inconclusive fail-open only
                                        # with tasks already queued on this gate.
                                        gate.reviewed_signature = signature
                                    # Inconclusive verdicts are not persisted;
                                    # the next message creates a fresh gate.
        except Exception:
            log.exception("profile re-screening failed | chat=%s user=%s", chat.id, user.id)
            if not violation_confirmed:
                return await handler(event, data)

        if not violation_confirmed:
            return await handler(event, data)

        async def preserve_ban() -> bool:
            if manual_unban_generation_is_active(chat.id, user.id):
                return False
            async with self.session_factory() as policy_session:
                if not await is_group_authorized(policy_session, chat.id):
                    await policy_session.commit()
                    return False
                blocked = await verification_release_blocked_by_ban(
                    policy_session,
                    group_id=chat.id,
                    user_id=user.id,
                )
                await policy_session.commit()
                return blocked

        async def restriction_required() -> bool:
            async with self.session_factory() as policy_session:
                required = await verification_restriction_required(
                    policy_session,
                    group_id=chat.id,
                    user_id=user.id,
                )
                await policy_session.commit()
                return required

        async def enforce_once() -> str:
            log.info(
                "[%s] profile re-screening hit | user=%s reason=%s",
                chat.id,
                user.id,
                reason or "-",
            )
            enforcement = await enforce_ban_with_policy_reconciliation_result(
                event.bot,
                int(chat.id),
                int(user.id),
                preserve_ban,
                restriction_required,
            )
            final_banned = enforcement.final_banned
            if final_banned is None:
                if not enforcement.retryable:
                    # Deterministic Telegram rejection (missing rights,
                    # unbannable target, or unreachable group): replaying the
                    # update cannot succeed and would trip the transport
                    # failure threshold.
                    # Keep deleting messages; enforcement resumes once an
                    # operator fixes the group setup.
                    log.error(
                        "[%s] profile screening ban needs group setup change; "
                        "completing update without retry | user=%s "
                        "unreachable=%s operator_action=%s",
                        chat.id,
                        user.id,
                        enforcement.group_unreachable,
                        enforcement.operator_action_required,
                    )
                    return "unenforced"
                log.error(
                    "[%s] profile screening ban unconfirmed; durable global policy retained | "
                    "user=%s",
                    chat.id,
                    user.id,
                )
                request_current_update_retry()
                return "retry"
            if not final_banned:
                return (
                    "restricted"
                    if enforcement.final_restricted is True
                    else "released"
                )
            if ban_recovery is not None:
                async with self.session_factory() as completion_session:
                    completed = await complete_leased_join_verification(
                        completion_session,
                        verification_id=int(ban_recovery.verification_id),
                        lease_until=ban_recovery.lease_until,
                        status="unbanning",
                    )
                    if completed:
                        await completion_session.commit()
                    else:
                        await completion_session.rollback()
                        enforcement = (
                            await enforce_ban_with_policy_reconciliation_result(
                                event.bot,
                                int(chat.id),
                                int(user.id),
                                preserve_ban,
                                restriction_required,
                            )
                        )
                        final_banned = enforcement.final_banned
                        if final_banned is not True:
                            if final_banned is None:
                                if not enforcement.retryable:
                                    return "unenforced"
                                request_current_update_retry()
                                return "retry"
                            return (
                                "restricted"
                                if enforcement.final_restricted is True
                                else "released"
                            )
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
                    auto_delete_seconds=configured_auto_delete_seconds(
                        settings, "moderation"
                    ),
                )
            except Exception:
                log.exception("[%s] profile screening notice failed", chat.id)
            return "banned"

        async with self._screening_gate(chat.id, user.id) as enforcement_gate:
            outcome = enforcement_gate.enforcement_outcome
            if not outcome:
                outcome = await enforce_once()
                if outcome != "retry":
                    enforcement_gate.enforcement_outcome = outcome
                    if outcome == "released":
                        enforcement_gate.violation_confirmed = False
                        enforcement_gate.reason = ""

        if outcome == "released":
            return await handler(event, data)
        if outcome == "retry":
            return None
        # Only one waiter performs the expensive, idempotent Telegram ban and
        # sends the moderation notice, but every message that entered this
        # shared violating-profile generation must still be removed.  Keeping
        # deletion outside ``enforce_once`` prevents queued messages from
        # surviving merely because another waiter won the enforcement gate.
        try:
            await event.delete()
        except Exception:
            pass
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
