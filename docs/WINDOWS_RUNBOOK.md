# SEWC FlowPilot Windows 运行与测试指南

本文用于在 Windows 10/11 实机或长期运行的 Windows VM 上验证当前 Python Runtime。
当前阶段没有 Electron 界面，主要通过 CLI、Runtime HTTP API 和 WinPeekaboo CLI 测试。

## 1. 测试边界

当前需要验证：

- Python Runtime 能安装、启动并保存本地状态。
- 模型 Provider 能完成配置检查和真实请求。
- WinPeekaboo 能枚举窗口/UIA 元素并执行键鼠原子操作。
- Microsoft Edge 能由 Playwright 启动和操作。
- Classic Outlook（`outlook.exe`）和 New Teams（通常为 `ms-teams.exe`）能被识别。
- Skill、Run、Event、Task/Cron API 能正常工作。
- 失败时能生成截图、UIA 信息和错误元数据。

当前不属于本轮验收范围：

- Electron 界面和 Windows 安装包。
- 锁屏后的桌面自动化。
- New Outlook。
- 生产环境自动更新和内部软件分发。
- PowerShell Skill 执行；脚本注册表完成前 Runtime 会主动拒绝该步骤。

## 2. Windows 环境要求

建议测试环境：

| 项目 | 建议值 |
|---|---|
| Windows | Windows 10/11 64 位 |
| Python | 3.11 或 3.12，64 位 |
| 会话 | 用户已登录、桌面未锁屏 |
| 分辨率 | 固定为 1920×1080 或记录实际值 |
| 缩放 | 固定值，建议第一轮使用 100% |
| 电源 | 禁止自动睡眠、休眠和自动锁屏 |
| Outlook | Classic Outlook，进程为 `outlook.exe` |
| Edge | 已安装的 Microsoft Edge |
| Teams | New Teams；记录实际进程名和版本 |

桌面自动化依赖交互式桌面。测试期间不要锁屏，也不要让 VM 进入睡眠。通过 RDP
测试时，应确认断开连接后交互式桌面仍然可用；不同 VM 平台的 RDP 会话行为可能不同。

检查 Python 和应用进程：

```powershell
py -0p
py -3.11 --version

Get-Process OUTLOOK -ErrorAction SilentlyContinue |
  Select-Object Name, Id, Path
Get-Process msedge -ErrorAction SilentlyContinue |
  Select-Object Name, Id, Path
Get-Process ms-teams -ErrorAction SilentlyContinue |
  Select-Object Name, Id, Path
```

如果 Outlook 已启动，可进一步记录版本：

```powershell
$outlook = Get-Process OUTLOOK -ErrorAction Stop | Select-Object -First 1
(Get-Item $outlook.Path).VersionInfo |
  Select-Object ProductName, ProductVersion, FileVersion
```

当前 MVP 以 `outlook.exe` 作为 Classic Outlook 的验收标志。

## 3. 获取代码并创建环境

以下命令均在 PowerShell 中执行：

```powershell
git clone <repository-url>
Set-Location desktop-automation-agent
git status
git rev-parse HEAD

py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -e ".[dev]"
```

如果 PowerShell 阻止激活脚本，只为当前窗口临时放开：

```powershell
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
.\.venv\Scripts\Activate.ps1
```

也可以不激活环境，后续将 `python` 替换为：

```powershell
.\.venv\Scripts\python.exe
```

### 安装 WinPeekaboo

WinPeekaboo 是必需依赖，但当前仓库不包含它的安装包，也没有公共 PyPI 安装源。请使用
团队提供的 wheel、源码仓库或内部包源，例如：

```powershell
python -m pip install C:\Packages\winpeekaboo-<version>-py3-none-any.whl
```

安装后先验证模块和命令，而不是直接启动 Agent：

```powershell
python -c "import winpeekaboo; print(winpeekaboo.__file__)"
python -m winpeekaboo --help
python -m winpeekaboo list screens --json
python -m winpeekaboo list windows --json
```

