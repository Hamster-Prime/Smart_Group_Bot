"""In-process recent-message tracking for pre-mute residue cleanup.

``banChatMember(revoke_messages=True)`` only revokes the removed account's
view of prior history; other supergroup members keep seeing everything it
posted. A join-verification mute likewise only stops future messages: the
window between the join update and ``restrictChatMember`` landing lets a fast
sender leave spam that nothing retracts afterwards.

This module records the Telegram message ids each member recently sent per
group so verification and ban enforcement can delete that residue
retroactively. The buffer is deliberately in-process and bounded: it cleans a
short race window rather than serving as a durable archive, and Telegram only
lets bots delete messages younger than 48 hours anyway.
"""
from __future__ import annotations

import asyncio
import logging
from collections import deque
from datetime import datetime, timedelta

from bot.utils.timezone import now_shanghai_naive

log = logging.getLogger(__name__)

_MAX_TRACKED_MESSAGES_PER_MEMBER = 32
_MAX_TRACKED_MEMBERS = 4096
# Bot API forbids deleting messages older than this.
_MESSAGE_RETENTION = timedelta(hours=48)
# Bot API deleteMessages accepts at most 100 ids per call.
_DELETE_BATCH_SIZE = 100
_MAX_JOIN_MARKERS = 8192
# Residue sweeps only target members whose observed join is this fresh.
# Queued join security plus profile screening finish well within the bound;
# an established member's history is never bulk-deleted by a later ban.
_JOIN_RESIDUE_WINDOW = timedelta(hours=1)
# Message updates can be processed slightly before the join update that
# logically precedes them; reach a little behind the marker.
_JOIN_MARKER_SKEW_GRACE = timedelta(seconds=30)
_MAX_TRACKED_REMOVALS = 8192
# How long a removal stays armed while waiting for Telegram to deliver the
# matching ``left_chat_member`` ("X was removed") service message update.
_REMOVAL_SERVICE_WINDOW = timedelta(minutes=10)

_RECENT_MEMBER_MESSAGES: dict[tuple[int, int], deque[tuple[int, datetime]]] = {}
# When each member's latest observed join started; sweeps triggered by the
# join flow must never reach back into a previous membership's history.
_MEMBER_JOIN_MARKERS: dict[tuple[int, int], datetime] = {}
# Members we just removed for a verification/screening/raid reason. The
# resulting ``left_chat_member`` service message reprints the unverified
# display name and is created only after the ban, so it cannot be pre-recorded
# like other residue; the gate deletes it when its update arrives.
_MEMBER_REMOVALS: dict[tuple[int, int], datetime] = {}


def _prune_tracked_members(now: datetime) -> None:
    fresh_floor = now - _MESSAGE_RETENTION
    expired = [
        key
        for key, bucket in _RECENT_MEMBER_MESSAGES.items()
        if not bucket or bucket[-1][1] < fresh_floor
    ]
    for key in expired:
        _RECENT_MEMBER_MESSAGES.pop(key, None)
    while len(_RECENT_MEMBER_MESSAGES) >= _MAX_TRACKED_MEMBERS:
        _RECENT_MEMBER_MESSAGES.pop(next(iter(_RECENT_MEMBER_MESSAGES)), None)


def mark_member_join(
    group_id: int,
    user_id: int,
    *,
    at: datetime | None = None,
) -> None:
    """Anchor the residue-sweep window at this member's current join.

    Durable-inbox replays re-deliver the same join update, so an existing
    marker is kept: the earliest receipt bounds the membership. A rejoin gets
    a fresh marker because the leave handler clears the old one.
    """
    key = (int(group_id), int(user_id))
    if key in _MEMBER_JOIN_MARKERS:
        return
    if len(_MEMBER_JOIN_MARKERS) >= _MAX_JOIN_MARKERS:
        current = at or now_shanghai_naive()
        stale_floor = current - _MESSAGE_RETENTION
        for stale_key in [
            candidate
            for candidate, marked_at in _MEMBER_JOIN_MARKERS.items()
            if marked_at < stale_floor
        ]:
            _MEMBER_JOIN_MARKERS.pop(stale_key, None)
        while len(_MEMBER_JOIN_MARKERS) >= _MAX_JOIN_MARKERS:
            _MEMBER_JOIN_MARKERS.pop(next(iter(_MEMBER_JOIN_MARKERS)), None)
    _MEMBER_JOIN_MARKERS[key] = at or now_shanghai_naive()


