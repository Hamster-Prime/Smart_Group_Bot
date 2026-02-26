# 🤖 Smart Group Bot

<div align="center">

[![Python](https://img.shields.io/badge/Python-3.10+-blue.svg)](https://python.org)
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
- **自动处罚**：
  - 警告提示（3 次机会）
  - 自动删除违规消息（需管理权限）
  - 多次违规自动踢出群组
- **正则支持**：支持正则表达式匹配规则

### 🔍 内置技能
| 技能 | 说明 |
|------|------|
| `websearch` | 联网搜索实时信息 |
| `webfetch` | 获取网页详细内容 |

---

## 🚀 快速开始

### 环境要求
- Python 3.10+
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

# ---- 主模型 (知识库问答、闲聊) ----
# MAIN_PROVIDER: gemini / openai / openai_compatible
MAIN_PROVIDER=
MAIN_MODEL=
MAIN_API_KEY=
# 仅 openai_compatible 需要填写:
MAIN_API_BASE=

# ---- 决策模型 (回复判断、内容审核) ----
# 留空则复用主模型配置
DECISION_PROVIDER=
DECISION_MODEL=
DECISION_API_KEY=
DECISION_API_BASE=

# ---- 压缩模型 (上下文压缩) ----
# 留空则复用主模型配置
COMPRESS_PROVIDER=
COMPRESS_MODEL=
COMPRESS_API_KEY=
COMPRESS_API_BASE=

# ---- 嵌入模型 (知识库语义搜索) ----
EMBED_PROVIDER=
EMBED_MODEL=
EMBED_API_KEY=
EMBED_API_BASE=

# ---- 上下文设置 ----
MAX_CONTEXT_TOKENS=256000
MAX_OUTPUT_TOKENS=64000

# 数据库（默认 SQLite）
DATABASE_URL=sqlite+aiosqlite:///./data/bot.db
```

---

## 📖 使用指南

### 基础命令

| 命令 | 说明 | 示例 |
|------|------|------|
| `/help` | 显示帮助信息 | `/help` |
| `/kb <内容>` | 添加知识库条目 | `/kb 白菜是 LongEmby 的服主` |
| `/addrule <规则>` | 添加群规 | `/addrule 禁止骂人` |
| `/rules` | 查看当前群规 | `/rules` |
| `/aiexempt` | 回复用户消息，设置 AI 审查豁免（管理员） | `回复某人后发送 /aiexempt` |
| `/unaiexempt` | 回复用户消息，取消 AI 审查豁免（管理员） | `回复某人后发送 /unaiexempt` |

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
