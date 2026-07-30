"""Lightweight, per-reply progress notices for long-running group replies.

The tracker deliberately has no persistence or task-run semantics.  It keeps
one in-memory timeline for the lifetime of a single reply, reveals it only
after a short delay, and can hand that same Telegram message to the final
text reply.
Progress delivery is best-effort: it must never make the actual bot reply
fail.
"""
from __future__ import annotations

import asyncio
import html
import logging
from collections import OrderedDict
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass, replace
from typing import Literal
from urllib.parse import urlparse

from aiogram.exceptions import TelegramBadRequest
from aiogram.types import Message

from bot.services.message_templates import card_field, render_expandable_blockquote
from bot.utils.telegram import ReplyMessageOverlay, schedule_message_auto_delete_durable

log = logging.getLogger(__name__)

ProgressState = Literal["running", "completed", "failed"]


@dataclass(frozen=True, slots=True)
class ProgressReference:
    """A source that was actually read or used during this reply."""

    title: str
    url: str
    # Group-visible references are origin-only by default because secrets can
    # also live in URL paths.  A small set of trusted documentation skills may
    # opt in to displaying their public canonical path.
    trusted_path: bool = False


@dataclass(frozen=True, slots=True)
class ProgressUpdate:
    """One user-readable milestone in the current reply.

    Reusing ``key`` updates the existing line without changing its position in
    the timeline.  Callers should report meaningful milestones, not token- or
    item-level heartbeats.
    """

    key: str
    state: ProgressState
    text: str
    references: tuple[ProgressReference, ...] = ()


ProgressCallback = Callable[[ProgressUpdate], Awaitable[None]]

_STATUS_TEXT_LIMIT = 96
_REFERENCE_TITLE_LIMIT = 96
_MAX_VISIBLE_REFERENCES = 6
_TELEGRAM_IO_TIMEOUT_SECONDS = 2.0
_TELEGRAM_RENDER_BUDGET = 3900
_STATUS_RENDER_BUDGET = 2800
_REFERENCE_URL_LIMIT = 512
_HANDOFF_RENDER_BUDGET = 1200
_PROGRESS_TITLE = "消息回复"


def _compact_text(value: object, *, limit: int) -> str:
    """Make untrusted labels safe for a single Telegram progress line."""

    text = " ".join(str(value or "").split())
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 3)].rstrip() + "..."


def _completed_text(value: str) -> str:
    """Turn the small set of standard running labels into completed labels."""

    text = _compact_text(value, limit=_STATUS_TEXT_LIMIT)
    if text.startswith("正在"):
        return "已" + text[2:]
    if text.startswith("开始"):
        return "已" + text[2:]
    return text


def _safe_reference_url(value: object, *, trusted_path: bool = False) -> str:
    raw = str(value or "").strip()
    if (
        not raw
        or len(raw) > _REFERENCE_URL_LIMIT
        or any(ord(char) <= 32 or ord(char) == 127 for char in raw)
    ):
        return ""
    try:
        parsed = urlparse(raw)
        _ = parsed.port
    except (TypeError, ValueError):
        return ""
    if (
        parsed.scheme.lower() not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
    ):
        return ""
    if trusted_path:
        return parsed._replace(params="", query="", fragment="").geturl()
    return parsed._replace(path="", params="", query="", fragment="").geturl()


def _utf16_units(value: str) -> int:
    """Return Telegram's text-length unit count."""

    return len(value.encode("utf-16-le")) // 2


def _reply_target_is_definitely_missing(exc: Exception) -> bool:
    if not isinstance(exc, TelegramBadRequest):
        return False
    detail = str(exc).lower()
    return any(
        marker in detail
        for marker in (
            "reply message not found",
            "message to reply not found",
            "message to be replied not found",
            "replied message not found",
        )
    )


