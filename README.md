# 🤖 Smart Group Bot

<div align="center">

[![Python](https://img.shields.io/badge/Python-3.12+-blue.svg)](https://python.org)
[![License](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)
[![Telegram](https://img.shields.io/badge/Telegram-Bot-26A5E4?logo=telegram)](https://telegram.org)

**基于 LLM 的智能群聊管理机器人**

🎯 智能决策 · 📚 知识库 · 🛡️ 内容审查 · 🔍 联网搜索

</div>

---

## ✨ 功能特性

### 🧠 智能决策系统
- **LLM 驱动决策**：由大模型判断何时回复、回复什么内容
- **三级响应策略**：
  - `skip` - 忽略消息（闲聊场景）
  - `knowledge` - 调用知识库回复（问题场景）
  - `casual` - 自由闲聊（互动场景）
- **强制触发机制**：被 @ 时强制响应

### 📚 知识库系统
- **自然语言录入**：无需复杂命令，直接描述即可添加知识
- **自动向量化**：每小时自动整理和嵌入记忆
- **上下文感知**：基于群聊上下文智能检索相关信息
- **持久化存储**：本地存储，重启不丢失

### 🛡️ 群规与审查
- **动态群规**：支持自然语言添加群规则
- **内容审查**：LLM 实时检测违规内容
- **用户免审**：管理员可按群为指定用户设置 AI 审查豁免
- **命中动作可配置**：
  - `warn`：仅警告
  - `delete`：仅删除违规消息（需管理权限）
  - `ban`：2 次警告，第 3 次自动踢出群组（并删除违规消息）
- **正则支持**：支持正则表达式匹配规则

### 🔍 内置技能
技能接入方式：由主模型通过 tool-calling 自主决定是否调用技能，不再额外走“技能规划模型”。

| 技能 | 说明 |
|------|------|
| `websearch` | 联网搜索实时信息 |
| `webfetch` | 获取网页详细内容 |
| `send_sticker` | 在群里发送贴纸（支持自动学习贴纸库 + 默认贴纸池） |

贴纸学习机制：
- 收到贴纸消息后，机器人会自动记录贴纸 `file_id`、emoji、贴纸包信息，并结合视觉描述写入数据库表 `sticker_library`。
- 后续调用 `send_sticker` 时会优先按语义描述匹配已学习贴纸，实现“边聊边学”的贴纸选择。

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
pip install -r requirements.txt

# 配置环境变量
cp .env.example .env
# 编辑 .env 文件填写配置

# 启动机器人
python main.py
```

### Docker 部署（可选）

```bash
# 构建镜像
docker build -t smart-group-bot .

# 运行
docker run -d --env-file .env --name smart-bot smart-group-bot
```

---

## ⚙️ 配置说明

编辑 `.env` 文件：

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
MODEL_PROVIDER_GEMINI_PROVIDER=gemini
MODEL_PROVIDER_GEMINI_API_KEY=
MODEL_PROVIDER_GEMINI_API_BASE=

MODEL_PROVIDER_ARK_PROVIDER=openai_compatible
MODEL_PROVIDER_ARK_API_KEY=
MODEL_PROVIDER_ARK_API_BASE=

# ---- 主模型 (知识库问答、闲聊) ----
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

# ---- 嵌入模型 (知识库语义搜索) ----
# 留空则复用主模型 provider_name
EMBED_PROVIDER_NAME=
EMBED_MODEL=text-embedding-004
EMBED_FALLBACKS=

# ---- 上下文设置 ----
MAX_CONTEXT_TOKENS=256000
MAX_OUTPUT_TOKENS=64000

# ---- 技能配置 ----
# 贴纸 file_id 列表，逗号分隔（供 send_sticker 默认使用）
# 已学习贴纸会保存到数据库表 sticker_library（首次会自动导入 memory/stickers/<group_id>.json）
SKILL_STICKER_FILE_IDS=

# 数据库（默认 SQLite）
DATABASE_URL=sqlite+aiosqlite:///./data/bot.db
```

日志行会携带流程上下文：
- `流`：单条消息处理链路ID（同一条消息全流程一致）

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

| 命令 | 说明 | 示例 |
|------|------|------|
| `/help` | 显示帮助信息 | `/help` |
| `/kb <内容>` | 添加知识库条目 | `/kb 白菜是 LongEmby 的服主` |
| `/addrule <规则>` | 添加群规 | `/addrule 禁止骂人` |
| `/rules` | 查看当前群规 | `/rules` |
| `/aiexempt` | 回复用户消息，设置 AI 审查豁免（已授权群管理） | `回复某人后发送 /aiexempt` |
| `/unaiexempt` | 回复用户消息，取消 AI 审查豁免（已授权群管理） | `回复某人后发送 /unaiexempt` |
| `/authadmin <群ID> <用户ID>` | 最高管理员授权群管理权限 | `/authadmin -1001234567890 12345678` |

### 自然语言交互

无需记忆命令，直接用自然语言与机器人交互：

```
用户：白菜是谁？
Bot：白菜是 LongEmby 的服主和主理人。

用户：增加一条规则，禁止发广告
Bot：已添加群规：禁止发广告

用户：@Bot 今天的天气怎么样？
Bot：[调用 websearch 查询天气并回复]
```

### 决策提示词示例

机器人内置的决策逻辑：

```
你是一个群聊消息决策器。判断机器人是否应回复，以及回复类型。

输入区块：
- [是否@机器人]：是/否
- [消息类型]
- [知识库标题]
- [知识库条目摘要]
- [消息正文]

决策规则：
1. 如果 [是否@机器人]=是，必须回复
2. 如果消息是"问题"且知识库有答案，输出 knowledge
3. 普通闲聊、寒暄：优先 skip
4. 不要因为"有问号"就一律回复

仅输出一个词（小写）：skip / knowledge / casual
```

---

## 🏗️ 架构设计

```
┌─────────────────────────────────────────┐
│           Telegram Group                │
└──────────────┬──────────────────────────┘
               │
┌──────────────▼──────────────────────────┐
│         Smart Group Bot                 │
│  ┌──────────────┐  ┌──────────────┐    │
│  │   Decision   │  │   Knowledge  │    │
│  │     LLM      │  │     Base     │    │
│  └──────────────┘  └──────────────┘    │
│  ┌──────────────┐  ┌──────────────┐    │
│  │   Content    │  │    Skills    │    │
│  │  Moderation  │  │(websearch...)│    │
│  └──────────────┘  └──────────────┘    │
└─────────────────────────────────────────┘
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

**Made with ❤️ by Hamster-Prime**

[问题反馈](https://github.com/Hamster-Prime/Smart_Group_Bot/issues) · [功能建议](https://github.com/Hamster-Prime/Smart_Group_Bot/discussions)

</div>
