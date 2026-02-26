import asyncio
import logging

from bot.config import load_settings
from bot.loader import create_bot, dp
from bot.db.engine import init_db
from bot.services.llm import LLMService
from bot.services.memory import MemoryService
from bot.services.knowledge import KnowledgeService
from bot.services import memory_holder
from bot.handlers import commands, admin, group
from bot.middlewares.db import DbSessionMiddleware
from bot.middlewares.throttle import ThrottleMiddleware
from bot.middlewares.logging_mw import LoggingMiddleware

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger(__name__)


async def main() -> None:
    settings = load_settings()

    # Init database
    engine, session_factory = await init_db(settings.database_url)

    # Store settings in dispatcher for access in handlers
    dp["settings"] = settings
    dp["session_factory"] = session_factory

    # Init memory service and load history from disk
    llm = LLMService(
        settings.bot.main_model,
        settings.bot.decision_model,
        settings.bot.compress_model,
        moderation=settings.bot.moderation_model,
        embed=settings.bot.embed_model,
    )
    memory = MemoryService(settings.bot, llm)
    memory.load_all()
    memory_holder.init(memory)

    # Backfill embeddings for existing knowledge entries
    kb = KnowledgeService(settings.knowledge, llm)
    async with session_factory() as session:
        count = await kb.backfill_embeddings(session)
        if count:
            await session.commit()

    # Register middlewares (outer → inner)
    dp.message.middleware(LoggingMiddleware())
    dp.message.middleware(ThrottleMiddleware(rate_limit=1.0))
    dp.message.middleware(DbSessionMiddleware(session_factory, memory=memory))

    # Register routers
    dp.include_router(commands.router)
    dp.include_router(admin.router)
    dp.include_router(group.router)

    bot = create_bot(settings)
    log.info("Bot starting...")

    try:
        await dp.start_polling(
            bot,
            allowed_updates=dp.resolve_used_update_types(),
            drop_pending_updates=settings.bot.drop_pending_updates,
        )
    finally:
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
