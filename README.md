# Desktop Agent

> 基于 LLM 的 Windows 桌面自动化 Agent — 通过自然语言控制桌面应用、浏览器、定时任务。

---

## 项目概述

Desktop Agent 是一个运行在 Windows 上的**双模态桌面自动化 Agent**。它通过 LLM（大语言模型）驱动一个"思考-执行-观察"循环，将自然语言指令转化为对 Windows 桌面和浏览器的操作。

### 能力范围

| 模态 | 能力 | 技术栈 |
|------|------|--------|
| **桌面模态** | 启动应用、点击、输入、窗口管理 | UIA + win32 API + 多模态视觉模型 |
| **浏览器模态** | 网页导航、点击、填表、截图 | Playwright + 系统 Chrome |
| **定时任务** | Cron 表达式创建、持久化、自动恢复 | APScheduler + SQLAlchemyJobStore |
| **Skill 系统** | 触发词匹配的可复用自动化流程 | Markdown + YAML front matter |

---

## 快速开始

### 环境要求

- Windows 10/11 (23H2+)
- Python >= 3.11
- 系统 Chrome（用于浏览器自动化）
- 任意 OpenAI 兼容接口（vLLM / OpenAI / Azure）

### 安装

```powershell
git clone <repo-url>
cd desktop-agent

python -m venv .venv
.venv\Scripts\activate

pip install -e .
pip install playwright>=1.45
```

### 配置

项目根目录的 `config.yaml` 是主配置文件，`.env` 存放密钥：

```yaml
active_profile: azure   # local / cloud / azure

profiles:
  local:
    llm:
      model: Qwen3.6-35B
      api_base: http://your-vllm-server/v1
      api_key_env: LLM_API_KEY
    vision:
      model: Qwen3.6-35B
      api_base: http://your-vision-endpoint/v1
      api_key_env: VISION_API_KEY

agent:
  max_iterations: 100
  skills_dir: ./skills/user_skills
  memory_db: ./data/agent.db
```

`.env` 文件：

```bash
LLM_API_KEY=not-needed
VISION_API_KEY=not-needed
AZURE_OPENAI_API_KEY=sk-xxx
```

### 运行

```powershell
python main.py
```

进入 REPL 后输入自然语言指令：

```
> 打开计算器并计算 23 + 12
> 访问 https://github.com 并搜索 playwright
> 生成日报
> /help
```

---

## 项目架构

```
desktop-agent/
├── agent/                  # Agent 核心循环
│   ├── loop.py             #   主循环：LLM → 工具 → 观察 → 循环
│   ├── context.py          #   上下文组装（历史 + 记忆 + Skills）
│   ├── planner.py          #   任务计划数据模型（TaskPlan）
│   └── tool_dispatcher.py  #   工具调用分发器（并发执行）
├── cli/                    # CLI 界面
│   ├── app.py              #   Click CLI + prompt_toolkit REPL
│   └── display.py          #   Rich 格式输出
├── config/                 # 配置管理
│   └── settings.py         #   config.yaml + .env → Pydantic Settings
├── llm/                    # LLM 客户端
│   └── client.py           #   OpenAI SDK 封装（对话 + 视觉）
├── memory/                 # 持久化层（SQLite via SQLModel）
│   ├── models.py           #   数据模型
│   └── store.py            #   CRUD 接口
├── tools/                  # 工具层（43+ 个 Agent 可调用原子操作）
│   ├── registry.py         #   @tool 装饰器 + JSON Schema 生成
│   ├── vision.py           #   视觉分析 + UI 定位（UIA + LLM + VLM 三层）
│   ├── winpeekaboo.py      #   桌面键鼠 / 窗口 / 应用操作
│   ├── browser.py          #   Playwright 浏览器操作
│   ├── system.py           #   文件 / 命令 / 剪贴板
│   ├── actions.py          #   批量确定性操作（run_actions）
│   ├── scheduler_tool.py   #   定时任务管理
│   └── planner.py          #   计划生成
├── scheduler/              # 定时任务调度
│   ├── engine.py           #   APScheduler 引擎
│   └── job_runner.py       #   Job 执行器
├── skills/                 # Skill 系统
│   ├── parser.py           #   YAML front matter 解析
│   └── registry.py         #   注册 + 触发匹配（关键词+LLM语义）
├── winpeekaboo/            #   桌面控制底层库
├── config.yaml             # 主配置
├── pyproject.toml          # 项目元数据
└── main.py                 # 入口
```

