"""Call-admin: "@admin" in a group pings the group's administrators.

Any member may send "@admin" (optionally followed by a reason) to summon the
admins — e.g. to report spam the bot missed. The notice @-mentions the
selected admins with real tg://user links so it must be sent directly via
bot.send_message (answer_with_auto_delete would sanitize the mentions away);
it joins the "call_admin" auto-delete category.

Per-group configuration lives in Group.settings:
- call_admin_enabled: None inherits the global default.
- call_admin_targets: list of admin user_ids to mention; missing/empty means
  "all current admins" (the Mini App default of every box checked).
A short per-group cooldown stops @admin floods from re-pinging everyone.
"""
from __future__ import annotations

import html
import logging
import re
import time

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from bot.config import Settings
from bot.db.models import Admin, GroupMember
from bot.services.message_templates import (
    card_field,
    render_summary_notice,
    truncate_telegram_text,
)
from bot.utils.telegram import (
    configured_auto_delete_seconds,
    is_reply_target_missing_error,
    schedule_message_auto_delete_durable,
)

log = logging.getLogger(__name__)

CALL_ADMIN_ENABLED_KEY = "call_admin_enabled"
CALL_ADMIN_TARGETS_KEY = "call_admin_targets"

# Standalone "@admin"/"@admins" token (any case), optionally with more text.
# The lookbehind also excludes "/" and "." so URL paths (t.me/@admin) and
# email-ish text never summon anyone.
_CALL_ADMIN_RE = re.compile(
    r"(?<![A-Za-z0-9_/.])@admins?(?![A-Za-z0-9_])", re.IGNORECASE
)

# Keep the single notice comfortably below Telegram's text limit even when
# administrators use maximum-length display names. Usernames are already
# short; full names and stale roster values are compacted before rendering.
_ADMIN_MENTION_LABEL_LIMIT = 32

_last_call_at: dict[int, float] = {}


def is_call_admin_trigger(text: str) -> bool:
    return bool(_CALL_ADMIN_RE.search(str(text or "")))


def call_admin_policy(settings: Settings, group_settings: dict | None) -> bool:
    """Per-group call_admin_enabled override; None inherits the global."""
    if isinstance(group_settings, dict) and group_settings.get(CALL_ADMIN_ENABLED_KEY) is not None:
        return bool(group_settings.get(CALL_ADMIN_ENABLED_KEY))
    return bool(getattr(settings, "call_admin_enabled", True))


def call_admin_targets(group_settings: dict | None) -> set[int]:
    """Selected admin ids; empty set means "mention all admins"."""
    if not isinstance(group_settings, dict):
        return set()
    raw = group_settings.get(CALL_ADMIN_TARGETS_KEY)
    if not isinstance(raw, list):
        return set()
    targets: set[int] = set()
    for item in raw:
        try:
            value = int(item)
        except (TypeError, ValueError):
            continue
        if value > 0:
            targets.add(value)
    return targets


def _cooldown_remaining(group_id: int, cooldown_seconds: int) -> float:
    if cooldown_seconds <= 0:
        return 0.0
    last = _last_call_at.get(int(group_id))
    if last is None:
        return 0.0
    remaining = cooldown_seconds - (time.monotonic() - last)
    return max(0.0, remaining)


def _claim_call(group_id: int) -> float:
    """Claim the cooldown slot; returns the claim timestamp for rollback."""
    if len(_last_call_at) > 2048:
        _last_call_at.clear()
    stamp = time.monotonic()
    _last_call_at[int(group_id)] = stamp
    return stamp


def _release_call(group_id: int, stamp: float) -> None:
    """Undo a claim after a failed summon, unless re-claimed meanwhile."""
    if _last_call_at.get(int(group_id)) == stamp:
        _last_call_at.pop(int(group_id), None)


def _break_user_mentions(text: str) -> str:
    """Neutralize @tokens in user-provided text so the raw-mention send path
    cannot be laundered into pinging arbitrary users."""
    return re.sub(r"@(?=\w)", "@​", str(text or ""))


def _compact_admin_label(value: object, *, fallback: str) -> str:
    """Keep one all-admin notice within Telegram's single-message budget."""
    label = truncate_telegram_text(
        str(value or "").strip(), _ADMIN_MENTION_LABEL_LIMIT
    )
    return label or fallback