通过标准：

- 四条命令均退出码为 `0`。
- `list screens` 返回当前显示器信息。
- `list windows` 返回 JSON，且能看到当前已打开的桌面窗口。

如果 `import winpeekaboo` 指向项目根目录下的空目录，而不是实际安装包，说明
WinPeekaboo 尚未正确安装或包路径被空目录遮蔽，需要先修正安装来源。

## 4. 配置模型

复制开发环境密钥模板：

```powershell
Copy-Item .env.example .env
```

编辑 `config.yaml`：

1. 将 `active_profile` 设置为要测试的 Profile。
2. 确认 Chat 和 Vision 的 `provider`、`model`、URL 与实际服务一致。
3. 在 `.env` 中填写对应 `apiKeyEnv` 指向的密钥。
4. 内部 HTTPS 服务应配置 CA Bundle，不要通过关闭 TLS 校验绕过证书问题。

### Ollama

Ollama Profile 的基础形式：

```yaml
active_profile: ollama

profiles:
  ollama:
    models:
      chat:
        provider: ollama
        model: qwen3
        baseUrl: http://127.0.0.1:11434
      vision:
        provider: ollama
        model: qwen3-vl
        baseUrl: http://127.0.0.1:11434
        capabilities:
          chat: true
          streaming: true
          toolCalling: false
          vision: true
          jsonOutput: false
```

先在 Ollama 自身确认模型存在并可调用，再启动 FlowPilot。模型名称应以测试电脑中
`ollama list` 的结果为准。

### OpenAI-compatible 或内部模型

```yaml
chat:
  provider: openai_compatible
  model: your-model
  baseUrl: https://internal-model.example.com/v1
  apiKeyEnv: LLM_API_KEY
  tls:
    verify: true
    caBundle: ./internal-ca.pem
```

`caBundle` 可以使用绝对路径，也可以使用相对于 `config.yaml` 的路径。CA 文件必须是
PEM 证书链，不得包含私钥。文件不存在或配置的 SHA-256 指纹不匹配时，Runtime 会拒绝
加载配置。

开发环境可以使用 `.env`；Windows 正式运行方案使用 Credential Manager，并在
`config.yaml` 中保存 `apiKeySecret` 引用。不要提交 `.env`、内部证书或密钥。

## 5. 先运行自动化测试

自动化测试不要求真实 Outlook、Teams、WinPeekaboo 或可访问的模型服务，适合先判断
代码和 Python 环境是否正常：

```powershell
python -m pytest -q
```

当前基线预期：

```text
27 passed
```

测试覆盖：

| 范围 | 测试内容 |
|---|---|
| Model Provider | 配置校验、密钥脱敏、CA/指纹、Ollama URL、错误脱敏 |
| Runtime | Run/Step/Event 状态、暂停/恢复、确认、取消、桌面互斥 |
| Runtime API | Token 鉴权、Skill/Task/Run 生命周期、模型和证书接口 |
| Persistence | Run、Step、Event、Evidence 的 SQLite 持久化 |
| Skill | Schema、版本生命周期、输入、重试、嵌套 Skill、执行策略 |
| Task/Scheduler | Cron、时区、手动执行、旧任务迁移 |

当前可能出现两个已知 warning：

- Starlette `TestClient` 关于 `httpx` 的弃用提示。
- 视觉 BBox 工具中的 `TestReport` 不参与 pytest 收集。

两者不影响 27 项测试通过。如果出现 failed/error，请保留完整输出：

```powershell
python -m pytest -q 2>&1 |
  Tee-Object -FilePath .\pytest-windows.log
```

核心重构模块的静态检查：

```powershell
python -m ruff check runtime skills tasks config credentials llm tests `
  --exclude tests/vision_bbox
