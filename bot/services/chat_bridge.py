from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any

from bot.services.llm import LLMService
from bot.utils.conversation_context import (
    build_current_turn_focus_context,
    format_recent_group_context,
)
from bot.utils.prompts import CHAT_BRIDGE_SYSTEM, with_persona
from bot.utils.runtime_context import build_bot_runtime_profile_context, build_current_time_context
from bot.utils.security import (
    build_defended_system,
    clean_multiline_text,
    contains_prompt_injection,
    sanitize_history_for_llm,
    wrap_untrusted_multiline,
)

log = logging.getLogger(__name__)

CHAT_BRIDGE_ENABLED_KEY = "chat_bridge_enabled"
CHAT_BRIDGE_TARGET_USERNAME_KEY = "chat_bridge_target_username"
CHAT_BRIDGE_PENDING_ADMIN_ID_KEY = "chat_bridge_pending_admin_id"
CHAT_BRIDGE_PROMPT_MESSAGE_ID_KEY = "chat_bridge_prompt_message_id"

_USERNAME_TOKEN_RE = re.compile(r"^\s*@([A-Za-z][A-Za-z0-9_]{4,31})\s*$")
_INCOMING_CHAT_BRIDGE_RE = re.compile(
    r"^\s*/chat@([A-Za-z][A-Za-z0-9_]{4,31})\s+(.+?)\s*$",
    flags=re.DOTALL,
)
_BOT_SUFFIX_STRIP_RE = re.compile(r"[^a-z0-9]+")


@dataclass(frozen=True, slots=True)
class ChatBridgeState:
    enabled: bool
    target_username: str = ""
    pending_admin_id: int = 0
    prompt_message_id: int = 0

    @property
    def waiting_for_target(self) -> bool:
        return self.enabled and self.pending_admin_id > 0

    @property
    def active(self) -> bool:
        return self.enabled and bool(self.target_username)


@dataclass(frozen=True, slots=True)
class ChatBridgeIncoming:
    target_username: str
    body: str


def _as_int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def normalize_chat_bridge_username(value: str) -> str:
    raw = (value or "").strip().lstrip("@").strip()
    if not raw:
        return ""
    if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{4,31}", raw):
        return ""
    return raw.lower()


def extract_chat_bridge_target_username(text: str) -> str:
    match = _USERNAME_TOKEN_RE.fullmatch(text or "")
    if not match:
        return ""
    return normalize_chat_bridge_username(match.group(1))


def parse_incoming_chat_bridge_message(text: str) -> ChatBridgeIncoming | None:
    match = _INCOMING_CHAT_BRIDGE_RE.fullmatch(text or "")
    if not match:
        return None
    target_username = normalize_chat_bridge_username(match.group(1))
    body = clean_multiline_text(match.group(2), max_len=600)
    if not target_username or not body:
        return None
    return ChatBridgeIncoming(target_username=target_username, body=body)


def is_bot_style_name(*values: str) -> bool:
    for value in values:
        compact = _BOT_SUFFIX_STRIP_RE.sub("", (value or "").strip().lower())
        if compact.endswith("bot") and len(compact) >= 4:
            return True
    return False


def strip_chat_bridge_prefix(text: str) -> str:
    incoming = parse_incoming_chat_bridge_message(text)
    if incoming is not None:
        return incoming.body
    return clean_multiline_text(text, max_len=600)


def compose_chat_bridge_message(target_username: str, body: str) -> str:
    target = normalize_chat_bridge_username(target_username)
    payload = strip_chat_bridge_prefix(body)
    if not target or not payload:
        return ""
    return f"/chat@{target} {payload}"


def get_chat_bridge_state(group_settings: dict | None) -> ChatBridgeState:
    data = dict(group_settings or {})
    enabled = bool(data.get(CHAT_BRIDGE_ENABLED_KEY, False))
    target_username = normalize_chat_bridge_username(data.get(CHAT_BRIDGE_TARGET_USERNAME_KEY, ""))
    pending_admin_id = _as_int(data.get(CHAT_BRIDGE_PENDING_ADMIN_ID_KEY, 0))
    prompt_message_id = _as_int(data.get(CHAT_BRIDGE_PROMPT_MESSAGE_ID_KEY, 0))
    return ChatBridgeState(
        enabled=enabled,
        target_username=target_username,
        pending_admin_id=pending_admin_id,
        prompt_message_id=prompt_message_id,
    )


def set_chat_bridge_enabled(group_settings: dict | None, enabled: bool) -> dict[str, Any]:
    settings_data = dict(group_settings or {})
    if enabled:
        settings_data[CHAT_BRIDGE_ENABLED_KEY] = True
        return settings_data

    settings_data.pop(CHAT_BRIDGE_ENABLED_KEY, None)
    settings_data.pop(CHAT_BRIDGE_TARGET_USERNAME_KEY, None)
    settings_data.pop(CHAT_BRIDGE_PENDING_ADMIN_ID_KEY, None)
    settings_data.pop(CHAT_BRIDGE_PROMPT_MESSAGE_ID_KEY, None)
    return settings_data


def begin_chat_bridge_target_selection(
    group_settings: dict | None,
    *,
    admin_id: int,
    prompt_message_id: int = 0,
) -> dict[str, Any]:
    settings_data = set_chat_bridge_enabled(group_settings, True)
    settings_data.pop(CHAT_BRIDGE_TARGET_USERNAME_KEY, None)
    settings_data[CHAT_BRIDGE_PENDING_ADMIN_ID_KEY] = max(0, int(admin_id or 0))
    if prompt_message_id > 0:
        settings_data[CHAT_BRIDGE_PROMPT_MESSAGE_ID_KEY] = int(prompt_message_id)
    else:
        settings_data.pop(CHAT_BRIDGE_PROMPT_MESSAGE_ID_KEY, None)
    return settings_data