def clear_member_join_marker(group_id: int, user_id: int) -> None:
    _MEMBER_JOIN_MARKERS.pop((int(group_id), int(user_id)), None)


def mark_member_removed(
    group_id: int,
    user_id: int,
    *,
    at: datetime | None = None,
) -> None:
    """Arm deletion of the pending ``left_chat_member`` service message.

    A verification/screening/raid removal makes Telegram post a "X was
    removed" service message that reprints the unverified display name. That
    message is created after the ban and cannot be pre-recorded, so callers
    mark the removal here and the gate deletes the announcement when its
    update arrives. Bounded and short-lived — it only bridges the gap until
    that one update is seen.
    """
    key = (int(group_id), int(user_id))
    current = at or now_shanghai_naive()
    if len(_MEMBER_REMOVALS) >= _MAX_TRACKED_REMOVALS:
        stale_floor = current - _REMOVAL_SERVICE_WINDOW
        for stale_key in [
            candidate
            for candidate, marked_at in _MEMBER_REMOVALS.items()
            if marked_at < stale_floor
        ]:
            _MEMBER_REMOVALS.pop(stale_key, None)
        while len(_MEMBER_REMOVALS) >= _MAX_TRACKED_REMOVALS:
            _MEMBER_REMOVALS.pop(next(iter(_MEMBER_REMOVALS)), None)
    _MEMBER_REMOVALS[key] = current


def consume_member_removal(
    group_id: int,
    user_id: int,
    *,
    now: datetime | None = None,
) -> bool:
    """Whether this member was just removed by us, clearing the one-shot mark.

    The mark is popped on every call: one removal yields exactly one service
    message, and dropping an expired mark keeps the tracker bounded.
    """
    key = (int(group_id), int(user_id))
    marked_at = _MEMBER_REMOVALS.pop(key, None)
    if marked_at is None:
        return False
    current = now or now_shanghai_naive()
    return marked_at >= current - _REMOVAL_SERVICE_WINDOW


def member_join_marker(group_id: int, user_id: int) -> datetime | None:
    return _MEMBER_JOIN_MARKERS.get((int(group_id), int(user_id)))


def record_group_message(
    group_id: int,
    user_id: int,
    message_id: int,
    *,
    at: datetime | None = None,
) -> None:
    """Remember one member-authored group message for later sweeps."""
    message_id = int(message_id or 0)
    if message_id <= 0:
        return
    key = (int(group_id), int(user_id))
    seen_at = at or now_shanghai_naive()
    bucket = _RECENT_MEMBER_MESSAGES.get(key)
    if bucket is None:
        if len(_RECENT_MEMBER_MESSAGES) >= _MAX_TRACKED_MEMBERS:
            _prune_tracked_members(seen_at)
        bucket = deque(maxlen=_MAX_TRACKED_MESSAGES_PER_MEMBER)
        _RECENT_MEMBER_MESSAGES[key] = bucket
    if any(existing == message_id for existing, _ in bucket):
        return
    bucket.append((message_id, seen_at))


def drain_recent_member_messages(
    group_id: int,
    user_id: int,
    *,
    since: datetime | None = None,
    now: datetime | None = None,
) -> list[int]:
    """Remove and return the member's deletable message ids.

    ``since`` bounds the sweep window: entries recorded earlier stay tracked
    (a rejoining member's pre-leave history must survive a join-time sweep),
    while entries past the 48-hour Bot API deletion limit are dropped
    entirely.
    """
    key = (int(group_id), int(user_id))
    bucket = _RECENT_MEMBER_MESSAGES.get(key)
    if not bucket:
        return []
    current = now or now_shanghai_naive()
    fresh_floor = current - _MESSAGE_RETENTION
    drained: list[int] = []
    kept: list[tuple[int, datetime]] = []
    for message_id, seen_at in bucket:
        if seen_at < fresh_floor:
            continue
        if since is not None and seen_at < since:
            kept.append((message_id, seen_at))
            continue
        drained.append(message_id)
    if kept:
        _RECENT_MEMBER_MESSAGES[key] = deque(
            kept, maxlen=_MAX_TRACKED_MESSAGES_PER_MEMBER
        )
    else:
        _RECENT_MEMBER_MESSAGES.pop(key, None)
    return drained