```

当前预期为 `All checks passed!`。完整仓库的 `ruff check .` 仍包含旧 CLI、工具和视觉
评估脚本的存量问题，不作为本轮 Windows 通过条件。

## 6. 启动 CLI

在项目根目录运行：

```powershell
python main.py
```

指定其他配置文件：

```powershell
python main.py --config C:\FlowPilot\config.yaml
```

启动成功后，应显示：

- 当前 Profile。
- Chat 和 Vision 模型。
- 已加载 Skill 数量。
- 当前会话 ID。

先执行无副作用检查：

```text
/config
/tools
/skills
/jobs
```

然后发送一个不调用桌面工具的简单问题，确认 Chat 模型能够返回结果。退出使用：

```text
/exit
```

## 7. 启动 Runtime API

Runtime 只监听 `127.0.0.1`。建议使用固定测试 Token，便于在第二个 PowerShell 窗口
调用 API。

PowerShell 窗口 A：

```powershell
Set-Location C:\Path\To\desktop-automation-agent
.\.venv\Scripts\Activate.ps1

$env:FLOWPILOT_RUNTIME_TOKEN = "flowpilot-local-smoke-token"
$env:DESKTOP_AGENT_CONFIG = (Resolve-Path .\config.yaml).Path
python -m runtime.api
```

预期日志包含 `Uvicorn running on http://127.0.0.1:8765`。

PowerShell 窗口 B：

```powershell
$baseUrl = "http://127.0.0.1:8765"
$token = "flowpilot-local-smoke-token"
$headers = @{ "X-Runtime-Token" = $token }

Invoke-RestMethod "$baseUrl/health"
Invoke-RestMethod "$baseUrl/runtime/capabilities" -Headers $headers
Invoke-RestMethod "$baseUrl/runtime/environment" -Headers $headers
Invoke-RestMethod "$baseUrl/models" -Headers $headers
Invoke-RestMethod "$baseUrl/skills" -Headers $headers
Invoke-RestMethod "$baseUrl/tasks" -Headers $headers
Invoke-RestMethod "$baseUrl/runs" -Headers $headers
```

通过标准：

- `/health` 返回 `status: ok`。
- 不带 Token 请求 `/runtime/capabilities` 时返回 HTTP 401。
- 带 Token 时各接口返回 JSON。
- `/runtime/environment` 的数据库和 Skill 路径指向当前配置目录。
- `/models` 不返回 API Key 或环境变量名。

配置级模型健康检查不发起真实推理：

```powershell
Invoke-RestMethod "$baseUrl/models/chat/health?probe=configuration" `
  -Method Post -Headers $headers
```

完成密钥和网络配置后，再执行真实服务检查：

```powershell
Invoke-RestMethod "$baseUrl/models/chat/health?probe=request" `
  -Method Post -Headers $headers
```

真实检查失败时应返回受控的 502 和脱敏错误，不应在响应或日志中出现 API Key。
Runtime API Schema 可在浏览器查看 `http://127.0.0.1:8765/docs`。

停止 Runtime 使用 `Ctrl+C`。

## 8. WinPeekaboo 原子操作测试

原子操作测试不经过 LLM，先用它隔离桌面层问题。

### W01：显示器和窗口枚举

```powershell
python -m winpeekaboo list screens --json
python -m winpeekaboo list windows --json
python -m winpeekaboo list apps --json
```

预期：JSON 可解析，窗口标题没有大面积乱码。

### W02：记事本启动、输入和截图

```powershell
New-Item -ItemType Directory -Force .\screenshots | Out-Null
python -m winpeekaboo app launch --name notepad.exe --wait
python -m winpeekaboo list windows --json --filter "Notepad"
```

从输出中取得实际窗口标题。中文 Windows 的标题可能包含“记事本”，不要硬编码英文标题。

```powershell
python -m winpeekaboo type --text "FlowPilot Windows smoke test" `
  --window "<实际窗口标题>"
python -m winpeekaboo list elements --window "<实际窗口标题>" --json
python -m winpeekaboo image --output .\screenshots\notepad-smoke.png `
  --window "<实际窗口标题>"
```

预期：

