"""New-member join screening: name + bio checked against group moderation rules."""
from __future__ import annotations

import asyncio
import html
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from types import SimpleNamespace

from aiogram import F, Router
from aiogram.filters import IS_NOT_MEMBER, IS_MEMBER, ChatMemberUpdatedFilter
from aiogram.types import CallbackQuery, ChatMemberUpdated, Update
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from bot.config import Settings
from bot.db.models import AuthorizedGroup, Group, JoinVerification, UserWarning
from bot.services.admin_status import invalidate_admin_status_cache
from bot.services.authz import (
    is_group_authorized,
    is_super_admin_user_id,
    set_group_bot_present,
)
from bot.services.callback_auth import is_group_admin_or_higher
from bot.services.group_settings import acquire_group_settings_write_intent
from bot.services.join_screening import (
    add_global_ban,
    build_join_profile_text,
    is_globally_banned,
    is_join_screening_exempt,
    mark_profile_screened,
    moderation_rules_fingerprint,
    profile_screen_signature,
    screen_member_profile_verbose,
)
from bot.services.join_verification import (
    PATROL_VERIFY_CALLBACK_DATA,
    RAID_VERIFY_CALLBACK_DATA,
    TERMINAL_LEASE_SECONDS,
    VERIFICATION_CALLBACK_APPROVE,
    VERIFICATION_CALLBACK_PREFIX,
    VERIFICATION_CALLBACK_REJECT,
    VERIFICATION_CALLBACK_START,
    VERIFICATION_KIND_JOIN,
    VERIFICATION_KIND_MODERATION,
    VERIFICATION_KIND_PATROL,
    VERIFICATION_KIND_RAID,
    VERIFICATION_STATUS_ENFORCING,
    VERIFICATION_STATUS_PENDING,
    VERIFICATION_STATUS_PREPARING,
    VERIFICATION_STATUS_RELEASING,
    VERIFICATION_STATUS_UNBANNING,
    ban_member,
    build_group_prompt_keyboard,
    build_group_prompt_text,
    build_private_deep_link,
    claim_join_verification,
    chat_member_is_present,
    complete_leased_join_verification,
    commit_prepared_join_verification,
    delete_join_verification,
    delete_verification_prompt,
    enforce_ban_with_policy_reconciliation,
    extend_pending_verification_deadlines,
    get_join_verification,
    join_verification_ready,
    join_verification_policy,
    join_verification_lease_is_current,
    manual_unban_generation_is_active,
    kick_member,
    lease_expired_join_verification,
    lease_join_verification_for_unban,
    mark_group_banned,
    parse_verification_callback_data,
    prepare_join_verification,
    reconcile_moderation_ban_after_lost_lease,
    reconcile_stale_verification_restriction,
    refresh_pending_join_verification,
    renew_join_verification_lease,
    renew_prepared_join_verification,
    restore_member_permissions,
    resume_group_verification_recovery,
    rollback_group_ban,
    restrict_new_member,
    shield_abort_prepared_join_verification,
    telegram_group_is_unreachable_error,
    upsert_join_verification,
    verification_deadline_passed,
    verification_release_blocked_by_ban,
    verification_restriction_required,
    verification_timeout_seconds_for_kind,
)
from bot.services.llm import LLMService
from bot.services.moderation import ModerationService
from bot.services.patrol import mark_group_member_left, track_group_member
from bot.services.raid_guard import (
    RAID_REMOVE_CALLBACK_DATA,
    RaidRemovalResult,
    get_raid_guard_service,
    remove_raid_challenged_users,
)
from bot.services.privileged_tasks import submit_privileged_task
from bot.services.request_priority import privileged_request_scope
from bot.services.update_completion import request_current_update_retry
from bot.services.update_delivery import unmark_privileged_operator
from bot.services.welcome import send_group_welcome
from bot.utils.bot_identity import get_bot_identity
from bot.utils.telegram import (
    configured_auto_delete_seconds,
    schedule_message_auto_delete_durable,
)
from bot.utils.timezone import now_shanghai_naive

router = Router()
log = logging.getLogger(__name__)


@dataclass(slots=True)
class _PendingMemberJoinSecurity:
    job_key: str
    version: int
    latest_update_id: int
    event: ChatMemberUpdated
    settings: Settings


_PENDING_MEMBER_JOIN_SECURITY: dict[tuple[int, int], _PendingMemberJoinSecurity] = {}
_MEMBER_JOIN_JOB_SEQUENCE = 0


async def _ack_security_callback(
    callback: CallbackQuery,
    text: str,
    *,
    show_alert: bool = False,
) -> None:
    async def _answer() -> None:
        # CallbackQuery.answer() returns a TelegramMethod (awaitable, not a
        # coroutine); create_task requires a genuine coroutine.
        await callback.answer(text, show_alert=show_alert)

    with privileged_request_scope():
        task = asyncio.create_task(
            _answer(),
            name="security-callback-ack",
        )
    done, _pending = await asyncio.wait({task}, timeout=2.0)
    if task in done:
        try:
            await task
        except Exception:
            log.debug("security callback acknowledgement failed", exc_info=True)
        return
    task.cancel()

    def consume(done: asyncio.Task[object]) -> None:
        if done.cancelled():
            return
        try:
            done.exception()
        except (asyncio.CancelledError, Exception):
            pass

    task.add_done_callback(consume)


async def _publish_security_callback_result(
    callback: CallbackQuery,
    text: str,
) -> None:
    message = callback.message
    try:
        if message is not None and hasattr(message, "answer"):
            await message.answer(text)
        else:
            chat = getattr(message, "chat", None)
            chat_id = int(getattr(chat, "id", 0) or 0)
            if chat_id:
                await callback.bot.send_message(chat_id, text)
    except Exception:
        log.debug("security callback result delivery failed", exc_info=True)


async def _publish_raid_removal_result(
    callback: CallbackQuery,
    *,
    group_id: int,
    prompt_message_id: int,
    result: RaidRemovalResult,
) -> None:
    removed_count = len(result.removed_user_ids)
    failed_count = len(result.failed_user_ids)
    if failed_count == 0:
        try:
            async with asyncio.timeout(5.0):
                await callback.bot.edit_message_reply_markup(
                    chat_id=group_id,
                    message_id=prompt_message_id,
                    reply_markup=None,
                )
        except Exception:
            log.debug(
                "raid bulk-remove keyboard cleanup failed | group=%s message=%s",
                group_id,
                prompt_message_id,
                exc_info=True,
            )
    if result.pending_count == 0:
        text = "该批追溯用户已全部处理。"
    elif failed_count:
        text = f"爆破防护批量移除完成：已移除 {removed_count} 人，{failed_count} 人待重试。"
    else:
        text = f"爆破防护批量移除完成：已移除 {removed_count} 名被追溯用户。"
    message = callback.message
    try:
        if message is not None and hasattr(message, "answer"):
            await message.answer(text)
        else:
            await callback.bot.send_message(group_id, text)
    except Exception:
        log.exception(
            "raid bulk-remove result delivery failed | group=%s message=%s",
            group_id,
            prompt_message_id,
        )


async def _fetch_user_bio(event: ChatMemberUpdated, user_id: int) -> str:
    """Bio is only exposed via a full getChat on the user's private chat."""
    try:
        chat = await event.bot.get_chat(user_id)
        return str(getattr(chat, "bio", "") or "")
    except Exception as exc:
        log.info("join screening bio fetch failed | user=%s error=%s", user_id, exc)
        return ""


async def _join_member_still_present(
    event: ChatMemberUpdated,
    user_id: int,
    *,
    stage: str,
) -> bool:
    """Confirm queued join work still targets a current chat member.

    An inconclusive Telegram lookup is retryable and must fail the durable
    security job instead of silently admitting or mutating a stale user.
    """

    present = await chat_member_is_present(event.bot, int(event.chat.id), int(user_id))
    if present is None:
        raise RuntimeError(
            f"membership could not be confirmed before join security stage {stage}"
        )
    if not present:
        log.info(
            "join security stopped | reason=member_left stage=%s group=%s user=%s",
            stage,
            event.chat.id,
            user_id,
        )
    return present


async def _reconcile_stale_restriction(
    event: ChatMemberUpdated,
    session: AsyncSession,
    *,
    user_id: int,
    session_factory: async_sessionmaker[AsyncSession] | None,
) -> bool:
    if session_factory is not None:
        return await reconcile_stale_verification_restriction(
            event.bot,
            session_factory,
            int(event.chat.id),
            int(user_id),
        )

    # Direct helper/unit invocations do not always carry a factory. Keep the
    # same latest-intent semantics without sharing a transaction across the
    # Telegram call.
    await session.rollback()
    blocked = await verification_release_blocked_by_ban(
        session,
        group_id=int(event.chat.id),
        user_id=int(user_id),
    )
    current = await get_join_verification(session, int(event.chat.id), int(user_id))
    current_status = (
        str(current.status or VERIFICATION_STATUS_PENDING) if current is not None else ""
    )
    await session.commit()
    if blocked:
        return await ban_member(event.bot, int(event.chat.id), int(user_id))
    if current_status in {
        VERIFICATION_STATUS_PREPARING,
        VERIFICATION_STATUS_PENDING,
        VERIFICATION_STATUS_ENFORCING,
    }:
        return True
    return await restore_member_permissions(event.bot, int(event.chat.id), int(user_id))


