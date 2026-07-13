"""Turnstile verification via a Telegram Mini App (join + moderation kinds).

Join flow (single-group deployment):
- A new member passes ban-check + profile screening, then gets fully muted
  via restrict_chat_member. The group prompt carries a deep link into the
  bot's private chat.
- /start in private chat sends a Mini App button (web_app); tapping it opens
  the challenge page inside Telegram — no visible URL, no browser jump.
- The page embeds Cloudflare Turnstile and submits the widget token together
  with Telegram's signed initData to the bot's built-in HTTP server
  (bot.services.verify_web). The server validates the Turnstile token against
  the siteverify API and the initData signature against the bot token, so
  the verified user identity cannot be forged; no secret link tokens needed.
- Passing lifts the restriction (all-True permissions returns the user to a
  plain member governed by the chat's default permissions).
- Missing the deadline kicks the user (ban + unban, so they may rejoin and
  retry later).

Moderation-challenge flow (kind="moderation"):
- A message judged violating with LOW confidence is deleted, the sender is
  fully muted, and the same private-chat deep link + Mini App challenge is
  issued (begin_moderation_challenge).
- Passing restores permissions; missing the deadline bans permanently and
  records the user in the global ban registry (/unban lifts it).

Pending records live in join_verifications; a background sweeper enforces
deadlines even across bot restarts.
"""
from __future__ import annotations

import asyncio
import html
import logging
import weakref
from datetime import datetime, timedelta

from aiogram import Bot
from aiogram.types import (
    ChatPermissions,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    WebAppInfo,
)
from sqlalchemy import delete, select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from bot.config import Settings
from bot.db.models import JoinVerification
from bot.services.join_screening import add_global_ban
from bot.utils.timezone import now_shanghai_naive

log = logging.getLogger(__name__)

_MODERATION_CHALLENGE_LOCKS: weakref.WeakValueDictionary[
    tuple[int, int], asyncio.Lock
] = weakref.WeakValueDictionary()

# Constant /start payload for the group deep link. Mini App (web_app) buttons
# are only allowed in private chats, so the group prompt sends the user to a
# private chat first, where the actual Mini App button lives.
START_VERIFY_PAYLOAD = "verify"
VERIFICATION_KIND_JOIN = "join"
VERIFICATION_KIND_MODERATION = "moderation"

_FULL_RESTRICT = ChatPermissions(
    can_send_messages=False,
    can_send_audios=False,
    can_send_documents=False,
    can_send_photos=False,
    can_send_videos=False,
    can_send_video_notes=False,
    can_send_voice_notes=False,
    can_send_polls=False,
    can_send_other_messages=False,
    can_add_web_page_previews=False,
    can_change_info=False,
    can_invite_users=False,
    can_pin_messages=False,
    can_manage_topics=False,
)
# Bot API: passing True for all permissions lifts the restriction entirely,
# returning the user to a normal member governed by chat default permissions.
_FULL_ALLOW = ChatPermissions(
    can_send_messages=True,
    can_send_audios=True,
    can_send_documents=True,
    can_send_photos=True,
    can_send_videos=True,
    can_send_video_notes=True,
    can_send_voice_notes=True,
    can_send_polls=True,
    can_send_other_messages=True,
    can_add_web_page_previews=True,
    can_change_info=True,
    can_invite_users=True,
    can_pin_messages=True,
    can_manage_topics=True,
)


def turnstile_verification_configured(settings: Settings) -> bool:
    """Return whether the shared Turnstile Mini App has complete config."""
    return bool(
        settings.join_verification_turnstile_site_key.strip()
        and settings.join_verification_turnstile_secret_key.strip()
        and settings.join_verification_public_base_url.strip()
    )


def join_verification_ready(settings: Settings) -> bool:
    """Return whether new-member verification is enabled and configured."""
    return bool(
        settings.join_verification_enabled
        and turnstile_verification_configured(settings)
    )


def moderation_challenge_ready(settings: Settings) -> bool:
    """Return whether low-confidence moderation challenges can be issued."""
    return bool(
        settings.moderation.enabled
        and turnstile_verification_configured(settings)
    )


def verification_service_ready(settings: Settings) -> bool:
    """The shared service also drains pending records after toggles are off."""
    return turnstile_verification_configured(settings)


def build_mini_app_url(settings: Settings) -> str:
    base = settings.join_verification_public_base_url.strip().rstrip("/")
    return f"{base}/verify"


