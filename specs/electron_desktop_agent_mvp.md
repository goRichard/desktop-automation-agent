# SEWC FlowPilot — Electron Desktop Automation MVP Spec

## 1. 文档状态

- 状态：Draft
- 目标平台：Windows 10/11
- 目标运行环境：长期登录、不锁屏的专用 Windows VM
- 桌面外壳：Electron
- 自动化运行时：Python
- 工作名称：SEWC FlowPilot

## 2. 产品目标

构建一个面向 Windows 桌面应用的自动化产品。用户可以通过对话创建任务、编辑
Skill、逐步调试流程，并将验证成功的 Skill 配置为定时任务。

产品以 UIA、浏览器 DOM、鼠标和键盘操作为主要执行方式，LLM 和视觉模型用于：

1. 根据用户需求生成 Skill 草稿。
2. 在首次运行时辅助识别界面和补全定位器。
3. 在步骤失败时分析原因并提出修复建议。
4. 在确定性定位方式失败时提供受控回退。

MVP 不以完整替代 UiPath、Power Automate 等平台为目标，而是先验证 Outlook、Teams
和浏览器场景下的有人值守调试与 VM 无人值守执行能力。

## 3. 已确认范围

### 3.1 首批应用

| 应用 | MVP 范围 | 识别方式 |
|---|---|---|
| Classic Outlook | 优先完整支持 | `OUTLOOK.EXE` |
| New Teams | 支持常用点击和输入流程 | `ms-teams.exe` + `MSTeams` MSIX 包 |
| Microsoft Edge | 优先支持导航、点击、输入和下载 | Playwright `msedge` channel |

New Outlook（`olk.exe`）不属于 MVP，但应用模型必须允许未来增加独立适配器。
Chrome 不属于第一批验收范围，但浏览器工具层不得使用 Edge 私有业务逻辑，以便后续
通过 Playwright channel 增加 Chrome。

### 3.2 执行环境

- 自动化运行在专用 Windows VM。
- Windows 用户会话必须保持登录。
- VM 不得自动锁屏、睡眠或休眠。
- 显示分辨率和缩放比例必须固定。
- 同一交互会话同一时间只允许一个桌面自动化 Run 操作 UI。
- Electron 窗口关闭不能中止已提交的无人值守任务。
- VM 重启后，运行时应能自动启动并恢复可恢复的 Task。

### 3.3 自动化边界

- Outlook 和 Teams 优先采用可见 UI 点击、输入和快捷键操作。
- 所有 Windows 桌面原子操作必须通过 WinPeekaboo 实现。WinPeekaboo 是产品运行时的
  固定基础组件，不是待替换的临时封装。
- 不使用 COM、VBA、Outlook Object Model 或脚本直接写入 Outlook/Teams 数据。
- 允许受控 PowerShell 步骤，但不向 Skill 暴露文件删除能力。
- 无人值守 Task 只能使用已发布且通过验证的 Skill 版本。

## 4. 非目标

MVP 暂不包含：

- New Outlook 自动化。
- Chrome 的正式兼容性验收。
- macOS、Linux 桌面自动化。
- Citrix、远程应用或被锁定的 Windows 会话。
- 多用户云端控制台和 RBAC。
- BPMN 编辑器。
- 在线 Skill Marketplace。
- 多 VM 集群调度。
- 完全任意、无审核的 PowerShell 脚本执行。

## 5. 系统架构

```text
Electron Renderer (React + TypeScript)
        │ IPC
Electron Main Process
        │ localhost HTTP/WebSocket + per-launch token
Python Runtime Service
        ├── Agent Runtime
        ├── Skill Runtime
        ├── Demo-to-Task Recorder
        ├── Task Scheduler
        ├── Tool Runtime
        │   ├── WinPeekaboo (Windows UIA / Win32 / Input / Screenshot)
        │   ├── Vision Model
        │   └── Playwright (Microsoft Edge)
        └── SQLite
```

### 5.1 Electron 职责

- 管理主窗口、托盘和通知。
- 启动、健康检查和关闭 Python Runtime。
- 渲染对话、Skill、Task 和 Run 页面。
- 提供本地文件选择、证书选择和设置管理入口。
- 通过 WebSocket 展示实时执行事件。

### 5.2 Python Runtime 职责

