"""Load LLM prompt templates from prompt/ directory."""
from __future__ import annotations

from pathlib import Path

_PROMPT_DIR = Path(__file__).resolve().parent.parent.parent / "prompt"


def _load(name: str) -> str:
    return (_PROMPT_DIR / name).read_text(encoding="utf-8").strip()


def with_persona(task_prompt: str) -> str:
    persona = PERSONA_SYSTEM.strip()
    task = (task_prompt or "").strip()
    if persona and task:
        return f"{persona}\n\n[TASK_PROMPT]\n{task}"
    if persona:
        return persona
    return task


DECISION_SYSTEM: str = _load("decision.md")
MODERATION_SYSTEM: str = _load("moderation.md")
CASUAL_SYSTEM: str = _load("casual.md")
RULE_MANAGE_SYSTEM: str = _load("rule_manage.md")
GROUP_INTENT_SYSTEM: str = _load("group_intent.md")
COMPRESS_SYSTEM: str = _load("compress.md")
SKILL_TOOL_SYSTEM: str = _load("skill_tools.md")
STICKER_DECISION_SYSTEM: str = _load("sticker_decision.md")
PERSONA_SYSTEM: str = _load("persona.md")
