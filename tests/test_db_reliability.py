from __future__ import annotations

import asyncio
import os
import sqlite3
import stat
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.ext.asyncio import AsyncSession

from bot.config import Settings
from bot.db.engine import init_db, _normalize_database_url, _warn_about_sqlite_shadow_paths
from bot.db.sqlite_session import (
    SQLiteSafeAsyncSession,
    _PrioritySQLiteWriteLock,
    is_database_locked_error,
)
from bot.middlewares.global_ban import GlobalBanEnforcementMiddleware
from bot.middlewares.profile_screen import ProfileScreenEnforcementMiddleware
from bot.services.join_verification import BanEnforcementResult
from bot.services.request_priority import ExecutionPriority, execution_priority_scope
from bot.services.join_screening import profile_screen_signature
from bot.services.request_priority import ExecutionPriority, execution_priority_scope


class _TrackedContext:
    def __init__(self, state: SimpleNamespace, session: object) -> None:
        self.state = state
        self.session = session

    async def __aenter__(self) -> object:
        self.state.active += 1
        return self.session

    async def __aexit__(self, exc_type, exc, traceback) -> bool:
        self.state.active -= 1
        return False


class DatabaseUrlTests(unittest.TestCase):
    def test_control_characters_are_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "control character"):
            _normalize_database_url("sqlite+aiosqlite:///./data/bot.db\r")
        with self.assertRaisesRegex(ValueError, "percent-encoded control"):
            _normalize_database_url("sqlite+aiosqlite:///./data/bot.db%0D")

    def test_outer_spaces_are_normalized_without_changing_path(self) -> None:
        parsed, path = _normalize_database_url(
            "  sqlite+aiosqlite:///./data/bot.db  "
        )
        self.assertEqual(parsed.drivername, "sqlite+aiosqlite")
        self.assertEqual(str(path), "data/bot.db")

    def test_tilde_path_is_expanded_in_the_engine_url_too(self) -> None:
        with TemporaryDirectory() as tmpdir, patch.dict(os.environ, {"HOME": tmpdir}):
            parsed, path = _normalize_database_url(
                "sqlite+aiosqlite:///~/data/bot.db"
            )
        expected = str(Path(tmpdir) / "data" / "bot.db")
        self.assertEqual(str(path), expected)
        self.assertEqual(parsed.database, expected)

    def test_clean_path_reports_legacy_shadow_sibling(self) -> None:
        with TemporaryDirectory() as tmpdir:
            clean = Path(tmpdir) / "bot.db"
            clean.touch()
            (Path(tmpdir) / "bot.db\r").touch()
            with self.assertLogs("bot.db.engine", level="ERROR") as captured:
                _warn_about_sqlite_shadow_paths(clean)
        self.assertIn("shadow database", "\n".join(captured.output).lower())

    def test_same_inode_shadow_symlink_is_not_ambiguous(self) -> None:
        with TemporaryDirectory() as tmpdir:
            clean = Path(tmpdir) / "bot.db"
            connection = sqlite3.connect(clean)
            connection.execute("CREATE TABLE runtime_config (revision INTEGER)")
            connection.commit()
            connection.close()
            (Path(tmpdir) / "bot.db\r").symlink_to(clean)

            _warn_about_sqlite_shadow_paths(clean)

    def test_nonempty_newer_shadow_fails_closed_for_empty_clean_path(self) -> None:
        with TemporaryDirectory() as tmpdir:
            clean = Path(tmpdir) / "bot.db"
            clean.touch()
            shadow = Path(tmpdir) / "bot.db\r"
            shadow.write_bytes(b"legacy sqlite data")
            with self.assertRaisesRegex(RuntimeError, "ambiguous SQLite shadow"):
                _warn_about_sqlite_shadow_paths(clean)

    def test_older_shadow_with_user_rows_still_fails_closed(self) -> None:
        with TemporaryDirectory() as tmpdir:
            clean = Path(tmpdir) / "bot.db"
            shadow = Path(tmpdir) / "bot.db\r"
            for db_path, group_id in ((clean, -100), (shadow, -200)):
                connection = sqlite3.connect(db_path)
                connection.executescript(
                    "CREATE TABLE runtime_config (revision INTEGER);"
                    "CREATE TABLE groups (id BIGINT PRIMARY KEY);"
                )
                connection.execute(
                    "INSERT INTO runtime_config (revision) VALUES (?)",
                    (20 if db_path == clean else 1,),
                )
                connection.execute("INSERT INTO groups (id) VALUES (?)", (group_id,))
                connection.commit()
                connection.close()
            # Reproduce the misleading case where the wrong clean DB was opened
            # later and therefore has the newer mtime.
            os.utime(shadow, (1, 1))

            with self.assertRaisesRegex(RuntimeError, "ambiguous SQLite shadow"):
                _warn_about_sqlite_shadow_paths(clean)