- 管理会话、Agent 推理和工具调用。
- 解析、校验、执行和版本化 Skill。
- 将成功 demo Run、用户反馈和执行记忆转换为 draft Skill。
- 执行 Cron Task。
- 管理桌面自动化互斥锁。
- 保存 Run、Step、截图、日志和用户反馈。
- 生成 Skill 修改建议，但不得自动覆盖已发布版本。

### 5.3 WinPeekaboo 原子层

WinPeekaboo 是桌面工具的唯一底层入口，负责：

- 屏幕和窗口截图。
- 窗口发现、激活、移动、调整大小和关闭。
- 应用启动、切换和退出。
- UIA 元素枚举和属性读取。
- 鼠标点击、拖放和滚动。
- 键盘输入、单键和组合键。

上层 Agent、Skill Runtime 和应用适配器不得直接调用 `pyautogui`、`pywinauto`、
Win32 输入 API 或其他平行桌面控制库。新增桌面能力时，先扩展 WinPeekaboo，再通过
结构化 Tool Adapter 暴露给 Runtime。

WinPeekaboo 的每次调用必须转换为统一结果：

```json
{
  "ok": true,
  "action": "ui.click",
  "data": {},
  "error": null,
  "artifacts": [],
  "durationMs": 120
}
```

当前 `tools/winpeekaboo.py` 的中文字符串返回值需要在迁移阶段改造成该结构，但现有
WinPeekaboo CLI/库调用方式可以继续复用。

### 5.4 进程模型

Python Runtime 应作为独立后台进程运行。Electron Main 负责启动和发现 Runtime，但
Renderer 刷新或 Electron 窗口关闭不应影响正在执行的 Run。

MVP 中 Runtime 仅监听 `127.0.0.1`。Electron 启动 Runtime 时生成一次性认证 Token，
所有 HTTP 和 WebSocket 请求必须携带该 Token。

### 5.5 基于当前仓库的最小改造

不进行一次性重写。保留当前可工作的 Python 模块，并建立清晰边界：

```text
desktop-automation-agent/
├── runtime/                 # 新增：本地 API、Run 状态机、事件流
│   ├── api/
│   ├── runs/
│   └── events/
├── agent/                   # 保留并收敛：LLM 决策与上下文
├── tools/                   # 保留：Tool Adapter
│   ├── winpeekaboo.py       # 必须保留，桌面原子能力入口
│   ├── browser.py           # 改为 Edge 优先
│   └── ...
├── skills/                  # 演进：解析器、Schema、版本与执行器
├── scheduler/               # 演进：Task/Cron，不直接持有 Agent 逻辑
├── memory/                  # 演进：领域模型和迁移
├── llm/                     # 演进：多 Provider 与 TLS
├── desktop/                 # 新增：Electron + React + TypeScript
├── config/
├── specs/
└── packaging/               # 新增：Windows 构建和安装配置
```

现有 `cli/` 在开发阶段保留为诊断入口，但不再作为产品主界面。Electron 通过 Runtime
API 调用能力，不直接 import Python 模块。

| 当前模块 | MVP 处理方式 |
|---|---|
| `agent/loop.py` | 保留推理循环，移除 UI 回调职责，接入 Run 状态机和 Event Bus |
| `agent/context.py` | 保留上下文组装，修复重复消息并增加 Token 预算 |
| `tools/winpeekaboo.py` | 必须保留，改为统一桌面 Tool Adapter |
| `tools/browser.py` | 保留 Playwright 逻辑，默认改用系统 Edge |
| `tools/vision.py` | 保留 UIA→语义→视觉回退，输出改为结构化定位结果 |
| `tools/registry.py` | 保留注册机制，补充参数描述、风险等级和权限元数据 |
| `skills/` | 兼容读取当前 Markdown Skill，新增 YAML v1 Schema 和版本执行器 |
| `scheduler/` | 保留 APScheduler，Task 固定引用已发布 Skill 版本 |
| `memory/` | 保留 SQLite/SQLModel，增加领域表和数据库迁移机制 |
| `llm/` | 保留当前客户端能力，重构为 Provider Adapter 和独立 TLS 配置 |
| `cli/` | 保留为开发、恢复和诊断入口 |

## 6. 核心领域模型

### 6.1 Skill

Skill 是可版本化的流程定义，包含输入参数、应用要求、执行步骤、验证规则和失败策略。

