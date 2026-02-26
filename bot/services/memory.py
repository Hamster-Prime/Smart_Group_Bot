from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from pathlib import Path

import litellm

from bot.config import BotConfig
from bot.services.llm import LLMService
from bot.utils.prompts import COMPRESS_SYSTEM

log = logging.getLogger(__name__)

MEMORY_DIR = Path(__file__).resolve().parent.parent.parent / "memory"
SUMMARY_PREFIX = "[历史记忆摘要]"


class MemoryService:
    """Per-group conversation memory with token tracking and compression."""

    def __init__(self, config: BotConfig, llm: LLMService) -> None:
        self.max_context = config.max_context_tokens
        self.max_output = config.max_output_tokens
        self.llm = llm
        # group_id -> list of {"role": ..., "content": ...}
        self._history: dict[int, list[dict[str, str]]] = {}
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

    def add_message(self, group_id: int, role: str, content: str) -> None:
        history = self.get_history(group_id)
        history.append({"role": role, "content": content})
        usage = self._count_tokens(history)
        log.info("[Memory] group=%s +%s, history=%d条, tokens≈%d/%d", group_id, role, len(history), usage, self.max_context)

    def token_usage(self, group_id: int) -> int:
        return self._count_tokens(self.get_history(group_id))

    # ---- Compression ----

    async def maybe_compress(self, group_id: int) -> bool:
        """Compress when token usage reaches threshold."""
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
                log.exception("群 %s 记忆压缩失败", group_id)
        return processed

    @staticmethod
    def _is_summary_only(history: list[dict[str, str]]) -> bool:
        if len(history) != 1:
            return False
        item = history[0]
        return item.get("role") == "system" and item.get("content", "").startswith(SUMMARY_PREFIX)

    @staticmethod
    def _extract_summary(content: str) -> str:
        if content.startswith(SUMMARY_PREFIX):
            return content[len(SUMMARY_PREFIX) :].lstrip("\n").strip()
        return content.strip()

    async def _compress_group(self, group_id: int, force: bool) -> bool:
        async with self._compress_lock:
            history = self.get_history(group_id)
            if not history:
                return False

            usage = self._count_tokens(history)
            if not force and usage < self.max_context:
                return False

            if force and self._is_summary_only(history):
                summary = self._extract_summary(history[0].get("content", ""))
                if summary:
                    self._save_memory(group_id, summary)
                    log.info("群 %s 只有摘要记忆，已落盘快照", group_id)
                    return True
                return False

            log.info("群 %s 开始压缩记忆: context=%d/%d force=%s", group_id, usage, self.max_context, force)

            # Snapshot current history; new messages may arrive during await.
            snapshot = list(history)
            snapshot_len = len(snapshot)
            lines = [f"[{m.get('role', 'user')}] {m.get('content', '')}" for m in snapshot]
            history_text = "\n".join(lines)

            prompt = COMPRESS_SYSTEM.replace("{history}", history_text)
            summary = await self.llm.compress(prompt, "请压缩以上对话历史。")

            if not summary:
                log.warning("群 %s 压缩失败，回退为裁剪旧消息", group_id)
                half = len(history) // 2
                self._history[group_id] = history[half:]
                return True

            self._save_memory(group_id, summary)

            current_history = self.get_history(group_id)
            tail = current_history[snapshot_len:] if len(current_history) > snapshot_len else []
            self._history[group_id] = [
                {"role": "system", "content": f"{SUMMARY_PREFIX}\n{summary}"},
                *tail,
            ]
            log.info("群 %s 记忆压缩完成，保留新增消息=%d", group_id, len(tail))
            return True

    # ---- File persistence ----

    def _group_dir(self, group_id: int) -> Path:
        d = MEMORY_DIR / str(group_id)
        d.mkdir(parents=True, exist_ok=True)
        return d

    def _save_memory(self, group_id: int, summary: str) -> None:
        ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        path = self._group_dir(group_id) / f"{ts}.md"
        path.write_text(summary, encoding="utf-8")
        log.info("Memory saved: %s", path)

    def load_all(self) -> None:
        """On startup, load latest memory file for each group."""
        if not MEMORY_DIR.exists():
            MEMORY_DIR.mkdir(parents=True, exist_ok=True)
            return

        for group_dir in MEMORY_DIR.iterdir():
            if not group_dir.is_dir():
                continue
            try:
                group_id = int(group_dir.name)
            except ValueError:
                continue

            files = sorted(group_dir.glob("*.md"))
            if not files:
                continue

            latest = files[-1]
            content = latest.read_text(encoding="utf-8").strip()
            if content:
                self._history[group_id] = [
                    {"role": "system", "content": f"{SUMMARY_PREFIX}\n{content}"},
                ]
                log.info("Loaded memory for group %s from %s", group_id, latest.name)