def set_chat_bridge_target(group_settings: dict | None, target_username: str) -> dict[str, Any]:
    normalized = normalize_chat_bridge_username(target_username)
    settings_data = set_chat_bridge_enabled(group_settings, True)
    if normalized:
        settings_data[CHAT_BRIDGE_TARGET_USERNAME_KEY] = normalized
    else:
        settings_data.pop(CHAT_BRIDGE_TARGET_USERNAME_KEY, None)
    settings_data.pop(CHAT_BRIDGE_PENDING_ADMIN_ID_KEY, None)
    settings_data.pop(CHAT_BRIDGE_PROMPT_MESSAGE_ID_KEY, None)
    return settings_data


def build_chat_bridge_status_text(*, group_id: int, group_settings: dict | None) -> str:
    state = get_chat_bridge_state(group_settings)
    if not state.enabled:
        detail = "当前关闭。使用 /chat enable 后，最高管理员回复目标 bot 用户名即可开始。"
        status = "已关闭"
    elif state.waiting_for_target:
        detail = "已开启，正在等待最高管理员回复目标 bot 用户名，例如 @examplebot。"
        status = "等待目标"
    elif state.target_username:
        detail = f"已开启，当前对话目标为 @{state.target_username}。"
        status = "已开启"
    else:
        detail = "已开启，但尚未设置目标 bot。可再次发送 /chat enable 重新指定。"
        status = "未指定目标"

    return (
        "<b>/chat 对话设置</b>\n"
        f"<b>群ID</b>: {group_id}\n"
        f"<b>状态</b>: {status}\n"
        f"<b>说明</b>: {detail}"
    )


class ChatBridgeService:
    def __init__(self, llm: LLMService, *, settings: Any | None = None) -> None:
        self.llm = llm
        self.settings = settings

    @staticmethod
    def _normalize_input_text(text: str) -> tuple[str, int]:
        input_limit = 600
        return clean_multiline_text(text, max_len=input_limit), input_limit

    @staticmethod
    def _build_mode_context(*, mode: str) -> str:
        normalized_mode = "start" if mode == "start" else "reply"
        return (
            "[CHAT_BRIDGE_MODE]\n"
            f"mode: {normalized_mode}\n"
            "The caller will prepend the final /chat@peer_bot_username prefix.\n"
            "Only produce the message body."
        )

    @staticmethod
    def _build_peer_context(peer_username: str) -> str:
        shown = normalize_chat_bridge_username(peer_username)
        if not shown:
            shown = "unknownbot"
        return (
            "[PEER_BOT]\n"
            f"username: @{shown}\n"
            "Assume this peer is another Telegram bot in the same group."
        )

    def build_prompt_payload(
        self,
        *,
        mode: str,
        peer_username: str,
        current_message: str,
        history: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        normalized_text, input_limit = self._normalize_input_text(current_message)
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": build_defended_system(with_persona(CHAT_BRIDGE_SYSTEM))},
        ]

        if history:
            messages.extend(sanitize_history_for_llm(history, max_items=len(history)))
        recent_context = format_recent_group_context(history, max_items=8)
        if recent_context:
            messages.append({"role": "system", "content": recent_context})
        messages.append({"role": "system", "content": build_current_time_context()})
        messages.append(
            {
                "role": "system",
                "content": build_bot_runtime_profile_context(self.llm, settings=self.settings),
            }
        )
        messages.append({"role": "system", "content": self._build_mode_context(mode=mode)})
        messages.append({"role": "system", "content": self._build_peer_context(peer_username)})

        focus_seed = normalized_text
        if mode == "start" and not focus_seed:
            focus_seed = "结合最近群聊，为另一个 bot 生成一句自然的开场问题。"
        focus_context = build_current_turn_focus_context(
            focus_seed,
            merged_count=1,
            merged_context="",
        )
        if focus_context:
            focus_context = focus_context.replace("[CURRENT_TURN_FOCUS]", "[CURRENT_TURN_FOCUS]")
            messages.append({"role": "system", "content": focus_context})

        if mode == "start":
            user_text = (
                "请结合最近群聊，给目标 bot 发出一句自然、简短、能继续聊下去的开场问题。"
            )
        else:
            user_text = normalized_text

        messages.append(
            {
                "role": "user",
                "content": (
                    "[CURRENT_BRIDGE_MESSAGE]\n"
                    + wrap_untrusted_multiline(
                        "current_bridge_message",
                        user_text,
                        max_len=input_limit,
                    )
                ),
            }
        )
        return {"messages": messages}

    async def reply(
        self,
        *,
        mode: str,
        peer_username: str,
        current_message: str,
        history: list[dict[str, Any]] | None = None,
    ) -> str:
        normalized_text, _ = self._normalize_input_text(current_message)
        if normalized_text and contains_prompt_injection(normalized_text):
            log.warning("chat bridge input may contain prompt injection")

        payload = self.build_prompt_payload(
            mode=mode,
            peer_username=peer_username,
            current_message=current_message,
            history=history,
        )
        log.info(
            "chat bridge request: mode=%s peer=@%s history=%d",
            mode,
            normalize_chat_bridge_username(peer_username) or "unknownbot",
            len(history) if history else 0,
        )
        result = await self.llm.chat(payload["messages"])
        return clean_multiline_text(strip_chat_bridge_prefix(result), max_len=160)