def _build_llm(settings: Settings) -> LLMService:
    return LLMService(
        settings.bot.main_model,
        settings.bot.decision_model,
        settings.bot.compress_model,
        moderation=settings.bot.moderation_model,
        vision=settings.bot.vision_model,
        embed=settings.bot.embed_model,
        max_context_tokens=settings.bot.max_context_tokens,
    )


async def _ban_and_notify(
    event: ChatMemberUpdated,
    settings: Settings,
    *,
    user_id: int,
    display_name: str,
    reason: str,
    preserve_ban: Callable[[], Awaitable[bool]] | None = None,
    restriction_required: Callable[[], Awaitable[bool]] | None = None,
) -> bool:
    if preserve_ban is None:
        final_banned: bool | None = (
            True
            if await ban_member(event.bot, int(event.chat.id), int(user_id))
            else None
        )
    else:
        final_banned = await enforce_ban_with_policy_reconciliation(
            event.bot,
            int(event.chat.id),
            int(user_id),
            preserve_ban,
            restriction_required,
        )
    if final_banned is not True:
        log.error(
            "join screening ban not retained by latest policy | group=%s user=%s state=%s",
            event.chat.id,
            user_id,
            final_banned,
        )
        return False
    shown = html.escape(display_name or str(user_id))
    reason_text = html.escape(reason or "入群资料命中群规")
    try:
        sent = await event.bot.send_message(
            event.chat.id,
            f"🚫 已封禁新成员 <b>{shown}</b>（ID: <code>{user_id}</code>）\n原因：{reason_text}\n"
            "如需解封请管理员使用 /unban 命令。",
            parse_mode="HTML",
        )
        # This is a moderation outcome ("审核通知"): honor the group's
        # auto-delete retention like the on-message ban notice in
        # profile_screen.py does.
        await schedule_message_auto_delete_durable(
            sent, configured_auto_delete_seconds(settings, "moderation")
        )
    except Exception:
        log.exception("join screening notice failed | group=%s", event.chat.id)
    return True


def _invalidate_admin_cache(event: ChatMemberUpdated) -> None:
    # Promotion must invalidate a cached non-admin denial immediately.
    # Positive admin grants are never cached by admin_status.
    user = getattr(getattr(event, "new_chat_member", None), "user", None)
    if user is not None:
        invalidate_admin_status_cache(event.chat.id, user.id)
        unmark_privileged_operator(user.id, group_id=event.chat.id)


async def _start_join_verification(
    event: ChatMemberUpdated,
    session: AsyncSession,
    settings: Settings,
    *,
    user_id: int,
    display_name: str,
    provider: str,
    session_factory: async_sessionmaker[AsyncSession] | None = None,
    require_current_membership: bool = False,
) -> None:
    """Durably prepare, mute, prompt, then activate a join challenge.

    The short-lived ``preparing`` lease is committed before the first Telegram
    side effect.  A crash after muting therefore leaves recoverable state for
    the sweeper instead of a permanently restricted member with no challenge.
    """
    group_id = event.chat.id
    # This handler awaited network calls (bio fetch, screening LLM) since its
    # last record check; a raid retro sweep may have issued a challenge for
    # this member meanwhile. Upserting kind="join" would clobber it and break
    # its shared challenge button, so the existing record wins. Commit first:
    # the raid record was written by another session and a stale SQLite
    # snapshot from before those awaits would hide it.
    await session.commit()
    existing = await get_join_verification(session, group_id, user_id)
    if existing is not None:
        existing_status = str(existing.status or VERIFICATION_STATUS_PENDING)
        if (
            existing.kind != VERIFICATION_KIND_JOIN
            or existing_status != VERIFICATION_STATUS_PENDING
        ):
            log.info(
                "join verification skipped | reason=existing_%s_%s group=%s user=%s",
                existing.kind,
                existing_status,
                group_id,
                user_id,
            )
            return

        # A duplicate/rejoin already has a durable pending work item. Keep that
        # generation authoritative until the replacement prompt is successfully
        # sent and CAS-refreshed. Setup failure therefore preserves the old
        # challenge instead of turning it into an orphaned mute.
        old_prompt_message_id = int(existing.prompt_message_id or 0)
        old_deadline = existing.deadline_at
        existing_id = int(existing.id)
        deadline = now_shanghai_naive() + timedelta(
            seconds=settings.join_verification_timeout_seconds
        )
        prompt_message_id = 0
        refreshed = False

        async def _commit_refresh() -> bool:
            won = await refresh_pending_join_verification(
                session,
                verification_id=existing_id,
                deadline_at=old_deadline,
                kind=VERIFICATION_KIND_JOIN,
                new_deadline_at=deadline,
                prompt_message_id=prompt_message_id,
                provider=provider,
                display_name=display_name,
            )
            if not won:
                await session.rollback()
                return False
            await session.commit()
            return True

        async def _cleanup_failed_refresh() -> None:
            await session.rollback()
            recovery = await prepare_join_verification(
                session,
                group_id=group_id,
                user_id=user_id,
                deadline_at=deadline,
                display_name=display_name,
                prompt_message_id=prompt_message_id,
                provider=provider,
            )
            if recovery is not None:
                # The old generation disappeared while this refresh was in
                # flight. Own the now-empty unique key before restoring so a
                # concurrent new challenge cannot be accidentally unmuted.
                await session.commit()
                await shield_abort_prepared_join_verification(
                    event.bot,
                    session,
                    prepared=recovery,
                    prompt_message_id=prompt_message_id,
                )
            else:
                await session.rollback()
            if prompt_message_id and recovery is None:
                await delete_verification_prompt(
                    event.bot,
                    group_id,
                    prompt_message_id,
                )
            if recovery is None:
                await _reconcile_stale_restriction(
                    event,
                    session,
                    user_id=user_id,
                    session_factory=session_factory,
                )

        # ``existing`` has now been reduced to immutable ids/deadlines. End
        # the duplicate-record read transaction before restricting the member
        # and sending a replacement prompt; the CAS refresh below opens a new
        # short transaction only after those Telegram calls finish.
        await session.commit()
        try:
            if require_current_membership and not await _join_member_still_present(
                event,
                user_id,
                stage="duplicate_challenge_restrict",
            ):
                return
            if not await restrict_new_member(event.bot, group_id, user_id):
                return
            current = await get_join_verification(session, group_id, user_id)
            current_owned = bool(
                current is not None
                and int(current.id) == existing_id
                and str(current.status or VERIFICATION_STATUS_PENDING)
                == VERIFICATION_STATUS_PENDING
                and current.deadline_at == old_deadline
                and current.kind == VERIFICATION_KIND_JOIN
            )
            await session.commit()
            if not current_owned:
                await _reconcile_stale_restriction(
                    event,
                    session,
                    user_id=user_id,
                    session_factory=session_factory,
                )
                return
            sent = await event.bot.send_message(
                group_id,
                build_group_prompt_text(
                    user_id=user_id,
                    display_name=display_name,
                    timeout_seconds=settings.join_verification_timeout_seconds,
                ),
                parse_mode="HTML",
                reply_markup=build_group_prompt_keyboard(user_id),
            )
            prompt_message_id = int(getattr(sent, "message_id", 0) or 0)
            refresh_task = asyncio.create_task(
                _commit_refresh(),
                name=f"join-verification-refresh:{group_id}:{user_id}",
            )
            try:
                refreshed = await asyncio.shield(refresh_task)
            except asyncio.CancelledError:
                try:
                    refreshed = await refresh_task
                except Exception:
                    refreshed = False
                    log.exception(
                        "join verification refresh failed while cancellation was pending | "
                        "group=%s user=%s",
                        group_id,
                        user_id,
                    )
                raise
            if not refreshed:
                await _cleanup_failed_refresh()
                return
        except asyncio.CancelledError:
            if not refreshed:
                await asyncio.shield(_cleanup_failed_refresh())
            raise
        except Exception:
            log.exception(
                "join verification refresh failed | group=%s user=%s",
                group_id,
                user_id,
            )
            if not refreshed:
                await asyncio.shield(_cleanup_failed_refresh())
            return
        if old_prompt_message_id and old_prompt_message_id != prompt_message_id:
            await delete_verification_prompt(
                event.bot,
                group_id,
                old_prompt_message_id,
            )
        log.info("join verification refreshed | group=%s user=%s", group_id, user_id)
        return

    old_prompt_message_id = 0
    deadline = now_shanghai_naive() + timedelta(
        seconds=settings.join_verification_timeout_seconds
    )
    prepared = await prepare_join_verification(
        session,
        group_id=group_id,
        user_id=user_id,
        deadline_at=deadline,
        display_name=display_name,
        prompt_message_id=old_prompt_message_id,
        provider=provider,
    )
    if prepared is None:
        await session.rollback()
        return
    # The recovery lease must be durable before the first Telegram side effect.
    await session.commit()

    text = build_group_prompt_text(
        user_id=user_id,
        display_name=display_name,
        timeout_seconds=settings.join_verification_timeout_seconds,
    )
    prompt_message_id = 0
    activated = False
    try:
        if require_current_membership and not await _join_member_still_present(
            event,
            user_id,
            stage="challenge_prepare",
        ):
            await shield_abort_prepared_join_verification(
                event.bot,
                session,
                prepared=prepared,
                restore_permissions=False,
            )
            return
        renewed = await renew_prepared_join_verification(session, prepared=prepared)
        if renewed is None:
            await session.rollback()
            return
        await session.commit()
        prepared = renewed
        if not await restrict_new_member(event.bot, group_id, user_id):
            await shield_abort_prepared_join_verification(
                event.bot,
                session,
                prepared=prepared,
            )
            return

        renewed = await renew_prepared_join_verification(session, prepared=prepared)
        if renewed is None:
            await session.rollback()
            await _reconcile_stale_restriction(
                event,
                session,
                user_id=user_id,
                session_factory=session_factory,
            )
            return
        await session.commit()
        prepared = renewed
        if require_current_membership and not await _join_member_still_present(
            event,
            user_id,
            stage="challenge_prompt",
        ):
            compensated = await shield_abort_prepared_join_verification(
                event.bot,
                session,
                prepared=prepared,
            )
            if not compensated:
                await _reconcile_stale_restriction(
                    event,
                    session,
                    user_id=user_id,
                    session_factory=session_factory,
                )
            return

        sent = await event.bot.send_message(
            group_id,
            text,
            parse_mode="HTML",
            reply_markup=build_group_prompt_keyboard(user_id),
        )
        prompt_message_id = int(getattr(sent, "message_id", 0) or 0)

        activation_task = asyncio.create_task(
            commit_prepared_join_verification(
                session,
                prepared=prepared,
                prompt_message_id=prompt_message_id,
                deadline_at=deadline,
            ),
            name=f"join-verification-activate:{group_id}:{user_id}",
        )
        try:
            activated = await asyncio.shield(activation_task)
        except asyncio.CancelledError:
            try:
                activated = await activation_task
            except Exception:
                activated = False
                log.exception(
                    "join verification activation failed while cancellation was pending | "
                    "group=%s user=%s",
                    group_id,
                    user_id,
                )
            raise
        if not activated:
            compensated = await shield_abort_prepared_join_verification(
                event.bot,
                session,
                prepared=prepared,
                prompt_message_id=prompt_message_id,
            )
            if not compensated:
                await _reconcile_stale_restriction(
                    event,
                    session,
                    user_id=user_id,
                    session_factory=session_factory,
                )
            return
    except asyncio.CancelledError:
        if not activated:
            compensated = await shield_abort_prepared_join_verification(
                event.bot,
                session,
                prepared=prepared,
                prompt_message_id=prompt_message_id,
            )
            if not compensated:
                await _reconcile_stale_restriction(
                    event,
                    session,
                    user_id=user_id,
                    session_factory=session_factory,
                )
        raise
    except Exception:
        log.exception(
            "join verification setup failed | group=%s user=%s",
            group_id,
            user_id,
        )
        if not activated:
            compensated = await shield_abort_prepared_join_verification(
                event.bot,
                session,
                prepared=prepared,
                prompt_message_id=prompt_message_id,
            )
            if not compensated:
                await _reconcile_stale_restriction(
                    event,
                    session,
                    user_id=user_id,
                    session_factory=session_factory,
                )
        return
    if old_prompt_message_id and old_prompt_message_id != prompt_message_id:
        await delete_verification_prompt(
            event.bot,
            group_id,
            old_prompt_message_id,
        )
    log.info("join verification issued | group=%s user=%s", group_id, user_id)