---

## 核心处理流程

### Agent Loop 生命周期

```
User Input
    │
    ▼
Context Assembly ──────────────────────┐
  ├── System Prompt (含工具选择规则)     │
  ├── Skills 摘要 + 匹配 Skill 详细步骤  │
  ├── 跨会话记忆                        │
  └── 历史消息                          │
    │                                   │
    ▼                                   │
LLM.chat(messages, tools=schemas)       │
    │                                   │
    ├──有 tool_calls?                    │
    │   ├─▼                              │
    │   │  并发执行所有工具 (asyncio.gather)
    │   │      │
    │   │      ▼
    │   │  步骤验证 (选做)
    │   │  └─ LLM 生成预期描述
    │   │  └─ vision 模型截图验证
    │   │      │
    │   │      ▼
    │   │  结果 → messages → 继续循环──┘
    │
    └──无 tool_calls?
        └─▼
           流式输出最终回复 → 结束
```

### Plan-First 模式（Skill 匹配时）

```
用户输入 → Skill 触发词匹配 → LLM 生成步骤计划 → 用户确认 →
  → 已确认计划注入 system prompt（最高优先级约束）→ 严格按步骤执行 →
  → 每步后截图验证 → 失败上报用户
```

---

## 工具清单

当前 Agent 可调用 **43 个工具**，分为 6 大类：

### 视觉分析定位（vision.py）
`analyze_screen` / `analyze_image` / `parse_image_to_markdown` / `extract_text_from_image`
`find_element` / `batch_locate_elements` / `find_and_click` / `find_and_click_batch`

**定位策略**：UIA 优先 → LLM 语义匹配 → VLM 视觉兜底（三层降级）

### 桌面键鼠窗口（winpeekaboo.py）
`capture_image` / `click` / `scroll` / `drag` / `type_text` / `press_key` / `hotkey`
窗口管理：`window_activate` / `minimize` / `maximize` / `restore` / `close` / `move` / `resize`
应用管理：`app_launch` / `app_quit` / `app_switch`
查询：`list_windows` / `list_apps` / `list_screens` / `list_elements`

### 浏览器自动化（browser.py）
`browser_navigate` / `browser_get_state` / `browser_click` / `browser_type`
`browser_screenshot` / `browser_scroll` / `browser_go_back` / `browser_press_key` / `browser_close`

### 系统工具（system.py）
`sleep` / `read_file` / `write_file` / `list_dir` / `run_command` / `get_clipboard` / `set_clipboard`

### 批量编排（actions.py / planner.py / scheduler_tool.py）
`run_actions` / `create_plan` / `get_plan_status`
`create_job` / `list_scheduled_jobs` / `delete_job` / `toggle_job`

---

## Skill 系统

Skill 是用 Markdown + YAML front matter 定义的可复用自动化流程。

```markdown
---
name: daily_report
description: 截取屏幕内容生成结构化日报
triggers:
  - 生成日报
  - 工作日报
---

## 执行步骤
1. 调用 `capture_image` 截图
2. 调用 `parse_image_to_markdown` 提取文字
3. 生成结构化 Markdown 报告
4. 调用 `write_file` 保存
```

Skill 匹配策略：
1. **触发词匹配**（快速，零模型调用）
2. **LLM 语义匹配**（兜底）

---

## 当前已知问题与改进方向

### Agent 处理逻辑问题

以下是代码评审中发现的关键架构问题：

