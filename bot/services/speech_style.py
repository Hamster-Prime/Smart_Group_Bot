"""Persona mimicry: collect a target user's messages and distill a speech-style profile.

Flow:
- An admin picks a target user per group (/mimic). The choice, the distilled
  profile text, and counters live in ``Group.settings["speech_style"]``.
- Every group message from the target is stored into ``speech_style_samples``
  (rolling window of ``max_samples`` rows per group).
- Every ``distill_every`` collected samples the corpus window is distilled by
  the compress-role LLM into an incremental profile; the profile is injected
  into reply prompts as a ``[SPEECH_STYLE_PROFILE]`` system block.
- Collection stops permanently once ``total_cap`` samples have been seen for
  the current target (reset by re-targeting), so the feature cannot grow
  unbounded.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from bot.db.models import Group, SpeechStyleSample
from bot.utils.prompts import get_prompt
from bot.utils.security import clean_multiline_text, clean_text

log = logging.getLogger(__name__)

STYLE_SETTINGS_KEY = "speech_style"
DEFAULT_DISTILL_EVERY = 50
DEFAULT_MAX_SAMPLES = 200
DEFAULT_TOTAL_CAP = 1000

_distill_locks: dict[int, asyncio.Lock] = {}


def _default_state() -> dict[str, Any]:
    return {
        "target_user_id": 0,
        "target_user_name": "",
        "profile_text": "",
        "sample_count": 0,
        "distilled_at_count": 0,
    }


def get_style_state(group_settings: dict | None) -> dict[str, Any]:
    settings = group_settings or {}
    raw = settings.get(STYLE_SETTINGS_KEY)
    state = _default_state()
    if isinstance(raw, dict):
        state.update({k: raw.get(k, v) for k, v in state.items()})
    return state


def set_style_target(group_settings: dict | None, *, user_id: int, user_name: str) -> dict:
    """Returns a NEW settings dict with the mimic target set (0 disables).

    Retargeting resets the profile and counters.
    """
    settings = dict(group_settings or {})
    state = _default_state()
    state["target_user_id"] = int(user_id or 0)
    state["target_user_name"] = clean_text(user_name or "", max_len=80)
    settings[STYLE_SETTINGS_KEY] = state
    return settings


def _merge_style_state(group_settings: dict | None, **updates: Any) -> dict:
    settings = dict(group_settings or {})
    state = get_style_state(settings)
    state.update(updates)
    settings[STYLE_SETTINGS_KEY] = state
    return settings


def build_style_profile_context(profile_text: str, *, target_name: str = "") -> str:
    profile = clean_multiline_text(profile_text or "", max_len=1200).strip()
    if not profile:
        return ""
    shown = clean_text(target_name or "", max_len=80) or "目标用户"
    return (
        "[SPEECH_STYLE_PROFILE]\n"
        f"以下是群成员「{shown}」的说话风格画像，模仿 TA 的语气、口头禅和节奏来回复。\n"
        "只模仿说话方式，不复述画像本身，也不透露你在模仿谁。\n"
        f"{profile}"
    )


class SpeechStyleService:
    def __init__(
        self,
        llm: Any,
        *,
        distill_every: int = DEFAULT_DISTILL_EVERY,
        max_samples: int = DEFAULT_MAX_SAMPLES,
        total_cap: int = DEFAULT_TOTAL_CAP,
    ) -> None:
        self.llm = llm
        self.distill_every = max(1, int(distill_every))
        self.max_samples = max(1, int(max_samples))
        self.total_cap = max(1, int(total_cap))

    async def collect_sample(
        self,
        session: AsyncSession,
        *,
        group_id: int,
        user_id: int,
        text: str,
    ) -> bool:
        """Store one utterance if it comes from the mimic target.

        Returns True when the sample was collected. Triggers a distill pass
        every ``distill_every`` samples.
        """
        content = clean_multiline_text(text or "", max_len=800).strip()
        if not content:
            return False

        group = await session.get(Group, group_id)
        if group is None:
            return False
        state = get_style_state(group.settings)
        target_id = int(state.get("target_user_id") or 0)
        if not target_id or int(user_id) != target_id:
            return False
        if int(state.get("sample_count") or 0) >= self.total_cap:
            return False

        session.add(
            SpeechStyleSample(group_id=group_id, user_id=target_id, content=content)
        )
        new_count = int(state.get("sample_count") or 0) + 1
        group.settings = _merge_style_state(group.settings, sample_count=new_count)
        await session.flush()
        await self._trim_samples(session, group_id)

        if new_count - int(state.get("distilled_at_count") or 0) >= self.distill_every:
            # Commit the sample first: flush() above grabbed the process-wide
            # SQLite write lock, and the distill LLM call below can take tens
            # of seconds — holding the lock across it would stall every other
            # writer in the bot (same reason memory compaction runs its
            # compress outside any write transaction).
            await session.commit()
            await self._distill(session, group, sample_count=new_count)
        return True

    async def _trim_samples(self, session: AsyncSession, group_id: int) -> None:
        ids = (
            await session.execute(
                select(SpeechStyleSample.id)
                .where(SpeechStyleSample.group_id == group_id)
                .order_by(SpeechStyleSample.id.desc())
                .offset(self.max_samples)
            )
        ).scalars().all()
        if ids:
            await session.execute(
                delete(SpeechStyleSample).where(SpeechStyleSample.id.in_(list(ids)))
            )

    async def _distill(self, session: AsyncSession, group: Group, *, sample_count: int) -> None:
        group_id = int(group.id)
        lock = _distill_locks.setdefault(group_id, asyncio.Lock())
        if lock.locked():
            return
        async with lock:
            state = get_style_state(group.settings)
            rows = (
                await session.execute(
                    select(SpeechStyleSample.content)
                    .where(SpeechStyleSample.group_id == group_id)
                    .order_by(SpeechStyleSample.id.desc())
                    .limit(self.max_samples)
                )
            ).scalars().all()
            corpus_lines = [line for line in reversed(list(rows)) if line]
            if not corpus_lines:
                return

            previous_profile = clean_multiline_text(
                str(state.get("profile_text") or ""), max_len=1200
            )
            payload = (
                f"[已有画像]\n{previous_profile or '(空)'}\n\n"
                f"[新一批消息，共{len(corpus_lines)}条]\n" + "\n".join(corpus_lines)
            )
            try:
                profile = await self.llm.compress(get_prompt("style_distill"), payload)
            except Exception:
                log.exception("speech style distill failed | group=%s", group_id)
                profile = ""

            profile = clean_multiline_text(profile or "", max_len=1200).strip()
            if not profile:
                # Keep the previous profile; retry at the next threshold.
                group.settings = _merge_style_state(
                    group.settings,
                    sample_count=sample_count,
                    distilled_at_count=sample_count,
                )
                log.warning("speech style distill empty | group=%s", group_id)
                return

            group.settings = _merge_style_state(
                group.settings,
                profile_text=profile,
                sample_count=sample_count,
                distilled_at_count=sample_count,
            )
            log.info(
                "speech style distilled | group=%s samples=%d profile_len=%d",
                group_id,
                len(corpus_lines),
                len(profile),
            )
