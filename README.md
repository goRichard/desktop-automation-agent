# SEWC FlowPilot

基于 LLM 和 WinPeekaboo 的 Windows 桌面自动化 Agent。当前仓库是 Python Runtime
原型，后续通过 Electron 封装为独立 Windows 应用。

产品设计见 [Electron MVP Spec](specs/electron_desktop_agent_mvp.md)。

准备在 Windows 实机或 VM 上运行时，请先阅读
[Windows 运行与测试指南](docs/WINDOWS_RUNBOOK.md)。该文档包含环境准备、模型配置、
CLI/Runtime API 启动、WinPeekaboo 原子操作验证、Outlook/Edge/Teams 验收项和问题反馈模板。

## 当前能力

- Windows 桌面：通过 WinPeekaboo 启动应用、管理窗口、枚举 UIA 元素以及执行键鼠操作。
- 视觉定位：统一解析 WinPeekaboo UIA，候选评分优先，歧义时由 LLM 返回唯一元素 key，
  最后才使用视觉模型兜底；显式 AutomationId 未命中时严格失败。
- 浏览器：通过 Playwright 控制系统 Microsoft Edge。
- Agent Loop：统一支持 OpenAI、OpenAI-compatible、Ollama 和 Azure OpenAI Provider。
- 模型设置：支持运行时切换、健康检查、受管 CA 和 Windows Credential Manager 密钥。
- Skill：支持版本化 YAML Schema、Markdown 兼容导入、生命周期管理和确定性步骤执行。
- Classic Outlook：提供基于 WinPeekaboo 的应用 Adapter，确定性完成启动、写信、填写、
  附件和发送；附件使用 `Alt+N → A → F → Browse This PC`，并适配主题导致的写信窗口
  标题变化。标准附件对话框完全使用 UIA，不调用 Chat/Vision 模型；只有 Adapter 整体失败
  后才进入受限 Agent fallback。
- 调度：通过 APScheduler 和 SQLite 持久化 Cron Task。
- 历史：通过 SQLModel/SQLite 保存会话、消息、任务和执行日志。

MVP 优先适配 Classic Outlook、New Teams 和 Microsoft Edge。New Outlook 和 Chrome
暂不属于第一批验收范围。

## 运行要求

- Windows 10/11
- Python 3.11+
- Microsoft Edge
- WinPeekaboo（当前为项目必需的桌面原子操作运行时）
- OpenAI、Azure OpenAI、OpenAI-compatible 或后续 Ollama 模型服务

桌面自动化必须运行在已登录且未锁屏的交互式 Windows 会话中。无人值守模式建议运行
在固定分辨率、禁止睡眠和锁屏的专用 VM。

## 安装

```powershell
python -m venv .venv
.venv\Scripts\activate
pip install -e .
```

WinPeekaboo 当前不在公共 PyPI 中，需要按内部开发环境的方式安装或加入 Python 环境。
正式 Windows 构建会将它和 Python Runtime 一起打包。

## 配置

复制密钥模板：

```powershell
Copy-Item .env.example .env
```

模型、证书和 Agent 参数位于 `config.yaml`。密钥只放在 `.env`，不得提交到 Git。

支持指定其他配置文件：

```powershell
python main.py --config C:\FlowPilot\config.yaml
```

相对的 Skill、数据库和证书路径均以配置文件所在目录为基准。

浏览器默认配置：

```yaml
browser:
  channel: msedge
```

模型 Provider 支持 `openai`、`openai_compatible`、`ollama` 和 `azure_openai`。
Chat 与 Vision 可以选择不同 Provider。内部 CA Bundle 按模型独立配置：

```yaml
profiles:
  local:
    models:
      chat:
        provider: openai_compatible
        model: your-model
        baseUrl: https://internal-model.example.com/v1
        apiKeyEnv: LLM_API_KEY
        tls:
          verify: true
          caBundle: ./internal-ca.pem
```

CA 文件不存在或指纹不匹配时配置加载失败，不会静默降级到系统证书。`apiKeyEnv` 仅作为
当前开发阶段的密钥来源；Electron 版本将通过 Windows Credential Manager/safeStorage
解析 `apiKeySecret`，密钥不会由 `/models` 接口返回。
`tls.verify: false` 默认拒绝；仅开发环境显式设置
`FLOWPILOT_ALLOW_INSECURE_TLS=1` 后可临时启用，且不得用于无人值守任务。

Windows 正式运行时使用 Credential Manager 保存模型密钥。配置中只保留引用：

```yaml
apiKeySecret: models/chat
```

修改 Provider 或密钥时 Runtime 会重建客户端；存在执行中的 Run 时会拒绝修改。

## 启动

```powershell
python main.py
```

或安装后使用：

```powershell
desktop-agent
```

启动供 Electron 使用的本地 Runtime API：

