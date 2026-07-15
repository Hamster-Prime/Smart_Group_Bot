"""Screening for messages sent by other Telegram bots.

Some spam bots deliver ads through the group's guest/bot posting surface, so
bot senders cannot be ignored outright. Each bot's messages are moderated
until it accumulates the configured number of consecutively clean messages in
a group; it is then whitelisted and never screened again in that group. A
violation before whitelisting resets the clean counter.
"""
from __future__ import annotations

import logging

from sqlalchemy import case, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from bot.db.models import BotScreening

log = logging.getLogger(__name__)


async def is_bot_whitelisted(session: AsyncSession, group_id: int, bot_id: int) -> bool:
    stmt = select(BotScreening.whitelisted).where(
        BotScreening.group_id == group_id,
        BotScreening.bot_id == bot_id,
    )
    with session.no_autoflush:
        result = await session.execute(stmt)
    return bool(result.scalar_one_or_none())


async def record_bot_screening_pass(
    session: AsyncSession, group_id: int, bot_id: int, threshold: int
) -> tuple[int, bool]:
    """记录一条干净的 bot 消息，返回(当前通过数, 是否已加入白名单)。"""
    threshold = max(1, int(threshold))

    async def increment_existing() -> tuple[int, bool] | None:
        next_count = BotScreening.passed_count + 1
        result = await session.execute(
            update(BotScreening)
            .where(
                BotScreening.group_id == group_id,
                BotScreening.bot_id == bot_id,
                BotScreening.whitelisted.is_(False),
            )
            .values(
                passed_count=next_count,
                whitelisted=case(
                    (next_count >= threshold, True),
                    else_=BotScreening.whitelisted,
                ),
            )
            .returning(BotScreening.passed_count, BotScreening.whitelisted)
        )
        row = result.first()
        if row is None:
            return None
        return int(row[0]), bool(row[1])

    incremented = await increment_existing()
    if incremented is not None:
        return incremented

    # No row updated: either the row does not exist yet, or the bot is
    # already whitelisted (a concurrent pass may have just promoted it).
    if await is_bot_whitelisted(session, group_id, bot_id):
        return threshold, True

    should_whitelist = threshold <= 1
    try:
        async with session.begin_nested():
            screening = BotScreening(
                group_id=group_id,
                bot_id=bot_id,
                passed_count=1,
                whitelisted=should_whitelist,
            )
            session.add(screening)
            await session.flush()
        return 1, should_whitelist
    except IntegrityError:
        incremented = await increment_existing()
        if incremented is not None:
            return incremented
        if await is_bot_whitelisted(session, group_id, bot_id):
            return threshold, True
        raise


async def reset_bot_screening(session: AsyncSession, group_id: int, bot_id: int) -> None:
    """违规后清零通过计数；已白名单的 bot 不受影响。"""
    await session.execute(
        update(BotScreening)
        .where(
            BotScreening.group_id == group_id,
            BotScreening.bot_id == bot_id,
            BotScreening.whitelisted.is_(False),
        )
        .values(passed_count=0)
    )


async def remove_bot_whitelist(session: AsyncSession, group_id: int, bot_id: int) -> bool:
    """撤销 bot 白名单并清零计数，返回是否确实撤销了白名单。

    未白名单的 bot 不受影响（不清零其审核进度）。"""
    result = await session.execute(
        update(BotScreening)
        .where(
            BotScreening.group_id == group_id,
            BotScreening.bot_id == bot_id,
            BotScreening.whitelisted.is_(True),
        )
        .values(passed_count=0, whitelisted=False)
    )
    return int(result.rowcount or 0) > 0
