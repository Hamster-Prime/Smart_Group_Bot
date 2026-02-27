from __future__ import annotations

import logging
import time
import zlib
from typing import Any, Awaitable, Callable

from aiogram import BaseMiddleware
from aiogram.types import Message

from bot.utils.logging_setup import reset_log_context, set_log_context

log = logging.getLogger(__name__)


class LoggingMiddleware(BaseMiddleware):
    @staticmethod
    def _build_flow_id(update_id: int, chat_id: int, message_id: int) -> str:
        seed = f"{update_id}:{chat_id}:{message_id}".encode("utf-8")
        return f"{zlib.crc32(seed) & 0xFFFFFFFF:08x}"[:6]

    async def __call__(
        self,
        handler: Callable[[Message, dict[str, Any]], Awaitable[Any]],
        event: Message,
        data: dict[str, Any],
    ) -> Any:
        user = event.from_user
        update = data.get("event_update")
        update_id = getattr(update, "update_id", 0)
        chat_id = event.chat.id if event.chat else 0
        user_id = user.id if user else 0
        req_id = f"{chat_id}:{event.message_id}"
        flow_id = self._build_flow_id(update_id or 0, chat_id or 0, event.message_id or 0)
        tokens = set_log_context(
            req_id=req_id,
            flow_id=flow_id,
            update_id=update_id or "-",
            chat_id=chat_id or "-",
            user_id=user_id or "-",
        )

        name = user.username or user.full_name if user else "unknown"
        chat = event.chat.title or event.chat.id if event.chat else "?"
        msg_type = getattr(event, "content_type", "unknown")
        raw_text = (event.text or event.caption or "").replace("\n", "\\n").replace("|", "/").strip()
        preview = raw_text[:100] if raw_text else "-"

        started = time.perf_counter()
        try:
            log.info(
                "【入口】收到消息 | 聊天=%s | 用户=%s | 类型=%s | 内容=%s",
                chat,
                name,
                msg_type,
                preview,
            )
            result = await handler(event, data)
            elapsed_ms = int((time.perf_counter() - started) * 1000)
            log.info("【出口】处理完成 | 状态=成功 | 耗时=%dms", elapsed_ms)
            return result
        except Exception:
            elapsed_ms = int((time.perf_counter() - started) * 1000)
            log.exception("【出口】处理完成 | 状态=失败 | 耗时=%dms", elapsed_ms)
            raise
        finally:
            reset_log_context(tokens)
