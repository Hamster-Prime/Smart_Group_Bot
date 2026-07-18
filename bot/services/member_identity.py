"""Small helpers for consistent member identity documents in the Mini App."""
from __future__ import annotations

from typing import Any


def member_display_name(
    user_id: int,
    *,
    full_name: object = "",
    username: object = "",
) -> str:
    """Prefer a visible name, then @username, with Telegram ID as fallback."""
    clean_name = str(full_name or "").strip()
    clean_username = str(username or "").strip().lstrip("@")
    return clean_name or (
        f"@{clean_username}" if clean_username else str(int(user_id))
    )


def member_identity_document(
    user_id: int,
    *,
    full_name: object = "",
    username: object = "",
) -> dict[str, Any]:
    """Return the stable identity fields shared by policy-list responses."""
    clean_name = str(full_name or "").strip()
    clean_username = str(username or "").strip().lstrip("@")
    return {
        "user_id": int(user_id),
        "full_name": clean_name,
        "username": clean_username,
        "display_name": member_display_name(
            int(user_id),
            full_name=clean_name,
            username=clean_username,
        ),
    }