def build_private_deep_link(bot_username: str) -> str:
    username = (bot_username or "").strip().lstrip("@")
    return f"https://t.me/{username}?start={START_VERIFY_PAYLOAD}"


def _format_timeout(timeout_seconds: int) -> str:
    seconds = max(1, int(timeout_seconds))
    if seconds % 60 == 0:
        return f"{seconds // 60} 分钟"
    return f"{seconds} 秒"


def build_group_prompt_text(
    *,
    user_id: int,
    display_name: str,
    timeout_seconds: int,
) -> str:
    shown = html.escape((display_name or "").strip() or str(user_id))
    return (
        f'👋 欢迎 <a href="tg://user?id={user_id}">{shown}</a>！\n'
        "本群已开启入群真人验证，验证通过前你暂时无法发言。\n\n"
        f"请在 {_format_timeout(timeout_seconds)} 内点击下方按钮，"
        "与我私聊并完成人机验证；\n"
        "超时将被移出群聊（可重新加入再试）。"
    )


def build_group_prompt_keyboard(bot_username: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="🔐 私聊完成验证",
                    url=build_private_deep_link(bot_username),
                )
            ]
        ]
    )


def build_moderation_prompt_text(
    *,
    user_id: int,
    display_name: str,
    reason: str,
    timeout_seconds: int,
) -> str:
    shown = html.escape((display_name or "").strip() or str(user_id))
    safe_reason = html.escape((reason or "疑似命中群规").strip())
    return (
        f'⚠️ <a href="tg://user?id={user_id}">{shown}</a> 的消息触发了低置信度违规判定。\n'
        f"原因：{safe_reason}\n\n"
        "验证通过前你暂时无法继续发言。\n"
        f"请在 {_format_timeout(timeout_seconds)} 内点击下方按钮完成真人验证；\n"
        "验证成功后自动恢复发言权限，超时将被封禁。"
    )


def build_private_challenge_text(
    *,
    deadline_at: datetime,
    kind: str = VERIFICATION_KIND_JOIN,
    reason: str = "",
) -> str:
    deadline = deadline_at.strftime("%H:%M:%S")
    if kind == VERIFICATION_KIND_MODERATION:
        safe_reason = html.escape((reason or "疑似命中群规").strip())
        return (
            "<b>消息审查真人验证</b>\n"
            f"待核验原因：{safe_reason}\n"
            "点击下方按钮完成人机验证，通过后即可恢复群内发言权限。\n\n"
            f"请在今天 {deadline} 前完成，超时将被封禁。"
        )
    return (
        "<b>入群真人验证</b>\n"
        "点击下方按钮，在弹出的窗口中完成人机验证，通过后即可在群内发言。\n\n"
        f"请在今天 {deadline} 前完成，超时将被移出群聊。"
    )


def build_private_challenge_keyboard(settings: Settings) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="✅ 开始验证",
                    web_app=WebAppInfo(url=build_mini_app_url(settings)),
                )
            ]
        ]
    )


async def get_join_verification(
    session: AsyncSession, group_id: int, user_id: int
) -> JoinVerification | None:
    # populate_existing: the sqlite upsert below writes past the identity map,
    # so a row loaded earlier in this session must be refreshed from the DB.
    stmt = (
        select(JoinVerification)
        .where(
            JoinVerification.group_id == group_id,
            JoinVerification.user_id == user_id,
        )
        .execution_options(populate_existing=True)
    )
    with session.no_autoflush:
        result = await session.execute(stmt)
    return result.scalar_one_or_none()


async def get_pending_verification_for_user(
    session: AsyncSession, user_id: int
) -> JoinVerification | None:
    stmt = (
        select(JoinVerification)
        .where(JoinVerification.user_id == user_id)
        .order_by(JoinVerification.deadline_at.desc())
        .limit(1)
        .execution_options(populate_existing=True)
    )
    with session.no_autoflush:
        result = await session.execute(stmt)
    return result.scalar_one_or_none()