Skill 状态：

```text
draft -> testing -> validated -> published -> deprecated
```

- `draft`：可编辑，不能被无人值守 Task 使用。
- `testing`：正在单步调试。
- `validated`：至少有一次完整成功记录。
- `published`：固定版本，可被 Task 引用。
- `deprecated`：不再允许创建新 Task，历史 Run 仍可查看。

### 6.2 Task

Task 将固定版本的 Skill、输入参数和 Cron 计划绑定为无人值守任务。

Task 状态：

```text
enabled | paused | disabled
```

### 6.3 Run

Run 是一次 Skill 执行实例，保存输入、步骤状态、日志、截图、反馈和最终结果。

Run 状态：

```text
queued -> preparing -> running -> waiting_user
       -> paused -> succeeded | failed | cancelled
```

### 6.4 Step Run

每个步骤独立记录：

- 开始和结束时间。
- 实际使用的工具和参数。
- 定位器选择过程。
- 工具结构化返回值。
- 验证结果。
- 失败分类。
- 执行前后截图。
- 用户反馈。

## 7. Skill Schema

Skill 使用 YAML 作为持久化格式。Electron 编辑器提供表单视图和源码视图，两者编辑
同一个结构化模型。

```yaml
apiVersion: desktop-agent/v1alpha1
kind: Skill

metadata:
  id: send_outlook_email
  name: Send Outlook Email
  version: 1.0.0
  status: draft
  description: Send an email through Classic Outlook
  tags: [outlook, email]

applications:
  - id: outlook_classic
    process: OUTLOOK.EXE
    required: true

inputs:
  recipient:
    type: string
    required: true
  subject:
    type: string
    required: true
  body:
    type: string
    required: true
  attachments:
    type: array
    items: string
    required: false

execution:
  defaultMode: guided
  timeoutSeconds: 300
  steps:
    - id: launch_outlook
      name: Launch Classic Outlook
      action: app.launch
      with:
        process: OUTLOOK.EXE
      verify:
        type: window.exists
        timeoutSeconds: 20
        locator:
          process: OUTLOOK.EXE
      retry:
        maxAttempts: 2
      onFailure: stop

    - id: new_email
      name: Create a new email
      action: ui.click
      target:
        automationId: NewItemButton
        name: New Email
        controlType: Button
      fallback:
        type: agent
        allowedTools: [ui.inspect, ui.locate, ui.click, vision.locate]
      verify:
        type: window.exists
        locator:
          process: OUTLOOK.EXE
          nameContains: Message

    - id: fill_email
      name: Fill email fields
      action: agent
      instruction: Fill recipient, subject and body using the provided inputs.
      allowedTools: [ui.inspect, ui.click, ui.type, ui.key]

    - id: confirm_send
      name: Confirm sending
      action: user.confirm
      policy:
        skipWhen: unattendedApproved

    - id: send
      name: Send email
      action: ui.click
      target:
        name: Send
        controlType: Button
      risk: external_side_effect
      verify:
        type: window.closed
```

### 7.1 步骤类型

MVP 支持：

- `app.launch`
- `app.activate`
- `ui.inspect`
- `ui.locate`
- `ui.click`
- `ui.type`
- `ui.key`
- `ui.hotkey`
- `ui.actions`
- `ui.scroll`
- `ui.wait`
- `browser.navigate`
- `browser.click`
- `browser.type`
- `browser.wait`
- `file.read`
- `file.write`
- `powershell.runApproved`
- `agent`
- `condition`
- `user.confirm`
- `skill.call`

### 7.2 定位器优先级

1. `automationId`。
2. UIA 属性组合：`name + controlType + ancestor`。
3. 相对定位。
4. 浏览器 DOM locator。
5. LLM 从 UIA/DOM 候选元素中选择。
6. 视觉定位。
7. 暂停并请求用户选择目标。

固定坐标只能作为临时调试信息，不得作为发布版 Skill 的唯一定位器。

## 8. 执行模式

### 8.1 Step 模式

- 每一步执行前等待用户确认。
- 显示目标、工具参数和预期结果。
- 用于首次运行和失败修复。

### 8.2 Guided 模式

- 自动执行普通步骤。
- 高风险步骤、规则指定步骤或异常恢复时等待用户确认。
- 作为交互式执行的默认模式。

