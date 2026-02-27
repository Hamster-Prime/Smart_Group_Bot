from __future__ import annotations

import logging
import os
import sys
from contextvars import ContextVar, Token
from typing import Any

_REQ_ID: ContextVar[str] = ContextVar("log_req_id", default="-")
_UPDATE_ID: ContextVar[str] = ContextVar("log_update_id", default="-")
_CHAT_ID: ContextVar[str] = ContextVar("log_chat_id", default="-")
_USER_ID: ContextVar[str] = ContextVar("log_user_id", default="-")

_DEFAULT_FORMAT = (
    "%(asctime)s.%(msecs)03d | %(level_cn)s | %(name)s | "
    "更新=%(update_id)s 请求=%(req_id)s 群=%(chat_id)s 用户=%(user_id)s | %(message)s"
)
_DEFAULT_DATEFMT = "%Y-%m-%d %H:%M:%S"
_LEVEL_CN = {
    "DEBUG": "调试",
    "INFO": "信息",
    "WARNING": "警告",
    "ERROR": "错误",
    "CRITICAL": "严重",
}


class LogContextFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.req_id = _REQ_ID.get()
        record.update_id = _UPDATE_ID.get()
        record.chat_id = _CHAT_ID.get()
        record.user_id = _USER_ID.get()
        return True


class ChineseLogFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        record.level_cn = _LEVEL_CN.get(record.levelname, record.levelname)
        return super().format(record)


def _parse_level(raw: str | int | None, *, default: int) -> int:
    if isinstance(raw, int):
        return raw
    if isinstance(raw, str):
        name = raw.strip().upper()
        mapping = logging.getLevelNamesMapping()
        if name in mapping:
            return int(mapping[name])
    return default


def configure_logging(*, force: bool = False) -> None:
    """Configure a compact, context-aware logging pipeline."""
    root = logging.getLogger()
    if root.handlers and not force:
        return

    log_level = _parse_level(os.getenv("LOG_LEVEL"), default=logging.INFO)
    third_party_level = _parse_level(
        os.getenv("LOG_THIRD_PARTY_LEVEL"),
        default=logging.WARNING,
    )

    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except Exception:
                pass

    handler = logging.StreamHandler(stream=sys.stdout)
    handler.setFormatter(ChineseLogFormatter(_DEFAULT_FORMAT, datefmt=_DEFAULT_DATEFMT))
    handler.addFilter(LogContextFilter())

    root.handlers.clear()
    root.setLevel(log_level)
    root.addHandler(handler)

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
    update_id: str | int | None = None,
    chat_id: str | int | None = None,
    user_id: str | int | None = None,
) -> dict[str, Token[str]]:
    tokens: dict[str, Token[str]] = {}
    if req_id is not None:
        tokens["req"] = _REQ_ID.set(str(req_id))
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
        "update": _UPDATE_ID,
        "chat": _CHAT_ID,
        "user": _USER_ID,
    }
    for key in ("user", "chat", "update", "req"):
        token = tokens.get(key)
        if token is None:
            continue
        var = mapping[key]
        var.reset(token)


def get_log_context() -> dict[str, Any]:
    return {
        "req_id": _REQ_ID.get(),
        "update_id": _UPDATE_ID.get(),
        "chat_id": _CHAT_ID.get(),
        "user_id": _USER_ID.get(),
    }
