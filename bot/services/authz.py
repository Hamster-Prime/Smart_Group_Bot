from __future__ import annotations

import asyncio

from aiogram.types import Message
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from bot.config import Settings
from bot.db.models import Admin, AuthorizedGroup


def _schedule_auto_delete(sent: Message | None, settings: Settings) -> None:
    categories = {
        str(item or "").strip().lower()
        for item in getattr(settings.bot, "auto_delete_categories", [])
    }
    seconds = int(getattr(settings.bot, "auto_delete_seconds", 0) or 0)
    if not sent or seconds <= 0 or "management" not in categories:
        return

    async def _delete_later() -> None:
        await asyncio.sleep(seconds)
        try:
            await sent.delete()
        except Exception:
            pass

    try:
        asyncio.create_task(_delete_later(), name=f"auto-delete-authz:{sent.chat.id}:{sent.message_id}")
    except RuntimeError:
        pass


def is_super_admin_user_id(user_id: int, settings: Settings) -> bool:
    return bool(settings.super_admin_id) and user_id == settings.super_admin_id


async def ensure_super_admin(message: Message, settings: Settings) -> bool:
    user = message.from_user
    if user and is_super_admin_user_id(user.id, settings):
        return True
    sent = await message.answer("仅最高管理员可使用该命令。")
    _schedule_auto_delete(sent, settings)
    return False


async def is_group_authorized(session: AsyncSession, group_id: int) -> bool:
    row = await session.get(AuthorizedGroup, group_id)
    return row is not None


async def authorize_group(session: AsyncSession, group_id: int, operator_id: int = 0) -> bool:
    row = await session.get(AuthorizedGroup, group_id)
    if row:
        return False
    session.add(AuthorizedGroup(group_id=group_id, authorized_by=operator_id or None))
    return True


async def deauthorize_group(session: AsyncSession, group_id: int) -> bool:
    row = await session.get(AuthorizedGroup, group_id)
    if not row:
        return False
    await session.delete(row)
    return True


async def list_authorized_groups(session: AsyncSession) -> list[AuthorizedGroup]:
    stmt = select(AuthorizedGroup).order_by(AuthorizedGroup.created_at.desc())
    result = await session.execute(stmt)
    return list(result.scalars().all())


async def is_group_admin_authorized(session: AsyncSession, group_id: int, user_id: int) -> bool:
    stmt = select(Admin.id).where(
        Admin.group_id == group_id,
        Admin.user_id == user_id,
    )
    result = await session.execute(stmt)
    return result.scalar_one_or_none() is not None


async def authorize_group_admin(
    session: AsyncSession, group_id: int, user_id: int, role: str = "admin"
) -> bool:
    stmt = select(Admin).where(
        Admin.group_id == group_id,
        Admin.user_id == user_id,
    )
    result = await session.execute(stmt)
    row = result.scalar_one_or_none()
    if row:
        if row.role != role:
            row.role = role
        return False

    session.add(Admin(group_id=group_id, user_id=user_id, role=role))
    return True


async def deauthorize_group_admin(session: AsyncSession, group_id: int, user_id: int) -> bool:
    stmt = select(Admin).where(
        Admin.group_id == group_id,
        Admin.user_id == user_id,
    )
    result = await session.execute(stmt)
    row = result.scalar_one_or_none()
    if not row:
        return False
    await session.delete(row)
    return True


async def list_group_admins(session: AsyncSession, group_id: int) -> list[Admin]:
    stmt = select(Admin).where(Admin.group_id == group_id).order_by(Admin.id.desc())
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

    sent = await message.answer("无授权,禁止使用")
    _schedule_auto_delete(sent, settings)
    return False


async def ensure_group_admin_permission(
    message: Message,
    session: AsyncSession,
    settings: Settings,
    *,
    allow_super_admin: bool = True,
) -> bool:
    if not message.chat or message.chat.type not in ("group", "supergroup"):
        sent = await message.answer("该命令仅可在群内使用。")
        _schedule_auto_delete(sent, settings)
        return False

    user = message.from_user
    if user and allow_super_admin and is_super_admin_user_id(user.id, settings):
        return True

    if not user:
        sent = await message.answer("无法识别操作者。")
        _schedule_auto_delete(sent, settings)
        return False

    ok = await is_group_admin_authorized(session, message.chat.id, user.id)
    if ok:
        return True

    sent = await message.answer("你没有群管理权限，请联系最高管理员授权。")
    _schedule_auto_delete(sent, settings)
    return False