### 8.3 Unattended 模式

- 仅允许执行已发布 Skill 的固定版本。
- Task 参数必须完整且通过校验。
- 不允许出现运行时必填用户输入。
- 高风险步骤必须在 Task 发布时获得明确授权。
- 失败后按照重试策略执行，仍失败则保存证据并结束 Run。

## 9. 失败反馈与 Skill 优化

系统不得根据一次失败静默修改正式 Skill。优化流程为：

```text
Run 失败
  -> 保存 UIA 树、截图、工具参数和错误
  -> 用户标注失败原因或选择正确元素
  -> Agent 生成 Skill Patch 建议
  -> UI 展示字段级 Diff
  -> 创建新 Draft 版本
  -> Step 模式重新验证
  -> 用户发布新版本
```

### 9.1 失败分类

- 应用未安装或未运行。
- 窗口未出现。
- 定位器失效。
- 元素不可交互。
- 页面或窗口加载超时。
- 视觉识别不确定。
- 验证失败。
- 用户取消。
- 权限或策略拒绝。
- Runtime 或模型错误。

### 9.2 可学习信息

- 新的 Automation ID 或 UIA 属性组合。
- 有效的父子窗口层级。
- 文本别名和语言差异。
- 更合理的等待条件和超时时间。
- 应用版本与定位器的兼容关系。

所有学习结果必须绑定应用版本和 Skill 版本，且必须经用户确认后进入发布版本。

## 10. Task 与 Cron

```yaml
apiVersion: desktop-agent/v1alpha1
kind: Task

metadata:
  id: weekday_report_email
  name: Weekday report email

schedule:
  cron: "0 9 * * 1-5"
  timezone: Asia/Shanghai
  misfirePolicy: run_once
  maxConcurrentRuns: 1

skill:
  id: send_outlook_email
  version: 1.0.0

parameters:
  recipient: team@example.com
  subject: Daily report
  body: "{{ task.executionDate }} report"

execution:
  mode: unattended
  timeoutSeconds: 300
  retries: 1

permissions:
  externalSideEffectsApproved: true
```

Task 页面必须显示：

- Skill 和固定版本。
- Cron 表达式、人类可读说明和时区。
- 下一次运行时间。
- 启用状态。
- 输入参数。
- 超时和重试策略。
- 最近运行结果。
- 即时运行入口。

## 11. PowerShell 安全模型

MVP 不提供任意 `powershell.exe <string>` 工具。Skill 只能引用已审核脚本：

```yaml
- action: powershell.runApproved
  with:
    scriptId: collect_report_files
    parameters:
      directory: C:\Reports
```

要求：

- 脚本存放在受管理目录。
- 脚本以内容哈希或签名标识。
- 发布 Skill 时固定脚本版本。
- 参数必须符合脚本声明的 Schema。
- 禁止暴露文件删除工具。
- 保存脚本 ID、哈希、参数、输出和退出码。
- 无人值守模式不能执行未审核脚本。

由于任意 PowerShell 可以通过多种方式间接删除文件，系统不能同时承诺“任意脚本”与
“严格禁止删除”。严格禁止删除需要进一步采用 Windows 账户权限、ACL、WDAC/AppLocker
或隔离 VM 等操作系统级控制；不依赖字符串过滤实现。

## 12. Electron 页面

### 12.1 Chat / Run 页面

- 对话消息。
- 匹配到的 Skill。
- 当前计划和步骤状态。
- 实时工具调用和结构化结果。
- 当前截图。
- Step、Guided 模式选择。
- 暂停、继续、取消和确认操作。
- 将成功流程保存为 Skill。

### 12.2 Skill 页面

- Skill 列表、状态、版本和标签。
- 表单步骤编辑器。
- YAML 源码编辑器。
- 输入参数编辑器。
- 应用要求和定位器编辑器。
- 单步测试。
- 版本 Diff、发布和回滚。
- 历史成功率和失败类型。

### 12.3 Task 页面

- Task 列表和状态。
- Skill 固定版本选择。
- Cron 和时区编辑。
- 参数编辑。
- 权限授权。
- 下一次执行时间预览。
- 暂停、恢复、立即执行和历史记录。

### 12.4 Run History 页面

