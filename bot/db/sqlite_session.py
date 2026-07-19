from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncSession

log = logging.getLogger(__name__)

_SQLITE_WRITE_LOCK_TIMEOUT_SECONDS = 5.0
_SQLITE_WRITE_LOCK_INFO_SECONDS = 0.1
_SQLITE_WRITE_LOCK_WARN_SECONDS = 1.0


def _sqlite_write_lock() -> asyncio.Lock:
    loop = asyncio.get_running_loop()
    lock = getattr(loop, "_smart_group_bot_sqlite_write_lock", None)
    if lock is None:
        lock = asyncio.Lock()
        setattr(loop, "_smart_group_bot_sqlite_write_lock", lock)
    return lock


def _sqlite_write_lock_owner() -> dict[str, Any] | None:
    loop = asyncio.get_running_loop()
    owner = getattr(loop, "_smart_group_bot_sqlite_write_lock_owner", None)
    return owner if isinstance(owner, dict) else None


def _set_sqlite_write_lock_owner(owner: dict[str, Any] | None) -> None:
    loop = asyncio.get_running_loop()
    setattr(loop, "_smart_group_bot_sqlite_write_lock_owner", owner)


def is_database_locked_error(exc: BaseException) -> bool:
    detail = str(getattr(exc, "orig", exc)).lower()
    return (
        "database is locked" in detail
        or "database table is locked" in detail
        or "sqlite write lock timeout" in detail
    )


def _statement_is_write(statement: Any) -> bool:
    for attr in ("is_insert", "is_update", "is_delete", "is_dml"):
        if bool(getattr(statement, attr, False)):
            return True
    return False


class SQLiteSafeAsyncSession(AsyncSession):
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._sqlite_write_lock_held: asyncio.Lock | None = None

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
        lock = _sqlite_write_lock()
        try:
            async with asyncio.timeout(_SQLITE_WRITE_LOCK_TIMEOUT_SECONDS):
                await lock.acquire()
        except TimeoutError as exc:
            owner = _sqlite_write_lock_owner() or {}
            held_for_ms = 0
            acquired_at = owner.get("acquired_at")
            if isinstance(acquired_at, (int, float)):
                held_for_ms = max(0, int((time.perf_counter() - acquired_at) * 1000))
            log.error(
                "sqlite write lock timeout | op=%s wait_ms=%d owner_session=%s "
                "owner_op=%s held_ms=%d",
                op,
                int((time.perf_counter() - started) * 1000),
                owner.get("session_id", "unknown"),
                owner.get("op", "unknown"),
                held_for_ms,
            )
            raise OperationalError(
                "sqlite write lock acquire",
                None,
                TimeoutError(
                    f"sqlite write lock timeout after "
                    f"{_SQLITE_WRITE_LOCK_TIMEOUT_SECONDS:.1f}s"
                ),
            ) from exc
        self._sqlite_write_lock_held = lock
        acquired_at = time.perf_counter()
        _set_sqlite_write_lock_owner(
            {
                "session_id": id(self),
                "op": op,
                "acquired_at": acquired_at,
            }
        )
        waited_ms = int((time.perf_counter() - started) * 1000)
        if waited_ms >= int(_SQLITE_WRITE_LOCK_WARN_SECONDS * 1000):
            log.warning("sqlite write serialized | op=%s wait_ms=%d", op, waited_ms)
        elif waited_ms >= int(_SQLITE_WRITE_LOCK_INFO_SECONDS * 1000):
            log.info("sqlite write serialized | op=%s wait_ms=%d", op, waited_ms)

    def _release_write_lock(self) -> None:
        if not self._sqlite_write_lock_held:
            return
        lock = self._sqlite_write_lock_held
        self._sqlite_write_lock_held = None
        owner = _sqlite_write_lock_owner()
        if owner and owner.get("session_id") == id(self):
            acquired_at = owner.get("acquired_at")
            held_ms = 0
            if isinstance(acquired_at, (int, float)):
                held_ms = max(0, int((time.perf_counter() - acquired_at) * 1000))
            if held_ms >= int(_SQLITE_WRITE_LOCK_WARN_SECONDS * 1000):
                log.warning(
                    "sqlite write lock released | session=%s op=%s held_ms=%d",
                    id(self),
                    owner.get("op", "unknown"),
                    held_ms,
                )
            elif held_ms >= int(_SQLITE_WRITE_LOCK_INFO_SECONDS * 1000):
                log.info(
                    "sqlite write lock released | session=%s op=%s held_ms=%d",
                    id(self),
                    owner.get("op", "unknown"),
                    held_ms,
                )
            _set_sqlite_write_lock_owner(None)
        lock.release()

    async def _rollback_failed_operation(
        self,
        *,
        op: str,
        failure: BaseException,
    ) -> None:
        """Best-effort rollback without masking the triggering failure.

        Call the base implementation directly so failure handling cannot
        recurse through :meth:`rollback`. Full rollbacks release the process
        write lock; a successfully rolled-back savepoint retains it for the
        active outer transaction. A rollback error is logged but must not
        replace the original exception seen by the caller.
        """

        # Integrity/statement failures inside ``begin_nested()`` are expected
        # conflict-control flow in several upsert paths. Roll back only that
        # SAVEPOINT and retain the process lock for the still-active outer
        # transaction; releasing it here would let another session write while
        # SQLite may still hold the outer RESERVED lock. Cancellation and
        # OperationalError invalidate the whole operation and therefore always
        # take the full rollback path below.
        try:
            nested = self.get_nested_transaction()
        except BaseException:
            nested = None
            log.exception(
                "sqlite nested-transaction lookup after failed operation also failed | "
                "op=%s session=%s",
                op,
                id(self),
            )
        if (
            nested is not None
            and op != "commit"
            and not isinstance(failure, (asyncio.CancelledError, OperationalError))
        ):
            try:
                await nested.rollback()
                return
            except BaseException:
                log.exception(
                    "sqlite savepoint rollback after failed operation also failed | "
                    "op=%s session=%s",
                    op,
                    id(self),
                )

        try:
            await super().rollback()
        except BaseException:
            log.exception(
                "sqlite rollback after failed operation also failed | "
                "op=%s session=%s",
                op,
                id(self),
            )
        finally:
            self._release_write_lock()

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
        try:
            if should_guard:
                await self._acquire_write_lock(op="execute")
            return await parent_execute(
                statement,
                params=params,
                execution_options=execution_options,
                bind_arguments=bind_arguments,
                **kw,
            )
        except BaseException as exc:
            await self._rollback_failed_operation(op="execute", failure=exc)
            raise

    async def flush(self, objects: Any | None = None) -> None:
        should_guard = self._uses_sqlite() and (objects is not None or self._has_pending_writes())
        try:
            if should_guard:
                await self._acquire_write_lock(op="flush")
            await super().flush(objects=objects)
        except BaseException as exc:
            await self._rollback_failed_operation(op="flush", failure=exc)
            raise

    async def commit(self) -> None:
        should_guard = self._uses_sqlite() and (self._sqlite_write_lock_held or self._has_pending_writes())
        try:
            if should_guard:
                await self._acquire_write_lock(op="commit")
            await super().commit()
        except BaseException as exc:
            await self._rollback_failed_operation(op="commit", failure=exc)
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