async def _resolve_admin_mentions(
    bot: Bot,
    session: AsyncSession,
    group_id: int,
    targets: set[int],
) -> list[str]:
    """HTML mentions for the group's admins, filtered by the selection.

    Live Telegram admins are the source of truth (an admin selected in the
    Mini App but since demoted is dropped); the bot itself is excluded.
    Display names come from the live member objects, falling back to the
    roster and the local Admin table when Telegram is unreachable.
    """
    admins: dict[int, str] = {}
    try:
        me_id = int((await bot.me()).id)
    except Exception:
        me_id = 0
    try:
        for member in await bot.get_chat_administrators(int(group_id)) or []:
            user = getattr(member, "user", None)
            if user is None or getattr(user, "is_bot", False):
                continue
            user_id = int(user.id)
            if user_id == me_id:
                continue
            username = str(getattr(user, "username", "") or "").strip()
            if username:
                label = _compact_admin_label(f"@{username}", fallback=str(user_id))
            else:
                label = _compact_admin_label(
                    getattr(user, "full_name", ""), fallback=str(user_id)
                )
            admins[user_id] = label
    except Exception:
        log.warning(
            "call admin: get_chat_administrators failed | group=%s",
            group_id,
            exc_info=True,
        )
        # Fallback: authorized-admin table joined with the roster names.
        rows = (
            await session.execute(
                select(Admin.user_id, GroupMember.full_name, GroupMember.username)
                .outerjoin(
                    GroupMember,
                    (GroupMember.group_id == Admin.group_id)
                    & (GroupMember.user_id == Admin.user_id),
                )
                .where(Admin.group_id == int(group_id))
            )
        ).all()
        for user_id, full_name, username in rows:
            user_id = int(user_id)
            clean_username = str(username or "").strip()
            label = (
                _compact_admin_label(f"@{clean_username}", fallback=str(user_id))
                if clean_username
                else _compact_admin_label(full_name, fallback=str(user_id))
            )
            admins[user_id] = label

    if targets:
        admins = {uid: label for uid, label in admins.items() if uid in targets}

    return [
        f'<a href="tg://user?id={uid}">{html.escape(label)}</a>'
        for uid, label in sorted(admins.items())
    ]


def build_call_admin_text(
    mentions: list[str],
    *,
    caller_id: int,
    caller_name: str,
    reason: str,
    reported_text: str,
) -> str:
    shown = html.escape(str(caller_name or "").strip() or str(caller_id))
    details: list[str] = []
    clean_reason = str(reason or "").strip()
    if clean_reason:
        details.append(
            card_field("补充说明", html.escape(_break_user_mentions(clean_reason[:200])))
        )
    clean_reported = str(reported_text or "").strip()
    if clean_reported:
        details.append(
            card_field("被举报消息", html.escape(_break_user_mentions(clean_reported[:200])))
        )
    return render_summary_notice(
        "呼叫管理员 · 需要处理",
        " ".join(mentions),
        context=card_field(
            "发起人", f'<a href="tg://user?id={int(caller_id)}">{shown}</a>'
        ),
        details=details,
    )


async def handle_call_admin(
    message,
    session: AsyncSession,
    settings: Settings,
    *,
    group_settings: dict | None,
    caller_id: int,
    caller_name: str,
) -> bool:
    """Send the admin summon for one "@admin" message; False if not sent."""
    group_id = int(message.chat.id)
    if not call_admin_policy(settings, group_settings):
        return False

    cooldown = int(getattr(settings, "call_admin_cooldown_seconds", 60) or 0)
    remaining = _cooldown_remaining(group_id, cooldown)
    if remaining > 0:
        log.info(
            "[%s] call admin suppressed by cooldown | user=%s remaining=%.0fs",
            group_id,
            caller_id,
            remaining,
        )
        return False
    # Claim before the admin lookup: concurrent "@admin" messages awaiting
    # get_chat_administrators must not all pass the check above.
    claim_stamp = _claim_call(group_id)

    # This helper is invoked from the main message handler after several local
    # authorization/settings reads. Release that snapshot before Telegram's
    # potentially slow administrator lookup.
    try:
        await session.commit()
    except Exception:
        _release_call(group_id, claim_stamp)
        log.warning(
            "[%s] call admin transaction release failed before lookup",
            group_id,
            exc_info=True,
        )
        return False

    mentions = await _resolve_admin_mentions(
        message.bot, session, group_id, call_admin_targets(group_settings)
    )
    # The Telegram fallback path may have queried the local Admin/roster
    # tables. End that fresh read transaction before sending the notification
    # so it cannot remain checked out across network retries.
    try:
        await session.commit()
    except Exception:
        _release_call(group_id, claim_stamp)
        log.warning(
            "[%s] call admin transaction release failed after lookup",
            group_id,
            exc_info=True,
        )
        return False
    if not mentions:
        _release_call(group_id, claim_stamp)
        log.info("[%s] call admin skipped: no admins to mention", group_id)
        return False

    reason = _CALL_ADMIN_RE.sub("", str(message.text or message.caption or "")).strip()
    reply = getattr(message, "reply_to_message", None)
    reported_text = str(getattr(reply, "text", None) or getattr(reply, "caption", None) or "")
    reply_target = int(getattr(reply, "message_id", 0) or 0) or None
    text = build_call_admin_text(
        mentions,
        caller_id=caller_id,
        caller_name=caller_name,
        reason=reason,
        reported_text=reported_text,
    )
    try:
        try:
            sent = await message.bot.send_message(
                group_id,
                text,
                parse_mode="HTML",
                reply_to_message_id=reply_target,
            )
        except TelegramBadRequest as exc:
            # Only retry unanchored when the reported message is gone; a
            # blanket retry would double-ping on transient errors.
            if reply_target is None or not is_reply_target_missing_error(str(exc)):
                raise
            sent = await message.bot.send_message(group_id, text, parse_mode="HTML")
    except Exception:
        _release_call(group_id, claim_stamp)
        log.warning(
            "[%s] call admin send failed",
            group_id,
            exc_info=True,
        )
        return False
    await schedule_message_auto_delete_durable(
        sent, configured_auto_delete_seconds(settings, "call_admin")
    )
    return True