| 问题 | 描述 | 影响 |
|------|------|------|
| ~~**app_launch 返回结果缺少结构化窗口标题**~~ | ✅ 已修复 | `winpeekaboo.py` app_launch 自动发现窗口标题并返回 `window_title: xxx` |
| ~~**计划管理形同虚设**~~ | ✅ 已修复 | `loop.py` 接入了 TaskPlan 数据模型，支持步骤进度追踪 |
| ~~**验证结果污染工具返回值**~~ | ✅ 已修复 | `[屏幕观察]` 作为独立消息追加，不修改原始工具内容 |
| ~~**并发执行无依赖检查**~~ | ✅ 已修复 | `tool_dispatcher.py` 改为顺序执行，消除竞态条件 |
| ~~**run/run_stream 代码重复**~~ | ✅ 已修复 | `run()` 改为 `run_stream()` 的薄包装 |
| **无主动 Token 管理** | 仅被动检测 overflow 报错，无主动计数/压缩机制 | 长对话必然触发上下文溢出 |
| **工具参数无描述** | schema 生成不提取 docstring 中的参数说明 | LLM 只能靠参数名猜测用途 |

### 后续优化方向

1. **Plan 状态机落地** — 将 `TaskPlan` 接入 loop，实现步骤进度追踪、失败恢复、进度可视化
2. **验证机制精简** — 减少不必要的双 LLM 调用，仅关键操作后验证
3. **流式工具结果** — 工具结果逐步回流，LLM 边执行边推理
4. **Token 预算管理** — 主动监测 token 消耗，历史摘要压缩
5. **run/run_stream 合并** — 消除代码重复
6. **多模态支持** — 扩展到更多模型后端

---

## 内置命令

| 命令 | 功能 |
|------|------|
| `/help` | 显示帮助 |
| `/exit` `/quit` `/q` | 退出程序 |
| `/new` | 创建新会话 |
| `/clear` | 清屏 |
| `/history` | 查看当前会话历史 |
| `/sessions` | 列出最近会话 |
| `/skills` | 列出已加载的 Skills |
| `/jobs` | 查看定时任务 |
| `/memory` | 查看跨会话记忆 |
| `/tools` | 列出所有可用工具 |
| `/config` | 显示当前配置 |

---

## 常见问题

### Chrome 浏览器没有自动打开？

确保系统已安装 Google Chrome：
```
C:\Program Files\Google\Chrome\Application\chrome.exe
```

### /exit 退出时报 Event loop is closed？

Windows + Python 3.12 ProactorEventLoop 已知问题，已在 `sys.unraisablehook` 中静默处理，不影响功能。

### 中文 print 报 UnicodeEncodeError？

Agent 生成的 Python 脚本 print 语句须用英文，并在脚本首部加 `sys.stdout.reconfigure(encoding='utf-8')`。已在 system_prompt 中约束。

---

## 开发

```powershell
pip install -e ".[dev]"
ruff check .
pytest tests/
```
# Desktop Agent

> A Claude Code-style desktop automation agent for Windows.
> Control your desktop, automate browser tasks, and execute scheduled workflows — all via natural language.

## 项目概述

Desktop Agent 是一个运行在 Windows 上的**双模态桌面自动化 Agent**，类似 Claude Code 的工作方式：

- **桌面模态**：通过 UIA（UI Automation）+ 视觉模型，识别和控制任意 Windows 应用（计算器、记事本、Office、Teams 等）
- **浏览器模态**：基于 Playwright + Chrome，控制网页导航、点击、表单填写等
- **任务调度**：内置 APScheduler，支持 Cron 定时任务的创建、持久化与恢复
- **Skill 系统**：用户可定义可复用的自动化流程（如"生成日报"），Agent 自动匹配触发

核心能力：截屏 → 视觉分析 → 定位 UI 元素 → 执行操作 → 结果验证 → 下一轮决策。

---

## 项目架构

