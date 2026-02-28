from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

from aiogram.types import Message


@dataclass(slots=True)
class SkillContext:
    message: Message | None = None
    sender_user_id: int = 0
    sender_username: str = ""
    sender_is_owner: bool = False
    current_user_text: str = ""
    default_sticker_file_ids: list[str] = field(default_factory=list)


@dataclass(slots=True)
class SkillRunResult:
    ok: bool
    skill: str
    summary: str
    payload: dict[str, Any] = field(default_factory=dict)
    error: str = ""


class Skill(Protocol):
    name: str
    description: str
    parameters_schema: dict[str, Any]

    async def run(self, arguments: dict[str, Any], context: SkillContext) -> SkillRunResult:
        ...
