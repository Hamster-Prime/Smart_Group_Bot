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
| `knowledge` | 问题 + 知识库有答案 | 检索知识库后回复 |
| `casual` | 需要互动的场景 | 自由闲聊回复 |

- 被 **@** 时强制响应，无需额外配置
- 决策模型可独立配置，支持 fallback 链自动降级

---

### 📚 知识库 (RAG)

```
用户提问 → 向量语义检索 → 上下文匹配 → LLM 生成回答
```

- **自然语言录入** — 直接描述即可添加，无需记忆复杂命令
- **向量检索** — 基于语义相似度精准匹配
- **自动整理** — 每小时自动嵌入和索引
- **参数可调** — top_k / 相似度阈值 / 宽松模式 / 最低可信分

---

### 🛡️ 内容审查

三级惩罚机制，从警告到踢出逐步升级：

```
违规 → warn → warn → ban (第3次自动踢出)
  │                │
  └─ delete ───────┘  (同时删除违规消息)
```

- **LLM 实时检测** — 内容审核模型独立配置
- **正则 + AI** — 精确匹配 + 语义理解双保险
- **豁免机制** — 管理员可按群为指定用户设置审查豁免
- **命中动作可选**：`warn`（仅警告） / `delete`（删除） / `ban`（累计踢出）

---

### 🎭 贴纸系统

边聊边学的智能贴纸选择：

1. 收到贴纸 → 自动学习（file_id + emoji + 视觉描述）
2. 写入 `sticker_library` 数据库
3. 调用 `send_sticker` 时按语义匹配最佳贴纸
4. 支持默认贴纸池兜底

---

### 🔍 内置技能

由主模型通过 **tool-calling** 自主决定是否调用，无需额外规划层。

| 技能 | 说明 | 调用方式 |
|:---|:---|:---|
| `websearch` | 联网搜索实时信息 | 自动 |
| `webfetch` | 获取网页详细内容 | 自动 |
| `send_sticker` | 语义匹配发送贴纸 | 自动 |

---

### 🧬 多层记忆架构 (Memory v2)

```
┌─────────────────────────────────────────────────┐
│              Memory Architecture                 │
│                                                  │
│  ┌──────────┐   ┌──────────┐   ┌──────────┐    │
│  │  工作记忆  │   │  向量检索  │   │  知识图谱  │   │
│  │  (Recent) │   │ (Qdrant) │   │ (Neo4j)  │    │
│  └────┬─────┘   └────┬─────┘   └────┬─────┘    │
│       │              │              │            │
│       └──────────────┼──────────────┘            │
│                      ▼                           │
│              混合召回 + 重排序                      │
│         (时间衰减 + 重要性加权)                     │
└─────────────────────────────────────────────────┘
```

- **混合召回** — 工作记忆 + 向量检索，多路融合
- **智能加权** — 相似度 × 时间衰减 × 重要性评分
- **自动整合** — 高重要性记忆自动归档
- **过期清理** — 可配置保留天数，自动剪枝

---

### 🔌 多模型供应商

通过 LiteLLM 统一接口，支持几乎所有主流 LLM：

| 角色 | 用途 | 独立配置 |
|:---|:---|:---:|
| `MAIN` | 知识库问答、闲聊、技能调用 | ✅ |
| `DECISION` | 判断是否回复 | ✅ |
| `MODERATION` | 内容审核 | ✅ |
| `COMPRESS` | 上下文压缩 | ✅ |
| `EMBED` | 知识库语义向量化 | ✅ |

每个角色支持独立的 provider、model 和 **fallback 链**。

---

## 🚀 快速开始

### 环境要求

- Python 3.12+
- Telegram Bot Token
- 大模型 API Key（Gemini / OpenAI / 兼容接口均可）

### 方式一：直接运行

```bash
git clone https://github.com/Hamster-Prime/Smart_Group_Bot.git
cd Smart_Group_Bot
pip install -e .
cp .env.example .env
# 编辑 .env 填写配置
python -m bot
```

### 方式二：Docker

