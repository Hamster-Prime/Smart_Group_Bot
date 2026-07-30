from __future__ import annotations

import logging

from bot.services.doubao_tts import DoubaoTTSService, TTSDeliveryResult
from bot.services.skills.base import SkillContext, SkillRunResult
from bot.utils.security import clean_text
from bot.utils.telegram import send_chat_message, send_reply

log = logging.getLogger(__name__)


class DoubaoTTSSkill:
    name = "doubao_tts"
    description = (
        "Synthesize the provided Chinese text with Doubao TTS and send it as a voice message in the current chat. Prefer this only when speech clearly adds warmth, emotion, companionship, or performance value, not as the default for all replies."
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
                "description": "Optional emotion style like happy, sad, angry, cheerful, or calm. Prefer using it only when the tone is clear.",
            },
            "emotion_scale": {
                "type": "integer",
                "minimum": 1,
                "maximum": 5,
                "description": "Optional emotion intensity from 1 to 5. Use together with emotion when stronger expression is needed.",
            },
            "context": {
                "type": "string",
                "description": "Optional natural-language voice direction for emotional delivery, such as scene, mood, pacing, or speaking style. Prefer a short vivid sentence instead of a single word.",
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
        context_text = clean_text(str(arguments.get("context", "")), max_len=160)

        emotion_scale_raw = arguments.get("emotion_scale")
        emotion_scale = int(emotion_scale_raw) if isinstance(emotion_scale_raw, int) else None
        if emotion_scale is not None:
            emotion_scale = min(5, max(1, emotion_scale))

        speech_rate_raw = arguments.get("speech_rate")
        loudness_rate_raw = arguments.get("loudness_rate")
        speech_rate = int(speech_rate_raw) if isinstance(speech_rate_raw, int) else None
        loudness_rate = int(loudness_rate_raw) if isinstance(loudness_rate_raw, int) else None

        uid = str(context.sender_user_id or context.chat_id or "smart-group-bot")
        delivery = TTSDeliveryResult()
        if context.message is not None:
            delivery = await self.tts_service.send_message_tts_result(
                context.message,
                text,
                delivery_mode=delivery_mode,
                auto_delete_seconds=context.auto_delete_media_seconds,
                uid=uid,
                emotion=emotion,
                emotion_scale=emotion_scale,
                speech_rate=speech_rate,
                loudness_rate=loudness_rate,
                context=context_text,
                on_delivery=context.delivery_callback,
            )
        elif context.bot is not None and int(context.chat_id or 0):
            delivery = await self.tts_service.send_chat_tts_result(
                context.bot,
                int(context.chat_id),
                text,
                auto_delete_seconds=context.auto_delete_media_seconds,
                uid=uid,
                emotion=emotion,
                emotion_scale=emotion_scale,
                speech_rate=speech_rate,
                loudness_rate=loudness_rate,
                context=context_text,
                on_delivery=context.delivery_callback,
            )
        else:
            return SkillRunResult(
                ok=False,
                skill=self.name,
                summary="当前上下文不支持发送 TTS",
                error="missing_chat_context",
            )

        if not delivery.any_sent:
            log.warning("doubao_tts skill send failed")
            return SkillRunResult(ok=False, skill=self.name, summary="TTS 发送失败", error="send_failed")

        context.handled = True
        context.tts_sent = True
        context.tts_telegram_message_ids = tuple(
            int(message_id)
            for message_id in delivery.telegram_message_ids
            if int(message_id or 0)
        )
        context.suppress_followup_text = True
        remaining_text_sent = False
        if not delivery.complete and delivery.remaining_text:
            if context.message is not None:
                remaining_text_sent = await send_reply(
                    context.message,
                    delivery.remaining_text,
                    delivery_mode=delivery_mode,
                    stream=False,
                    auto_delete_seconds=context.auto_delete_reply_seconds,
                    disable_link_preview=context.disable_link_preview,
                    on_delivery=context.delivery_callback,
                )
            elif context.bot is not None and int(context.chat_id or 0):
                remaining_text_sent = await send_chat_message(
                    context.bot,
                    int(context.chat_id),
                    delivery.remaining_text,
                    auto_delete_seconds=context.auto_delete_reply_seconds,
                    disable_link_preview=context.disable_link_preview,
                    on_delivery=context.delivery_callback,
                )

        delivered_parts = [delivery.delivered_text]
        if remaining_text_sent:
            delivered_parts.append(delivery.remaining_text)
        context.tts_text = "\n".join(part for part in delivered_parts if part).strip()

        if not delivery.complete:
            log.warning(
                "doubao_tts skill partially delivered | sent=%d total=%d "
                "text_fallback=%s error=%s",
                delivery.sent_segment_count,
                len(delivery.requested_segments),
                remaining_text_sent,
                delivery.error or "unknown",
            )
            if remaining_text_sent:
                return SkillRunResult(
                    ok=True,
                    skill=self.name,
                    summary=context.tts_text,
                    payload={
                        "text": context.tts_text,
                        "delivery_mode": delivery_mode,
                        "complete": True,
                        "voice_complete": False,
                        "text_fallback_sent": True,
                        "segments_sent": delivery.sent_segment_count,
                        "segments_total": len(delivery.requested_segments),
                    },
                )
            return SkillRunResult(
                ok=False,
                skill=self.name,
                summary="TTS 已部分发送，后续分段发送失败",
                error="partial_send",
                payload={
                    "text": context.tts_text,
                    "complete": False,
                    "voice_complete": False,
                    "text_fallback_sent": False,
                    "segments_sent": delivery.sent_segment_count,
                    "segments_total": len(delivery.requested_segments),
                },
            )
        return SkillRunResult(
            ok=True,
            skill=self.name,
            summary=context.tts_text,
            payload={
                "text": context.tts_text,
                "delivery_mode": delivery_mode,
                "emotion": emotion,
                "emotion_scale": emotion_scale,
                "speech_rate": speech_rate,
                "loudness_rate": loudness_rate,
                "context": context_text,
                "complete": True,
                "voice_complete": True,
                "text_fallback_sent": False,
                "segments_sent": delivery.sent_segment_count,
            },
        )