async def upsert_join_verification(
    session: AsyncSession,
    *,
    group_id: int,
    user_id: int,
    deadline_at: datetime,
    kind: str = VERIFICATION_KIND_JOIN,
    reason: str = "",
    display_name: str = "",
    prompt_message_id: int = 0,
) -> None:
    """Create or replace the pending verification for (group, user).

    A rejoin while a stale record exists must issue a fresh deadline, so this
    always resets every mutable field.
    """
    if kind not in {VERIFICATION_KIND_JOIN, VERIFICATION_KIND_MODERATION}:
        raise ValueError(f"unsupported verification kind: {kind}")
    await session.flush()
    values = {
        "kind": kind,
        "reason": (reason or "")[:500],
        "display_name": (display_name or "")[:255],
        "prompt_message_id": prompt_message_id,
        "deadline_at": deadline_at,
    }
    dialect = getattr(getattr(session, "bind", None), "dialect", None)
    if getattr(dialect, "name", "") == "sqlite":
        stmt = sqlite_insert(JoinVerification).values(
            group_id=group_id,
            user_id=user_id,
            **values,
        )
        stmt = stmt.on_conflict_do_update(
            index_elements=[JoinVerification.group_id, JoinVerification.user_id],
            set_=values,
        )
        await session.execute(stmt)
        return

    row = await get_join_verification(session, group_id, user_id)
    if row is None:
        try:
            async with session.begin_nested():
                session.add(
                    JoinVerification(group_id=group_id, user_id=user_id, **values)
                )
                await session.flush()
            return
        except IntegrityError:
            row = await get_join_verification(session, group_id, user_id)
            if row is None:
                raise
    for key, value in values.items():
        setattr(row, key, value)


async def delete_join_verification(
    session: AsyncSession, group_id: int, user_id: int
) -> bool:
    await session.flush()
    result = await session.execute(
        delete(JoinVerification).where(
            JoinVerification.group_id == group_id,
            JoinVerification.user_id == user_id,
        )
    )
    return bool(result.rowcount)


async def delete_join_verifications_for_user(
    session: AsyncSession, user_id: int
) -> int:
    """Drop every pending verification for a user across all groups
    (used by /ban and /unban, which operate on the global registry)."""
    await session.flush()
    result = await session.execute(
        delete(JoinVerification).where(JoinVerification.user_id == user_id)
    )
    return int(result.rowcount or 0)


async def claim_join_verification(
    session: AsyncSession,
    *,
    verification_id: int,
    deadline_at: datetime,
    kind: str,
    now: datetime,
    expired: bool,
) -> bool:
    """Atomically claim an unchanged pending record for one terminal action.

    Both the web callback and deadline sweeper use this compare-and-delete so
    only one side may restore, kick, or ban a member. Matching the deadline and
    kind also prevents a stale worker from consuming a freshly replaced row.
    """
    await session.flush()
    deadline_condition = (
        JoinVerification.deadline_at <= now
        if expired
        else JoinVerification.deadline_at > now
    )
    result = await session.execute(
        delete(JoinVerification).where(
            JoinVerification.id == verification_id,
            JoinVerification.deadline_at == deadline_at,
            JoinVerification.kind == kind,
            deadline_condition,
        )
    )
    return bool(result.rowcount)


async def list_expired_verifications(
    session: AsyncSession, *, now: datetime, limit: int = 50
) -> list[JoinVerification]:
    stmt = (
        select(JoinVerification)
        .where(JoinVerification.deadline_at <= now)
        .order_by(JoinVerification.deadline_at)
        .limit(max(1, limit))
    )
    with session.no_autoflush:
        result = await session.execute(stmt)
    return list(result.scalars().all())


async def restrict_new_member(bot: Bot, chat_id: int, user_id: int) -> bool:
    try:
        await bot.restrict_chat_member(chat_id, user_id, permissions=_FULL_RESTRICT)
        return True
    except Exception:
        log.exception("join verification restrict failed | chat=%s user=%s", chat_id, user_id)
        return False


async def restore_member_permissions(bot: Bot, chat_id: int, user_id: int) -> bool:
    try:
        await bot.restrict_chat_member(chat_id, user_id, permissions=_FULL_ALLOW)
        return True
    except Exception:
        log.exception("join verification restore failed | chat=%s user=%s", chat_id, user_id)
        return False


async def kick_member(bot: Bot, chat_id: int, user_id: int) -> bool:
    """Kick (not permanently ban): the user may rejoin and verify again."""
    try:
        await bot.ban_chat_member(chat_id, user_id)
        await bot.unban_chat_member(chat_id, user_id, only_if_banned=True)
        return True
    except Exception:
        log.exception("join verification kick failed | chat=%s user=%s", chat_id, user_id)
        return False


async def ban_member(bot: Bot, chat_id: int, user_id: int) -> bool:
    """Permanently ban a member; unlike kick_member this never unbans."""
    try:
        await bot.ban_chat_member(chat_id, user_id)
        return True
    except Exception:
        log.exception("moderation challenge ban failed | chat=%s user=%s", chat_id, user_id)
        return False


