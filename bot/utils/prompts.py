"""Load LLM prompt templates from prompt/ directory."""
from __future__ import annotations

from pathlib import Path

from bot.utils.bot_identity import build_bot_identity_context

_PROMPT_DIR = Path(__file__).resolve().parent.parent.parent / "prompt"


def _load(name: str) -> str:
    return (_PROMPT_DIR / name).read_text(encoding="utf-8").strip()


def with_persona(task_prompt: str) -> str:
    persona = PERSONA_SYSTEM.strip()
    identity = build_bot_identity_context().strip()
    task = (task_prompt or "").strip()
    parts = [part for part in (persona, identity) if part]
    if task:
        parts.append(f"[TASK_PROMPT]\n{task}")
    return "\n\n".join(parts)


DECISION_SYSTEM: str = _load("decision.md")
MODERATION_SYSTEM: str = _load("moderation.md")
CASUAL_SYSTEM: str = _load("casual.md")
MANAGE_INTENT_SYSTEM: str = _load("manage_intent.md")
COMPRESS_SYSTEM: str = _load("compress.md")
SKILL_TOOL_SYSTEM: str = _load("skill_tools_v2.md")
STICKER_DECISION_SYSTEM: str = _load("sticker_decision.md")
REPLY_MODE_SYSTEM: str = _load("reply_mode.md")
PERSONA_SYSTEM: str = _load("persona.md")
PROACTIVE_TOPIC_SYSTEM: str = _load("proactive_topic.md")
STYLE_DISTILL_SYSTEM: str = _load("style_distill.md")
