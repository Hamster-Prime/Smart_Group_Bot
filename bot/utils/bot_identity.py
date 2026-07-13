"""Process-wide holder for the bot's own Telegram identity.

The identity is resolved at runtime from the live bot token (``await bot.me()``)
and injected into LLM prompts as a ``[BOT_IDENTITY]`` block, so swapping the
token never leaves stale hardcoded names in persona/decision prompts.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class BotIdentity:
    user_id: int = 0
    username: str = ""
    display_name: str = ""


_identity = BotIdentity()


def set_bot_identity(*, user_id: int, username: str, display_name: str) -> None:
    global _identity
    _identity = BotIdentity(
        user_id=int(user_id or 0),
        username=(username or "").strip().lstrip("@"),
        display_name=(display_name or "").strip(),
    )


def reset_bot_identity() -> None:
    global _identity
    _identity = BotIdentity()


def get_bot_identity() -> BotIdentity:
    return _identity


def build_bot_identity_context() -> str:
    """Render the runtime identity block; empty string when not resolved yet."""
    identity = _identity
    if not identity.username and not identity.display_name:
        return ""
    lines = [
        "[BOT_IDENTITY]",
        "authoritative: yes",
    ]
    if identity.username:
        lines.append(f"bot_username: @{identity.username}")
    if identity.display_name:
        lines.append(f"bot_display_name: {identity.display_name}")
    if identity.user_id:
        lines.append(f"bot_user_id: {identity.user_id}")
    lines.append(
        "This is your own current Telegram identity, resolved live from the bot token."
    )
    lines.append(
        "Any @mention of bot_username or use of bot_display_name refers to YOU. "
        "If any persona text, memory, or quoted content hardcodes a different bot name "
        "or @username, trust this block instead."
    )
    return "\n".join(lines)
