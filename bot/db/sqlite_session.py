from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncSession

log = logging.getLogger(__name__)

_SQLITE_WRITE_LOCK = asyncio.Lock()


def is_database_locked_error(exc: BaseException) -> bool:
    detail = str(getattr(exc, "orig", exc)).lower()
    return "database is locked" in detail or "database table is locked" in detail


def _statement_is_write(statement: Any) -> bool:
    for attr in ("is_insert", "is_update", "is_delete", "is_dml"):
        if bool(getattr(statement, attr, False)):
            return True
    return False


class SQLiteSafeAsyncSession(AsyncSession):
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._sqlite_write_lock_held = False

    def _uses_sqlite(self) -> bool:
        try:
            bind = self.sync_session.get_bind()
        except Exception:
            bind = getattr(self.sync_session, "bind", None)
        dialect = getattr(bind, "dialect", None)
        return str(getattr(dialect, "name", "") or "").lower() == "sqlite"

    def _has_pending_writes(self) -> bool:
        sync_session = self.sync_session
        return bool(sync_session.new or sync_session.dirty or sync_session.deleted)

    async def _acquire_write_lock(self, *, op: str) -> None:
        if not self._uses_sqlite() or self._sqlite_write_lock_held:
            return
        started = time.perf_counter()
        await _SQLITE_WRITE_LOCK.acquire()
        self._sqlite_write_lock_held = True
        waited_ms = int((time.perf_counter() - started) * 1000)
        if waited_ms >= 100:
            log.info("sqlite write serialized | op=%s wait_ms=%d", op, waited_ms)

    def _release_write_lock(self) -> None:
        if not self._sqlite_write_lock_held:
            return
        self._sqlite_write_lock_held = False
        _SQLITE_WRITE_LOCK.release()

    async def execute(
        self,
        statement: Any,
        params: Any | None = None,
        *,
        execution_options: Any | None = None,
        bind_arguments: Any | None = None,
        **kw: Any,
    ) -> Any:
        parent_execute = super().execute
        should_guard = self._uses_sqlite() and _statement_is_write(statement)
        acquired_here = False
        if should_guard:
            acquired_here = not self._sqlite_write_lock_held
            await self._acquire_write_lock(op="execute")
        try:
            return await parent_execute(
                statement,
                params=params,
                execution_options=execution_options,
                bind_arguments=bind_arguments,
                **kw,
            )
        except OperationalError:
            try:
                await super().rollback()
            finally:
                self._release_write_lock()
            raise
        except Exception:
            if acquired_here:
                self._release_write_lock()
            raise

    async def flush(self, objects: Any | None = None) -> None:
        should_guard = self._uses_sqlite() and (objects is not None or self._has_pending_writes())
        acquired_here = False
        if should_guard:
            acquired_here = not self._sqlite_write_lock_held
            await self._acquire_write_lock(op="flush")
        try:
            await super().flush(objects=objects)
        except OperationalError:
            try:
                await super().rollback()
            finally:
                self._release_write_lock()
            raise
        except Exception:
            if acquired_here:
                self._release_write_lock()
            raise

    async def commit(self) -> None:
        should_guard = self._uses_sqlite() and (self._sqlite_write_lock_held or self._has_pending_writes())
        if should_guard:
            await self._acquire_write_lock(op="commit")
        try:
            await super().commit()
        except Exception:
            try:
                await super().rollback()
            finally:
                self._release_write_lock()
            raise
        else:
            self._release_write_lock()

    async def rollback(self) -> None:
        try:
            await super().rollback()
        finally:
            self._release_write_lock()

    async def close(self) -> None:
        try:
            await super().close()
        finally:
            self._release_write_lock()
