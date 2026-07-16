"""New-member join screening: name + bio checked against group moderation rules."""
from __future__ import annotations

import html
import logging
from datetime import timedelta

from aiogram import F, Router
from aiogram.filters import IS_NOT_MEMBER, IS_MEMBER, ChatMemberUpdatedFilter
from aiogram.types import CallbackQuery, ChatMemberUpdated
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from bot.config import Settings
from bot.db.models import Group, JoinVerification, UserWarning
from bot.services.admin_status import invalidate_admin_status_cache
from bot.services.authz import is_group_authorized, is_super_admin_user_id
from bot.services.callback_auth import is_group_admin_or_higher
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
    PATROL_VERIFY_CALLBACK_DATA,
    RAID_VERIFY_CALLBACK_DATA,
    VERIFICATION_CALLBACK_APPROVE,
    VERIFICATION_CALLBACK_PREFIX,
    VERIFICATION_CALLBACK_REJECT,
    VERIFICATION_CALLBACK_START,
    VERIFICATION_KIND_JOIN,
    VERIFICATION_KIND_MODERATION,
    VERIFICATION_KIND_PATROL,
    VERIFICATION_KIND_RAID,
    ban_member,
    build_group_prompt_keyboard,
    build_group_prompt_text,
    build_private_deep_link,
    claim_join_verification,
    delete_join_verification,
    get_join_verification,
    join_verification_ready,
    join_verification_policy,
    kick_member,
    mark_group_banned,
    parse_verification_callback_data,
    restore_member_permissions,
    rollback_group_ban,
    restrict_new_member,
    upsert_join_verification,
    verification_deadline_passed,
    verification_timeout_seconds_for_kind,
)
from bot.services.llm import LLMService
from bot.services.moderation import ModerationService
from bot.services.patrol import mark_group_member_left, track_group_member
from bot.services.raid_guard import get_raid_guard_service
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
) -> bool:
    try:
        banned = await event.chat.ban(user_id)
        if banned is False:
            raise RuntimeError("Telegram returned false")
    except Exception:
        log.exception("join screening ban failed | group=%s user=%s", event.chat.id, user_id)
        return False
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
    return True


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
    # This handler awaited network calls (bio fetch, screening LLM) since its
    # last record check; a raid retro sweep may have issued a challenge for
    # this member meanwhile. Upserting kind="join" would clobber it and break
    # its shared challenge button, so the existing record wins. Commit first:
    # the raid record was written by another session and a stale SQLite
    # snapshot from before those awaits would hide it.
    await session.commit()
    existing = await get_join_verification(session, group_id, user_id)
    if existing is not None and existing.kind != VERIFICATION_KIND_JOIN:
        log.info(
            "join verification skipped | reason=pending_%s group=%s user=%s",
            existing.kind,
            group_id,
            user_id,
        )
        return
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
            reply_markup=build_group_prompt_keyboard(user_id),
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
    settings: Settings,
    record: JoinVerification,
    *,
    display_name: str,
) -> None:
    """Keep an unresolved message/patrol/raid challenge intact across leave/rejoin.

    Moderation challenges ban on expiry; patrol and raid challenges kick
    without banning, matching the sweeper's consequences for each kind.
    """
    if is_super_admin_user_id(record.user_id, settings):
        await delete_join_verification(session, record.group_id, record.user_id)
        await session.commit()
        await restore_member_permissions(event.bot, record.group_id, record.user_id)
        return

    is_patrol = record.kind in (VERIFICATION_KIND_PATROL, VERIFICATION_KIND_RAID)
    now = now_shanghai_naive()
    if verification_deadline_passed(record.deadline_at, now=now):
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
        if is_patrol:
            await session.commit()
            enforced = await kick_member(event.bot, record.group_id, record.user_id)
            if not enforced:
                await upsert_join_verification(
                    session,
                    group_id=record.group_id,
                    user_id=record.user_id,
                    deadline_at=_verification_retry_deadline(settings, record.kind),
                    kind=record.kind,
                    provider=record.provider,
                    reason=record.reason,
                    display_name=display_name or record.display_name,
                    prompt_message_id=record.prompt_message_id,
                )
                await session.commit()
                await restrict_new_member(event.bot, record.group_id, record.user_id)
            return
        ban_state = await mark_group_banned(
            session,
            record.group_id,
            record.user_id,
        )
        await session.commit()
        enforced = await _ban_and_notify(
            event,
            user_id=record.user_id,
            display_name=display_name,
            reason="消息审查真人验证超时",
        )
        if not enforced:
            rolled_back = await rollback_group_ban(
                session,
                record.group_id,
                record.user_id,
                ban_state,
            )
            if rolled_back and not (ban_state and ban_state[1]):
                await upsert_join_verification(
                    session,
                    group_id=record.group_id,
                    user_id=record.user_id,
                    deadline_at=_verification_retry_deadline(settings, record.kind),
                    kind=record.kind,
                    provider=record.provider,
                    reason=record.reason,
                    display_name=display_name or record.display_name,
                    prompt_message_id=record.prompt_message_id,
                )
            await session.commit()
            if rolled_back:
                await restrict_new_member(event.bot, record.group_id, record.user_id)
        return

    # End the read transaction before the Telegram call. A concurrent web
    # verification can then delete the row and win without being hidden by a
    # stale SQLite snapshot.
    await session.commit()
    restricted = await restrict_new_member(event.bot, record.group_id, record.user_id)
    if not restricted:
        return
    current = await get_join_verification(session, record.group_id, record.user_id)
    if current is None or current.kind != record.kind:
        # Verification completed while the rejoin restriction was in flight.
        await restore_member_permissions(event.bot, record.group_id, record.user_id)
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
) -> None:
    kind = str(snapshot["kind"])
    await upsert_join_verification(
        session,
        group_id=int(snapshot["group_id"]),
        user_id=int(snapshot["user_id"]),
        deadline_at=_verification_retry_deadline(settings, kind),
        kind=kind,
        provider=str(snapshot["provider"]),
        reason=str(snapshot["reason"]),
        display_name=str(snapshot["display_name"]),
        prompt_message_id=int(snapshot["prompt_message_id"]),
    )
    await session.commit()


