from __future__ import annotations

import logging
from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties

from bot.config import Settings
from bot.services.telegram_session import PriorityAiohttpSession

log = logging.getLogger(__name__)

_TELEGRAM_HTTP_TIMEOUT_SECONDS = 30.0

dp = Dispatcher()


def create_bot(settings: Settings) -> Bot:
    return Bot(
        token=settings.bot.token,
        session=PriorityAiohttpSession(timeout=_TELEGRAM_HTTP_TIMEOUT_SECONDS),
        default=DefaultBotProperties(parse_mode=settings.bot.parse_mode),
    )