async def begin_moderation_challenge(
    *,
    bot: Bot,
    session: AsyncSession,
    settings: Settings,
    group_id: int,
    user_id: int,
    display_name: str,
    bot_username: str,
    reason: str,
) -> bool:
    """Mute a sender, issue a Turnstile challenge, and persist its deadline.

    Returns False when the challenge is unavailable or cannot be presented.
    A prompt failure restores permissions so callers can safely fall back to
    the rule's normal high-confidence action without stranding the member.
    """
    key = (group_id, user_id)
    lock = _MODERATION_CHALLENGE_LOCKS.get(key)
    if lock is None:
        lock = asyncio.Lock()
        _MODERATION_CHALLENGE_LOCKS[key] = lock

    async with lock:
        return await _begin_moderation_challenge_locked(
            bot=bot,
            session=session,
            settings=settings,
            group_id=group_id,
            user_id=user_id,
            display_name=display_name,
            bot_username=bot_username,
            reason=reason,
        )


async def _begin_moderation_challenge_locked(
    *,
    bot: Bot,
    session: AsyncSession,
    settings: Settings,
    group_id: int,
    user_id: int,
    display_name: str,
    bot_username: str,
    reason: str,
) -> bool:
    if not moderation_challenge_ready(settings):
        return False

    # This handler has already performed several reads. Close any stale
    # snapshot before checking the record created by a concurrent message.
    await session.commit()
    current = await get_join_verification(session, group_id, user_id)
    if current is not None:
        if current.kind == VERIFICATION_KIND_MODERATION:
            # Another concurrent message already issued the challenge. Keep
            # the original deadline and prompt instead of extending it.
            await restrict_new_member(bot, group_id, user_id)
            return True
        return False

    if not await restrict_new_member(bot, group_id, user_id):
        return False

    timeout_seconds = max(60, int(settings.moderation.challenge_timeout_seconds))
    prompt_message_id = 0
    try:
        sent = await bot.send_message(
            group_id,
            build_moderation_prompt_text(
                user_id=user_id,
                display_name=display_name,
                reason=reason,
                timeout_seconds=timeout_seconds,
            ),
            parse_mode="HTML",
            reply_markup=build_group_prompt_keyboard(bot_username),
        )
        prompt_message_id = int(getattr(sent, "message_id", 0) or 0)
    except Exception:
        log.exception(
            "moderation challenge prompt failed | group=%s user=%s",
            group_id,
            user_id,
        )
        await restore_member_permissions(bot, group_id, user_id)
        return False

    try:
        await upsert_join_verification(
            session,
            group_id=group_id,
            user_id=user_id,
            deadline_at=now_shanghai_naive() + timedelta(seconds=timeout_seconds),
            kind=VERIFICATION_KIND_MODERATION,
            reason=(reason or "疑似命中群规")[:500],
            display_name=display_name,
            prompt_message_id=prompt_message_id,
        )
        # The mute and prompt are already visible external side effects. Make
        # the corresponding durable record visible before releasing the
        # per-user lock so a concurrent message cannot issue a second prompt.
        await session.commit()
    except Exception:
        await session.rollback()
        log.exception(
            "moderation challenge persistence failed | group=%s user=%s",
            group_id,
            user_id,
        )
        await restore_member_permissions(bot, group_id, user_id)
        return False
    log.info(
        "moderation challenge issued | group=%s user=%s timeout=%ss",
        group_id,
        user_id,
        timeout_seconds,
    )
    return True


async def warn_if_bot_cannot_verify(
    bot: Bot,
    settings: Settings,
    session_factory: "async_sessionmaker[AsyncSession] | None" = None,
) -> bool:
    """Startup self-check: verification silently does nothing unless the
    bot is a group admin with the restrict right.

    Telegram only delivers chat_member updates (member joins) to admin bots,
    and restrict_chat_member requires can_restrict_members. Checks every
    authorized group; returns True when all of them are in place.
    """
    if session_factory is None:
        return False
    from bot.services.authz import list_authorized_groups

    async with session_factory() as session:
        rows = await list_authorized_groups(session)
    group_ids = [int(row.group_id) for row in rows]
    if not group_ids:
        log.warning(
            "真人验证已启用，但当前没有任何已授权群（/authgroup），验证不会生效。"
        )
        return False

    try:
        me = await bot.me()
    except Exception:
        log.warning("join verification self-check failed: cannot resolve bot identity")
        return False

    all_ok = True
    for group_id in group_ids:
        try:
            member = await bot.get_chat_member(group_id, me.id)
        except Exception:
            log.warning(
                "join verification self-check failed: cannot query bot membership "
                "in group %s (is the bot in the group?)",
                group_id,
            )
            all_ok = False
            continue

        status = str(getattr(member, "status", "") or "")
        can_restrict = bool(getattr(member, "can_restrict_members", False))
        if status == "creator" or (status == "administrator" and can_restrict):
            continue
        log.warning(
            "真人验证已启用，但 bot 在群 %s 中%s：禁言/踢人需要「封禁用户」权限，"
            "入群事件也只会推送给管理员 bot。请将 bot 提升为管理员并勾选 Ban users，"
            "否则入群验证和消息审查质询不会生效。",
            group_id,
            "不是管理员" if status != "administrator" else "缺少「封禁用户」权限",
        )
        all_ok = False
    return all_ok


