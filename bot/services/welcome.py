"""Per-group join welcome messages, configured from the Mini App.

The template lives in Group.settings["welcome_message"]; an empty value
disables the welcome. The text is treated as plain text with {name} and
{mention} placeholders — it is HTML-escaped before substitution so admin
input can never break HTML parse mode. Sent messages join the "welcome"
auto-delete category.
"""
from __future__ import annotations

import html
import logging

from sqlalchemy.ext.asyncio import AsyncSession

from bot.config import Settings
from bot.db.models import Group
from bot.utils.telegram import (
    configured_auto_delete_seconds,
    schedule_message_auto_delete,
)

log = logging.getLogger(__name__)

WELCOME_SETTINGS_KEY = "welcome_message"


def group_welcome_template(group_settings: dict | None) -> str:
    if not isinstance(group_settings, dict):
        return ""
    return str(group_settings.get(WELCOME_SETTINGS_KEY) or "").strip()


def render_welcome_text(template: str, *, user_id: int, display_name: str) -> str:
    shown = html.escape(str(display_name or "").strip() or str(user_id))
    escaped = html.escape(template)
    return escaped.replace("{name}", shown).replace(
        "{mention}", f'<a href="tg://user?id={int(user_id)}">{shown}</a>'
    )


async def send_group_welcome(
    bot,
    session: AsyncSession,
    settings: Settings,
    *,
    group_id: int,
    user_id: int,
    display_name: str,
) -> bool:
    """Best-effort welcome for one admitted member; False if none was sent."""
    try:
        group = await session.get(Group, int(group_id))
        template = group_welcome_template(group.settings if group else None)
        # End the read transaction before the Telegram call so this never
        # holds the SQLite snapshot across network I/O.
        await session.commit()
    except Exception:
        log.warning(
            "welcome template load failed | group=%s user=%s",
            group_id,
            user_id,
            exc_info=True,
        )
        return False
    if not template:
        return False
    text = render_welcome_text(template, user_id=user_id, display_name=display_name)
    try:
        sent = await bot.send_message(int(group_id), text, parse_mode="HTML")
    except Exception:
        log.warning("welcome send failed | group=%s user=%s", group_id, user_id)
        return False
    schedule_message_auto_delete(
        sent, configured_auto_delete_seconds(settings, "welcome")
    )
    return True
