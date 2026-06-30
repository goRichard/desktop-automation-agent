# Desktop Automation Agent — 完整设计规范（Spec）

---

## 1. 项目概述

### 目标

构建一个**类 Claude Code 风格**的命令行桌面自动化 Agent，用户通过自然语言对话驱动复杂的 Windows 桌面操作工作流。

### 核心特性

| 特性 | 说明 |
|------|------|
| 命令行交互 | Rich + Click 实现流式输出、工具调用可视化、REPL 模式 |
| Agent Loop | 自定义 async 循环，多轮工具调用 → 观察 → 继续 |
| Skills 系统 | SKILL.md 格式定义可复用工作流，LLM 自动识别并执行 |
| 桌面原子操作 | winpeekaboo 覆盖截图/UI识别/输入/窗口/应用全部操作 |
| OCR/文档解析 | MinerU 本地部署接口，返回 Markdown |
| 多模态视觉 | vLLM 部署的多模态模型（OpenAI API 兼容） |
| 持久化定时任务 | LLM 直接生成 cron 表达式，APScheduler + SQLite，跨 session 存活 |
| 配置化模型 | YAML 配置文件管理模型 profile，.env 管理密钥 |

### 技术约束

- **全部 Python 实现**（Python 3.11+）
- 仅 Windows 平台（winpeekaboo 依赖）
- 单一 SQLite 数据库，零额外基础设施依赖

---

## 2. 技术栈

| 层次 | 技术选型 | 说明 |
|------|---------|------|
| CLI 界面 | **Click + Rich** | 命令解析、流式输出、Spinner、工具调用渲染 |
| Agent Loop | **自定义实现** | 完全可控，避免 LangChain 过度抽象 |
| LLM 统一接口 | **LiteLLM** | 统一调用所有 OpenAI-compatible 接口 |
| 桌面原子操作 | **winpeekaboo** | Python CLI 工具，覆盖全部桌面操作原子能力 |
| OCR/文档解析 | **MinerU（本地部署）** | POST /file_parse，支持 PDF/PNG/JPG/DOCX → Markdown |
| 多模态视觉 | **vLLM 多模态模型** | OpenAI API 兼容，通过 LiteLLM 统一调用 |
| 定时调度 | **APScheduler** | cron/interval/date 三种触发方式 |
| 持久化存储 | **SQLite（SQLModel）** | 零配置，5 张业务表 |
| 配置管理 | **YAML + Pydantic Settings** | config.yaml 管理模型配置，.env 管理密钥 |
| HTTP 客户端 | **httpx** | 异步调用 MinerU 接口 |

---

## 3. 系统架构

```
┌──────────────────────────────────────────────────────────────┐
│                      CLI 层 (Rich + Click)                    │
│   REPL 交互 / 流式输出 / 工具调用可视化 / 命令解析 / Spinner   │
├──────────────────────────────────────────────────────────────┤
│                      Agent Loop 核心                          │
│  User Input → Context Assembly → LLM Call → Tool Dispatch    │
│            → Observation → Loop / Final Response             │
│            ↕ 每条消息自动写入 SQLite                           │
├───────────────┬─────────────────┬────────────────────────────┤
│   LLM 层      │   Skills 引擎   │   Scheduler 引擎            │
│  (LiteLLM)    │  (SKILL.md)     │  (APScheduler)             │
│  对话 + 视觉  │  注册/加载/注入  │  cron 解析/持久化/恢复      │
├───────────────┴─────────────────┴────────────────────────────┤
│                  原子工具层 (Tool Registry)                    │
│  winpeekaboo: see/image/click/type/press/hotkey/scroll/drag  │
│               window/app/list                                │
│  MinerU OCR:  parse_file_to_markdown / screenshot_and_ocr   │
│  Vision:      analyze_screen（截图 → vLLM 多模态）            │
│  System:      文件操作 / 剪贴板 / 系统命令                    │
│  Scheduler:   create_job / list_jobs / delete_job / toggle   │
├──────────────────────────────────────────────────────────────┤
│                  持久化层 (SQLite via SQLModel)                │
│  sessions / messages / scheduled_jobs /                      │
│  job_execution_logs / agent_memory                           │
└──────────────────────────────────────────────────────────────┘
```