```
desktop-agent/
├── agent/                  # Agent 核心循环
│   ├── loop.py             #   主循环：LLM 调用 → 工具分发 → 观察 → 循环
│   ├── context.py          #   上下文组装（历史 + 记忆 + Skills）
│   ├── planner.py          #   任务计划模型（TaskPlan / TaskStep）
│   └── tool_dispatcher.py  #   工具调用分发器（并发执行）
├── cli/                    # CLI 界面
│   ├── app.py              #   Click CLI + prompt_toolkit REPL
│   └── display.py          #   Rich 格式输出
├── config/                 # 配置管理
│   └── settings.py         #   Pydantic Settings（合并 config.yaml + .env）
├── llm/                    # LLM 客户端
│   └── client.py           #   OpenAI SDK 封装（对话 + 视觉）
├── memory/                 # 持久化层（SQLite）
│   ├── models.py           #   SQLModel 数据模型（Session / Message / Memory / Job）
│   └── store.py            #   增删改查接口
├── tools/                  # 工具层（Agent 可调用原子操作）
│   ├── registry.py         #   @tool 装饰器 + OpenAI JSON Schema 自动生成
│   ├── vision.py           #   视觉分析：截图 / OCR / 视觉分析 / UI 定位（UIA+LLM+VLM 三层）
│   ├── winpeekaboo.py      #   桌面操作：键鼠 / 窗口管理 / 应用启动
│   ├── browser.py          #   浏览器操作：导航 / 点击 / 输入 / 截图（Playwright）
│   ├── system.py           #   系统工具：文件读写 / 命令执行 / 剪贴板 / 等待
│   ├── actions.py          #   批量操作：一次调用执行多个确定性动作
│   ├── scheduler_tool.py   #   调度器工具：创建/删除/查询/启停定时任务
│   └── planner.py          #   计划生成工具：拆解目标为可执行步骤
├── scheduler/              # 任务调度
│   ├── engine.py           #   APScheduler 引擎（SQLAlchemy JobStore）
│   └── job_runner.py       #   Job 执行器（调用 Agent 完成 Skill）
├── skills/                 # Skill 系统
│   ├── parser.py           #   YAML front matter 解析
│   ├── registry.py         #   注册表 + 触发匹配
│   └── user_skills/        #   用户定义的 Skill 文件
├── winpeekaboo/            # 桌面键鼠控制库（Python + win32 API）
├── config.yaml             # 主配置文件
├── pyproject.toml          # 项目元数据 + 依赖
└── main.py                 # 程序入口
```

---

## 环境要求

| 组件 | 要求 |
|------|------|
| 操作系统 | Windows 10/11 (23H2+) |
| Python | >= 3.11 |
| 浏览器 | Google Chrome（系统已安装，用于浏览器自动化） |
| LLM 服务 | 任意 OpenAI 兼容接口（vLLM / OpenAI / Azure 等） |

---

## 安装

```powershell
# 1. 克隆项目
git clone <repo-url>
cd desktop-agent

# 2. 创建虚拟环境
python -m venv .venv
.venv\Scripts\activate

# 3. 安装依赖
pip install -e .

# 4. 安装 Playwright（使用系统 Chrome，无需下载 Chromium）
pip install playwright>=1.45
```

---

## 环境配置

### 1. API 密钥（.env）

在项目根目录创建 `.env` 文件：

```bash
# 内网 vLLM 部署（通常不需要真实 key，填任意字符串即可）
LLM_API_KEY=not-needed
VISION_API_KEY=not-needed

# 云端 OpenAI（可选）
OPENAI_API_KEY=sk-xxxx
```

### 2. 模型配置（config.yaml）

```yaml
active_profile: local   # 使用 local 或 cloud

profiles:
  local:                # 内网 vLLM 部署
    llm:
      model: Qwen3.6-35B
      api_base: http://your-vllm-server/api/.../v1
      api_key_env: LLM_API_KEY
      temperature: 0.7
      max_tokens: 4096
    vision:
      model: Qwen3.6-35B
      api_base: http://your-vllm-server/api/.../v1
      api_key_env: VISION_API_KEY

  cloud:                # 云端 OpenAI
    llm:
      model: gpt-4o
      api_base: https://api.openai.com/v1/
      api_key_env: OPENAI_API_KEY
    vision:
      model: gpt-4o
      api_key_env: OPENAI_API_KEY

agent:
  max_iterations: 100           # 最大工具调用轮数
  skills_dir: ./skills/user_skills
  memory_db: ./data/agent.db
  system_prompt: |
    你是一个强大的 Windows 桌面自动化 Agent...
```

### 3. 配置项说明

| 配置项 | 默认值 | 说明 |
|--------|--------|------|
| `active_profile` | `local` | 当前使用的 Profile |
| `agent.max_iterations` | `100` | 单次任务最大 LLM 调用轮数 |
| `agent.skills_dir` | `./skills/user_skills` | Skill 文件目录 |
| `agent.memory_db` | `./data/agent.db` | SQLite 数据库路径 |
| `agent.system_prompt` | (内置) | Agent 系统提示词 |

