from __future__ import annotations

import asyncio
import logging

from bot.config import load_settings
from bot.db.engine import init_db
from bot.handlers import admin, commands, group, membership
from bot.loader import create_bot, dp
from bot.middlewares.db import DbSessionMiddleware
from bot.middlewares.global_ban import GlobalBanEnforcementMiddleware
from bot.middlewares.logging_mw import LoggingMiddleware
from bot.middlewares.profile_screen import ProfileScreenEnforcementMiddleware
from bot.services import memory_holder
from bot.services.join_verification import (
    JoinVerificationSweeper,
    verification_service_ready,
    warn_if_bot_cannot_verify,
)
from bot.services.llm import LLMService
from bot.services.memory import MemoryService
from bot.services.proactive import ProactiveTopicService
from bot.utils.bot_identity import set_bot_identity
from bot.utils.logging_setup import configure_logging

configure_logging()
log = logging.getLogger(__name__)


async def main() -> None:
    settings = load_settings()

    engine, session_factory = await init_db(settings.database_url)

    dp["settings"] = settings
    dp["session_factory"] = session_factory

    llm = LLMService(
        settings.bot.main_model,
        settings.bot.decision_model,
        settings.bot.compress_model,
        moderation=settings.bot.moderation_model,
        vision=settings.bot.vision_model,
        embed=settings.bot.embed_model,
        max_context_tokens=settings.bot.max_context_tokens,
    )
    memory = MemoryService(
        settings.bot,
        llm,
        session_factory=session_factory,
    )
    await memory.bootstrap()
    memory_holder.init(memory)

    dp.message.outer_middleware(GlobalBanEnforcementMiddleware(session_factory))
    # Outer so it also covers matched commands (/help), videos, and empty-text
    # media that the group handler bypasses.
    dp.message.outer_middleware(ProfileScreenEnforcementMiddleware(session_factory))
    dp.message.middleware(LoggingMiddleware())
    # No throttle middleware: it silently drops rapid consecutive messages,
    # which breaks inbound batch merging and lets a fast second violating
    # message skip moderation. Burst smoothing is handled by the pending-reply
    # debounce in bot.handlers.group instead.
    dp.message.middleware(DbSessionMiddleware(session_factory))
    dp.callback_query.middleware(DbSessionMiddleware(session_factory))
    dp.chat_member.middleware(DbSessionMiddleware(session_factory))

    dp.include_router(commands.router)
    dp.include_router(admin.router)
    dp.include_router(membership.router)
    dp.include_router(group.router)

    bot = create_bot(settings)
    me = await bot.me()
    set_bot_identity(
        user_id=me.id,
        username=me.username or "",
        display_name=me.full_name or me.first_name or "",
    )
    log.info("Bot identity resolved: @%s (%s)", me.username, me.full_name)
    proactive = ProactiveTopicService(
        settings=settings,
        bot=bot,
        memory=memory,
        session_factory=session_factory,
        llm=llm,
    )
    proactive_runner = asyncio.create_task(
        proactive.run_forever(),
        name="proactive-topic-runner",
    )
    sweeper = JoinVerificationSweeper(
        bot=bot,
        session_factory=session_factory,
        check_interval_seconds=settings.join_verification_check_interval_seconds,
    )
    # Always drain persisted deadlines. Operators may disable a flow or remove
    # its web configuration while records from an earlier run still exist.
    verification_runner = asyncio.create_task(
        sweeper.run_forever(),
        name="verification-sweeper",
    )
    verify_web = None
    if verification_service_ready(settings):
        from bot.services.verify_web import VerifyWebServer

        await warn_if_bot_cannot_verify(bot, settings, session_factory)
        verify_web = VerifyWebServer(
            bot=bot,
            settings=settings,
            session_factory=session_factory,
        )
        await verify_web.start()
    elif settings.moderation.enabled:
        log.warning(
            "消息审核已启用，但 Turnstile 密钥或公网验证地址不完整；"
            "低置信度命中将无法发起真人质询，并回退到原群规动作。"
        )
    log.info("Bot starting...")

    if settings.bot.drop_pending_updates:
        # aiogram 3.x start_polling has no drop_pending_updates parameter;
        # discarding the backlog is done via delete_webhook before polling.
        try:
            await bot.delete_webhook(drop_pending_updates=True)
        except Exception:
            log.warning("failed to drop pending updates", exc_info=True)

    try:
        await dp.start_polling(
            bot,
            allowed_updates=dp.resolve_used_update_types(),
            handle_as_tasks=True,
            tasks_concurrency_limit=8,
        )
    finally:
        proactive_runner.cancel()
        await asyncio.gather(proactive_runner, return_exceptions=True)
        verification_runner.cancel()
        await asyncio.gather(verification_runner, return_exceptions=True)
        if verify_web is not None:
            try:
                await verify_web.stop()
            except Exception:
                log.exception("verification web server shutdown failed")
        try:
            await group.flush_pending_inbound_batches()
        except Exception:
            log.exception("pending inbound batch flush failed")
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
