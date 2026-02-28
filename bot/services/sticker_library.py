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

from bot.utils.security import clean_text

_ROOT = Path(__file__).resolve().parent.parent.parent
_STICKER_DIR = _ROOT / "memory" / "stickers"
_INVALID_VISION_MARKERS = {"NO_VALID_IMAGE_CONTENT", "NO_VALID_IMAGE"}
_SPACE_RE = re.compile(r"\s+")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _normalize(text: str) -> str:
    s = (text or "").lower().strip()
    s = _SPACE_RE.sub("", s)
    return s


@dataclass(slots=True)
class StickerPick:
    file_id: str = ""
    score: int = 0
    source: str = "none"
    description: str = ""


class StickerLibrary:
    """Per-group sticker metadata store with lightweight semantic matching."""

    def __init__(self, base_dir: Path | None = None) -> None:
        self.base_dir = base_dir or _STICKER_DIR
        self.base_dir.mkdir(parents=True, exist_ok=True)

    def _path(self, group_id: int) -> Path:
        return self.base_dir / f"{group_id}.json"

    def _load(self, group_id: int) -> dict[str, Any]:
        path = self._path(group_id)
        if not path.exists():
            return {"group_id": group_id, "updated_at": "", "stickers": []}
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return {"group_id": group_id, "updated_at": "", "stickers": []}
        if not isinstance(data, dict):
            return {"group_id": group_id, "updated_at": "", "stickers": []}
        stickers = data.get("stickers")
        if not isinstance(stickers, list):
            data["stickers"] = []
        return data

    def _save(self, group_id: int, data: dict[str, Any]) -> None:
        data["group_id"] = group_id
        data["updated_at"] = _now_iso()
        path = self._path(group_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

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
    def _record_text(record: dict[str, Any]) -> str:
        parts = [
            str(record.get("description", "")),
            str(record.get("emoji", "")),
            str(record.get("set_name", "")),
            " ".join(str(x) for x in record.get("aliases", []) if x),
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

    def learn_from_message(
        self,
        group_id: int,
        message: Message,
        *,
        vision_description: str = "",
    ) -> dict[str, Any] | None:
        sticker = getattr(message, "sticker", None)
        if not sticker:
            return None

        file_id = (getattr(sticker, "file_id", None) or "").strip()
        if not file_id:
            return None

        emoji = (getattr(sticker, "emoji", None) or "").strip()
        set_name = (getattr(sticker, "set_name", None) or "").strip()
        description = self._build_auto_description(emoji, set_name, vision_description)
        now = _now_iso()

        data = self._load(group_id)
        stickers = data.setdefault("stickers", [])

        existing: dict[str, Any] | None = None
        for item in stickers:
            if str(item.get("file_id", "")).strip() == file_id:
                existing = item
                break

        if existing is None:
            record = {
                "file_id": file_id,
                "emoji": emoji,
                "set_name": set_name,
                "description": description,
                "aliases": [],
                "seen_count": 1,
                "sent_count": 0,
                "created_at": now,
                "last_seen_at": now,
                "last_sent_at": "",
                "source": "group_message",
            }
            stickers.append(record)
            self._save(group_id, data)
            return record

        existing["emoji"] = emoji or existing.get("emoji", "")
        existing["set_name"] = set_name or existing.get("set_name", "")
        existing["last_seen_at"] = now
        existing["seen_count"] = int(existing.get("seen_count", 0) or 0) + 1
        if description and description != existing.get("description", ""):
            aliases = existing.setdefault("aliases", [])
            old_desc = str(existing.get("description", "")).strip()
            if old_desc and old_desc not in aliases:
                aliases.append(old_desc)
            existing["description"] = description

        self._save(group_id, data)
        return existing

    def mark_sent(self, group_id: int, file_id: str) -> None:
        fid = (file_id or "").strip()
        if not fid:
            return
        data = self._load(group_id)
        stickers = data.get("stickers", [])
        now = _now_iso()
        changed = False
        for item in stickers:
            if str(item.get("file_id", "")).strip() == fid:
                item["sent_count"] = int(item.get("sent_count", 0) or 0) + 1
                item["last_sent_at"] = now
                changed = True
                break
        if changed:
            self._save(group_id, data)

    def pick_sticker(
        self,
        group_id: int,
        *,
        query: str = "",
        fallback_pool: list[str] | None = None,
    ) -> StickerPick:
        data = self._load(group_id)
        stickers = data.get("stickers", [])
        fallback = [x.strip() for x in (fallback_pool or []) if x and x.strip()]

        if stickers:
            q = clean_text(query, max_len=200)
            if q:
                best_score = -1
                best: dict[str, Any] | None = None
                for item in stickers:
                    score = self._score(q, self._record_text(item))
                    if score > best_score:
                        best_score = score
                        best = item
                if best and best_score >= 25:
                    return StickerPick(
                        file_id=str(best.get("file_id", "")).strip(),
                        score=best_score,
                        source="library_match",
                        description=str(best.get("description", "")),
                    )

            # No clear semantic hit: prefer frequently used and recently seen stickers.
            ranked = sorted(
                stickers,
                key=lambda x: (
                    int(x.get("sent_count", 0) or 0),
                    int(x.get("seen_count", 0) or 0),
                    str(x.get("last_seen_at", "")),
                ),
                reverse=True,
            )
            if ranked:
                top = ranked[: min(8, len(ranked))]
                chosen = random.choice(top)
                return StickerPick(
                    file_id=str(chosen.get("file_id", "")).strip(),
                    score=0,
                    source="library_recent",
                    description=str(chosen.get("description", "")),
                )

        if fallback:
            return StickerPick(file_id=random.choice(fallback), score=0, source="fallback_pool", description="")
        return StickerPick()


sticker_library = StickerLibrary()
