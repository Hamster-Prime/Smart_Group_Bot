"""HTTP helpers and routes for the Telegram Mini App."""

from __future__ import annotations

from typing import Any

__all__ = ["register_settings_routes", "require_super_admin"]


def __getattr__(name: str) -> Any:
    # Keep auth-only imports lightweight; settings routes also load the
    # proactive task helpers and their service dependencies.
    if name == "require_super_admin":
        from bot.web.auth import require_super_admin

        return require_super_admin
    if name == "register_settings_routes":
        from bot.web.settings_api import register_settings_routes

        return register_settings_routes
    raise AttributeError(name)
