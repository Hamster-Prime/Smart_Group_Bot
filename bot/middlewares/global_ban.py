from __future__ import annotations

import logging
from typing import Any, Awaitable, Callable

from aiogram import BaseMiddleware
from aiogram.types import Message
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from bot.db.models import UserWarning, Violation
from bot.services.authz import is_group_authorized, is_super_admin_user_id
from bot.services.ban_audit import record_ban_event
from bot.services.join_screening import is_globally_banned
from bot.services.join_verification import (
    enforce_ban_with_policy_reconciliation_result,
    reconcile_moderation_ban_after_lost_lease,
    verification_restriction_required,
)
from bot.services.recent_messages import retract_removed_member_residue
from bot.services.update_completion import request_current_update_retry

log = logging.getLogger(__name__)


async def _confirm_pending_local_bans(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    group_id: int,
    user_id: int,
) -> int:
    """Promote durable unconfirmed violation attempts after a later re-ban.

    The CAS makes concurrent messages harmless: only the worker that changes a
    row from False to True appends the success audit.  The update and audit share
    one transaction, so a database failure keeps the inbox update retryable.
    """

    async with session_factory() as session:
        authorized = await is_group_authorized(session, int(group_id))
        globally_banned = authorized and await is_globally_banned(
            session,
            int(user_id),
        )
        locally_banned = bool(
            authorized
            and await session.scalar(
                select(UserWarning.id).where(
                    UserWarning.group_id == int(group_id),
                    UserWarning.user_id == int(user_id),
                    UserWarning.is_banned.is_(True),
                )
            )
        )
        if not globally_banned and not locally_banned:
            await session.commit()
            return -1
        pending = list(
            await session.scalars(
                select(Violation).where(
                    Violation.group_id == int(group_id),
                    Violation.user_id == int(user_id),
                    Violation.ban_enforced.is_(False),
                )
            )
        )
        confirmed = 0
        for violation in pending:
            result = await session.execute(
                update(Violation)
                .where(
                    Violation.id == int(violation.id),
                    Violation.ban_enforced.is_(False),
                )
                .values(ban_enforced=True)
                .execution_options(synchronize_session=False)
            )
            if int(result.rowcount or 0) != 1:
                continue
            confirmed += 1
            await record_ban_event(
                session,
                group_id=group_id,
                target_user_id=user_id,
                action="ban",
                source="ban_reconciliation",
                outcome="succeeded",
                reason="后续消息触发持久封禁重试并确认 Telegram 已封禁",
                evidence=str(violation.message_text or ""),
                reference_type="violation",
                reference_id=int(violation.id),
            )
        await session.commit()
        return confirmed


async def _durable_ban_policy_exists(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    group_id: int,
    user_id: int,
) -> bool:
    async with session_factory() as session:
        if not await is_group_authorized(session, int(group_id)):
            return False
        if await is_globally_banned(session, int(user_id)):
            return True
        return bool(
            await session.scalar(
                select(UserWarning.id).where(
                    UserWarning.group_id == int(group_id),
                    UserWarning.user_id == int(user_id),
                    UserWarning.is_banned.is_(True),
                )
            )
        )


class GlobalBanEnforcementMiddleware(BaseMiddleware):
    """Blocks every group message from globally or locally banned users.

    Registered as an OUTER middleware on the message observer, so it runs for
    every incoming group message — commands, media, polls, locations — even
    when no handler matches. It opens its own short-lived session because
    outer middlewares run before DbSessionMiddleware. Enforcement deletes the
    message and re-bans the user in that chat. Unauthorized groups are
    skipped entirely.
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
            or getattr(event, "sender_chat", None) is not None
        ):
            return await handler(event, data)

        settings = data.get("settings")
        if settings is not None and is_super_admin_user_id(user.id, settings):
            return await handler(event, data)

        try:
            async with self.session_factory() as session:
                authorized = await is_group_authorized(session, chat.id)
                globally_banned = authorized and await is_globally_banned(
                    session,
                    user.id,
                )
                locally_banned = False
                scalar = getattr(session, "scalar", None)
                if authorized and callable(scalar):
                    locally_banned = bool(
                        await scalar(
                            select(UserWarning.id).where(
                                UserWarning.group_id == int(chat.id),
                                UserWarning.user_id == int(user.id),
                                UserWarning.is_banned.is_(True),
                            )
                        )
                    )
                banned = bool(globally_banned or locally_banned)
        except Exception:
            log.exception("global ban check failed | chat=%s user=%s", chat.id, user.id)
            return await handler(event, data)

        # The outer middleware owns a private session.  Never keep it checked
        # out while the complete downstream LLM/Telegram handler runs.
        if not authorized or not banned:
            return await handler(event, data)

        log.info("[%s] blocked message from durably banned user %s", chat.id, user.id)
        try:
            await event.delete()
        except Exception:
            pass
        # A banned rejoin may have raced several messages in before this one;
        # sweep the residue from the same fresh-join window and (for a fresh
        # rejoin) arm deletion of the "X was removed" notice the imminent ban
        # will post. Runs before the ban, so the live join marker is intact.
        try:
            await retract_removed_member_residue(event.bot, int(chat.id), int(user.id))
        except Exception:
            log.debug(
                "banned-user residue sweep failed | chat=%s user=%s",
                chat.id,
                user.id,
                exc_info=True,
            )
        try:
            async def preserve_ban() -> bool:
                return await _durable_ban_policy_exists(
                    self.session_factory,
                    group_id=int(chat.id),
                    user_id=int(user.id),
                )

            async def restriction_required() -> bool:
                async with self.session_factory() as policy_session:
                    required = await verification_restriction_required(
                        policy_session,
                        group_id=int(chat.id),
                        user_id=int(user.id),
                    )
                    await policy_session.commit()
                    return required

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
                    # Missing rights, an unbannable target, or an unreachable
                    # group is deterministic: replaying the update cannot help
                    # and would needlessly demote webhook to polling. The
                    # durable policy stays and can be enforced after the group
                    # setup changes.
                    log.warning(
                        "[%s] durable ban cannot be enforced until group setup "
                        "changes; completing update without retry | user=%s "
                        "unreachable=%s operator_action=%s",
                        chat.id,
                        user.id,
                        enforcement.group_unreachable,
                        enforcement.operator_action_required,
                    )
                else:
                    log.warning(
                        "[%s] durable ban enforcement was not confirmed | user=%s",
                        chat.id,
                        user.id,
                    )
                    request_current_update_retry()
            elif final_banned and locally_banned:
                try:
                    confirmed = await _confirm_pending_local_bans(
                        self.session_factory,
                        group_id=int(chat.id),
                        user_id=int(user.id),
                    )
                    if confirmed < 0:
                        reconciled = await reconcile_moderation_ban_after_lost_lease(
                            event.bot,
                            int(chat.id),
                            int(user.id),
                            preserve_ban,
                            restriction_required=restriction_required,
                        )
                        if not reconciled:
                            log.error(
                                "[%s] revoked durable ban could not be reconciled | user=%s",
                                chat.id,
                                user.id,
                            )
                except Exception:
                    log.exception(
                        "[%s] durable ban confirmation persistence failed | user=%s",
                        chat.id,
                        user.id,
                    )
                    if await preserve_ban():
                        request_current_update_retry()
                    else:
                        await reconcile_moderation_ban_after_lost_lease(
                            event.bot,
                            int(chat.id),
                            int(user.id),
                            preserve_ban,
                            restriction_required=restriction_required,
                        )
        except Exception:
            log.exception("[%s] global ban enforcement failed | user=%s", chat.id, user.id)
            request_current_update_retry()
        return None
