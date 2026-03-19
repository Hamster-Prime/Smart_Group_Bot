from __future__ import annotations

import logging

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

    async def run(self, arguments: dict, context: SkillContext) -> SkillRunResult:
        llm = context.llm
        bot = context.bot
        chat_id = int(context.chat_id or 0)
        if llm is None:
            return SkillRunResult(
                ok=False,
                skill=self.name,
                summary="定时任务缺少 LLM 上下文",
                error="missing_llm",
            )
        if bot is None or chat_id == 0:
            return SkillRunResult(
                ok=False,
                skill=self.name,
                summary="定时任务缺少群聊上下文",
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
                summary="定时任务参数不完整或类型不支持",
                error="invalid_arguments",
            )

        messages: list[dict[str, str]] = [
            {"role": "system", "content": build_defended_system(with_persona(SCHEDULED_TASK_SYSTEM))},
            {"role": "system", "content": build_current_time_context()},
            {"role": "system", "content": f"[SCHEDULED_TASK]\nname: {task_name}"},
        ]
        if context.history:
            messages.extend(sanitize_history_for_llm(context.history, max_items=len(context.history)))
        messages.append(
            {
                "role": "user",
                "content": wrap_untrusted("scheduled_task_brief", task_brief, max_len=500),
            }
        )

        generated = (await llm.chat(messages)).strip()
        reply = clean_text(generated, max_len=240)
        if not reply or reply.upper() in _SKIP_MARKERS:
            return SkillRunResult(
                ok=True,
                skill=self.name,
                summary="当前无需发送定时任务消息",
                payload={"sent": False, "text": "", "task_name": task_name},
            )

        if not send_message:
            return SkillRunResult(
                ok=True,
                skill=self.name,
                summary=reply,
                payload={"sent": False, "text": reply, "task_name": task_name},
            )

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
                summary="定时任务消息发送失败",
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
