"""New-member join screening: name + bio checked against group moderation rules."""
from __future__ import annotations

import html
import logging
from datetime import timedelta

from aiogram import Router
from aiogram.filters import IS_NOT_MEMBER, IS_MEMBER, ChatMemberUpdatedFilter
from aiogram.types import ChatMemberUpdated
from sqlalchemy.ext.asyncio import AsyncSession

from bot.config import Settings
from bot.db.models import Group, JoinVerification
from bot.services.admin_status import invalidate_admin_status_cache
from bot.services.authz import is_group_authorized, is_super_admin_user_id
from bot.services.join_screening import (
    add_global_ban,
    build_join_profile_text,
    is_globally_banned,
    mark_profile_screened,
    moderation_rules_fingerprint,
    profile_screen_signature,
    screen_member_profile_verbose,
)
from bot.services.join_verification import (
    VERIFICATION_KIND_MODERATION,
    build_group_prompt_keyboard,
    build_group_prompt_text,
    claim_join_verification,
    delete_join_verification,
    get_join_verification,
    join_verification_ready,
    join_verification_policy,
    restore_member_permissions,
    restrict_new_member,
    upsert_join_verification,
)
from bot.services.llm import LLMService
from bot.services.moderation import ModerationService
from bot.utils.bot_identity import get_bot_identity
from bot.utils.timezone import now_shanghai_naive

router = Router()
log = logging.getLogger(__name__)


async def _fetch_user_bio(event: ChatMemberUpdated, user_id: int) -> str:
    """Bio is only exposed via a full getChat on the user's private chat."""
    try:
        chat = await event.bot.get_chat(user_id)
        return str(getattr(chat, "bio", "") or "")
    except Exception as exc:
        log.info("join screening bio fetch failed | user=%s error=%s", user_id, exc)
        return ""


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
    *,
    user_id: int,
    display_name: str,
    reason: str,
) -> None:
    try:
        await event.chat.ban(user_id)
    except Exception:
        log.exception("join screening ban failed | group=%s user=%s", event.chat.id, user_id)
        return
    shown = html.escape(display_name or str(user_id))
    reason_text = html.escape(reason or "入群资料命中群规")
    try:
        await event.bot.send_message(
            event.chat.id,
            f"🚫 已封禁新成员 <b>{shown}</b>（ID: <code>{user_id}</code>）\n原因：{reason_text}\n"
            "如需解封请管理员使用 /unban 命令。",
            parse_mode="HTML",
        )
    except Exception:
        log.exception("join screening notice failed | group=%s", event.chat.id)


def _invalidate_admin_cache(event: ChatMemberUpdated) -> None:
    # Promotion must invalidate a cached non-admin denial immediately.
    # Positive admin grants are never cached by admin_status.
    user = getattr(getattr(event, "new_chat_member", None), "user", None)
    if user is not None:
        invalidate_admin_status_cache(event.chat.id, user.id)


async def _start_join_verification(
    event: ChatMemberUpdated,
    session: AsyncSession,
    settings: Settings,
    *,
    user_id: int,
    display_name: str,
    provider: str,
) -> None:
    """Mute the new member and post the private-chat deep link; failure fails open.

    Restriction comes first: if the bot cannot restrict (missing rights), no
    pending record is written, so the member keeps normal permissions instead
    of being kicked later by the deadline sweeper for a challenge they never
    had access to.
    """
    group_id = event.chat.id
    if not await restrict_new_member(event.bot, group_id, user_id):
        return

    text = build_group_prompt_text(
        user_id=user_id,
        display_name=display_name,
        timeout_seconds=settings.join_verification_timeout_seconds,
    )
    prompt_message_id = 0
    try:
        sent = await event.bot.send_message(
            group_id,
            text,
            parse_mode="HTML",
            reply_markup=build_group_prompt_keyboard(
                get_bot_identity().username,
                group_id,
            ),
        )
        prompt_message_id = int(getattr(sent, "message_id", 0) or 0)
    except Exception:
        # Without a visible prompt the user cannot find the challenge; lift the mute.
        log.exception("join verification prompt failed | group=%s user=%s", group_id, user_id)
        await restore_member_permissions(event.bot, group_id, user_id)
        return

    deadline = now_shanghai_naive() + timedelta(
        seconds=settings.join_verification_timeout_seconds
    )
    await upsert_join_verification(
        session,
        group_id=group_id,
        user_id=user_id,
        deadline_at=deadline,
        display_name=display_name,
        prompt_message_id=prompt_message_id,
        provider=provider,
    )
    log.info("join verification issued | group=%s user=%s", group_id, user_id)


