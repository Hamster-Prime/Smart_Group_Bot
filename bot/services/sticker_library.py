from __future__ import annotations

import json
import random
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any

from aiogram.types import Message
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from bot.db.models import StickerLibraryRecord
from bot.utils.security import clean_text

_ROOT = Path(__file__).resolve().parent.parent.parent
_LEGACY_STICKER_DIR = _ROOT / "memory" / "stickers"
_INVALID_VISION_MARKERS = {"NO_VALID_IMAGE_CONTENT", "NO_VALID_IMAGE"}
_SPACE_RE = re.compile(r"\s+")
_MAX_STICKERS_PER_GROUP = 100


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _normalize(text: str) -> str:
    s = (text or "").lower().strip()
    s = _SPACE_RE.sub("", s)
    return s


def _parse_iso_datetime(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except Exception:
        return None


def _dt_rank(dt: datetime | None) -> float:
    if not dt:
        return 0.0
    try:
        return float(dt.timestamp())
    except Exception:
        return 0.0


@dataclass(slots=True)
class StickerPick:
    file_id: str = ""
    score: int = 0
    source: str = "none"
    description: str = ""


class StickerLibrary:
    """Per-group sticker store backed by database with legacy JSON import."""

    def __init__(self, legacy_dir: Path | None = None) -> None:
        self.legacy_dir = legacy_dir or _LEGACY_STICKER_DIR
        self._checked_groups: set[int] = set()

    def _legacy_path(self, group_id: int) -> Path:
        return self.legacy_dir / f"{group_id}.json"

    async def _ensure_legacy_import(self, session: AsyncSession, group_id: int) -> None:
        if group_id in self._checked_groups:
            return

        exists_stmt = (
            select(StickerLibraryRecord.id)
            .where(StickerLibraryRecord.group_id == group_id)
            .limit(1)
        )
        exists = (await session.execute(exists_stmt)).scalar_one_or_none()
        if exists is not None:
            await self._trim_group_records(session, group_id)
            self._checked_groups.add(group_id)
            return

        path = self._legacy_path(group_id)
        if not path.exists():
            self._checked_groups.add(group_id)
            return

        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            self._checked_groups.add(group_id)
            return
        if not isinstance(data, dict):
            self._checked_groups.add(group_id)
            return

        rows = data.get("stickers")
        if not isinstance(rows, list) or not rows:
            self._checked_groups.add(group_id)
            return

        imported_file_ids: set[str] = set()
        for item in rows:
            if not isinstance(item, dict):
                continue
            file_id = str(item.get("file_id", "")).strip()
            if not file_id or file_id in imported_file_ids:
                continue
            imported_file_ids.add(file_id)

            aliases_raw = item.get("aliases", [])
            if isinstance(aliases_raw, list):
                aliases = [str(x).strip() for x in aliases_raw if str(x).strip()]
            else:
                aliases = []

            session.add(
                StickerLibraryRecord(
                    group_id=group_id,
                    file_id=file_id,
                    emoji=str(item.get("emoji", "")).strip(),
                    set_name=str(item.get("set_name", "")).strip(),
                    description=clean_text(str(item.get("description", "")), max_len=240),
                    aliases=aliases,
                    seen_count=max(0, int(item.get("seen_count", 0) or 0)),
                    sent_count=max(0, int(item.get("sent_count", 0) or 0)),
                    source=str(item.get("source", "legacy_json")).strip() or "legacy_json",
                    created_at=_parse_iso_datetime(item.get("created_at")),
                    last_seen_at=_parse_iso_datetime(item.get("last_seen_at")),
                    last_sent_at=_parse_iso_datetime(item.get("last_sent_at")),
                )
            )

        await session.flush()
        await self._trim_group_records(session, group_id)
        self._checked_groups.add(group_id)

    @staticmethod
    def _is_valid_vision_description(text: str) -> bool:
        normalized = (text or "").strip()
        if not normalized:
            return False
        return all(marker not in normalized for marker in _INVALID_VISION_MARKERS)

    @staticmethod
    def _build_auto_description(emoji: str, set_name: str, vision_description: str) -> str:
        vd = clean_text(vision_description, max_len=120)
        if StickerLibrary._is_valid_vision_description(vd):
            return vd
        if emoji and set_name:
            return f"贴纸 {emoji}（来自 {set_name}）"
        if emoji:
            return f"贴纸 {emoji}"
        if set_name:
            return f"来自 {set_name} 的贴纸"
        return "群聊贴纸"

    @staticmethod
    def _record_text(record: StickerLibraryRecord) -> str:
        aliases = " ".join(str(x) for x in (record.aliases or []) if x)
        parts = [
            str(record.description or ""),
            str(record.emoji or ""),
            str(record.set_name or ""),
            aliases,
        ]
        return clean_text(" ".join(parts), max_len=800)

    @staticmethod
    def _score(query: str, target: str) -> int:
        q = _normalize(query)
        t = _normalize(target)
        if not q or not t:
            return 0
        ratio = int(SequenceMatcher(None, q, t).ratio() * 100)
        bonus = 20 if q in t else 0
        overlap = len(set(q) & set(t))
        return ratio + bonus + overlap

    @staticmethod
    def _row_to_dict(row: StickerLibraryRecord) -> dict[str, Any]:
        return {
            "file_id": row.file_id,
            "emoji": row.emoji,
            "set_name": row.set_name,
            "description": row.description,
            "aliases": list(row.aliases or []),
            "seen_count": row.seen_count,
            "sent_count": row.sent_count,
            "source": row.source,
        }

    @staticmethod
    async def _group_row_count(session: AsyncSession, group_id: int) -> int:
        stmt = select(func.count(StickerLibraryRecord.id)).where(
            StickerLibraryRecord.group_id == group_id
        )
        return int((await session.execute(stmt)).scalar_one() or 0)

    async def total_sent_count(self, session: AsyncSession, group_id: int) -> int:
        await self._ensure_legacy_import(session, group_id)
        stmt = select(func.coalesce(func.sum(StickerLibraryRecord.sent_count), 0)).where(
            StickerLibraryRecord.group_id == group_id
        )
        total = (await session.execute(stmt)).scalar_one_or_none()
        return max(0, int(total or 0))

    async def _trim_group_records(self, session: AsyncSession, group_id: int) -> None:
        stmt = (
            select(StickerLibraryRecord)
            .where(StickerLibraryRecord.group_id == group_id)
            .order_by(
                StickerLibraryRecord.last_seen_at.desc(),
                StickerLibraryRecord.last_sent_at.desc(),
                StickerLibraryRecord.sent_count.desc(),
                StickerLibraryRecord.seen_count.desc(),
                StickerLibraryRecord.id.desc(),
            )
        )
        rows = list((await session.execute(stmt)).scalars().all())
        if len(rows) <= _MAX_STICKERS_PER_GROUP:
            return
        for stale in rows[_MAX_STICKERS_PER_GROUP:]:
            await session.delete(stale)
        await session.flush()

    async def learn_from_message(
        self,
        session: AsyncSession,
        group_id: int,
        message: Message,
        *,
        vision_description: str = "",
    ) -> dict[str, Any] | None:
        await self._ensure_legacy_import(session, group_id)

        sticker = getattr(message, "sticker", None)
        if not sticker:
            return None
        file_id = (getattr(sticker, "file_id", None) or "").strip()
        if not file_id:
            return None

        emoji = (getattr(sticker, "emoji", None) or "").strip()
        set_name = (getattr(sticker, "set_name", None) or "").strip()
        description = self._build_auto_description(emoji, set_name, vision_description)
        now = _now_utc()

        stmt = select(StickerLibraryRecord).where(
            StickerLibraryRecord.group_id == group_id,
            StickerLibraryRecord.file_id == file_id,
        )
        row = (await session.execute(stmt)).scalar_one_or_none()
        if row is None:
            row_count = await self._group_row_count(session, group_id)
            if row_count >= _MAX_STICKERS_PER_GROUP:
                await self._trim_group_records(session, group_id)
                return None
            row = StickerLibraryRecord(
                group_id=group_id,
                file_id=file_id,
                emoji=emoji,
                set_name=set_name,
                description=description,
                aliases=[],
                seen_count=1,
                sent_count=0,
                source="group_message",
                created_at=now,
                last_seen_at=now,
            )
            session.add(row)
            await session.flush()
            await self._trim_group_records(session, group_id)
            return self._row_to_dict(row)

        row.emoji = emoji or row.emoji
        row.set_name = set_name or row.set_name
        row.last_seen_at = now
        row.seen_count = max(0, int(row.seen_count or 0)) + 1
        old_desc = (row.description or "").strip()
        if description and description != old_desc:
            aliases = list(row.aliases or [])
            if old_desc and old_desc not in aliases:
                aliases.append(old_desc)
            row.aliases = aliases
            row.description = description

        await session.flush()
        return self._row_to_dict(row)

    async def mark_sent(self, session: AsyncSession, group_id: int, file_id: str) -> None:
        await self._ensure_legacy_import(session, group_id)

        fid = (file_id or "").strip()
        if not fid:
            return
        now = _now_utc()

        stmt = select(StickerLibraryRecord).where(
            StickerLibraryRecord.group_id == group_id,
            StickerLibraryRecord.file_id == fid,
        )
        row = (await session.execute(stmt)).scalar_one_or_none()
        if row is None:
            row_count = await self._group_row_count(session, group_id)
            if row_count >= _MAX_STICKERS_PER_GROUP:
                await self._trim_group_records(session, group_id)
                return
            row = StickerLibraryRecord(
                group_id=group_id,
                file_id=fid,
                emoji="",
                set_name="",
                description="默认贴纸",
                aliases=[],
                seen_count=0,
                sent_count=1,
                source="skill_send",
                created_at=now,
                last_seen_at=now,
                last_sent_at=now,
            )
            session.add(row)
            await session.flush()
            await self._trim_group_records(session, group_id)
            return
        else:
            row.sent_count = max(0, int(row.sent_count or 0)) + 1
            row.last_sent_at = now
        await session.flush()

    async def pick_sticker(
        self,
        session: AsyncSession,
        group_id: int,
        *,
        query: str = "",
        fallback_pool: list[str] | None = None,
    ) -> StickerPick:
        await self._ensure_legacy_import(session, group_id)

        stmt = select(StickerLibraryRecord).where(StickerLibraryRecord.group_id == group_id)
        rows = list((await session.execute(stmt)).scalars().all())
        fallback = [x.strip() for x in (fallback_pool or []) if x and x.strip()]

        if rows:
            q = clean_text(query, max_len=200)
            if q:
                best_score = -1
                best: StickerLibraryRecord | None = None
                for row in rows:
                    score = self._score(q, self._record_text(row))
                    if score > best_score:
                        best_score = score
                        best = row
                if best and best_score >= 25:
                    return StickerPick(
                        file_id=best.file_id,
                        score=best_score,
                        source="library_match",
                        description=best.description or "",
                    )

            ranked = sorted(
                rows,
                key=lambda x: (
                    int(x.sent_count or 0),
                    int(x.seen_count or 0),
                    _dt_rank(x.last_seen_at),
                ),
                reverse=True,
            )
            if ranked:
                top = ranked[: min(8, len(ranked))]
                chosen = random.choice(top)
                return StickerPick(
                    file_id=chosen.file_id,
                    score=0,
                    source="library_recent",
                    description=chosen.description or "",
                )

        if fallback:
            return StickerPick(file_id=random.choice(fallback), score=0, source="fallback_pool", description="")
        return StickerPick()

    async def list_candidates(
        self,
        session: AsyncSession,
        group_id: int,
        *,
        limit: int = 20,
        fallback_pool: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        await self._ensure_legacy_import(session, group_id)

        stmt = (
            select(StickerLibraryRecord)
            .where(StickerLibraryRecord.group_id == group_id)
            .order_by(
                StickerLibraryRecord.last_seen_at.desc(),
                StickerLibraryRecord.last_sent_at.desc(),
                StickerLibraryRecord.sent_count.desc(),
                StickerLibraryRecord.seen_count.desc(),
                StickerLibraryRecord.id.desc(),
            )
        )
        rows = list((await session.execute(stmt)).scalars().all())

        normalized_limit = max(1, min(int(limit or 20), 50))
        out: list[dict[str, Any]] = []
        used: set[str] = set()
        for row in rows:
            fid = (row.file_id or "").strip()
            if not fid or fid in used:
                continue
            used.add(fid)
            out.append(
                {
                    "file_id": fid,
                    "emoji": (row.emoji or "").strip(),
                    "set_name": (row.set_name or "").strip(),
                    "description": clean_text(str(row.description or ""), max_len=180),
                    "seen_count": int(row.seen_count or 0),
                    "sent_count": int(row.sent_count or 0),
                    "source": (row.source or "").strip() or "group_message",
                }
            )
            if len(out) >= normalized_limit:
                break

        for fid in [x.strip() for x in (fallback_pool or []) if x and x.strip()]:
            if fid in used:
                continue
            used.add(fid)
            out.append(
                {
                    "file_id": fid,
                    "emoji": "",
                    "set_name": "",
                    "description": "默认贴纸",
                    "seen_count": 0,
                    "sent_count": 0,
                    "source": "fallback_pool",
                }
            )
            if len(out) >= normalized_limit:
                break

        return out


sticker_library = StickerLibrary()