---

## 运行

### 启动 REPL 交互模式

```powershell
python main.py
# 或
desktop-agent
```

进入后输入自然语言指令即可：

```
> 打开计算器并计算 23 + 12
> 访问 https://github.com 并搜索 playwright
> 生成日报
> /help          # 查看内置命令
> /exit          # 退出
```

### 内置斜杠命令

| 命令 | 功能 |
|------|------|
| `/help` | 显示帮助 |
| `/exit`, `/quit`, `/q` | 退出程序 |
| `/new` | 创建新会话 |
| `/clear` | 清屏 |
| `/history` | 查看当前会话历史 |
| `/sessions` | 列出最近会话 |
| `/skills` | 列出已加载的 Skills |
| `/jobs` | 查看定时任务 |
| `/memory` | 查看跨会话记忆 |
| `/tools` | 列出所有可用工具 |
| `/config` | 显示当前配置 |

---

## 工具清单

当前 Agent 可调用共计 **43 个工具**，分为 6 大类。

### 视觉分析与定位（vision.py）

智能地通过多模态视觉模型理解屏幕内容、定位 UI 元素并执行精准点击。

| 工具 | 说明 |
|------|------|
| `analyze_screen` | 截取当前屏幕/窗口，提交视觉模型分析界面内容（UI 布局、操作状态），返回文字描述 |
| `analyze_image` | 对指定图片文件调用视觉模型进行分析，支持 PNG/JPG/WEBP 等格式 |
| `parse_image_to_markdown` | 将图片中的文字/表格/结构化信息提取为 Markdown（替代传统 OCR） |
| `extract_text_from_image` | 从图片中提取纯文字内容，不保留格式（适合简单 OCR 场景） |
| `find_element` | 定位窗口中的 UI 元素位置，返回名称、类型、坐标等结构化信息，**不执行点击** |
| `batch_locate_elements` | 一次 UIA 扫描批量定位多个元素坐标，支持 automation_id 精确匹配，返回所有坐标后再逐步操作 |
| `find_and_click` | 🔑 三层定位 + 点击：UIA 优先 → LLM 语义匹配 → VLM 视觉兜底。支持 automation_id 确定性匹配（零模型调用）。点击后自动检测并激活新弹出窗口 |
| `find_and_click_batch` | 批量定位并按顺序点击多个元素（如计算器按键），一次 UIA 扫描 + VLM 批量兜底 |

> **定位策略**：`find_and_click` / `find_element` / `batch_locate_elements` 统一采用「UIA + LLM + VLM」三层降级——当 UIA 元素树可用时通过 automation_id 或语义匹配精确定位（零/一次模型调用），仅在 UIA 失效时降级为截图 + 视觉模型识别坐标。

### 桌面键鼠与窗口控制（winpeekaboo.py）

封装 winpeekaboo 底层库，提供完整的桌面原子操作能力。

| 工具 | 说明 |
|------|------|
| `capture_image` | 截取屏幕/窗口/区域截图保存到文件，指定窗口时自动激活前置确保截图正确 |
| `click` | 点击屏幕坐标或 UI 元素名称，支持左/右/中键 |
| `scroll` | 滚动桌面鼠标滚轮（适用于记事本、Office、资源管理器等非浏览器窗口），方向 up/down/left/right |
| `drag` | 拖放操作：起点 → 终点（坐标或元素名） |
| `type_text` | 在目标窗口中输入文字，支持自定义打字延迟模拟人工输入 |
| `press_key` | 按下单个键：Enter / Escape / Tab / F1-F12 / 方向键 等 |
| `hotkey` | 执行组合键：Ctrl+C、Ctrl+Shift+T、Alt+F4 等 |

**窗口管理**

| 工具 | 说明 |
|------|------|
| `window_activate` | 激活/聚焦指定窗口，将其置于前台 |
| `window_minimize` | 最小化指定窗口 |
| `window_maximize` | 最大化指定窗口 |
| `window_restore` | 还原窗口（从最大化/最小化恢复） |
| `window_close` | 关闭指定窗口 |
| `window_move` | 移动窗口到屏幕指定坐标 |
| `window_resize` | 调整窗口宽度和高度 |