async def delete_recent_member_messages(
    bot: object,
    group_id: int,
    user_id: int,
    *,
    since: datetime | None = None,
) -> int:
    """Best-effort deletion of the member's recorded messages.

    Bulk ``deleteMessages`` skips ids it cannot find, so replays and
    already-deleted messages are harmless. Fake bots without the bulk method
    fall back to per-message deletion.
    """
    message_ids = drain_recent_member_messages(group_id, user_id, since=since)
    if not message_ids:
        return 0
    deleted = 0
    bulk_delete = getattr(bot, "delete_messages", None)
    if callable(bulk_delete):
        for start in range(0, len(message_ids), _DELETE_BATCH_SIZE):
            chunk = message_ids[start : start + _DELETE_BATCH_SIZE]
            try:
                async with asyncio.timeout(10.0):
                    result = await bulk_delete(
                        chat_id=int(group_id),
                        message_ids=chunk,
                    )
                if result is not False:
                    deleted += len(chunk)
            except Exception:
                log.debug(
                    "recent message bulk delete failed | group=%s user=%s count=%s",
                    group_id,
                    user_id,
                    len(chunk),
                    exc_info=True,
                )
        return deleted
    for message_id in message_ids:
        try:
            async with asyncio.timeout(5.0):
                result = await bot.delete_message(int(group_id), int(message_id))
            if result is not False:
                deleted += 1
        except Exception:
            log.debug(
                "recent message delete failed | group=%s user=%s message=%s",
                group_id,
                user_id,
                message_id,
                exc_info=True,
            )
    return deleted


async def delete_messages_since_join(
    bot: object,
    group_id: int,
    user_id: int,
    *,
    marker: datetime | None = None,
) -> int:
    """Delete residue posted since the member's recently observed join.

    Only sweeps when a fresh join marker exists: without one (restart, or the
    join happened long ago) there is no verified pre-mute race window, and
    deleting an established member's tracked history on a much later ban
    would be wrong — Telegram's ``revoke_messages`` already covers the
    banned account's own view.

    Terminal enforcement (timeout kick, screening/admin ban) removes the
    member, and the resulting leave update clears the live join marker
    concurrently. Those callers must snapshot ``member_join_marker`` before
    the Telegram removal and pass it as ``marker`` so the post-removal sweep
    still sees the membership window it is cleaning.
    """
    anchor = marker if marker is not None else member_join_marker(group_id, user_id)
    if anchor is None or anchor < now_shanghai_naive() - _JOIN_RESIDUE_WINDOW:
        return 0
    return await delete_recent_member_messages(
        bot,
        group_id,
        user_id,
        since=anchor - _JOIN_MARKER_SKEW_GRACE,
    )


async def retract_removed_member_residue(
    bot: object,
    group_id: int,
    user_id: int,
    *,
    marker: datetime | None = None,
) -> int:
    """Clean up both service-message traces of a terminal member removal.

    Terminal enforcement (verification timeout kick, moderation/screening/raid
    ban) leaves two name-exposing artifacts that ``revoke_messages`` does not
    remove for other members: the "X joined" announcement plus any raced-in
    residue (deleted now via the join marker snapshot), and the "X was
    removed" service message Telegram posts as a result of this ban (deleted
    by the gate when its update arrives — armed here). ``marker`` must be the
    pre-removal ``member_join_marker`` snapshot; the removing ban emits a leave
    update whose handler clears the live marker concurrently.

    Arming the removal-notice deletion is gated on a fresh join, exactly like
    the residue sweep: only a member who joined within the residue window is
    an unverified-name exposure. An established member banned later (a stale
    moderation challenge, ``/ban``) keeps their removal notice so admins retain
    that audit trail.
    """
    anchor = marker if marker is not None else member_join_marker(group_id, user_id)
    if anchor is not None and anchor >= now_shanghai_naive() - _JOIN_RESIDUE_WINDOW:
        mark_member_removed(group_id, user_id)
    return await delete_messages_since_join(
        bot,
        group_id,
        user_id,
        marker=marker,
    )


def clear_recent_member_messages() -> None:
    """Reset all tracked state (tests and shutdown)."""
    _RECENT_MEMBER_MESSAGES.clear()
    _MEMBER_JOIN_MARKERS.clear()
    _MEMBER_REMOVALS.clear()
