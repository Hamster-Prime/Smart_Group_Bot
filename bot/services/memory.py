from __future__ import annotations

import asyncio
import json
import logging
import re
from datetime import datetime, timezone
from pathlib import Path

import litellm

from bot.config import BotConfig
from bot.services.llm import LLMService
from bot.utils.prompts import COMPRESS_SYSTEM

log = logging.getLogger(__name__)

MEMORY_DIR = Path(__file__).resolve().parent.parent.parent / "memory"
HISTORY_FILE_NAME = "_history.jsonl"
SUMMARY_PREFIX = "[history-summary]"
LEGACY_SUMMARY_PREFIXES = (
    SUMMARY_PREFIX,
    "[历史记忆摘要]",
    "[鍘嗗彶璁板繂鎽樿]",
)


class MemoryService:
    """Per-group conversation memory with full-history persistence."""

    def __init__(self, config: BotConfig, llm: LLMService) -> None:
        self.max_context = config.max_context_tokens
        self.max_output = config.max_output_tokens
        self.llm = llm
        # group_id -> list of {"role": ..., "content": ...}
        self._history: dict[int, list[dict[str, str]]] = {}
        # group_id -> latest long-term summary text
        self._summary: dict[int, str] = {}
        # group_id -> message count at last successful compression
        self._last_compressed_len: dict[int, int] = {}
        self._compress_min_new_messages = 20
        self._llm_max_history_items = 2000
        self._llm_reserve_tokens = max(1024, self.max_output // 2)
        self._compress_lock = asyncio.Lock()

    # ---- Token counting ----

    def _count_tokens(self, messages: list[dict[str, str]]) -> int:
        try:
            return litellm.token_counter(
                model=self.llm.main.model,
                messages=messages,
            )
        except Exception:
            # Fallback: rough estimate based on characters
            return sum(len(m.get("content", "")) for m in messages)

    # ---- History access ----

    def get_history(self, group_id: int) -> list[dict[str, str]]:
        return self._history.setdefault(group_id, [])

    def _select_recent_messages(
        self,
        history: list[dict[str, str]],
        *,
        budget_tokens: int,
        max_items: int,
    ) -> list[dict[str, str]]:
        if not history:
            return []

        # Fast approximation to avoid counting tokens for each candidate message.
        budget_chars = max(2048, budget_tokens * 4)
        selected: list[dict[str, str]] = []
        used_chars = 0
        for msg in reversed(history):
            content = msg.get("content", "")
            item_chars = max(24, len(content))
            if selected and used_chars + item_chars > budget_chars:
                break
            selected.append({"role": msg.get("role", "user"), "content": content})
            used_chars += item_chars
            if len(selected) >= max_items:
                break
        selected.reverse()
        return selected

    def get_history_for_llm(
        self,
        group_id: int,
        *,
        reserve_tokens: int | None = None,
        max_items: int | None = None,
    ) -> list[dict[str, str]]:
        history = self.get_history(group_id)
        summary = self._summary.get(group_id, "").strip()
        if not history and not summary:
            return []

        reserve = self._llm_reserve_tokens if reserve_tokens is None else max(0, reserve_tokens)
        item_limit = self._llm_max_history_items if max_items is None else max(1, max_items)
        budget_tokens = max(1024, self.max_context - self.max_output - reserve)
        selected = self._select_recent_messages(
            history,
            budget_tokens=budget_tokens,
            max_items=item_limit,
        )
        if not summary:
            return selected

        # Prepend latest summary so long-term facts survive even when recent window shifts.
        summary_msg = {"role": "system", "content": f"{SUMMARY_PREFIX}\n{summary}"}
        if selected:
            first = selected[0]
            if first.get("role") == "system" and self._extract_summary(first.get("content", "")) == summary:
                return selected
        return [summary_msg, *selected]

    def add_message(self, group_id: int, role: str, content: str) -> None:
        history = self.get_history(group_id)
        history.append({"role": role, "content": content})
        self._append_history_event(group_id, role, content)
        usage = self._count_tokens(history)
        log.info(
            "[Memory] group=%s +%s, history=%d, tokens~%d/%d",
            group_id,
            role,
            len(history),
            usage,
            self.max_context,
        )

    def token_usage(self, group_id: int) -> int:
        return self._count_tokens(self.get_history(group_id))

    # ---- Compression ----

    async def maybe_compress(self, group_id: int) -> bool:
        """Create snapshot summary when token usage reaches threshold."""
        return await self._compress_group(group_id, force=False)

    async def compress_all(self, force: bool = True) -> int:
        """Compress/persist all in-memory groups."""
        group_ids = list(self._history.keys())
        if not group_ids:
            return 0

        processed = 0
        for group_id in group_ids:
            try:
                if await self._compress_group(group_id, force=force):
                    processed += 1
            except Exception:
                log.exception("Memory compression failed for group=%s", group_id)
        return processed

    @staticmethod
    def _is_summary_only(history: list[dict[str, str]]) -> bool:
        if len(history) != 1:
            return False
        item = history[0]
        return item.get("role") == "system" and any(
            item.get("content", "").startswith(prefix) for prefix in LEGACY_SUMMARY_PREFIXES
        )

    @staticmethod
    def _extract_summary(content: str) -> str:
        for prefix in LEGACY_SUMMARY_PREFIXES:
            if content.startswith(prefix):
                return content[len(prefix) :].lstrip("\n").strip()
        return content.strip()

    async def _compress_group(self, group_id: int, force: bool) -> bool:
        async with self._compress_lock:
            history = self.get_history(group_id)
            if not history:
                if force:
                    summary = self._summary.get(group_id, "").strip()
                    if summary:
                        self._save_memory(group_id, summary)
                        return True
                return False

            usage = self._count_tokens(history)
            if not force and usage < self.max_context:
                return False
            if not force:
                delta = len(history) - self._last_compressed_len.get(group_id, 0)
                if delta < self._compress_min_new_messages:
                    return False

            log.info(
                "Memory compress start: group=%s context=%d/%d force=%s",
                group_id,
                usage,
                self.max_context,
                force,
            )

            # Build compression input from summary + recent full history window.
            snapshot = self.get_history_for_llm(group_id, reserve_tokens=self.max_output, max_items=500)
            lines = [f"[{m.get('role', 'user')}] {m.get('content', '')}" for m in snapshot]
            history_text = "\n".join(lines)

            prompt = COMPRESS_SYSTEM.replace("{history}", history_text)
            summary = (await self.llm.compress(prompt, "请压缩以上对话历史。")).strip()
            if not summary:
                log.warning("Memory compress failed: group=%s", group_id)
                self._last_compressed_len[group_id] = len(history)
                return False

            self._save_memory(group_id, summary)
            self._summary[group_id] = summary
            self._last_compressed_len[group_id] = len(history)
            log.info("Memory compress saved snapshot: group=%s", group_id)
            return True

    # ---- File persistence ----

    def _group_dir(self, group_id: int) -> Path:
        d = MEMORY_DIR / str(group_id)
        d.mkdir(parents=True, exist_ok=True)
        return d

    def _history_path(self, group_id: int) -> Path:
        return self._group_dir(group_id) / HISTORY_FILE_NAME

    def _append_history_event(self, group_id: int, role: str, content: str) -> None:
        event = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "role": role,
            "content": content,
        }
        path = self._history_path(group_id)
        try:
            with path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(event, ensure_ascii=False) + "\n")
        except Exception:
            log.exception("Failed to append history event: group=%s", group_id)

    def _save_memory(self, group_id: int, summary: str) -> None:
        ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        path = self._group_dir(group_id) / f"{ts}.md"
        path.write_text(summary, encoding="utf-8")
        log.info("Memory saved: %s", path)

    @staticmethod
    def _parse_group_id_from_filename(path: Path) -> int | None:
        """Parse legacy memory filename like '<group_id>_*.md'."""
        m = re.match(r"^(-?\d+)", path.stem)
        if not m:
            return None
        try:
            return int(m.group(1))
        except ValueError:
            return None

    def _load_history_events(self, group_id: int) -> list[dict[str, str]]:
        path = self._history_path(group_id)
        if not path.exists():
            return []

        messages: list[dict[str, str]] = []
        with path.open("r", encoding="utf-8") as f:
            for raw in f:
                line = raw.strip()
                if not line:
                    continue
                try:
                    data = json.loads(line)
                except Exception:
                    continue
                role = str(data.get("role", "")).strip().lower()
                if role not in {"system", "user", "assistant"}:
                    continue
                content = str(data.get("content", "")).strip()
                if not content:
                    continue
                messages.append({"role": role, "content": content})
        return messages

    def load_all(self) -> None:
        """On startup, load persisted full history and legacy summary snapshots."""
        if not MEMORY_DIR.exists():
            MEMORY_DIR.mkdir(parents=True, exist_ok=True)
            return

        group_files: dict[int, list[Path]] = {}
        group_ids: set[int] = set()

        # Preferred layout: memory/<group_id>/*.md
        for group_dir in MEMORY_DIR.iterdir():
            if not group_dir.is_dir():
                continue
            try:
                group_id = int(group_dir.name)
            except ValueError:
                continue
            group_ids.add(group_id)
            files = sorted(group_dir.glob("*.md"), key=lambda p: p.name)
            if files:
                group_files.setdefault(group_id, []).extend(files)

        # Backward-compatible layout: memory/<group_id>_*.md
        for path in MEMORY_DIR.glob("*.md"):
            group_id = self._parse_group_id_from_filename(path)
            if group_id is None:
                continue
            group_ids.add(group_id)
            group_files.setdefault(group_id, []).append(path)

        loaded_groups = 0
        loaded_summary_files = 0
        loaded_events = 0
        for group_id in sorted(group_ids):
            history = self._load_history_events(group_id)
            if history:
                self._history[group_id] = history
                self._last_compressed_len[group_id] = len(history)
                loaded_events += len(history)

            files = sorted(group_files.get(group_id, []), key=lambda p: p.name)
            latest_summary = ""
            for f in files:
                try:
                    content = f.read_text(encoding="utf-8").strip()
                except Exception:
                    log.exception("Failed to read memory file: %s", f)
                    continue
                if not content:
                    continue
                latest_summary = self._extract_summary(content)
                loaded_summary_files += 1

            if latest_summary:
                self._summary[group_id] = latest_summary

            if not history and latest_summary:
                # Legacy mode fallback: no event journal yet, keep summary in history.
                self._history[group_id] = [
                    {"role": "system", "content": f"{SUMMARY_PREFIX}\n{latest_summary}"},
                ]
                self._last_compressed_len[group_id] = 1

            if group_id not in self._history and group_id not in self._summary:
                continue
            loaded_groups += 1

        if loaded_groups:
            log.info(
                "Memory bootstrap completed: groups=%d summary_files=%d history_events=%d",
                loaded_groups,
                loaded_summary_files,
                loaded_events,
            )