- Run 列表和筛选。
- 步骤时间线。
- 工具输入输出。
- 截图和 UIA 快照。
- 用户反馈。
- 生成 Skill 修复建议。

### 12.5 Settings 页面

- LLM 和视觉模型配置。
- API Key 安全存储。
- 内部 CA 证书选择和校验。
- 浏览器路径。
- VM 环境检查。
- 数据和截图保留策略。
- 受审核 PowerShell 脚本管理。

## 13. 本地 API

### 13.1 HTTP

```text
GET    /health
GET    /runtime/capabilities
GET    /runtime/environment
GET    /models
PUT    /models/{chat|vision}
PUT    /models/{chat|vision}/credential
DELETE /models/{chat|vision}/credential
POST   /models/{chat|vision}/health
POST   /certificates/import

GET    /skills
POST   /skills
GET    /skills/{id}/versions/{version}
PUT    /skills/{id}/versions/{version}
POST   /skills/{id}/versions/{version}/validate
POST   /skills/{id}/versions/{version}/publish

GET    /tasks
POST   /tasks
GET    /tasks/{id}
PUT    /tasks/{id}
POST   /tasks/{id}/enable
POST   /tasks/{id}/pause
POST   /tasks/{id}/run
GET    /tasks/{id}/executions

POST   /runs
GET    /runs/{id}
GET    /runs/{id}/evidence
POST   /runs/{id}/pause
POST   /runs/{id}/resume
POST   /runs/{id}/cancel
POST   /runs/{id}/confirm
POST   /runs/{id}/feedback
POST   /runs/{id}/propose-skill-patch

GET    /settings
PUT    /settings
```

### 13.2 WebSocket 事件

```text
run.queued
run.started
run.paused
run.waiting_user
run.confirmation_requested
run.confirmation_resolved
run.completed
run.failed
step.started
step.tool_called
step.tool_result
step.screenshot
step.validation
step.completed
step.failed
skill.patch_proposed
task.next_run_changed
```

每个事件必须包含 `eventId`、`timestamp`、`runId` 和单调递增的 `sequence`，使 UI
可以断线重连并恢复状态。

## 14. 持久化

SQLite 至少包含：

- `skills`
- `skill_versions`
- `tasks`
- `runs`
- `step_runs`
- `run_events`
- `artifacts`
- `user_feedback`
- `approved_scripts`
- `settings_metadata`

Skill YAML、截图和大型 UIA 快照可保存为文件 Artifact，数据库仅保存路径、哈希、类型
和保留时间。

## 15. 模型 Provider 与内部证书

### 15.1 Provider 类型

Runtime 通过统一 `ModelProvider` 接口支持：

| Provider | 用途 | 接口方式 |
|---|---|---|
| OpenAI | 公有 OpenAI 服务 | OpenAI API |
| OpenAI Compatible | vLLM、企业内部网关及其他兼容服务 | 可配置 `/v1` Base URL |
| Ollama | VM 本地或局域网 Ollama | Ollama API 或 OpenAI-compatible `/v1` |
| Azure OpenAI | 保留当前代码已有能力 | Azure endpoint + API version |

对话模型和视觉模型可以使用不同 Provider。Provider 必须声明并在保存配置时探测以下
能力：`chat`、`streaming`、`toolCalling`、`vision` 和 `jsonOutput`。

不支持 Tool Calling 的本地模型不能直接用于 Agent 主循环，只能用于文本生成、分类或
视觉分析等与其能力匹配的步骤。

### 15.2 配置示例

```yaml
models:
  chat:
    provider: openai_compatible
    model: Qwen3.6-35B-A3B-FP8
    baseUrl: https://internal-llm.example.com/v1
    apiKeySecret: llm/internal
    tls:
      verify: true
      caBundle: ${APP_DATA}/certificates/internal-ca.pem

  vision:
    provider: ollama
    model: qwen3-vl
    baseUrl: http://127.0.0.1:11434
```

UI 配置必须转换为受校验的领域对象，不能把任意字段直接透传给模型 SDK。

### 15.3 TLS 与证书