async def maybe_send_private_verification(
    message: Message, session: AsyncSession, settings: Settings
) -> bool:
    """Handle /start in private chat for a user with a pending verification.

    Returns True when the Mini App button was sent (the caller should not
    fall through to the normal welcome text).
    """
    if not verification_service_ready(settings):
        return False
    user = getattr(message, "from_user", None)
    if user is None:
        return False

    record = await get_pending_verification_for_user(session, user.id)
    if record is None:
        return False
    if record.deadline_at <= now_shanghai_naive():
        # The sweeper will kick shortly; a fresh button would be useless.
        return False

    await message.answer(
        build_private_challenge_text(
            deadline_at=record.deadline_at,
            kind=record.kind,
            reason=record.reason,
        ),
        parse_mode="HTML",
        reply_markup=build_private_challenge_keyboard(settings),
    )
    log.info(
        "verification mini app sent | kind=%s group=%s user=%s",
        record.kind,
        record.group_id,
        user.id,
    )
    return True


class JoinVerificationSweeper:
    """Background loop enforcing verification deadlines.

    Records survive restarts, so on each pass every expired (group, user) is
    kicked, its prompt message updated, and the record removed.
    """

    def __init__(
        self,
        *,
        bot: Bot,
        session_factory: async_sessionmaker[AsyncSession],
        check_interval_seconds: float = 30.0,
    ) -> None:
        self.bot = bot
        self.session_factory = session_factory
        self.check_interval_seconds = max(5.0, float(check_interval_seconds))

    async def run_forever(self) -> None:
        log.info("verification sweeper started")
        while True:
            try:
                await self.sweep_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("join verification sweep failed")
            await asyncio.sleep(self.check_interval_seconds)

    async def sweep_once(self) -> int:
        async with self.session_factory() as session:
            now = now_shanghai_naive()
            expired = await list_expired_verifications(session, now=now)
            if not expired:
                return 0
            claimed: list[JoinVerification] = []
            for record in expired:
                won = await claim_join_verification(
                    session,
                    verification_id=record.id,
                    deadline_at=record.deadline_at,
                    kind=record.kind,
                    now=now,
                    expired=True,
                )
                if not won:
                    continue
                if record.kind == VERIFICATION_KIND_MODERATION:
                    await add_global_ban(
                        session,
                        record.user_id,
                        reason=f"消息审查质询超时: {record.reason or '疑似命中群规'}"[:500],
                        source="moderation_challenge_timeout",
                        created_by=0,
                    )
                claimed.append(record)
            # Persist the terminal claim (and moderation ban registry entry)
            # before Telegram calls. Failed API calls are then safely enforced
            # by the global-ban middleware on the user's next update.
            await session.commit()

        for record in claimed:
            log.info(
                "verification timeout | kind=%s group=%s user=%s",
                record.kind,
                record.group_id,
                record.user_id,
            )
            if record.kind == VERIFICATION_KIND_MODERATION:
                await ban_member(self.bot, record.group_id, record.user_id)
                text = "⏰ 消息审查验证超时，已封禁。请联系管理员处理。"
            else:
                await kick_member(self.bot, record.group_id, record.user_id)
                text = "⏰ 验证超时，已移出群聊。可重新加入再次验证。"
            await self._finalize_prompt(record, text)
        return len(claimed)

    async def _finalize_prompt(self, record: JoinVerification, text: str) -> None:
        message_id = int(record.prompt_message_id or 0)
        if not message_id:
            return
        try:
            await self.bot.edit_message_text(
                chat_id=record.group_id,
                message_id=message_id,
                text=text,
            )
        except Exception:
            log.debug(
                "join verification prompt finalize skipped | group=%s message=%s",
                record.group_id,
                message_id,
            )
