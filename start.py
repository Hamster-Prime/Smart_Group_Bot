"""一键启动：仅 Telegram Bot（不再包含 Web/API/MiniApp）"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import sys
from contextlib import suppress
from pathlib import Path

ROOT = Path(__file__).parent
os.chdir(ROOT)
sys.path.insert(0, str(ROOT))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("launcher")


def preflight() -> None:
    """启动前检查。"""
    if not Path(".env").exists():
        if Path(".env.example").exists():
            shutil.copy(".env.example", ".env")
            log.warning("已从 .env.example 创建 .env，请先填入 BOT_TOKEN。")
            print("\n⚠️  请先编辑 .env，配置 BOT_TOKEN 后重试。\n")
            sys.exit(1)
        log.error("缺少 .env 与 .env.example")
        sys.exit(1)

    from dotenv import load_dotenv

    load_dotenv()
    token = os.getenv("BOT_TOKEN", "")
    if not token or token == "your_bot_token_here":
        print("\n⚠️  请在 .env 中设置有效 BOT_TOKEN 后重试。\n")
        sys.exit(1)

    Path("data").mkdir(exist_ok=True)
    log.info("预检通过 ✓")


async def start_bot(settings, session_factory) -> None:
    """启动 Telegram Bot。"""
    from bot.handlers import admin, commands, group
    from bot.loader import create_bot, dp
    from bot.middlewares.db import DbSessionMiddleware
    from bot.middlewares.logging_mw import LoggingMiddleware
    from bot.middlewares.throttle import ThrottleMiddleware
    from bot.services import memory_holder
    from bot.services.knowledge import KnowledgeService
    from bot.services.llm import LLMService
    from bot.services.memory import MemoryService

    dp["settings"] = settings
    dp["session_factory"] = session_factory

    llm = LLMService(
        settings.bot.main_model,
        settings.bot.decision_model,
        settings.bot.compress_model,
        embed=settings.bot.embed_model,
    )

    memory = MemoryService(settings.bot, llm)
    memory.load_all()
    memory_holder.init(memory)

    kb = KnowledgeService(settings.knowledge, llm)
    async with session_factory() as session:
        count = await kb.backfill_embeddings(session)
        if count:
            await session.commit()

    dp.message.middleware(LoggingMiddleware())
    dp.message.middleware(ThrottleMiddleware(rate_limit=0.0))
    dp.message.middleware(DbSessionMiddleware(session_factory))

    dp.include_router(commands.router)
    dp.include_router(admin.router)
    dp.include_router(group.router)

    bot = create_bot(settings)
    log.info("Bot 启动中...")

    async def _periodic_memory_compress() -> None:
        while True:
            await asyncio.sleep(3600)
            try:
                count = await memory.compress_all(force=True)
                log.info("定时记忆压缩完成: groups=%d", count)
            except Exception:
                log.exception("定时记忆压缩失败")

    compress_task = asyncio.create_task(_periodic_memory_compress(), name="memory-hourly-compress")

    try:
        await dp.start_polling(
            bot,
            allowed_updates=dp.resolve_used_update_types(),
            drop_pending_updates=settings.bot.drop_pending_updates,
            handle_as_tasks=True,
            tasks_concurrency_limit=64,
        )
    finally:
        compress_task.cancel()
        with suppress(asyncio.CancelledError):
            await compress_task
        try:
            count = await memory.compress_all(force=True)
            log.info("停止前记忆压缩完成: groups=%d", count)
        except Exception:
            log.exception("停止前记忆压缩失败")


async def main() -> None:
    from bot.config import load_settings
    from bot.db.engine import init_db

    settings = load_settings()
    engine, session_factory = await init_db(settings.database_url)

    print()
    print("=" * 50)
    print("  Smart Group Bot - 本地测试")
    print("=" * 50)
    print("  Bot: polling 模式")
    print("  Web/API/MiniApp: 已移除")
    print("=" * 50)
    print("  按 Ctrl+C 停止")
    print("=" * 50)
    print()

    try:
        await start_bot(settings, session_factory)
    except asyncio.CancelledError:
        pass
    finally:
        await engine.dispose()
        log.info("已停止所有服务")


if __name__ == "__main__":
    preflight()
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n已退出。")