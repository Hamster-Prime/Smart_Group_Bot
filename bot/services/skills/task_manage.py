from __future__ import annotations

import html
import re

from bot.services.skills.base import SkillContext, SkillRunResult
from bot.services.task_intent import TaskIntentService
from bot.utils.security import clean_text

_TASK_LIST_PATTERNS = (
    re.compile(r"(?:查看|列出|显示|看看).*(?:定时任务|任务列表|提醒列表)"),
    re.compile(r"(?:定时任务|任务|提醒)列表"),
    re.compile(r"(?:有哪些|还有什么).*(?:提醒|任务)"),
)


def _looks_like_task_list_request(text: str) -> bool:
    normalized = clean_text(text, max_len=1200)
    if not normalized:
        return False
    return any(pattern.search(normalized) for pattern in _TASK_LIST_PATTERNS)


def _extract_scheduled_task_target(context: SkillContext) -> tuple[int, str]:
    message = context.message
    if message is None:
        return 0, ""
    source_text = message.text or message.caption or ""
    entities = list(message.entities or []) + list(message.caption_entities or [])
    for entity in entities:
        et = str(getattr(entity, "type", "") or "")
        if et == "text_mention":
            user = getattr(entity, "user", None)
            if user is not None:
                return int(getattr(user, "id", 0) or 0), (user.full_name or user.username or "").strip()
        if et == "mention":
            offset = int(getattr(entity, "offset", 0) or 0)
            length = int(getattr(entity, "length", 0) or 0)
            mention = source_text[offset : offset + length].strip().lstrip("@")
            if mention:
                return 0, mention
    return 0, ""


class TaskManageSkill:
    name = "task_manage"
    description = (
        "Create or list scheduled tasks from user requests. "
        "Never use this tool to delete tasks; task deletion must go through /tasks or /canceltask."
    )
    parameters_schema = {
        "type": "object",
        "properties": {
            "request_text": {
                "type": "string",
                "description": "The user's task-management request. Omit it to use the current user message.",
            }
        },
        "additionalProperties": False,
    }

    async def run(self, arguments: dict, context: SkillContext) -> SkillRunResult:
        if context.session is None or context.llm is None:
            return SkillRunResult(
                ok=False,
                skill=self.name,
                summary="当前无法处理定时任务，请稍后重试。",
                error="missing_context",
            )

        group_id = int(context.chat_id or 0)
        if group_id == 0:
            return SkillRunResult(
                ok=False,
                skill=self.name,
                summary="当前会话不支持定时任务管理。",
                error="missing_chat_context",
            )

        request_text = clean_text(
            str(arguments.get("request_text", "") or context.current_user_text),
            max_len=1200,
        )
        if not request_text:
            return SkillRunResult(
                ok=True,
                skill=self.name,
                summary="请直接说要创建什么提醒或任务；删除请使用 /tasks 或 /canceltask。",
            )

        if _looks_like_task_list_request(request_text):
            from bot.services.scheduled_tasks import list_group_scheduled_tasks, render_task_list_text

            rows = await list_group_scheduled_tasks(context.session, group_id=group_id, limit=20)
            return SkillRunResult(
                ok=True,
                skill=self.name,
                summary=render_task_list_text(rows) + "\n\n删除请使用 /tasks 或 /canceltask。",
                payload={"action": "list", "count": len(rows)},
            )

        intent_svc = TaskIntentService(context.llm)
        intent = await intent_svc.detect(request_text, history=context.history)
        if intent.intent != "task_manage":
            return SkillRunResult(
                ok=True,
                skill=self.name,
                summary="我没看懂这条定时任务指令。你可以说“今晚9点提醒我吃饭”，删除请使用 /tasks 或 /canceltask。",
            )

        if intent.task_action == "delete":
            return SkillRunResult(
                ok=True,
                skill=self.name,
                summary="定时任务删除请使用 /tasks 打开列表后点按钮，或直接用 /canceltask <任务ID>。",
                payload={"redirect_command": "/tasks", "action": "delete"},
            )

        if intent.task_action != "add" or intent.due_at is None or not intent.task_content:
            return SkillRunResult(
                ok=True,
                skill=self.name,
                summary="这条定时任务请求暂不支持；删除请使用 /tasks 或 /canceltask。",
            )

        message = context.message
        reply_target = getattr(message, "reply_to_message", None) if message is not None else None
        target_user = getattr(reply_target, "from_user", None) if reply_target else None
        target_name = ""
        target_user_id = 0
        reply_to_message_id = int(getattr(message, "message_id", 0) or 0)
        if reply_target is not None:
            reply_to_message_id = int(getattr(reply_target, "message_id", 0) or reply_to_message_id)
            if target_user is not None:
                target_name = (target_user.full_name or target_user.username or "").strip()
                target_user_id = int(getattr(target_user, "id", 0) or 0)
        if not target_name:
            target_user_id, target_name = _extract_scheduled_task_target(context)
        if not target_name:
            target_name = clean_text(context.sender_username or "", max_len=120) or str(context.sender_user_id or "")
            target_user_id = int(context.sender_user_id or 0)

        creator_name = ""
        if message is not None and getattr(message, "from_user", None) is not None:
            sender = message.from_user
            creator_name = (sender.full_name or sender.username or "").strip()
        if not creator_name:
            creator_name = clean_text(context.sender_username or "", max_len=120)
        creator_name = creator_name or str(context.sender_user_id or "-")
        from bot.services.scheduled_tasks import create_scheduled_task, format_task_summary

        row = await create_scheduled_task(
            context.session,
            group_id=group_id,
            creator_user_id=int(context.sender_user_id or 0),
            creator_name=creator_name,
            due_at=intent.due_at,
            task_type=intent.task_type,
            content=intent.task_content,
            reply_to_message_id=reply_to_message_id,
            target_user_id=target_user_id,
            target_user_name=target_name,
            source="skill",
        )
        await context.session.commit()
        await context.session.refresh(row)

        ack_text = html.escape(intent.ack_text or "好，到时间我会处理。")
        return SkillRunResult(
            ok=True,
            skill=self.name,
            summary=f"{ack_text}\n\n<b>定时任务已创建</b>\n{format_task_summary(row)}",
            payload={"action": "add", "task_id": row.id, "task_type": row.task_type},
        )
