from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime, timezone
from typing import Any

from bot.utils.command_catalog import build_command_guide_context


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


def _same_model_family(left: Any, right: Any) -> bool:
    if left is None or right is None:
        return False
    if str(getattr(left, "model", "") or "").strip() != str(getattr(right, "model", "") or "").strip():
        return False
    return _format_fallback_models(left) == _format_fallback_models(right)


def _model_value(
    *,
    settings: Any | None,
    llm: Any,
    settings_attr: str,
    llm_attr: str,
) -> Any:
    bot_cfg = getattr(settings, "bot", None)
    candidate = getattr(bot_cfg, settings_attr, None) if bot_cfg is not None else None
    if candidate is not None:
        return candidate
    return getattr(llm, llm_attr, None)


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
        "send_sticker": "按语义发送贴纸",
        "music_search": "音乐搜索、歌曲发送、播放链接、封面与歌词",
        "websearch": "联网搜索实时信息",
        "webfetch": "抓取网页正文",
        "bilibili_search": "B站搜索、视频详情、热门与排行榜",
        "weibo_search": "微博热搜、搜索与链接内容提取",
        "sub2api_query": "本站模型网关查询：可用模型列表与指定模型测活（问模型能不能用时优先用它，不要联网搜索）",
        "doubao_tts": "文字转语音",
        "vote_ban": "明确请求且回复目标消息时发起民主投票封禁（受用户额度限制）",
    }

    capabilities = [
        "群聊闲聊与问答",
        "图片/贴纸内容理解",
        "永久记忆读写与上下文摘要",
        "群规审核与违规处理" if moderation_enabled else "群规审核当前关闭",
    ]
    main_cfg = _model_value(settings=settings, llm=llm, settings_attr="main_model", llm_attr="main")
    decision_cfg = _model_value(
        settings=settings,
        llm=llm,
        settings_attr="decision_model",
        llm_attr="decision_config",
    )
    vision_cfg = _model_value(
        settings=settings,
        llm=llm,
        settings_attr="vision_model",
        llm_attr="vision_config",
    )
    moderation_cfg = _model_value(
        settings=settings,
        llm=llm,
        settings_attr="moderation_model",
        llm_attr="moderation_config",
    )
    compress_cfg = _model_value(
        settings=settings,
        llm=llm,
        settings_attr="compress_model",
        llm_attr="compress_config",
    )
    embed_cfg = _model_value(
        settings=settings,
        llm=llm,
        settings_attr="embed_model",
        llm_attr="embed_config",
    )
    vision_same_as_main = _same_model_family(vision_cfg, main_cfg)

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
            "runtime_logic: 先做安全边界和内容审核；管理员的语义管理请求会优先交给主回复模型调管理技能；"
            "永久记忆/群规的删除统一走 /lm、/rules 命令页内联按钮；"
            "普通对话会写入记忆并在需要时压缩；决策模型先判断是否回复；"
            "需要外部能力时调用技能；否则由主回复模型生成自然回复。"
        ),
        f"main_reply_model: {getattr(main_cfg, 'model', '(unknown)')}",
        f"main_reply_fallbacks: {_format_fallback_models(main_cfg)}",
        "skill_planner_model: same_as_main_reply",
        f"vision_model: {'same_as_main_reply' if vision_same_as_main else getattr(vision_cfg, 'model', '(unknown)')}",
        f"vision_fallbacks: {'same_as_main_reply' if vision_same_as_main else _format_fallback_models(vision_cfg)}",
        f"decision_model: {getattr(decision_cfg, 'model', '(unknown)')}",
        f"decision_fallbacks: {_format_fallback_models(decision_cfg)}",
        f"moderation_model: {getattr(moderation_cfg, 'model', '(unknown)')}",
        f"moderation_fallbacks: {_format_fallback_models(moderation_cfg)}",
        f"compress_model: {getattr(compress_cfg, 'model', '(unknown)')}",
        f"compress_fallbacks: {_format_fallback_models(compress_cfg)}",
        f"embed_model: {getattr(embed_cfg, 'model', '(unknown)')}",
        f"embed_fallbacks: {_format_fallback_models(embed_cfg)}",
    ]
    if "doubao_tts" in normalized_skills:
        tts_model = str(getattr(settings, "doubao_tts_model", "") or "").strip() or "(provider default)"
        lines.append(f"tts_model: {tts_model}")

    lines.append(build_command_guide_context())
    lines.append(
        "When user asks what you can do, how you work, or what model each module uses, answer from this block."
    )
    return "\n".join(lines)
