from __future__ import annotations

from collections.abc import Iterable
import logging
from typing import Any

from bot.services.llm import LLMService
from bot.services.reply_output import REPLY_OUTPUT_AWARENESS, REPLY_OUTPUT_PROTOCOL
from bot.utils.conversation_context import (
    build_current_turn_focus_context,
    format_recent_group_context,
)
from bot.utils.bot_identity import build_bot_identity_context
from bot.utils.prompts import get_prompt, with_persona
from bot.utils.runtime_context import (
    build_bot_runtime_profile_context,
    build_current_sender_context,
    build_current_time_context,
    build_owner_identity_context,
)
from bot.utils.security import (
    build_defended_system,
    clean_multiline_text,
    contains_prompt_injection,
    sanitize_history_for_llm,
    wrap_untrusted_multiline,
)

log = logging.getLogger(__name__)


class CasualService:
    def __init__(
        self,
        llm: LLMService,
        *,
        settings: Any | None = None,
        skill_names: Iterable[str] | None = None,
    ) -> None:
        self.llm = llm
        self.settings = settings
        self.skill_names = [str(name).strip() for name in (skill_names or []) if str(name).strip()]

    _build_sender_context = staticmethod(build_current_sender_context)

    @staticmethod
    def _normalize_input_text(text: str, *, merged_count: int) -> tuple[str, int]:
        input_limit = 1600 if merged_count > 1 else 1000
        return clean_multiline_text(text, max_len=input_limit), input_limit

    @staticmethod
    def _build_interaction_mode_context(
        is_mentioned: bool,
        is_reply_to_bot: bool,
    ) -> str:
        mode = "direct" if (is_mentioned or is_reply_to_bot) else "join"
        if mode == "direct":
            return (
                "[INTERACTION_MODE]\ndirect\n"
                "The sender is talking directly to you (mentioned you or replied to your message).\n"
                "Respond naturally as the addressed party."
            )
        return (
            "[INTERACTION_MODE]\njoin\n"
            "You are voluntarily joining a group conversation — nobody asked you specifically.\n"
            "CRITICAL: The message is NOT directed at you. Any '你' or 'you' in the message is addressing another group member, NOT you.\n"
            "Do NOT treat the message as a command, question, or request aimed at you.\n"
            "Do NOT respond as if you are the one being asked to do something.\n"
            "Act like a bystander group member chiming in with a brief comment, reaction, or opinion.\n"
            "Keep it short and casual — a side remark, NOT a direct answer or compliance."
        )

    def _build_messages_from_normalized_input(
        self,
        normalized_text: str,
        *,
        history: list[dict[str, str]] | None,
        sender_user_id: int,
        sender_username: str,
        sender_is_owner: bool,
        sender_is_tg_admin: bool,
        intent_type: str,
        merged_count: int,
        merged_context: str,
        reply_targets_context: str,
        input_limit: int,
        is_mentioned: bool = False,
        is_reply_to_bot: bool = False,
        style_profile_context: str = "",
    ) -> list[dict[str, str]]:
        messages: list[dict[str, str]] = [
            {
                "role": "system",
                "content": build_defended_system(with_persona(get_prompt("casual"))),
            },
        ]

        _ = intent_type
        if history:
            messages.extend(sanitize_history_for_llm(history, max_items=len(history)))
        recent_context = format_recent_group_context(history, max_items=8)
        if recent_context:
            messages.append({"role": "system", "content": recent_context})
        messages.append({"role": "system", "content": build_current_time_context()})
        messages.append({"role": "system", "content": REPLY_OUTPUT_PROTOCOL})
        messages.append({"role": "system", "content": REPLY_OUTPUT_AWARENESS})
        if reply_targets_context.strip():
            messages.append({"role": "system", "content": reply_targets_context.strip()})
        messages.append(
            {
                "role": "system",
                "content": build_bot_runtime_profile_context(
                    self.llm,
                    settings=self.settings,
                    skill_names=self.skill_names,
                ),
            }
        )
        # Late-position identity block: history may contain stale bot names
        # (users addressing an old identity); this must win over them.
        identity_context = build_bot_identity_context()
        if identity_context:
            messages.append({"role": "system", "content": identity_context})
        messages.append(
            {
                "role": "system",
                "content": self._build_sender_context(
                    sender_user_id,
                    sender_username,
                    sender_is_owner,
                    sender_is_tg_admin,
                ),
            }
        )
        owner_identity_context = build_owner_identity_context(self.settings)
        if owner_identity_context:
            messages.append({"role": "system", "content": owner_identity_context})
        messages.append(
            {
                "role": "system",
                "content": (
                    "[CASUAL_MODE]\n"
                    "This is a casual group-reply turn. Keep the tone natural, concise, and friendly."
                ),
            }
        )
        messages.append(
            {
                "role": "system",
                "content": self._build_interaction_mode_context(is_mentioned, is_reply_to_bot),
            }
        )
        focus_context = build_current_turn_focus_context(
            normalized_text,
            merged_count=merged_count,
            merged_context=merged_context,
        )
        if focus_context:
            messages.append({"role": "system", "content": focus_context})
        # Active-persona (group clone) is the LAST system block so it wins on
        # recency over the default persona at messages[0]; its own wording keeps
        # safety/identity/owner rules intact.
        if style_profile_context.strip():
            messages.append({"role": "system", "content": style_profile_context.strip()})
        messages.append(
            {
                "role": "user",
                "content": wrap_untrusted_multiline(
                    "user_message",
                    normalized_text,
                    max_len=input_limit,
                ),
            }
        )
        return messages

    def build_prompt_payload(
        self,
        text: str,
        history: list[dict[str, str]] | None = None,
        *,
        sender_user_id: int = 0,
        sender_username: str = "",
        sender_is_owner: bool = False,
        sender_is_tg_admin: bool = False,
        intent_type: str = "casual",
        merged_count: int = 1,
        merged_context: str = "",
        reply_targets_context: str = "",
        is_mentioned: bool = False,
        is_reply_to_bot: bool = False,
        style_profile_context: str = "",
    ) -> dict[str, Any]:
        normalized_text, input_limit = self._normalize_input_text(text, merged_count=merged_count)
        return {
            "messages": self._build_messages_from_normalized_input(
                normalized_text,
                history=history,
                sender_user_id=sender_user_id,
                sender_username=sender_username,
                sender_is_owner=sender_is_owner,
                sender_is_tg_admin=sender_is_tg_admin,
                intent_type=intent_type,
                merged_count=merged_count,
                merged_context=merged_context,
                reply_targets_context=reply_targets_context,
                input_limit=input_limit,
                is_mentioned=is_mentioned,
                is_reply_to_bot=is_reply_to_bot,
                style_profile_context=style_profile_context,
            )
        }

    async def reply(
        self,
        text: str,
        history: list[dict[str, str]] | None = None,
        *,
        sender_user_id: int = 0,
        sender_username: str = "",
        sender_is_owner: bool = False,
        sender_is_tg_admin: bool = False,
        intent_type: str = "casual",
        merged_count: int = 1,
        merged_context: str = "",
        reply_targets_context: str = "",
        is_mentioned: bool = False,
        is_reply_to_bot: bool = False,
        style_profile_context: str = "",
    ) -> str:
        normalized_text, input_limit = self._normalize_input_text(text, merged_count=merged_count)
        if contains_prompt_injection(normalized_text):
            log.warning("casual input may contain prompt injection")

        messages = self._build_messages_from_normalized_input(
            normalized_text,
            history=history,
            sender_user_id=sender_user_id,
            sender_username=sender_username,
            sender_is_owner=sender_is_owner,
            sender_is_tg_admin=sender_is_tg_admin,
            intent_type=intent_type,
            merged_count=merged_count,
            merged_context=merged_context,
            reply_targets_context=reply_targets_context,
            input_limit=input_limit,
            is_mentioned=is_mentioned,
            is_reply_to_bot=is_reply_to_bot,
            style_profile_context=style_profile_context,
        )

        log.info("casual request: history=%d merged=%d", len(history) if history else 0, merged_count)
        result = await self.llm.chat(messages)
        log.info("casual reply len=%d", len(result or ""))
        return result