---

## 4. 项目目录结构

```
desktop-agent/
├── main.py                        # 入口
├── pyproject.toml                 # 依赖声明
├── config.yaml                    # 模型 & 服务配置（版本管理）
├── .env                           # 密钥（不纳入版本管理）
├── .env.example                   # 密钥模板
│
├── agent/
│   ├── loop.py                    # Agent Loop 主逻辑（async）
│   ├── context.py                 # 上下文组装（DB历史+记忆+Skills摘要）
│   └── tool_dispatcher.py         # 工具调用分发与执行
│
├── llm/
│   ├── client.py                  # LiteLLM 封装（对话流式 + 视觉调用）
│   └── config.py                  # 从 Settings 构建 LiteLLM 调用参数
│
├── skills/
│   ├── registry.py                # 扫描、注册、加载所有 SKILL.md
│   ├── parser.py                  # 解析 SKILL.md → SkillDefinition
│   └── user_skills/               # 用户定义的 skill 目录
│       └── example.skill.md
│
├── tools/
│   ├── registry.py                # @tool 装饰器，自动生成 JSON Schema
│   ├── winpeekaboo.py             # winpeekaboo 全部命令封装为工具函数
│   ├── ocr.py                     # MinerU OCR 工具
│   ├── vision.py                  # 多模态视觉分析工具
│   ├── system.py                  # 文件/剪贴板/系统命令工具
│   └── scheduler_tool.py          # 定时任务管理工具
│
├── scheduler/
│   ├── engine.py                  # APScheduler 初始化 + SQLAlchemyJobStore
│   └── job_runner.py              # 执行定时任务（写日志 + 调用 Agent Loop）
│
├── memory/
│   ├── models.py                  # SQLModel 表定义（5 张表）
│   ├── session.py                 # 会话内短期记忆操作
│   └── store.py                   # 数据库 CRUD 封装
│
├── cli/
│   ├── app.py                     # Click 命令定义（chat/run/jobs/skills 等）
│   └── display.py                 # Rich 组件（Panel/Spinner/ToolCallDisplay）
│
├── config/
│   └── settings.py                # Pydantic Settings：合并 config.yaml + .env
│
└── data/
    └── agent.db                   # SQLite（首次启动自动创建）
```

---

## 5. 配置体系

### 5.1 `config.yaml`（模型与服务配置）

```yaml
# 当前激活的 profile（local / cloud）
active_profile: local

profiles:
  local:                                          # 内网 vLLM 部署
    llm:
      model: openai/qwen2.5-72b
      api_base: https://llm-ai-model.apps.os.sewc.siemens.cn/v1/
      api_key_env: LLM_API_KEY                    # 指向 .env 变量名
      temperature: 0.7
      max_tokens: 4096
    vision:
      model: openai/qwen2-vl-72b
      api_base: https://llm-ai-model.apps.os.sewc.siemens.cn/v1/
      api_key_env: VISION_API_KEY

  cloud:                                          # 云端 OpenAI
    llm:
      model: openai/gpt-4o
      api_base: https://api.openai.com/v1/
      api_key_env: OPENAI_API_KEY
      temperature: 0.7
      max_tokens: 4096
    vision:
      model: openai/gpt-4o
      api_base: https://api.openai.com/v1/
      api_key_env: OPENAI_API_KEY

services:
  mineru:
    url: https://mineru-ai-model.apps.os.sewc.siemens.cn/file_parse
    parse_method: auto                            # auto / ocr / txt
    timeout: 120

agent:
  max_iterations: 20
  skills_dir: ./skills/user_skills
  memory_db: ./data/agent.db
```

### 5.2 `.env`（仅存放密钥）

```env
LLM_API_KEY=                  # 本地 vLLM 通常留空
VISION_API_KEY=
OPENAI_API_KEY=sk-xxx
```

