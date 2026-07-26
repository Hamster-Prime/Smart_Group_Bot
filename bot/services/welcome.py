"""Per-group Markdown welcome messages, configured from the Mini App."""
from __future__ import annotations

import html
import logging

from sqlalchemy.ext.asyncio import AsyncSession

from bot.config import Settings
from bot.db.models import Group
from bot.services.message_templates import (
    build_template_keyboard,
    normalize_template_buttons,
    render_markdown_html,
    render_plain_template,
    send_template_with_fallback,
)
from bot.utils.telegram import (
    configured_auto_delete_seconds,
    schedule_message_auto_delete_durable,
)

log = logging.getLogger(__name__)

WELCOME_SETTINGS_KEY = "welcome_message"
WELCOME_BUTTONS_SETTINGS_KEY = "welcome_buttons"
WELCOME_DISABLE_LINK_PREVIEW_SETTINGS_KEY = "welcome_disable_link_preview"


def group_welcome_template(group_settings: dict | None) -> str:
    if not isinstance(group_settings, dict):
        return ""
    return str(group_settings.get(WELCOME_SETTINGS_KEY) or "").strip()


def group_welcome_buttons(group_settings: dict | None) -> list[dict]:
    if not isinstance(group_settings, dict):
        return []
    try:
        return normalize_template_buttons(
            group_settings.get(WELCOME_BUTTONS_SETTINGS_KEY)
        )
    except ValueError:
        return []


def group_welcome_disables_link_preview(group_settings: dict | None) -> bool:
    if not isinstance(group_settings, dict):
        return True
    return bool(
        group_settings.get(WELCOME_DISABLE_LINK_PREVIEW_SETTINGS_KEY, True)
    )


def render_welcome_text(template: str, *, user_id: int, display_name: str) -> str:
    shown = html.escape(str(display_name or "").strip() or str(user_id))
    return render_markdown_html(
        template,
        replacements={
            "{name}": shown,
            "{mention}": f'<a href="tg://user?id={int(user_id)}">{shown}</a>',
        },
    )


def render_welcome_plain(template: str, *, user_id: int, display_name: str) -> str:
    shown = str(display_name or "").strip() or str(user_id)
    return render_plain_template(
        template,
        replacements={"{name}": shown, "{mention}": shown},
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
        group_settings = group.settings if group else None
        template = group_welcome_template(group_settings)
        buttons = group_welcome_buttons(group_settings)
        disable_link_preview = group_welcome_disables_link_preview(group_settings)
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
    plain_text = render_welcome_plain(
        template,
        user_id=user_id,
        display_name=display_name,
    )
    keyboard = build_template_keyboard(buttons)
    try:
        sent = await send_template_with_fallback(
            lambda body, **kwargs: bot.send_message(
                int(group_id),
                body,
                **kwargs,
            ),
            formatted_text=text,
            plain_text=plain_text,
            reply_markup=keyboard,
            disable_link_preview=disable_link_preview,
        )
    except Exception:
        log.warning("welcome send failed | group=%s user=%s", group_id, user_id)
        return False
    await schedule_message_auto_delete_durable(
        sent, configured_auto_delete_seconds(settings, "welcome")
    )
    return True


__all__ = [
    "WELCOME_BUTTONS_SETTINGS_KEY",
    "WELCOME_DISABLE_LINK_PREVIEW_SETTINGS_KEY",
    "WELCOME_SETTINGS_KEY",
    "group_welcome_buttons",
    "group_welcome_disables_link_preview",
    "group_welcome_template",
    "render_welcome_plain",
    "render_welcome_text",
    "send_group_welcome",
]