- 记事本被启动并置于前台。
- 文本只输入一次且字符完整。
- UIA 元素列表可返回。
- 截图内容和目标窗口一致。

本测试不保存文件，不执行删除操作。关闭记事本时如出现保存提示，选择“不保存”。

### W03：Classic Outlook 识别

先手工启动 Outlook，再执行：

```powershell
python -m winpeekaboo list apps --json --filter outlook
python -m winpeekaboo list windows --json --filter "Outlook"
```

从输出中取得主窗口标题：

```powershell
python -m winpeekaboo list elements --window "<Outlook 主窗口标题>" --json
```

预期：

- 进程为 `outlook.exe`。
- 能发现 Outlook 主窗口。
- UIA 列表中能看到部分邮件视图控件。

首轮测试不要发送真实邮件。后续邮件流程使用专用测试邮箱，先测试“新建并填写草稿但不发送”，
确认收件人、主题、正文和窗口切换无误后，再单独审批发送测试。

### W04：New Teams 识别

先手工启动并登录 New Teams：

```powershell
python -m winpeekaboo list apps --json --filter teams
python -m winpeekaboo list windows --json --filter "Teams"
python -m winpeekaboo list elements --window "<Teams 主窗口标题>" --json
```

记录实际进程名、窗口标题和 Teams 版本。如果 `ms-teams.exe` 存在但 WinPeekaboo 无法
枚举窗口，需要同时记录它是否为 MSIX 应用以及当前 WinPeekaboo 版本。

### W05：Edge/Playwright

以下脚本直接调用浏览器工具，不经过模型：

```powershell
@'
import asyncio
from tools.browser import browser_close, browser_get_state, browser_navigate

async def main():
    print(await browser_navigate("https://example.com"))
    state = await browser_get_state()
    print(state[:1000])
    print(await browser_close())

asyncio.run(main())
'@ | python -
```

预期：

- 系统 Edge 以可见窗口启动。
- 页面打开 `https://example.com`。
- 状态输出包含页面标题或可交互元素。
- 测试结束后由 `browser_close` 关闭该测试浏览器实例。

这一路径使用独立 Playwright Context，不会接管用户已经打开的普通 Edge 窗口。

## 9. Agent 端到端测试顺序

只有自动化测试、模型检查和 W01–W05 通过后，再进行 Agent 端到端测试。推荐按风险从低到高：

| ID | 测试 | 通过标准 |
|---|---|---|
| A01 | 询问普通文本问题 | 模型正常回复，不调用桌面工具 |
| A02 | 打开记事本 | 使用 `app_launch`，窗口可见 |
| A03 | 在记事本输入固定文本 | 使用 WinPeekaboo，文本准确 |
| A04 | 打开 Edge 访问测试页面 | 使用 `browser_navigate`，不是 `app_launch` |
| A05 | 读取 Edge 页面结构 | 能返回页面元素和标题 |
| A06 | 打开 Outlook 并停留在收件箱 | 不切换到浏览器工具 |
| A07 | 新建邮件并填写测试草稿 | 字段正确，不点击 Send |
| A08 | 打开 Teams 并定位搜索框 | 能枚举/定位控件，不发送消息 |
| A09 | Skill `step` 模式 | 每一步进入 `waiting_user`，确认后继续 |
| A10 | Skill `guided` 模式 | 仅风险步骤需要确认 |
| A11 | 同时启动两个桌面 Run | 第二个 Run 等待桌面互斥锁 |
| A12 | 暂停、恢复和取消 Run | 状态和 Event 顺序正确 |

邮件发送、Teams 发消息等外部副作用必须使用测试账号和测试接收人，并作为独立用例执行。
无人值守模式只能运行已发布的固定 Skill 版本，并显式批准外部副作用。

## 10. Task/Cron 测试

Task 验收前提：

- 引用的 Skill 已处于 `published`。
- Task 引用明确的 Skill 版本，不能只写 latest。
- `execution.mode` 为 `unattended`。
- VM 在触发时间保持用户登录、桌面未锁屏。

