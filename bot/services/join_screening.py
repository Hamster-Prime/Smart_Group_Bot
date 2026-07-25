"""Ban registry + member name/bio screening helpers.

Ban semantics (single-group deployment):
- /ban adds the user to the ban registry and bans them in the group.
- Banned users are enforced on join and on every message (outer middleware).
- Unbanning via /unban lifts the ban AND records a permanent screening
  exemption: that user's name/bio are no longer screened, though their
  messages still go through normal moderation.
- Re-banning revokes the exemption so a future unban re-creates it cleanly.

Profile screening runs in two places:
- on join (membership handler);
- on message, when the user's profile signature changed or was never checked
  (see ``bot.middlewares.profile_screen``).

Profile-screening violations create a ban only in the current group. They do
not write to the cross-group ``GlobalBan`` registry.
"""
from __future__ import annotations

import hashlib
import logging

from sqlalchemy import delete, func, select, update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from bot.db.models import GlobalBan, JoinScreeningExemption, ModerationRule, UserProfileScreen
from bot.services.ban_audit import record_ban_event

log = logging.getLogger(__name__)


async def is_globally_banned(session: AsyncSession, user_id: int) -> bool:
    with session.no_autoflush:
        row = await session.get(GlobalBan, user_id)
    return row is not None


async def get_global_ban(session: AsyncSession, user_id: int) -> GlobalBan | None:
    with session.no_autoflush:
        return await session.get(GlobalBan, user_id)


async def add_global_ban(
    session: AsyncSession,
    user_id: int,
    *,
    reason: str = "",
    source: str = "manual",
    created_by: int = 0,
) -> bool:
    """Returns True if a new ban row was created, False if it already existed."""
    # Session factory runs with autoflush=False; flush so prior pending
    # changes in this transaction are ordered before the statements below.
    await session.flush()

    # A re-ban always revokes the permanent profile-screening exemption.
    await session.execute(
        delete(JoinScreeningExemption).where(JoinScreeningExemption.user_id == user_id)
    )

    dialect = getattr(getattr(session, "bind", None), "dialect", None)
    dialect_name = getattr(dialect, "name", "")
    if dialect_name == "sqlite":
        stmt = sqlite_insert(GlobalBan).values(
            user_id=user_id,
            reason=reason,
            source=source,
            created_by=created_by,
        )
        stmt = stmt.on_conflict_do_nothing(index_elements=[GlobalBan.user_id])
        result = await session.execute(stmt)
        created = result.rowcount == 1
        if not created:
            values: dict[str, str] = {}
            if reason:
                values["reason"] = reason
            if source:
                values["source"] = source
            if values:
                await session.execute(
                    update(GlobalBan)
                    .where(GlobalBan.user_id == user_id)
                    .values(**values)
                )
        await record_ban_event(
            session,
            group_id=0,
            target_user_id=user_id,
            action="ban",
            source=source,
            outcome="policy_added" if created else "policy_updated",
            reason=reason,
            actor_user_id=created_by,
            details={"registry_created": bool(created)},
        )
        return created

    # Other dialects still avoid get-then-add. The savepoint keeps the outer
    # transaction usable when a concurrent insert wins the primary-key race.
    try:
        async with session.begin_nested():
            session.add(
                GlobalBan(
                    user_id=user_id,
                    reason=reason,
                    source=source,
                    created_by=created_by,
                )
            )
            await session.flush()
        await record_ban_event(
            session,
            group_id=0,
            target_user_id=user_id,
            action="ban",
            source=source,
            outcome="policy_added",
            reason=reason,
            actor_user_id=created_by,
            details={"registry_created": True},
        )
        return True
    except IntegrityError:
        values = {}
        if reason:
            values["reason"] = reason
        if source:
            values["source"] = source
        if values:
            await session.execute(
                update(GlobalBan).where(GlobalBan.user_id == user_id).values(**values)
            )
        await record_ban_event(
            session,
            group_id=0,
            target_user_id=user_id,
            action="ban",
            source=source,
            outcome="policy_updated",
            reason=reason,
            actor_user_id=created_by,
            details={"registry_created": False},
        )
        return False


