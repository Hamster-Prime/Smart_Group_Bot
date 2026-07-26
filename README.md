<h1 align="center">🤖 Smart Group Bot</h1>

<p align="center">
  <img src="https://img.shields.io/badge/Python-3.12+-blue.svg" alt="Python Version">
  <img src="https://img.shields.io/badge/License-MIT-green.svg" alt="License">
  <img src="https://img.shields.io/badge/aiogram-3.x-0066CC.svg" alt="aiogram">
  <img src="https://img.shields.io/badge/LiteLLM-multi--provider-8B5CF6.svg" alt="LiteLLM">
  <a href="https://github.com/Hamster-Prime/Smart_Group_Bot/stargazers">
    <img src="https://img.shields.io/github/stars/Hamster-Prime/Smart_Group_Bot.svg?style=social&label=Star" alt="GitHub Stars">
  </a>
</p>

> 一个由大模型驱动的 Telegram 群聊智能机器人，把「聊天陪伴」和「群组治理」合并进同一条消息管线：既能自然地参与群聊、调用技能查资料，也能完成内容审核、入群验证、爆破防护和民主投票封禁。全部运行配置在 Telegram Mini App 内可视化完成。

---

## 📜 目录

- [✨ 核心特性](#-核心特性)
- [🧭 架构概览](#-架构概览)
- [🚀 快速开始 (Docker 推荐)](#-快速开始-docker-推荐)
- [🛠️ 手动部署](#️-手动部署)
- [📖 使用指南](#-使用指南)
- [⚙️ 设置中心 (Mini App)](#️-设置中心-mini-app)
- [🧠 智能对话与模型](#-智能对话与模型)
- [🛡️ 内容审核](#️-内容审核)
- [🔐 入群验证](#-入群验证)
- [🕵️ 资料筛查与自动巡检](#️-资料筛查与自动巡检)
- [🚨 爆破防护](#-爆破防护)
- [🗳️ 民主投票封禁](#️-民主投票封禁)
- [📣 呼叫管理员](#-呼叫管理员)
- [⌨️ 关键词回复与定时消息](#️-关键词回复与定时消息)
- [👋 入群欢迎语](#-入群欢迎语)
- [🕰️ 群权限与夜间模式](#️-群权限与夜间模式)
- [🧰 技能系统](#-技能系统)
- [📝 永久记忆与主动话题](#-永久记忆与主动话题)
- [🔧 配置说明](#-配置说明)
- [📂 项目结构](#-项目结构)
- [🤝 贡献指南](#-贡献指南)
- [📄 许可证](#-许可证)
- [🙏 致谢](#-致谢)

---

## ✨ 核心特性

| 特性 | 描述 |
| :--- | :--- |
| 🧠 **智能决策回复** | 独立的决策模型先判断「该不该说话」：被 @ 或被回复时必答，群友互聊时保持安静，避免机器人刷屏。 |
| 🔌 **多供应商大模型** | 基于 LiteLLM 接入 OpenAI / Anthropic / Gemini / 任意 OpenAI 兼容网关；main、decision、moderation、vision、compress、embed 六种角色分别选型，并支持 fallback 链自动降级。 |
| 🛡️ **三级内容审核** | 关键词、正则、LLM 语义三种规则类型，每条规则独立配置 warn / delete / ban 动作，并按置信度分级处置。 |
| 🤖 **Bot 广告机审核** | 其他 bot 发的群消息同样进入审核，累计 5 条干净消息后自动加入白名单，违规则删除并计入警告。 |
| 🔐 **入群人机验证** | 新成员先禁言，再在 Telegram Mini App 内完成 Cloudflare Turnstile / hCaptcha（可双重）验证后恢复权限。 |
| 🕵️ **资料筛查与全局封禁** | 入群和每日巡检时用群规审查昵称、用户名和简介，命中即加入全局封禁名单。 |
| 🔁 **每日自动巡检** | 每天定时分批复查全群成员资料（默认 04:30、每批 500 人），违规者禁言并发起真人质询。 |
| 🚨 **爆破防护** | 短窗口内批量入群（默认 60 秒 8 人）自动锁群，追溯质询近期加入者，锁定到期主动解除。 |
| 🗳️ **民主投票封禁** | 回复骚扰消息发起 `/voteban`，达到票数即封禁；管理员可提前终止或直接封禁，全程写入审计账本。 |
| 📣 **呼叫管理员** | 群成员发送 `@admin` 一键真实 @ 提及管理员，支持冷却时间与逐群指定目标。 |
| ⌨️ **关键词与定时消息** | 逐群配置关键词触发回复（包含 / 完全 / 正则）与定时群发（每日定时或固定间隔），均支持自定义内联按钮与自动置顶。 |
| 🕰️ **群权限与夜间模式** | 完整编辑 Telegram `ChatPermissions`，并可配置跨午夜、按星期、带优先级的定时权限时段。 |
| 🧰 **技能系统** | 主模型通过 function calling 自主调用 14 个技能：联网搜索、网页抓取、音乐点播、影视信息、B 站 / 微博、mihomo 与 RouterOS 官方文档等。 |
| 📝 **永久记忆** | 群组级长期记忆写入数据库，回复时自动注入上下文，重启不丢失；支持翻页与内联删除。 |
| 🎭 **贴纸与语音** | 自动学习群内贴纸并语义匹配发送；集成豆包 TTS 语音合成，支持三档语音模式。 |
| 📱 **Mini App 设置中心** | 全部运行配置在 Telegram 内的可视化面板完成，密钥加密入库，`.env` 只保留启动引导项。 |

---

## 🧭 架构概览

```text
消息进入
  │
  ├─ 更新去重 + 持久化 inbox（webhook 崩溃不丢更新）
  │
  ├─ 全局封禁拦截（外层中间件，命中即删消息 + 封禁）
  │
  ├─ 验证闸门（存在未完成验证记录 → 直接删除抢跑消息）
  │
  ├─ 成员名单维护（供每日自动巡检使用）
  │
  ├─ 内容审核（keyword / regex / llm 三级检测，带置信度）
  │     ├─ 高置信度命中 → warn / delete / ban（按规则独立配置）
  │     └─ 低置信度命中 → 删消息 + 可选真人质询
  │
  ├─ 管理意图路由（manage_intent）
  │     └─ memory_manage / rule_manage → 直接执行
  │
  ├─ 决策模型（decision）
  │     ├─ skip → 结束
  │     └─ casual → 进入回复流程
  │
  ├─ 回复流程（skill tool-calling loop）
  │     ├─ 主模型自主选择并调用技能
  │     ├─ 贴纸决策模块独立判断是否发送贴纸
  │     └─ 回复模式选择（reply / message）
  │
  └─ 输出（流式编辑 + 按类别自动删除 / 内联删除按钮）
```

后台常驻任务：Telegram 更新投递、消息清理监控、主动话题、验证扫描、资料巡检、定时消息、群权限调度、资源健康看门狗、停机看门狗。

---

## 🚀 快速开始 (Docker 推荐)

> [!TIP]\
> 强烈推荐使用 Docker 部署，可以免去 Python 环境与依赖版本的麻烦。镜像已锁定全部直接与间接依赖版本。

1. 克隆项目并进入目录

```bash
git clone https://github.com/Hamster-Prime/Smart_Group_Bot.git
cd Smart_Group_Bot
```

2. 从模板创建 `.env` 配置文件

```bash
cp .env.example .env
```

3. 生成主密钥并编辑配置

```bash
openssl rand -hex 32   # 把输出填入 CONFIG_MASTER_KEY
nano .env
```

<details>
<summary>📝 .env 文件配置示例 (点击展开)</summary>

`.env` 只保留**无法由 Mini App 自举**的启动引导项，其余全部配置在 Telegram 内完成：

```env
# --- 必需配置 ---

# Telegram Bot Token，从 @BotFather 获取
BOT_TOKEN=

# 最高管理员的 Telegram 用户 ID（必须为正整数）
# 这是打开设置中心的唯一「破窗」身份，请填自己的账号 ID
SUPER_ADMIN_ID=

# 主密钥：用于加密数据库中的供应商密钥、验证码密钥、TTS 与网关凭据
# 用 openssl rand -hex 32 生成，且必须在重启和备份之间保持不变
# 一旦丢失，数据库里所有已加密的第三方密钥都将无法解密
CONFIG_MASTER_KEY=

# --- 数据库 ---
# 数据库位置需要在读取数据库配置之前就确定，因此保留在 .env 中
DATABASE_URL=sqlite+aiosqlite:///./data/bot.db

# --- Mini App 服务 ---

# 对外可访问的 HTTPS origin，必须反代到下面的监听地址
# Telegram 会在 Mini App 内打开 ${MINIAPP_PUBLIC_BASE_URL}/settings 和 /verify
MINIAPP_PUBLIC_BASE_URL=https://bot.example.com
MINIAPP_LISTEN_HOST=0.0.0.0
MINIAPP_LISTEN_PORT=8480

# --- 可选：Telegram Webhook 传输 ---
# 两项都填写才会启用；留空或校验失败会自动降级为长轮询
# WEBHOOK_URL 的完整路径需由反代转发到上面的监听端口，长度不超过 256 字符
# WEBHOOK_SECRET 需为 32-256 位的字母、数字、下划线或连字符
# 同样可用 openssl rand -hex 32 生成
WEBHOOK_URL=
WEBHOOK_SECRET=
```

> [!IMPORTANT]\
> `CONFIG_MASTER_KEY` 必须随数据库一起备份。它是数据库内全部第三方密钥的解密钥匙，更换后已保存的密钥将全部失效。

</details>

4. 启动容器

```bash
APP_UID="$(id -u)" APP_GID="$(id -g)" docker compose up -d --build
```

> **参数解析：**
> - `APP_UID` / `APP_GID`：容器内进程的 uid / gid，应与宿主机 `./data` 目录所有者一致，容器不会以 root 运行。
> - Compose 默认只把后端端口绑定到 `127.0.0.1:8480`，适合同机 HTTPS 反向代理；确需让其他主机或容器直接访问时，才显式设置 `MINIAPP_BIND_ADDRESS=0.0.0.0` 并配置防火墙。
> - 默认资源限制：内存 1536m、CPU 2.0、进程数 128，可分别用 `BOT_MEMORY_LIMIT`、`BOT_CPU_LIMIT`、`BOT_PIDS_LIMIT` 覆盖。
> - `stop_grace_period` 为 **125 秒**，略高于应用内 110 秒的有序停机硬上限，确保在途更新、LLM 回复与 Telegram 清理任务能有界落盘。

5. 查看日志并更新

```bash
# 查看运行日志
docker compose logs -f

# 更新到最新代码
git pull
docker compose down
docker compose up -d --build
```

---

## 🛠️ 手动部署

如果不使用 Docker，也可以直接在宿主机运行。

#### 1. 克隆项目

```bash
git clone https://github.com/Hamster-Prime/Smart_Group_Bot.git
cd Smart_Group_Bot
```

#### 2. 安装依赖

```bash
# 建议创建并激活虚拟环境
python -m venv .venv
source .venv/bin/activate   # Linux / macOS
# .venv\Scripts\activate    # Windows

pip install -e .
```

#### 3. 配置环境变量

```bash
cp .env.example .env
nano .env
```

#### 4. 启动

```bash
python start.py
```

`start.py` 会自动完成一键引导：检测并安装缺失依赖、在 `.env` 不存在时从模板创建并收紧为 `0600` 权限、校验启动配置、创建 `data/` 目录，然后启动机器人。也可以直接用 `python -m bot` 跳过引导。

> [!NOTE]\
> 依赖锁定在 `uv.lock` 与由其导出的 `requirements.lock` 中，两者都纳入版本控制，Docker 镜像只安装锁定版本。修改依赖后请重新导出：
> ```bash
> uv lock
> uv export --frozen --no-dev --no-emit-project --no-hashes \
>   --no-annotate --no-header --output-file requirements.lock
> ```

---

## 📖 使用指南

### 🔑 获取必要信息

1. **Bot Token**：在 Telegram 中与 [@BotFather](https://t.me/BotFather) 对话，用 `/newbot` 创建机器人获得。
2. **你的用户 ID**：填入 `SUPER_ADMIN_ID`，这是打开设置中心的唯一身份。
3. **大模型 API 密钥**：至少准备一个可用的供应商（OpenAI / Anthropic / Gemini / 任意 OpenAI 兼容网关），启动后在 Mini App 内填写。
4. **公网 HTTPS 地址**：Mini App 与验证页必须通过 HTTPS 暴露，把 `MINIAPP_PUBLIC_BASE_URL` 反代到 `MINIAPP_LISTEN_HOST:MINIAPP_LISTEN_PORT`。
5. **人机验证密钥**（启用入群验证 / 巡检时必需）：在 [Cloudflare Turnstile](https://dash.cloudflare.com/?to=/:account/turnstile) 或 [hCaptcha](https://dashboard.hcaptcha.com/) 申请 Site Key 与 Secret Key。

> [!IMPORTANT]\
> **必须把机器人设为群管理员**，且至少授予「封禁用户」权限；置顶消息、删除消息、限制成员等能力同样依赖管理员权限。启动时会自检并在日志中告警。
>
> 若要审核**其他 bot** 发送的群消息，需在 @BotFather 中为本 bot 开启 **Bot-to-Bot Communication Mode**（Bot API 10.0+）。

### 📜 命令列表

#### 所有成员可用

| 命令 | 说明 |
| :--- | :--- |
| `/start` | 启动机器人，显示欢迎信息 |
| `/help` | 查看完整命令帮助 |
| `/settings` | 打开可视化设置中心（最高管理员与已授权群管理员可用） |
| `/lm` | 查看永久记忆列表（翻页 + 内联删除） |
| `/lm add <内容>` | 新增一条永久记忆 |
| `/lm replace <#ID 或关键词> => <新内容>` | 修改已有永久记忆 |
| `/addrule <自然语言>` | 新增群规 |
| `/rules` | 查看群规列表（翻页 + 内联删除） |
| `/av <番号 / 演员 / 关键词>` | 搜索影片资源（需本群已启用） |
| `/voteban [举报理由]` | 回复目标消息后发起民主投票封禁（受用户额度限制） |
| `@admin [说明]` | 呼叫群管理员，可回复某条消息进行举报 |

#### 群管理员 / 已授权群管理

| 命令 | 说明 |
| :--- | :--- |
| `/warnings` | 查看本群警告 / 封禁名单 |
| `/clearwarnings [用户ID]` | 清空某用户累计违规次数（也可回复消息使用） |
| `/ban [用户ID] [原因]` | 本群封禁；最高管理员会收到「仅本群 / 全局」内联选择 |
| `/spam [用户ID] [原因]` | 封禁垃圾账号并加入全局封禁名单；回复使用时同时删除该垃圾消息 |
| `/unban [用户ID]` | 本群解封；最高管理员可选择全局范围 |
| `/raidguard on [分钟]｜off｜status` | 手动开启、限时开启或解除爆破防护（数字单位为分钟） |
| `/aiexempt` | 回复用户消息后豁免其 AI 审核（对 bot 同样有效） |
| `/unaiexempt` | 取消审核豁免；对 bot 同时撤销其审核白名单 |
| `/mute` | 回复用户消息后忽略其后续回复 |
| `/mute all` | 全群仅做审核，不再回复 |
| `/unmute`｜`/unmute all` | 恢复单个用户 / 全群的正常回复 |
| `/proactive on｜off｜status` | 主动话题开关与状态 |
| `/mimic [status｜off]` | 回复用户后学习其说话风格 |
| `/compact` | 立即把本群临时对话历史压缩进背景摘要 |

#### 最高管理员命令

| 命令 | 说明 |
| :--- | :--- |
| `/authgroup [群ID]` | 授权群组（群内可直接使用） |
| `/unauthgroup [群ID]` | 撤销群组授权 |
| `/authlist` | 查看授权群组列表 |
| `/authadmin [群ID] [用户ID]` | 授权群管理员（也可回复用户消息） |
| `/unauthadmin [群ID] [用户ID]` | 撤销群管理员权限 |
| `/adminlist [群ID]` | 查看群管理列表 |
| `/banlist` | 查看全局封禁名单 |
| `/atreply [enable｜disable]` | 仅 @ 才回复模式 |
| `/tts [enable｜disable｜always]` | TTS 语音模式（智能 / 关闭 / 始终语音） |
| `/av enable｜disable` | 逐群开关影片查询 |

> [!TIP]\
> 命令别名同样可用：`/raid` = `/raidguard`，`/clearwarning`、`/clearwarns`、`/clearwarn` = `/clearwarnings`。更多细节请查看下方各功能模块的展开说明。

---

## ⚙️ 设置中心 (Mini App)

启动后，最高管理员在**私聊**中向 bot 发送 `/settings`，即可在 Telegram Mini App 内可视化配置全部运行参数。这是本项目的核心配置方式——`.env` 只负责启动引导，其余一切都在这里完成。

#### 功能特点

- **十个配置分区**：概览、模型、Prompts、Bot 行为、审核验证、媒体能力、外部服务、群组设置、权限封禁、日志
- **逐群独立配置**：每个授权群有自己的十个配置小节，互不干扰
- **密钥加密入库**：第三方密钥用 `CONFIG_MASTER_KEY` 加密存储，API 只返回「已配置」状态，绝不回传明文
- **热生效**：保存即对后续请求生效，无需重启
- **乐观锁**：全局配置使用 revision 机制，防止多个页面互相覆盖
- **分级可见**：被授权的群管理员也能用 `/settings`，但只能看到自己负责的群

<details>
<summary>📝 更多详细说明 (点击展开)</summary>

#### 全局配置分区

| 分区 | 可配置内容 |
| :--- | :--- |
| **概览** | 运行状态与启动参数一览 |
| **模型** | 供应商、API 密钥、六种角色模型、fallback 回退链、推理强度、超时与重试参数 |
| **Prompts** | 全部模块的系统提示词，可直接在线编辑 |
| **Bot 行为** | 消息处理、流式输出、上下文长度、主动话题、按类别配置的自动删除策略 |
| **审核验证** | 内容审核开关与阈值、Cloudflare Turnstile / hCaptcha、每日巡检时间与批大小、质询超时 |
| **媒体能力** | TTS 语音、音乐点播、影片查询、贴纸池 |
| **外部服务** | 影视信息服务（TMDB / IMDb）等第三方凭据接入 |
| **群组设置** | 逐群的全部行为开关与自动化配置 |
| **权限封禁** | 群授权、群管理员任免、全局封禁名单 |
| **日志** | 应用与第三方库日志级别、彩色输出、文件路径、轮转大小与保留数量 |

#### 每个群的十个配置小节

回复与媒体、模型 API、入群欢迎、成员权限、安全防护、管理投票、主动与风格、关键词与定时、群规与记忆、成员名单。

#### 自动删除类别

支持按消息类别独立配置清理策略，共 10 个类别：`reply`（回复）、`management`（管理）、`moderation`（审核）、`media`（媒体）、`proactive`（主动话题）、`keyword`（关键词回复）、`scheduled`（定时消息）、`welcome`（欢迎语）、`call_admin`（呼叫管理员）、`vote`（民主投票）。

每个类别可二选一：

- **定时自动删除**：单独设置秒数，留空则继承全局秒数
- **内联删除按钮**：在消息上附带删除按钮，仅群管理员可点击

默认启用自动删除的类别为 `management` 和 `moderation`。

#### 权限边界

被授权的群管理员打开的 Mini App **只返回自己负责的群组和群级资源**，无法读取其他群组、全局运行配置、任何密钥、授权关系或全局封禁名单。`/banlist` 与全局名单页面仅最高管理员可见。

#### 旧版本升级

若数据库中尚无运行时配置记录，现有 `.env` 与 `config.toml` 中的业务项会被**一次性导入**。创建数据库配置记录后，后续启动不再用文件覆盖 Mini App 设置。确认设置中心内容无误后，可清理旧文件里的模型、功能与第三方密钥值；Docker 部署保留一个空的 `config.toml` 占位即可。

</details>

---

## 🧠 智能对话与模型

机器人不会见消息就回复。独立的**决策模型**先接收消息上下文（是否 @ bot、是否回复 bot、发送者身份、最近历史等），输出 `skip` 或 `casual` 两种结论：被 @ 或被回复时强制响应，群友互相聊天时优先保持沉默。

#### 功能特点

- **六种模型角色**：可分别选择供应商、模型、推理强度和回退链，未指定的辅助角色自动复用上级模型
- **多供应商接入**：通过 LiteLLM 支持 OpenAI、Anthropic、Gemini，以及任意 OpenAI 兼容网关
- **自动降级**：每个角色都可配置 fallback 链，上游故障时逐级回退
- **流式输出**：默认开启，按 36 字符分块、每 1.0 秒编辑一次消息
- **上下文压缩**：对话历史达到阈值后由后台主动蒸馏为背景摘要，不等待下一次回复触发；也可用 `/compact` 手动触发
- **说话风格模仿**：`/mimic` 学习指定用户的说话方式并注入回复提示词

<details>
<summary>📝 更多详细说明 (点击展开)</summary>

#### 模型角色表

| 角色 | 用途 | 默认超时 | 默认推理强度 | 未配置时 |
| :--- | :--- | :--- | :--- | :--- |
| **main** | 聊天回复、技能工具调用 | 12.0s | low | 必须配置 |
| **decision** | 判断是否回复（skip / casual） | 6.0s | none | 复用 main |
| **moderation** | 内容审核 | 8.0s | none | 复用 decision |
| **vision** | 图片 / 贴纸理解 | 15.0s | none | 复用 main |
| **compress** | 上下文压缩摘要、风格蒸馏 | 12.0s | none | 复用 main |
| **embed** | 向量嵌入 | 10.0s | — | 复用 main 供应商，默认 `text-embedding-004` |

默认重试参数：重试 2 次、退避 0.8 秒、每次重试超时递增系数 1.35。上下文上限默认 256000 tokens，单次输出上限 2048 tokens。推理强度可选 `none` / `minimal` / `low` / `medium` / `high`。

#### 供应商兼容处理

- **provider 别名归一**：`google→gemini`、`claude→anthropic`、`doubao`/`ark→volcengine`、`qwen`/`alibaba→dashscope`、`kimi`/`moonshotai→moonshot`、`grok→xai`、`minimaxi→minimax`
- **openai 前缀回退**：LiteLLM 无原生适配器且配置了自定义 `api_base` 时，自动规范为 `openai/<model>` 前缀
- **端点自适应**：根据 `api_base` 后缀自动识别 `/chat/completions`、`/responses`、`/v1/messages`、`/v1beta/models` 等形态
- **think 标签剥离**：推理模型输出的思考标签在进入回复前被清理
- **参数拒绝重试**：上游拒绝某个参数时自动去掉该参数重试

#### 说话风格模仿

管理员回复目标用户消息后发送 `/mimic`，bot 开始采样该用户的群消息：滚动窗口 **200 条**、总上限 **1000 条**，每约 **50 条**由 compress 模型蒸馏一次风格画像，并以最高优先级人格块注入回复提示词。`/mimic status` 查看进度，`/mimic off` 停止并清理样本。

#### 提示词

全部提示词以 Markdown 形式存放在 `prompt/` 目录，也可在 Mini App 的 Prompts 分区在线编辑：人设、决策、审核、技能系统、管理意图路由、回复模式、贴纸决策、主动话题、风格蒸馏、上下文压缩、闲聊。

</details>

---

## 🛡️ 内容审核

支持三种规则类型，每条规则可独立配置命中动作。审核模型输出 0.0–1.0 的置信度，据此分级处置。

#### 功能特点

- **三种规则类型**：`keyword`（关键词字面匹配）、`regex`（正则表达式）、`llm`（语义判断，可识别同义词、变体、谐音）
- **三种命中动作**：`warn`（警告）、`delete`（删除消息）、`ban`（累计警告达阈值后踢出）
- **置信度分级**：高置信度（默认 ≥ **0.9**）直接执行规则动作；低置信度不直接处罚，改为删除消息并发起真人质询
- **警告阈值**：默认累计 **3** 次警告触发封禁
- **审核豁免**：可按用户设置 AI 审核豁免，也支持全群「仅审核不回复」模式

<details>
<summary>📝 更多详细说明 (点击展开)</summary>

#### 使用步骤

1. 用 `/addrule <自然语言>` 添加群规，或在 Mini App 群组页的「群规与记忆」小节维护
2. 用 `/rules` 查看规则列表并通过内联按钮删除
3. 用 `/warnings` 查看本群累计的警告 / 封禁名单
4. 用 `/clearwarnings` 清空某用户的累计违规次数
5. 对信任用户回复 `/aiexempt` 设置审核豁免，`/unaiexempt` 撤销

#### 置信度分级工作流程

1. 消息进入审核，模型返回违规判定与 0.0–1.0 置信度
2. 置信度 ≥ 高置信度阈值（默认 0.9）→ 直接执行该规则配置的 warn / delete / ban
3. 置信度低于阈值 → **不直接处罚**，删除消息并发起真人质询（默认限时 **600 秒**）
4. 通过质询即恢复；超时未通过则封禁
5. 审核输出不可解析时**按不违规处理**，且不写入已审核缓存，避免误伤

#### Bot 消息审核

部分广告机借助 Telegram bot（如 guest 模式）在群内发广告，因此其他 bot 发送的消息同样进入内容审核：

- 每个 bot 在每个群累计通过 **5 条**干净消息后自动加入白名单，之后不再审核
- 只有携带**可审核内容**的消息才计入累计（文本、caption、可识别图片、文件名、联系人卡片）；纯占位媒体（无字幕的语音 / 视频等）既不计数也不调用审核模型
- 违规会删除消息、清零累计并计入警告；累计达到警告阈值自动封禁该 bot；高置信度命中 ban 规则时立即封禁
- 群管理员 bot 与人工豁免的 bot 自动跳过
- `/unaiexempt`（回复消息或 `/unaiexempt <ID>`）可撤销已获得的白名单
- bot 消息**只做审核**，不进入回复 / 决策管线，也不发起真人质询（bot 无法完成验证）

#### 注意事项

- 要收到其他 bot 的群消息，需在 @BotFather 中为本 bot 开启 **Bot-to-Bot Communication Mode**（Bot API 10.0+）
- 低置信度质询依赖已配置的 Turnstile / hCaptcha 与公网地址，否则不会发起
- 审核相关通知默认归入 `moderation` 自动删除类别

</details>

---

## 🔐 入群验证

新成员入群后**先于资料筛查立即被禁言**（禁言不等待 LLM 审查结果），需点击群内按钮跳转 bot 私聊，在 Telegram Mini App 内完成验证码后恢复权限。

#### 功能特点

- **逐群配置**：每个群都能单独关闭、开启或继承全局默认
- **三种验证方式**：Cloudflare Turnstile、hCaptcha，或「Turnstile + hCaptcha」双重验证
- **身份可信**：由 Telegram initData 签名（HMAC bot token）保证，无法伪造
- **默认超时 600 秒**，扫描间隔 30 秒
- **抢跑消息清理**：禁言生效前抢发的消息会被追溯删除

<details>
<summary>📝 更多详细说明 (点击展开)</summary>

#### 使用步骤

1. 在 [Cloudflare Turnstile](https://dash.cloudflare.com/?to=/:account/turnstile) 或 [hCaptcha](https://dashboard.hcaptcha.com/) 申请 Site Key 与 Secret Key
2. 在 Mini App 的「审核验证」分区填入密钥并选择验证方式
3. 在群组页的「入群欢迎」小节为该群开启入群验证
4. 确保 bot 是带「封禁用户」权限的群管理员

#### 三种终态语义

| 终态 | 处理 | 能否重新加入 |
| :--- | :--- | :--- |
| **通过验证** | 恢复全部发言权限 | — |
| **超时未完成** | 移出群聊，**不封禁** | ✅ 可重进重试 |
| **管理员点「拒绝」** | 在本群**永久封禁** | ❌ 需管理员手动解封 |

验证到达任一终态后，群内提示与私聊验证入口消息都会被改写或删除，Mini App 页面也会提前校验并拒绝重复验证。

#### 双重验证模式

Mini App 页面同时展示两个验证组件并引导按顺序完成：先完成第 1 步 Turnstile（完成前 hCaptcha 步骤置灰锁定），再完成第 2 步 hCaptcha，两个 token 一并提交，服务端依次经两家 siteverify 校验，全部通过才放行。该模式要求 Turnstile 和 hCaptcha 两套 Site / Secret Key **均已配置**。

#### 抢跑消息清理

禁言毕竟晚于入群一瞬，若新成员在禁言生效前抢发了消息，这些消息未经过任何验证：

1. 消息入口的**验证闸门**会直接删除「存在未完成验证记录」的发送者的消息
2. 验证禁言落地、资料筛查封禁执行时，都会**追溯删除**该成员自本次入群以来抢发的消息（进程内近况缓冲，覆盖典型竞态窗口）

#### 成员处置语义

永久封禁和「仅移出群聊」统一直接调用 `banChatMember(revoke_messages=True)`；仅移出随后调用 `unbanChatMember`，不留下封禁记录，用户之后可重新加入。

> [!NOTE]\
> 按 Telegram 官方说明，`revoke_messages` 会撤销被移除账号对旧群历史的访问，但**不会删除该账号过去发出的消息**。Bot API 目前没有按成员删除全部发言的方法（同名能力只存在于仅用户账号可调用的 MTProto API）。作为补充，bot 在进程内记录每名成员近期消息 id，刚入群的成员被验证禁言或封禁时，其入群后抢发的消息会经 `deleteMessages` 批量追溯删除。

#### 注意事项

- 未配置公网 HTTPS 地址时，验证按钮无法工作
- bot 必须拥有「封禁用户」权限，启动时会自检并在日志中告警
- 入群验证默认关闭，需在 Mini App 中开启

</details>

---

## 🕵️ 资料筛查与自动巡检

新成员入群时用群规审查其昵称、用户名和简介，命中即自动加入全局封禁名单。日常发言只审核消息内容本身；资料复查交由每日自动巡检完成。

#### 功能特点

- **入群即筛查**：昵称 / 用户名 / 简介命中群规立即加入全局封禁名单
- **全局封禁生效**：被全局封禁的用户在任意授权群发言即被删消息并封禁
- **每日自动巡检**：默认每天 **04:30**（Asia/Shanghai）分批复查全群成员，默认每批 **500** 人、批间停顿 **5.0 秒**
- **缓存跳过**：已通过且资料未变的成员会被跳过；修改群规后自动全员重查

<details>
<summary>📝 更多详细说明 (点击展开)</summary>

#### 巡检工作流程

1. 成员名单由入群 / 退群事件和消息流量自动维护，并从保留的对话历史一次性回填
2. 每天到达设定时间后，分批复查所有已知成员的昵称 / 用户名 / 简介是否违反群规
3. 发现违规：违规者被禁言，bot 发出一条 @ 全部违规成员的警告消息
4. 警告消息附带共享的「真人质询」内联按钮——**仅被点名成员可点击**
5. 点击后跳转 bot 私聊，在 Mini App 内完成人机验证即恢复权限
6. 超时（默认 **600 秒**）未完成的成员会被移出群聊但**不封禁**，可以重新进群

管理员、审核豁免用户和其他 bot 不参与巡检。

#### 手动触发

Mini App 的「审核验证」页可配置全局开关、每日巡检时间、批大小、质询超时等；群组页支持逐群覆盖开关，并可点击「立即巡检」手动触发一次。

#### 封禁命令

- `/ban`、`/unban` 对所有群管理员开放，默认只操作当前群；最高管理员使用时会收到「仅本群 / 全局」内联选择
- 回复消息执行 `/ban` 时，封禁成功后会同时删除被回复的消息
- `/spam` 沿用群管理员权限，直接封禁目标、删除被回复的垃圾消息并加入全局封禁名单
- `/banlist` 和 Mini App 全局名单仅最高管理员可见

#### 注意事项

- 巡检需要已配置 Turnstile / hCaptcha 真人验证服务，否则**不会执行**（避免禁言后无法解除）
- 巡检默认关闭，需在 Mini App 中开启

</details>

---

## 🚨 爆破防护

短时间批量入群达到阈值时自动锁定群组，锁定期间新加入者会被**临时移出但不永久封禁**。

#### 功能特点

- **默认触发条件**：**60 秒**窗口内 **8** 名成员加入
- **默认锁定时长**：**600 秒**，到期后主动发送解除通知，无需等待下一次入群
- **追溯质询**：回溯 **300 秒**内的加入者并发送真人质询
- **手动控制**：管理员可用 `/raidguard` 命令随时开启或解除
- **状态持久化**：手动状态重启后仍继续生效

<details>
<summary>📝 更多详细说明 (点击展开)</summary>

#### 命令说明

| 命令 | 说明 |
| :--- | :--- |
| `/raidguard on` | 无限期手动开启防护 |
| `/raidguard 30` 或 `/raidguard on 30` | 按分钟限时开启（此例为 30 分钟） |
| `/raidguard off` | 解除防护 |
| `/raidguard status` | 查看当前状态 |

命令别名 `/raid` 同样可用。

#### 工作流程

1. 监测入群速率，60 秒窗口内达到 8 人（阈值下限强制为 2 人、窗口下限 5 秒）即触发锁定
2. 触发时发送防护通知，全局默认会将其置顶
3. 追溯最近 300 秒内的加入者，发送真人质询消息：第一行是被点名成员的验证按钮，第二行是仅管理员可用的「一键移除被追溯用户」
4. 锁定期间新加入者被临时移出（不永久封禁），可在解除后重新加入
5. 锁定到期或管理员手动结束，主动发送解除通知并取消防护通知的置顶

#### 置顶与清理

触发与解除通知都长期保留。全局默认在防护触发时置顶通知，群组可用「继承全局 / 开启 / 关闭」三态单独覆盖。无论自动锁定超时结束还是管理员手动结束，都会取消该防护通知的置顶。待取消的精确消息 ID 会**持久化并自动重试**（重试间隔 30 秒、最多 5 次），因此消息替换、短暂网络失败或 Bot 重启不会丢失清理责任。

#### 注意事项

- 爆破防护默认关闭，需在 Mini App 中开启
- 追溯质询同样依赖已配置的真人验证服务

</details>

---

## 🗳️ 民主投票封禁

任何群成员回复骚扰消息并发送 `/voteban [举报理由]`，即对被回复用户发起民主投票；也可在回复目标消息时直接要求 Bot 发起，由 `vote_ban` 技能执行。

#### 功能特点

- **默认票数阈值 5 票**，投票有效期默认 **1800 秒**
- **每人一票**，发起人自动投出第一票
- **用户额度**：命令与 AI 技能**共享**持久化的单用户触发额度，默认每群每位用户 **3 次 / 3600 秒**，重启后不会清零
- **管理员快捷操作**：投票消息带「取消投票」和「直接封禁」两个管理员按钮
- **审计账本**：封禁 / 解封决定与 Telegram 实际执行结果全部入库

<details>
<summary>📝 更多详细说明 (点击展开)</summary>

#### 使用步骤

1. 回复骚扰消息，发送 `/voteban 广告刷屏`
2. Bot 发出带「投票封禁」内联按钮的投票消息，发起人已自动投出第一票
3. 其他成员点击按钮投票，每人限一票
4. 达到本群设定票数后立即在本群封禁该用户，并删除最初被投票的消息
5. 结果同步写入警告 / 封禁名单，Mini App 可见

#### 管理员操作

群管理员及以上可在投票消息上：

- **取消投票**：提前终止本次投票
- **直接封禁**：跳过计票立即封禁

结果通知与审计账本会记录执行的管理员身份。

#### 限制条件

- 民主投票封禁**默认关闭**，需在 Mini App 中开启
- 管理员、最高管理员、机器人与匿名身份**不可被投票**
- 同一目标同时只能有一个进行中 / 执行中的投票
- 超过有效期自动失效
- 额度耗尽时技能向主模型返回结构化错误，主模型必须拒绝并说明恢复时间，**不得建议改用命令绕过**
- 全局与每群均可覆盖票数、有效期、额度次数和统计窗口

#### 置顶与清理

全局默认会在投票进行期间置顶投票消息，群组可用「继承全局 / 开启 / 关闭」三态单独覆盖。票数达标完成、管理员中途取消、管理员直接封禁或票数不足超时等**任一结束路径**都会取消置顶。进行中的投票消息不会被定时删除，结束后的结果通知按 `vote` 类别清理。

#### 可信上下文

Bot 的可信上下文会包含当前封禁对象、原因、发起人、投票票数和成功 / 失败状态，主模型据此回答相关询问。

</details>

---

## 📣 呼叫管理员

群成员发送 `@admin`（或 `@admins`）即可一键呼叫群管理员，机器人发送一条**真实 @ 提及**管理员的通知。

#### 功能特点

- **默认开启**，冷却时间默认 **60 秒**
- **可附说明**：`@admin` 后可跟随说明文字
- **可锚定举报**：回复某条消息发送时，会同时附上被举报消息内容并锚定回复
- **逐群指定目标**：Mini App 群组页可勾选要 @ 的管理员，默认全选
- **审核前置**：审核不通过的消息不会触发呼叫

<details>
<summary>📝 更多详细说明 (点击展开)</summary>

#### 使用步骤

1. 在群内直接发送 `@admin`，或 `@admin 有人刷屏`
2. 若要举报特定消息，回复该消息再发送 `@admin`
3. Bot 发出真实提及管理员的通知消息

#### 配置说明

- **全局配置**：开关（默认开启）、冷却秒数（默认 60）、通知自动置顶（默认**关闭**）
- **逐群覆盖**：群组页可覆盖开关，并勾选目标管理员
- **全选语义**：默认全部勾选，全选状态下**新晋管理员自动包含**

#### 工作流程

1. 成员发送 `@admin`，先经过内容审核
2. 检查冷却时间，冷却期内不重复呼叫
3. 查询本群管理员列表，按配置筛选目标
4. 发送真实 @ 提及通知（管理员显示名上限 32 字符）
5. 开启通知置顶后，管理员可在通知上标记「已处理」并取消置顶

通知消息属于独立的 `call_admin` 自动删除类别。

</details>

---

## ⌨️ 关键词回复与定时消息

在 Mini App 群组页的「关键词与定时」小节逐群配置，两者共享同一套内容与按钮编辑能力。

#### 功能特点

- **关键词三种匹配**：包含（contains）、完全匹配（exact）、正则（regex）
- **正则安全防护**：正则匹配设有 **0.05 秒**引擎超时，防止灾难性回溯（ReDoS）
- **定时两种节奏**：「每天定时」按 HH:MM（Asia/Shanghai）每天一次；「固定间隔」按分钟循环，最短 **5 分钟**
- **自定义内联按钮**：跳转链接、复制文字、分享、管理员删除，可横向排列
- **按钮样式**：支持 Telegram 的 `primary`（蓝）、`success`（绿）、`danger`（红）
- **独立置顶控制**：每条规则 / 任务自己的置顶开关，不受活动通知的全局默认影响

<details>
<summary>📝 更多详细说明 (点击展开)</summary>

#### 关键词回复

1. 在 Mini App 群组页「关键词与定时」小节新增规则
2. 填写触发关键词并选择匹配方式（包含 / 完全匹配 / 正则）
3. 编写回复内容，支持换行、可拉伸编辑框和安全 Markdown
4. 可选：添加自定义内联按钮，配置置顶、自动删除和启用状态

无效或过慢的正则会被自动跳过并记录日志，不会拖垮消息处理。

#### 定时消息

1. 选择节奏类型：
   - **每天定时**：按 HH:MM（Asia/Shanghai）每天发送一次
   - **固定间隔**：按分钟数循环发送，低于 5 分钟会被强制提升到 5 分钟
2. 编写内容并配置按钮
3. 每条任务可独立设置：置顶、取消上一条置顶、自动删除、启用状态

后台调度器按固定间隔检查到期任务。`last_run_at` 锚定机制确保重启不会重复触发：间隔型任务需在 `last_run_at`（首次为 `created_at`）超过设定间隔后才触发。

#### 注意事项

- 关键词回复归入 `keyword` 自动删除类别，定时消息归入 `scheduled` 类别
- 两者的置顶开关都是**逐条独立**的，不继承活动通知的全局默认值

</details>

---

## 👋 入群欢迎语

在 Mini App 群组页的「入群欢迎」小节逐群配置，留空则不发送。

#### 功能特点

- **占位符支持**：`{name}`（新成员名称）和 `{mention}`（可点击提及）
- **富文本**：换行、安全 Markdown 与自定义内联按钮
- **按钮样式**：可在行号后追加 `primary`（蓝）、`success`（绿）或 `danger`（红）
- **发送时机智能**：开启入群验证的群，欢迎语在成员**通过验证后**才发送

<details>
<summary>📝 更多详细说明 (点击展开)</summary>

#### 发送时机

| 群配置 | 欢迎语发送时机 |
| :--- | :--- |
| 已开启入群验证 | 成员**通过验证后**发送（含管理员直接通过） |
| 未开启入群验证 | 完成资料筛查后发送 |

这样可以避免给尚未通过验证、甚至即将被移出的账号发送欢迎语。

#### 使用步骤

1. 进入 Mini App 群组页 →「入群欢迎」小节
2. 填写欢迎语内容，可使用 `{name}` 和 `{mention}` 占位符
3. 可选：配置内联按钮与链接预览开关
4. 保存后立即对新入群成员生效

欢迎语归入 `welcome` 自动删除类别。

</details>

---

## 🕰️ 群权限与夜间模式

Mini App 群组页的「成员权限」小节可读取并**完整编辑** Telegram 当前 `ChatPermissions` 的全部字段。

#### 功能特点

- **完整权限编辑**：覆盖 Telegram `ChatPermissions` 全字段
- **独立时区**：每个群单独保存 IANA 时区
- **定时权限时段**：可跨午夜、选择星期、设置优先级
- **部分覆盖**：时段只覆盖指定权限，其余继承基础权限
- **自动恢复**：后台在切换点、重启后及周期校准时重新应用

<details>
<summary>📝 更多详细说明 (点击展开)</summary>

#### 使用示例

假设基础权限允许发送图片，配置一个 23:00–07:00 的「禁止发送图片」时段覆盖后：

1. 每晚 23:00 自动关闭图片发送权限
2. 每天早上 07:00 自动恢复到基础权限
3. 时段跨越午夜由系统正确处理

#### 工作流程

1. 在 Mini App 中编辑基础权限与时区
2. 添加一个或多个定时时段，选择生效星期与优先级
3. 保存后**立即下发**到 Telegram
4. 后台 `group-permission-runner` 常驻任务在时段切换点重新应用
5. Bot 重启后与周期校准时都会重新对齐当前应生效的权限

#### 注意事项

- 需要 bot 拥有相应的群管理员权限
- 多个时段重叠时按配置的优先级决定生效顺序

</details>

---

## 🧰 技能系统

主模型通过 function calling **自主决定**调用哪些技能，没有独立的技能规划模型。

#### 功能特点

- **14 个内置技能**，覆盖搜索、抓取、多媒体、文档查询与群管理
- **按需注册**：需要凭据的技能只在凭据合规可用时才注册
- **逐群开关**：部分技能可按群独立启用
- **SSRF 防护**：外部地址只允许访问 DNS 校验后的公网 HTTPS 目标

<details>
<summary>📝 更多详细说明 (点击展开)</summary>

#### 技能清单

| 技能 | 说明 | 前置条件 |
| :--- | :--- | :--- |
| `memory_manage` | 查看 / 新增 / 修改永久记忆（删除走 `/lm`） | 无 |
| `rule_manage` | 查看 / 新增群规（删除走 `/rules`） | 无 |
| `send_sticker` | 语义匹配发送贴纸 | 无 |
| `websearch` | DuckDuckGo 联网搜索 | 无 |
| `webfetch` | 抓取网页正文内容 | 无 |
| `music_search` | GD Studio 音乐 API：搜索、点播、歌词、专辑封面 | 默认开启 |
| `bilibili_search` | B 站视频 / UP 主搜索、热门、排行榜 | 无 |
| `weibo_search` | 微博热搜、内容搜索、Feed 流 | 无 |
| `mihomo_doc` | 实时查询 mihomo（Clash Meta）官方 Wiki | 无 |
| `routeros_doc` | 实时查询 MikroTik RouterOS 官方手册与 CLI 参考 | 无 |
| `movie_info` | TMDB / 官方 IMDb 电影搜索、详情、评分与上映状态 | 需配置凭据 |
| `api_model_query` | 逐群配置的 OpenAI 兼容 API 模型列表与测活 | **默认关闭**，按群启用 |
| `doubao_tts` | 豆包 TTS 语音合成 | 需配置凭据 |
| `vote_ban` | 明确请求且回复目标消息时发起民主投票 | 与命令共用用户额度 |

#### api_model_query

默认对所有群关闭。每个群在设置中心单独填写自己的 Base URL 和 API Key 后启用；API Key 使用 `CONFIG_MASTER_KEY` 加密并**与其他群隔离**。模型测活会先刷新该群的模型列表，只测试列表中精确存在的模型 ID。配置地址只允许访问 DNS 校验后的公网 HTTPS 目标，鉴权请求不会跟随重定向，上游返回文本也会在进入主模型前按当前群 API Key 脱敏。

#### movie_info 授权提示

供应商凭据在设置中心配置，**只有至少一个合规可用的供应商才会注册**该技能。IMDb 实时接口仅指官方 AWS Data Exchange GraphQL API，需要相应商业订阅与许可，不会抓取 IMDb 网页。TMDB 也必须先取得适用于当前用途的 API 授权，尤其是 AI / chatbot 或商业场景，不应仅凭普通 developer key 默认上线。

凭据申请、授权范围与接口字段以官方文档为准：[IMDb API Getting Access](https://developer.imdb.com/documentation/api-documentation/getting-access/)、[IMDb API Calling](https://developer.imdb.com/documentation/api-documentation/calling-the-api/)、[TMDB Application Authentication](https://developer.themoviedb.org/docs/authentication-application)、[TMDB API Terms of Use](https://www.themoviedb.org/api-terms-of-use)。

#### 贴纸系统

收到贴纸后自动记录 file_id、emoji、贴纸包和视觉描述到贴纸库。回复时由独立的**贴纸决策模块**判断是否发送，优先按语义从已学习贴纸中选择，支持默认贴纸池兜底。

#### 语音合成（TTS）

集成豆包 TTS，默认音频格式 `ogg_opus`、采样率 48000、码率 96000、单次文本上限 500 字。三种模式通过 `/tts` 切换：

- `/tts disable` — 关闭语音
- `/tts enable` — 智能模式，由模型决定是否发语音
- `/tts always` — 始终以语音回复

#### 影片查询（AV）

支持按番号直查、按演员 / 关键词搜索，来源覆盖 JAVBUS / MADOUQU / DMM / FC2，默认单次最多 18 条结果。每群独立开关（`/av enable|disable`）；群内仅在已授权且已启用时可用，私聊仅最高管理员可用。支持内联翻页浏览详情和种子。

</details>

---

## 📝 永久记忆与主动话题

#### 功能特点

- **永久记忆**：管理员通过自然语言或 `/lm` 命令维护群组长期记忆，回复时自动注入上下文
- **持久化**：存储于数据库，重启不丢失，支持列表翻页和内联按钮删除
- **主动话题**：群组长时间沉默后，bot 可自动抛出一个结合群记忆的话题
- **静默时段**：默认 0 点–9 点不主动发言

<details>
<summary>📝 更多详细说明 (点击展开)</summary>

#### 永久记忆命令

| 命令 | 说明 |
| :--- | :--- |
| `/lm` | 查看记忆列表，支持翻页和内联删除 |
| `/lm add <内容>` | 新增一条永久记忆 |
| `/lm replace <#ID 或关键词> => <新内容>` | 修改已有记忆 |

主模型也会通过 `memory_manage` 技能自动完成新增 / 查看 / 修改；删除统一走 `/lm` 命令页的内联按钮。

#### 主动话题

| 参数 | 默认值 |
| :--- | :--- |
| 默认开关 | 关闭 |
| 沉默阈值 | 180 分钟 |
| 随机抖动 | 60 分钟 |
| 检查间隔 | 60 秒 |
| 静默时段 | 00:00 – 09:00 |
| 失败重试 | 30 分钟 |

用 `/proactive on|off|status` 控制，全部参数可在 Mini App 的「Bot 行为」分区调整。

#### 上下文压缩

对话历史达到阈值后由 compress 模型蒸馏为背景摘要落库，压缩在后台主动进行，不等待下一次触发回复——即使群里长期无人 @ Bot（仅 @ 回复模式），上下文也不会积压到回复时才压缩。触发条件（满足其一）：

- token 达到上下文预算（默认 256000 tokens）的 85%（估算按 CJK 每字约 1 token 计）
- 临时对话历史达到 800 条（提前于 1000 条硬上限，避免旧消息未摘要即被丢弃）

自动压缩会保留最近 50 条原文作为短期语境，其余蒸馏进摘要。管理员也可用 `/compact` 立即把本群全部临时对话历史压缩进背景摘要。

</details>

---

## 🔧 配置说明

配置分为两层：**`.env` 启动引导**（无法自举的少数项）与 **Mini App 运行配置**（其余全部）。

#### `.env` 启动引导项

| 变量 | 必填 | 默认值 | 说明 |
| :--- | :---: | :--- | :--- |
| `BOT_TOKEN` | ✅ | — | Telegram Bot Token，不可使用模板占位符 |
| `SUPER_ADMIN_ID` | ✅ | — | 最高管理员 Telegram 用户 ID，必须为正整数 |
| `CONFIG_MASTER_KEY` | ✅ | — | 数据库密钥加密主密钥，必须长期稳定 |
| `DATABASE_URL` | | `sqlite+aiosqlite:///./data/bot.db` | 数据库连接串 |
| `MINIAPP_PUBLIC_BASE_URL` | | 空 | 对外 HTTPS origin，留空则设置与验证按钮不可用 |
| `MINIAPP_LISTEN_HOST` | | `0.0.0.0` | 监听地址 |
| `MINIAPP_LISTEN_PORT` | | `8480` | 监听端口 |
| `WEBHOOK_URL` | | 空 | Webhook 公网地址，不超过 256 字符 |
| `WEBHOOK_SECRET` | | 空 | 32–256 位字母、数字、下划线或连字符 |

#### 反向代理

需要把以下路径转发到 `MINIAPP_LISTEN_HOST:MINIAPP_LISTEN_PORT`：

| 路径 | 用途 |
| :--- | :--- |
| `/settings` | Mini App 设置中心页面 |
| `/verify` | 入群验证 / 真人质询页面 |
| `/api/v1/*` | 设置中心后端 API |
| `/healthz` | 健康检查（Docker healthcheck 使用） |
| `WEBHOOK_URL` 的完整路径 | Telegram webhook 投递（若启用） |

#### Webhook 与长轮询

配置 `WEBHOOK_URL` 和 `WEBHOOK_SECRET` 后，Telegram 更新会通过现有 Mini App HTTP 监听器接收。启动时会从 bot 所在主机探测该公网地址，运行中也会监控 Telegram 投递错误。

出现以下任一情况时，bot 会在日志中写明原因、清理远端 webhook，并**自动降级为长轮询**：

- 未配置 `WEBHOOK_URL` 或 `WEBHOOK_SECRET`
- `WEBHOOK_URL` 超过 256 字符或路径格式非法
- `WEBHOOK_SECRET` 不符合 32–256 位字符集要求
- 公网地址自检失败
- Telegram 注册失败
- 运行中出现投递异常

运行中降级会保留已经积压的更新。更新在处理前先写入持久化 inbox 并去重，因此进程崩溃或重启不会丢失或重复处理更新。

#### 数据库

默认使用 SQLite（WAL 模式），共 32 张表，涵盖群组、群规、关键词回复、定时消息、投票会话、封禁审计、违规记录、入群验证、成员名单、webhook inbox、删除任务、巡检记录、风格样本、贴纸库等。所有运行配置保存在数据库中并对后续请求热生效。

第三方密钥使用 `CONFIG_MASTER_KEY` 加密，API 只返回「已配置」状态，不回传明文。全局配置使用 revision 乐观锁防止多个页面互相覆盖。

#### 有序停机

收到停止信号后按固定顺序收尾：停止接收新更新 → 等待在途更新完成（最长 35 秒）→ 关闭后台任务（10 秒）→ 执行清理（20 秒）→ 关闭 Web 服务（20 秒），整体硬上限 110 秒，超时由看门狗强制退出。Docker `stop_grace_period` 设为 125 秒以完整覆盖该流程。

---

## 📂 项目结构

```text
Smart_Group_Bot/
├── bot/
│   ├── __main__.py              # 入口（python -m bot），生命周期与有序停机
│   ├── loader.py                # Bot / Dispatcher 初始化与中间件注册
│   ├── config.py                # 启动配置、模型配置类型与供应商归一
│   ├── handlers/
│   │   ├── commands.py          # 通用命令（/start /help /settings /lm /av /voteban /compact）
│   │   ├── admin.py             # 管理命令（授权、群规、审核、封禁、raidguard、TTS 等）
│   │   ├── membership.py        # 入群 / 离群事件（筛查、验证）
│   │   └── group.py             # 群消息主流程（审核 → 决策 → 回复）
│   ├── middlewares/
│   │   ├── db.py                # 数据库会话注入
│   │   ├── logging_mw.py        # 日志追踪（trace_id）
│   │   ├── global_ban.py        # 全局封禁拦截（外层）
│   │   ├── member_roster.py     # 成员名单维护（外层，供巡检）
│   │   ├── update_dedup.py      # 更新去重
│   │   └── verification_gate.py # 验证闸门（拦截抢跑消息）
│   ├── services/
│   │   ├── llm.py               # LLM 调用封装（LiteLLM + fallback + 流式合并）
│   │   ├── decision.py          # 决策引擎（skip / casual）
│   │   ├── moderation.py        # 内容审核（置信度分级）
│   │   ├── manage_intent.py     # 管理意图路由
│   │   ├── memory.py            # 记忆服务（对话历史 + 上下文压缩）
│   │   ├── recent_messages.py   # 近期消息缓冲（抢跑消息追溯）
│   │   ├── reply_mode.py        # 回复模式选择（reply / message）
│   │   ├── reply_output.py      # 回复解析与输出
│   │   ├── reply_progress.py    # 流式进度消息
│   │   ├── casual.py            # 闲聊回复
│   │   ├── proactive.py         # 主动话题
│   │   ├── speech_style.py      # 说话风格模仿（/mimic）
│   │   ├── at_reply.py          # 仅 @ 回复模式
│   │   ├── join_screening.py    # 入群资料筛查 + 全局封禁
│   │   ├── join_verification.py # 入群验证 / 审核质询
│   │   ├── verify_web.py        # 内置验证页与 HTTP 服务（aiohttp）
│   │   ├── patrol.py            # 每日自动巡检
│   │   ├── welcome.py           # 入群欢迎语
│   │   ├── raid_guard.py        # 爆破防护
│   │   ├── vote_ban.py          # 民主投票封禁
│   │   ├── ban_audit.py         # 封禁审计账本
│   │   ├── call_admin.py        # 呼叫管理员（@admin）
│   │   ├── bot_screening.py     # 其他 bot 的消息审核与白名单
│   │   ├── notification_pins.py # 通知置顶与持久化取消
│   │   ├── telegram_cleanup.py  # 消息删除任务队列
│   │   ├── keyword_reply.py     # 关键词回复
│   │   ├── scheduled_messages.py# 定时消息
│   │   ├── message_templates.py # 消息模板与内联按钮
│   │   ├── group_permissions.py # 群默认权限与定时时段
│   │   ├── group_settings.py    # 逐群设置读写
│   │   ├── member_identity.py   # 成员身份解析
│   │   ├── admin_status.py      # 管理员身份缓存
│   │   ├── authz.py             # 授权模型（授权群 / 群管理 / 最高管理员）
│   │   ├── callback_auth.py     # 内联回调鉴权
│   │   ├── runtime_config.py    # 数据库运行时配置、加密与热应用
│   │   ├── update_delivery.py   # Webhook / 长轮询投递与降级
│   │   ├── update_completion.py # 在途更新完成度追踪
│   │   ├── request_priority.py  # 请求优先级队列
│   │   ├── privileged_tasks.py  # 特权任务队列
│   │   ├── telegram_session.py  # Telegram 会话与限流
│   │   ├── background_health.py # 后台任务健康监控
│   │   ├── resource_health.py   # 资源看门狗
│   │   ├── sticker_decision.py  # 贴纸决策模块
│   │   ├── sticker_library.py   # 贴纸学习库
│   │   ├── av_search.py         # 影片搜索（JAVBUS / MADOUQU / DMM / FC2）
│   │   ├── doubao_tts.py        # 豆包 TTS 服务
│   │   ├── api_model_query.py   # 逐群模型 API 查询
│   │   └── skills/              # 技能实现（tool-calling）
│   │       ├── service.py       # 技能调度循环
│   │       ├── base.py          # 技能基类
│   │       ├── platform_common.py  # 公网校验、限流与脱敏
│   │       ├── memory_manage.py / rule_manage.py / vote_ban.py
│   │       ├── send_sticker.py / websearch.py / webfetch.py
│   │       ├── music_search.py / bilibili_search.py / weibo_search.py
│   │       ├── movie_info.py / api_model_query.py / doubao_tts.py
│   │       └── mihomo_doc.py / routeros_doc.py
│   ├── db/
│   │   ├── models.py            # ORM 模型（32 张表）
│   │   ├── engine.py            # 数据库引擎与迁移
│   │   └── sqlite_session.py    # SQLite 并发处理
│   ├── utils/
│   │   ├── telegram.py          # Telegram 工具函数与自动删除类别
│   │   ├── prompts.py           # 提示词加载
│   │   ├── runtime_context.py   # 运行时上下文构建
│   │   ├── conversation_context.py
│   │   ├── bot_identity.py      # 运行时 bot 身份块
│   │   ├── command_catalog.py   # 命令注册表（供提示词与 /help 复用）
│   │   ├── security.py          # 输入安全处理与转义
│   │   ├── logging_setup.py     # 日志配置与轮转
│   │   ├── project_info.py      # 项目信息
│   │   └── timezone.py          # 时区工具
│   └── web/
│       ├── settings_api.py      # 设置中心 REST API
│       ├── auth.py              # Mini App initData 鉴权
│       └── static/              # 设置中心前端页面
├── prompt/                      # 各模块提示词（Markdown）
│   ├── persona.md               # 人设
│   ├── decision.md              # 决策提示词
│   ├── moderation.md            # 审核提示词（含置信度）
│   ├── skill_tools_v2.md        # 技能系统提示词
│   ├── manage_intent.md         # 管理意图路由
│   ├── reply_mode.md            # 回复模式
│   ├── sticker_decision.md      # 贴纸决策
│   ├── proactive_topic.md       # 主动话题
│   ├── style_distill.md         # 风格蒸馏
│   ├── compress.md              # 上下文压缩
│   └── casual.md                # 闲聊
├── tests/                       # 测试（unittest）
├── config.toml                  # 旧版本一次性迁移输入，导入后忽略
├── .env.example                 # 最小启动配置模板
├── pyproject.toml
├── uv.lock / requirements.lock  # 锁定依赖
├── Dockerfile
├── docker-compose.yml
└── start.py                     # 一键启动脚本
```

运行测试：

```bash
python -m unittest discover -s tests
```

---

## 🤝 贡献指南

欢迎任何形式的贡献！如果你有好的想法或发现了 Bug，请随时提交 Pull Request 或创建 Issue。

- 源码仓库：[Hamster-Prime/Smart_Group_Bot](https://github.com/Hamster-Prime/Smart_Group_Bot)
- 开发者：[@Sanite_Ava](https://t.me/Sanite_Ava)
- 联系方式：[@Sanite_Ava_Private_ChatBot](https://t.me/Sanite_Ava_Private_ChatBot)

提交代码前请确保测试通过：`python -m unittest discover -s tests`

## 📄 许可证

本项目采用 [MIT 许可协议](LICENSE)。

## 🙏 致谢

- [aiogram](https://github.com/aiogram/aiogram) — 现代化的异步 Telegram Bot 框架
- [LiteLLM](https://github.com/BerriAI/litellm) — 统一多供应商大模型抽象层
- [SQLAlchemy](https://www.sqlalchemy.org/) — 强大的异步 ORM
- [aiohttp](https://github.com/aio-libs/aiohttp) — HTTP 客户端与内置 Web 服务
- [Pydantic](https://github.com/pydantic/pydantic) — 配置校验与模型定义
- [ddgs](https://github.com/deedy5/ddgs) — DuckDuckGo 搜索能力
- [Cloudflare Turnstile](https://www.cloudflare.com/products/turnstile/) / [hCaptcha](https://www.hcaptcha.com/) — 真人验证服务

---

<p align="center">
  如果这个项目对你有帮助，请给个 Star ⭐️
</p>
<p align="center">
  <a href="https://www.star-history.com/#Hamster-Prime/Smart_Group_Bot&type=date&legend=bottom-right">
    <img src="https://api.star-history.com/svg?repos=Hamster-Prime/Smart_Group_Bot&type=date&legend=bottom-right" alt="Star History Chart">
  </a>
</p>
