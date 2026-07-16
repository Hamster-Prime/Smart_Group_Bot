"""Human verification via a Telegram Mini App (join + moderation kinds).

Join flow (per-group policy):
- A new member passes ban-check + profile screening, then gets fully muted
  via restrict_chat_member. The group prompt carries a target-locked callback
  that redirects the affected member into the bot's private chat.
- /start in private chat sends a Mini App button (web_app); tapping it opens
  the challenge page inside Telegram — no visible URL, no browser jump.
- The page embeds the selected Turnstile or hCaptcha widget and submits its token together
  with Telegram's signed initData to the bot's built-in HTTP server
  (bot.services.verify_web). The server validates the widget token against
  the provider siteverify API and the initData signature against the bot token, so
  the verified user identity cannot be forged; no secret link tokens needed.
- Passing lifts the restriction (all-True permissions returns the user to a
  plain member governed by the chat's default permissions).
- Missing the deadline kicks the user (ban + unban, so they may rejoin and
  retry later).

Moderation-challenge flow (kind="moderation"):
- A message judged violating with LOW confidence is deleted, the sender is
  fully muted, and the same private-chat deep link + Mini App challenge is
  issued (begin_moderation_challenge).
- Passing restores permissions; missing the deadline bans the user in the
  current group until a group administrator lifts that ban.

Pending records live in join_verifications; a background sweeper enforces
deadlines even across bot restarts.
"""
from __future__ import annotations

import asyncio
import html
import logging
import weakref
from datetime import datetime, timedelta
from urllib.parse import urlencode

from aiogram import Bot
from aiogram.types import (
    ChatPermissions,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    WebAppInfo,
)
from sqlalchemy import delete, select, update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from bot.config import Settings
from bot.db.models import JoinVerification, UserWarning
from bot.services.authz import is_super_admin_user_id
from bot.utils.timezone import now_shanghai_naive

log = logging.getLogger(__name__)

_MODERATION_CHALLENGE_LOCKS: weakref.WeakValueDictionary[
    tuple[int, int], asyncio.Lock
] = weakref.WeakValueDictionary()
_BLOCKED_VERIFICATION_CONFIGS: dict[
    tuple[int, str], tuple[Settings, tuple[str, ...], str]
] = {}

# Constant /start payload used after the target-locked group callback. Mini App
# (web_app) buttons are only allowed in private chats, so the callback redirects
# the affected user there before the actual challenge button is shown.
START_VERIFY_PAYLOAD = "verify"
# Interactive challenges (hCaptcha image rounds, slow proxies) legitimately
# finish shortly after deadline_at. Submissions and admin actions stay valid
# for this long past the deadline, and the sweeper only enforces timeouts once
# the grace has also elapsed, so a member who solved in time is never rejected
# by a race with enforcement.
CHALLENGE_SUBMIT_GRACE = timedelta(seconds=60)
VERIFICATION_KIND_JOIN = "join"
VERIFICATION_KIND_MODERATION = "moderation"
VERIFICATION_KIND_PATROL = "patrol"
VERIFICATION_KINDS = frozenset(
    {
        VERIFICATION_KIND_JOIN,
        VERIFICATION_KIND_MODERATION,
        VERIFICATION_KIND_PATROL,
    }
)
# The combined provider requires solving both base challenges in one session.
COMBINED_VERIFICATION_PROVIDER = "turnstile_hcaptcha"
_BASE_VERIFICATION_PROVIDERS = ("turnstile", "hcaptcha")
VERIFICATION_PROVIDERS = frozenset(
    {*_BASE_VERIFICATION_PROVIDERS, COMBINED_VERIFICATION_PROVIDER}
)
VERIFICATION_CALLBACK_PREFIX = "jv"
VERIFICATION_CALLBACK_START = "v"
VERIFICATION_CALLBACK_APPROVE = "a"
VERIFICATION_CALLBACK_REJECT = "r"
VERIFICATION_CALLBACK_ACTIONS = frozenset(
    {
        VERIFICATION_CALLBACK_START,
        VERIFICATION_CALLBACK_APPROVE,
        VERIFICATION_CALLBACK_REJECT,
    }
)
# The patrol warning message is shared by many violators, so its button has no
# embedded user id: the handler resolves the clicker's own pending record.
PATROL_VERIFY_CALLBACK_DATA = "ptv"

