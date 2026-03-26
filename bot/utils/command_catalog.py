from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class CommandEntry:
    command: str
    usage: str
    purpose: str
    suggest_when: str
    section: str


_COMMANDS: tuple[CommandEntry, ...] = (
    CommandEntry("/help", "/help", "查看完整帮助", "用户问你有哪些命令、怎么用、功能总览", "核心入口"),
    CommandEntry("/lm", "/lm", "查看永久记忆列表，支持翻页和删除", "用户要查看、删除永久记忆，或找不到某条记忆", "核心入口"),
    CommandEntry("/lm add", "/lm add <内容>", "新增一条永久记忆", "用户想显式通过命令写入永久记忆", "核心入口"),
    CommandEntry(
        "/lm replace",
        "/lm replace <#ID或关键词> => <新内容>",
        "修改已有永久记忆",
        "用户想显式通过命令修改永久记忆",
        "核心入口",
    ),
    CommandEntry("/task", "/task <自然语言>", "创建定时任务", "用户想显式创建提醒或定时查询任务", "核心入口"),
    CommandEntry("/tasks", "/tasks", "查看定时任务列表，支持翻页和删除", "用户要查看或删除已有定时任务", "核心入口"),
    CommandEntry("/canceltask", "/canceltask <任务ID>", "按任务 ID 取消定时任务", "用户已经知道任务 ID，想直接取消", "核心入口"),
    CommandEntry("/addrule", "/addrule <自然语言>", "新增群规", "用户想显式通过命令新增群规", "核心入口"),
    CommandEntry("/rules", "/rules", "查看群规列表，支持翻页和删除", "用户要查看或删除群规", "核心入口"),
    CommandEntry("/av", "/av <番号/演员/关键词>", "搜索 AV 资源", "用户想搜片或查询 AV 详情", "核心入口"),
    CommandEntry("/warnings", "/warnings", "查看当前群警告/封禁名单", "管理员想查看审核处罚情况", "群审核管理"),
    CommandEntry("/aiexempt", "回复目标用户消息后发送 /aiexempt", "豁免某用户的 AI 审核", "管理员想让某用户跳过审核", "群审核管理"),
    CommandEntry("/unaiexempt", "回复目标用户消息后发送 /unaiexempt", "取消某用户的 AI 审核豁免", "管理员想恢复某用户的审核", "群审核管理"),
    CommandEntry("/mute", "回复目标用户消息后发送 /mute", "忽略某用户后续消息回复", "管理员不想让 bot 再回复某用户", "群审核管理"),
    CommandEntry("/mute all", "/mute all", "全群仅审核不回复", "管理员希望 bot 暂时只做审核", "群审核管理"),
    CommandEntry("/unmute", "回复目标用户消息后发送 /unmute", "恢复某用户的消息回复", "管理员想重新允许 bot 回复某用户", "群审核管理"),
    CommandEntry("/unmute all", "/unmute all", "恢复本群正常回复", "管理员想退出全群静默回复模式", "群审核管理"),
    CommandEntry("/proactive", "/proactive on|off|status", "控制主动话题开关和状态", "管理员要控制 bot 主动开话题", "群审核管理"),
    CommandEntry("/authgroup", "/authgroup <群ID> 或群内直接 /authgroup", "授权群组", "最高管理员要授权新群", "最高管理员命令"),
    CommandEntry("/unauthgroup", "/unauthgroup <群ID> 或群内直接 /unauthgroup", "撤销群组授权", "最高管理员要取消群授权", "最高管理员命令"),
    CommandEntry("/authlist", "/authlist", "查看授权群组列表", "最高管理员要查看所有已授权群", "最高管理员命令"),
    CommandEntry("/authadmin", "回复用户后 /authadmin，或 /authadmin <群ID> <用户ID>", "授权群管理员", "最高管理员要给某人群管理权限", "最高管理员命令"),
    CommandEntry("/unauthadmin", "回复用户后 /unauthadmin，或 /unauthadmin <群ID> <用户ID>", "撤销群管理员权限", "最高管理员要取消某人群管理权限", "最高管理员命令"),
    CommandEntry("/adminlist", "/adminlist <群ID> 或群内直接 /adminlist", "查看群管理列表", "最高管理员要查看某群管理列表", "最高管理员命令"),
    CommandEntry("/atreply", "/atreply 或 /atreply enable|disable", "控制仅 @ 才回复", "最高管理员要调整 bot 的 @ 回复模式", "最高管理员命令"),
    CommandEntry("/tts", "/tts 或 /tts enable|disable|always", "控制 TTS 状态", "最高管理员要查看或调整语音模式", "最高管理员命令"),
    CommandEntry("/av enable", "/av enable", "启用本群 AV 查询", "最高管理员要在当前群打开 AV 查询", "最高管理员命令"),
    CommandEntry("/av disable", "/av disable", "停用本群 AV 查询", "最高管理员要在当前群关闭 AV 查询", "最高管理员命令"),
)


