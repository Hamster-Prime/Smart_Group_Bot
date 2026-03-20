from __future__ import annotations

import logging
from pathlib import Path

from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from bot.db.models import Base
from bot.db.sqlite_session import SQLiteSafeAsyncSession

log = logging.getLogger(__name__)

_SQLITE_TIMEOUT_SECONDS = 5
_SQLITE_BUSY_TIMEOUT_MS = _SQLITE_TIMEOUT_SECONDS * 1000


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
        connect_args={"timeout": _SQLITE_TIMEOUT_SECONDS} if "sqlite" in url else {},
    )

    if "sqlite" in url:
        @event.listens_for(engine.sync_engine, "connect")
        def _configure_sqlite_connection(dbapi_connection, _connection_record) -> None:
            cursor = dbapi_connection.cursor()
            try:
                cursor.execute("PRAGMA journal_mode=WAL")
                cursor.execute(f"PRAGMA busy_timeout={_SQLITE_BUSY_TIMEOUT_MS}")
                cursor.execute("PRAGMA synchronous=NORMAL")
            finally:
                cursor.close()

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        if "sqlite" in url:
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
            await conn.execute(
                text(
                    "CREATE INDEX IF NOT EXISTS ix_message_vectors_group_created "
                    "ON message_vectors (group_id, created_at)"
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
        class_=SQLiteSafeAsyncSession,
        expire_on_commit=False,
        autoflush=False,
    )
    log.info("Database initialized: %s", url)
    return engine, session_factory
