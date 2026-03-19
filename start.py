"""Local launcher for Telegram bot polling mode."""

from __future__ import annotations

import asyncio
import importlib.util
import logging
import os
import subprocess
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).parent
os.chdir(ROOT)
sys.path.insert(0, str(ROOT))

from bot.utils.logging_setup import configure_logging

configure_logging()
log = logging.getLogger("launcher")


REQUIRED_MODULES = (
    "aiogram",
    "sqlalchemy",
    "litellm",
    "pydantic",
    "pydantic_settings",
    "dotenv",
)


def _missing_modules(modules: tuple[str, ...]) -> list[str]:
    return [name for name in modules if importlib.util.find_spec(name) is None]


def ensure_dependencies() -> None:
    """Best-effort one-click dependency bootstrap."""
    missing = _missing_modules(REQUIRED_MODULES)
    if not missing:
        return

    log.warning("Missing dependencies detected: %s", ", ".join(missing))
    log.info("Installing project dependencies via: %s -m pip install -e .", sys.executable)
    result = subprocess.run(
        [sys.executable, "-m", "pip", "install", "-e", "."],
        cwd=str(ROOT),
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError("Dependency installation failed, please check pip/network and retry.")

    still_missing = _missing_modules(REQUIRED_MODULES)
    if still_missing:
        raise RuntimeError("Dependencies still missing after install: " + ", ".join(still_missing))
    log.info("Dependency bootstrap finished")


def preflight() -> None:
    """Validate local runtime prerequisites before startup."""
    env_path = Path(".env")
    if not env_path.exists():
        example = Path(".env.example")
        if example.exists():
            shutil.copy(example, env_path)
            log.warning("Created .env from .env.example, please fill BOT_TOKEN first")
            print("\nPlease edit .env and set BOT_TOKEN, then retry.\n")
            sys.exit(1)
        log.error("Missing .env and .env.example")
        sys.exit(1)

    from dotenv import load_dotenv

    load_dotenv()
    configure_logging(force=True)
    token = os.getenv("BOT_TOKEN", "").strip()
    if not token or token == "your_bot_token_here":
        print("\nPlease set a valid BOT_TOKEN in .env and retry.\n")
        sys.exit(1)
    if not os.getenv("SUPER_ADMIN_ID", "").strip():
        log.warning("SUPER_ADMIN_ID is empty, group authorization commands will be unavailable")

    Path("data").mkdir(exist_ok=True)
    log.info("Preflight passed")


async def start_bot(settings, session_factory) -> None:
    """Start telegram polling and memory flush task."""
    from bot.handlers import admin, commands, group
    from bot.loader import create_bot, dp
    from bot.middlewares.db import DbSessionMiddleware
    from bot.middlewares.logging_mw import LoggingMiddleware
    from bot.middlewares.throttle import ThrottleMiddleware
    from bot.services import memory_holder
    from bot.services.llm import LLMService
    from bot.services.memory import MemoryService
    from bot.services.scheduled_tasks import ScheduledTaskService
    from bot.services.skills import SkillService

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
    )
    await memory.bootstrap()
    memory_holder.init(memory)
    sticker_pool = [
        x.strip()
        for x in (settings.skill_sticker_file_ids or "").split(",")
        if x and x.strip()
    ]
    scheduler_skill = SkillService(llm, default_sticker_file_ids=sticker_pool)

    dp.message.middleware(LoggingMiddleware())
    dp.message.middleware(ThrottleMiddleware(rate_limit=0.0))
    dp.message.middleware(DbSessionMiddleware(session_factory))
    dp.callback_query.middleware(DbSessionMiddleware(session_factory))

    dp.include_router(commands.router)
    dp.include_router(admin.router)
    dp.include_router(group.router)

    bot = create_bot(settings)
    scheduled_tasks = ScheduledTaskService(
        settings=settings,
        bot=bot,
        memory=memory,
        session_factory=session_factory,
        skill_service=scheduler_skill,
    )
    scheduled_task_runner = asyncio.create_task(
        scheduled_tasks.run_forever(),
        name="scheduled-task-runner",
    )
    log.info("Bot starting in polling mode")

    try:
        await dp.start_polling(
            bot,
            allowed_updates=dp.resolve_used_update_types(),
            drop_pending_updates=settings.bot.drop_pending_updates,
            handle_as_tasks=True,
            tasks_concurrency_limit=8,
        )
    finally:
        scheduled_task_runner.cancel()
        await asyncio.gather(scheduled_task_runner, return_exceptions=True)
        try:
            await group.flush_pending_inbound_batches()
            log.info("pending inbound batches flushed")
        except Exception:
            log.exception("pending inbound batch flush failed")


async def main() -> None:
    from bot.config import load_settings
    from bot.db.engine import init_db

    settings = load_settings()
    engine, session_factory = await init_db(settings.database_url)

    print()
    print("=" * 50)
    print("  Smart Group Bot - Local Run")
    print("=" * 50)
    print("  Mode: Telegram polling")
    print("  Press Ctrl+C to stop")
    print("=" * 50)
    print()

    try:
        await start_bot(settings, session_factory)
    except asyncio.CancelledError:
        pass
    finally:
        await engine.dispose()
        log.info("all services stopped")


if __name__ == "__main__":
    try:
        ensure_dependencies()
        preflight()
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nExited.")
    except Exception as exc:
        log.error("Startup failed: %s", exc)
        sys.exit(1)