- LLM、视觉模型和内部更新服务分别支持自定义 CA Bundle。
- 配置中保存证书路径和指纹，不保存证书私钥。
- 启动时检查证书文件是否存在并进行连接测试。
- Python `httpx` 和 Electron 网络客户端分别加载对应 CA，不能只修改一个进程的信任链。
- 每个 Provider 可独立选择系统证书库或自定义 CA Bundle。
- 默认禁止 `verify: false`。开发模式临时关闭验证时必须显示警告，发布版 Task 不得使用
  关闭 TLS 验证的 Provider。
- API Key 和 Token 使用 Windows Credential Manager 或 Electron `safeStorage`，不得写入
  明文 YAML、SQLite 或日志。

### 15.4 Provider 健康检查

设置页面提供 TLS 握手、模型存在性、最小文本请求、Tool Calling 和视觉输入测试，并
显示响应耗时与脱敏错误信息。

## 16. Windows 打包、安装与运行

### 16.1 交付形态

最终产品交付为 Windows 安装包，用户不需要单独安装 Python、Node.js、Playwright 或
WinPeekaboo。

```text
SEWC-FlowPilot-Setup.exe
  ├── Electron application
  ├── Python Runtime executable
  ├── WinPeekaboo runtime/module
  ├── Python dependencies
  ├── database migrations
  ├── built-in Skill templates
  └── licenses and notices
```

Python Runtime MVP 优先采用 PyInstaller `onedir`，避免 `onefile` 每次启动解压带来的
启动延迟和原生依赖查找问题。Electron 使用 electron-builder；NSIS、MSIX 或企业软件
中心格式在内部分发渠道明确后确定。

### 16.2 运行目录

- 程序文件：安装目录，只读。
- 配置：`%LOCALAPPDATA%\SEWC\FlowPilot\config`。
- SQLite：`%LOCALAPPDATA%\SEWC\FlowPilot\data`。
- Skill：`%LOCALAPPDATA%\SEWC\FlowPilot\skills`。
- 日志和 Artifact：`%LOCALAPPDATA%\SEWC\FlowPilot\runs`。
- 自定义 CA：复制到受管理的应用数据目录并记录指纹。

Runtime 不得向安装目录写入数据库、截图或动态配置。

### 16.3 Microsoft Edge

- Playwright 默认使用系统 Edge 的 `msedge` channel。
- MVP 不下载或捆绑 Chromium。
- 启动时检查 Edge 版本与可用性。
- 浏览器 Profile 默认使用产品专属目录，避免破坏用户个人 Profile。
- 后续支持 Chrome 时只新增 browser channel，不改变 Skill 的通用浏览器步骤。

### 16.4 VM 与后台运行

Runtime 必须运行在已登录用户的交互会话中，不能作为 Session 0 Windows Service 执行
UI 自动化。Electron 可最小化到托盘；关闭主窗口时，已提交 Run 和 Cron Scheduler 继续
运行。VM 重启后的自动登录属于基础设施策略，Runtime 提供登录后自动启动能力。

### 16.5 构建约束

- Windows 构建必须在 Windows CI 或构建机完成。
- 锁定 Python、Node 和 WinPeekaboo 依赖版本。
- 安装包和内部更新包应支持代码签名。
- 生成第三方依赖和开源许可证清单。
- 不得把开发 `.env`、会话数据库、内部证书或历史截图打入安装包。
- 自动更新和内部分发不得依赖公共服务，但具体渠道暂不固定。

## 17. 可观测性与审计

每个 Run 必须可以回答：

- 谁或哪个 Task 发起了执行。
- 使用了哪个 Skill 版本。
- 输入参数是什么，敏感值需脱敏。
- 每一步调用了什么工具。
- 实际操作了哪个进程、窗口和元素。
- 哪一步失败，失败证据是什么。
- 用户是否批准了高风险操作。
- Skill 后续是否因该失败产生了新版本。

日志中不得记录 API Key、认证 Token、密码和未脱敏的敏感输入。

## 18. MVP 验收标准

### 18.1 产品闭环

用户能够：

1. 在 Electron 中创建或导入 Skill。
2. 编辑输入、步骤、定位器、验证和失败策略。
3. 使用 Step 模式调试 Skill。
4. 查看实时步骤、工具结果和截图。
5. 根据失败反馈生成并确认 Skill Patch。
6. 发布一个固定 Skill 版本。
7. 将发布版本配置成 Cron Task。
8. 在长期登录的 VM 中无人值守执行 Task。
9. 查看完整执行历史并回滚 Skill 版本。

