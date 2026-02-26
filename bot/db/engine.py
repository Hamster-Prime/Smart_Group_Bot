from __future__ import annotations

import logging
from pathlib import Path

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy import text

from bot.db.models import Base

log = logging.getLogger(__name__)


async def init_db(
    url: str = "sqlite+aiosqlite:///./data/bot.db",
) -> tuple[AsyncEngine, async_sessionmaker[AsyncSession]]:
    """Create engine, ensure tables exist, return engine + session factory."""
    # Ensure data directory exists for SQLite
    if "sqlite" in url:
        db_path = url.split("///")[-1]
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)

    engine = create_async_engine(
        url,
        echo=False,
        connect_args={"timeout": 60} if "sqlite" in url else {},
    )

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        # Enable WAL mode for concurrent read/write
        if "sqlite" in url:
            await conn.execute(text("PRAGMA journal_mode=WAL"))
            await conn.execute(text("PRAGMA busy_timeout=60000"))
            await conn.execute(text("PRAGMA synchronous=NORMAL"))
            # Migrate: add embedding column if missing
            result = await conn.execute(text("PRAGMA table_info(knowledge_entries)"))
            columns = [row[1] for row in result.fetchall()]
            if "embedding" not in columns:
                await conn.execute(text(
                    "ALTER TABLE knowledge_entries ADD COLUMN embedding BLOB"
                ))
                log.info("Migrated: added embedding column to knowledge_entries")
        # Create FTS5 virtual table for knowledge search
        await conn.execute(text(
            "CREATE VIRTUAL TABLE IF NOT EXISTS knowledge_fts "
            "USING fts5(title, content, content=knowledge_entries, content_rowid=id)"
        ))

    session_factory = async_sessionmaker(
        engine,
        expire_on_commit=False,
        autoflush=False,
    )
    log.info("Database initialized: %s", url)
    return engine, session_factory
