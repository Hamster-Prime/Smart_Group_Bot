from __future__ import annotations

import asyncio
import logging
from contextlib import suppress

from bot.config import load_settings
from bot.db.engine import init_db
from bot.handlers import admin, commands, group
from bot.loader import create_bot, dp
from bot.middlewares.db import DbSessionMiddleware
from bot.middlewares.logging_mw import LoggingMiddleware
from bot.middlewares.throttle import ThrottleMiddleware
from bot.services import memory_holder
from bot.services.knowledge import KnowledgeService
from bot.services.llm import LLMService
from bot.services.memory import MemoryService
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
        embed=settings.bot.embed_model,
    )
    memory = MemoryService(
        settings.bot,
        llm,
        session_factory=session_factory,
        memory_v2=settings.memory_v2,
    )
    await memory.bootstrap()
    memory_holder.init(memory)

    kb = KnowledgeService(settings.knowledge, llm)
    async with session_factory() as session:
        count = await kb.backfill_embeddings(session)
        if count:
            await session.commit()

    dp.message.middleware(LoggingMiddleware())
    dp.message.middleware(ThrottleMiddleware(rate_limit=1.0))
    dp.message.middleware(DbSessionMiddleware(session_factory))
    dp.callback_query.middleware(DbSessionMiddleware(session_factory))

    dp.include_router(commands.router)
    dp.include_router(admin.router)
    dp.include_router(group.router)

    bot = create_bot(settings)
    log.info("Bot starting...")

    async def _periodic_memory_maintenance() -> None:
        while True:
            await asyncio.sleep(3600)
            try:
                stats = await memory.maybe_run_daily_memory_maintenance()
                if any(int(v) > 0 for v in stats.values()):
                    log.info(
                        "daily memory maintenance: groups=%d consolidated_messages=%d facts=%d preferences=%d pruned=%d",
                        stats.get("groups", 0),
                        stats.get("consolidated_messages", 0),
                        stats.get("facts", 0),
                        stats.get("preferences", 0),
                        stats.get("pruned", 0),
                    )
            except Exception:
                log.exception("daily memory maintenance failed")

    maintenance_task = asyncio.create_task(
        _periodic_memory_maintenance(),
        name="memory-v2-maintenance",
    )

    try:
        await dp.start_polling(
            bot,
            allowed_updates=dp.resolve_used_update_types(),
            drop_pending_updates=settings.bot.drop_pending_updates,
            handle_as_tasks=True,
            tasks_concurrency_limit=8,
        )
    finally:
        maintenance_task.cancel()
        with suppress(asyncio.CancelledError):
            await maintenance_task
        await memory.flush_background_tasks(timeout_sec=5.0)
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