### 18.2 首批场景

- Classic Outlook：创建并发送包含收件人、主题、正文和可选附件的邮件。
- Classic Outlook：搜索指定邮件并打开结果。
- New Teams：打开指定聊天并发送消息。
- Microsoft Edge：打开网页、填写表单并提交。
- Task：按 Cron 触发上述一个已发布 Skill。

### 18.3 可靠性

- 同一 VM 不发生两个桌面 Run 并发操作 UI。
- Runtime 重启后不会丢失 Task、Run 和事件记录。
- WebSocket 重连后 UI 能恢复当前 Run 状态。
- 定位失败时不会盲目点击未知坐标。
- 失败时至少保存截图、错误类型和最后一次定位信息。

## 19. 实施阶段

### Phase 0 — 当前代码整理

- 修复当前消息重复问题。
- 将 CLI 与 Agent Runtime 解耦。
- 统一工具结构化返回格式。
- 清理仓库中的数据库、历史消息、证书和编译产物。
- 补齐依赖声明和配置路径处理。
- 保持 WinPeekaboo 能力不变，在外层增加统一结构化结果 Adapter。

### Phase 1 — Runtime 基础

- 建立 Run、Step Run 和 Event 状态模型。
- 实现本地 HTTP/WebSocket 服务。
- 实现暂停、继续、取消和用户确认。
- 实现桌面执行互斥锁。
- 实现 OpenAI、OpenAI-compatible、Ollama 和 Azure Provider 配置与 TLS 健康检查。

### Phase 2 — Skill Runtime

- 定义并校验 Skill Schema。
- 实现确定性步骤、Agent 回退和验证策略。
- 实现 Draft、Validate、Publish 和版本回滚。
- 实现失败证据与 Skill Patch 建议。

### Phase 3 — Demo-to-Task Recorder

- 从成功 demo Run 中提取可复用步骤、输入参数和用户修正。
- 过滤观察工具、失败路径和一次性调试动作。
- 生成 draft Skill，并标记需要人工审查的坐标点击、视觉 fallback 和外部副作用。
- 在 Skill validate/publish 后生成 Task draft，Task 仍必须绑定 published 固定版本。

### Phase 4 — Electron MVP

- Chat / Run 页面。
- Skill 列表和编辑器。
- 实时事件和截图展示。
- Settings 页面。
- Windows `onedir` 开发构建和安装路径验证。

### Phase 5 — Task Scheduler

- Task 编辑和 Cron 预览。
- 无人值守运行前检查。
- Task 历史、重试和失败记录。

### Phase 6 — 首批应用稳定化

- Classic Outlook 应用适配器。
- New Teams 应用适配器。
- Microsoft Edge 浏览器适配器。
- 建立首批端到端基准 Skill。

### Phase 7 — Windows 发布

- 构建 Electron 安装包。
- 捆绑 Python Runtime 和 WinPeekaboo。
- 验证干净 Windows VM 安装和运行。
- 增加代码签名、许可证清单和升级迁移机制。

## 20. 产品命名

当前推荐工作名为 **SEWC FlowPilot**：

- `SEWC` 明确内部归属。
- `Flow` 表达可配置 Skill 和业务流程。
- `Pilot` 表达 Agent 辅助和逐步走向无人值守执行。

| 备选名称 | 定位 |
|---|---|
| SieFlow | 简短、产品化，但需要确认 `Sie` 商标和内部命名规范 |
| SEWC SkillPilot | 强调 Skill 编辑和执行 |
| SEWC DeskPilot | 强调 Windows 桌面操作 |
| SieAutomate | 含义直接，但差异化较弱 |

正式发布前需要完成企业命名和商标审核。代码包名、数据目录和 API namespace 在名称确认
前使用中性内部标识，避免后续大范围迁移。

## 21. 待确认事项

以下事项不阻塞 Phase 0，但应在相应阶段开始前确认：

1. 正式名称是否采用 SEWC FlowPilot。
2. Electron 技术栈是否固定为 React + TypeScript。
3. New Teams 第一批必须覆盖的具体业务流程。
4. VM 自动登录由产品提供说明，还是完全由企业基础设施负责。
5. 自动更新使用内部 HTTP、软件中心、Intune 还是其他企业分发渠道。
6. Skill 和 Task 是否需要中英文双语展示。
