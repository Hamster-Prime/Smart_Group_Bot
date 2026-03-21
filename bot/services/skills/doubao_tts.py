from __future__ import annotations

import logging

from bot.services.doubao_tts import DoubaoTTSService
from bot.services.skills.base import SkillContext, SkillRunResult
from bot.utils.security import clean_text

log = logging.getLogger(__name__)


class DoubaoTTSSkill:
    name = "doubao_tts"
    description = (
        "Synthesize the provided Chinese text with Doubao TTS and send it as a voice message in the current chat."
    )
    parameters_schema = {
        "type": "object",
        "properties": {
            "text": {
                "type": "string",
                "description": "The exact text that should be spoken in the voice message.",
            },
            "delivery_mode": {
                "type": "string",
                "enum": ["reply", "message"],
                "description": "reply replies to the triggering message; message sends as a standalone message.",
                "default": "reply",
            },
            "emotion": {
                "type": "string",
                "description": "Optional emotion style. Only use it when you are confident the configured speaker supports it.",
            },
            "speech_rate": {
                "type": "integer",
                "description": "Optional speaking speed adjustment, usually between -50 and 100.",
            },
            "loudness_rate": {
                "type": "integer",
                "description": "Optional volume adjustment, usually between -50 and 100.",
            },
        },
        "required": ["text"],
        "additionalProperties": False,
    }

    def __init__(self, tts_service: DoubaoTTSService) -> None:
        self.tts_service = tts_service

    async def run(self, arguments: dict, context: SkillContext) -> SkillRunResult:
        text = clean_text(str(arguments.get("text", "") or context.current_user_text), max_len=2400)
        if not text:
            return SkillRunResult(ok=False, skill=self.name, summary="TTS 文本为空", error="empty_text")

        delivery_mode = clean_text(str(arguments.get("delivery_mode", "reply")), max_len=16).lower()
        if delivery_mode not in {"reply", "message"}:
            delivery_mode = "reply"

        emotion = clean_text(str(arguments.get("emotion", "")), max_len=32)

        speech_rate_raw = arguments.get("speech_rate")
        loudness_rate_raw = arguments.get("loudness_rate")
        speech_rate = int(speech_rate_raw) if isinstance(speech_rate_raw, int) else None
        loudness_rate = int(loudness_rate_raw) if isinstance(loudness_rate_raw, int) else None

        uid = str(context.sender_user_id or context.chat_id or "smart-group-bot")
        ok = False
        if context.message is not None:
            ok = await self.tts_service.send_message_tts(
                context.message,
                text,
                delivery_mode=delivery_mode,
                auto_delete_minutes=0,
                uid=uid,
                emotion=emotion,
                speech_rate=speech_rate,
                loudness_rate=loudness_rate,
            )
        elif context.bot is not None and int(context.chat_id or 0):
            ok = await self.tts_service.send_chat_tts(
                context.bot,
                int(context.chat_id),
                text,
                auto_delete_minutes=0,
                uid=uid,
                emotion=emotion,
                speech_rate=speech_rate,
                loudness_rate=loudness_rate,
            )
        else:
            return SkillRunResult(
                ok=False,
                skill=self.name,
                summary="当前上下文不支持发送 TTS",
                error="missing_chat_context",
            )

        if not ok:
            log.warning("doubao_tts skill send failed")
            return SkillRunResult(ok=False, skill=self.name, summary="TTS 发送失败", error="send_failed")

        context.handled = True
        context.tts_sent = True
        context.tts_text = text
        context.suppress_followup_text = True
        return SkillRunResult(
            ok=True,
            skill=self.name,
            summary=text,
            payload={
                "text": text,
                "delivery_mode": delivery_mode,
                "emotion": emotion,
                "speech_rate": speech_rate,
                "loudness_rate": loudness_rate,
            },
        )