_GroupBanState = tuple[int, bool] | None


def normalize_verification_provider(value: object, *, default: str = "turnstile") -> str:
    provider = str(value or "").strip().lower()
    if provider in VERIFICATION_PROVIDERS:
        return provider
    fallback = str(default or "turnstile").strip().lower()
    return fallback if fallback in VERIFICATION_PROVIDERS else "turnstile"


def verification_subproviders(
    provider: object,
    *,
    default: str = "turnstile",
) -> tuple[str, ...]:
    """Base challenge services the member must solve for a selected provider.

    Base providers map to themselves; the combined provider expands to both
    base services in the order the member is guided through them.
    """
    normalized = normalize_verification_provider(provider, default=default)
    if normalized == COMBINED_VERIFICATION_PROVIDER:
        return _BASE_VERIFICATION_PROVIDERS
    return (normalized,)


def verification_deadline_passed(
    deadline_at: datetime,
    *,
    now: datetime | None = None,
) -> bool:
    """True once the deadline plus CHALLENGE_SUBMIT_GRACE has elapsed."""
    current = now if now is not None else now_shanghai_naive()
    return deadline_at <= current - CHALLENGE_SUBMIT_GRACE


async def mark_group_banned(
    session: AsyncSession,
    group_id: int,
    user_id: int,
) -> _GroupBanState:
    warning = await session.scalar(
        select(UserWarning).where(
            UserWarning.group_id == group_id,
            UserWarning.user_id == user_id,
        )
    )
    if warning is None:
        session.add(
            UserWarning(
                group_id=group_id,
                user_id=user_id,
                count=0,
                is_banned=True,
            )
        )
        return None
    previous = (max(0, int(warning.count or 0)), bool(warning.is_banned))
    warning.is_banned = True
    return previous


async def rollback_group_ban(
    session: AsyncSession,
    group_id: int,
    user_id: int,
    previous: _GroupBanState,
) -> bool:
    """Undo our local-ban write only when no concurrent update replaced it."""
    if previous is None:
        result = await session.execute(
            delete(UserWarning).where(
                UserWarning.group_id == group_id,
                UserWarning.user_id == user_id,
                UserWarning.count == 0,
                UserWarning.is_banned.is_(True),
            )
        )
        return int(result.rowcount or 0) == 1

    previous_count, was_banned = previous
    if was_banned:
        return True
    result = await session.execute(
        update(UserWarning)
        .where(
            UserWarning.group_id == group_id,
            UserWarning.user_id == user_id,
            UserWarning.count == previous_count,
            UserWarning.is_banned.is_(True),
        )
        .values(is_banned=False)
    )
    return int(result.rowcount or 0) == 1


def verification_timeout_seconds_for_kind(settings: Settings, kind: str) -> int:
    """Challenge window for a verification kind, min-clamped to 60 seconds."""
    if kind == VERIFICATION_KIND_MODERATION:
        raw = settings.moderation.challenge_timeout_seconds
    elif kind == VERIFICATION_KIND_PATROL:
        raw = (
            getattr(settings, "patrol_challenge_timeout_seconds", 0)
            or settings.join_verification_timeout_seconds
        )
    else:
        raw = settings.join_verification_timeout_seconds
    return max(60, int(raw))


def verification_provider(
    settings: Settings,
    group_settings: dict | None = None,
) -> str:
    default = normalize_verification_provider(
        getattr(settings, "join_verification_provider", "turnstile")
    )
    if isinstance(group_settings, dict) and "join_verification_provider" in group_settings:
        return normalize_verification_provider(
            group_settings.get("join_verification_provider"),
            default=default,
        )
    return default


