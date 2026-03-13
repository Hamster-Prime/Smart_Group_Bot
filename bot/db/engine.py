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


async def _sqlite_table_columns(conn, table: str) -> set[str]:
    result = await conn.execute(text(f"PRAGMA table_info({table})"))
    return {str(row[1]) for row in result.fetchall()}


async def _sqlite_ensure_column(conn, table: str, column: str, column_def_sql: str) -> bool:
    columns = await _sqlite_table_columns(conn, table)
    if column in columns:
        return False
    await conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {column_def_sql}"))
    log.info("Migrated: added %s.%s", table, column)
    return True


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
            # Ensure message_vectors schema keeps compatibility with previous versions.
            await _sqlite_ensure_column(
                conn,
                "message_vectors",
                "role",
                "role VARCHAR(16) NOT NULL DEFAULT 'user'",
            )
            await _sqlite_ensure_column(
                conn,
                "message_vectors",
                "content",
                "content TEXT NOT NULL DEFAULT ''",
            )
            await _sqlite_ensure_column(
                conn,
                "message_vectors",
                "importance_score",
                "importance_score FLOAT NOT NULL DEFAULT 0.5",
            )
            await _sqlite_ensure_column(
                conn,
                "message_vectors",
                "access_count",
                "access_count INTEGER NOT NULL DEFAULT 0",
            )
            await _sqlite_ensure_column(
                conn,
                "message_vectors",
                "vector_id",
                "vector_id VARCHAR(64) NOT NULL DEFAULT ''",
            )
            await _sqlite_ensure_column(
                conn,
                "message_vectors",
                "last_accessed",
                "last_accessed DATETIME",
            )
            await conn.execute(
                text(
                    "UPDATE message_vectors "
                    "SET vector_id = message_id "
                    "WHERE vector_id IS NULL OR vector_id = ''"
                )
            )
            await conn.execute(
                text(
                    "CREATE INDEX IF NOT EXISTS ix_message_vectors_group_created "
                    "ON message_vectors (group_id, created_at)"
                )
            )
            await conn.execute(
                text(
                    "CREATE INDEX IF NOT EXISTS ix_message_vectors_group_importance "
                    "ON message_vectors (group_id, importance_score)"
                )
            )
            await conn.execute(
                text(
                    "CREATE INDEX IF NOT EXISTS ix_message_vectors_message_id "
                    "ON message_vectors (message_id)"
                )
            )

    session_factory = async_sessionmaker(
        engine,
        expire_on_commit=False,
        autoflush=False,
    )
    log.info("Database initialized: %s", url)
    return engine, session_factory
