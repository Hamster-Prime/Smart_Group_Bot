<div align="center">

# 🤖 Smart Group Bot

[![Python](https://img.shields.io/badge/Python-3.12+-3776AB?style=for-the-badge&logo=python&logoColor=white)](https://python.org)
[![License](https://img.shields.io/badge/License-MIT-00C853?style=for-the-badge)](LICENSE)
[![Telegram](https://img.shields.io/badge/Telegram-Bot-26A5E4?style=for-the-badge&logo=telegram&logoColor=white)](https://telegram.org)
[![aiogram](https://img.shields.io/badge/aiogram-3.x-0066CC?style=for-the-badge&logo=aiogram&logoColor=white)](https://docs.aiogram.dev/)
[![LiteLLM](https://img.shields.io/badge/LiteLLM-supported-8B5CF6?style=for-the-badge)](https://docs.litellm.ai/)

**基于 LLM 的 Telegram 群聊智能机器人**

🧠 智能决策 · 🛡️ 内容审核 · 📝 永久记忆 · ⏰ 定时任务 · 🎭 贴纸系统 · 🔍 联网搜索 · 🎵 音乐点播 · 🗣️ TTS 语音

</div>

---

## 架构概览

```
消息进入
  │
  ├─ 内容审核 (keyword / regex / LLM 三级检测)
  │     └─ 命中 → warn / delete / ban（按规则独立配置）
  │
  ├─ 管理意图路由 (manage_intent)
  │     └─ memory_manage / rule_manage / task_manage → 直接执行
  │
  ├─ 决策模型 (decision)
  │     ├─ skip → 结束
  │     └─ casual → 进入回复流程
  │
  ├─ 回复流程 (skill tool-calling loop)
  │     ├─ 主模型自主选择技能调用
  │     ├─ 贴纸决策模块独立判断是否发送贴纸
  │     └─ 回复模式选择 (reply / message)
  │
  └─ 输出
```

---

## 模型角色

项目通过 LiteLLM 接入多供应商，支持 fallback 链自动降级。以下角色均可独立配置供应商和模型，未配置则自动复用主模型：

| 角色 | 用途 | 默认模型 |
|:---|:---|:---|
| **main** | 聊天回复、技能工具调用 | 必须配置 |
| **decision** | 判断是否回复（skip / casual） | 复用 main |
| **moderation** | 内容审核 | 复用 decision |
| **vision** | 图片/贴纸理解 | 复用 main |
| **chat_bridge** | bot 间 /chat 对话 | 复用 main |
| **compress** | 上下文压缩摘要 | 复用 main |
| **embed** | 向量嵌入（预留） | text-embedding-004 |

---

## 功能特性

### 智能决策

决策模型接收消息上下文（是否 @bot、是否回复 bot、发送者身份、最近历史等），输出 `skip` 或 `casual`。被 @ 或回复 bot 消息时强制响应；群友互聊时优先保持沉默。

### 内容审核

支持三种规则类型，每种规则可独立配置命中动作：

| 规则类型 | 说明 |
|:---|:---|
| `keyword` | 关键词字面匹配 |
| `regex` | 正则表达式匹配 |
| `llm` | LLM 语义判断（同义词、变体、谐音等） |

命中动作：`warn`（警告）、`delete`（删除消息）、`ban`（累计 3 次警告后踢出）。支持按用户设置 AI 审核豁免，支持全群「仅审核不回复」模式。

### 永久记忆

管理员通过自然语言或 `/lm` 命令维护群组永久记忆。回复时自动注入 `[permanent-memory]` 上下文。支持列表翻页和内联按钮删除。存储于 SQLite，重启不丢失。

### 定时任务

支持自然语言创建提醒和定时查询任务：
- `reminder`：到时间后提醒群成员
- `agent_task`：到时间后自动执行 LLM 查询并返回结果

通过 `/task` 创建，`/tasks` 查看列表，`/canceltask` 取消。内置后台调度器持续轮询执行。

### 主动话题

群组长时间沉默后，bot 可自动抛出一个结合群记忆的话题。通过 `/proactive on|off|status` 控制，支持静默时段配置。

### 技能系统 (Tool Calling)

主模型通过 function calling 自主决定调用哪些技能，无独立技能规划模型：

| 技能 | 说明 |
|:---|:---|
| `memory_manage` | 查看/新增/修改永久记忆（删除走 /lm） |
| `rule_manage` | 查看/新增群规（删除走 /rules） |
| `task_manage` | 创建/查看定时任务（删除走 /tasks） |
| `send_sticker` | 语义匹配发送贴纸 |
| `websearch` | DuckDuckGo 联网搜索 |
| `webfetch` | 抓取网页正文内容 |
| `music_search` | GD Studio 音乐 API：搜索、点播、歌词、专辑封面 |
| `bilibili_search` | B站视频/UP主搜索、热门、排行榜 |
| `weibo_search` | 微博热搜、内容搜索、Feed 流 |
| `doubao_tts` | 豆包 TTS 语音合成（需配置） |

### 贴纸系统

收到贴纸后自动记录 file_id、emoji、贴纸包和视觉描述到 `sticker_library` 表。回复时由独立的贴纸决策模块判断是否发送，优先按语义从已学习贴纸中选择，支持默认贴纸池兜底。

### Chat Bridge

通过 `/chat enable` 开启 bot 间自动对话。当群内有其他 bot 发消息时，本 bot 会自动回复，形成 bot 间持续对话。

### AV 查询

支持按番号直查、按演员/关键词搜索，来源覆盖 JAVBUS / MADOUQU / DMM / FC2。每群独立开关（`/av enable|disable`），支持内联翻页浏览详情和种子。

### 日志

日志行携带流程上下文（trace_id），支持彩色输出和文件轮转：

| 配置项 | 说明 |
|:---|:---|
| `LOG_LEVEL` | 日志级别 |
| `LOG_COLOR` | on / off / auto |
| `LOG_TO_FILE` | 是否写入文件 |
| `LOG_FILE_PATH` | 文件路径 |
| `LOG_FILE_MAX_BYTES` | 单文件最大字节（超出轮转） |
| `LOG_FILE_BACKUP_COUNT` | 保留历史文件数 |

---

## 命令参考

### 核心入口

| 命令 | 说明 |
|:---|:---|
| `/help` | 查看完整帮助 |
| `/lm` | 永久记忆列表（翻页 + 内联删除） |
| `/lm add <内容>` | 新增永久记忆 |
| `/lm replace <#ID或关键词> => <新内容>` | 修改永久记忆 |
| `/task <自然语言>` | 创建定时任务 |
| `/tasks` | 定时任务列表（翻页 + 内联删除） |
| `/canceltask <ID>` | 按 ID 取消定时任务 |
| `/addrule <自然语言>` | 新增群规 |
| `/rules` | 群规列表（翻页 + 内联删除） |
| `/av <番号/演员/关键词>` | 搜索 AV 资源 |

### 群审核管理（需已授权群管理）

| 命令 | 说明 |
|:---|:---|
| `/warnings` | 查看警告/封禁名单 |
| `/aiexempt` | 回复用户消息后豁免其 AI 审核 |
| `/unaiexempt` | 回复用户消息后取消审核豁免 |
| `/mute` | 回复用户消息后忽略其后续回复 |
| `/mute all` | 全群仅审核，不再回复 |
| `/unmute` | 回复用户消息后恢复其回复 |
| `/unmute all` | 恢复全群正常回复 |
| `/proactive on\|off\|status` | 主动话题开关/状态 |

### 最高管理员命令

| 命令 | 说明 |
|:---|:---|
| `/authgroup [群ID]` | 授权群组 |
| `/unauthgroup [群ID]` | 撤销群组授权 |
| `/authlist` | 授权群组列表 |
| `/authadmin [群ID] [用户ID]` | 授权群管理 |
| `/unauthadmin [群ID] [用户ID]` | 撤销群管理 |
| `/adminlist [群ID]` | 群管理列表 |
| `/atreply [enable\|disable]` | 仅 @ 才回复模式 |
| `/chat [enable\|disable]` | bot 间对话开关 |
| `/tts [enable\|disable\|always]` | TTS 语音模式 |
| `/av enable\|disable` | 每群 AV 查询开关 |

---

## 快速开始

### 环境要求

- Python 3.12+
- Telegram Bot Token
- 至少一个大模型 API（支持 OpenAI / Anthropic / Gemini / OpenAI Compatible 等）

### 安装

```bash
git clone https://github.com/Hamster-Prime/Smart_Group_Bot.git
cd Smart_Group_Bot

# 安装依赖
pip install -e .

# 配置
cp .env.example .env
# 编辑 .env，至少填写 BOT_TOKEN、SUPER_ADMIN_ID、MAIN_PROVIDER_NAME、MAIN_MODEL

# 启动
python start.py
```

### Docker

```bash
docker compose up -d
```

---

## 配置说明

### 供应商配置

```env
# 格式：MODEL_PROVIDER_<NAME>_PROVIDER / API_KEY / API_BASE / STREAM
# 支持 provider: gemini / openai / anthropic / openai_compatible
# API_BASE 后缀自动检测端点格式

MODEL_PROVIDER_GEMINI_PROVIDER=gemini
MODEL_PROVIDER_GEMINI_API_KEY=xxx

MODEL_PROVIDER_ARK_PROVIDER=openai_compatible
MODEL_PROVIDER_ARK_API_KEY=xxx
MODEL_PROVIDER_ARK_API_BASE=https://ark.example.com/v1/chat/completions
```

### 模型分配

```env
MAIN_PROVIDER_NAME=ARK
MAIN_MODEL=your-chat-model
MAIN_FALLBACKS=GEMINI:gemini-2.0-flash

# 以下可选，留空则复用主模型配置
DECISION_PROVIDER_NAME=
DECISION_MODEL=
MODERATION_PROVIDER_NAME=
MODERATION_MODEL=
VISION_PROVIDER_NAME=
VISION_MODEL=
```

### 运行时

```env
BOT_ENABLE_TYPING=true          # 发送前显示「正在输入」
BOT_ENABLE_STREAMING=true       # 流式回复
BOT_INBOUND_DEBOUNCE_SECONDS=5  # 入站消息合并等待时间
BOT_AUTO_DELETE_MINUTES=0       # 自动删除 bot 消息（分钟），0=关闭
BOT_DECISION_CONTEXT_ITEMS=5    # 决策模型可见的历史条数

# 主动话题
BOT_PROACTIVE_DEFAULT_ENABLED=false
BOT_PROACTIVE_IDLE_MINUTES=180
BOT_PROACTIVE_QUIET_HOURS_START=0
BOT_PROACTIVE_QUIET_HOURS_END=9
```

### 数据库

```env
DATABASE_URL=sqlite+aiosqlite:///./data/bot.db
```

完整配置项参见 `.env.example`。

---

## 项目结构

```
Smart_Group_Bot/
├── bot/
│   ├── __main__.py             # 入口（python -m bot）
│   ├── config.py               # 配置加载（.env + config.toml）
│   ├── loader.py               # Bot / Dispatcher 初始化
│   ├── handlers/
│   │   ├── commands.py         # 命令处理（/start /help /lm /task /av 等）
│   │   ├── admin.py            # 管理命令（授权、群规、审核、TTS 等）
│   │   └── group.py            # 群消息主流程（审核→决策→回复）
│   ├── middlewares/
│   │   ├── db.py               # 数据库会话注入
│   │   ├── logging_mw.py       # 日志追踪
│   │   └── throttle.py         # 限流
│   ├── services/
│   │   ├── llm.py              # LLM 调用封装（LiteLLM + fallback）
│   │   ├── decision.py         # 决策引擎
│   │   ├── moderation.py       # 内容审核
│   │   ├── memory.py           # 记忆服务（对话历史 + 上下文压缩）
│   │   ├── memory_holder.py    # 全局记忆持有者
│   │   ├── manage_intent.py    # 管理意图路由
│   │   ├── reply_mode.py       # 回复模式选择（reply / message）
│   │   ├── reply_output.py     # 回复解析与输出
│   │   ├── chat_bridge.py      # bot 间对话
│   │   ├── sticker_decision.py # 贴纸决策模块
│   │   ├── sticker_library.py  # 贴纸学习库
│   │   ├── at_reply.py         # 仅 @ 回复模式
│   │   ├── authz.py            # 权限管理
│   │   ├── av_search.py        # AV 搜索
│   │   ├── doubao_tts.py       # 豆包 TTS 服务
│   │   ├── scheduled_tasks.py  # 定时任务调度
│   │   └── skills/
│   │       ├── service.py      # 技能调度（tool-calling loop）
│   │       ├── base.py         # 技能基类
│   │       ├── memory_manage.py
│   │       ├── rule_manage.py
│   │       ├── task_manage.py
│   │       ├── scheduled_task.py
│   │       ├── send_sticker.py
│   │       ├── websearch.py
│   │       ├── webfetch.py
│   │       ├── music_search.py
│   │       ├── bilibili_search.py
│   │       ├── weibo_search.py
│   │       └── doubao_tts.py
│   ├── db/
│   │   ├── models.py           # ORM 模型
│   │   ├── engine.py           # 数据库引擎
│   │   └── sqlite_session.py   # SQLite 并发处理
│   └── utils/
│       ├── command_catalog.py  # 命令注册表
│       ├── conversation_context.py
│       ├── logging_setup.py    # 日志配置
│       ├── prompts.py          # 提示词加载
│       ├── runtime_context.py  # 运行时上下文构建
│       ├── security.py         # 输入安全处理
│       ├── telegram.py         # Telegram 工具函数
│       └── timezone.py         # 时区工具
├── prompt/                     # 各模块提示词（Markdown）
│   ├── persona.md              # 人设
│   ├── decision.md             # 决策提示词
│   ├── moderation.md           # 审核提示词
│   ├── skill_tools_v2.md       # 技能系统提示词
│   ├── manage_intent.md        # 管理意图路由提示词
│   ├── reply_mode.md           # 回复模式提示词
│   ├── sticker_decision.md     # 贴纸决策提示词
│   ├── chat_bridge.md          # bot 间对话提示词
│   ├── compress.md             # 上下文压缩提示词
│   └── scheduled_task.md       # 定时任务提示词
├── tests/                      # 测试（pytest）
├── config.toml                 # 可选 TOML 配置覆盖
├── .env.example                # 环境变量模板
├── pyproject.toml
├── Dockerfile
├── docker-compose.yml
├── start.py                    # 一键启动脚本
└── start.bat                   # Windows 启动脚本
```

---

## 技术栈

| 组件 | 用途 |
|:---|:---|
| aiogram 3.x | Telegram Bot API |
| LiteLLM | 多供应商 LLM 抽象层 |
| SQLAlchemy 2.x (async) | 数据库 ORM |
| aiosqlite | SQLite 异步驱动 |
| Pydantic v2 | 配置模型 |
| DuckDuckGo (ddgs) | 联网搜索 |
| aiohttp | HTTP 客户端 |

---

## 开源协议

[MIT](LICENSE)