def verification_keys_for_provider(
    settings: Settings,
    provider: str,
) -> tuple[str, str]:
    """Site/secret pair for one base challenge service.

    The combined provider has no single key pair; callers handling it must
    iterate verification_subproviders() instead. An unknown value falls back
    to Turnstile, matching normalize_verification_provider.
    """
    if normalize_verification_provider(provider) == "hcaptcha":
        return (
            settings.join_verification_hcaptcha_site_key.strip(),
            settings.join_verification_hcaptcha_secret_key.strip(),
        )
    return (
        settings.join_verification_turnstile_site_key.strip(),
        settings.join_verification_turnstile_secret_key.strip(),
    )


def verification_keys(settings: Settings) -> tuple[str, str]:
    return verification_keys_for_provider(settings, verification_provider(settings))


def join_verification_policy(
    settings: Settings,
    group_settings: dict | None = None,
) -> tuple[bool, str]:
    enabled = bool(settings.join_verification_enabled)
    if isinstance(group_settings, dict) and "join_verification_enabled" in group_settings:
        raw_enabled = group_settings.get("join_verification_enabled")
        if isinstance(raw_enabled, str):
            normalized = raw_enabled.strip().lower()
            if normalized in {"1", "true", "yes", "on", "enable", "enabled"}:
                enabled = True
            elif normalized in {"0", "false", "no", "off", "disable", "disabled"}:
                enabled = False
        elif raw_enabled is not None:
            enabled = bool(raw_enabled)
    return enabled, verification_provider(settings, group_settings)


def _verification_config_fingerprint(
    settings: Settings,
    provider: str | None = None,
) -> tuple[str, ...]:
    selected_provider = normalize_verification_provider(
        provider,
        default=verification_provider(settings),
    )
    parts: list[str] = [selected_provider]
    for subprovider in verification_subproviders(selected_provider):
        parts.extend(verification_keys_for_provider(settings, subprovider))
    parts.append(settings.join_verification_public_base_url.strip().rstrip("/"))
    return tuple(parts)


def mark_turnstile_configuration_unavailable(
    settings: Settings,
    *,
    reason: str,
    provider: str | None = None,
) -> None:
    """Block one provider configuration until its credentials change.

    Blocks are stored per base service; a combined provider blocks each base
    service it expands to (callers that know which one failed should pass it).
    """
    selected_provider = normalize_verification_provider(
        provider,
        default=verification_provider(settings),
    )
    for subprovider in verification_subproviders(selected_provider):
        fingerprint = _verification_config_fingerprint(settings, subprovider)
        _BLOCKED_VERIFICATION_CONFIGS[(id(settings), subprovider)] = (
            settings,
            fingerprint,
            (reason or f"{subprovider} 配置不可用").strip(),
        )


def clear_turnstile_configuration_unavailable(
    settings: Settings,
    *,
    provider: str | None = None,
) -> None:
    """Clear a provider block after the same configuration verifies successfully."""
    selected_provider = normalize_verification_provider(
        provider,
        default=verification_provider(settings),
    )
    for subprovider in verification_subproviders(selected_provider):
        fingerprint = _verification_config_fingerprint(settings, subprovider)
        key = (id(settings), subprovider)
        blocked = _BLOCKED_VERIFICATION_CONFIGS.get(key)
        if blocked is not None and blocked[0] is settings and blocked[1] == fingerprint:
            _BLOCKED_VERIFICATION_CONFIGS.pop(key, None)


def turnstile_runtime_configuration_issue(
    settings: Settings,
    provider: str | None = None,
) -> str:
    """Return a provider's runtime block, clearing it after config edits.

    A combined provider reports the first blocked base service it expands to.
    """
    selected_provider = normalize_verification_provider(
        provider,
        default=verification_provider(settings),
    )
    for subprovider in verification_subproviders(selected_provider):
        fingerprint = _verification_config_fingerprint(settings, subprovider)
        key = (id(settings), subprovider)
        blocked = _BLOCKED_VERIFICATION_CONFIGS.get(key)
        if blocked is None or blocked[0] is not settings:
            continue
        if blocked[1] != fingerprint:
            _BLOCKED_VERIFICATION_CONFIGS.pop(key, None)
            continue
        return blocked[2]
    return ""

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
    can_react_to_messages=False,
    can_edit_tag=False,
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
    can_react_to_messages=True,
    can_edit_tag=True,
    can_change_info=True,
    can_invite_users=True,
    can_pin_messages=True,
    can_manage_topics=True,
)