async def _verification_callback_record(
    callback: CallbackQuery,
    session: AsyncSession,
    target_user_id: int,
) -> JoinVerification | None:
    message = callback.message
    chat = getattr(message, "chat", None)
    if message is None or chat is None or chat.type not in ("group", "supergroup"):
        await callback.answer("验证消息已失效", show_alert=True)
        return None

    record = await get_join_verification(session, int(chat.id), target_user_id)
    message_id = int(getattr(message, "message_id", 0) or 0)
    if (
        record is None
        or int(record.prompt_message_id or 0) != message_id
        or verification_deadline_passed(record.deadline_at)
    ):
        await callback.answer("验证已失效、过期或已处理", show_alert=True)
        return None
    return record


async def _edit_verification_prompt(
    callback: CallbackQuery,
    *,
    text: str,
) -> None:
    message = callback.message
    chat = getattr(message, "chat", None)
    if message is None or chat is None:
        return
    try:
        await callback.bot.edit_message_text(
            chat_id=chat.id,
            message_id=message.message_id,
            text=text,
            parse_mode="HTML",
            reply_markup=None,
        )
    except Exception:
        log.debug(
            "verification admin prompt edit failed | group=%s message=%s",
            chat.id,
            message.message_id,
            exc_info=True,
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
) -> None:
    message = callback.message
    chat = getattr(message, "chat", None)
    operator = callback.from_user
    if message is None or chat is None or chat.type not in ("group", "supergroup"):
        await callback.answer("验证消息已失效", show_alert=True)
        return
    if operator is None:
        await callback.answer("无法识别操作者", show_alert=True)
        return
    group_id = int(chat.id)
    operator_id = int(operator.id)
    if not await is_group_authorized(session, group_id):
        await callback.answer("当前群组未授权", show_alert=True)
        return
    if not await is_group_admin_or_higher(
        bot=callback.bot,
        session=session,
        settings=settings,
        group_id=group_id,
        user_id=operator_id,
    ):
        await callback.answer("仅群管理员及以上权限可操作", show_alert=True)
        return

    record = await _verification_callback_record(callback, session, target_user_id)
    if record is None:
        return
    if action == VERIFICATION_CALLBACK_REJECT and is_super_admin_user_id(
        target_user_id, settings
    ):
        await callback.answer("不能封禁最高管理员", show_alert=True)
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
        if locally_banned or await is_globally_banned(session, target_user_id):
            await callback.answer("该用户已被封禁，请先解封后再通过", show_alert=True)
            return

    snapshot = _verification_snapshot(record)
    claimed = await claim_join_verification(
        session,
        verification_id=int(record.id),
        deadline_at=record.deadline_at,
        kind=record.kind,
        now=now_shanghai_naive(),
        expired=False,
    )
    if not claimed:
        await session.rollback()
        await callback.answer("验证已由其他操作处理", show_alert=True)
        return

    kind = str(snapshot["kind"])
    shown = html.escape(str(snapshot["display_name"] or target_user_id))
    if action == VERIFICATION_CALLBACK_APPROVE:
        await session.commit()
        restored = await restore_member_permissions(callback.bot, group_id, target_user_id)
        if not restored:
            await _requeue_verification(session, settings, snapshot)
            await callback.answer("权限恢复失败，验证已保留待处理", show_alert=True)
            return
        approved_text = (
            f"✅ <b>{shown}</b> 已由管理员直接通过消息审查验证，发言权限已恢复。"
            if kind == VERIFICATION_KIND_MODERATION
            else f"✅ <b>{shown}</b> 已由管理员直接通过入群验证，欢迎加入！"
        )
        await _edit_verification_prompt(callback, text=approved_text)
        await callback.answer("已直接通过验证")
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
            requeued = rolled_back and not (ban_state and ban_state[1])
            if requeued:
                await _requeue_verification(session, settings, snapshot)
            else:
                await session.commit()
            log.warning(
                "moderation verification admin ban failed | group=%s user=%s "
                "state_restored=%s",
                group_id,
                target_user_id,
                rolled_back,
            )
            await callback.answer(
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
        enforced = await kick_member(callback.bot, group_id, target_user_id)
        if not enforced:
            await _requeue_verification(session, settings, snapshot)
            await callback.answer("移出群聊失败，验证已保留待处理", show_alert=True)
            return
        rejected_text = (
            f"❌ <b>{shown}</b> 的入群验证已被管理员拒绝，已移出群聊。"
        )
    await _edit_verification_prompt(callback, text=rejected_text)
    await callback.answer("已直接拒绝验证")


@router.callback_query(F.data.startswith(f"{VERIFICATION_CALLBACK_PREFIX}:"))
async def on_verification_callback(
    callback: CallbackQuery,
    session: AsyncSession,
    settings: Settings,
) -> None:
    parsed = parse_verification_callback_data(callback.data or "")
    if parsed is None:
        await callback.answer("验证按钮参数错误", show_alert=True)
        return
    action, target_user_id = parsed
    if action == VERIFICATION_CALLBACK_START:
        await _handle_verification_start_callback(callback, session, target_user_id)
        return
    await _handle_verification_admin_callback(
        callback,
        session,
        settings,
        action=action,
        target_user_id=target_user_id,
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
        or record.kind != kind
        or verification_deadline_passed(record.deadline_at)
    ):
        await callback.answer("仅被点名的违规成员可点击", show_alert=True)
        return

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
    if is_super_admin_user_id(user.id, settings):
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
    if pending is not None and pending.kind in (
        VERIFICATION_KIND_MODERATION,
        VERIFICATION_KIND_PATROL,
        VERIFICATION_KIND_RAID,
    ):
        await _enforce_pending_moderation_challenge(
            event,
            session,
            settings,
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
