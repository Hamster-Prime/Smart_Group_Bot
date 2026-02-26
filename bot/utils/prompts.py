"""Load LLM prompt templates from prompt/ directory."""
from __future__ import annotations

from pathlib import Path

_PROMPT_DIR = Path(__file__).resolve().parent.parent.parent / "prompt"


def _load(name: str) -> str:
    return (_PROMPT_DIR / name).read_text(encoding="utf-8").strip()


DECISION_SYSTEM: str = _load("decision.md")
MODERATION_SYSTEM: str = _load("moderation.md")
RAG_SYSTEM: str = _load("rag.md")
CASUAL_SYSTEM: str = _load("casual.md")
KB_MANAGE_SYSTEM: str = _load("kb_manage.md")
RULE_MANAGE_SYSTEM: str = _load("rule_manage.md")
COMPRESS_SYSTEM: str = _load("compress.md")
SKILL_SYSTEM: str = _load("skill.md")
SKILL_ANSWER_SYSTEM: str = _load("skill_answer.md")
