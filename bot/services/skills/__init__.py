from __future__ import annotations

__all__ = ["SkillService"]


def __getattr__(name: str):
    if name == "SkillService":
        from bot.services.skills.service import SkillService

        return SkillService
    raise AttributeError(name)
