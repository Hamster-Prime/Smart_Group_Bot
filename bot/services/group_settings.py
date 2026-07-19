from __future__ import annotations

from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from bot.db.models import Group


async def acquire_group_settings_write_intent(
    session: AsyncSession,
    group_id: int,
) -> None:
    """Serialize a following ``Group.settings`` read/modify/write transaction.

    The settings document is stored in one JSON column.  Taking the write
    transaction before reading prevents a later whole-document ORM UPDATE from
    overwriting an intervening mutation made by another handler.
    """

    await session.execute(
        update(Group)
        .where(Group.id == int(group_id))
        .values(title=Group.title)
        .execution_options(synchronize_session=False)
    )
