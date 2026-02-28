from __future__ import annotations

from datetime import datetime, timezone


def build_current_time_context() -> str:
    """Build real-time clock context for LLM prompts."""
    now_local = datetime.now().astimezone()
    now_utc = datetime.now(timezone.utc)
    tz_name = now_local.tzname() or "local"
    offset_raw = now_local.strftime("%z")
    if len(offset_raw) == 5:
        offset = f"{offset_raw[:3]}:{offset_raw[3:]}"
    else:
        offset = offset_raw or "+00:00"

    return (
        "[CURRENT_TIME]\n"
        f"local_datetime: {now_local.strftime('%Y-%m-%d %H:%M:%S')}\n"
        f"local_weekday: {now_local.strftime('%A')}\n"
        f"timezone: {tz_name} (UTC{offset})\n"
        f"utc_datetime: {now_utc.strftime('%Y-%m-%d %H:%M:%S')}\n"
        "If user asks about current time/date/today/tomorrow, use this block as the authoritative source."
    )
