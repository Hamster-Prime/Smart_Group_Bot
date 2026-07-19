from __future__ import annotations

import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from sqlalchemy.exc import IntegrityError

from bot.config import Settings
from bot.db.engine import init_db
from bot.handlers import admin
from bot.services.authz import (
    authorize_group,
    authorize_group_admin,
    deauthorize_group,
    is_group_admin_authorized,
)


class AuthorizationConsistencyTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        fd, self.db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.engine, self.session_factory = await init_db(
            f"sqlite+aiosqlite:///{self.db_path}"
        )

    async def asyncTearDown(self) -> None:
        await self.engine.dispose()
        for suffix in ("", "-wal", "-shm"):
            try:
                os.remove(self.db_path + suffix)
            except OSError:
                pass

    async def test_deauthorizing_group_removes_delegated_admins(self) -> None:
        async with self.session_factory() as session:
            await authorize_group(session, -100, 1)
            await authorize_group_admin(session, -100, 42)
            await session.commit()
            self.assertTrue(await is_group_admin_authorized(session, -100, 42))

            self.assertTrue(await deauthorize_group(session, -100))
            await session.commit()
            await authorize_group(session, -100, 1)
            await session.commit()

            self.assertFalse(await is_group_admin_authorized(session, -100, 42))

    async def test_foreign_key_rejects_admin_without_group_grant(self) -> None:
        async with self.session_factory() as session:
            from bot.db.models import Admin

            session.add(Admin(group_id=-999, user_id=42, role="admin"))
            with self.assertRaises(IntegrityError):
                await session.commit()
            await session.rollback()
            self.assertFalse(await deauthorize_group(session, -999))
            await session.commit()
            self.assertFalse(await is_group_admin_authorized(session, -999, 42))

    async def test_authadmin_rejects_an_unauthorized_target_group(self) -> None:
        settings = Settings(_env_file=None)
        settings.super_admin_id = 1
        message = SimpleNamespace(
            chat=SimpleNamespace(id=1, type="private"),
            from_user=SimpleNamespace(id=1),
            text="/authadmin -999 42",
            reply_to_message=None,
        )
        session = SimpleNamespace(commit=AsyncMock())
        with (
            patch(
                "bot.handlers.admin.ensure_group_authorized",
                new=AsyncMock(return_value=True),
            ),
            patch(
                "bot.handlers.admin.ensure_super_admin",
                new=AsyncMock(return_value=True),
            ),
            patch(
                "bot.handlers.admin.is_group_authorized",
                new=AsyncMock(return_value=False),
            ),
            patch(
                "bot.handlers.admin.authorize_group_admin",
                new=AsyncMock(),
            ) as authorize_admin,
            patch("bot.handlers.admin._answer", new=AsyncMock()) as answer,
        ):
            await admin.cmd_authadmin(message, session=session, settings=settings)

        authorize_admin.assert_not_awaited()
        session.commit.assert_awaited_once()
        self.assertIn("目标群组尚未授权", answer.await_args.args[2])


if __name__ == "__main__":
    unittest.main()