```powershell
$env:FLOWPILOT_RUNTIME_TOKEN = "replace-with-a-random-token"
flowpilot-runtime
```

Runtime 仅监听 `127.0.0.1:8765`。除 `/health` 外，HTTP 请求需要通过
`X-Runtime-Token` 或 Bearer Token 鉴权；WebSocket 事件地址为 `/events`。

核心接口：

```text
POST /runs
GET  /runs
GET  /runs/{id}
GET  /runs/{id}/events
GET  /runs/{id}/evidence
POST /runs/{id}/pause
POST /runs/{id}/resume
POST /runs/{id}/confirm
POST /runs/{id}/cancel
WS   /events
POST /skills
GET  /skills
GET  /skills/{id}
GET  /skills/{id}/versions/{version}
PUT  /skills/{id}/versions/{version}
POST /skills/{id}/versions/{version}/validate
POST /skills/{id}/versions/{version}/publish
POST /skills/{id}/versions/{version}/deprecate
GET  /models
PUT  /models/{chat|vision}
PUT  /models/{chat|vision}/credential
DELETE /models/{chat|vision}/credential
POST /models/{chat|vision}/health?probe=configuration|models|request|tool_calling|vision
POST /certificates/import
GET  /tasks
POST /tasks
GET  /tasks/{id}
PUT  /tasks/{id}
POST /tasks/{id}/enable
POST /tasks/{id}/pause
POST /tasks/{id}/run
GET  /tasks/{id}/executions
```

执行已发布 Skill：

```json
{
  "skillId": "send-email",
  "skillVersion": "2.0.0",
  "inputs": {
    "recipient": "team@example.com",
    "subject": "Status",
    "body": "Hello"
  },
  "mode": "guided"
}
```

省略 `skillVersion` 时使用当前 published 版本。`step` 模式会在每个步骤前进入
`waiting_user`，由 `/runs/{id}/confirm` 继续；`unattended` 只允许 published 固定版本，
外部副作用还必须显式提供 `externalSideEffectsApproved: true`。Run 历史会保存 Skill ID、
版本、模式和输入参数。

用户确认执行计划后，Runtime 会按当前步骤收窄模型可见的工具：计划声明的工具负责完成
步骤，`list_windows`、`list_elements`、截图等只读观察工具可以辅助判断状态，但不能代替
计划动作。策略越界调用不会执行，模型有一次改正机会；再次越界或已授权工具实际执行失败
时，计划才会停止。

CLI 会在每次 Chat/Vision 模型响应后显示当前 Run 的累计 Token 用量。`GET /runs/{id}`
返回 `token_usage`，WebSocket 和 Run Event 历史通过 `run.usage` 推送单次增量及累计值。
如果某个 OpenAI-compatible/Ollama 服务没有返回 `usage`，模型调用次数仍会记录，并明确
标记未报告的调用；此时 Token 累计值可能不完整。

视觉验证默认使用分层检查点，不再对每次点击和输入都调用 Vision。每 3 个完成步骤、窗口
切换、最终步骤和 Send/Submit/Delete 等高风险动作会触发验证。验证只判断刚执行工具的
直接效果，不再额外调用 Chat 模型生成预期。工具报告新窗口时截图跟随新窗口；没有可靠
标题时保持当前前台窗口。`⚠️ 无法确定` 作为提示，只有明确 `❌` 才标记失败。

```yaml
agent:
  verification:
    mode: checkpoint       # checkpoint | all | off
    checkpointInterval: 3
    verifyWindowTransitions: true
    verifyFinalStep: true
    verifyHighRiskActions: true
```

Run 同时维护结构化 `execution_memory`：记录计划/Skill 步骤、工具、脱敏参数、结果和验证
状态。模型下一轮会收到最近 12 条精简记录，避免重复点击或输入；Run 最多持久化 100 条。
`GET /runs/{id}` 返回该字段，Event 类型为 `run.execution_memory`。

版本化 Skill 可使用 `ui.hotkey`、`ui.key` 和 `ui.actions` 绕过不必要的 Agent 推理。
`ui.actions` 会把一组确定性键盘/输入动作交给 `run_actions` 一次执行：

```yaml
- id: fill-recipient
  name: Fill recipient with keyboard
  action: ui.actions
  with:
    actions:
      - tool: type_text
        args: {text: "{{ input.recipient }}"}
      - tool: press_key
        args: {key: Enter}
      - tool: press_key
        args: {key: Tab}

- id: send
  name: Send confirmed email
  action: ui.hotkey
  with: {keys: "Alt+S"}
  risk: external_side_effect
```

