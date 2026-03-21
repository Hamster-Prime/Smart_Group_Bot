from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Protocol

from aiogram.types import Message

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


@dataclass(slots=True)
class SkillContext:
    session: AsyncSession | None = None
    message: Message | None = None
    bot: Any | None = None
    chat_id: int = 0
    llm: Any | None = None
    history: list[dict[str, str]] = field(default_factory=list)
    sender_user_id: int = 0
    sender_username: str = ""
    sender_is_owner: bool = False
    sender_is_tg_admin: bool = False
    current_user_text: str = ""
    default_sticker_file_ids: list[str] = field(default_factory=list)
    handled: bool = False
    sticker_sent: bool = False
    sticker_file_id: str = ""
    tts_sent: bool = False
    tts_text: str = ""
    embedded_reply_sent: bool = False
    embedded_reply_text: str = ""
    suppress_followup_text: bool = False


@dataclass(slots=True)
class SkillRunResult:
    ok: bool
    skill: str
    summary: str
    payload: dict[str, Any] = field(default_factory=dict)
    error: str = ""


@dataclass(slots=True)
class SkillAnswerResult:
    text: str = ""
    handled: bool = False
    sticker_sent: bool = False
    sticker_file_id: str = ""
    tts_sent: bool = False
    tts_text: str = ""


class Skill(Protocol):
    name: str
    description: str
    parameters_schema: dict[str, Any]

    async def run(self, arguments: dict[str, Any], context: SkillContext) -> SkillRunResult:
        ...
