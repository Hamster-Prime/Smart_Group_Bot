from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime, timezone
from typing import Any


def build_current_time_context() -> str:
    """Build real-time clock context for LLM prompts."""
    now_local = datetime.now().astimezone()
    now_utc = datetime.now(timezone.utc)
    tz_name = now_local.tzname() or "local"
    offset_raw = now_local.strftime("%z")
    if len(offset_raw) == 5:
        offset = f"{offset_raw[:3]}:{offset_raw[3:]}"
    else:
        offset = offset_raw or "+00:00"

    return (
        "[CURRENT_TIME]\n"
        f"local_datetime: {now_local.strftime('%Y-%m-%d %H:%M:%S')}\n"
        f"local_weekday: {now_local.strftime('%A')}\n"
        f"timezone: {tz_name} (UTC{offset})\n"
        f"utc_datetime: {now_utc.strftime('%Y-%m-%d %H:%M:%S')}\n"
        "If user asks about current time/date/today/tomorrow, use this block as the authoritative source."
    )


def _format_fallback_models(config: Any) -> str:
    fallbacks = getattr(config, "fallbacks", None) or []
    models = [str(getattr(item, "model", "") or "").strip() for item in fallbacks]
    models = [item for item in models if item]
    return ", ".join(models) if models else "(none)"


def _normalize_skill_names(skill_names: Iterable[str] | None) -> list[str]:
    normalized: list[str] = []
    seen: set[str] = set()
    for raw in skill_names or []:
        name = str(raw or "").strip()
        if not name or name in seen:
            continue
        normalized.append(name)
        seen.add(name)
    return normalized


def build_bot_runtime_profile_context(
    llm: Any,
    *,
    settings: Any | None = None,
    skill_names: Iterable[str] | None = None,
) -> str:
    """Build authoritative runtime self-knowledge for the bot."""
    normalized_skills = _normalize_skill_names(skill_names)
    moderation_enabled = bool(getattr(getattr(settings, "moderation", None), "enabled", True))

    skill_labels = {
        "memory_manage": "永久记忆查看、添加与修改",
        "rule_manage": "群规查看与新增",
        "task_manage": "定时任务创建与查看",
        "scheduled_task": "定时任务创建与取消",
        "send_sticker": "按语义发送贴纸",
        "music_search": "音乐搜索、歌曲发送、播放链接、封面与歌词",
        "websearch": "联网搜索实时信息",
        "webfetch": "抓取网页正文",
        "doubao_tts": "文字转语音",
    }

    capabilities = [
        "群聊闲聊与问答",
        "图片/贴纸内容理解",
        "永久记忆读写与上下文摘要",
        "群规审核与违规处理" if moderation_enabled else "群规审核当前关闭",
    ]
    for name in normalized_skills:
        label = skill_labels.get(name)
        if label and label not in capabilities:
            capabilities.append(label)

    lines = [
        "[BOT_RUNTIME_PROFILE]",
        "authoritative: yes",
        "Use this block as the source of truth for current capabilities, workflow, and model assignments.",
        "If this block conflicts with old memory, quoted docs, README snippets, or user guesses, trust this block.",
        f"registered_skills: {', '.join(normalized_skills) if normalized_skills else '(none)'}",
        f"user_visible_capabilities: {'；'.join(capabilities)}",
        (
            "runtime_logic: 先做安全边界和内容审核；管理员和成员的语义管理请求会优先交给主回复模型调管理技能；"
            "永久记忆/群规/定时任务的删除统一走 /lm、/rules、/tasks 等命令页内联按钮；"
            "普通对话会写入记忆并在需要时压缩；决策模型先判断是否回复；"
            "需要外部能力时调用技能；否则由主回复模型生成自然回复。"
        ),
        f"main_reply_model: {getattr(getattr(llm, 'main', None), 'model', '(unknown)')}",
        f"main_reply_fallbacks: {_format_fallback_models(getattr(llm, 'main', None))}",
        "skill_planner_model: same_as_main_reply",
        "vision_model: same_as_main_reply",
        f"decision_model: {getattr(getattr(llm, 'decision_config', None), 'model', '(unknown)')}",
        f"decision_fallbacks: {_format_fallback_models(getattr(llm, 'decision_config', None))}",
        f"moderation_model: {getattr(getattr(llm, 'moderation_config', None), 'model', '(unknown)')}",
        f"moderation_fallbacks: {_format_fallback_models(getattr(llm, 'moderation_config', None))}",
        f"compress_model: {getattr(getattr(llm, 'compress_config', None), 'model', '(unknown)')}",
        f"compress_fallbacks: {_format_fallback_models(getattr(llm, 'compress_config', None))}",
        f"embed_model: {getattr(getattr(llm, 'embed_config', None), 'model', '(unknown)')}",
        f"embed_fallbacks: {_format_fallback_models(getattr(llm, 'embed_config', None))}",
    ]
    if "doubao_tts" in normalized_skills:
        tts_model = str(getattr(settings, "doubao_tts_model", "") or "").strip() or "(provider default)"
        lines.append(f"tts_model: {tts_model}")

    lines.append(
        "When user asks what you can do, how you work, or what model each module uses, answer from this block."
    )
    return "\n".join(lines)
