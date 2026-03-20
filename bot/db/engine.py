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
_SQLITE_SCHEMA_VERSION = 1


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


async def _sqlite_get_user_version(conn) -> int:
    result = await conn.execute(text("PRAGMA user_version"))
    row = result.first()
    return int(row[0] or 0) if row else 0


async def _sqlite_set_user_version(conn, version: int) -> None:
    await conn.execute(text(f"PRAGMA user_version = {int(version)}"))


async def _sqlite_migrate_message_vector_timestamps(conn) -> bool:
    user_version = await _sqlite_get_user_version(conn)
    if user_version >= _SQLITE_SCHEMA_VERSION:
        return False

    columns = await _sqlite_table_columns(conn, "message_vectors")
    changed = False

    # Legacy SQLite rows were stored via CURRENT_TIMESTAMP (UTC). Shift them once to Asia/Shanghai.
    if "created_at" in columns:
        await conn.execute(
            text(
                "UPDATE message_vectors "
                "SET created_at = datetime(created_at, '+8 hours') "
                "WHERE created_at IS NOT NULL"
            )
        )
        changed = True
    if "last_accessed" in columns:
        await conn.execute(
            text(
                "UPDATE message_vectors "
                "SET last_accessed = datetime(last_accessed, '+8 hours') "
                "WHERE last_accessed IS NOT NULL"
            )
        )
        changed = True

    await _sqlite_set_user_version(conn, _SQLITE_SCHEMA_VERSION)
    if changed:
        log.info("Migrated: normalized message_vectors timestamps to Asia/Shanghai")
    return changed


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
            await _sqlite_ensure_column(
                conn,
                "message_vectors",
                "embedding",
                "embedding BLOB",
            )
            await _sqlite_ensure_column(
                conn,
                "message_vectors",
                "sender_id",
                "sender_id BIGINT",
            )
            await _sqlite_ensure_column(
                conn,
                "message_vectors",
                "sender_name",
                "sender_name TEXT NOT NULL DEFAULT ''",
            )
            await _sqlite_ensure_column(
                conn,
                "message_vectors",
                "message_type",
                "message_type VARCHAR(64) NOT NULL DEFAULT 'text'",
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
            await _sqlite_migrate_message_vector_timestamps(conn)

    session_factory = async_sessionmaker(
        engine,
        class_=SQLiteSafeAsyncSession,
        expire_on_commit=False,
        autoflush=False,
    )
    log.info("Database initialized: %s", url)
    return engine, session_factory
