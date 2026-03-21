from __future__ import annotations

import logging

from bot.services.skills.base import SkillContext, SkillRunResult
from bot.services.sticker_library import sticker_library
from bot.utils.security import clean_text
from bot.utils.telegram import send_sticker_with_auto_delete

log = logging.getLogger(__name__)


class SendStickerSkill:
    name = "send_sticker"
    description = "Send a sticker in the current chat using a semantic query or an exact sticker file_id."
    parameters_schema = {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "Short emotion or scene description for picking a suitable sticker.",
            },
            "sticker_file_id": {
                "type": "string",
                "description": "Exact sticker file_id to send when already known.",
            },
            "delivery_mode": {
                "type": "string",
                "enum": ["reply", "message"],
                "description": "reply replies to the triggering message; message sends as a standalone message.",
                "default": "reply",
            },
        },
        "additionalProperties": False,
    }

    async def run(self, arguments: dict, context: SkillContext) -> SkillRunResult:
        session = context.session
        message = context.message
        if session is None or message is None:
            return SkillRunResult(
                ok=False,
                skill=self.name,
                summary="当前上下文不支持发送贴纸",
                error="missing_context",
            )

        group_id = int(getattr(getattr(message, "chat", None), "id", 0) or 0)
        if group_id == 0:
            return SkillRunResult(
                ok=False,
                skill=self.name,
                summary="当前会话无法定位群组",
                error="missing_group",
            )

        sticker_file_id = clean_text(str(arguments.get("sticker_file_id", "")), max_len=255)
        query = clean_text(str(arguments.get("query", "")), max_len=120)
        delivery_mode = clean_text(str(arguments.get("delivery_mode", "reply")), max_len=16).lower()
        if delivery_mode not in {"reply", "message"}:
            delivery_mode = "reply"

        picked_source = "explicit"
        picked_description = ""
        if not sticker_file_id:
            picked = await sticker_library.pick_sticker(
                session,
                group_id,
                query=query or context.current_user_text,
                fallback_pool=context.default_sticker_file_ids,
            )
            sticker_file_id = (picked.file_id or "").strip()
            picked_source = picked.source or "none"
            picked_description = clean_text(picked.description or "", max_len=120)

        if not sticker_file_id:
            return SkillRunResult(
                ok=False,
                skill=self.name,
                summary="当前没有可用贴纸",
                error="no_sticker",
            )

        try:
            await send_sticker_with_auto_delete(
                message,
                sticker=sticker_file_id,
                delivery_mode=delivery_mode,
                auto_delete_minutes=0,
            )
            await sticker_library.mark_sent(session, group_id, sticker_file_id)
        except Exception as exc:
            log.exception("[%s] send_sticker skill failed", group_id)
            return SkillRunResult(
                ok=False,
                skill=self.name,
                summary="贴纸发送失败",
                error=str(exc),
            )

        context.handled = True
        context.sticker_sent = True
        context.sticker_file_id = sticker_file_id
        context.suppress_followup_text = True
        return SkillRunResult(
            ok=True,
            skill=self.name,
            summary="",
            payload={
                "sticker_file_id": sticker_file_id,
                "query": query,
                "source": picked_source,
                "description": picked_description,
                "delivery_mode": delivery_mode,
            },
        )
