from __future__ import annotations

import logging
import os
import sys
from contextvars import ContextVar, Token
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any

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


def configure_logging(*, force: bool = False, config: Any | None = None) -> None:
    """Configure a compact, context-aware logging pipeline."""
    root = logging.getLogger()
    if root.handlers and not force:
        return

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
        log_file_path_raw = str(getattr(config, "file_path", "bot.log") or "bot.log").strip()
        log_file_max_bytes = max(1024, int(getattr(config, "file_max_bytes", 5 * 1024 * 1024)))
        log_file_backup_count = max(1, int(getattr(config, "file_backup_count", 3)))
    else:
        color_mode = os.getenv("LOG_COLOR", "on").strip().lower()
        log_to_file = _parse_bool(os.getenv("LOG_TO_FILE"), default=False)
        log_file_path_raw = (os.getenv("LOG_FILE_PATH") or "bot.log").strip() or "bot.log"
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
    handler.addFilter(LogContextFilter())

    root.handlers.clear()
    root.setLevel(log_level)
    root.addHandler(handler)

    if log_to_file:
        file_path = Path(log_file_path_raw)
        if not file_path.is_absolute():
            file_path = Path.cwd() / file_path
        try:
            file_path.parent.mkdir(parents=True, exist_ok=True)
            file_handler = RotatingFileHandler(
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
            file_handler.addFilter(LogContextFilter())
            root.addHandler(file_handler)
        except Exception as exc:
            try:
                sys.stderr.write(f"[logging_setup] enable file logging failed: {exc}\n")
            except Exception:
                pass

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
