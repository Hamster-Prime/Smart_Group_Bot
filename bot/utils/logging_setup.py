from __future__ import annotations

import atexit
import logging
import os
import queue
import sys
import threading
import time
from contextvars import ContextVar, Token
from dataclasses import dataclass
from logging.handlers import QueueHandler, QueueListener, RotatingFileHandler
from pathlib import Path
from typing import Any

from bot.services.resource_health import register_resource_health_provider

_REQ_ID: ContextVar[str] = ContextVar("log_req_id", default="-")
_UPDATE_ID: ContextVar[str] = ContextVar("log_update_id", default="-")
_CHAT_ID: ContextVar[str] = ContextVar("log_chat_id", default="-")
_USER_ID: ContextVar[str] = ContextVar("log_user_id", default="-")
_FLOW_ID: ContextVar[str] = ContextVar("log_flow_id", default="-")

_DEFAULT_FORMAT = (
    "%(asctime)s.%(msecs)03d | %(level_cn)s | %(short_name)s | 流=%(flow_id)s | %(event)s"
)
_DEFAULT_DATEFMT = "%Y-%m-%d %H:%M:%S"
_LEVEL_CN = {
    "DEBUG": "调试",
    "INFO": "信息",
    "WARNING": "警告",
    "ERROR": "错误",
    "CRITICAL": "严重",
}
_COLOR_RESET = "\x1b[0m"
_LEVEL_COLORS = {
    "DEBUG": "\x1b[38;5;245m",
    "INFO": "\x1b[38;5;111m",
    "WARNING": "\x1b[38;5;220m",
    "ERROR": "\x1b[38;5;203m",
    "CRITICAL": "\x1b[38;5;196m",
}
_TAG_COLORS = {
    "【入口】": "\x1b[38;5;117m",
    "【出口】": "\x1b[38;5;117m",
    "【流程】": "\x1b[38;5;81m",
    "【结束】": "\x1b[38;5;50m",
    "【决策】": "\x1b[38;5;214m",
    "【LLM请求】": "\x1b[38;5;141m",
    "【LLM返回】": "\x1b[38;5;77m",
    "【LLM失败】": "\x1b[38;5;203m",
    "【回复】": "\x1b[38;5;151m",
    "【视觉】": "\x1b[38;5;75m",
}

_DEFAULT_QUEUE_SIZE = 8192
_LISTENER_STOP_TIMEOUT_SECONDS = 2.0
_LOGGING_STATE_LOCK = threading.RLock()
_LOG_QUEUE_HANDLER: _DroppingQueueHandler | None = None
_LOG_LISTENER: _BoundedQueueListener | None = None
_LOG_SINK_HANDLERS: tuple[logging.Handler, ...] = ()
_LOG_SATURATED_SINCE: float | None = None
_LOG_REAPER_THREADS: dict[threading.Thread, float] = {}
_ATEXIT_REGISTERED = False


def logging_resource_health_snapshot() -> dict[str, Any]:
    global _LOG_SATURATED_SINCE

    now = time.monotonic()
    with _LOGGING_STATE_LOCK:
        for retired in tuple(_LOG_REAPER_THREADS):
            if not retired.is_alive():
                _LOG_REAPER_THREADS.pop(retired, None)
        handler = _LOG_QUEUE_HANDLER
        listener = _LOG_LISTENER
        log_queue = getattr(listener, "queue", None)
        thread = getattr(listener, "_thread", None)
    queue_size = int(log_queue.qsize()) if log_queue is not None else 0
    queue_capacity = int(getattr(log_queue, "maxsize", 0) or 0)
    dropped = int(getattr(handler, "dropped_records", 0) or 0)
    listener_alive = bool(thread is not None and thread.is_alive())
    queue_ratio = (
        float(queue_size) / float(queue_capacity) if queue_capacity > 0 else 0.0
    )
    saturated = queue_ratio >= 0.90
    with _LOGGING_STATE_LOCK:
        if saturated:
            if _LOG_SATURATED_SINCE is None:
                _LOG_SATURATED_SINCE = now
        else:
            _LOG_SATURATED_SINCE = None
        saturated_since = _LOG_SATURATED_SINCE
        reaper_ages = [
            max(0.0, now - started_at)
            for started_at in _LOG_REAPER_THREADS.values()
        ]
    saturated_age = (
        max(0.0, now - saturated_since) if saturated_since is not None else 0.0
    )
    oldest_reaper_age = max(reaper_ages, default=0.0)
    configured_but_dead = listener is not None and not listener_alive
    fatal = bool(
        configured_but_dead
        or saturated_age >= 60.0
        or oldest_reaper_age >= 120.0
    )
    return {
        "ok": bool(
            queue_ratio < 0.80
            and not configured_but_dead
            and not reaper_ages
        ),
        "fatal": fatal,
        "queue_depth": queue_size,
        "queue_capacity": queue_capacity,
        "queue_ratio": round(queue_ratio, 4),
        "dropped_records": dropped,
        "listener_alive": listener_alive,
        "saturated_seconds": round(saturated_age, 3),
        "retired_listener_reapers": len(reaper_ages),
        "oldest_reaper_seconds": round(oldest_reaper_age, 3),
    }


