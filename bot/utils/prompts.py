"""Load LLM prompt templates from prompt/ directory."""
from __future__ import annotations

from pathlib import Path

from bot.utils.bot_identity import build_bot_identity_context
from bot.utils.project_info import build_bot_project_info_context

_PROMPT_DIR = Path(__file__).resolve().parent.parent.parent / "prompt"

_PROMPT_FILES = {
    "decision": "decision.md",
    "moderation": "moderation.md",
    "casual": "casual.md",
    "manage_intent": "manage_intent.md",
    "compress": "compress.md",
    "skill_tools": "skill_tools_v2.md",
    "sticker_decision": "sticker_decision.md",
    "reply_mode": "reply_mode.md",
    "persona": "persona.md",
    "proactive_topic": "proactive_topic.md",
    "style_distill": "style_distill.md",
}


def _load(name: str) -> str:
    return (_PROMPT_DIR / name).read_text(encoding="utf-8").strip()


def load_prompt_defaults() -> dict[str, str]:
    return {key: _load(filename) for key, filename in _PROMPT_FILES.items()}


_RUNTIME_PROMPTS = load_prompt_defaults()


def get_prompt(name: str) -> str:
    key = str(name or "").strip().lower()
    if key not in _PROMPT_FILES:
        raise KeyError(f"unknown prompt: {name}")
    return _RUNTIME_PROMPTS.get(key) or _load(_PROMPT_FILES[key])


def set_runtime_prompts(values: dict[str, str]) -> None:
    for key in _PROMPT_FILES:
        value = str((values or {}).get(key) or "").strip()
        _RUNTIME_PROMPTS[key] = value or _load(_PROMPT_FILES[key])


def with_persona(task_prompt: str) -> str:
    persona = get_prompt("persona").strip()
    project_info = build_bot_project_info_context().strip()
    identity = build_bot_identity_context().strip()
    task = (task_prompt or "").strip()
    parts = [part for part in (persona, project_info, identity) if part]
    if task:
        parts.append(f"[TASK_PROMPT]\n{task}")
    return "\n\n".join(parts)


# Compatibility constants retain the repository defaults. Runtime consumers
# call ``get_prompt`` so Mini App updates take effect without a restart.
DECISION_SYSTEM: str = _RUNTIME_PROMPTS["decision"]
MODERATION_SYSTEM: str = _RUNTIME_PROMPTS["moderation"]
CASUAL_SYSTEM: str = _RUNTIME_PROMPTS["casual"]
MANAGE_INTENT_SYSTEM: str = _RUNTIME_PROMPTS["manage_intent"]
COMPRESS_SYSTEM: str = _RUNTIME_PROMPTS["compress"]
SKILL_TOOL_SYSTEM: str = _RUNTIME_PROMPTS["skill_tools"]
STICKER_DECISION_SYSTEM: str = _RUNTIME_PROMPTS["sticker_decision"]
REPLY_MODE_SYSTEM: str = _RUNTIME_PROMPTS["reply_mode"]
PERSONA_SYSTEM: str = _RUNTIME_PROMPTS["persona"]
PROACTIVE_TOPIC_SYSTEM: str = _RUNTIME_PROMPTS["proactive_topic"]
STYLE_DISTILL_SYSTEM: str = _RUNTIME_PROMPTS["style_distill"]
