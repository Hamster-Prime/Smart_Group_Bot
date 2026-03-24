from __future__ import annotations

from typing import Any

AT_REPLY_MODE_KEY = "at_reply_mode"


def is_at_reply_enabled(value: Any, *, default: bool = False) -> bool:
    if isinstance(value, dict):
        value = value.get(AT_REPLY_MODE_KEY, default)

    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"1", "true", "yes", "on", "enable", "enabled"}:
            return True
        if normalized in {"0", "false", "no", "off", "disable", "disabled"}:
            return False
    return bool(value)


def set_at_reply_enabled(group_settings: dict | None, enabled: bool) -> dict[str, Any]:
    settings_data = dict(group_settings or {})
    if enabled:
        settings_data[AT_REPLY_MODE_KEY] = True
    else:
        settings_data.pop(AT_REPLY_MODE_KEY, None)
    return settings_data


def build_at_reply_status_text(*, group_id: int, group_settings: dict | None) -> str:
    enabled = is_at_reply_enabled(group_settings)
    state_text = "已开启" if enabled else "已关闭"
    detail_text = (
        "仅在显式 @bot 时必回复；未 @ 时仍会照常审核、写入记忆，但回复阶段按 skip 跳过。"
        if enabled
        else "当前保持默认回复策略，不影响现有决策逻辑。"
    )
    return (
        "<b>仅@回复设置</b>\n"
        f"<b>群ID</b>: {group_id}\n"
        f"<b>状态</b>: {state_text}\n"
        f"<b>说明</b>: {detail_text}"
    )