**切换模型 Profile**：仅需修改 `config.yaml` 中的 `active_profile`，无需改代码。

---

## 6. 核心模块设计

### 6.1 Agent Loop（`agent/loop.py`）

```python
async def run(user_input: str, session_id: str):
    # 1. 从 DB 加载历史消息 + 记忆 + Skills 摘要，组装 messages
    messages = await context.assemble(user_input, session_id)
    # 2. 循环调用 LLM
    for _ in range(settings.agent["max_iterations"]):
        response = await llm_client.chat(messages, tools=tool_registry.schemas())
        await store.save_message(session_id, role="assistant", ...)  # 写 DB
        if response.finish_reason == "tool_calls":
            results = await tool_dispatcher.execute(response.tool_calls)
            await store.save_messages(session_id, results)           # 写 DB
            messages.extend(results)
        else:
            yield response.content   # 流式输出最终答案
            break
```

### 6.2 Skills 系统（SKILL.md 格式）

```markdown
---
name: daily_report
description: 截取屏幕/文档，OCR 识别，生成结构化日报
version: 1.0
---
## 触发条件
用户说"生成日报"、"截图汇报"、"分析工作状态"等

## 执行步骤
1. 调用 `screenshot_and_ocr` 截图并 OCR 识别内容
2. 调用 `analyze_screen` 通过视觉模型分析布局（可选）
3. LLM 综合内容生成 Markdown 格式日报
4. 将结果写入文件并通知用户
```

Skills 以摘要形式注入 System Prompt，LLM 识别意图后按步骤自动调用工具。

### 6.3 winpeekaboo 工具层（`tools/winpeekaboo.py`）

| 分类 | 工具函数 | 对应命令 |
|------|---------|---------|
| 屏幕捕获 | `capture_image(output, window, region)` | `winpeekaboo image` |
| UI元素识别 | `see_elements(window)` | `winpeekaboo see --json` |
| 鼠标点击 | `click(on, window, button)` | `winpeekaboo click` |
| 键盘输入 | `type_text(text, window, delay)` | `winpeekaboo type` |
| 按键/组合键 | `press_key(key)` / `hotkey(keys)` | `winpeekaboo press/hotkey` |
| 滚动/拖拽 | `scroll(direction, amount)` / `drag(from_, to)` | `winpeekaboo scroll/drag` |
| 窗口管理 | `window_activate/minimize/maximize/move/resize/close` | `winpeekaboo window *` |
| 应用管理 | `app_launch/quit/switch/list` | `winpeekaboo app *` |
| 资源列表 | `list_windows/apps/screens/elements` | `winpeekaboo list *` |

### 6.4 MinerU OCR 工具（`tools/ocr.py`）

**接口**：`POST /file_parse`（multipart/form-data，同步，返回 Markdown）

```python
@tool(description="对文件（PDF/PNG/JPG/DOCX）进行 OCR 解析，返回 Markdown 内容")
async def parse_file_to_markdown(file_path: str, parse_method: str = "auto") -> str:
    async with httpx.AsyncClient(timeout=cfg.timeout) as client:
        resp = await client.post(cfg.url, data={"parse_method": parse_method},
                                 files={"file": (name, open(file_path, "rb"))})
    return resp.json().get("markdown", "")

@tool(description="截取屏幕/窗口，通过 MinerU OCR 识别文字，返回 Markdown")
async def screenshot_and_ocr(window: str = None, region: str = None) -> str:
    tmp = capture_image(output=tmp_path, window=window, region=region)
    return await parse_file_to_markdown(tmp, parse_method="ocr")
```

### 6.5 视觉分析工具（`tools/vision.py`）

```python
@tool(description="截取屏幕或指定窗口，通过多模态视觉模型分析图像内容，返回描述")
async def analyze_screen(prompt: str, window: str = None, region: str = None) -> str:
    img_path = capture_image(output=tmp_path, window=window, region=region)
    b64 = base64.b64encode(Path(img_path).read_bytes()).decode()
    return await llm_client.vision(b64=b64, prompt=prompt)
```