def turnstile_verification_configured(
    settings: Settings,
    provider: str | None = None,
) -> bool:
    """Return whether the selected challenge provider has complete config.

    The combined provider is only configured when every base service it
    expands to is configured and unblocked.
    """
    selected_provider = normalize_verification_provider(
        provider,
        default=verification_provider(settings),
    )
    if not settings.join_verification_public_base_url.strip():
        return False
    for subprovider in verification_subproviders(selected_provider):
        site_key, secret_key = verification_keys_for_provider(settings, subprovider)
        if (
            not site_key
            or not secret_key
            or site_key == secret_key
            or turnstile_runtime_configuration_issue(settings, subprovider)
        ):
            return False
    return True


def join_verification_ready(
    settings: Settings,
    group_settings: dict | None = None,
) -> bool:
    """Return whether new-member verification is enabled and configured."""
    enabled, provider = join_verification_policy(settings, group_settings)
    return bool(enabled and turnstile_verification_configured(settings, provider))


def moderation_challenge_ready(settings: Settings) -> bool:
    """Return whether low-confidence moderation challenges can be issued."""
    provider = verification_provider(settings)
    return bool(
        settings.moderation.enabled
        and turnstile_verification_configured(settings, provider)
    )


def verification_service_ready(
    settings: Settings,
    provider: str | None = None,
) -> bool:
    """The shared service also drains pending records after toggles are off."""
    return turnstile_verification_configured(settings, provider)


def build_mini_app_url(
    settings: Settings,
    provider: str | None = None,
    verification_id: int | None = None,
) -> str:
    base = settings.join_verification_public_base_url.strip().rstrip("/")
    url = f"{base}/verify"
    query: dict[str, str] = {}
    has_verification_id = verification_id is not None and int(verification_id) > 0
    if (
        provider is not None
        and (
            has_verification_id
            or normalize_verification_provider(provider)
            != verification_provider(settings)
        )
    ):
        query["provider"] = normalize_verification_provider(provider)
    if has_verification_id:
        query["verification_id"] = str(int(verification_id))
    if not query:
        return url
    return f"{url}?{urlencode(query)}"


def build_private_deep_link(
    bot_username: str,
    group_id: int | None = None,
) -> str:
    username = (bot_username or "").strip().lstrip("@")
    payload = START_VERIFY_PAYLOAD
    if group_id is not None:
        group_id = int(group_id)
        sign = "n" if group_id < 0 else "p"
        payload = f"{START_VERIFY_PAYLOAD}_{sign}{abs(group_id)}"
    return f"https://t.me/{username}?start={payload}"


def parse_private_verify_group_id(payload: str) -> int | None:
    normalized = str(payload or "").strip()
    prefix = f"{START_VERIFY_PAYLOAD}_"
    if not normalized.startswith(prefix):
        return None
    encoded = normalized[len(prefix) :]
    if len(encoded) < 2 or encoded[0] not in {"n", "p"} or not encoded[1:].isdigit():
        return None
    group_id = int(encoded[1:])
    return -group_id if encoded[0] == "n" else group_id


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


def build_verification_callback_data(action: str, user_id: int) -> str:
    normalized_action = str(action or "").strip().lower()
    if normalized_action not in VERIFICATION_CALLBACK_ACTIONS:
        raise ValueError(f"unsupported verification callback action: {action}")
    return f"{VERIFICATION_CALLBACK_PREFIX}:{normalized_action}:{int(user_id)}"


def parse_verification_callback_data(value: str) -> tuple[str, int] | None:
    parts = str(value or "").split(":")
    if len(parts) != 3 or parts[0] != VERIFICATION_CALLBACK_PREFIX:
        return None
    action = parts[1].strip().lower()
    if action not in VERIFICATION_CALLBACK_ACTIONS:
        return None
    try:
        user_id = int(parts[2])
    except (TypeError, ValueError):
        return None
    return (action, user_id) if user_id > 0 else None


