"""Global memory service holder — initialized at startup, imported by handlers."""
from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from bot.services.memory import MemoryService

_instance: MemoryService | None = None


def init(mem: MemoryService) -> None:
    global _instance
    _instance = mem


def get() -> MemoryService:
    assert _instance is not None, "MemoryService not initialized"
    return _instance


def get_optional() -> MemoryService | None:
    """Return the service when startup completed, else ``None`` for dry handlers/tests."""

    return _instance