**OCR vs Vision 场景选择**：

| 场景 | 工具 |
|------|------|
| 提取屏幕/图片中的文字 | `screenshot_and_ocr`（MinerU，结构化） |
| 理解 UI 状态/布局 | `analyze_screen`（vLLM Vision，描述性） |
| 解析 PDF/DOCX 文档 | `parse_file_to_markdown`（MinerU，保留格式） |

### 6.6 定时任务流程

```
用户: "每天早上9点自动截图检查工作状态"
  ↓
LLM 直接生成 tool call（无需独立 nl2cron 脚本）：
  create_job(cron="0 9 * * *", skill="daily_report",
             params={}, name="每天9点截图日报")
  ↓
写入 scheduled_jobs 表 + APScheduler SQLAlchemyJobStore
  ↓
程序重启 → APScheduler 从 SQLite 自动恢复所有 Job
  ↓
触发时 → job_runner 写 JobExecutionLog → 调用 Agent Loop → 更新日志
```

---

## 7. 数据库设计

**数据库**：单一 SQLite 文件 `data/agent.db`，5 张业务表 + APScheduler 内置表。

### 7.1 `sessions` — 会话表

```
id          TEXT PK    UUID，会话唯一标识
title       TEXT       首条消息前20字（自动生成）
created_at  DATETIME
updated_at  DATETIME
```

### 7.2 `messages` — 消息历史表

```
id            TEXT PK
session_id    TEXT FK → sessions.id
role          TEXT     user / assistant / tool / system
content       TEXT     文本内容
tool_calls    TEXT     JSON，[{id, name, arguments}]（role=assistant 且有工具调用时）
tool_call_id  TEXT     对应的 tool call id（role=tool 时）
tool_name     TEXT     工具名称（role=tool 时）
token_count   INT      可选，用于上下文窗口管理
created_at    DATETIME
```

### 7.3 `scheduled_jobs` — 定时任务表

```
id           TEXT PK
name         TEXT     用户可读描述
cron_expr    TEXT     如 "0 9 * * *"
skill_name   TEXT     触发的 skill 名称
params       TEXT     JSON 参数
status       TEXT     active / paused / deleted
created_at   DATETIME
last_run_at  DATETIME
next_run_at  DATETIME
run_count    INT      累计执行次数
last_result  TEXT     最近一次执行结果摘要
```

### 7.4 `job_execution_logs` — 任务执行日志表

```
id           TEXT PK
job_id       TEXT FK → scheduled_jobs.id
started_at   DATETIME
finished_at  DATETIME
status       TEXT     running / success / failed
result       TEXT     执行输出摘要
error        TEXT     错误信息（失败时）
session_id   TEXT     本次执行产生的会话 id（可追溯对话内容）
```

### 7.5 `agent_memory` — 跨会话记忆表

```
id                TEXT PK
key               TEXT UNIQUE   如 "user.preference.output_style"
value             TEXT          记忆内容
category          TEXT          fact / preference / context / skill_hint
source_session_id TEXT          来自哪个会话
created_at        DATETIME
updated_at        DATETIME
expires_at        DATETIME      可选过期时间
```

### 7.6 表关系图

```
sessions ──< messages
scheduled_jobs ──< job_execution_logs
agent_memory  (独立，全局)
apscheduler_jobs  (APScheduler 自动维护)
```

---

## 8. Python 依赖（`pyproject.toml`）

```toml
[project]
name = "desktop-agent"
version = "0.1.0"
requires-python = ">=3.11"
dependencies = [
    "litellm>=1.40",           # LLM 统一接口
    "rich>=13.0",              # 终端渲染
    "click>=8.1",              # CLI 框架
    "apscheduler>=3.10",       # 定时调度
    "sqlmodel>=0.0.19",        # ORM + SQLite
    "sqlalchemy>=2.0",         # APScheduler SQLAlchemyJobStore
    "httpx>=0.27",             # 异步 HTTP（MinerU 调用）
    "pyyaml>=6.0",             # config.yaml 解析
    "pydantic-settings>=2.0",  # .env 加载
    "winpeekaboo",             # 桌面自动化（本地安装）
]
```