建议第一轮创建一个只包含 `ui.wait` 或打开记事本的无副作用 Skill，然后：

1. 创建每 5 分钟触发的测试 Task。
2. 调用 `POST /tasks/{id}/run` 验证“立即运行”。
3. 检查 `GET /runs/{run_id}` 和 `/events`。
4. 检查 `GET /tasks/{id}/executions`。
5. 等待一次 Cron 触发。
6. 暂停 Task，确认不会再次触发。
7. 重启 Runtime，确认 Task 状态和下次执行时间恢复。

不要使用发送邮件或 Teams 消息作为首个 Cron 用例。

## 11. 失败证据和问题反馈

Skill 自动化步骤最终失败时，证据默认保存在：

```text
data\run_evidence\<run-id>\<step-id>-<evidence-id>\
```

可能包含：

- `metadata.json`：Run、Step、Action、错误和执行细节。
- `screen.png`：失败时桌面截图。
- `uia.txt`：目标窗口 UIA 元素。

API 查询：

```powershell
Invoke-RestMethod "$baseUrl/runs/<run-id>" -Headers $headers
Invoke-RestMethod "$baseUrl/runs/<run-id>/events" -Headers $headers
Invoke-RestMethod "$baseUrl/runs/<run-id>/evidence" -Headers $headers
```

提交问题时至少提供：

```text
Git commit/tag:
Windows 版本:
Python 版本:
WinPeekaboo 版本/安装来源:
Outlook/Edge/Teams 版本:
VM/物理机:
分辨率和缩放:
执行入口: CLI / Runtime API / WinPeekaboo CLI
测试用例 ID:
预期结果:
实际结果:
Run ID:
是否可稳定复现:
```

同时附上：

```powershell
git rev-parse HEAD
python --version
python -m pip freeze
```

分享日志或证据前必须检查并移除：

- API Key、Token 和内部服务地址。
- 邮件地址、聊天内容和业务数据。
- 内部证书。
- 截图中的敏感信息。
- `.env` 文件。

## 12. 常见故障

### `No module named winpeekaboo`

WinPeekaboo 没有安装到当前虚拟环境。用 `python -m pip show winpeekaboo` 和
`python -c "import winpeekaboo; print(winpeekaboo.__file__)"` 检查实际解释器和模块路径。

### WinPeekaboo 能导入但命令不可用

确认 `python -m winpeekaboo --help` 使用的是同一个虚拟环境。项目会用
`sys.executable -m winpeekaboo` 启动它，不会使用其他 Python 环境中的命令。

### UIA 找不到元素

依次记录：

1. 目标应用进程名和窗口标题。
2. 应用是否以管理员权限运行。
3. FlowPilot/终端是否具有相同权限级别。
4. Windows 缩放、语言和应用版本。
5. `list windows` 和 `list elements` 原始输出。

不要一开始就改用视觉点击；UIA 失败信息是后续稳定适配 Outlook/Teams 的必要输入。

### Edge 无法启动

确认系统已安装 Edge，`config.yaml` 中为：

```yaml
browser:
  channel: msedge
```

如果组织策略将 Edge 安装在非标准位置，可临时配置 `executable_path` 做定位测试，并在
反馈中记录实际路径。

### 模型证书错误

确认 CA Bundle 是 PEM 格式、路径相对于实际 `config.yaml`、证书链完整且没有私钥。
不要把 `tls.verify` 改为 `false` 作为正式解决方案。

### Runtime 返回 401

确认调用方的 `X-Runtime-Token` 与启动 Runtime 时的
`FLOWPILOT_RUNTIME_TOKEN` 完全一致。`/health` 是唯一不要求 Token 的核心接口。

### Runtime 端口被占用

使用其他本地端口：

```powershell
$env:FLOWPILOT_RUNTIME_PORT = "8876"
python -m runtime.api
```

随后将测试命令中的 `$baseUrl` 改为 `http://127.0.0.1:8876`。