**应用与资源列表**

| 工具 | 说明 |
|------|------|
| `app_launch` | 启动 Windows 应用程序（如 notepad.exe），支持命令行参数和等待启动完成 |
| `app_quit` | 关闭/退出指定应用 |
| `app_switch` | 切换到指定应用（激活其窗口） |
| `list_windows` | 列出所有打开的窗口（标题、句柄等），返回 JSON，支持过滤 |
| `list_apps` | 列出所有正在运行的应用程序，返回 JSON |
| `list_screens` | 列出所有显示器信息（分辨率、位置等），返回 JSON |
| `list_elements` | 列出窗口的所有 UIA 可交互元素（含 name、control_type、automation_id、bounds），用于发现 automation_id 以进行确定性匹配 |

### 浏览器自动化（browser.py）

基于 Playwright + 系统 Chrome，自动管理浏览器生命周期（首次调用自启动、上下文自愈）。

| 工具 | 说明 |
|------|------|
| `browser_navigate` | 打开指定 URL（首次调用自动启动 Chrome，非 headless 用户可见） |
| `browser_get_state` | 获取当前页面标题、URL、可交互 DOM 元素列表（含编号、角色、文本） |
| `browser_click` | 🔑 三层点击：Playwright 定位器 → DOM 扫描 + LLM 语义匹配 → VLM 视觉兜底。操作前自动等待页面加载 |
| `browser_type` | 在输入框中输入文字，优先通过 Playwright 定位器（label/placeholder/role），支持填前清空 |
| `browser_screenshot` | 截取当前浏览器页面截图，返回文件路径 |
| `browser_scroll` | 滚动页面：up / down / top / bottom |
| `browser_go_back` | 返回上一页 |
| `browser_press_key` | 在浏览器中按下键盘按键（Enter、Tab、Escape、ArrowDown 等） |
| `browser_close` | 关闭浏览器并释放所有资源 |

### 系统工具（system.py）

文件操作、命令执行、剪贴板访问等通用系统能力。

| 工具 | 说明 |
|------|------|
| `sleep` | 等待指定秒数（最大 30s），用于 UI 操作间等待响应 |
| `read_file` | 读取文本文件内容（UTF-8），文件不存在时返回错误 |
| `write_file` | 将文本写入文件，支持追加模式，父目录自动创建 |
| `list_dir` | 列出目录中的文件/子目录，支持 glob 过滤（如 `*.txt`） |
| `run_command` | 执行 PowerShell 命令，超时 30s |
| `get_clipboard` | 获取剪贴板中的文字内容 |
| `set_clipboard` | 将文字写入剪贴板 |

### 批量操作与任务编排（actions.py + planner.py + scheduler_tool.py）

| 工具 | 来源 | 说明 |
|------|------|------|
| `run_actions` | actions.py | 批量执行确定性操作（click → type_text → press_key → sleep），一次调用完成多步，减少 LLM 往返 |
| `create_plan` | planner.py | 调用 LLM 将复杂目标自动拆解为可执行步骤列表（如"发送 Outlook 邮件"拆为 5-8 步） |
| `get_plan_status` | planner.py | 查看当前任务计划的执行进度和状态 |
| `create_job` | scheduler_tool.py | 创建 Cron 定时任务（如"每天 9 点生成日报"） |
| `list_scheduled_jobs` | scheduler_tool.py | 列出所有定时任务及上次执行时间 |
| `delete_job` | scheduler_tool.py | 删除指定定时任务（不可恢复） |
| `toggle_job` | scheduler_tool.py | 暂停/恢复指定定时任务 |

---

## Skill 系统

Skill 是用 Markdown + YAML front matter 定义的可复用自动化流程。Agent 根据用户输入自动匹配并执行对应 Skill。

### 示例：daily_report.skill.md

