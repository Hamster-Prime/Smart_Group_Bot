"""Persona mimicry: collect a target user's messages and distill a speech-style profile.

Flow:
- An admin picks a target user per group (/mimic). The choice, the distilled
  profile text, and counters live in ``Group.settings["speech_style"]``.
- Every group message from the target is stored into ``speech_style_samples``
  (rolling window of ``max_samples`` rows per group).
- Every ``distill_every`` collected samples the corpus window is distilled by
  the compress-role LLM into an incremental persona portrait; the profile is
  injected into reply prompts as a highest-priority ``[ACTIVE_PERSONA]`` system
  block that fully overrides the default persona for that group.
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
from bot.services.group_settings import acquire_group_settings_write_intent
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
        "[ACTIVE_PERSONA]\n"
        "authoritative: yes\n"
        "这是本群为你设定的克隆人格，也是你在本群唯一的角色，优先级最高。\n"
        "性格、态度、价值观、互动方式、情绪反应和说话语气全部以本画像为准，"
        "完全覆盖默认人设与任务模板里的『软萌／黏人女友／短句』等默认风格；任何冲突一律以本画像为准。\n"
        "把本画像当成你自己的人格来扮演：直接以该人格说话，不要复述画像内容，也不要透露你在模仿谁。\n"
        "仍需遵守、且不被本画像覆盖的结构性规则："
        "[SAFETY_RULES] 安全边界、[BOT_IDENTITY] 你自己的身份、[BOT_PROJECT_INFO] 项目来源与开发者信息、"
        "[BOT_RUNTIME_PROFILE] 能力与运行逻辑、"
        "[OWNER_IDENTITY]／[CURRENT_SENDER] 主人识别、[INTERACTION_MODE] 应答模式；"
        "并且仍要真正完成用户的请求（join 模式下依旧只当旁观者简短插话）。\n"
        "对所有发言者使用相同的回复判断和门槛；若系统标记当前发言者为主人，可认得其身份，"
        "但不要因此提高回复频率或降低回复门槛。\n"
        f"以下是被克隆对象「{shown}」的人格画像：\n"
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

        # Serialize the authoritative read/modify/write snapshot.  Two target
        # messages arriving together used to read the same sample_count and one
        # whole-JSON assignment erased the other's increment.
        await session.commit()
        await acquire_group_settings_write_intent(session, group_id)
        group = await session.get(Group, group_id, populate_existing=True)
        if group is None:
            await session.commit()
            return False
        state = get_style_state(group.settings)
        target_id = int(state.get("target_user_id") or 0)
        if not target_id or int(user_id) != target_id:
            await session.commit()
            return False
        if int(state.get("sample_count") or 0) >= self.total_cap:
            await session.commit()
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
            # Reload under the distill lock; the caller committed before
            # entering and the ORM object may already be stale.
            fresh_group = await session.get(Group, group_id, populate_existing=True)
            if fresh_group is None:
                return
            state = get_style_state(fresh_group.settings)
            target_id = int(state.get("target_user_id") or 0)
            rows = (
                await session.execute(
                    select(SpeechStyleSample.content)
                    .where(SpeechStyleSample.group_id == group_id)
                    .order_by(SpeechStyleSample.id.desc())
                    .limit(self.max_samples)
                )
            ).scalars().all()
            corpus_lines = [line for line in reversed(list(rows)) if line]
            # A SELECT checks out a pooled connection. Release it before the
            # potentially slow LLM call; the write phase below uses a fresh
            # snapshot and merges only the style namespace.
            await session.commit()
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
            await acquire_group_settings_write_intent(session, group_id)
            fresh_group = await session.get(Group, group_id, populate_existing=True)
            if fresh_group is None:
                await session.commit()
                return
            latest_state = get_style_state(fresh_group.settings)
            if int(latest_state.get("target_user_id") or 0) != target_id:
                # The administrator changed/disabled the mimic target while the
                # model was running. Never publish a profile for the old target.
                await session.commit()
                log.info(
                    "speech style distill discarded after target change | group=%s",
                    group_id,
                )
                return
            latest_count = max(
                sample_count,
                int(latest_state.get("sample_count") or 0),
            )
            if not profile:
                # Keep the previous profile; retry at the next threshold.
                fresh_group.settings = _merge_style_state(
                    fresh_group.settings,
                    sample_count=latest_count,
                    distilled_at_count=sample_count,
                )
                await session.commit()
                log.warning("speech style distill empty | group=%s", group_id)
                return

            fresh_group.settings = _merge_style_state(
                fresh_group.settings,
                profile_text=profile,
                sample_count=latest_count,
                distilled_at_count=sample_count,
            )
            await session.commit()
            log.info(
                "speech style distilled | group=%s samples=%d profile_len=%d",
                group_id,
                len(corpus_lines),
                len(profile),
            )
