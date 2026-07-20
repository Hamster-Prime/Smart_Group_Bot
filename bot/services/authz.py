from __future__ import annotations

from aiogram.types import Message
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from bot.config import Settings
from bot.db.models import Admin, AuthorizedGroup
from bot.services.update_delivery import mark_privileged_operator


async def _schedule_auto_delete(sent: Message | None, settings: Settings) -> None:
    # Deferred import: bot.utils.telegram imports is_super_admin_user_id from
    # this module, so a top-level import would be circular.
    from bot.utils.telegram import (
        configured_auto_delete_seconds,
        schedule_message_auto_delete_durable,
    )

    await schedule_message_auto_delete_durable(
        sent, configured_auto_delete_seconds(settings, "management")
    )


def is_super_admin_user_id(user_id: int, settings: Settings) -> bool:
    return bool(settings.super_admin_id) and user_id == settings.super_admin_id


async def _end_read_transaction(session: AsyncSession) -> None:
    """Return a read-only connection to the pool before network replies."""
    in_transaction = getattr(session, "in_transaction", None)
    if callable(in_transaction) and in_transaction():
        await session.commit()


async def ensure_super_admin(message: Message, settings: Settings) -> bool:
    user = message.from_user
    if user and is_super_admin_user_id(user.id, settings):
        mark_privileged_operator(int(user.id))
        return True
    sent = await message.answer("仅最高管理员可使用该命令。")
    await _schedule_auto_delete(sent, settings)
    return False


async def is_group_authorized(session: AsyncSession, group_id: int) -> bool:
    row = await session.get(AuthorizedGroup, group_id)
    return row is not None


async def authorize_group(session: AsyncSession, group_id: int, operator_id: int = 0) -> bool:
    row = await session.get(AuthorizedGroup, group_id)
    if row:
        return False
    session.add(AuthorizedGroup(group_id=group_id, authorized_by=operator_id or None))
    # Make the grant visible to subsequent authorization helpers in the same
    # transaction.  Without this flush, ``authorize_group_admin`` can observe
    # no group row when callers intentionally compose both grants atomically.
    await session.flush()
    return True


async def deauthorize_group(session: AsyncSession, group_id: int) -> bool:
    row = await session.get(AuthorizedGroup, group_id)
    # Group-admin grants are subordinate to the group authorization.  Keeping
    # them would silently resurrect old privileges if the group is authorized
    # again later.  Run the cleanup even when the authorization row is already
    # absent, repairing stale grants created by older versions.
    await session.execute(delete(Admin).where(Admin.group_id == group_id))
    if row is not None:
        await session.delete(row)
    return row is not None


async def list_authorized_groups(session: AsyncSession) -> list[AuthorizedGroup]:
    stmt = select(AuthorizedGroup).order_by(AuthorizedGroup.created_at.desc())
    result = await session.execute(stmt)
    return list(result.scalars().all())


async def is_group_admin_authorized(session: AsyncSession, group_id: int, user_id: int) -> bool:
    stmt = (
        select(Admin.id)
        .join(
            AuthorizedGroup,
            AuthorizedGroup.group_id == Admin.group_id,
        )
        .where(
            Admin.group_id == group_id,
            Admin.user_id == user_id,
        )
    )
    result = await session.execute(stmt)
    return result.scalar_one_or_none() is not None


async def authorize_group_admin(
    session: AsyncSession, group_id: int, user_id: int, role: str = "admin"
) -> bool:
    if await session.get(AuthorizedGroup, group_id) is None:
        return False
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
    stmt = (
        select(Admin)
        .join(
            AuthorizedGroup,
            AuthorizedGroup.group_id == Admin.group_id,
        )
        .where(Admin.group_id == group_id)
        .order_by(Admin.id.desc())
    )
    result = await session.execute(stmt)
    return list(result.scalars().all())


async def warm_privileged_operator_cache(
    session_factory: async_sessionmaker[AsyncSession],
) -> int:
    """Preload durable delegated-admin grants into the admission cache."""

    async with session_factory() as session:
        rows = (
            await session.execute(
                select(Admin.group_id, Admin.user_id).join(
                    AuthorizedGroup,
                    AuthorizedGroup.group_id == Admin.group_id,
                )
            )
        ).all()
        await session.commit()
    for group_id, user_id in rows:
        mark_privileged_operator(int(user_id), group_id=int(group_id))
    return len(rows)


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
    await _end_read_transaction(session)
    if ok:
        return True

    sent = await message.answer("无授权,禁止使用")
    await _schedule_auto_delete(sent, settings)
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
        await _schedule_auto_delete(sent, settings)
        return False

    user = message.from_user
    if user and allow_super_admin and is_super_admin_user_id(user.id, settings):
        mark_privileged_operator(int(user.id))
        return True

    if not user:
        sent = await message.answer("无法识别操作者。")
        await _schedule_auto_delete(sent, settings)
        return False

    ok = await is_group_admin_authorized(session, message.chat.id, user.id)
    await _end_read_transaction(session)
    if ok:
        mark_privileged_operator(int(user.id), group_id=int(message.chat.id))
        return True

    sent = await message.answer("你没有群管理权限，请联系最高管理员授权。")
    await _schedule_auto_delete(sent, settings)
    return False