```markdown
---
name: daily_report
description: 截取屏幕截图，通过 OCR 识别内容，生成结构化日报
version: 1.0
triggers:
  - 生成日报
  - 截图汇报
  - 工作日报
---

## 执行步骤
1. 调用 `capture_image` 截图
2. 调用 `parse_image_to_markdown` 提取文字
3. 生成结构化 Markdown 报告
4. 调用 `write_file` 保存到 `./reports/`
```

当用户说"生成日报"时，Agent 自动加载此 Skill 并按步骤执行。

### 创建自定义 Skill

在 `skills/user_skills/` 目录下创建 `your_skill.skill.md`，格式同上。Agent 启动时自动加载。

---

## 定时任务调度

支持创建 Cron 定时任务，任务在重启后自动恢复。

```python
# Agent 对话中：
"每天早上 9 点生成日报"
"每小时检查一次 Outlook 收件箱"
```

Agent 会调用 `create_job` 工具创建定时任务，底层使用 APScheduler + SQLAlchemy JobStore 持久化到 SQLite。

---

## 后续任务安排

### Phase 1：定时任务增强（进行中）

- [ ] **基于用户任务定义的 Schedule Job 配置**：支持用户在 Skill 文件中声明 schedule（cron 表达式），Agent 启动时自动注册为定时任务
- [ ] Job 执行日志与通知：任务执行结果推送给用户（桌面通知 / Teams / 邮件）
- [ ] Job 失败重试策略配置

### Phase 2：安全加固（计划中）

- [ ] **命令执行白名单**：`run_command` 工具限制可执行的命令范围，防止注入攻击
- [ ] **操作确认机制**：高危操作（删除文件、发送邮件、修改系统配置）要求用户二次确认
- [ ] **文件访问沙箱**：限制 `read_file` / `write_file` 的可访问路径范围
- [ ] **工具权限分级**：为每个工具标注风险等级（safe / warning / dangerous），Agent 决策时参考
- [ ] **Agent 操作审计日志**：记录所有工具调用及参数，支持事后审查
- [ ] **LLM 输出校验**：对 Agent 生成的命令/脚本做语法和安全检查

### Phase 3：体验优化（探索中）

- [ ] 上下文自动压缩（长对话 token 超限前自动摘要历史）
- [ ] 多显示器支持优化
- [ ] 操作录制与回放（Record & Replay）
- [ ] Web Dashboard（监控 Agent 状态、查看历史、管理 Job）

---

## 开发

```powershell
# 安装开发依赖
pip install -e ".[dev]"

# 代码检查
ruff check .

# 运行测试
pytest tests/
```

---

## 常见问题

### Chrome 浏览器没有自动打开？

确保系统已安装 Google Chrome，路径为：
```
C:\Program Files\Google\Chrome\Application\chrome.exe
```

如果 Edge 被 IT 策略禁用了 DevTools 远程调试（`DevTools remote debugging is disallowed`），项目已自动切换为 Chrome。详见 `tools/browser.py` 中 `executable_path` 配置。

### /exit 退出时报 Event loop is closed？

这是 Windows + Python 3.12 ProactorEventLoop 的已知问题，不影响功能。v0.1.0+ 已通过 `sys.unraisablehook` 静默处理。

### 中文 print 报 UnicodeEncodeError？

Agent 生成的 Python 脚本 print 语句须用英文，并在首部加 `sys.stdout.reconfigure(encoding='utf-8')`。已在 system_prompt 中约束。
## 开发

```powershell
# 安装开发依赖
pip install -e ".[dev]"

# 代码检查
ruff check .

# 运行测试
pytest tests/
```

---

## 常见问题

### Chrome 浏览器没有自动打开？

确保系统已安装 Google Chrome，路径为：
```
C:\Program Files\Google\Chrome\Application\chrome.exe
```

如果 Edge 被 IT 策略禁用了 DevTools 远程调试（`DevTools remote debugging is disallowed`），项目已自动切换为 Chrome。详见 `tools/browser.py` 中 `executable_path` 配置。

### /exit 退出时报 Event loop is closed？

这是 Windows + Python 3.12 ProactorEventLoop 的已知问题，不影响功能。v0.1.0+ 已通过 `sys.unraisablehook` 静默处理。

### 中文 print 报 UnicodeEncodeError？

