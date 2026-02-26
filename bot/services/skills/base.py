from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(slots=True)
class SkillRunResult:
    ok: bool
    skill: str
    summary: str
    payload: dict[str, Any] = field(default_factory=dict)
    error: str = ""
