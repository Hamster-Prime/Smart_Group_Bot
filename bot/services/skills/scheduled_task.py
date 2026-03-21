from __future__ import annotations

import logging

from bot.config import Settings
from bot.services.doubao_tts import DoubaoTTSService, is_tts_always_enabled, load_group_tts_mode
from bot.services.skills.base import SkillContext, SkillRunResult
from bot.utils.prompts import SCHEDULED_TASK_SYSTEM, with_persona
from bot.utils.runtime_context import build_current_time_context
from bot.utils.security import build_defended_system, clean_text, sanitize_history_for_llm, wrap_untrusted
from bot.utils.telegram import send_chat_message

log = logging.getLogger(__name__)

_SKIP_MARKERS = {"SKIP_TASK", "SKIP_PROACTIVE"}


class ScheduledTaskSkill:
    name = "scheduled_task"
    description = "Run a scheduled per-group task with its own LLM worker and optionally send the result."
    parameters_schema = {
        "type": "object",
        "properties": {
            "task_name": {
                "type": "string",
                "description": "Stable scheduled task identifier, for example reminder or cooldown_topic.",
            },
            "task_brief": {
                "type": "string",
                "description": "Concrete task instructions for this scheduled execution.",
            },
            "send_message": {
                "type": "boolean",
                "description": "Whether to send the generated output to the current group.",
                "default": True,
            },
            "reply_to_message_id": {
                "type": "integer",
                "description": "Optional Telegram message id to reply to when sending the scheduled result.",
            },
            "fallback_mention_user_id": {
                "type": "integer",
                "description": "If reply target is gone, mention this user when sending the fallback message.",
            },
            "fallback_mention_name": {
                "type": "string",
                "description": "Display name used for the fallback user mention.",
            },
        },
        "required": ["task_name", "task_brief"],
        "additionalProperties": False,
    }

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings
        self.tts_service = DoubaoTTSService(settings) if settings is not None else None

    @classmethod
    def build_prompt_payload(
        cls,
        *,
        task_name: str,
        task_brief: str,
        history: list[dict[str, str]] | None = None,
    ) -> dict[str, list[dict[str, str]]]:
        normalized_task_name = clean_text(task_name, max_len=64).lower()
        normalized_task_brief = clean_text(task_brief, max_len=500)
        messages: list[dict[str, str]] = [
            {"role": "system", "content": build_defended_system(with_persona(SCHEDULED_TASK_SYSTEM))},
            {"role": "system", "content": build_current_time_context()},
            {"role": "system", "content": f"[SCHEDULED_TASK]\nname: {normalized_task_name}"},
        ]
        if history:
            messages.extend(sanitize_history_for_llm(history, max_items=len(history)))
        messages.append(
            {
                "role": "user",
                "content": wrap_untrusted("scheduled_task_brief", normalized_task_brief, max_len=500),
            }
        )
        return {"messages": messages}

    async def run(self, arguments: dict, context: SkillContext) -> SkillRunResult:
        llm = context.llm
        bot = context.bot
        chat_id = int(context.chat_id or 0)
        if llm is None:
            return SkillRunResult(
                ok=False,
                skill=self.name,
                summary="Scheduled task is missing an LLM context.",
                error="missing_llm",
            )
        if bot is None or chat_id == 0:
            return SkillRunResult(
                ok=False,
                skill=self.name,
                summary="Scheduled task is missing a chat context.",
                error="missing_chat_context",
            )

        task_name = clean_text(str(arguments.get("task_name", "")), max_len=64).lower()
        task_brief = clean_text(
            str(arguments.get("task_brief", "") or context.current_user_text),
            max_len=500,
        )
        send_message = bool(arguments.get("send_message", True))
        reply_to_message_id = int(arguments.get("reply_to_message_id", 0) or 0) or None
        fallback_mention_user_id = int(arguments.get("fallback_mention_user_id", 0) or 0)
        fallback_mention_name = clean_text(str(arguments.get("fallback_mention_name", "")), max_len=120)
        if task_name not in {"reminder", "cooldown_topic"} or not task_brief:
            return SkillRunResult(
                ok=False,
                skill=self.name,
                summary="Scheduled task arguments are missing or unsupported.",
                error="invalid_arguments",
            )

        messages = self.build_prompt_payload(
            task_name=task_name,
            task_brief=task_brief,
            history=context.history,
        )["messages"]

        generated = (await llm.chat(messages)).strip()
        reply = clean_text(generated, max_len=240)
        if not reply or reply.upper() in _SKIP_MARKERS:
            return SkillRunResult(
                ok=True,
                skill=self.name,
                summary="No scheduled-task message needs to be sent right now.",
                payload={"sent": False, "text": "", "task_name": task_name},
            )

        if not send_message:
            return SkillRunResult(
                ok=True,
                skill=self.name,
                summary=reply,
                payload={"sent": False, "text": reply, "task_name": task_name},
            )

        sent_ok = False
        always_tts = False
        if context.session is not None:
            tts_mode = await load_group_tts_mode(context.session, chat_id)
            always_tts = is_tts_always_enabled(tts_mode)
            if always_tts and self.tts_service and self.tts_service.available:
                sent_ok = await self.tts_service.send_chat_tts(
                    bot,
                    chat_id,
                    reply,
                    reply_to_message_id=reply_to_message_id,
                    fallback_mention_user_id=fallback_mention_user_id,
                    fallback_mention_name=fallback_mention_name,
                    auto_delete_minutes=0,
                    uid=str(context.sender_user_id or chat_id),
                )

        if not sent_ok and not always_tts:
            sent_ok = await send_chat_message(
                bot,
                chat_id,
                reply,
                reply_to_message_id=reply_to_message_id,
                fallback_mention_user_id=fallback_mention_user_id,
                fallback_mention_name=fallback_mention_name,
                auto_delete_minutes=0,
            )
        if not sent_ok:
            log.warning("[%s] scheduled_task send failed | task=%s", chat_id, task_name)
            return SkillRunResult(
                ok=False,
                skill=self.name,
                summary="Scheduled task message delivery failed.",
                error="send_failed",
                payload={"sent": False, "text": reply, "task_name": task_name},
            )

        context.handled = True
        return SkillRunResult(
            ok=True,
            skill=self.name,
            summary=reply,
            payload={"sent": True, "text": reply, "task_name": task_name},
        )
