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


async def _send_access_notice(
    message: Message,
    settings: Settings,
    *,
    title: str,
    action: str,
) -> None:
    """Send an action-first access notice without creating an import cycle."""
    # message_templates imports bot.utils.telegram, which imports this module.
    # Keeping this import inside the send path preserves that dependency order.
    from bot.services.message_templates import render_action_notice

    sent = await message.answer(
        render_action_notice(title, action=action),
        parse_mode="HTML",
    )
    await _schedule_auto_delete(sent, settings)


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
    await _send_access_notice(
        message,
        settings,
        title="权限不足",
        action="仅最高管理员可使用该命令。",
    )
    return False


async def is_group_authorized(session: AsyncSession, group_id: int) -> bool:
    row = await session.get(AuthorizedGroup, group_id)
    return row is not None and bool(row.bot_present)


async def set_group_bot_present(
    session: AsyncSession,
    group_id: int,
    *,
    present: bool,
) -> bool:
    """Update Telegram reachability without deleting authorization intent.

    Returns ``True`` only when an existing authorized row changed.  A bot being
    added to an unknown group must never implicitly authorize that group.
    """

    row = await session.get(AuthorizedGroup, int(group_id))
    if row is None or bool(row.bot_present) == bool(present):
        return False
    row.bot_present = bool(present)
    await session.flush()
    return True


async def authorize_group(session: AsyncSession, group_id: int, operator_id: int = 0) -> bool:
    row = await session.get(AuthorizedGroup, group_id)
    if row:
        if not bool(row.bot_present):
            # Explicit authorization is also the manual recovery path when a
            # rejoin my_chat_member update was missed.
            row.bot_present = True
            if operator_id:
                row.authorized_by = int(operator_id)
            await session.flush()
            return True
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


async def list_authorized_groups(
    session: AsyncSession,
    *,
    include_inactive: bool = False,
) -> list[AuthorizedGroup]:
    stmt = select(AuthorizedGroup)
    if not include_inactive:
        stmt = stmt.where(AuthorizedGroup.bot_present.is_(True))
    stmt = stmt.order_by(AuthorizedGroup.created_at.desc())
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
            AuthorizedGroup.bot_present.is_(True),
        )
    )
    result = await session.execute(stmt)
    return result.scalar_one_or_none() is not None


async def authorize_group_admin(
    session: AsyncSession, group_id: int, user_id: int, role: str = "admin"
) -> bool:
    authorized = await session.get(AuthorizedGroup, group_id)
    if authorized is None or not bool(authorized.bot_present):
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
        .where(AuthorizedGroup.bot_present.is_(True))
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
                ).where(AuthorizedGroup.bot_present.is_(True))
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

    await _send_access_notice(
        message,
        settings,
        title="当前群未授权",
        action="请联系最高管理员完成群组授权。",
    )
    return False


async def ensure_group_admin_permission(
    message: Message,
    session: AsyncSession,
    settings: Settings,
    *,
    allow_super_admin: bool = True,
) -> bool:
    if not message.chat or message.chat.type not in ("group", "supergroup"):
        await _send_access_notice(
            message,
            settings,
            title="无法执行",
            action="该命令仅可在群内使用。",
        )
        return False

    user = message.from_user
    if user and allow_super_admin and is_super_admin_user_id(user.id, settings):
        mark_privileged_operator(int(user.id))
        return True

    if not user:
        await _send_access_notice(
            message,
            settings,
            title="无法执行",
            action="无法识别操作者。",
        )
        return False

    ok = await is_group_admin_authorized(session, message.chat.id, user.id)
    await _end_read_transaction(session)
    if ok:
        mark_privileged_operator(int(user.id), group_id=int(message.chat.id))
        return True

    await _send_access_notice(
        message,
        settings,
        title="权限不足",
        action="你没有群管理权限，请联系最高管理员授权。",
    )
    return False