class SQLiteFilePermissionTests(unittest.IsolatedAsyncioTestCase):
    async def test_init_db_restricts_database_and_existing_sidecars(self) -> None:
        with TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "private.db"
            path.touch(mode=0o666)
            wal = Path(f"{path}-wal")
            shm = Path(f"{path}-shm")
            wal.touch(mode=0o666)
            shm.touch(mode=0o666)

            engine, _factory = await init_db(f"sqlite+aiosqlite:///{path}")
            try:
                self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
                for sidecar in (wal, shm):
                    if sidecar.exists():
                        self.assertEqual(stat.S_IMODE(sidecar.stat().st_mode), 0o600)
            finally:
                await engine.dispose()


class SQLiteWriteLockTests(unittest.IsolatedAsyncioTestCase):
    async def test_critical_waiter_overtakes_queued_normal_writer(self) -> None:
        lock = _PrioritySQLiteWriteLock()
        await lock.acquire()
        acquired: list[str] = []

        async def waiter(label: str) -> None:
            await lock.acquire()
            acquired.append(label)
            lock.release()

        with execution_priority_scope(ExecutionPriority.NORMAL):
            normal = asyncio.create_task(waiter("normal"))
        await asyncio.sleep(0)
        with execution_priority_scope(ExecutionPriority.CRITICAL):
            critical = asyncio.create_task(waiter("critical"))
        await asyncio.sleep(0)

        lock.release()
        await asyncio.wait_for(asyncio.gather(normal, critical), timeout=0.2)

        self.assertEqual(acquired, ["critical", "normal"])

    async def test_security_writer_overtakes_ordinary_waiters(self) -> None:
        owner = SQLiteSafeAsyncSession()
        ordinary = SQLiteSafeAsyncSession()
        critical = SQLiteSafeAsyncSession()
        for session in (owner, ordinary, critical):
            session._uses_sqlite = lambda: True  # type: ignore[method-assign]
        order: list[str] = []
        await owner._acquire_write_lock(op="owner")

        async def wait_for(session: SQLiteSafeAsyncSession, label: str, priority: ExecutionPriority) -> None:
            with execution_priority_scope(priority):
                await session._acquire_write_lock(op=label)
                order.append(label)
                session._release_write_lock()

        ordinary_task = asyncio.create_task(
            wait_for(ordinary, "ordinary", ExecutionPriority.NORMAL)
        )
        await asyncio.sleep(0)
        critical_task = asyncio.create_task(
            wait_for(critical, "critical", ExecutionPriority.CRITICAL)
        )
        await asyncio.sleep(0)
        owner._release_write_lock()
        await asyncio.gather(ordinary_task, critical_task)
        self.assertEqual(order, ["critical", "ordinary"])
        await owner.close()
        await ordinary.close()
        await critical.close()

    async def test_write_lock_wait_has_a_hard_deadline(self) -> None:
        owner = SQLiteSafeAsyncSession()
        waiter = SQLiteSafeAsyncSession()
        owner._uses_sqlite = lambda: True  # type: ignore[method-assign]
        waiter._uses_sqlite = lambda: True  # type: ignore[method-assign]
        await owner._acquire_write_lock(op="owner")
        try:
            with patch(
                "bot.db.sqlite_session._SQLITE_WRITE_LOCK_TIMEOUT_SECONDS",
                0.02,
            ):
                with self.assertRaises(OperationalError) as caught:
                    await waiter._acquire_write_lock(op="waiter")
            self.assertTrue(is_database_locked_error(caught.exception))
        finally:
            owner._release_write_lock()
            await owner.close()
            await waiter.close()

    async def test_cancellation_rolls_back_and_releases_write_lock(self) -> None:
        session = SQLiteSafeAsyncSession()
        session._uses_sqlite = lambda: True  # type: ignore[method-assign]
        statement = SimpleNamespace(
            is_insert=False,
            is_update=True,
            is_delete=False,
            is_dml=True,
        )
        rollback = AsyncMock()
        try:
            with (
                patch.object(
                    AsyncSession,
                    "execute",
                    new=AsyncMock(side_effect=asyncio.CancelledError),
                ),
                patch.object(AsyncSession, "rollback", new=rollback),
            ):
                with self.assertRaises(asyncio.CancelledError):
                    await session.execute(statement)
            self.assertFalse(session._sqlite_write_lock_held)
            rollback.assert_awaited_once()
        finally:
            await session.close()

    async def test_integrity_error_rolls_back_before_next_session_writes(self) -> None:
        owner = SQLiteSafeAsyncSession()
        waiter = SQLiteSafeAsyncSession()
        owner._uses_sqlite = lambda: True  # type: ignore[method-assign]
        waiter._uses_sqlite = lambda: True  # type: ignore[method-assign]
        statement = SimpleNamespace(
            is_insert=True,
            is_update=False,
            is_delete=False,
            is_dml=True,
        )
        written = object()
        execute = AsyncMock(
            side_effect=[
                IntegrityError("insert", {}, RuntimeError("duplicate")),
                written,
            ]
        )
        rollback = AsyncMock()
        try:
            with (
                patch.object(AsyncSession, "execute", new=execute),
                patch.object(AsyncSession, "rollback", new=rollback),
            ):
                with self.assertRaises(IntegrityError):
                    await owner.execute(statement)
                self.assertFalse(owner._sqlite_write_lock_held)
                rollback.assert_awaited_once()

                async with asyncio.timeout(0.1):
                    result = await waiter.execute(statement)
                self.assertIs(result, written)
                await waiter.rollback()
        finally:
            await owner.close()
            await waiter.close()

    async def test_rollback_failure_does_not_mask_statement_error(self) -> None:
        session = SQLiteSafeAsyncSession()
        session._uses_sqlite = lambda: True  # type: ignore[method-assign]
        statement = SimpleNamespace(
            is_insert=True,
            is_update=False,
            is_delete=False,
            is_dml=True,
        )
        statement_error = IntegrityError(
            "insert",
            {},
            RuntimeError("duplicate"),
        )
        try:
            with (
                patch.object(
                    AsyncSession,
                    "execute",
                    new=AsyncMock(side_effect=statement_error),
                ),
                patch.object(
                    AsyncSession,
                    "rollback",
                    new=AsyncMock(side_effect=RuntimeError("rollback failed")),
                ),
                self.assertLogs("bot.db.sqlite_session", level="ERROR"),
            ):
                with self.assertRaises(IntegrityError) as caught:
                    await session.execute(statement)
            self.assertIs(caught.exception, statement_error)
            self.assertFalse(session._sqlite_write_lock_held)
        finally:
            await session.close()

    async def test_nested_integrity_error_rolls_back_only_savepoint(self) -> None:
        session = SQLiteSafeAsyncSession()
        session._uses_sqlite = lambda: True  # type: ignore[method-assign]
        statement = SimpleNamespace(
            is_insert=True,
            is_update=False,
            is_delete=False,
            is_dml=True,
        )
        # ORM flush failures mark the SAVEPOINT inactive before surfacing the
        # IntegrityError, but rolling back that transaction object still
        # performs the required savepoint cleanup without touching the root.
        nested = SimpleNamespace(is_active=False, rollback=AsyncMock())
        root_rollback = AsyncMock()
        try:
            with (
                patch.object(
                    session,
                    "get_nested_transaction",
                    return_value=nested,
                ),
                patch.object(
                    AsyncSession,
                    "execute",
                    new=AsyncMock(
                        side_effect=IntegrityError(
                            "insert",
                            {},
                            RuntimeError("duplicate"),
                        )
                    ),
                ),
                patch.object(AsyncSession, "rollback", new=root_rollback),
            ):
                with self.assertRaises(IntegrityError):
                    await session.execute(statement)
            nested.rollback.assert_awaited_once()
            root_rollback.assert_not_awaited()
            self.assertIsNotNone(session._sqlite_write_lock_held)
        finally:
            session._release_write_lock()
            await session.close()


class OuterMiddlewareSessionLifetimeTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def _event() -> SimpleNamespace:
        return SimpleNamespace(
            chat=SimpleNamespace(id=-100, type="supergroup", ban=AsyncMock()),
            bot=SimpleNamespace(
                ban_chat_member=AsyncMock(return_value=True),
                get_chat_member=AsyncMock(
                    return_value=SimpleNamespace(status="kicked")
                ),
            ),
            from_user=SimpleNamespace(
                id=42,
                is_bot=False,
                full_name="Alice",
                username="alice",
            ),
            sender_chat=None,
            delete=AsyncMock(),
            text="hello",
        )

    async def test_global_ban_session_is_closed_before_downstream_handler(self) -> None:
        state = SimpleNamespace(active=0)
        session = object()
        middleware = GlobalBanEnforcementMiddleware(
            lambda: _TrackedContext(state, session)
        )

        async def handler(event, data):
            self.assertEqual(state.active, 0)
            return "handled"

        with (
            patch(
                "bot.middlewares.global_ban.is_group_authorized",
                new=AsyncMock(return_value=True),
            ),
            patch(
                "bot.middlewares.global_ban.is_globally_banned",
                new=AsyncMock(return_value=False),
            ),
        ):
            result = await middleware(
                handler,
                self._event(),
                {"settings": Settings(_env_file=None)},
            )
        self.assertEqual(result, "handled")

    async def test_global_ban_enforcement_uses_bounded_helper_after_session_close(
        self,
    ) -> None:
        state = SimpleNamespace(active=0)
        session = object()
        middleware = GlobalBanEnforcementMiddleware(
            lambda: _TrackedContext(state, session)
        )
        event = self._event()

        async def enforce(_bot, chat_id, user_id, preserve_ban, restriction_required):
            self.assertEqual(state.active, 0)
            self.assertEqual((chat_id, user_id), (-100, 42))
            self.assertTrue(callable(preserve_ban))
            self.assertTrue(callable(restriction_required))
            return BanEnforcementResult(final_banned=True)

        with (
            patch(
                "bot.middlewares.global_ban.is_group_authorized",
                new=AsyncMock(return_value=True),
            ),
            patch(
                "bot.middlewares.global_ban.is_globally_banned",
                new=AsyncMock(return_value=True),
            ),
            patch(
                "bot.middlewares.global_ban.enforce_ban_with_policy_reconciliation_result",
                side_effect=enforce,
            ) as ban_mock,
        ):
            result = await middleware(
                AsyncMock(return_value="handled"),
                event,
                {"settings": Settings(_env_file=None)},
            )

        self.assertIsNone(result)
        event.delete.assert_awaited_once()
        ban_mock.assert_awaited_once()
        self.assertEqual(ban_mock.await_args.args[:3], (event.bot, -100, 42))

    async def _run_unconfirmed_ban(
        self,
        *,
        retryable: bool,
    ) -> Mock:
        middleware = GlobalBanEnforcementMiddleware(
            lambda: _TrackedContext(SimpleNamespace(active=0), object())
        )
        retry = Mock(return_value=True)
        with (
            patch(
                "bot.middlewares.global_ban.is_group_authorized",
                new=AsyncMock(return_value=True),
            ),
            patch(
                "bot.middlewares.global_ban.is_globally_banned",
                new=AsyncMock(return_value=True),
            ),
            patch(
                "bot.middlewares.global_ban.enforce_ban_with_policy_reconciliation_result",
                new=AsyncMock(
                    return_value=BanEnforcementResult(
                        final_banned=None,
                        retryable=retryable,
                        operator_action_required=not retryable,
                    )
                ),
            ),
            patch(
                "bot.middlewares.global_ban.request_current_update_retry",
                new=retry,
            ),
        ):
            result = await middleware(
                AsyncMock(return_value="handled"),
                self._event(),
                {"settings": Settings(_env_file=None)},
            )
        self.assertIsNone(result)
        return retry

    async def test_unconfirmed_ban_requests_durable_retry_for_transient_failure(
        self,
    ) -> None:
        retry = await self._run_unconfirmed_ban(retryable=True)
        retry.assert_called_once()

    async def test_deterministic_rights_failure_completes_update_without_retry(
        self,
    ) -> None:
        # A group where the bot lacks "Ban users" produces the same failure on
        # every replay; retrying would trip the webhook failure threshold and
        # demote the transport for all groups.
        retry = await self._run_unconfirmed_ban(retryable=False)
        retry.assert_not_called()

    async def test_profile_cache_session_is_closed_before_downstream_handler(self) -> None:
        state = SimpleNamespace(active=0)
        session = object()
        middleware = ProfileScreenEnforcementMiddleware(
            lambda: _TrackedContext(state, session)
        )
        settings = Settings(_env_file=None)
        settings.moderation.enabled = True
        signature = profile_screen_signature(
            full_name="Alice",
            username="alice",
            rules_fingerprint="rules",
        )

        async def handler(event, data):
            self.assertEqual(state.active, 0)
            return "handled"

        with (
            patch(
                "bot.middlewares.profile_screen.is_group_authorized",
                new=AsyncMock(return_value=True),
            ),
            patch(
                "bot.middlewares.profile_screen.moderation_rules_fingerprint",
                new=AsyncMock(return_value="rules"),
            ),
            patch(
                "bot.middlewares.profile_screen.get_profile_screen_hash",
                new=AsyncMock(return_value=signature),
            ),
            patch(
                "bot.middlewares.profile_screen.get_global_ban",
                new=AsyncMock(return_value=None),
            ),
        ):
            result = await middleware(
                handler,
                self._event(),
                {"settings": settings},
            )
        self.assertEqual(result, "handled")


if __name__ == "__main__":
    unittest.main()