register_resource_health_provider("logging", logging_resource_health_snapshot)


@dataclass(slots=True)
class _FlushBarrier:
    event: threading.Event


class _DroppingQueueHandler(QueueHandler):
    """Never block the asyncio/event-loop thread on log I/O.

    The queue is deliberately bounded. During sustained sink backpressure,
    dropping diagnostic records is safer than allowing logs to consume all
    process memory or synchronously stall every Telegram update.
    """

    def __init__(self, log_queue: queue.Queue[Any]) -> None:
        super().__init__(log_queue)
        self.dropped_records = 0

    def enqueue(self, record: logging.LogRecord) -> None:
        try:
            self.queue.put_nowait(record)
        except queue.Full:
            self.dropped_records += 1


class _BoundedQueueListener(QueueListener):
    """QueueListener with flush barriers and a bounded, non-hanging stop."""

    def handle(self, record: Any) -> None:
        if isinstance(record, _FlushBarrier):
            try:
                for handler in self.handlers:
                    handler.flush()
            finally:
                record.event.set()
            return
        super().handle(record)

    def stop_bounded(self, timeout: float) -> bool:
        thread = self._thread
        if thread is None:
            return True

        deadline = time.monotonic() + max(0.0, float(timeout))
        while True:
            try:
                self.queue.put_nowait(self._sentinel)
                break
            except queue.Full:
                # Shutdown/reconfiguration must not hang merely because the
                # log queue is saturated. Sacrifice the oldest diagnostic
                # entry to make room for the sentinel.
                try:
                    self.queue.get_nowait()
                except queue.Empty:
                    if time.monotonic() >= deadline:
                        return False

        thread.join(timeout=max(0.0, deadline - time.monotonic()))
        stopped = not thread.is_alive()
        if stopped:
            self._thread = None
        return stopped


class _SecureRotatingFileHandler(RotatingFileHandler):
    """Keep current and post-rollover log files owner-readable only."""

    def _open(self):
        stream = super()._open()
        try:
            os.chmod(self.baseFilename, 0o600)
        except OSError:
            # Logging must remain available on filesystems that do not expose
            # POSIX modes (for example some Windows/container mounts).
            pass
        return stream


class LogContextFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.req_id = _REQ_ID.get()
        record.update_id = _UPDATE_ID.get()
        record.chat_id = _CHAT_ID.get()
        record.user_id = _USER_ID.get()
        record.flow_id = _FLOW_ID.get()
        return True