async def _enforce_pending_moderation_challenge(
    event: ChatMemberUpdated,
    session: AsyncSession,
    settings: Settings,
    record: JoinVerification,
    *,
    display_name: str,
) -> None:
    """Keep an unresolved message/patrol/raid challenge intact across leave/rejoin.

    Moderation challenges ban on expiry; patrol and raid challenges kick
    without banning, matching the sweeper's consequences for each kind.
    """
    now = now_shanghai_naive()
    expired = verification_deadline_passed(record.deadline_at, now=now)
    snapshot = _verification_snapshot(record)
    if is_super_admin_user_id(record.user_id, settings):
        lease_until = await _lease_terminal_verification(
            session,
            record,
            now=now,
            expired=expired,
            target_status=VERIFICATION_STATUS_RELEASING,
        )
        if lease_until is None:
            return
        restored = await restore_member_permissions(
            event.bot,
            record.group_id,
            record.user_id,
        )
        if not restored:
            return
        await _complete_terminal_verification(
            session,
            verification_id=int(record.id),
            lease_until=lease_until,
            status=VERIFICATION_STATUS_RELEASING,
        )
        return

    is_patrol = record.kind in (VERIFICATION_KIND_PATROL, VERIFICATION_KIND_RAID)
    if expired:
        lease_until = await _lease_terminal_verification(
            session,
            now=now,
            record=record,
            expired=True,
            target_status=VERIFICATION_STATUS_ENFORCING,
        )
        if lease_until is None:
            return
        if is_patrol:
            async def preserve_ban() -> bool:
                blocked = await verification_release_blocked_by_ban(
                    session,
                    group_id=int(record.group_id),
                    user_id=int(record.user_id),
                )
                await session.commit()
                return blocked

            enforced = await kick_member(
                event.bot,
                record.group_id,
                record.user_id,
                preserve_ban=preserve_ban,
            )
            if enforced:
                await _complete_terminal_verification(
                    session,
                    verification_id=int(record.id),
                    lease_until=lease_until,
                    status=VERIFICATION_STATUS_ENFORCING,
                )
            else:
                requeued = await _requeue_verification(
                    session,
                    settings,
                    snapshot,
                    verification_id=int(record.id),
                    lease_until=lease_until,
                    status=VERIFICATION_STATUS_ENFORCING,
                )
                if requeued:
                    await restrict_new_member(
                        event.bot,
                        record.group_id,
                        record.user_id,
                    )
            return
        await mark_group_banned(
            session,
            record.group_id,
            record.user_id,
        )
        await session.commit()

        async def preserve_timeout_ban() -> bool:
            await session.rollback()
            blocked = await verification_release_blocked_by_ban(
                session,
                group_id=int(record.group_id),
                user_id=int(record.user_id),
            )
            await session.commit()
            return blocked

        async def timeout_restriction_required() -> bool:
            await session.rollback()
            required = await verification_restriction_required(
                session,
                group_id=int(record.group_id),
                user_id=int(record.user_id),
            )
            await session.commit()
            return required

        enforced = await _ban_and_notify(
            event,
            settings,
            user_id=record.user_id,
            display_name=display_name,
            reason="消息审查真人验证超时",
            preserve_ban=preserve_timeout_ban,
            restriction_required=timeout_restriction_required,
        )
        if not enforced:
            deferred = await _defer_terminal_verification(
                session,
                verification_id=int(record.id),
                lease_until=lease_until,
                status=VERIFICATION_STATUS_ENFORCING,
            )
            log.warning(
                "moderation timeout ban unconfirmed; durable ban/enforcement retained | "
                "group=%s user=%s deferred=%s",
                record.group_id,
                record.user_id,
                deferred,
            )
            return
        await _complete_moderation_enforcement_or_reconcile(
            event.bot,
            session,
            group_id=int(record.group_id),
            user_id=int(record.user_id),
            verification_id=int(record.id),
            lease_until=lease_until,
        )
        return

    # End the read transaction before the Telegram call. A concurrent web
    # verification can then delete the row and win without being hidden by a
    # stale SQLite snapshot.
    await session.commit()
    restricted = await restrict_new_member(event.bot, record.group_id, record.user_id)
    if not restricted:
        return
    current = await get_join_verification(session, record.group_id, record.user_id)
    if current is None:
        recovery = await prepare_join_verification(
            session,
            group_id=record.group_id,
            user_id=record.user_id,
            deadline_at=record.deadline_at,
            kind=record.kind,
            provider=record.provider,
            reason=record.reason,
            display_name=display_name or record.display_name,
            prompt_message_id=0,
        )
        if recovery is not None:
            await session.commit()
            await shield_abort_prepared_join_verification(
                event.bot,
                session,
                prepared=recovery,
            )
        else:
            await session.rollback()
        return
    if (
        int(current.id) != int(record.id)
        or current.kind != record.kind
        or str(current.status or VERIFICATION_STATUS_PENDING)
        != VERIFICATION_STATUS_PENDING
    ):
        # A newer challenge now owns this member's permissions.
        await session.commit()
        await _reconcile_stale_restriction(
            event,
            session,
            user_id=int(record.user_id),
            session_factory=None,
        )
        return
    log.info(
        "%s challenge re-enforced after rejoin | group=%s user=%s",
        record.kind,
        record.group_id,
        record.user_id,
    )