def build_group_prompt_keyboard(user_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="🔐 开始验证",
                    callback_data=build_verification_callback_data(
                        VERIFICATION_CALLBACK_START,
                        user_id,
                    ),
                )
            ],
            [
                InlineKeyboardButton(
                    text="✅ 管理员通过",
                    callback_data=build_verification_callback_data(
                        VERIFICATION_CALLBACK_APPROVE,
                        user_id,
                    ),
                ),
                InlineKeyboardButton(
                    text="❌ 管理员拒绝",
                    callback_data=build_verification_callback_data(
                        VERIFICATION_CALLBACK_REJECT,
                        user_id,
                    ),
                ),
            ],
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
    if kind == VERIFICATION_KIND_PATROL:
        safe_reason = html.escape((reason or "资料疑似命中群规").strip())
        return (
            "<b>资料巡检真人质询</b>\n"
            f"待核验原因：{safe_reason}\n"
            "点击下方按钮完成人机验证，通过后即可恢复群内发言权限。\n\n"
            f"请在今天 {deadline} 前完成，超时将被移出群聊（可重新加入）。"
        )
    return (
        "<b>入群真人验证</b>\n"
        "点击下方按钮，在弹出的窗口中完成人机验证，通过后即可在群内发言。\n\n"
        f"请在今天 {deadline} 前完成，超时将被移出群聊。"
    )


def build_private_challenge_keyboard(
    settings: Settings,
    provider: str | None = None,
    verification_id: int | None = None,
) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="✅ 开始验证",
                    web_app=WebAppInfo(
                        url=build_mini_app_url(
                            settings,
                            provider,
                            verification_id,
                        )
                    ),
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


async def get_sole_pending_verification_for_user(
    session: AsyncSession, user_id: int
) -> JoinVerification | None:
    """The user's pending record, but only when it is unambiguous.

    Used to recover a submission whose page pinned a stale verification id:
    with records pending in several groups there is no way to tell which one
    the page belonged to, so recovery must not guess.
    """
    stmt = (
        select(JoinVerification)
        .where(JoinVerification.user_id == user_id)
        .limit(2)
        .execution_options(populate_existing=True)
    )
    with session.no_autoflush:
        result = await session.execute(stmt)
    rows = list(result.scalars().all())
    return rows[0] if len(rows) == 1 else None