def build_help_text() -> str:
    return (
        "<b>命令总览</b>\n\n"
        "<b>核心入口</b>\n"
        "/help：查看帮助\n"
        "/lm：永久记忆列表，支持翻页和删除\n"
        "/lm add &lt;内容&gt;：新增永久记忆\n"
        "/lm replace &lt;#ID或关键词&gt; =&gt; &lt;新内容&gt;：修改永久记忆\n"
        "/task &lt;自然语言&gt;：创建定时任务\n"
        "/tasks：定时任务列表，支持翻页和删除\n"
        "/canceltask &lt;任务ID&gt;：按 ID 取消任务\n"
        "/addrule &lt;自然语言&gt;：新增群规\n"
        "/rules：群规列表，支持翻页和删除\n"
        "/av &lt;番号/演员/关键词&gt;：搜索 JAVBUS + MADOUQU + DMM + FC2\n\n"
        "<b>语义入口</b>\n"
        "主模型会自动调用 skill 处理：永久记忆新增/查看/修改、群规新增/查看、定时任务创建/查看。\n"
        "删除统一走 /lm、/rules、/tasks 这些命令页的内联按钮。\n\n"
        "<b>群审核管理（需已授权）</b>\n"
        "/warnings：查看警告/封禁名单\n"
        "/aiexempt：回复目标用户消息后豁免审核\n"
        "/unaiexempt：回复目标用户消息后取消豁免\n"
        "/mute：回复目标用户消息后忽略其后续回复\n"
        "/mute all：本群仅做审核，不再回复\n"
        "/unmute：回复目标用户消息后恢复其回复\n"
        "/unmute all：恢复本群正常回复\n"
        "/proactive on|off|status：主动话题开关/状态\n\n"
        "<b>最高管理员命令</b>\n"
        "/authgroup / unauthgroup / authlist\n"
        "/authadmin / unauthadmin / adminlist\n"
        "/atreply / atreply enable|disable\n"
        "/tts / tts enable|disable|always\n"
        "/av enable|disable"
    )


def build_command_guide_context() -> str:
    lines = [
        "[BOT_COMMAND_GUIDE]",
        "authoritative: yes",
        "Use this block when users ask what commands exist, how to use them, or when you should remind them of the correct /command form.",
        "If a user is trying to delete memories/rules/tasks, prefer reminding the corresponding explicit command entrypoint.",
        "When suggesting a command, mention a short usage example rather than only saying the command name.",
        "commands:",
    ]
    for item in _COMMANDS:
        lines.append(f"- command: {item.command}")
        lines.append(f"  section: {item.section}")
        lines.append(f"  usage: {item.usage}")
        lines.append(f"  purpose: {item.purpose}")
        lines.append(f"  suggest_when: {item.suggest_when}")
    return "\n".join(lines)