def _verification_snapshot(record: JoinVerification) -> dict[str, object]:
    return {
        "group_id": int(record.group_id),
        "user_id": int(record.user_id),
        "kind": str(record.kind),
        "provider": str(record.provider),
        "reason": str(record.reason or ""),
        "display_name": str(record.display_name or ""),
        "prompt_message_id": int(record.prompt_message_id or 0),
    }


def _verification_retry_deadline(
    settings: Settings,
    kind: str,
):
    return now_shanghai_naive() + timedelta(
        seconds=verification_timeout_seconds_for_kind(settings, kind)
    )


async def _requeue_verification(
    session: AsyncSession,
    settings: Settings,
    snapshot: dict[str, object],
    *,
    verification_id: int,
    lease_until: datetime,
    status: str,
) -> bool:
    if status != VERIFICATION_STATUS_ENFORCING:
        raise ValueError("only punitive enforcement leases may be deferred here")
    del settings, snapshot
    return await _defer_terminal_verification(
        session,
        verification_id=verification_id,
        lease_until=lease_until,
        status=status,
    )


async def _defer_terminal_verification(
    session: AsyncSession,
    *,
    verification_id: int,
    lease_until: datetime,
    status: str,
) -> bool:
    retry_lease = now_shanghai_naive() + timedelta(seconds=TERMINAL_LEASE_SECONDS)
    renewed = await renew_join_verification_lease(
        session,
        verification_id=verification_id,
        lease_until=lease_until,
        new_lease_until=retry_lease,
        status=status,
    )
    if not renewed:
        await session.rollback()
        return False
    await session.commit()
    return True


async def _lease_terminal_verification(
    session: AsyncSession,
    record: JoinVerification,
    *,
    now: datetime,
    expired: bool,
    target_status: str,
) -> datetime | None:
    """Own one exact verification generation before a Telegram side effect."""
    lease_until = now + timedelta(seconds=TERMINAL_LEASE_SECONDS)
    current_status = str(record.status or VERIFICATION_STATUS_PENDING)
    if current_status == VERIFICATION_STATUS_PENDING:
        won = await claim_join_verification(
            session,
            verification_id=int(record.id),
            deadline_at=record.deadline_at,
            kind=record.kind,
            now=now,
            expired=expired,
            lease_until=lease_until,
            target_status=target_status,
        )
    elif (
        current_status
        in {VERIFICATION_STATUS_ENFORCING, VERIFICATION_STATUS_RELEASING}
        and record.lease_until is not None
        and record.lease_until <= now
    ):
        won = await lease_expired_join_verification(
            session,
            record=record,
            now=now,
            lease_until=lease_until,
            target_status=target_status,
        )
    else:
        won = False
    if not won:
        await session.rollback()
        return None
    await session.commit()
    return lease_until


async def _complete_terminal_verification(
    session: AsyncSession,
    *,
    verification_id: int,
    lease_until: datetime,
    status: str,
) -> bool:
    completed = await complete_leased_join_verification(
        session,
        verification_id=verification_id,
        lease_until=lease_until,
        status=status,
    )
    if not completed:
        await session.rollback()
        return False
    await session.commit()
    return True


async def _complete_moderation_enforcement_or_reconcile(
    bot: object,
    session: AsyncSession,
    *,
    group_id: int,
    user_id: int,
    verification_id: int,
    lease_until: datetime,
) -> bool:
    """Complete an exact moderation generation or undo its stale remote ban."""

    async def preserve_ban() -> bool:
        # Completion loss normally rolls back itself. A cancellation/DB error
        # may leave an aborted transaction, so normalize it before every
        # authoritative policy read used by the Telegram cleanup retry loop.
        await session.rollback()
        blocked = await verification_release_blocked_by_ban(
            session,
            group_id=group_id,
            user_id=user_id,
        )
        await session.commit()
        return blocked

    async def reconcile() -> bool:
        return await reconcile_moderation_ban_after_lost_lease(
            bot,
            group_id,
            user_id,
            preserve_ban,
        )

    try:
        completed = await _complete_terminal_verification(
            session,
            verification_id=verification_id,
            lease_until=lease_until,
            status=VERIFICATION_STATUS_ENFORCING,
        )
    except asyncio.CancelledError:
        await reconcile()
        raise
    except Exception:
        await reconcile()
        raise
    if not completed:
        await reconcile()
    return completed


async def _verification_callback_record(
    callback: CallbackQuery,
    session: AsyncSession,
    target_user_id: int,
) -> JoinVerification | None:
    message = callback.message
    chat = getattr(message, "chat", None)
    if message is None or chat is None or chat.type not in ("group", "supergroup"):
        await _ack_security_callback(callback, "验证消息已失效", show_alert=True)
        return None

    record = await get_join_verification(session, int(chat.id), target_user_id)
    message_id = int(getattr(message, "message_id", 0) or 0)
    if (
        record is None
        or str(record.status or VERIFICATION_STATUS_PENDING)
        != VERIFICATION_STATUS_PENDING
        or int(record.prompt_message_id or 0) != message_id
        or verification_deadline_passed(record.deadline_at)
    ):
        # Clean up the stale keyboard immediately. This covers old prompts
        # left by a duplicate join, manual leave, or a transient terminal-edit
        # failure and prevents repeated "expired" clicks.
        await session.commit()
        await delete_verification_prompt(callback.bot, int(chat.id), message_id)
        await _ack_security_callback(
            callback,
            "验证已失效、过期或已处理",
            show_alert=True,
        )
        return None
    # Callers only need the immutable-in-practice challenge snapshot. Release
    # the SELECT transaction before bot.me(), callback.answer(), permission
    # restoration, or any other Telegram operation.
    await session.commit()
    return record


async def _edit_verification_prompt(
    callback: CallbackQuery,
    settings: Settings,
    *,
    text: str,
) -> None:
    message = callback.message
    chat = getattr(message, "chat", None)
    if message is None or chat is None:
        return
    try:
        edited = await callback.bot.edit_message_text(
            chat_id=chat.id,
            message_id=message.message_id,
            text=text,
            parse_mode="HTML",
            reply_markup=None,
        )
        # The prompt is now a moderation outcome notice ("审核通知"): honor the
        # group's auto-delete retention like the other verification outcomes.
        await schedule_message_auto_delete_durable(
            edited if not isinstance(edited, bool) else None,
            configured_auto_delete_seconds(settings, "moderation"),
        )
    except Exception:
        log.debug(
            "verification admin prompt edit failed | group=%s message=%s",
            chat.id,
            message.message_id,
            exc_info=True,
        )
        await delete_verification_prompt(
            callback.bot,
            int(chat.id),
            int(message.message_id),
        )


async def _callback_bot_username(callback: CallbackQuery) -> str:
    username = get_bot_identity().username
    if username:
        return username
    try:
        me = await callback.bot.me()
    except Exception:
        return ""
    return str(getattr(me, "username", "") or "").strip().lstrip("@")


async def _handle_verification_start_callback(
    callback: CallbackQuery,
    session: AsyncSession,
    target_user_id: int,
) -> None:
    operator = callback.from_user
    if operator is None or int(operator.id) != target_user_id:
        await callback.answer("仅受验证用户本人可点击", show_alert=True)
        return

    record = await _verification_callback_record(callback, session, target_user_id)
    if record is None:
        return
    username = await _callback_bot_username(callback)
    if not username:
        await callback.answer("验证入口暂时不可用，请稍后重试", show_alert=True)
        return
    await callback.answer(
        url=build_private_deep_link(username, int(record.group_id)),
    )