async def get_pending_verification_by_id_for_user(
    session: AsyncSession,
    verification_id: int,
    user_id: int,
) -> JoinVerification | None:
    stmt = (
        select(JoinVerification)
        .where(
            JoinVerification.id == int(verification_id),
            JoinVerification.user_id == int(user_id),
        )
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
    provider: str = "turnstile",
) -> None:
    """Create or replace the pending verification for (group, user).

    A rejoin while a stale record exists must issue a fresh deadline, so this
    always resets every mutable field.
    """
    if kind not in VERIFICATION_KINDS:
        raise ValueError(f"unsupported verification kind: {kind}")
    selected_provider = normalize_verification_provider(provider)
    await session.flush()
    values = {
        "kind": kind,
        "provider": selected_provider,
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

    The deadline is enforced with CHALLENGE_SUBMIT_GRACE: a pass may still be
    claimed until deadline+grace, and a timeout only after it. The two
    conditions stay complementary, so exactly one side can win at any instant.
    """
    await session.flush()
    cutoff = now - CHALLENGE_SUBMIT_GRACE
    deadline_condition = (
        JoinVerification.deadline_at <= cutoff
        if expired
        else JoinVerification.deadline_at > cutoff
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
        .where(JoinVerification.deadline_at <= now - CHALLENGE_SUBMIT_GRACE)
        .order_by(JoinVerification.deadline_at)
        .limit(max(1, limit))
    )
    with session.no_autoflush:
        result = await session.execute(stmt)
    return list(result.scalars().all())


async def extend_pending_verification_deadlines(
    session: AsyncSession,
    *,
    settings: Settings,
    now: datetime | None = None,
    provider: str | None = None,
) -> int:
    """Give affected pending users a fresh window while a provider is unavailable."""
    current = now or now_shanghai_naive()
    stmt = select(JoinVerification)
    if provider is not None:
        normalized = normalize_verification_provider(provider)
        # An unavailable base service also stalls combined records that
        # include it, so those must receive the same deadline extension.
        affected = sorted(
            candidate
            for candidate in VERIFICATION_PROVIDERS
            if candidate == normalized
            or normalized in verification_subproviders(candidate)
        )
        stmt = stmt.where(JoinVerification.provider.in_(affected))
    result = await session.execute(stmt)
    extended = 0
    for record in result.scalars().all():
        timeout_seconds = verification_timeout_seconds_for_kind(settings, record.kind)
        target = current + timedelta(seconds=timeout_seconds)
        if record.deadline_at < target:
            record.deadline_at = target
            extended += 1
    return extended


async def restrict_new_member(bot: Bot, chat_id: int, user_id: int) -> bool:
    try:
        restricted = await bot.restrict_chat_member(
            chat_id,
            user_id,
            permissions=_FULL_RESTRICT,
        )
        return restricted is not False
    except Exception:
        log.exception("join verification restrict failed | chat=%s user=%s", chat_id, user_id)
        return False


async def restore_member_permissions(bot: Bot, chat_id: int, user_id: int) -> bool:
    last_error: Exception | None = None
    for attempt in range(1, 4):
        try:
            restored = await bot.restrict_chat_member(
                chat_id,
                user_id,
                permissions=_FULL_ALLOW,
                use_independent_chat_permissions=True,
            )
            if restored:
                return True
        except Exception as exc:
            last_error = exc
            log.warning(
                "join verification restore request failed | chat=%s user=%s attempt=%s/3 error=%s",
                chat_id,
                user_id,
                attempt,
                exc,
            )

        # Telegram may apply the permission update even when the HTTP response
        # is lost. Confirm the actual member state before retrying or reporting
        # failure to the Mini App.
        try:
            member = await bot.get_chat_member(chat_id, user_id)
            status = str(getattr(member, "status", "") or "")
            if status in {"member", "administrator", "creator"} or (
                status == "restricted"
                and bool(getattr(member, "can_send_messages", False))
            ):
                log.info(
                    "join verification restore confirmed after ambiguous response | chat=%s user=%s",
                    chat_id,
                    user_id,
                )
                return True
        except Exception:
            log.debug(
                "join verification restore status check failed | chat=%s user=%s",
                chat_id,
                user_id,
                exc_info=True,
            )

        if attempt < 3:
            await asyncio.sleep(0.35 * attempt)

    log.error(
        "join verification restore failed | chat=%s user=%s error=%s",
        chat_id,
        user_id,
        last_error or "Telegram returned false",
    )
    return False


async def kick_member(bot: Bot, chat_id: int, user_id: int) -> bool:
    """Kick (not permanently ban): the user may rejoin and verify again."""
    try:
        banned = await bot.ban_chat_member(chat_id, user_id)
        if banned is False:
            return False
        unbanned = await bot.unban_chat_member(chat_id, user_id, only_if_banned=True)
        return unbanned is not False
    except Exception:
        log.exception("join verification kick failed | chat=%s user=%s", chat_id, user_id)
        return False


async def ban_member(bot: Bot, chat_id: int, user_id: int) -> bool:
    """Permanently ban a member; unlike kick_member this never unbans."""
    try:
        banned = await bot.ban_chat_member(chat_id, user_id)
        return banned is not False
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
    """Mute a sender, issue a provider challenge, and persist its deadline.

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
            reply_markup=build_group_prompt_keyboard(user_id),
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
            provider=verification_provider(settings),
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
    message: Message,
    session: AsyncSession,
    settings: Settings,
    *,
    group_id: int | None = None,
) -> bool:
    """Handle /start in private chat for a user with a pending verification.

    Returns True when the Mini App button was sent (the caller should not
    fall through to the normal welcome text).
    """
    user = getattr(message, "from_user", None)
    if user is None:
        return False

    record = (
        await get_join_verification(session, int(group_id), user.id)
        if group_id is not None
        else await get_pending_verification_for_user(session, user.id)
    )
    if record is None:
        return False
    if verification_deadline_passed(record.deadline_at):
        # The sweeper will kick shortly; a fresh button would be useless.
        return False
    provider = normalize_verification_provider(record.provider)
    if not verification_service_ready(settings, provider):
        return False

    # Reaching the private chat is when the interactive solve actually starts;
    # the original deadline started at join/challenge time and may already be
    # mostly consumed by the group-prompt hop. Restart the window here so an
    # hCaptcha image challenge cannot be raced by the sweeper mid-solve.
    # Capped at 3x the timeout since issuance: repeated /start must not defer
    # the kick/ban forever.
    timeout_seconds = verification_timeout_seconds_for_kind(settings, record.kind)
    now = now_shanghai_naive()
    lifetime_cap = (record.created_at or now) + timedelta(seconds=3 * timeout_seconds)
    fresh_deadline = min(now + timedelta(seconds=timeout_seconds), lifetime_cap)
    shown_deadline = record.deadline_at
    if fresh_deadline > record.deadline_at:
        # Targeted UPDATE instead of mutating the ORM row: a concurrent pass
        # may have consumed the record, and 0 matched rows must not raise.
        result = await session.execute(
            update(JoinVerification)
            .where(
                JoinVerification.id == record.id,
                JoinVerification.deadline_at < fresh_deadline,
            )
            .values(deadline_at=fresh_deadline)
        )
        await session.commit()
        if result.rowcount:
            shown_deadline = fresh_deadline

    await message.answer(
        build_private_challenge_text(
            deadline_at=shown_deadline,
            kind=record.kind,
            reason=record.reason,
        ),
        parse_mode="HTML",
        reply_markup=build_private_challenge_keyboard(
            settings,
            provider,
            int(record.id),
        ),
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
        settings: Settings | None = None,
    ) -> None:
        self.bot = bot
        self.session_factory = session_factory
        self.check_interval_seconds = max(5.0, float(check_interval_seconds))
        self.settings = settings
        self._paused_providers: set[str] = set()

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
            unavailable_providers: set[str] = set()
            if self.settings is not None:
                for provider in sorted(VERIFICATION_PROVIDERS):
                    if verification_service_ready(self.settings, provider):
                        continue
                    unavailable_providers.add(provider)
                    extended = await extend_pending_verification_deadlines(
                        session,
                        settings=self.settings,
                        now=now,
                        provider=provider,
                    )
                    if extended and provider not in self._paused_providers:
                        log.warning(
                            "verification timeout enforcement paused | provider=%s reason=%s "
                            "pending_extended=%d",
                            provider,
                            turnstile_runtime_configuration_issue(self.settings, provider)
                            or f"{provider} 配置不完整",
                            extended,
                        )
                resumed = self._paused_providers - unavailable_providers
                for provider in sorted(resumed):
                    log.info(
                        "verification timeout enforcement resumed | provider=%s",
                        provider,
                    )
                self._paused_providers = unavailable_providers
                await session.commit()
            expired = await list_expired_verifications(session, now=now)
            if not expired:
                return 0
            claimed: list[tuple[JoinVerification, _GroupBanState, bool]] = []
            for record in expired:
                record_provider = normalize_verification_provider(record.provider)
                if (
                    record_provider in unavailable_providers
                    or self.settings is not None
                    and not verification_service_ready(self.settings, record_provider)
                ):
                    if self.settings is not None:
                        timeout_seconds = verification_timeout_seconds_for_kind(
                            self.settings, record.kind
                        )
                        record.deadline_at = now + timedelta(seconds=timeout_seconds)
                    continue
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
                protected_owner = bool(
                    record.kind == VERIFICATION_KIND_MODERATION
                    and self.settings is not None
                    and is_super_admin_user_id(record.user_id, self.settings)
                )
                ban_state = None
                if record.kind == VERIFICATION_KIND_MODERATION and not protected_owner:
                    ban_state = await mark_group_banned(
                        session,
                        record.group_id,
                        record.user_id,
                    )
                claimed.append((record, ban_state, protected_owner))
            # Persist the terminal claim before Telegram calls. A failed
            # moderation ban is compensated below and the challenge is requeued.
            await session.commit()

        for record, ban_state, protected_owner in claimed:
            log.info(
                "verification timeout | kind=%s group=%s user=%s",
                record.kind,
                record.group_id,
                record.user_id,
            )
            if protected_owner:
                restored = await restore_member_permissions(
                    self.bot,
                    record.group_id,
                    record.user_id,
                )
                if not restored:
                    async with self.session_factory() as session:
                        await upsert_join_verification(
                            session,
                            group_id=record.group_id,
                            user_id=record.user_id,
                            deadline_at=now_shanghai_naive() + timedelta(minutes=5),
                            kind=record.kind,
                            provider=record.provider,
                            reason=record.reason,
                            display_name=record.display_name,
                            prompt_message_id=record.prompt_message_id,
                        )
                        await session.commit()
                    continue
                await self._finalize_prompt(
                    record,
                    "✅ 最高管理员无需消息审查验证，发言权限已恢复。",
                )
                continue
            if record.kind == VERIFICATION_KIND_MODERATION:
                enforced = await ban_member(self.bot, record.group_id, record.user_id)
                if not enforced:
                    async with self.session_factory() as session:
                        rolled_back = await rollback_group_ban(
                            session,
                            record.group_id,
                            record.user_id,
                            ban_state,
                        )
                        if rolled_back and not (ban_state and ban_state[1]):
                            timeout_seconds = (
                                verification_timeout_seconds_for_kind(
                                    self.settings, record.kind
                                )
                                if self.settings is not None
                                else 300
                            )
                            await upsert_join_verification(
                                session,
                                group_id=record.group_id,
                                user_id=record.user_id,
                                deadline_at=now_shanghai_naive()
                                + timedelta(seconds=max(60, int(timeout_seconds))),
                                kind=record.kind,
                                provider=record.provider,
                                reason=record.reason,
                                display_name=record.display_name,
                                prompt_message_id=record.prompt_message_id,
                            )
                        await session.commit()
                    log.warning(
                        "moderation verification timeout ban failed | group=%s user=%s "
                        "state_restored=%s",
                        record.group_id,
                        record.user_id,
                        rolled_back,
                    )
                    continue
                text = "⏰ 消息审查验证超时，已封禁。请联系管理员处理。"
            else:
                enforced = await kick_member(self.bot, record.group_id, record.user_id)
                if not enforced:
                    async with self.session_factory() as session:
                        timeout_seconds = (
                            verification_timeout_seconds_for_kind(
                                self.settings, record.kind
                            )
                            if self.settings is not None
                            else 300
                        )
                        await upsert_join_verification(
                            session,
                            group_id=record.group_id,
                            user_id=record.user_id,
                            deadline_at=now_shanghai_naive()
                            + timedelta(seconds=max(60, int(timeout_seconds))),
                            kind=record.kind,
                            provider=record.provider,
                            reason=record.reason,
                            display_name=record.display_name,
                            prompt_message_id=record.prompt_message_id,
                        )
                        await session.commit()
                    log.warning(
                        "join verification timeout kick failed; requeued | "
                        "group=%s user=%s",
                        record.group_id,
                        record.user_id,
                    )
                    continue
                if record.kind == VERIFICATION_KIND_PATROL:
                    text = "⏰ 资料巡检质询超时，已移出群聊（未封禁，可重新加入）。"
                else:
                    text = "⏰ 验证超时，已移出群聊。可重新加入再次验证。"
            await self._finalize_prompt(record, text)
        return len(claimed)

    async def _finalize_prompt(self, record: JoinVerification, text: str) -> None:
        # A patrol prompt is shared by several violators; editing it would
        # remove the warning and its challenge button for the others, so post
        # the outcome as a separate message instead.
        if record.kind == VERIFICATION_KIND_PATROL:
            shown = html.escape(
                (record.display_name or "").strip() or str(record.user_id)
            )
            try:
                await self.bot.send_message(
                    record.group_id,
                    f"<b>{shown}</b>：{text}",
                    parse_mode="HTML",
                )
            except Exception:
                log.debug(
                    "patrol timeout notice failed | group=%s user=%s",
                    record.group_id,
                    record.user_id,
                )
            return
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