```bash
git clone https://github.com/Hamster-Prime/Smart_Group_Bot.git
cd Smart_Group_Bot
cp .env.example .env
# 编辑 .env 填写配置
docker compose up -d
```

---

## ⚙️ 配置说明

完整配置项参见 `.env.example`。

### 最小配置

```env
BOT_TOKEN=your-bot-token
SUPER_ADMIN_ID=your-telegram-id

MODEL_PROVIDER_ARK_PROVIDER=openai_compatible
MODEL_PROVIDER_ARK_API_KEY=your-api-key
MODEL_PROVIDER_ARK_API_BASE=https://your-endpoint/v1

MAIN_PROVIDER_NAME=ARK
MAIN_MODEL=your-model-name
```

### 模型角色配置

每个角色可通过 `*_PROVIDER_NAME` / `*_MODEL` / `*_FALLBACKS` 独立配置：

```env
# 主模型
MAIN_PROVIDER_NAME=ARK
MAIN_MODEL=gpt-4o
MAIN_FALLBACKS=GEMINI:gemini-2.0-flash

# 决策模型（留空则复用主模型）
DECISION_PROVIDER_NAME=
DECISION_MODEL=

# 审核模型（留空则复用决策模型）
MODERATION_PROVIDER_NAME=
MODERATION_MODEL=
```

### 供应商配置

通过 `MODEL_PROVIDER_<NAME>` 前缀定义供应商 profile，支持多个：

```env
MODEL_PROVIDER_GEMINI_PROVIDER=gemini
MODEL_PROVIDER_GEMINI_API_KEY=xxx

MODEL_PROVIDER_ARK_PROVIDER=openai_compatible
MODEL_PROVIDER_ARK_API_KEY=xxx
MODEL_PROVIDER_ARK_API_BASE=https://endpoint/v1
```

---

## 📖 命令参考

| 命令 | 权限 | 说明 |
|:---|:---:|:---|
| `/help` | 所有人 | 显示帮助信息 |
| `/kb <内容>` | 管理员 | 添加知识库条目 |
| `/addrule <规则>` | 管理员 | 添加群规 |
| `/rules` | 所有人 | 查看当前群规 |
| `/aiexempt` | 管理员 | 回复用户，设置 AI 审查豁免 |
| `/unaiexempt` | 管理员 | 回复用户，取消 AI 审查豁免 |
| `/mute` | 管理员 | 回复用户，加入"只审查不回复"名单 |
| `/mute all` | 管理员 | 本群开启"只审查不回复"模式 |
| `/unmute` | 管理员 | 回复用户，移出"只审查不回复"名单 |
| `/unmute all` | 管理员 | 本群关闭"只审查不回复"模式 |
| `/authadmin` | 最高管理员 | `/authadmin <群ID> <用户ID>` 授权群管理 |

> 💡 除了命令，也可以直接用自然语言与机器人交互。

---

## 🏗️ 项目结构

```
Smart_Group_Bot/
├── bot/
│   ├── config.py          # 配置加载
│   ├── loader.py          # 应用初始化
│   ├── handlers/          # 消息处理器
│   │   ├── group.py       #   群聊消息
│   │   ├── admin.py       #   管理操作
│   │   └── commands.py    #   命令处理
│   ├── middlewares/       # 中间件
│   │   ├── throttle.py    #   限流
│   │   ├── db.py          #   数据库会话
│   │   └── logging_mw.py  #   日志追踪
│   ├── services/          # 核心业务
│   │   ├── decision.py    #   决策引擎
│   │   ├── knowledge.py   #   知识库
│   │   ├── moderation.py  #   内容审查
│   │   ├── casual.py      #   闲聊
│   │   ├── memory_v2.py   #   多层记忆
│   │   ├── llm.py         #   LLM 调用
│   │   ├── rag.py         #   RAG 管道
│   │   └── skills/        #   可扩展技能
│   ├── db/                # 数据库模型
│   └── utils/             # 工具函数
├── prompt/                # 各模块提示词
├── config.toml            # 可选配置覆盖
├── .env.example           # 环境变量模板
├── Dockerfile
└── docker-compose.yml
```

---

## 🤝 贡献

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

</div>