async def _handle_verification_admin_callback(
    callback: CallbackQuery,
    session: AsyncSession,
    settings: Settings,
    *,
    action: str,
    target_user_id: int,
    session_factory: async_sessionmaker[AsyncSession] | None = None,
) -> None:
    message = callback.message
    chat = getattr(message, "chat", None)
    operator = callback.from_user
    if message is None or chat is None or chat.type not in ("group", "supergroup"):
        await _ack_security_callback(callback, "验证消息已失效", show_alert=True)
        return
    if operator is None:
        await _ack_security_callback(callback, "无法识别操作者", show_alert=True)
        return
    group_id = int(chat.id)
    operator_id = int(operator.id)
    authorized = await is_group_authorized(session, group_id)
    await session.commit()
    if not authorized:
        await _ack_security_callback(callback, "当前群组未授权", show_alert=True)
        return
    if not await is_group_admin_or_higher(
        bot=callback.bot,
        session=session,
        settings=settings,
        group_id=group_id,
        user_id=operator_id,
    ):
        await _ack_security_callback(
            callback,
            "仅群管理员及以上权限可操作",
            show_alert=True,
        )
        return

    record = await _verification_callback_record(callback, session, target_user_id)
    if record is None:
        return
    if action == VERIFICATION_CALLBACK_REJECT and is_super_admin_user_id(
        target_user_id, settings
    ):
        await _ack_security_callback(callback, "不能封禁最高管理员", show_alert=True)
        return
    if action == VERIFICATION_CALLBACK_APPROVE:
        locally_banned = bool(
            await session.scalar(
                select(UserWarning.id).where(
                    UserWarning.group_id == group_id,
                    UserWarning.user_id == target_user_id,
                    UserWarning.is_banned.is_(True),
                )
            )
        )
        globally_banned = await is_globally_banned(session, target_user_id)
        await session.commit()
        if locally_banned or globally_banned:
            await _ack_security_callback(
                callback,
                "该用户已被封禁，请先解封后再通过",
                show_alert=True,
            )
            return

    snapshot = _verification_snapshot(record)
    terminal_status = (
        VERIFICATION_STATUS_RELEASING
        if action == VERIFICATION_CALLBACK_APPROVE
        else VERIFICATION_STATUS_ENFORCING
    )
    lease_until = await _lease_terminal_verification(
        session,
        now=now_shanghai_naive(),
        record=record,
        expired=False,
        target_status=terminal_status,
    )
    if lease_until is None:
        await _ack_security_callback(
            callback,
            "验证已由其他操作处理",
            show_alert=True,
        )
        return

    kind = str(snapshot["kind"])
    shown = html.escape(str(snapshot["display_name"] or target_user_id))
    lease_is_current = await join_verification_lease_is_current(
        session,
        verification_id=int(record.id),
        lease_until=lease_until,
        status=terminal_status,
    )
    await session.commit()
    if not lease_is_current:
        await _ack_security_callback(
            callback,
            "验证状态已被更高优先级的权限操作更新",
            show_alert=True,
        )
        return
    if action == VERIFICATION_CALLBACK_APPROVE:
        restored = await restore_member_permissions(callback.bot, group_id, target_user_id)
        if not restored:
            deferred = await _defer_terminal_verification(
                session,
                verification_id=int(record.id),
                lease_until=lease_until,
                status=VERIFICATION_STATUS_RELEASING,
            )
            await _ack_security_callback(
                callback,
                "权限恢复失败，后台将继续重试放行"
                if deferred
                else "权限恢复失败，恢复工单已由后台接管",
                show_alert=True,
            )
            return
        if not await _complete_terminal_verification(
            session,
            verification_id=int(record.id),
            lease_until=lease_until,
            status=VERIFICATION_STATUS_RELEASING,
        ):
            await _reconcile_stale_restriction(
                SimpleNamespace(bot=callback.bot, chat=chat),
                session,
                user_id=target_user_id,
                session_factory=session_factory,
            )
            await _ack_security_callback(
                callback,
                "权限已按最新策略校准，验证状态由后台继续确认",
                show_alert=True,
            )
            return
        approved_text = (
            f"✅ <b>{shown}</b> 已由管理员直接通过消息审查验证，发言权限已恢复。"
            if kind == VERIFICATION_KIND_MODERATION
            else f"✅ <b>{shown}</b> 已由管理员直接通过入群验证，欢迎加入！"
        )
        await _edit_verification_prompt(callback, settings, text=approved_text)
        await _ack_security_callback(callback, "已直接通过验证")
        if kind == VERIFICATION_KIND_JOIN:
            await send_group_welcome(
                callback.bot,
                session,
                settings,
                group_id=group_id,
                user_id=target_user_id,
                display_name=str(snapshot["display_name"] or ""),
            )
        return

    ban_state = None
    if kind == VERIFICATION_KIND_MODERATION:
        ban_state = await mark_group_banned(session, group_id, target_user_id)
    await session.commit()

    if kind == VERIFICATION_KIND_MODERATION:
        enforced = await ban_member(callback.bot, group_id, target_user_id)
        if not enforced:
            rolled_back = await rollback_group_ban(
                session,
                group_id,
                target_user_id,
                ban_state,
            )
            await session.commit()
            requeued = bool(
                rolled_back
                and not (ban_state and ban_state[1])
                and await _requeue_verification(
                    session,
                    settings,
                    snapshot,
                    verification_id=int(record.id),
                    lease_until=lease_until,
                    status=VERIFICATION_STATUS_ENFORCING,
                )
            )
            log.warning(
                "moderation verification admin ban failed | group=%s user=%s "
                "state_restored=%s",
                group_id,
                target_user_id,
                rolled_back,
            )
            await _ack_security_callback(
                callback,
                "封禁失败，验证已保留待处理"
                if requeued
                else "Telegram 封禁失败，群内状态已变化，请人工检查",
                show_alert=True,
            )
            return
        rejected_text = (
            f"🚫 <b>{shown}</b> 的消息审查验证已被管理员拒绝，已在当前群封禁。"
        )
    else:
        async def preserve_ban() -> bool:
            blocked = await verification_release_blocked_by_ban(
                session,
                group_id=group_id,
                user_id=target_user_id,
            )
            await session.commit()
            return blocked

        enforced = await kick_member(
            callback.bot,
            group_id,
            target_user_id,
            preserve_ban=preserve_ban,
        )
        if not enforced:
            requeued = await _requeue_verification(
                session,
                settings,
                snapshot,
                verification_id=int(record.id),
                lease_until=lease_until,
                status=VERIFICATION_STATUS_ENFORCING,
            )
            await _ack_security_callback(
                callback,
                "移出群聊失败，验证已保留待处理"
                if requeued
                else "移出群聊失败，执行工单已由后台接管",
                show_alert=True,
            )
            return
        rejected_text = (
            f"❌ <b>{shown}</b> 的入群验证已被管理员拒绝，已移出群聊。"
        )
    if kind == VERIFICATION_KIND_MODERATION:
        completed = await _complete_moderation_enforcement_or_reconcile(
            callback.bot,
            session,
            group_id=group_id,
            user_id=target_user_id,
            verification_id=int(record.id),
            lease_until=lease_until,
        )
    else:
        completed = await _complete_terminal_verification(
            session,
            verification_id=int(record.id),
            lease_until=lease_until,
            status=VERIFICATION_STATUS_ENFORCING,
        )
    if not completed:
        if kind != VERIFICATION_KIND_MODERATION:
            await _reconcile_stale_restriction(
                SimpleNamespace(bot=callback.bot, chat=chat),
                session,
                user_id=target_user_id,
                session_factory=session_factory,
            )
        await _ack_security_callback(
            callback,
            "操作已执行，验证状态由后台继续确认",
            show_alert=True,
        )
        return
    await _edit_verification_prompt(callback, settings, text=rejected_text)
    await _ack_security_callback(callback, "已直接拒绝验证")


@router.callback_query(F.data.startswith(f"{VERIFICATION_CALLBACK_PREFIX}:"))
async def on_verification_callback(
    callback: CallbackQuery,
    session: AsyncSession,
    settings: Settings,
    session_factory: async_sessionmaker[AsyncSession] | None = None,
) -> None:
    parsed = parse_verification_callback_data(callback.data or "")
    if parsed is None:
        await callback.answer("验证按钮参数错误", show_alert=True)
        return
    action, target_user_id = parsed
    if action == VERIFICATION_CALLBACK_START:
        await _handle_verification_start_callback(callback, session, target_user_id)
        return
    if session_factory is None:
        await _handle_verification_admin_callback(
            callback,
            session,
            settings,
            action=action,
            target_user_id=target_user_id,
        )
        return

    # Release Telegram's spinner and the reserved update worker before any
    # lease transition, permission restore, kick, or ban. Authorization itself
    # stays in the HIGH admission lane: untrusted callback data must never be
    # able to allocate a CRITICAL background job.
    await _ack_security_callback(callback, "正在验证权限并执行…")
    message = callback.message
    chat = getattr(message, "chat", None)
    operator = callback.from_user
    message_id = int(getattr(message, "message_id", 0) or 0)
    group_id = int(getattr(chat, "id", 0) or 0)
    if (
        message is None
        or chat is None
        or getattr(chat, "type", "") not in {"group", "supergroup"}
        or operator is None
        or group_id == 0
    ):
        await session.commit()
        await _publish_security_callback_result(callback, "验证操作消息已失效")
        return
    if not await is_group_authorized(session, group_id):
        await session.commit()
        await _publish_security_callback_result(callback, "当前群组未授权")
        return
    await session.commit()
    if not await is_group_admin_or_higher(
        bot=callback.bot,
        session=session,
        settings=settings,
        group_id=group_id,
        user_id=int(operator.id),
    ):
        await _publish_security_callback_result(
            callback,
            "仅群管理员及以上权限可操作",
        )
        return
    in_transaction = getattr(session, "in_transaction", None)
    if callable(in_transaction) and in_transaction():
        await session.commit()

    async def operation() -> None:
        async with session_factory() as work_session:
            await _handle_verification_admin_callback(
                callback,
                work_session,
                settings,
                action=action,
                target_user_id=target_user_id,
                session_factory=session_factory,
            )

    submission = submit_privileged_task(
        # Approve/reject are mutually exclusive mutations of one generation;
        # omit the action so two buttons cannot execute concurrently.
        key=f"verification-admin:{group_id}:{message_id}:{target_user_id}",
        label=(
            f"verification admin {action} for {target_user_id} "
            f"in {group_id} prompt {message_id}"
        ),
        operation=operation,
        lane="critical",
        priority=0,
        timeout_seconds=120.0,
    )
    if submission.accepted:
        if not submission.created and message is not None and hasattr(message, "answer"):
            try:
                await message.answer("该验证权限操作正在执行，未重复提交。")
            except Exception:
                pass
        return

    # Saturation must not silently drop a permission decision, but it also must
    # not move a minutes-long Telegram operation back into the HIGH update
    # worker. Keep the durable inbox row retryable instead.
    log.error(
        "verification admin queue rejected task; scheduling durable retry | "
        "group=%s user=%s reason=%s",
        group_id,
        target_user_id,
        submission.reason,
    )
    request_current_update_retry()
    await _publish_security_callback_result(
        callback,
        "权限任务队列正忙，本次操作会自动重试。",
    )