class ChineseLogFormatter(logging.Formatter):
    def __init__(self, *args: Any, use_color: bool = True, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.use_color = use_color

    @staticmethod
    def _short_name(name: str) -> str:
        parts = (name or "").split(".")
        if len(parts) >= 2:
            return ".".join(parts[-2:])
        return name or "-"

    def _paint(self, text: str, color: str) -> str:
        if not self.use_color:
            return text
        return f"{color}{text}{_COLOR_RESET}"

    def _colorize_event(self, message: str) -> str:
        if not self.use_color:
            return message
        for tag, color in _TAG_COLORS.items():
            if message.startswith(tag):
                return f"{color}{message}{_COLOR_RESET}"
        return message

    def format(self, record: logging.LogRecord) -> str:
        level_cn = _LEVEL_CN.get(record.levelname, record.levelname)
        level_color = _LEVEL_COLORS.get(record.levelname, "")
        record.level_cn = self._paint(level_cn, level_color)
        record.short_name = self._short_name(record.name)
        record.flow_id = getattr(record, "flow_id", "-")
        record.event = self._colorize_event(record.getMessage())
        record.message = record.event
        if self.usesTime():
            record.asctime = self.formatTime(record, self.datefmt)

        s = self.formatMessage(record)
        if record.exc_info:
            if not record.exc_text:
                record.exc_text = self.formatException(record.exc_info)
        if record.exc_text:
            if s and s[-1:] != "\n":
                s += "\n"
            s += record.exc_text
        if record.stack_info:
            if s and s[-1:] != "\n":
                s += "\n"
            s += self.formatStack(record.stack_info)
        return s


def _parse_level(raw: str | int | None, *, default: int) -> int:
    if isinstance(raw, int):
        return raw
    if isinstance(raw, str):
        name = raw.strip().upper()
        mapping = logging.getLevelNamesMapping()
        if name in mapping:
            return int(mapping[name])
    return default


def _parse_bool(raw: str | None, *, default: bool) -> bool:
    if raw is None:
        return default
    v = raw.strip().lower()
    if v in {"1", "true", "yes", "on"}:
        return True
    if v in {"0", "false", "no", "off"}:
        return False
    return default


def _parse_int(raw: str | None, *, default: int, min_value: int = 1) -> int:
    if raw is None:
        return default
    try:
        val = int(raw.strip())
    except Exception:
        return default
    return max(min_value, val)


def flush_logging(*, timeout: float = 1.0) -> bool:
    """Flush queued records without blocking indefinitely.

    This is intended for tests, controlled reconfiguration and shutdown. The
    normal application path only enqueues records and never waits for sinks.
    """

    with _LOGGING_STATE_LOCK:
        listener = _LOG_LISTENER
        log_queue = getattr(listener, "queue", None)
    if listener is None or log_queue is None or listener._thread is None:
        return True

    barrier = _FlushBarrier(threading.Event())
    try:
        log_queue.put(barrier, timeout=max(0.0, float(timeout)))
    except queue.Full:
        return False
    return barrier.event.wait(timeout=max(0.0, float(timeout)))


def shutdown_logging(*, timeout: float = _LISTENER_STOP_TIMEOUT_SECONDS) -> bool:
    """Stop the asynchronous logging pipeline and close owned sinks."""

    global _LOG_QUEUE_HANDLER, _LOG_LISTENER, _LOG_SINK_HANDLERS
    global _LOG_SATURATED_SINCE

    with _LOGGING_STATE_LOCK:
        queue_handler = _LOG_QUEUE_HANDLER
        listener = _LOG_LISTENER
        sinks = _LOG_SINK_HANDLERS
        _LOG_QUEUE_HANDLER = None
        _LOG_LISTENER = None
        _LOG_SINK_HANDLERS = ()
        _LOG_SATURATED_SINCE = None

        root = logging.getLogger()
        if queue_handler is not None and queue_handler in root.handlers:
            root.removeHandler(queue_handler)

    if listener is None:
        return True

    deadline = time.monotonic() + max(0.0, float(timeout))
    flush_budget = max(0.0, min(deadline - time.monotonic(), 1.0))
    if flush_budget:
        barrier = _FlushBarrier(threading.Event())
        try:
            listener.queue.put(barrier, timeout=flush_budget)
            barrier.event.wait(timeout=flush_budget)
        except queue.Full:
            pass

    stopped = listener.stop_bounded(
        timeout=max(0.0, deadline - time.monotonic())
    )
    if stopped:
        for handler in sinks:
            try:
                handler.flush()
            finally:
                handler.close()
    else:
        # ``stop_bounded`` has already queued the sentinel.  A pathological
        # sink may still be blocked in emit/flush, so let a daemon reaper own
        # the old listener and close its handlers once the sink recovers.  Do
        # not lose those references during a forced runtime reconfiguration.
        def reap_old_listener() -> None:
            thread = listener._thread
            try:
                if thread is not None:
                    thread.join()
                for handler in sinks:
                    try:
                        handler.flush()
                    except Exception:
                        pass
                    try:
                        handler.close()
                    except Exception:
                        pass
            finally:
                with _LOGGING_STATE_LOCK:
                    _LOG_REAPER_THREADS.pop(threading.current_thread(), None)

        with _LOGGING_STATE_LOCK:
            active_reapers = [
                thread
                for thread in _LOG_REAPER_THREADS
                if thread.is_alive()
            ]
            if not active_reapers:
                reaper = threading.Thread(
                    target=reap_old_listener,
                    name="logging-listener-reaper",
                    daemon=True,
                )
                _LOG_REAPER_THREADS[reaper] = time.monotonic()
                reaper.start()
    if queue_handler is not None:
        queue_handler.close()
    return stopped


def configure_logging(*, force: bool = False, config: Any | None = None) -> None:
    """Configure a compact, context-aware logging pipeline."""
    global _ATEXIT_REGISTERED, _LOG_LISTENER, _LOG_QUEUE_HANDLER, _LOG_SINK_HANDLERS

    root = logging.getLogger()
    if root.handlers and not force:
        return

    if force:
        with _LOGGING_STATE_LOCK:
            for retired in tuple(_LOG_REAPER_THREADS):
                if not retired.is_alive():
                    _LOG_REAPER_THREADS.pop(retired, None)
            # Do not create another listener/sink generation while a prior
            # blocked sink is still being retired. The current async pipeline
            # remains usable and a later settings update can retry safely.
            if _LOG_REAPER_THREADS and _LOG_LISTENER is not None:
                return
        # Runtime settings reload runs on the event-loop thread. Keep a stuck
        # old sink from turning log reconfiguration into another global pause;
        # the full atexit shutdown still uses the longer default budget.
        shutdown_logging(timeout=0.05)

    log_level = _parse_level(
        getattr(config, "level", None) if config is not None else os.getenv("LOG_LEVEL"),
        default=logging.INFO,
    )
    third_party_level = _parse_level(
        getattr(config, "third_party_level", None)
        if config is not None
        else os.getenv("LOG_THIRD_PARTY_LEVEL"),
        default=logging.WARNING,
    )
    if config is not None:
        color_mode = str(getattr(config, "color", "on") or "on").strip().lower()
        log_to_file = bool(getattr(config, "to_file", False))
        log_file_path_raw = str(
            getattr(config, "file_path", "data/bot.log") or "data/bot.log"
        ).strip()
        log_file_max_bytes = max(1024, int(getattr(config, "file_max_bytes", 5 * 1024 * 1024)))
        log_file_backup_count = max(1, int(getattr(config, "file_backup_count", 3)))
    else:
        color_mode = os.getenv("LOG_COLOR", "on").strip().lower()
        log_to_file = _parse_bool(os.getenv("LOG_TO_FILE"), default=False)
        log_file_path_raw = (
            (os.getenv("LOG_FILE_PATH") or "data/bot.log").strip()
            or "data/bot.log"
        )
        log_file_max_bytes = _parse_int(
            os.getenv("LOG_FILE_MAX_BYTES"),
            default=5 * 1024 * 1024,
            min_value=1024,
        )
        log_file_backup_count = _parse_int(
            os.getenv("LOG_FILE_BACKUP_COUNT"),
            default=3,
            min_value=1,
        )
    queue_size = _parse_int(
        os.getenv("LOG_QUEUE_SIZE"),
        default=_DEFAULT_QUEUE_SIZE,
        min_value=256,
    )

    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except Exception:
                pass

    handler = logging.StreamHandler(stream=sys.stdout)
    use_color = color_mode == "on" or (color_mode == "auto" and bool(getattr(sys.stdout, "isatty", lambda: False)()))
    if color_mode == "off":
        use_color = False
    handler.setFormatter(
        ChineseLogFormatter(
            _DEFAULT_FORMAT,
            datefmt=_DEFAULT_DATEFMT,
            use_color=use_color,
        )
    )
    sink_handlers: list[logging.Handler] = [handler]

    if log_to_file:
        file_path = Path(log_file_path_raw)
        if not file_path.is_absolute():
            file_path = Path.cwd() / file_path
        try:
            file_path.parent.mkdir(parents=True, exist_ok=True)
            file_handler = _SecureRotatingFileHandler(
                file_path,
                maxBytes=log_file_max_bytes,
                backupCount=log_file_backup_count,
                encoding="utf-8",
            )
            file_handler.setFormatter(
                ChineseLogFormatter(
                    _DEFAULT_FORMAT,
                    datefmt=_DEFAULT_DATEFMT,
                    use_color=False,
                )
            )
            sink_handlers.append(file_handler)
        except Exception as exc:
            try:
                sys.stderr.write(f"[logging_setup] enable file logging failed: {exc}\n")
            except Exception:
                pass

    log_queue: queue.Queue[Any] = queue.Queue(maxsize=queue_size)
    queue_handler = _DroppingQueueHandler(log_queue)
    # ContextVars belong to the event-loop thread. Capture them before the
    # record crosses into the listener thread.
    queue_handler.addFilter(LogContextFilter())
    listener = _BoundedQueueListener(
        log_queue,
        *sink_handlers,
        respect_handler_level=True,
    )
    listener.start()

    old_handlers = list(root.handlers)
    root.handlers.clear()
    root.setLevel(log_level)
    root.addHandler(queue_handler)
    for old_handler in old_handlers:
        if old_handler is queue_handler:
            continue
        try:
            old_handler.close()
        except Exception:
            pass

    with _LOGGING_STATE_LOCK:
        _LOG_QUEUE_HANDLER = queue_handler
        _LOG_LISTENER = listener
        _LOG_SINK_HANDLERS = tuple(sink_handlers)
        if not _ATEXIT_REGISTERED:
            atexit.register(shutdown_logging)
            _ATEXIT_REGISTERED = True

    noisy_loggers = (
        "LiteLLM",
        "litellm",
        "httpx",
        "httpcore",
        "aiogram.event",
        "aiogram.dispatcher",
    )
    for logger_name in noisy_loggers:
        lg = logging.getLogger(logger_name)
        lg.setLevel(third_party_level)
        # Let root handler own formatting/output to avoid duplicate styles.
        lg.handlers.clear()
        lg.propagate = True


def set_log_context(
    *,
    req_id: str | int | None = None,
    flow_id: str | int | None = None,
    update_id: str | int | None = None,
    chat_id: str | int | None = None,
    user_id: str | int | None = None,
) -> dict[str, Token[str]]:
    tokens: dict[str, Token[str]] = {}
    if req_id is not None:
        tokens["req"] = _REQ_ID.set(str(req_id))
    if flow_id is not None:
        tokens["flow"] = _FLOW_ID.set(str(flow_id))
    if update_id is not None:
        tokens["update"] = _UPDATE_ID.set(str(update_id))
    if chat_id is not None:
        tokens["chat"] = _CHAT_ID.set(str(chat_id))
    if user_id is not None:
        tokens["user"] = _USER_ID.set(str(user_id))
    return tokens


def reset_log_context(tokens: dict[str, Token[str]]) -> None:
    mapping: dict[str, ContextVar[str]] = {
        "req": _REQ_ID,
        "flow": _FLOW_ID,
        "update": _UPDATE_ID,
        "chat": _CHAT_ID,
        "user": _USER_ID,
    }
    for key in ("user", "chat", "update", "flow", "req"):
        token = tokens.get(key)
        if token is None:
            continue
        var = mapping[key]
        var.reset(token)


def get_log_context() -> dict[str, Any]:
    return {
        "req_id": _REQ_ID.get(),
        "flow_id": _FLOW_ID.get(),
        "update_id": _UPDATE_ID.get(),
        "chat_id": _CHAT_ID.get(),
        "user_id": _USER_ID.get(),
    }
