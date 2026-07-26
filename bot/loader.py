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
    # Keep link-preview defaults on the same legacy-compatible field used by
    # per-message overrides.  Passing link_preview_is_disabled to the
    # constructor also materializes link_preview_options in aiogram; sending
    # both can make a per-item False override conflict with the global True.
    default = DefaultBotProperties(parse_mode=settings.bot.parse_mode)
    default.link_preview_is_disabled = bool(
        getattr(settings.bot, "disable_link_preview", True)
    )
    return Bot(
        token=settings.bot.token,
        session=PriorityAiohttpSession(timeout=_TELEGRAM_HTTP_TIMEOUT_SECONDS),
        default=default,
    )
