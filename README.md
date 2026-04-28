<div align="center">

# 🤖 Smart Group Bot

[![Python](https://img.shields.io/badge/Python-3.12+-3776AB?style=for-the-badge&logo=python&logoColor=white)](https://python.org)
[![License](https://img.shields.io/badge/License-MIT-00C853?style=for-the-badge)](LICENSE)
[![Telegram](https://img.shields.io/badge/Telegram-Bot-26A5E4?style=for-the-badge&logo=telegram&logoColor=white)](https://telegram.org)
[![aiogram](https://img.shields.io/badge/aiogram-3.x-0066CC?style=for-the-badge&logo=aiogram&logoColor=white)](https://docs.aiogram.dev/)
[![LiteLLM](https://img.shields.io/badge/LiteLLM-supported-8B5CF6?style=for-the-badge)](https://docs.litellm.ai/)

**基于 LLM 的 Telegram 群聊智能管理机器人**

🧠 智能决策 · 📚 知识库 RAG · 🛡️ 内容审查 · 🔍 联网搜索 · 🎭 贴纸学习 · 🧬 多层记忆

</div>

---

## ✨ 功能特性

<div align="center">

| 🧠 智能决策 | 📚 知识库 RAG | 🛡️ 内容审查 | 🔍 联网搜索 |
|:---:|:---:|:---:|:---:|
| LLM 判断是否回复 | 语义向量检索 | 实时违规检测 | 实时信息获取 |
| 三级响应策略 | 自然语言录入 | 三级惩罚机制 | 网页内容抓取 |
| 被 @ 强制响应 | 自动整理嵌入 | 正则 + AI 双模式 | 工具自主调用 |

</div>

---

### 🧠 智能决策系统

大模型驱动的三级响应策略，精准控制机器人行为：

| 策略 | 触发条件 | 说明 |
|:---:|:---|:---|
| `skip` | 普通闲聊 / 无关消息 | 忽略，不回复 |
| `question` | 问题 + 需要回答 | 检索知识库或直接回答 |
| `casual` | 需要互动的场景 | 自由闲聊回复 |

- 被 **@** 时强制响应，无需额外配置
- 决策模型可独立配置，支持 fallback 链自动降级

---

### 📚 知识库 (RAG)

```
用户提问 → 向量语义检索 → 上下文匹配 → LLM 生成回答
```

- **自然语言录入** — 直接描述即可添加，无需记忆复杂命令（`/kb` 命令）
- **向量检索** — 基于语义相似度精准匹配
- **自动整理** — 每小时自动嵌入和索引
- **参数可调** — `top_k` / `similarity_threshold` / `enable_relaxed` / `min_reliable_score`

---

### 🧠 永久记忆与上下文压缩

- **永久记忆**：管理员可直接自然语言写入/修改/删除群组永久记忆
- **自动压缩**：上下文达到预算上限后，自动归纳为摘要并持续滚动更新
- **持久化存储**：永久记忆和摘要都写入本地数据库，重启不丢失
- **上下文注入**：回复时自动带入 `[permanent-memory]` 与 `[context-summary]`

---

### 🛡️ 群规与审查

三级惩罚机制，从警告到踢出逐步升级：

```
违规 → warn → warn → ban (第3次自动踢出)
  │                │
  └─ delete ───────┘  (同时删除违规消息)
```

- **动态群规**：支持自然语言添加群规则
- **内容审查**：LLM 实时检测违规内容
- **用户免审**：管理员可按群为指定用户设置 AI 审查豁免
- **命中动作可配置**：
  - `warn`：仅警告
  - `delete`：仅删除违规消息（需管理权限）
  - `ban`：2 次警告，第 3 次自动踢出群组（并删除违规消息）
- **正则支持**：支持正则表达式匹配规则

---

### 🎭 贴纸系统

边聊边学的智能贴纸选择：

- 收到贴纸消息后，机器人会自动记录贴纸 `file_id`、emoji、贴纸包信息，并结合视觉描述写入数据库表 `sticker_library`
- 贴纸回复由独立贴纸决策模块控制，会优先按语义从已学习贴纸中选择，实现"边聊边学"
- 支持默认贴纸池兜底（`SKILL_STICKER_FILE_IDS` 配置）

---

### 🔍 内置技能

技能接入方式：由主模型通过 tool-calling 自主决定是否调用技能，不再额外走"技能规划模型"。

| 技能 | 说明 |
|:---|:---|
| `websearch` | 联网搜索实时信息 |
| `webfetch` | 获取网页详细内容 |
| `kb_search` | 知识库语义检索 |

贴纸发送由独立的**贴纸决策模块**控制（非 tool-calling），收到消息后自动判断是否发送贴纸，并按语义从已学习贴纸库中选择。

---

## 🚀 快速开始

### 环境要求

- Python 3.12+
- Telegram Bot Token
- 大模型 API（支持 GPT/Claude/Gemini 等）

### 安装部署

```bash
# 克隆项目
git clone https://github.com/Hamster-Prime/Smart_Group_Bot.git
cd Smart_Group_Bot

# 安装依赖
pip install -e .

# 配置环境变量
cp .env.example .env
# 编辑 .env 文件填写配置

# 启动机器人
python -m bot
```

### Docker 部署（可选）

```bash
# 使用 docker compose（推荐）
docker compose up -d

# 或手动构建
docker build -t smart-group-bot .
docker run -d --env-file .env --name smart-bot smart-group-bot
```

---

## ⚙️ 配置说明

编辑 `.env` 文件，完整配置参见 `.env.example`：

```env
# Telegram Bot
BOT_TOKEN=
SUPER_ADMIN_ID=

# ---- 日志 ----
LOG_LEVEL=INFO
LOG_THIRD_PARTY_LEVEL=WARNING
LOG_COLOR=on
LOG_TO_FILE=false
LOG_FILE_PATH=bot.log
LOG_FILE_MAX_BYTES=5242880
LOG_FILE_BACKUP_COUNT=3

# ---- 模型供应商池（可配置多个 NAME）----
# If a provider or gateway only accepts streaming requests, set MODEL_PROVIDER_<NAME>_STREAM=true
# MODEL_PROVIDER_<NAME>_API_BASE may point to the full upstream endpoint URL.
# The request format is auto-detected from the API_BASE suffix:
#   .../chat/completions -> OpenAI chat_completions
#   .../responses -> OpenAI responses
#   .../v1/messages -> Anthropic messages
#   .../v1beta/models -> Gemini models
# MODEL_PROVIDER_<NAME>_CHAT_ENDPOINT is only needed as a legacy fallback when API_BASE has no suffix
MODEL_PROVIDER_GEMINI_PROVIDER=gemini
MODEL_PROVIDER_GEMINI_API_KEY=
MODEL_PROVIDER_GEMINI_API_BASE=
MODEL_PROVIDER_GEMINI_STREAM=false

MODEL_PROVIDER_OPENAI_PROVIDER=openai
MODEL_PROVIDER_OPENAI_API_KEY=
MODEL_PROVIDER_OPENAI_API_BASE=
MODEL_PROVIDER_OPENAI_STREAM=false

MODEL_PROVIDER_ARK_PROVIDER=openai_compatible
MODEL_PROVIDER_ARK_API_KEY=
MODEL_PROVIDER_ARK_API_BASE=
MODEL_PROVIDER_ARK_STREAM=false

# ---- 主模型 (聊天、工具调用) ----
MAIN_PROVIDER_NAME=ARK
MAIN_MODEL=
# fallback 格式: provider_name:model,provider_name2:model2
MAIN_FALLBACKS=GEMINI:gemini-2.0-flash

# ---- 决策模型 (回复判断、内容审核) ----
# 留空则复用主模型 provider_name/model
DECISION_PROVIDER_NAME=
DECISION_MODEL=
DECISION_FALLBACKS=

# ---- 审核模型（内容审核）----
# 留空则复用决策模型配置
MODERATION_PROVIDER_NAME=
MODERATION_MODEL=
MODERATION_FALLBACKS=

# ---- 压缩模型 (上下文压缩) ----
# 留空则复用主模型配置
COMPRESS_PROVIDER_NAME=
COMPRESS_MODEL=
COMPRESS_FALLBACKS=

# ---- 嵌入模型 (保留，可选) ----
# 留空则复用主模型 provider_name
EMBED_PROVIDER_NAME=
EMBED_MODEL=text-embedding-004
EMBED_FALLBACKS=

# ---- 知识库检索参数 ----
KNOWLEDGE_TOP_K=3
KNOWLEDGE_SIMILARITY_THRESHOLD=0.55
KNOWLEDGE_ENABLE_FALLBACK=false
KNOWLEDGE_ENABLE_RELAXED=false
KNOWLEDGE_MIN_RELIABLE_SCORE=0.60

# ---- 上下文设置 ----
MAX_CONTEXT_TOKENS=256000
MAX_OUTPUT_TOKENS=64000
BOT_ENABLE_TYPING=true
BOT_ENABLE_STREAMING=true
BOT_STREAM_CHUNK_SIZE=36
BOT_STREAM_EDIT_INTERVAL_SEC=1.0
# Bot 发出的消息在 N 分钟后自动删除，0 表示关闭
BOT_AUTO_DELETE_MINUTES=0
# 决策模型可见的最近上下文条数（0-20，0=不传历史）
BOT_DECISION_CONTEXT_ITEMS=5

# ---- 技能配置 ----
# 贴纸 file_id 列表，逗号分隔（供贴纸决策模块回退使用）
# 已学习贴纸会保存到数据库表 sticker_library（首次会自动导入 memory/stickers/<group_id>.json）
SKILL_STICKER_FILE_IDS=

# 数据库（默认 SQLite）
DATABASE_URL=sqlite+aiosqlite:///./data/bot.db

# ---- Memory v2（多层记忆架构）----
MEMORY_V2_ENABLED=true
MEMORY_WORKING_RECENT_ITEMS=50
MEMORY_VECTOR_BACKEND=qdrant
MEMORY_QDRANT_HOST=localhost
MEMORY_QDRANT_PORT=6333
MEMORY_QDRANT_COLLECTION_PREFIX=chat_memory
MEMORY_HYBRID_TOP_K=20
MEMORY_RETRIEVAL_CANDIDATE_MULTIPLIER=3
MEMORY_SIMILARITY_WEIGHT=0.4
MEMORY_TIME_WEIGHT=0.3
MEMORY_IMPORTANCE_WEIGHT=0.3
MEMORY_TIME_DECAY_FACTOR=0.95
MEMORY_IMPORTANCE_LLM_ENABLED=true
MEMORY_IMPORTANCE_LLM_MIN=0.3
MEMORY_IMPORTANCE_LLM_MAX=0.7
MEMORY_CONSOLIDATION_ENABLED=true
MEMORY_CONSOLIDATION_MIN_IMPORTANCE=0.7
MEMORY_PRUNE_ENABLED=true
MEMORY_PRUNE_DAYS=30
MEMORY_MAX_CONCURRENT_INDEX_TASKS=2
MEMORY_MIGRATE_LEGACY_ON_START=true
MEMORY_LEGACY_MEMORY_DIR=memory
MEMORY_LEGACY_MIGRATION_MARKER=data/memory_v2_legacy_migrated.flag
MEMORY_KG_ENABLED=false
MEMORY_KG_URI=
MEMORY_KG_USER=
MEMORY_KG_PASSWORD=

# ---- AV 搜索 ----
AV_ENABLED=true
AV_HTTP_TIMEOUT_SEC=15
AV_MAX_RESULTS=18
AV_JAVBUS_BASE_URL=https://www.javbus.com
AV_MADOUQU_BASE_URL=https://madouqu.com
```

### 日志配置

日志行会携带流程上下文：
- `流`：单条消息处理链路 ID（同一条消息全流程一致）

日志可选彩色输出：
- `LOG_COLOR=on`：始终彩色
- `LOG_COLOR=off`：关闭彩色
- `LOG_COLOR=auto`：仅在终端支持时彩色

日志可选写入文件（默认关闭）：
- `LOG_TO_FILE=false`：关闭文件输出
- `LOG_TO_FILE=true`：开启文件输出
- `LOG_FILE_PATH=bot.log`：日志文件路径（相对路径默认以项目根目录为基准）
- `LOG_FILE_MAX_BYTES=5242880`：单个日志文件最大字节数（超出后自动轮转）
- `LOG_FILE_BACKUP_COUNT=3`：保留历史轮转文件数量

---

## 📖 使用指南

### 基础命令

| 命令 | 权限 | 说明 |
|:---|:---:|:---|
| `/start` | 所有人 | 开始使用，显示简介 |
| `/help` | 所有人 | 查看完整命令帮助 |
| `/kb <自然语言指令>` | 已授权群管理 | 知识库管理（添加/删除/搜索/列表） |
| `/kb list` | 已授权群管理 | 查看知识库条目列表 |
| `/addrule <规则>` | 已授权群管理 | 添加群规 |
| `/rules` | 所有人 | 查看当前群规 |
| `/warnings` | 所有人 | 查看本群警告/封禁名单 |
| `/aiexempt` | 已授权群管理 | 回复用户消息，设置 AI 审查豁免 |
| `/unaiexempt` | 已授权群管理 | 回复用户消息，取消 AI 审查豁免 |
| `/mute` | 已授权群管理 | 回复用户消息，加入"只审查不回复"名单 |
| `/mute all` | 已授权群管理 | 本群开启"只审查不回复"模式 |
| `/unmute` | 已授权群管理 | 回复用户消息，移出"只审查不回复"名单 |
| `/unmute all` | 已授权群管理 | 本群关闭"只审查不回复"模式 |
| `/av <番号/演员/关键词>` | 需本群启用 | 搜索 JAVBUS + MADOUQU |
| `/av enable` | 最高管理员 | 在当前群启用 AV 查询 |
| `/av disable` | 最高管理员 | 在当前群停用 AV 查询 |
| `/authgroup <群ID>` | 最高管理员 | 授权群组 |
| `/unauthgroup <群ID>` | 最高管理员 | 撤销授权群组 |
| `/authlist` | 最高管理员 | 授权群组列表 |
| `/authadmin <群ID> <用户ID>` | 最高管理员 | 授权群管理 |
| `/unauthadmin <群ID> <用户ID>` | 最高管理员 | 撤销授权群管理 |
| `/adminlist` | 最高管理员 | 群管理列表 |

### 自然语言交互

无需记忆命令，直接用自然语言与机器人交互：

```
管理员：记住"白菜是 LongEmby 的服主和主理人"
Bot：已写入永久记忆 #12。

管理员：把"白菜是 LongEmby 的服主和主理人"改成"白菜是 LongEmby 的主理人"
Bot：已更新永久记忆。

管理员：增加一条规则，禁止发广告
Bot：已添加群规：禁止发广告

用户：@Bot 今天的天气怎么样？
Bot：[调用 websearch 查询天气并回复]
```

### 决策提示词示例

机器人内置的决策逻辑：

```
你是一个群聊消息决策器。判断机器人是否应回复。

输入区块：
- [是否@机器人]：是/否
- [是否回复消息]：是/否
- [是否回复机器人]：是/否
- [是否回复其他用户]：是/否
- [是否@其他用户]：是/否
- [当前发送者是否主人]：是/否
- [当前发送者是否TG群管理员]：是/否
- [消息类型]
- [发送者]
- [消息正文]
- [最近上下文]

决策规则（按优先级）：
1. 若 [是否@其他用户]=是：直接输出 skip
2. 若 [是否回复其他用户]=是：直接输出 skip（除非回复的是机器人）
3. 若 [是否@机器人]=是：必须回复
4. 若 [当前发送者是否主人]=是：不要轻易 skip
5. 若 [最近上下文] 显示群友互聊且无人指向机器人：优先 skip
6. 不要因为"有问号"就一律回复

仅输出一个词（小写）：skip / question / casual
```

---

## 🏗️ 架构设计

```
┌─────────────────────────────────────────────────────────┐
│                    Telegram Group                       │
└────────────────────────┬────────────────────────────────┘
                         │
┌────────────────────────▼────────────────────────────────┐
│                   Smart Group Bot                        │
│                                                          │
│  ┌──────────────┐  ┌──────────────┐  ┌──────────────┐  │
│  │   Decision   │  │  Moderation  │  │   Memory v2  │  │
│  │     LLM      │  │     LLM      │  │(working +    │  │
│  │ skip/q/casual│  │  keyword +   │  │ vector +     │  │
│  │              │  │  regex + llm │  │  knowledge)  │  │
│  └──────┬───────┘  └──────────────┘  └──────────────┘  │
│         │                                                │
│    ┌────▼──────────────────────────────────┐             │
│    │          Skill Service                │             │
│    │  ┌─────────┐ ┌──────────┐ ┌────────┐ │             │
│    │  │websearch│ │ webfetch │ │kb_search│ │             │
│    │  └─────────┘ └──────────┘ └────────┘ │             │
│    │  tool-calling loop (主模型自主决策)     │             │
│    └──────────────────────────────────────┘             │
│                                                          │
│  ┌──────────────┐  ┌──────────────┐                    │
│  │    Casual    │  │   Sticker    │                    │
│  │   Service    │  │   Decision   │                    │
│  │  (闲聊回复)  │  │  (贴纸决策)  │                    │
│  └──────────────┘  └──────────────┘                    │
└─────────────────────────────────────────────────────────┘
```

**消息处理流程：**
1. 消息进入 → 内容审查（keyword/regex/llm 三级检测）
2. 决策模型判断 → `skip` / `question` / `casual`
3. `question` → 知识库检索 → 若 `NO_TRUSTED_ANSWER` → 强制联网搜索降级
4. `casual` → 调用 Skill Service（tool-calling loop，自主选择技能）
5. 贴纸决策模块独立判断是否发送贴纸

---

## 📁 项目结构

```
Smart_Group_Bot/
├── bot/
│   ├── config.py            # 配置加载
│   ├── loader.py            # 应用初始化
│   ├── handlers/            # 消息处理器
│   │   ├── group.py         #   群聊消息（决策+回复主流程）
│   │   ├── admin.py         #   管理操作
│   │   └── commands.py      #   命令处理（/kb /av /rules 等）
│   ├── middlewares/         # 中间件
│   │   ├── throttle.py      #   限流
│   │   ├── db.py            #   数据库会话
│   │   └── logging_mw.py    #   日志追踪
│   ├── services/            # 核心业务
│   │   ├── decision.py      #   决策引擎（skip/question/casual）
│   │   ├── moderation.py    #   内容审查
│   │   ├── casual.py        #   闲聊回复
│   │   ├── knowledge.py     #   知识库管理
│   │   ├── rag.py           #   RAG 管道
│   │   ├── llm.py           #   LLM 调用封装
│   │   ├── memory.py        #   永久记忆
│   │   ├── memory_v2.py     #   多层记忆架构
│   │   ├── memory_holder.py #   记忆持有者
│   │   ├── authz.py         #   权限管理
│   │   ├── av_search.py     #   AV 搜索（JAVBUS/MADOUQU）
│   │   ├── sticker_decision.py #  贴纸决策模块
│   │   ├── sticker_library.py   #  贴纸学习库
│   │   ├── kb_metrics.py    #   知识库指标
│   │   └── skills/          #   可扩展技能
│   │       ├── base.py      #     技能基类
│   │       ├── service.py   #     技能调度服务（tool-calling loop）
│   │       ├── websearch.py #     联网搜索
│   │       ├── webfetch.py  #     网页抓取
│   │       ├── kb_search.py #     知识库检索
│   │       └── send_sticker.py #  贴纸发送
│   ├── db/                  # 数据库模型
│   └── utils/               # 工具函数
├── prompt/                  # 各模块提示词
├── config.toml              # 可选配置覆盖
├── .env.example             # 环境变量模板
├── Dockerfile
├── docker-compose.yml
├── start.py                 # 启动脚本
└── start.bat                # Windows 启动脚本
```

---

## 🤝 贡献指南

欢迎提交 Issue 和 PR！

1. Fork 本项目
2. 创建特性分支：`git checkout -b feature/AmazingFeature`
3. 提交更改：`git commit -m 'Add some AmazingFeature'`
4. 推送分支：`git push origin feature/AmazingFeature`
5. 提交 Pull Request

---

## 📄 开源协议

本项目基于 [MIT](LICENSE) 协议开源。

---

<div align="center">

**Made with ❤️ by [Hamster-Prime](https://github.com/Hamster-Prime)**

[![Issues](https://img.shields.io/github/issues/Hamster-Prime/Smart_Group_Bot?style=for-the-badge)](https://github.com/Hamster-Prime/Smart_Group_Bot/issues)
[![Stars](https://img.shields.io/github/stars/Hamster-Prime/Smart_Group_Bot?style=for-the-badge)](https://github.com/Hamster-Prime/Smart_Group_Bot/stargazers)
[![Forks](https://img.shields.io/github/forks/Hamster-Prime/Smart_Group_Bot?style=for-the-badge)](https://github.com/Hamster-Prime/Smart_Group_Bot/network/members)

[问题反馈](https://github.com/Hamster-Prime/Smart_Group_Bot/issues) · [功能建议](https://github.com/Hamster-Prime/Smart_Group_Bot/discussions)

</div>
