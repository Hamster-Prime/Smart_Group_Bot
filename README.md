# Smart Group Bot

一个面向 Telegram 群聊的智能机器人，支持：
- AI 审核（基于群规语义判定）
- 知识库问答（RAG）
- 日常闲聊回复
- 图片/贴纸视觉理解（OCR + 场景描述）
- 技能系统（`websearch` / `webfetch`）
- 记忆压缩与落盘（定时 + 停机前）

## 功能概览

### 1. 群消息处理链路
1. 内容审核（AI）
2. 决策模型（`skip` / `knowledge` / `casual`）
3. RAG 或技能调用
4. 闲聊兜底
5. 记忆写入与压缩

### 2. 媒体消息能力
- 图片：支持视觉识别
- 图片+文字：支持视觉识别并与文字合并
- 贴纸：按图片逻辑处理（静态贴纸直接识别，动态贴纸优先缩略图）
- 视频类媒体：直接放行（不触发回复）

### 3. 技能（Skills）
- `websearch`：基于 DDGS 搜索网页
- `webfetch`：抓取 URL 并提取正文

### 4. 安全防护
- 提示词注入检测
- 用户输入 / 历史上下文 / 知识片段 / 网页内容统一按不可信数据处理
- 模型系统提示词前置安全规则，避免越权指令执行

## 项目结构

```text
bot/
  handlers/        # Telegram 事件处理
  services/        # 决策、审核、RAG、技能、记忆等服务
  middlewares/     # 日志、限流、数据库会话
  db/              # 数据模型与数据库初始化
  utils/           # 工具函数与 prompt 加载
prompt/            # 模型提示词模板
data/              # SQLite 数据目录（运行时生成）
memory/            # 记忆压缩落盘目录（运行时生成）
start.py           # 推荐启动入口
```

## 运行要求

- Python `>= 3.12`
- Telegram Bot Token
- 可用的 LLM / Embedding 提供方配置（在 `.env`）

## 快速开始

### 1) 安装依赖

```bash
pip install -e .
```

### 2) 配置环境变量

复制并编辑：

```bash
cp .env.example .env
```

至少配置：
- `BOT_TOKEN`
- 主模型与 API Key（`MAIN_PROVIDER` / `MAIN_MODEL` / `MAIN_API_KEY`）

### 3) 启动 Bot

```bash
python start.py
```

Windows 可用：

```bat
start.bat
```

## 常用命令

- `/start`
- `/help`
- `/kb <自然语言指令>`（管理员）
- `/addrule <自然语言指令>`（管理员）
- `/rules`（管理员）
- `/warnings <用户ID>`（管理员）

## Docker（仅 Bot）

```bash
docker compose up --build -d
```

## GitHub 提交建议

建议不要提交以下内容：
- `.env`
- `data/`、`memory/`
- 本地虚拟环境与缓存文件
- IDE 临时文件

本仓库 `.gitignore` 已覆盖常见场景，可按需再扩展。

## 许可证

如需开源发布，请自行补充 `LICENSE` 文件。