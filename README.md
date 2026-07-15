<div align="center">

# 🤖 Smart Group Bot

[![Python](https://img.shields.io/badge/Python-3.12+-3776AB?style=for-the-badge&logo=python&logoColor=white)](https://python.org)
[![License](https://img.shields.io/badge/License-MIT-00C853?style=for-the-badge)](LICENSE)
[![Telegram](https://img.shields.io/badge/Telegram-Bot-26A5E4?style=for-the-badge&logo=telegram&logoColor=white)](https://telegram.org)
[![aiogram](https://img.shields.io/badge/aiogram-3.x-0066CC?style=for-the-badge&logo=aiogram&logoColor=white)](https://docs.aiogram.dev/)
[![LiteLLM](https://img.shields.io/badge/LiteLLM-supported-8B5CF6?style=for-the-badge)](https://docs.litellm.ai/)

**基于 LLM 的 Telegram 群聊智能机器人**

🧠 智能决策 · 🛡️ 内容审核 · 🔐 入群验证 · 📝 永久记忆 · 🎭 贴纸系统 · 🔍 联网搜索 · 🎵 音乐点播 · 🗣️ TTS 语音

</div>

---

## 架构概览

```
消息进入
  │
  ├─ 全局封禁拦截（外层中间件，命中即删+封）
  │
  ├─ 资料筛查（外层中间件，改名/改简介后自动复查）
  │
  ├─ 内容审核 (keyword / regex / LLM 三级检测，带置信度)
  │     ├─ 高置信度命中 → warn / delete / ban（按规则独立配置）
  │     └─ 低置信度命中 → 删消息 + 可选验证码真人质询
  │
  ├─ 管理意图路由 (manage_intent)
  │     └─ memory_manage / rule_manage → 直接执行
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

项目通过 LiteLLM 接入多供应商，支持 fallback 链自动降级。以下角色均可在设置中心独立选择供应商、模型、推理强度和回退链；未指定的辅助角色自动复用上级模型：

| 角色 | 用途 | 默认模型 |
|:---|:---|:---|
| **main** | 聊天回复、技能工具调用 | 必须配置 |
| **decision** | 判断是否回复（skip / casual） | 复用 main |
| **moderation** | 内容审核 | 复用 decision |
| **vision** | 图片/贴纸理解 | 复用 main |
| **compress** | 上下文压缩摘要、风格蒸馏 | 复用 main |
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

命中动作：`warn`（警告）、`delete`（删除消息）、`ban`（累计 3 次警告后踢出）。支持按用户设置 AI 审核豁免，支持全群「仅审核不回复」模式。管理员可用 `/clearwarnings` 清空某用户的累计违规次数。

**置信度分级**：审核模型输出 0.0–1.0 的置信度。高置信度（默认 ≥0.9）命中直接执行规则动作；低置信度命中不直接处罚，而是删除消息并发起真人质询（可在 Mini App 中切换 Cloudflare Turnstile/hCaptcha）——通过质询即恢复，超时未通过则封禁。审核输出不可解析时按不违规处理且不写入已审核缓存。

**bot 消息审核**：部分广告机借助 Telegram bot（如 guest 模式）在群内发广告，其他 bot 发送的消息同样进入内容审核。每个 bot 在每个群累计通过配置数量（默认 5 条）的干净消息后自动加入白名单，之后不再审核；只有携带可审核内容的消息（文本、caption、可识别图片、文件名、联系人卡片）才计入累计，纯占位媒体（无字幕语音/视频等）既不计数也不调用审核模型。违规会删除消息、清零累计并计入警告，累计达到警告阈值自动封禁该 bot；高置信度命中 ban 规则时立即封禁。群管理员 bot 与人工豁免的 bot 自动跳过。`/unaiexempt`（回复消息或 `/unaiexempt <ID>`）可撤销已获得的白名单。bot 消息只做审核，不进入回复/决策管线，也不发起真人质询（bot 无法完成验证）。注意：要收到其他 bot 的群消息，需在 @BotFather 中为本 bot 开启 **Bot-to-Bot Communication Mode**（Bot API 10.0+）；本 bot 已要求以管理员身份运行，开启该模式后即可收到群内其他 bot 的全部消息。

### 入群验证（Turnstile / hCaptcha 真人质询）

入群验证按群配置：每个群都能单独关闭、开启或继承全局默认，并可分别选择 Cloudflare Turnstile 或 hCaptcha。新成员入群后会先被禁言，需点击群内按钮跳转 bot 私聊，在 Telegram Mini App 内完成该群所选验证码后恢复权限；超时被移出群聊（可重进重试）。身份由 Telegram initData 签名（HMAC bot token）保证。需要 bot 是带「封禁用户」权限的群管理员（启动时自检并警告）。

### 资料筛查与全局封禁

新成员入群时用群规审查其昵称/用户名/简介，命中即自动加入全局封禁名单；此后每条群消息都会复查发送者可见资料（含 `/help`、视频、纯媒体消息），改名无法逃避。全局封禁的用户在任意授权群发言即被删消息并封禁。最高管理员可在 Mini App 或通过 `/ban`、`/unban`、`/banlist` 管理名单；`/unban` 同时清空违规次数并永久豁免资料审查。群管理员可在 Mini App 中维护自己群的本地封禁，不会获得全局封禁名单访问权。

### 永久记忆

管理员通过自然语言或 `/lm` 命令维护群组永久记忆。回复时自动注入 `[permanent-memory]` 上下文。支持列表翻页和内联按钮删除。存储于 SQLite，重启不丢失。

### 主动话题

群组长时间沉默后，bot 可自动抛出一个结合群记忆的话题。通过 `/proactive on|off|status` 控制，支持静默时段配置。

### 说话风格模仿

管理员回复目标用户消息后发送 `/mimic`，bot 开始采样该用户的群消息（滚动窗口 200 条、总上限 1000 条），每约 50 条由 compress 模型蒸馏一次风格画像，并以 `[SPEECH_STYLE_PROFILE]` 注入回复提示词。`/mimic status` 查看进度，`/mimic off` 停止并清理样本。

### 技能系统 (Tool Calling)

主模型通过 function calling 自主决定调用哪些技能，无独立技能规划模型：

| 技能 | 说明 |
|:---|:---|
| `memory_manage` | 查看/新增/修改永久记忆（删除走 /lm） |
| `rule_manage` | 查看/新增群规（删除走 /rules） |
| `send_sticker` | 语义匹配发送贴纸 |
| `websearch` | DuckDuckGo 联网搜索 |
| `webfetch` | 抓取网页正文内容 |
| `music_search` | GD Studio 音乐 API：搜索、点播、歌词、专辑封面 |
| `bilibili_search` | B站视频/UP主搜索、热门、排行榜 |
| `weibo_search` | 微博热搜、内容搜索、Feed 流 |
| `sub2api_query` | Sub2API 网关查询：模型列表、模型测活（需配置） |
| `doubao_tts` | 豆包 TTS 语音合成（需配置） |

### 贴纸系统

收到贴纸后自动记录 file_id、emoji、贴纸包和视觉描述到 `sticker_library` 表。回复时由独立的贴纸决策模块判断是否发送，优先按语义从已学习贴纸中选择，支持默认贴纸池兜底。

### AV 查询

支持按番号直查、按演员/关键词搜索，来源覆盖 JAVBUS / MADOUQU / DMM / FC2。每群独立开关（`/av enable|disable`），支持内联翻页浏览详情和种子。

### 日志

日志行携带流程上下文（trace_id），支持彩色输出和文件轮转。应用级别、第三方库级别、颜色、文件路径、轮转大小和保留数量均在设置中心管理。

---

## 命令参考

### 核心入口

| 命令 | 说明 |
|:---|:---|
| `/help` | 查看完整帮助 |
| `/settings` | 最高管理员打开可视化设置中心 |
| `/lm` | 永久记忆列表（翻页 + 内联删除） |
| `/lm add <内容>` | 新增永久记忆 |
| `/lm replace <#ID或关键词> => <新内容>` | 修改永久记忆 |
| `/addrule <自然语言>` | 新增群规 |
| `/rules` | 群规列表（翻页 + 内联删除） |
| `/av <番号/演员/关键词>` | 搜索 AV 资源 |

### 群审核管理（需已授权群管理）

| 命令 | 说明 |
|:---|:---|
| `/warnings` | 查看警告/封禁名单 |
| `/clearwarnings [用户ID]` | 清空某用户的累计违规次数（也可回复消息） |
| `/aiexempt` | 回复用户消息后豁免其 AI 审核（bot 亦可豁免） |
| `/unaiexempt` | 回复用户消息后取消审核豁免；对 bot 同时撤销其审核白名单 |
| `/mute` | 回复用户消息后忽略其后续回复 |
| `/mute all` | 全群仅审核，不再回复 |
| `/unmute` | 回复用户消息后恢复其回复 |
| `/unmute all` | 恢复全群正常回复 |
| `/proactive on\|off\|status` | 主动话题开关/状态 |
| `/mimic [status\|off]` | 回复用户后学习其说话风格 |

### 最高管理员命令

| 命令 | 说明 |
|:---|:---|
| `/authgroup [群ID]` | 授权群组 |
| `/unauthgroup [群ID]` | 撤销群组授权 |
| `/authlist` | 授权群组列表 |
| `/ban [用户ID] [原因]` | 全局封禁（也可回复消息） |
| `/unban [用户ID]` | 解封 + 清零违规 + 豁免资料审查 |
| `/banlist` | 查看封禁名单 |
| `/authadmin [群ID] [用户ID]` | 授权群管理 |
| `/unauthadmin [群ID] [用户ID]` | 撤销群管理 |
| `/adminlist [群ID]` | 群管理列表 |
| `/atreply [enable\|disable]` | 仅 @ 才回复模式 |
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
# 编辑 .env，只填写启动所需的 BOT_TOKEN、SUPER_ADMIN_ID、
# CONFIG_MASTER_KEY、DATABASE_URL、MINIAPP_*，以及可选的 WEBHOOK_* 配置

# 启动
python start.py
```

### Docker

```bash
docker compose up -d
```

---

## 配置说明

启动后，最高管理员私聊 bot 发送 `/settings`，在 Telegram Mini App 中配置：

- 模型供应商、API 密钥、角色模型、fallback、推理等级和重试参数
- Bot 回复、流式输出、上下文、主动话题和按消息类别、秒数配置的自动删除策略
- 审核、Cloudflare Turnstile/hCaptcha、TTS、音乐、Sub2API、AV 和贴纸池
- 日志输出、全部 LLM 提示词，以及每个授权群的功能开关、群规、永久记忆、警告/封禁、审核豁免、回复静默名单和说话风格画像

配置保存在数据库中并对后续请求热生效。第三方密钥使用 `CONFIG_MASTER_KEY` 加密，API 只返回“已配置”状态，不回传明文。全局配置使用 revision 防止多个页面互相覆盖。

被授权的群管理员也可在私聊中发送 `/settings`。其 Mini App 只返回自己负责的群组和群级资源，无法读取其他群组、全局运行配置、密钥、授权关系或全局封禁名单。

`.env` 只保留无法由 Mini App 自举的项目：

```env
BOT_TOKEN=
SUPER_ADMIN_ID=
CONFIG_MASTER_KEY=
DATABASE_URL=sqlite+aiosqlite:///./data/bot.db
MINIAPP_PUBLIC_BASE_URL=https://bot.example.com
MINIAPP_LISTEN_HOST=0.0.0.0
MINIAPP_LISTEN_PORT=8480
WEBHOOK_URL=https://bot.example.com/telegram/webhook
WEBHOOK_SECRET=replace_with_at_least_32_random_characters
```

`CONFIG_MASTER_KEY` 可用 `openssl rand -hex 32` 生成，并需随数据库备份一起妥善保管。

配置 `WEBHOOK_URL` 和 `WEBHOOK_SECRET` 后，Telegram 更新会通过现有 Mini App HTTP 监听器接收。反向代理需要把 `WEBHOOK_URL` 的完整路径转发到 `MINIAPP_LISTEN_HOST:MINIAPP_LISTEN_PORT`；密钥需为 32-256 位字母、数字、下划线或连字符，可用 `openssl rand -hex 32` 生成。启动时会从 bot 所在主机探测该公网地址，运行中也会监控 Telegram 投递错误。Webhook 未配置、自检失败、URL/密钥格式错误、Telegram 注册失败或投递异常时，bot 会在日志中写明原因，清理远端 webhook，并自动降级为长轮询；运行中降级会保留已经积压的更新。

升级旧版本时，如果数据库尚无运行时配置，现有 `.env` 和 `config.toml` 业务项会导入一次；创建数据库配置记录后，后续启动不再用文件覆盖 Mini App 设置。确认设置中心内容无误后，可清理旧文件中的模型、功能和第三方密钥值；Docker 部署保留空的 `config.toml` 占位即可。

---

## 项目结构

```
Smart_Group_Bot/
├── bot/
│   ├── __main__.py             # 入口（python -m bot）
│   ├── config.py               # 启动配置和模型配置类型
│   ├── loader.py               # Bot / Dispatcher 初始化
│   ├── handlers/
│   │   ├── commands.py         # 命令处理（/start /help /lm /av 等）
│   │   ├── admin.py            # 管理命令（授权、群规、审核、封禁、TTS 等）
│   │   ├── membership.py       # 入群/离群事件（筛查、验证）
│   │   └── group.py            # 群消息主流程（审核→决策→回复）
│   ├── middlewares/
│   │   ├── db.py               # 数据库会话注入
│   │   ├── logging_mw.py       # 日志追踪
│   │   ├── global_ban.py       # 全局封禁拦截（外层）
│   │   └── profile_screen.py   # 发言时资料复查（外层）
│   ├── services/
│   │   ├── llm.py              # LLM 调用封装（LiteLLM + fallback + 流式合并）
│   │   ├── decision.py         # 决策引擎
│   │   ├── moderation.py       # 内容审核（置信度分级）
│   │   ├── memory.py           # 记忆服务（对话历史 + 上下文压缩）
│   │   ├── memory_holder.py    # 全局记忆持有者
│   │   ├── manage_intent.py    # 管理意图路由
│   │   ├── reply_mode.py       # 回复模式选择（reply / message）
│   │   ├── reply_output.py     # 回复解析与输出
│   │   ├── proactive.py        # 主动话题
│   │   ├── join_screening.py   # 入群资料筛查 + 全局封禁
│   │   ├── join_verification.py# 入群验证 / 审核质询（Turnstile / hCaptcha）
│   │   ├── verify_web.py       # 内置验证页服务（aiohttp + Mini App）
│   │   ├── runtime_config.py   # 数据库运行时配置、加密和热应用
│   │   ├── speech_style.py     # 说话风格模仿（/mimic）
│   │   ├── admin_status.py     # 管理员身份缓存（仅缓存非管理员结果）
│   │   ├── sticker_decision.py # 贴纸决策模块
│   │   ├── sticker_library.py  # 贴纸学习库
│   │   ├── at_reply.py         # 仅 @ 回复模式
│   │   ├── authz.py            # 权限管理
│   │   ├── av_search.py        # AV 搜索
│   │   ├── doubao_tts.py       # 豆包 TTS 服务
│   │   └── skills/
│   │       ├── service.py      # 技能调度（tool-calling loop）
│   │       ├── base.py         # 技能基类
│   │       ├── memory_manage.py
│   │       ├── rule_manage.py
│   │       ├── send_sticker.py
│   │       ├── websearch.py
│   │       ├── webfetch.py
│   │       ├── music_search.py
│   │       ├── bilibili_search.py
│   │       ├── weibo_search.py
│   │       ├── sub2api_query.py
│   │       └── doubao_tts.py
│   ├── db/
│   │   ├── models.py           # ORM 模型
│   │   ├── engine.py           # 数据库引擎
│   │   └── sqlite_session.py   # SQLite 并发处理
│   ├── utils/
│   │   ├── bot_identity.py     # 运行时 bot 身份块
│   │   ├── command_catalog.py  # 命令注册表
│   │   ├── conversation_context.py
│   │   ├── logging_setup.py    # 日志配置
│   │   ├── prompts.py          # 提示词加载
│   │   ├── runtime_context.py  # 运行时上下文构建
│   │   ├── security.py         # 输入安全处理
│   │   ├── telegram.py         # Telegram 工具函数
│   │   └── timezone.py         # 时区工具
│   └── web/                    # 设置 Mini App 页面、鉴权和 API
├── prompt/                     # 各模块提示词（Markdown）
│   ├── persona.md              # 人设
│   ├── decision.md             # 决策提示词
│   ├── moderation.md           # 审核提示词（含置信度）
│   ├── skill_tools_v2.md       # 技能系统提示词
│   ├── manage_intent.md        # 管理意图路由提示词
│   ├── reply_mode.md           # 回复模式提示词
│   ├── sticker_decision.md     # 贴纸决策提示词
│   ├── proactive_topic.md      # 主动话题提示词
│   ├── style_distill.md        # 风格蒸馏提示词
│   └── compress.md             # 上下文压缩提示词
├── tests/                      # 测试（pytest）
├── config.toml                 # 旧版本一次性迁移输入，导入后忽略
├── .env.example                # 最小启动配置模板
├── pyproject.toml
├── Dockerfile
├── docker-compose.yml
└── start.py                    # 一键启动脚本
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
| cryptography | 数据库密钥加密 |
| DuckDuckGo (ddgs) | 联网搜索 |
| aiohttp | HTTP 客户端 |

---

## 开源协议

[MIT](LICENSE)