async def _handle_shared_challenge_callback(
    callback: CallbackQuery,
    session: AsyncSession,
    *,
    kind: str,
) -> None:
    """Shared-button challenge prompts: only mentioned members may use them.

    The callback data carries no user id (many members share one message),
    so authorization is the existence of the clicker's own pending record of
    the matching kind in this group.
    """
    message = callback.message
    chat = getattr(message, "chat", None)
    operator = callback.from_user
    if message is None or chat is None or chat.type not in ("group", "supergroup"):
        await callback.answer("质询入口已失效", show_alert=True)
        return
    if operator is None:
        await callback.answer("无法识别操作者", show_alert=True)
        return

    record = await get_join_verification(session, int(chat.id), int(operator.id))
    if (
        record is None
        or str(record.status or VERIFICATION_STATUS_PENDING)
        != VERIFICATION_STATUS_PENDING
        or record.kind != kind
        or verification_deadline_passed(record.deadline_at)
    ):
        await session.commit()
        await callback.answer("仅被点名的违规成员可点击", show_alert=True)
        return

    await session.commit()
    username = await _callback_bot_username(callback)
    if not username:
        await callback.answer("质询入口暂时不可用，请稍后重试", show_alert=True)
        return
    await callback.answer(
        url=build_private_deep_link(username, int(record.group_id)),
    )


@router.callback_query(F.data == PATROL_VERIFY_CALLBACK_DATA)
async def on_patrol_verify_callback(
    callback: CallbackQuery,
    session: AsyncSession,
    settings: Settings,
) -> None:
    await _handle_shared_challenge_callback(
        callback, session, kind=VERIFICATION_KIND_PATROL
    )


@router.callback_query(F.data == RAID_VERIFY_CALLBACK_DATA)
async def on_raid_verify_callback(
    callback: CallbackQuery,
    session: AsyncSession,
    settings: Settings,
) -> None:
    await _handle_shared_challenge_callback(
        callback, session, kind=VERIFICATION_KIND_RAID
    )


@router.callback_query(F.data == RAID_REMOVE_CALLBACK_DATA)
async def on_raid_remove_callback(
    callback: CallbackQuery,
    session: AsyncSession,
    settings: Settings,
    session_factory: async_sessionmaker[AsyncSession] | None = None,
) -> None:
    """Administrator-only bulk removal for one raid challenge message."""
    message = callback.message
    chat = getattr(message, "chat", None)
    operator = callback.from_user
    if message is None or chat is None or chat.type not in ("group", "supergroup"):
        await callback.answer("质询消息已失效", show_alert=True)
        return
    if operator is None:
        await callback.answer("无法识别操作者", show_alert=True)
        return
    # Stop Telegram's callback spinner before authorization or bulk work. The
    # acknowledgement discloses no privileged result and is safe for any user.
    await _ack_security_callback(callback, "正在验证权限并提交任务…")
    group_id = int(chat.id)
    authorized = await is_group_authorized(session, group_id)
    await session.commit()
    if not authorized:
        await _ack_security_callback(callback, "当前群组未授权", show_alert=True)
        try:
            await message.answer("当前群组未授权，未执行批量移除。")
        except Exception:
            pass
        return
    if not await is_group_admin_or_higher(
        bot=callback.bot,
        session=session,
        settings=settings,
        group_id=group_id,
        user_id=int(operator.id),
    ):
        await _ack_security_callback(
            callback,
            "仅群管理员可一键移除追溯用户",
            show_alert=True,
        )
        try:
            await message.answer("仅群管理员可一键移除追溯用户。")
        except Exception:
            pass
        return

    await session.commit()
    if session_factory is not None:
        prompt_message_id = int(message.message_id)

        async def operation() -> None:
            async with session_factory() as work_session:
                still_authorized = await is_group_authorized(work_session, group_id)
                await work_session.commit()
                still_operator = bool(
                    still_authorized
                    and await is_group_admin_or_higher(
                        bot=callback.bot,
                        session=work_session,
                        settings=settings,
                        group_id=group_id,
                        user_id=int(operator.id),
                    )
                )
                await work_session.commit()
                if not still_operator:
                    try:
                        await message.answer(
                            "爆破防护批量移除已取消：执行前复验发现群授权或操作者权限已失效。"
                        )
                    except Exception:
                        pass
                    return
                result = await remove_raid_challenged_users(
                    bot=callback.bot,
                    session=work_session,
                    session_factory=session_factory,
                    settings=settings,
                    group_id=group_id,
                    prompt_message_id=prompt_message_id,
                    group_settings=None,
                )
            await _publish_raid_removal_result(
                callback,
                group_id=group_id,
                prompt_message_id=prompt_message_id,
                result=result,
            )

        submission = submit_privileged_task(
            key=f"raid-remove:{group_id}:{prompt_message_id}",
            label=f"raid bulk remove in {group_id} prompt {prompt_message_id}",
            operation=operation,
            lane="critical_bulk",
            priority=10,
            timeout_seconds=180.0,
        )
        if not submission.accepted:
            try:
                await message.answer("权限任务队列正忙，本次未受理，请再次点击。")
            except Exception:
                pass
        elif not submission.created:
            try:
                await message.answer("该批移除任务正在执行，未重复提交。")
            except Exception:
                pass
        return

    result = await remove_raid_challenged_users(
        bot=callback.bot,
        session=session,
        settings=settings,
        group_id=group_id,
        prompt_message_id=int(message.message_id),
        group_settings=None,
    )
    # The service normally commits every lease/result transition. Keep this
    # callback boundary explicit as well so future no-op paths cannot leave a
    # final read snapshot checked out while editing/answering the Telegram UI.
    await session.commit()
    removed_count = len(result.removed_user_ids)
    failed_count = len(result.failed_user_ids)
    if failed_count == 0:
        try:
            await callback.bot.edit_message_reply_markup(
                chat_id=group_id,
                message_id=int(message.message_id),
                reply_markup=None,
            )
        except Exception:
            log.debug(
                "raid bulk-remove keyboard cleanup failed | group=%s message=%s",
                group_id,
                message.message_id,
                exc_info=True,
            )
    if result.pending_count == 0:
        await callback.answer("该批追溯用户已全部处理", show_alert=True)
    elif failed_count:
        await callback.answer(
            f"已移除 {removed_count} 人，{failed_count} 人移除失败，可稍后重试",
            show_alert=True,
        )
    else:
        await callback.answer(f"已移除 {removed_count} 名被追溯用户")


