from __future__ import annotations

import logging
import random

from bot.services.sticker_library import sticker_library
from bot.services.skills.base import SkillContext, SkillRunResult

log = logging.getLogger(__name__)


class SendStickerSkill:
    name = "send_sticker"
    description = "向当前群聊发送一张贴纸，可指定 sticker_file_id，未指定时尝试使用默认贴纸。"
    parameters_schema = {
        "type": "object",
        "properties": {
            "sticker_file_id": {
                "type": "string",
                "description": "贴纸 file_id；为空时使用回复目标贴纸或默认贴纸池",
            },
            "description": {
                "type": "string",
                "description": "想发送的贴纸描述，例如“开心庆祝/安慰抱抱/无语翻白眼”",
            },
            "reason": {"type": "string", "description": "发送贴纸的简短原因，便于日志追踪"},
        },
        "required": [],
        "additionalProperties": False,
    }

    @staticmethod
    def _pick_default_sticker(context: SkillContext) -> str:
        if context.message and context.message.reply_to_message and context.message.reply_to_message.sticker:
            return context.message.reply_to_message.sticker.file_id or ""
        if context.message and context.message.sticker:
            return context.message.sticker.file_id or ""
        pool = [x.strip() for x in context.default_sticker_file_ids if x and x.strip()]
        if not pool:
            return ""
        return random.choice(pool)

    async def run(self, arguments: dict, context: SkillContext) -> SkillRunResult:
        message = context.message
        if not message:
            return SkillRunResult(
                ok=False,
                skill=self.name,
                summary="当前上下文无法发送贴纸",
                error="missing_message_context",
            )

        sticker_file_id = str(arguments.get("sticker_file_id", "")).strip()
        description = str(arguments.get("description", "")).strip()
        reason = str(arguments.get("reason", "")).strip()
        if not sticker_file_id:
            query = description or reason or context.current_user_text
            picked = sticker_library.pick_sticker(
                message.chat.id,
                query=query,
                fallback_pool=context.default_sticker_file_ids,
            )
            sticker_file_id = picked.file_id
            if not sticker_file_id:
                sticker_file_id = self._pick_default_sticker(context)
        if not sticker_file_id:
            return SkillRunResult(
                ok=False,
                skill=self.name,
                summary="没有可用贴纸，请配置默认贴纸或传入 sticker_file_id",
                error="missing_sticker_file_id",
            )

        try:
            sent = await message.answer_sticker(sticker=sticker_file_id)
            sticker_library.mark_sent(message.chat.id, sticker_file_id)
            log.info("skill send_sticker ok: chat=%s reason=%s", message.chat.id, reason or "-")
            return SkillRunResult(
                ok=True,
                skill=self.name,
                summary="贴纸发送成功",
                payload={
                    "sent": True,
                    "sticker_file_id": sticker_file_id,
                    "message_id": sent.message_id if sent else 0,
                    "description_hint": description,
                },
            )
        except Exception as e:
            log.exception("skill send_sticker failed")
            return SkillRunResult(
                ok=False,
                skill=self.name,
                summary="贴纸发送失败",
                error=str(e),
            )