---

## 9. 实现任务列表

### Task 1：项目骨架与配置体系
- 初始化 `pyproject.toml`，安装依赖
- 实现 `config/settings.py`（YAML + .env 合并加载，profile 切换）
- 创建 `config.yaml` 模板、`.env.example`
- 实现 CLI 入口 `cli/app.py`（`chat`/`jobs`/`skills` 等子命令）

### Task 2：数据库层
- 实现 `memory/models.py`（5 张 SQLModel 表定义）
- 实现 `memory/store.py`（CRUD：会话、消息、任务、记忆的读写操作）
- 实现数据库初始化（首次启动自动建表）

### Task 3：LLM 客户端层
- 实现 `llm/client.py`（LiteLLM 封装：流式对话 + function calling + 视觉调用）
- 对话模型和视觉模型各自读取当前 profile 配置

### Task 4：原子工具层
- 实现 `@tool` 装饰器（`tools/registry.py`，自动生成 OpenAI JSON Schema）
- 封装 winpeekaboo 全部命令（`tools/winpeekaboo.py`）
- 实现 MinerU OCR 工具（`tools/ocr.py`）
- 实现视觉分析工具（`tools/vision.py`）
- 实现系统工具（`tools/system.py`：文件操作、剪贴板、系统命令）

### Task 5：Agent Loop
- 实现 `agent/loop.py`（多轮工具调用 → 观察 → 继续，消息自动写 DB）
- 实现 `agent/context.py`（从 DB 加载历史 + 记忆摘要 + Skills 列表）
- 实现 `agent/tool_dispatcher.py`（工具调用分发与并发执行）

### Task 6：Skills 系统
- 实现 SKILL.md 解析器（`skills/parser.py`，frontmatter + 步骤提取）
- 实现 Skills 注册与加载（`skills/registry.py`，扫描 user_skills 目录）
- Skills 摘要注入 System Prompt

### Task 7：定时任务调度
- 实现 `scheduler/engine.py`（APScheduler + SQLAlchemyJobStore 持久化）
- 实现 `scheduler/job_runner.py`（执行前写 JobExecutionLog，执行后更新状态）
- 实现 Scheduler 管理工具（`tools/scheduler_tool.py`）：
  - `create_job(cron, skill_name, params, name)`
  - `list_jobs()` / `delete_job(job_id)` / `toggle_job(job_id, enabled)`

### Task 8：CLI 终端界面
- 实现 Rich 渲染组件（`cli/display.py`）：工具调用面板、流式输出、Spinner
- 实现交互式 REPL 模式（`/history`、`/skills`、`/jobs`、`/memory` 等内置命令）

---

## 10. 验证方式

| 验证项 | 步骤 |
|--------|------|
| 项目启动 | `python main.py`，验证 DB 自动建表、Skills 加载成功 |
| Profile 切换 | 修改 `config.yaml active_profile: cloud`，验证使用云端模型 |
| 基础对话 | 输入自然语言，验证 LLM 工具调用流程和流式输出 |
| OCR | 输入"识别屏幕文字"，验证 MinerU 接口调用和 Markdown 返回 |
| 视觉分析 | 输入"分析当前屏幕布局"，验证 vLLM Vision 调用 |
| 文件解析 | 输入"解析这个 PDF"，验证 `parse_file_to_markdown` 结构化输出 |
| 技能触发 | 添加自定义 SKILL.md，输入触发词，验证按步骤自动执行 |
| 消息持久化 | 对话后重启，输入 `/history`，验证历史正确加载 |
| 定时任务 | 输入"每分钟截图一次"，验证 LLM 生成 cron，kill 进程后重启任务自动恢复 |
| 执行日志 | 任务执行后查询 `job_execution_logs` 表，验证日志记录完整 |
| 跨会话记忆 | 告知 Agent 某项偏好，新会话验证记忆被注入 System Prompt |