async def remove_global_ban(
    session: AsyncSession,
    user_id: int,
    *,
    operator_id: int = 0,
) -> bool:
    """Removes the ban and records a screening exemption.

    Returns True if a ban row existed. The exemption is recorded either way so
    an operator can pre-clear a user who was banned directly by Telegram admins.
    """
    await session.flush()
    removed = False
    row = await session.get(GlobalBan, user_id)
    if row is not None:
        await session.delete(row)
        removed = True

    exemption = await session.get(JoinScreeningExemption, user_id)
    if exemption is None:
        session.add(JoinScreeningExemption(user_id=user_id, created_by=operator_id))
    await record_ban_event(
        session,
        group_id=0,
        target_user_id=user_id,
        action="unban",
        source="manual",
        outcome="policy_removed" if removed else "noop",
        reason="管理员解除全局封禁",
        actor_user_id=operator_id,
        details={"registry_removed": bool(removed)},
    )
    return removed


async def is_join_screening_exempt(session: AsyncSession, user_id: int) -> bool:
    with session.no_autoflush:
        row = await session.get(JoinScreeningExemption, user_id)
    return row is not None


async def list_global_bans(
    session: AsyncSession,
    *,
    limit: int = 50,
    offset: int = 0,
) -> list[GlobalBan]:
    stmt = (
        select(GlobalBan)
        .order_by(GlobalBan.created_at.desc())
        .offset(max(0, int(offset)))
        .limit(max(1, int(limit)))
    )
    with session.no_autoflush:
        result = await session.execute(stmt)
    return list(result.scalars().all())


def build_join_profile_text(*, full_name: str, username: str, bio: str) -> str:
    """Assemble the text that gets screened for a member's profile."""
    parts: list[str] = []
    name = (full_name or "").strip()
    if name:
        parts.append(f"昵称: {name}")
    uname = (username or "").strip().lstrip("@")
    if uname:
        parts.append(f"用户名: @{uname}")
    bio_text = (bio or "").strip()
    if bio_text:
        parts.append(f"简介: {bio_text}")
    return "\n".join(parts)


