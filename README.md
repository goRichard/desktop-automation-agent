# SEWC FlowPilot

基于 LLM 和 WinPeekaboo 的 Windows 桌面自动化 Agent。当前仓库是 Python Runtime
原型，后续通过 Electron 封装为独立 Windows 应用。

产品设计见 [Electron MVP Spec](specs/electron_desktop_agent_mvp.md)。

## 当前能力

- Windows 桌面：通过 WinPeekaboo 启动应用、管理窗口、枚举 UIA 元素以及执行键鼠操作。
- 视觉定位：UIA 确定性匹配优先，LLM 语义选择其次，视觉模型兜底。
- 浏览器：通过 Playwright 控制系统 Microsoft Edge。
- Agent Loop：支持 OpenAI-compatible 和 Azure OpenAI 的工具调用循环。
- Skill：加载 Markdown + YAML front matter 定义的流程，并支持触发词和语义匹配。
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

内部模型证书可以配置在 Profile 或具体模型下：

```yaml
profiles:
  local:
    ssl_cert_path: ./internal-ca.pem
    llm:
      model: your-model
      api_base: https://internal-model.example.com/v1
      api_key_env: LLM_API_KEY
```

## 启动

```powershell
python main.py
```

或安装后使用：

```powershell
desktop-agent
```

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
User Input
  -> Skill 匹配与可选计划确认
  -> Context 组装
  -> LLM Tool Calling
  -> 工具按顺序执行
  -> 可选屏幕验证
  -> 继续推理或返回最终结果
```

当前共注册 52 个工具，分布在：

- `tools/winpeekaboo.py`：桌面原子操作。
- `tools/vision.py`：UIA、语义和视觉定位。
- `tools/browser.py`：Edge 网页自动化。
- `tools/system.py`：受控文件、命令和剪贴板操作。
- `tools/actions.py`：连续确定性操作。
- `tools/planner.py`：计划生成和状态。
- `tools/scheduler_tool.py`：Cron Task 管理。

## 目录

```text
agent/          Agent Loop、上下文和计划
cli/            当前开发用 REPL
config/         配置加载
llm/            模型客户端
memory/         SQLite/SQLModel 持久化
runtime/        Run/Step 状态、事件总线和桌面互斥锁
scheduler/      APScheduler 调度器
skills/         Skill 解析和注册
tools/          Agent 工具和 WinPeekaboo Adapter
tests/          当前测试与视觉定位评估
specs/          产品和架构规范
```

## 开发检查

```powershell
pip install -e ".[dev]"
pytest
ruff check .
```

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