Agent 生成的 Python 脚本 print 语句须用英文，并在首部加 `sys.stdout.reconfigure(encoding='utf-8')`。已在 system_prompt 中约束。

### Phase 2：安全加固（计划中）

- [ ] **命令执行白名单**：`run_command` 工具限制可执行的命令范围，防止注入攻击
- [ ] **操作确认机制**：高危操作（删除文件、发送邮件、修改系统配置）要求用户二次确认
- [ ] **文件访问沙箱**：限制 `read_file` / `write_file` 的可访问路径范围
- [ ] **工具权限分级**：为每个工具标注风险等级（safe / warning / dangerous），Agent 决策时参考
- [ ] **Agent 操作审计日志**：记录所有工具调用及参数，支持事后审查
- [ ] **LLM 输出校验**：对 Agent 生成的命令/脚本做语法和安全检查

### Phase 3：体验优化（探索中）

- [ ] 上下文自动压缩（长对话 token 超限前自动摘要历史）
- [ ] 多显示器支持优化
- [ ] 操作录制与回放（Record & Replay）
- [ ] Web Dashboard（监控 Agent 状态、查看历史、管理 Job）

---

## 开发

```powershell
# 安装开发依赖
pip install -e ".[dev]"

# 代码检查
ruff check .

# 运行测试
pytest tests/
```

---

## 常见问题

### Chrome 浏览器没有自动打开？

确保系统已安装 Google Chrome，路径为：
```
C:\Program Files\Google\Chrome\Application\chrome.exe
```

如果 Edge 被 IT 策略禁用了 DevTools 远程调试（`DevTools remote debugging is disallowed`），项目已自动切换为 Chrome。详见 `tools/browser.py` 中 `executable_path` 配置。

### /exit 退出时报 Event loop is closed？

这是 Windows + Python 3.12 ProactorEventLoop 的已知问题，不影响功能。v0.1.0+ 已通过 `sys.unraisablehook` 静默处理。

### 中文 print 报 UnicodeEncodeError？

Agent 生成的 Python 脚本 print 语句须用英文，并在首部加 `sys.stdout.reconfigure(encoding='utf-8')`。已在 system_prompt 中约束。

### Phase 2：安全加固（计划中）

- [ ] **命令执行白名单**：`run_command` 工具限制可执行的命令范围，防止注入攻击
- [ ] **操作确认机制**：高危操作（删除文件、发送邮件、修改系统配置）要求用户二次确认
- [ ] **文件访问沙箱**：限制 `read_file` / `write_file` 的可访问路径范围
- [ ] **工具权限分级**：为每个工具标注风险等级（safe / warning / dangerous），Agent 决策时参考
- [ ] **Agent 操作审计日志**：记录所有工具调用及参数，支持事后审查
- [ ] **LLM 输出校验**：对 Agent 生成的命令/脚本做语法和安全检查

### Phase 3：体验优化（探索中）

- [ ] 上下文自动压缩（长对话 token 超限前自动摘要历史）
- [ ] 多显示器支持优化
- [ ] 操作录制与回放（Record & Replay）
- [ ] Web Dashboard（监控 Agent 状态、查看历史、管理 Job）

---

## 开发

```powershell
# 安装开发依赖
pip install -e ".[dev]"

# 代码检查
ruff check .

# 运行测试
pytest tests/
```

---

## 常见问题

### Chrome 浏览器没有自动打开？

确保系统已安装 Google Chrome，路径为：
```
C:\Program Files\Google\Chrome\Application\chrome.exe
```

如果 Edge 被 IT 策略禁用了 DevTools 远程调试（`DevTools remote debugging is disallowed`），项目已自动切换为 Chrome。详见 `tools/browser.py` 中 `executable_path` 配置。

### /exit 退出时报 Event loop is closed？

这是 Windows + Python 3.12 ProactorEventLoop 的已知问题，不影响功能。v0.1.0+ 已通过 `sys.unraisablehook` 静默处理。

### 中文 print 报 UnicodeEncodeError？

Agent 生成的 Python 脚本 print 语句须用英文，并在首部加 `sys.stdout.reconfigure(encoding='utf-8')`。已在 system_prompt 中约束。
