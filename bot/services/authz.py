from __future__ import annotations

from aiogram.types import Message
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from bot.config import Settings
from bot.db.models import AuthorizedGroup


def is_super_admin_user_id(user_id: int, settings: Settings) -> bool:
    return bool(settings.super_admin_id) and user_id == settings.super_admin_id


async def ensure_super_admin(message: Message, settings: Settings) -> bool:
    user = message.from_user
    if user and is_super_admin_user_id(user.id, settings):
        return True
    await message.answer("仅最高管理员可使用该命令。")
    return False


async def is_group_authorized(session: AsyncSession, group_id: int) -> bool:
    row = await session.get(AuthorizedGroup, group_id)
    return row is not None


async def authorize_group(session: AsyncSession, group_id: int, operator_id: int = 0) -> bool:
    row = await session.get(AuthorizedGroup, group_id)
    if row:
        return False
    session.add(AuthorizedGroup(group_id=group_id, authorized_by=operator_id or None))
    await session.flush()
    return True


async def deauthorize_group(session: AsyncSession, group_id: int) -> bool:
    row = await session.get(AuthorizedGroup, group_id)
    if not row:
        return False
    await session.delete(row)
    await session.flush()
    return True


async def list_authorized_groups(session: AsyncSession) -> list[AuthorizedGroup]:
    stmt = select(AuthorizedGroup).order_by(AuthorizedGroup.created_at.desc())
    result = await session.execute(stmt)
    return list(result.scalars().all())


async def ensure_group_authorized(
    message: Message,
    session: AsyncSession,
    settings: Settings,
    *,
    allow_super_admin: bool = True,
) -> bool:
    if not message.chat or message.chat.type not in ("group", "supergroup"):
        return True

    user = message.from_user
    if allow_super_admin and user and is_super_admin_user_id(user.id, settings):
        return True

    ok = await is_group_authorized(session, message.chat.id)
    if ok:
        return True

    await message.answer("无授权,禁止使用")
    return False