class ReplyProgressTracker:
    """Best-effort progress UI scoped to one group-chat reply."""

    def __init__(
        self,
        message: Message,
        enabled: bool,
        *,
        reveal_after: float = 1.5,
        edit_interval: float = 1.0,
        auto_delete_seconds: int = 0,
        disable_link_preview: bool = True,
    ) -> None:
        self._message = message
        self._enabled = bool(enabled)
        self._reveal_after = max(0.0, float(reveal_after))
        self._edit_interval = max(0.0, float(edit_interval))
        # Preserve the existing negative delete-button sentinel as well as
        # positive timer values; zero alone means no cleanup policy.
        self._auto_delete_seconds = int(auto_delete_seconds or 0)
        self._disable_link_preview = bool(disable_link_preview)

        self._updates: OrderedDict[str, ProgressUpdate] = OrderedDict()
        self._references: OrderedDict[str, ProgressReference] = OrderedDict()
        self._sent: Message | None = None
        self._sent_as_reply = False
        self._started = False
        self._terminal: Literal["completed", "failed"] | None = None
        self._closed = False
        self._cleanup_scheduled = False
        self._send_attempted = False
        self._started_at = 0.0
        self._last_edit_at = 0.0

        self._lock = asyncio.Lock()
        self._reveal_task: asyncio.Task[None] | None = None
        self._flush_task: asyncio.Task[None] | None = None
        self._report_tail: asyncio.Task[None] | None = None

    @property
    def visible(self) -> bool:
        """Whether the delayed progress message was successfully delivered."""

        return self._sent is not None

    async def start(self, text: str = "正在理解问题") -> None:
        """Start the delayed notice without blocking for the reveal delay."""

        if not self._enabled:
            return
        async with self._lock:
            if self._closed or self._terminal is not None:
                return
            self._ensure_started_locked()
            if "understanding" not in self._updates:
                self._updates["understanding"] = ProgressUpdate(
                    key="understanding",
                    state="running",
                    text=_compact_text(text, limit=_STATUS_TEXT_LIMIT) or "正在理解问题",
                )
            if self._reveal_after <= 0 and self._sent is None:
                await self._send_locked()

    async def report(self, update: ProgressUpdate) -> None:
        """Queue one milestone without putting Telegram I/O on the tool path."""

        if not self._enabled:
            return
        if not isinstance(update, ProgressUpdate):
            log.debug("reply progress ignored invalid update: %r", update)
            return
        state = str(update.state or "").strip().lower()
        key = str(update.key or "").strip()
        text = _compact_text(update.text, limit=_STATUS_TEXT_LIMIT)
        if state not in {"running", "completed", "failed"} or not key or not text:
            log.debug("reply progress ignored incomplete update: %r", update)
            return

        normalized = ProgressUpdate(
            key=key,
            state=state,  # type: ignore[arg-type]
            text=text,
            references=tuple(update.references or ()),
        )
        previous = self._report_tail
        task = asyncio.get_running_loop().create_task(
            self._apply_report(normalized, previous),
            name="reply-progress-report",
        )
        task.add_done_callback(self._consume_task_result)
        self._report_tail = task

    async def _apply_report(
        self,
        normalized: ProgressUpdate,
        previous: asyncio.Task[None] | None,
    ) -> None:
        if previous is not None:
            try:
                await previous
            except (asyncio.CancelledError, Exception):
                pass
        async with self._lock:
            if self._closed or self._terminal is not None:
                return
            self._ensure_started_locked()
            state = normalized.state
            key = normalized.key
            if state == "running":
                self._complete_other_running_locked(key)
            self._updates[key] = normalized
            self._remember_references_locked(normalized.references)

            if self._sent is not None:
                await self._request_edit_locked()
            elif self._reveal_is_due_locked():
                self._schedule_reveal_locked(0.0)

    async def composing(self, text: str = "正在整理回答") -> None:
        """Move the notice to the standard answer-composition milestone."""

        await self.report(
            ProgressUpdate(key="composing", state="running", text=text)
        )

    async def finish(self, text: str = "已整理并发送回答") -> bool:
        """Finalize a visible notice; a fast reply remains completely silent."""

        return await self._finish_terminal("completed", text)

    async def fail(self, text: str = "处理失败") -> bool:
        """Mark a visible notice failed, using the sole warning glyph in it."""

        return await self._finish_terminal("failed", text)

    async def handoff(
        self,
        text: str = "已整理并发送回答",
    ) -> ReplyMessageOverlay | None:
        """Freeze a visible status message for adoption by the final reply."""

        if not self._enabled:
            return None
        report_tail = self._report_tail
        current = asyncio.current_task()
        if report_tail is not None and report_tail is not current:
            try:
                await report_tail
            except asyncio.CancelledError:
                if current is not None and current.cancelling():
                    raise
            except Exception:
                pass

        async with self._lock:
            if self._closed or self._terminal is not None:
                return None
            self._terminal = "completed"
            if self._reveal_task is not None:
                self._reveal_task.cancel()
                self._reveal_task = None
            if self._flush_task is not None:
                self._flush_task.cancel()
                self._flush_task = None

            for key, item in tuple(self._updates.items()):
                if item.state == "running":
                    self._updates[key] = replace(
                        item,
                        state="completed",
                        text=_completed_text(item.text),
                    )
            final_key = "composing" if "composing" in self._updates else "finished"
            self._updates[final_key] = ProgressUpdate(
                key=final_key,
                state="completed",
                text=(
                    _compact_text(text, limit=_STATUS_TEXT_LIMIT)
                    or "已整理并发送回答"
                ),
            )
            if self._sent is None:
                return None
            return ReplyMessageOverlay(
                message=self._sent,
                status_html=self._render(max_units=_HANDOFF_RENDER_BUDGET),
                reply_to_message_id=(
                    int(getattr(self._message, "message_id", 0) or 0) or None
                    if self._sent_as_reply
                    else None
                ),
                sent_as_reply=self._sent_as_reply,
            )

    async def dismiss(self) -> None:
        """Remove a temporary notice when this turn produces no final reply."""

        current = asyncio.current_task()
        async with self._lock:
            if self._closed:
                return
            self._closed = True
            sent = self._sent
            self._sent = None
            tasks = [self._reveal_task, self._flush_task, self._report_tail]
            self._reveal_task = None
            self._flush_task = None
            self._report_tail = None
        pending = list(
            dict.fromkeys(
                task for task in tasks if task is not None and task is not current
            )
        )
        await self._cancel_tasks(pending)
        if sent is None:
            return
        try:
            async with asyncio.timeout(_TELEGRAM_IO_TIMEOUT_SECONDS):
                await sent.delete()
        except asyncio.CancelledError:
            raise
        except Exception:
            log.debug("reply progress dismissal failed", exc_info=True)
            # Never leave a stale "currently processing" card behind.  If
            # deletion failed, make the known message terminal before handing
            # it to the normal durable cleanup path.
            try:
                async with asyncio.timeout(_TELEGRAM_IO_TIMEOUT_SECONDS):
                    await sent.edit_text(
                        (
                            f"<b>{_PROGRESS_TITLE} · 未完成</b>\n\n"
                            f"<blockquote>{card_field('当前', '⚠️ 处理已结束')}</blockquote>"
                        ),
                        parse_mode="HTML",
                        disable_web_page_preview=self._disable_link_preview,
                    )
            except asyncio.CancelledError:
                raise
            except Exception:
                log.debug(
                    "reply progress dismissal terminal edit failed",
                    exc_info=True,
                )
            await self._schedule_cleanup(sent)

    async def close(self) -> None:
        """Cancel pending delayed work so a notice can never appear late."""

        current = asyncio.current_task()
        async with self._lock:
            if self._closed:
                return
            self._closed = True
            tasks = [self._reveal_task, self._flush_task, self._report_tail]
            self._reveal_task = None
            self._flush_task = None
            self._report_tail = None
        pending = list(
            dict.fromkeys(
                task for task in tasks if task is not None and task is not current
            )
        )
        await self._cancel_tasks(pending)

    @staticmethod
    async def _cancel_tasks(tasks: list[asyncio.Task[None]]) -> None:
        for task in tasks:
            task.cancel()
        if not tasks:
            return
        done, still_pending = await asyncio.wait(tasks, timeout=0.5)
        for task in done:
            try:
                task.exception()
            except (asyncio.CancelledError, Exception):
                pass
        for task in still_pending:
            task.add_done_callback(ReplyProgressTracker._consume_task_result)

    @staticmethod
    def _consume_task_result(task: asyncio.Task[None]) -> None:
        try:
            task.exception()
        except (asyncio.CancelledError, Exception):
            pass

    def _ensure_started_locked(self) -> None:
        if self._started:
            return
        self._started = True
        loop = asyncio.get_running_loop()
        self._started_at = loop.time()
        if self._reveal_after > 0:
            self._schedule_reveal_locked(self._reveal_after)

    def _schedule_reveal_locked(self, delay: float) -> None:
        if self._send_attempted:
            return
        if self._reveal_task is not None and not self._reveal_task.done():
            return
        self._reveal_task = asyncio.get_running_loop().create_task(
            self._reveal_later(delay),
            name="reply-progress-reveal",
        )

    def _reveal_is_due_locked(self) -> bool:
        if not self._started:
            return False
        return (
            asyncio.get_running_loop().time() - self._started_at
            >= self._reveal_after
        )

    async def _reveal_later(self, delay: float) -> None:
        try:
            await asyncio.sleep(max(0.0, delay))
            async with self._lock:
                if self._closed or self._terminal is not None or self._sent is not None:
                    return
                await self._send_locked()
        except asyncio.CancelledError:
            return
        except Exception:
            log.debug("delayed reply progress delivery failed", exc_info=True)

    def _complete_other_running_locked(self, active_key: str) -> None:
        for key, item in tuple(self._updates.items()):
            if key == active_key or item.state != "running":
                continue
            self._updates[key] = replace(
                item,
                state="completed",
                text=_completed_text(item.text),
            )

    def _remember_references_locked(
        self,
        references: Iterable[ProgressReference],
    ) -> None:
        for reference in references:
            if not isinstance(reference, ProgressReference):
                continue
            title = _compact_text(reference.title, limit=_REFERENCE_TITLE_LIMIT)
            url = _safe_reference_url(
                reference.url,
                trusted_path=reference.trusted_path,
            )
            if not title:
                continue
            dedupe_key = url or title.casefold()
            self._references[dedupe_key] = ProgressReference(
                title=title,
                url=url,
                trusted_path=reference.trusted_path,
            )

    async def _request_edit_locked(self) -> None:
        now = asyncio.get_running_loop().time()
        remaining = max(0.0, self._edit_interval - (now - self._last_edit_at))
        if self._flush_task is None or self._flush_task.done():
            self._flush_task = asyncio.get_running_loop().create_task(
                self._flush_later(remaining),
                name="reply-progress-edit",
            )

    async def _flush_later(self, delay: float) -> None:
        try:
            await asyncio.sleep(max(0.0, delay))
            async with self._lock:
                if self._closed or self._terminal is not None or self._sent is None:
                    return
                await self._edit_locked()
        except asyncio.CancelledError:
            return
        except Exception:
            log.debug("delayed reply progress edit failed", exc_info=True)

    async def _send_locked(self) -> None:
        if (
            self._closed
            or self._terminal is not None
            or self._sent is not None
            or self._send_attempted
        ):
            return
        # A timed-out Telegram send has an unknown outcome.  Never make a
        # second logical attempt on a later progress update, because that can
        # create an untracked duplicate card.
        self._send_attempted = True
        body = self._render()
        options = {
            "parse_mode": "HTML",
            "disable_web_page_preview": self._disable_link_preview,
        }
        sent: Message | None = None
        sent_as_reply = False
        try:
            async with asyncio.timeout(_TELEGRAM_IO_TIMEOUT_SECONDS):
                sent = await self._message.reply(body, **options)
            sent_as_reply = True
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # A timeout/network error is outcome-ambiguous: retrying with
            # answer() could create a second, untracked card.  Fall back only
            # when Telegram definitively rejected the reply target itself.
            if not _reply_target_is_definitely_missing(exc):
                log.debug("reply progress delivery failed", exc_info=True)
                return
            log.debug("reply progress target missing; trying answer", exc_info=True)
            try:
                async with asyncio.timeout(_TELEGRAM_IO_TIMEOUT_SECONDS):
                    sent = await self._message.answer(body, **options)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.debug("reply progress delivery failed", exc_info=True)
                return
        self._sent = sent
        self._sent_as_reply = sent_as_reply
        self._last_edit_at = asyncio.get_running_loop().time()

    async def _edit_locked(self) -> bool:
        if self._sent is None:
            return False
        try:
            async with asyncio.timeout(_TELEGRAM_IO_TIMEOUT_SECONDS):
                await self._sent.edit_text(
                    self._render(),
                    parse_mode="HTML",
                    disable_web_page_preview=self._disable_link_preview,
                )
            self._last_edit_at = asyncio.get_running_loop().time()
            return True
        except asyncio.CancelledError:
            raise
        except Exception:
            log.debug("reply progress edit failed", exc_info=True)
            return False

    async def _finish_terminal(
        self,
        terminal: Literal["completed", "failed"],
        text: str,
    ) -> bool:
        if not self._enabled:
            return True
        report_tail = self._report_tail
        current = asyncio.current_task()
        if report_tail is not None and report_tail is not current:
            try:
                await report_tail
            except asyncio.CancelledError:
                if current is not None and current.cancelling():
                    raise
            except Exception:
                pass
        sent: Message | None = None
        edit_ok = True
        async with self._lock:
            if self._closed or self._terminal is not None:
                return False
            self._terminal = terminal
            if self._reveal_task is not None:
                self._reveal_task.cancel()
                self._reveal_task = None
            if self._flush_task is not None:
                self._flush_task.cancel()
                self._flush_task = None

            final_text = _compact_text(text, limit=_STATUS_TEXT_LIMIT)
            if terminal == "completed":
                for key, item in tuple(self._updates.items()):
                    if item.state == "running":
                        self._updates[key] = replace(
                            item,
                            state="completed",
                            text=_completed_text(item.text),
                        )
                final_key = "composing" if "composing" in self._updates else "finished"
                self._updates[final_key] = ProgressUpdate(
                    key=final_key,
                    state="completed",
                    text=final_text or "已整理并发送回答",
                )
            else:
                active_key = next(
                    (
                        key
                        for key, item in reversed(self._updates.items())
                        if item.state == "running"
                    ),
                    "failed",
                )
                self._updates[active_key] = ProgressUpdate(
                    key=active_key,
                    state="failed",
                    text=final_text or "处理失败",
                )

            # A reply that finishes before the reveal deadline must not create
            # a status message solely to announce its own completion.
            if self._sent is not None:
                edit_ok = await self._edit_locked()
                sent = self._sent

        if sent is not None:
            await self._schedule_cleanup(
                sent,
                delete_now_if_rejected=terminal == "completed",
            )
        return edit_ok

    async def _schedule_cleanup(
        self,
        sent: Message,
        *,
        delete_now_if_rejected: bool = True,
    ) -> bool:
        if self._cleanup_scheduled or self._auto_delete_seconds == 0:
            return True
        self._cleanup_scheduled = True
        try:
            accepted = await schedule_message_auto_delete_durable(
                sent,
                self._auto_delete_seconds,
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            log.debug("reply progress auto-delete scheduling failed", exc_info=True)
            accepted = False
        if accepted:
            return True

        if not delete_now_if_rejected:
            # A failed terminal card may be the only user-visible warning that
            # a side effect has an unknown outcome.  Preserve it when durable
            # cleanup is unavailable; safety beats transient retention here.
            return False

        # The progress card is explicitly transient.  If durable scheduling is
        # unhealthy, deleting it now is safer than leaving a permanent status
        # card.  This is never a reason to resend anything.
        try:
            async with asyncio.timeout(_TELEGRAM_IO_TIMEOUT_SECONDS):
                await sent.delete()
            if self._sent is sent:
                self._sent = None
            return True
        except asyncio.CancelledError:
            raise
        except Exception:
            log.debug("reply progress immediate cleanup fallback failed", exc_info=True)
            return False

    def _render(self, *, max_units: int = _TELEGRAM_RENDER_BUDGET) -> str:
        render_budget = max(256, min(_TELEGRAM_RENDER_BUDGET, int(max_units)))
        parts: list[str] = []
        updates = list(self._updates.values())
        if updates:
            current_index = next(
                (
                    index
                    for index in range(len(updates) - 1, -1, -1)
                    if updates[index].state == "running"
                ),
                len(updates) - 1,
            )
            if self._terminal == "failed":
                current_index = next(
                    (
                        index
                        for index in range(len(updates) - 1, -1, -1)
                        if updates[index].state == "failed"
                    ),
                    current_index,
                )

            current_item = updates[current_index]
            history_lines: list[str] = []
            for index, item in enumerate(updates):
                if index == current_index:
                    continue
                text = html.escape(
                    _compact_text(item.text, limit=_STATUS_TEXT_LIMIT)
                )
                if item.state == "completed":
                    history_lines.append(f"<s>{text}</s>")
                elif item.state == "failed":
                    history_lines.append(f"⚠️ {text}")
                else:
                    history_lines.append(text)

            current_text = html.escape(
                _compact_text(current_item.text, limit=_STATUS_TEXT_LIMIT)
            )
            if current_item.state == "failed":
                current_text = f"⚠️ {current_text}"

            if self._terminal == "completed":
                title_status = "已完成"
            elif self._terminal == "failed":
                title_status = "未完成"
            else:
                title_status = "处理中"

            next_step = ""
            if self._terminal is None:
                next_step = (
                    "发送回答"
                    if current_item.key == "composing"
                    else "整理并发送回答"
                )

            def _status_part(
                shown_history: list[str],
                omitted_updates: int,
            ) -> str:
                status_lines = list(shown_history)
                if omitted_updates:
                    status_lines.insert(
                        min(1, len(status_lines)),
                        f"… 另有 {omitted_updates} 个较早步骤未展开",
                    )
                status_lines.append(card_field("当前", current_text))
                if next_step:
                    status_lines.append(card_field("下一步", next_step))
                return (
                    f"<b>{_PROGRESS_TITLE} · {title_status}</b>\n\n"
                    f"<blockquote>{'\n'.join(status_lines)}</blockquote>"
                )

            shown_lines = list(history_lines)
            omitted_updates = 0
            while True:
                status_part = _status_part(shown_lines, omitted_updates)
                if (
                    _utf16_units(status_part)
                    <= min(_STATUS_RENDER_BUDGET, render_budget)
                    or not shown_lines
                ):
                    parts.append(status_part)
                    break
                # Keep the first milestone and the newest/current milestones;
                # collapse older middle history only when Telegram's hard
                # message limit makes showing every line impossible.
                shown_lines.pop(1 if len(shown_lines) > 2 else 0)
                omitted_updates += 1

        if self._references:
            all_references = list(self._references.values())
            selected: list[ProgressReference] = []

            def _reference_part(
                references: list[ProgressReference],
            ) -> str:
                reference_lines = ["<b>参考资料</b>"]
                for reference in references:
                    label = html.escape(reference.title)
                    if reference.url:
                        url = html.escape(reference.url, quote=True)
                        label = f'<a href="{url}">{label}</a>'
                    reference_lines.append(f"• {label}")
                omitted = len(all_references) - len(references)
                if omitted > 0:
                    reference_lines.append(f"另有 {omitted} 个来源未展开")
                return render_expandable_blockquote(reference_lines)

            for reference in all_references[:_MAX_VISIBLE_REFERENCES]:
                candidate = [*selected, reference]
                details = _reference_part(candidate)
                if _utf16_units("\n\n".join((*parts, details))) > render_budget:
                    break
                selected = candidate
            if selected:
                parts.append(_reference_part(selected))
        return "\n\n".join(parts)


__all__ = [
    "ProgressCallback",
    "ProgressReference",
    "ProgressState",
    "ProgressUpdate",
    "ReplyProgressTracker",
]
