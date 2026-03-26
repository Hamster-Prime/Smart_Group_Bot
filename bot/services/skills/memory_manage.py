from __future__ import annotations

import html
import re

from bot.services import memory_holder
from bot.services.group_intent import GroupIntentService
from bot.services.skills.base import SkillContext, SkillRunResult
from bot.utils.security import clean_text


def _truncate_text(text: str, max_len: int) -> str:
    cleaned = (text or "").replace("\n", " ").strip()
    if len(cleaned) <= max_len:
        return cleaned
    return cleaned[:max_len] + "..."


def _surface_text(text: str) -> str:
    surface = (text or "").strip()
    surface = re.split(r"\n\[(?:reply_to|quote):", surface, maxsplit=1)[0]
    return clean_text(surface, max_len=400)


class MemoryManageSkill:
    name = "memory_manage"
    description = (
        "View, add, or update group permanent memory for admin requests. "
        "Never use this tool to delete or clear memories; memory deletion must go through /lm."
    )
    parameters_schema = {
        "type": "object",
        "properties": {
            "request_text": {
                "type": "string",
                "description": "The user's memory-management request. Omit it to use the current user message.",
            }
        },
        "additionalProperties": False,
    }

    @staticmethod
    def _format_memory_list(items: list[object]) -> str:
        if not items:
            return "<b>永久记忆</b>\n当前为空。\n\n删除请使用 /lm 打开列表后点按钮。"

        lines = [
            "<b>永久记忆</b>",
            f"共 {len(items)} 条",
            "",
        ]
        for item in items[:20]:
            content = html.escape(_truncate_text(str(getattr(item, "content", "") or ""), 140))
            lines.append(f"#{getattr(item, 'id', 0)} {content}")
        if len(items) > 20:
            lines.append(f"... 其余 {len(items) - 20} 条请使用 /lm 查看")
        lines.extend(
            [
                "",
                "删除请使用 /lm 打开列表后点按钮。",
            ]
        )
        return "\n".join(lines).rstrip()

    async def run(self, arguments: dict, context: SkillContext) -> SkillRunResult:
        if context.llm is None:
            return SkillRunResult(
                ok=False,
                skill=self.name,
                summary="当前无法处理永久记忆，请稍后重试。",
                error="missing_llm",
            )

        group_id = int(context.chat_id or 0)
        if group_id == 0:
            return SkillRunResult(
                ok=False,
                skill=self.name,
                summary="当前会话不支持永久记忆管理。",
                error="missing_chat_context",
            )

        if not (context.sender_is_owner or context.sender_is_tg_admin):
            return SkillRunResult(
                ok=True,
                skill=self.name,
                summary="仅群管理员可管理永久记忆；删除请使用 /lm。",
                payload={"denied": True},
            )

        request_text = clean_text(
            str(arguments.get("request_text", "") or context.current_user_text),
            max_len=1200,
        )
        if not request_text:
            return SkillRunResult(
                ok=True,
                skill=self.name,
                summary="请直接说要查看、添加或修改哪条永久记忆；删除请使用 /lm。",
            )

        intent_svc = GroupIntentService(context.llm)
        intent = await intent_svc.detect(
            request_text,
            history=context.history,
            surface_text=_surface_text(request_text),
        )
        action = (intent.memory_action or "").strip().lower()
        if intent.intent != "memory_manage":
            return SkillRunResult(
                ok=True,
                skill=self.name,
                summary="我没看懂这条永久记忆指令。你可以说“记住xxx”“把xxx改成yyy”，删除请使用 /lm。",
            )

        if action in {"delete", "clear"}:
            return SkillRunResult(
                ok=True,
                skill=self.name,
                summary="永久记忆删除请使用 /lm 打开列表后点按钮，我这边不直接删除。",
                payload={"redirect_command": "/lm", "action": action},
            )

        memory = memory_holder.get()
        if action == "list":
            items = await memory.list_permanent_memories(group_id, limit=40)
            return SkillRunResult(
                ok=True,
                skill=self.name,
                summary=self._format_memory_list(items),
                payload={"action": "list", "count": len(items)},
            )

        if action == "add":
            content = (intent.memory_content or "").strip()
            row, created = await memory.add_permanent_memory(
                group_id,
                content,
                created_by=context.sender_user_id,
            )
            if not row:
                return SkillRunResult(
                    ok=True,
                    skill=self.name,
                    summary="永久记忆写入失败，内容为空或无效。",
                )
            status = "已写入" if created else "已存在"
            return SkillRunResult(
                ok=True,
                skill=self.name,
                summary=(
                    "<b>永久记忆{status}</b>\n"
                    "<b>ID</b>: #{mem_id}\n"
                    "<b>内容</b>: {content}"
                ).format(
                    status=status,
                    mem_id=row.id,
                    content=html.escape(_truncate_text(row.content or "", 180)),
                ),
                payload={"action": "add", "memory_id": row.id, "created": created},
            )

        if action == "replace":
            target = (intent.memory_target or "").strip()
            new_content = (intent.memory_content or "").strip()
            if not target or not new_content:
                return SkillRunResult(
                    ok=True,
                    skill=self.name,
                    summary="请明确要改哪条永久记忆，以及改成什么内容。",
                )
            deleted, created_row, created_new = await memory.replace_permanent_memory(
                group_id,
                target=target,
                new_content=new_content,
                created_by=context.sender_user_id,
            )
            if not created_row:
                return SkillRunResult(
                    ok=True,
                    skill=self.name,
                    summary="永久记忆修改失败，新内容为空或无效。",
                )
            action_text = "已更新" if deleted else "未命中旧记忆，已新增"
            write_mode = "新增" if created_new else "复用"
            return SkillRunResult(
                ok=True,
                skill=self.name,
                summary=(
                    "<b>永久记忆修改结果</b>\n"
                    "<b>状态</b>: {action_text}\n"
                    "<b>删除数量</b>: {deleted_count}\n"
                    "<b>写入方式</b>: {write_mode}\n"
                    "<b>新ID</b>: #{mem_id}\n"
                    "<b>内容</b>: {content}\n\n"
                    "如需删除，请使用 /lm。"
                ).format(
                    action_text=action_text,
                    deleted_count=len(deleted),
                    write_mode=write_mode,
                    mem_id=created_row.id,
                    content=html.escape(_truncate_text(created_row.content or "", 180)),
                ),
                payload={
                    "action": "replace",
                    "memory_id": created_row.id,
                    "deleted_count": len(deleted),
                    "created_new": created_new,
                },
            )

        return SkillRunResult(
            ok=True,
            skill=self.name,
            summary="这条永久记忆请求暂不支持。删除请使用 /lm。",
        )