def profile_signature(*, full_name: str, username: str, bio: str = "") -> str:
    """Stable hash of the visible profile, for change detection on messages."""
    raw = "\x00".join(
        (
            (full_name or "").strip(),
            (username or "").strip().lstrip("@"),
            (bio or "").strip(),
        )
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


async def moderation_rules_fingerprint(session: AsyncSession, group_id: int) -> str:
    """Fingerprint of the group's enabled moderation rules.

    Profile-screen results are cached against this, so adding/changing rules
    invalidates every cached "profile passed" verdict and forces re-screening.
    """
    stmt = (
        select(
            ModerationRule.id,
            ModerationRule.rule_type,
            ModerationRule.pattern,
            ModerationRule.action,
        )
        .where(
            ModerationRule.group_id == group_id,
            ModerationRule.enabled == True,  # noqa: E712
        )
        .order_by(ModerationRule.id)
    )
    with session.no_autoflush:
        rows = (await session.execute(stmt)).all()
    raw = "\x1e".join("\x1f".join(str(v) for v in row) for row in rows)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def profile_screen_signature(
    *,
    full_name: str,
    username: str,
    rules_fingerprint: str,
    bio: str = "",
) -> str:
    """Signature stored in UserProfileScreen: profile fields + rules version."""
    raw = "\x00".join(
        (
            (full_name or "").strip(),
            (username or "").strip().lstrip("@"),
            (bio or "").strip(),
            f"rules:{rules_fingerprint or ''}",
        )
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


async def get_profile_screen_hash(
    session: AsyncSession,
    group_id: int,
    user_id: int,
) -> str:
    with session.no_autoflush:
        row = await session.get(UserProfileScreen, (group_id, user_id))
    return str(row.profile_hash or "") if row else ""


async def mark_profile_screened(
    session: AsyncSession,
    group_id: int,
    user_id: int,
    *,
    profile_hash: str,
) -> None:
    # Concurrent handlers can race this write for a never-screened user
    # (handle_as_tasks runs handlers in parallel and the screening LLM call
    # keeps the window open for seconds). A plain get-then-add would blow up
    # the whole transaction at commit time with a duplicate-PK IntegrityError,
    # so use a native upsert when the dialect supports it.
    dialect = getattr(getattr(session, "bind", None), "dialect", None)
    dialect_name = getattr(dialect, "name", "")
    if dialect_name == "sqlite":
        stmt = sqlite_insert(UserProfileScreen).values(
            group_id=group_id,
            user_id=user_id,
            profile_hash=profile_hash,
        )
        stmt = stmt.on_conflict_do_update(
            index_elements=[UserProfileScreen.group_id, UserProfileScreen.user_id],
            set_={"profile_hash": profile_hash, "checked_at": func.now()},
        )
        await session.execute(stmt)
        return

    row = await session.get(UserProfileScreen, (group_id, user_id))
    if row is None:
        try:
            async with session.begin_nested():
                session.add(
                    UserProfileScreen(
                        group_id=group_id,
                        user_id=user_id,
                        profile_hash=profile_hash,
                    )
                )
                await session.flush()
        except IntegrityError:
            row = await session.get(UserProfileScreen, (group_id, user_id))
            if row is not None:
                row.profile_hash = profile_hash
    else:
        row.profile_hash = profile_hash


async def screen_member_profile_verbose(
    session: AsyncSession,
    moderation: object,
    *,
    group_id: int,
    user_id: int,
    profile_text: str,
) -> tuple[bool, str, bool]:
    """Screen a member's profile against the group's moderation rules.

    Returns (violated, reason, conclusive). conclusive=False means the
    verdict fell back to "not violated" because the moderation model output
    could not be parsed — callers must not cache that as a passed screening.
    Users unbanned via /unban are permanently exempt from profile screening
    (their messages still get moderated).
    """
    text = (profile_text or "").strip()
    if not text:
        return False, "", True

    if await is_join_screening_exempt(session, user_id):
        log.info("profile screening skipped | reason=unban_exempt user=%s group=%s", user_id, group_id)
        return False, "", True

    payload = f"[成员资料审核]\n{text}"
    evaluate = getattr(moderation, "evaluate", None)
    is_high_confidence = getattr(moderation, "is_high_confidence", None)
    if evaluate is not None and is_high_confidence is not None:
        verdict = await evaluate(session, group_id, payload)
        # The confidence challenge is a message-only workflow. Preserve the
        # old fail-open profile behavior for ambiguous matches so a nickname
        # or bio cannot trigger a permanent ban without high confidence.
        if verdict.violated and not is_high_confidence(verdict):
            log.info(
                "profile screening ignored low-confidence match | user=%s group=%s confidence=%.2f",
                user_id,
                group_id,
                verdict.confidence,
            )
            return False, "", True
        return bool(verdict.violated), str(verdict.reason or ""), bool(verdict.conclusive)

    check_verbose = getattr(moderation, "check_rules_verbose", None)
    if check_verbose is not None:
        violated, reason, _rule, conclusive = await check_verbose(session, group_id, payload)
        return bool(violated), str(reason or ""), bool(conclusive)
    violated, reason, _rule = await moderation.check_rules(session, group_id, payload)
    return bool(violated), str(reason or ""), True


async def screen_member_profile(
    session: AsyncSession,
    moderation: object,
    *,
    group_id: int,
    user_id: int,
    profile_text: str,
) -> tuple[bool, str]:
    violated, reason, _conclusive = await screen_member_profile_verbose(
        session,
        moderation,
        group_id=group_id,
        user_id=user_id,
        profile_text=profile_text,
    )
    return violated, reason


# Backward-compatible alias used by the join handler.
screen_new_member = screen_member_profile