async def _enforce_pending_moderation_challenge(
    event: ChatMemberUpdated,
    session: AsyncSession,
    record: JoinVerification,
    *,
    display_name: str,
) -> None:
    """Keep an unresolved message challenge intact across leave/rejoin."""
    now = now_shanghai_naive()
    if record.deadline_at <= now:
        claimed = await claim_join_verification(
            session,
            verification_id=record.id,
            deadline_at=record.deadline_at,
            kind=record.kind,
            now=now,
            expired=True,
        )
        if not claimed:
            return
        await add_global_ban(
            session,
            record.user_id,
            reason=f"消息审查质询超时: {record.reason or '疑似命中群规'}"[:500],
            source="moderation_challenge_timeout",
            created_by=0,
        )
        await session.commit()
        await _ban_and_notify(
            event,
            user_id=record.user_id,
            display_name=display_name,
            reason="消息审查真人验证超时",
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
    if current is None or current.kind != VERIFICATION_KIND_MODERATION:
        # Verification completed while the rejoin restriction was in flight.
        await restore_member_permissions(event.bot, record.group_id, record.user_id)
        return
    log.info(
        "moderation challenge re-enforced after rejoin | group=%s user=%s",
        record.group_id,
        record.user_id,
    )


@router.chat_member(ChatMemberUpdatedFilter(member_status_changed=IS_NOT_MEMBER >> IS_MEMBER))
async def on_member_join(
    event: ChatMemberUpdated, session: AsyncSession, settings: Settings
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
    if is_super_admin_user_id(user.id, settings):
        return
    if not await is_group_authorized(session, event.chat.id):
        return

    group_id = event.chat.id
    user_id = user.id

    # Banned users are removed immediately on rejoin, no screening needed.
    if await is_globally_banned(session, user_id):
        log.info("join blocked | reason=banned group=%s user=%s", group_id, user_id)
        await _ban_and_notify(
            event,
            user_id=user_id,
            display_name=user.full_name,
            reason="该用户在封禁名单中",
        )
        return

    pending = await get_join_verification(session, group_id, user_id)
    if pending is not None and pending.kind == VERIFICATION_KIND_MODERATION:
        await _enforce_pending_moderation_challenge(
            event,
            session,
            pending,
            display_name=user.full_name or "",
        )
        return

    async def _maybe_start_verification() -> None:
        group = await session.get(Group, group_id)
        group_settings = group.settings if group is not None else None
        enabled, provider = join_verification_policy(settings, group_settings)
        if enabled and join_verification_ready(settings, group_settings):
            await _start_join_verification(
                event,
                session,
                settings,
                user_id=user_id,
                display_name=user.full_name or "",
                provider=provider,
            )

    if not settings.moderation.enabled:
        await _maybe_start_verification()
        return

    bio = await _fetch_user_bio(event, user_id)
    profile_text = build_join_profile_text(
        full_name=user.full_name or "",
        username=user.username or "",
        bio=bio,
    )
    if not profile_text.strip():
        await _maybe_start_verification()
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
    if not violated:
        # Record the checked signature so on-message re-screening skips this
        # user until their visible profile or the enabled rules change. The
        # on-message signature has no bio (not available there), so store the
        # bio-less variant. Inconclusive verdicts are not cached.
        if conclusive:
            rules_fp = await moderation_rules_fingerprint(session, group_id)
            await mark_profile_screened(
                session,
                user_id,
                profile_hash=profile_screen_signature(
                    full_name=user.full_name or "",
                    username=user.username or "",
                    rules_fingerprint=rules_fp,
                ),
            )
        await _maybe_start_verification()
        return

    await add_global_ban(
        session,
        user_id,
        reason=f"入群资料命中群规: {reason}"[:500],
        source="join_screening",
        created_by=0,
    )
    await _ban_and_notify(
        event,
        user_id=user_id,
        display_name=user.full_name,
        reason=reason,
    )


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
    record = await get_join_verification(session, event.chat.id, user.id)
    if record is None:
        return
    if record.kind == VERIFICATION_KIND_MODERATION:
        log.info(
            "moderation challenge retained | reason=left group=%s user=%s",
            event.chat.id,
            user.id,
        )
        return
    if await delete_join_verification(session, event.chat.id, user.id):
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