async def _process_member_join(
    event: ChatMemberUpdated,
    session: AsyncSession,
    settings: Settings,
    *,
    session_factory: async_sessionmaker[AsyncSession] | None = None,
    require_current_membership: bool = False,
) -> None:
    log.info(
        "member join event | group=%s user=%s",
        event.chat.id,
        getattr(getattr(event.new_chat_member, "user", None), "id", "-"),
    )
    if event.chat.type not in ("group", "supergroup"):
        return
    _invalidate_admin_cache(event)
    user = event.new_chat_member.user
    if user.is_bot:
        return
    if require_current_membership and not await _join_member_still_present(
        event,
        int(user.id),
        stage="start",
    ):
        return
    if not await is_group_authorized(session, event.chat.id):
        return
    try:
        await track_group_member(
            session,
            event.chat.id,
            user_id=user.id,
            full_name=user.full_name or "",
            username=user.username or "",
            is_bot=False,
        )
        # Commit now: the rest of this handler awaits network calls (bio
        # fetch, screening LLM) and must not hold the SQLite write lock.
        await session.commit()
    except Exception:
        log.debug("join roster tracking failed | group=%s user=%s", event.chat.id, user.id, exc_info=True)
    group_id = event.chat.id
    user_id = user.id

    if is_super_admin_user_id(user.id, settings):
        record = await get_join_verification(session, group_id, user_id)
        if record is not None:
            await _enforce_pending_moderation_challenge(
                event,
                session,
                settings,
                record,
                display_name=user.full_name or "",
            )
        return

    # Banned users are removed immediately on rejoin, no screening needed.
    globally_banned = await is_globally_banned(session, user_id)
    locally_banned = bool(
        await session.scalar(
            select(UserWarning.id).where(
                UserWarning.group_id == group_id,
                UserWarning.user_id == user_id,
                UserWarning.is_banned.is_(True),
            )
        )
    )
    # End the read transaction before any Telegram API call below.
    await session.commit()
    if globally_banned or locally_banned:
        async def current_ban_policy() -> bool:
            await session.rollback()
            blocked = await verification_release_blocked_by_ban(
                session,
                group_id=group_id,
                user_id=user_id,
            )
            await session.commit()
            return blocked

        async def current_restriction_required() -> bool:
            await session.rollback()
            required = await verification_restriction_required(
                session,
                group_id=group_id,
                user_id=user_id,
            )
            await session.commit()
            return required

        if not await current_ban_policy():
            log.info(
                "join ban snapshot discarded | reason=manual_unban "
                "group=%s user=%s",
                group_id,
                user_id,
            )
            return
        ban_scope = "global" if globally_banned else "local"
        log.info(
            "join blocked | reason=%s_ban group=%s user=%s",
            ban_scope,
            group_id,
            user_id,
        )
        enforced = await _ban_and_notify(
            event,
            settings,
            user_id=user_id,
            display_name=user.full_name,
            reason=(
                "该用户在全局封禁名单中"
                if globally_banned
                else "该用户在本群封禁名单中"
            ),
            preserve_ban=current_ban_policy,
            restriction_required=current_restriction_required,
        )
        return

    if manual_unban_generation_is_active(group_id, user_id):
        log.info(
            "join security stopped | reason=recent_manual_unban group=%s user=%s",
            group_id,
            user_id,
        )
        return

    raid_guard = get_raid_guard_service()
    if raid_guard is not None:
        group = await session.get(Group, group_id)
        group_settings = dict(group.settings or {}) if group else None
        # End the read transaction before the Telegram calls inside the
        # raid-guard path so concurrent writers are not blocked.
        await session.commit()
        consumed = await raid_guard.handle_join(
            group_id=group_id,
            user_id=user_id,
            full_name=user.full_name or "",
            username=user.username or "",
            group_settings=group_settings,
        )
        if consumed:
            return

    pending = await get_join_verification(session, group_id, user_id)
    pending_status = (
        str(pending.status or VERIFICATION_STATUS_PENDING)
        if pending is not None
        else ""
    )
    if pending is not None and pending_status in {
        VERIFICATION_STATUS_PREPARING,
        VERIFICATION_STATUS_RELEASING,
        VERIFICATION_STATUS_UNBANNING,
    }:
        # A concurrent setup owns this short lease.  It will either activate
        # the challenge or compensate; after a crash the sweeper restores the
        # member and removes the preparation.  Do not clobber it on a duplicate
        # join update or treat its not-yet-valid prompt as interactive.
        await session.commit()
        log.info(
            "join handling deferred | reason=verification_%s kind=%s "
            "group=%s user=%s",
            pending_status,
            pending.kind,
            group_id,
            user_id,
        )
        return
    if (
        pending is not None
        and pending_status
        in {VERIFICATION_STATUS_PENDING, VERIFICATION_STATUS_ENFORCING}
        and pending.kind
        in (
            VERIFICATION_KIND_MODERATION,
            VERIFICATION_KIND_PATROL,
            VERIFICATION_KIND_RAID,
        )
    ):
        await _enforce_pending_moderation_challenge(
            event,
            session,
            settings,
            pending,
            display_name=user.full_name or "",
        )
        return

    # Bio lookup and profile moderation are external network operations.  Do
    # not retain the connection used for the pending-challenge read.
    await session.commit()

    async def _maybe_start_verification() -> bool:
        if manual_unban_generation_is_active(group_id, user_id):
            return False
        await session.rollback()
        if not await is_group_authorized(session, group_id):
            await session.commit()
            return False
        group = await session.get(Group, group_id)
        group_settings = group.settings if group is not None else None
        enabled, provider = join_verification_policy(settings, group_settings)
        await session.commit()
        if require_current_membership and not await _join_member_still_present(
            event,
            user_id,
            stage="verification_policy",
        ):
            return False
        if enabled and join_verification_ready(settings, group_settings):
            await _start_join_verification(
                event,
                session,
                settings,
                user_id=user_id,
                display_name=user.full_name or "",
                provider=provider,
                session_factory=session_factory,
                require_current_membership=require_current_membership,
            )
            return True
        return False

    async def _admit_member() -> None:
        if require_current_membership and not await _join_member_still_present(
            event,
            user_id,
            stage="admission",
        ):
            return
        # Verification (when enabled) owns the admission moment: the welcome
        # is sent after the challenge passes instead of on the raw join.
        if await _maybe_start_verification():
            return
        await send_group_welcome(
            event.bot,
            session,
            settings,
            group_id=group_id,
            user_id=user_id,
            display_name=user.full_name or "",
        )

    if not settings.moderation.enabled:
        await _admit_member()
        return

    bio = await _fetch_user_bio(event, user_id)
    if require_current_membership and not await _join_member_still_present(
        event,
        user_id,
        stage="profile_screening",
    ):
        return
    profile_text = build_join_profile_text(
        full_name=user.full_name or "",
        username=user.username or "",
        bio=bio,
    )
    if not profile_text.strip():
        await _admit_member()
        return

    moderation = ModerationService(settings.moderation, _build_llm(settings))
    violated, reason, conclusive = await screen_member_profile_verbose(
        session,
        moderation,
        group_id=group_id,
        user_id=user_id,
        profile_text=profile_text,
    )
    log.info(
        "join screening done | group=%s user=%s violated=%s conclusive=%s reason=%s",
        group_id,
        user_id,
        violated,
        conclusive,
        reason or "-",
    )
    # The profile LLM may take many seconds. End its read transaction and
    # re-authorize in a fresh one before any cache/global-ban/Telegram action.
    await session.rollback()
    if not await is_group_authorized(session, group_id):
        log.info(
            "join screening verdict discarded | reason=group_deauthorized "
            "group=%s user=%s",
            group_id,
            user_id,
        )
        return
    await session.commit()
    if require_current_membership and not await _join_member_still_present(
        event,
        user_id,
        stage="profile_verdict",
    ):
        return
    # A manual /unban performed while profile moderation was in flight is the
    # newer operator intent.  Re-read both the exemption and recovery row before
    # applying the old verdict or starting a new challenge.
    # Obtain SQLite's process-wide writer gate before the final exemption read.
    # Whichever of this stale screening verdict and a concurrent /unban commits
    # last becomes authoritative; an older verdict can no longer delete an
    # exemption that was created while its LLM request was in flight.
    await acquire_group_settings_write_intent(session, group_id)
    if not await is_group_authorized(session, group_id):
        await session.rollback()
        return
    if (
        manual_unban_generation_is_active(group_id, user_id)
        or await is_join_screening_exempt(session, user_id)
    ):
        await session.commit()
        log.info(
            "join screening verdict discarded | reason=manual_unban_exemption "
            "group=%s user=%s",
            group_id,
            user_id,
        )
        return
    current_verification = await get_join_verification(session, group_id, user_id)
    if current_verification is not None and str(current_verification.status or "") in {
        VERIFICATION_STATUS_RELEASING,
        VERIFICATION_STATUS_UNBANNING,
    }:
        await session.commit()
        log.info(
            "join screening verdict discarded | reason=permission_recovery_%s "
            "group=%s user=%s",
            current_verification.status,
            group_id,
            user_id,
        )
        return
    if not violated:
        # Record the checked signature so on-message re-screening skips this
        # user until their visible profile or the enabled rules change. The
        # on-message signature has no bio (not available there), so store the
        # bio-less variant. Inconclusive verdicts are not cached.
        if conclusive:
            rules_fp = await moderation_rules_fingerprint(session, group_id)
            await mark_profile_screened(
                session,
                group_id,
                user_id,
                profile_hash=profile_screen_signature(
                    full_name=user.full_name or "",
                    username=user.username or "",
                    rules_fingerprint=rules_fp,
                ),
            )
        await session.commit()
        await _admit_member()
        return

    recovery = await lease_join_verification_for_unban(
        session,
        group_id,
        user_id,
        manual_unban=False,
    )
    if recovery is None:
        await session.rollback()
        log.error(
            "join profile ban recovery journal could not be created | group=%s user=%s",
            group_id,
            user_id,
        )
        return
    await add_global_ban(
        session,
        user_id,
        reason=f"入群资料命中群规: {reason}"[:500],
        source="join_screening",
        created_by=0,
    )
    # The registry entry must be durable and the SQLite write lock released
    # before Telegram ban/notification calls.
    await session.commit()

    async def current_profile_ban_policy() -> bool:
        if manual_unban_generation_is_active(group_id, user_id):
            return False
        await session.rollback()
        if not await is_group_authorized(session, group_id):
            await session.commit()
            return False
        blocked = await verification_release_blocked_by_ban(
            session,
            group_id=group_id,
            user_id=user_id,
        )
        await session.commit()
        return blocked

    async def current_profile_restriction_required() -> bool:
        await session.rollback()
        required = await verification_restriction_required(
            session,
            group_id=group_id,
            user_id=user_id,
        )
        await session.commit()
        return required

    enforced = await _ban_and_notify(
        event,
        settings,
        user_id=user_id,
        display_name=user.full_name,
        reason=reason,
        preserve_ban=current_profile_ban_policy,
        restriction_required=current_profile_restriction_required,
    )
    if not enforced:
        return
    completed = await complete_leased_join_verification(
        session,
        verification_id=int(recovery.verification_id),
        lease_until=recovery.lease_until,
        status=VERIFICATION_STATUS_UNBANNING,
    )
    if completed:
        await session.commit()
        return
    await session.rollback()
    await enforce_ban_with_policy_reconciliation(
        event.bot,
        group_id,
        user_id,
        current_profile_ban_policy,
        current_profile_restriction_required,
    )