快捷键只能在目标窗口和焦点已确认时使用；发送、提交等快捷键仍必须标记
`external_side_effect` 并经过确认。`send-email@2.0.0` 使用 Classic Outlook Adapter：
常规路径不调用 Chat/Vision 模型，填写阶段只做一次 UIA 扫描和一次批量动作；打开写信和
填写失败时可调用 Skill 声明的受限 Agent fallback，发送步骤不允许 fallback，避免重复发送。

`condition` 步骤支持 `equals`、`contains`、真假和数值比较；`skill.call` 必须声明子 Skill
固定版本，并继承父 Run 的执行模式、确认策略和桌面锁。自动化步骤最终失败后，Runtime
会在 `data/run_evidence` 保存错误元数据，并尽力采集截图和目标窗口 UIA 信息。

Task 只能绑定 published Skill 固定版本，并使用 `unattended` 模式。Cron 保存 IANA 时区、
misfire 策略、超时、重试和外部副作用授权；调度触发和“立即运行”均进入同一个 Runtime
Run/Step/Event 流程。活动 Task 引用的 Skill 版本不能被弃用。升级前创建的提示词驱动
`scheduled_jobs` 会在启动时自动暂停并保留数据，需要重新创建为版本化 Task。

Skill 生命周期为 `draft → validated → published → deprecated`。只有 draft 可原地编辑；
已发布内容必须通过新版本更新。确定性步骤通过工具注册表执行，Windows 桌面原子操作仍由
WinPeekaboo 提供。PowerShell Skill 只能引用已审核、固定版本且不含删除能力的脚本 ID，
当前执行接口在脚本注册表接入前默认拒绝这类步骤。

常用命令：

| 命令 | 作用 |
|---|---|
| `/help` | 查看帮助 |
| `/new` | 创建会话 |
| `/history` | 查看当前会话历史 |
| `/skills` | 查看 Skill |
| `/jobs` | 查看 Cron Task |
| `/memory` | 查看跨会话记忆 |
| `/tools` | 查看已注册工具 |
| `/config` | 查看当前配置 |
| `/exit` | 退出 |

## 处理流程

```text
POST /runs (skillId) -> Skill Executor -> App Adapter -> WinPeekaboo
                                        \-> 失败时受限 Agent fallback
CLI / POST /runs (user_input) -> Agent Loop -> LLM Tool Calling -> WinPeekaboo
```

当前 CLI 的自由文本 Skill 匹配仍走 Agent 计划路径。需要验证确定性 Outlook Skill 时，
使用 Runtime API 提交 `skillId: send-email`；后续 Chat/UI 路由会先把自由文本解析为
结构化 Skill 输入，再提交同一条 Runtime 路径。

当前共注册 55 个工具，分布在：

- `tools/winpeekaboo.py`：桌面原子操作。
- `tools/vision.py`：UIA、语义和视觉定位。
- `tools/browser.py`：Edge 网页自动化。
- `tools/outlook.py`：Classic Outlook 确定性应用 Adapter。
- `tools/system.py`：受控文件、命令和剪贴板操作。
- `tools/actions.py`：连续确定性操作。
- `tools/planner.py`：计划生成和状态。
- `tools/scheduler_tool.py`：Cron Task 管理。

## 目录

```text
agent/          Agent Loop、上下文和计划
cli/            当前开发用 REPL
config/         配置加载
credentials/    Windows Credential Manager 密钥适配
llm/            模型客户端
memory/         SQLite/SQLModel 持久化
runtime/        Run/Step 状态、事件总线和桌面互斥锁
scheduler/      APScheduler 调度器
tasks/          版本化 Task、校验和执行服务
skills/         Skill 解析和注册
tools/          Agent 工具和 WinPeekaboo Adapter
tests/          当前测试与视觉定位评估
specs/          产品和架构规范
```

## 开发检查

```powershell
pip install -e ".[dev]"
python -m pytest -q
python -m ruff check agent runtime skills tasks config credentials llm memory tests `
  tools/actions.py tools/outlook.py tools/uia.py tools/vision.py tools/winpeekaboo.py `
  --exclude tests/vision_bbox
```

当前基线包含 88 项自动化测试。完整仓库的 `ruff check .` 尚有旧 CLI、工具和视觉评估
脚本的存量告警，因此现阶段使用上面的核心模块检查范围；这不影响 `pytest` 执行。

不要提交以下本地数据：

- `.env`
- 内部证书
- `data/*.db`
- `.agent_history/`
- 截图、日志和模型输出
- `__pycache__/`

## 当前重构方向

1. 将 CLI 与 Agent Runtime 解耦。
2. 增加 Run、Step 和 Event 状态模型。
3. 将工具结果统一为结构化格式。
4. 将 Skill 演进为可编辑、可验证、可发布和可回滚的版本化流程。
5. 增加本地 HTTP/WebSocket API。
6. 接入 Electron、Task 页面和 Windows 独立打包。

发布和定时打 Tag 的约定见 [RELEASING.md](RELEASING.md)。
