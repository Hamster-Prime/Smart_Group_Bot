from __future__ import annotations

import logging
from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties

from bot.config import Settings

log = logging.getLogger(__name__)

dp = Dispatcher()


def create_bot(settings: Settings) -> Bot:
    return Bot(
        token=settings.bot.token,
        default=DefaultBotProperties(parse_mode=settings.bot.parse_mode),
    )