def _member_status_value(member: object) -> str:
    raw = getattr(member, "status", "")
    return str(getattr(raw, "value", raw) or "").strip().lower()


@router.my_chat_member()
async def on_bot_membership_change(
    event: ChatMemberUpdated,
    session: AsyncSession,
    settings: Settings,
) -> None:
    """Keep authorized-group reachability in sync with Telegram authority.

    Durable inbox replay can deliver an old membership event.  The event's
    transition is therefore only a trigger: current bot membership is fetched
    from Telegram before changing the persisted state.
    """

    if event.chat.type not in ("group", "supergroup"):
        return
    group_id = int(event.chat.id)
    authorized = await session.get(AuthorizedGroup, group_id)
    if authorized is None:
        # Being added to a group never grants authorization implicitly.
        return
    # Do not hold a database read transaction across the Telegram authority
    # check; membership updates are rare but the API timeout is still seconds.
    await session.commit()

    bot_user = getattr(getattr(event, "new_chat_member", None), "user", None)
    bot_user_id = int(getattr(bot_user, "id", 0) or 0)
    if not bot_user_id:
        return

    try:
        async with asyncio.timeout(6.0):
            current = await event.bot.get_chat_member(group_id, bot_user_id)
        current_status = _member_status_value(current)
        present = current_status not in {"left", "kicked"}
        operational = bool(
            current_status == "creator"
            or current_status == "administrator"
            and bool(getattr(current, "can_restrict_members", False))
        )
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        if not telegram_group_is_unreachable_error(exc):
            log.warning(
                "bot membership refresh failed; authorization unchanged | "
                "group=%s error=%s",
                group_id,
                exc,
            )
            return
        present = False
        operational = False

    changed = await set_group_bot_present(
        session,
        group_id,
        present=present,
    )
    if not changed and not (present and operational):
        return
    if present:
        if operational:
            resumed = await resume_group_verification_recovery(session, group_id)
            refreshed = await extend_pending_verification_deadlines(
                session,
                settings=settings,
                group_id=group_id,
            )
        else:
            resumed = 0
            refreshed = 0
    else:
        resumed = 0
        refreshed = 0
    await session.commit()
    log.info(
        "authorized group bot membership changed | group=%s present=%s "
        "operational=%s recovery_resumed=%s pending_refreshed=%s",
        group_id,
        present,
        operational,
        resumed,
        refreshed,
    )


@router.chat_member(ChatMemberUpdatedFilter(member_status_changed=IS_NOT_MEMBER >> IS_MEMBER))
async def on_member_join(
    event: ChatMemberUpdated,
    session: AsyncSession,
    settings: Settings,
    session_factory: async_sessionmaker[AsyncSession] | None = None,
    event_update: Update | None = None,
) -> None:
    """Hand join security work to its reserved bounded lane.

    The critical administrator lane is separate, so slow profile moderation
    can no longer delay ``/ban``/``/unban``. Direct unit calls without the
    injected factory retain the synchronous implementation.
    """

    if session_factory is None:
        await _process_member_join(event, session, settings)
        return
    if event.chat.type not in ("group", "supergroup"):
        return
    user = getattr(getattr(event, "new_chat_member", None), "user", None)
    if user is None or bool(getattr(user, "is_bot", False)):
        return
    _invalidate_admin_cache(event)
    await session.commit()

    pair = (int(event.chat.id), int(user.id))
    update_id = int(getattr(event_update, "update_id", 0) or 0)
    pending = _PENDING_MEMBER_JOIN_SECURITY.get(pair)
    if pending is None:
        global _MEMBER_JOIN_JOB_SEQUENCE
        _MEMBER_JOIN_JOB_SEQUENCE += 1
        pending = _PendingMemberJoinSecurity(
            job_key=(
                f"member-join:{pair[0]}:{pair[1]}:"
                f"{update_id or _MEMBER_JOIN_JOB_SEQUENCE}"
            ),
            version=1,
            latest_update_id=update_id,
            event=event,
            settings=settings,
        )
        _PENDING_MEMBER_JOIN_SECURITY[pair] = pending
    else:
        # Exact durable replays attach their receipt to the same job. A newer
        # Telegram update replaces the snapshot and forces the active job to
        # run another generation before it can complete either receipt.
        is_new_generation = (
            update_id > pending.latest_update_id
            if update_id and pending.latest_update_id
            else pending.event is not event
        )
        if is_new_generation:
            pending.version += 1
            pending.latest_update_id = update_id
            pending.event = event
            pending.settings = settings

    async def operation() -> None:
        while True:
            generation = pending.version
            current_event = pending.event
            current_settings = pending.settings
            async with session_factory() as work_session:
                await _process_member_join(
                    current_event,
                    work_session,
                    current_settings,
                    session_factory=session_factory,
                    require_current_membership=True,
                )
            if pending.version != generation:
                continue
            if _PENDING_MEMBER_JOIN_SECURITY.get(pair) is pending:
                _PENDING_MEMBER_JOIN_SECURITY.pop(pair, None)
            return

    submission = submit_privileged_task(
        key=pending.job_key,
        label=f"member join security {int(user.id)} in {int(event.chat.id)}",
        operation=operation,
        lane="security",
        priority=0,
        timeout_seconds=180.0,
    )
    if submission.accepted:
        return

    # Fail-safe path: never silently drop a join security event. Queue
    # saturation is exceptional and may occupy this update worker, but it is
    # preferable to admitting a banned or unverified member unchecked.
    log.error(
        "join security queue rejected event; running inline | group=%s user=%s reason=%s",
        int(event.chat.id),
        int(user.id),
        submission.reason,
    )
    if _PENDING_MEMBER_JOIN_SECURITY.get(pair) is pending:
        _PENDING_MEMBER_JOIN_SECURITY.pop(pair, None)
    await _process_member_join(event, session, settings)


@router.chat_member(ChatMemberUpdatedFilter(member_status_changed=IS_MEMBER >> IS_NOT_MEMBER))
async def on_member_leave(
    event: ChatMemberUpdated, session: AsyncSession, settings: Settings
) -> None:
    """Drop join verification on leave, but retain moderation challenges.

    Join verification is only relevant while the member is present. A message
    challenge must survive leave/rejoin or the sender could evade its timeout.
    """
    if event.chat.type not in ("group", "supergroup"):
        return
    _invalidate_admin_cache(event)
    user = getattr(getattr(event, "new_chat_member", None), "user", None)
    if user is None:
        return
    try:
        await mark_group_member_left(session, event.chat.id, user.id)
    except Exception:
        log.debug("leave roster tracking failed | group=%s user=%s", event.chat.id, user.id, exc_info=True)
    record = await get_join_verification(session, event.chat.id, user.id)
    if record is None:
        return
    if record.kind in (
        VERIFICATION_KIND_MODERATION,
        VERIFICATION_KIND_PATROL,
        VERIFICATION_KIND_RAID,
    ):
        log.info(
            "%s challenge retained | reason=left group=%s user=%s",
            record.kind,
            event.chat.id,
            user.id,
        )
        return
    record_status = str(record.status or VERIFICATION_STATUS_PENDING)
    if record_status != VERIFICATION_STATUS_PENDING:
        log.info(
            "join verification retained | reason=terminal_%s group=%s user=%s",
            record_status,
            event.chat.id,
            user.id,
        )
        return
    prompt_message_id = int(record.prompt_message_id or 0)
    if await delete_join_verification(session, event.chat.id, user.id):
        # Commit before the Telegram call so a transient API failure cannot
        # roll back the terminal leave cleanup. The message is best-effort.
        await session.commit()
        bot = getattr(event, "bot", None)
        if bot is not None:
            await delete_verification_prompt(
                bot,
                int(event.chat.id),
                prompt_message_id,
            )
        log.info(
            "join verification cancelled | reason=left group=%s user=%s",
            event.chat.id,
            user.id,
        )


@router.chat_member()
async def on_member_status_change(
    event: ChatMemberUpdated, session: AsyncSession, settings: Settings
) -> None:
    """Catch-all for the remaining transitions (promote/demote/restrict).

    Registered after on_member_join / on_member_leave, so it only sees the
    transitions those filters do not match. Its only job is admin-status cache
    invalidation so a demoted admin loses moderation exemption on their next
    message.
    """
    if event.chat.type not in ("group", "supergroup"):
        return
    _invalidate_admin_cache(event)
