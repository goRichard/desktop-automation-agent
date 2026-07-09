# Windows 应用 Skill 与 Adapter 模板

本文从 Classic Outlook 和 New Teams 的实现中提炼可复用模式，用于后续增加其他 Windows
桌面应用。目标是让稳定流程走确定性 Adapter，让 LLM 只参与 Skill 生成、首次调试和受控
失败恢复，而不是参与每一个点击。

## 1. 分层原则

```text
用户输入 / Task 参数
        ↓
版本化 Skill：描述业务步骤、输入、重试、风险和失败策略
        ↓
应用 Adapter：处理窗口身份、UIA 匹配、快捷键、文件对话框和后置条件
        ↓
WinPeekaboo：窗口、UIA、鼠标、键盘和截图原子操作
```

- Skill 不保存坐标，不直接解析 WinPeekaboo 原始 JSON。
- Adapter 不做开放式业务推理，只执行边界明确的应用动作。
- WinPeekaboo 是所有 Windows 桌面操作的固定底层入口。
- 正常路径不调用 Chat/Vision 模型。
- 原始 `list_elements` 仅供 Adapter 和失败证据使用，不暴露给主 Agent。
- Agent 调试时默认直接使用 `find_and_click`；需要查看候选时使用压缩的
  `inspect_elements`。

## 2. 推荐业务步骤

一个“填写并提交”类应用 Skill 默认拆成以下步骤：

| 阶段 | 责任 | 典型实现 |
|---|---|---|
| Launch | 启动应用并取得主窗口 | `app_launch` + 进程过滤 |
| Navigate | 进入确定业务页面 | 稳定快捷键优先，UIA 点击补充 |
| Resolve | 重新解析当前业务窗口 | HWND 优先，标题仅作展示和回退 |
| Fill | 一次扫描并批量填写字段 | UIA 定位 + `run_actions` |
| Attach | 校验文件并添加附件 | UIA 菜单 + 前台文件对话框键盘输入 |
| Re-resolve | 提交前刷新窗口身份 | 防止标题、焦点或窗口层级变化 |
| Submit | 执行发送/提交 | UIA 点击或稳定快捷键 |
| Verify | 验证业务后置条件 | 窗口关闭、编辑区清空、成功状态出现 |

不要把每个点击都拆成一个 Skill 步骤。Skill 表达业务动作，Adapter 内部负责一组连续、
确定性的桌面原子操作。

## 3. Outlook 与 Teams 对照

| 项目 | Classic Outlook | New Teams | 可复用结论 |
|---|---|---|---|
| 进程 | `outlook.exe` | `ms-teams.exe` | 应用版本必须绑定进程身份 |
| 新建入口 | `Ctrl+N` 打开独立写信窗口 | `Ctrl+N` 在主窗口打开新聊天 | 快捷键适合稳定导航 |
| 窗口变化 | Subject 会改变窗口标题 | 通常复用主窗口 | 保存 HWND，不依赖旧标题 |
| 字段填写 | To/Cc/Subject/Body 一次扫描 | Recipient/Message 一次扫描 | 同一页面批量 UIA 扫描 |
| 附件菜单 | Ribbon + `Browse This PC` | Actions and apps + Attach file | 菜单用 UIA，系统对话框用前台键盘 |
| 系统文件对话框 | `Alt+N → 路径 → Enter` | `Alt+N → 路径 → Enter` | 不按对话框标题反复连接 |
| 提交 | 前台 `Alt+S` | 消息区聚焦后 `Ctrl+Enter`/`Enter` | 高风险动作只执行一次 |
| 成功条件 | 写信窗口关闭 | 当前仅确认点击成功 | 新 Adapter 必须定义业务后置条件 |

Teams 后续应补充强验证，例如消息编辑框清空、最新消息节点出现，或附件上传状态完成。
在此之前，Teams Adapter 的 `uia_click` 只代表发送点击已执行，不代表服务端已接收。

## 4. 通用 Skill YAML 骨架

```yaml
apiVersion: desktop-agent/v1alpha1
kind: Skill

metadata:
  id: operate-example-app
  name: operate_example_app
  version: 1.0.0
  status: draft
  description: 使用 Example App 执行确定性业务操作
  tags: [example, windows]
  triggers:
    - 示例业务操作

applications:
  - id: example-app
    process: example.exe
    required: true

inputs:
  recipient:
    type: string
    required: true
  content:
    type: string
    required: true
  attachments:
    type: array
    items: string
    required: false
    default: []

execution:
  defaultMode: guided
  timeoutSeconds: 300
  steps:
    - id: launch
      name: 启动应用
      action: example.launch
      retry:
        maxAttempts: 2
        delaySeconds: 2
      onFailure: stop

    - id: open-editor
      name: 打开业务编辑区
      action: example.openEditor
      target:
        window: "{{ steps.launch.output.data.windowTitle }}"
      retry:
        maxAttempts: 2
        delaySeconds: 1
      onFailure: stop

    - id: resolve-editor
      name: 解析当前业务窗口
      action: example.resolveEditor
      onFailure: stop

    - id: fill
      name: 批量填写业务字段
      action: example.fill
      target:
        window: "{{ steps.resolve-editor.output.data.windowTitle }}"
      with:
        recipient: "{{ input.recipient }}"
        content: "{{ input.content }}"
      retry:
        maxAttempts: 2
        delaySeconds: 1
      onFailure: stop

    - id: attach
      name: 添加可选附件
      action: example.addAttachments
      target:
        window: "{{ steps.resolve-editor.output.data.windowTitle }}"
      with:
        paths: "{{ input.attachments }}"
        timeout_seconds: 10
      onFailure: stop

    - id: resolve-before-submit
      name: 提交前刷新窗口
      action: example.resolveEditor
      onFailure: stop

    - id: submit
      name: 提交业务操作
      action: example.submit
      target:
        window: "{{ steps.resolve-before-submit.output.data.windowTitle }}"
      risk: external_side_effect
      policy:
        requireConfirmation: false
      onFailure: stop
```

`requireConfirmation: false` 只表示该已审核 Skill 在 guided 模式不暂停。它不会移除
`external_side_effect` 风险标记；unattended Task 仍必须预先授权外部副作用。
`user.confirm` 是 Skill Executor 控制动作，不是 Agent 工具，不应出现在执行计划中。

## 5. Adapter 工具契约

每个应用建议提供以下工具，并映射到 `skills/executor.py`：

```text
example.launch
example.openEditor
example.resolveEditor
example.fill
example.addAttachments
example.submit
```

工具返回统一结构：

```json
{
  "ok": true,
  "data": {
    "action": "fill",
    "windowTitle": "Current title",
    "actionCount": 8
  },
  "error": null
}
```

失败应抛出应用专用错误，例如 `ExampleAutomationError`。错误至少包含：失败阶段、目标
窗口、目标控件或 AutomationId，以及压缩的 UIA 摘要。不要把模型认证错误包装成桌面
控件错误。

## 6. 原子操作选择

按以下优先级选择操作方式：

1. 已知且稳定的应用快捷键，用于导航、新建和视图切换。
2. AutomationId 精确匹配。
3. UIA 名称 + ControlType + 可见/可用状态确定性匹配。
4. `inspect_elements` 辅助首次调试，只返回压缩候选。
5. 受限 Agent fallback。
6. Vision 只作为最后手段，不进入稳定 Skill 的常规路径。

快捷键适合打开页面，但提交动作必须有后置条件。UIA 点击适合 Send、Attach、Browse 等
语义明确的按钮。连续点击、输入和按键应通过 `run_actions` 一次执行，减少工具轮次。

## 7. 窗口和焦点

- 创建业务窗口时记录 HWND，并返回当前 `windowTitle`。
- 后续调用优先按 HWND 找到同一窗口，再读取它的最新标题。
- 输入 Subject、文档名或会话名后，不得继续信任创建时标题。
- 提交前重新解析窗口并只激活一次，等待焦点稳定后再输入快捷键。
- 临时菜单通常属于应用窗口；系统文件对话框弹出后保持前台，不按标题再次连接。
- 点击、输入、按键和滚动会使 UIA 缓存失效，不复用旧坐标。

## 8. 附件流程

附件 Adapter 必须：

1. 将路径展开为绝对路径。
2. 在打开 UI 前验证文件存在。
3. 使用应用 UI 打开附件菜单，不调用 PowerShell、COM、Graph 或后台脚本上传。
4. 在点击打开文件对话框前记录窗口快照。
5. 对话框出现后使用前台 `Alt+N → Ctrl+A → type path → Enter`。
6. 等待文件对话框关闭。
7. 等待应用中的上传/附件状态稳定后再允许提交。
8. 多附件逐个处理，任一失败立即停止。

## 9. Fallback 边界

Fallback 只用于提交前的可恢复步骤，并声明最小工具集：

```yaml
fallback:
  type: agent
  instruction: 使用一次 UIA 扫描完成字段填写，不要提交。
  allowedTools:
    - ui.inspect
    - ui.locate
    - ui.click
    - ui.type
    - ui.key
    - ui.hotkey
    - ui.actions
```

- Fallback 指令必须明确“不要发送/提交”。
- Submit 不配置 Agent fallback，避免点击结果不明确时重复提交。
- 不允许通过脚本或后台 API 绕过可见 UI 流程。
- 不允许任何删除文件操作。

## 10. 后置条件

提交工具不能只判断“点击命令没有报错”。至少实现一种：

- 编辑窗口关闭。
- 编辑框清空且最新记录出现。
- 成功提示、状态文本或业务 ID 出现。
- 原窗口/按钮状态发生明确且唯一的变化。

失败时只报告一次提交尝试，不自动重试高风险动作。若出现新对话框，应返回其标题作为
阻塞原因，例如 Outlook 的 Check Names 或策略提示。

## 11. 测试与版本

每个新 Adapter 至少覆盖：

- 主窗口选择，排除通知和无关窗口。
- 快捷键参数和焦点顺序。
- 字段 UIA 匹配及坐标校验。
- 空附件、文件不存在、单附件和对话框关闭。
- 标题变化后按 HWND 重新解析。
- Submit 只执行一次及成功/阻塞后置条件。
- Skill 输入模板解析和 Action-to-Tool 映射。
- `external_side_effect` 与确认策略。
- 正常路径不调用 Chat/Vision。

文件系统 Skill 是版本化内容。已经导入 SQLite 的同版本不会被启动导入覆盖；行为变化时
必须增加版本号，重启 Runtime 后再验证、发布并更新 Task 绑定版本。

## 12. 新应用 Skill 设计提示词

后续设计应用操作时，可将以下内容作为需求模板交给开发 Agent：

```text
请为 SEWC FlowPilot 设计一个 Windows 应用 Skill 和确定性 Adapter。

应用信息：
- 应用名称：<name>
- 版本：<version>
- 进程名：<process.exe>
- 主窗口特征：<title/class/process>

业务目标：
<用户希望完成的业务操作>

输入：
- <input name>: <type/required/description>

已知操作流程：
1. <launch/navigation>
2. <fill/select>
3. <attachment if any>
4. <submit>

成功后置条件：
<窗口关闭、状态出现、编辑框清空或业务记录出现>

约束：
- 所有桌面原子操作必须通过 WinPeekaboo。
- 优先使用稳定快捷键和确定性 UIA，不使用后台 API、COM、VBA 或业务写入脚本。
- 原始 UIA JSON 只能在 Adapter 内部使用，不得返回主 LLM。
- 同一页面只扫描一次 UIA，并用 run_actions 批量执行连续动作。
- 使用 HWND 跟踪会改标题的业务窗口，提交前重新解析窗口。
- 系统文件对话框保持前台，使用 Alt+N、路径输入和 Enter。
- 提交动作标记 external_side_effect，只执行一次，不配置 Agent fallback。
- 不生成 user.confirm；如不需要 guided 确认，使用 requireConfirmation: false。
- 不允许删除文件。
- 正常路径不得调用 Chat/Vision；仅允许提交前步骤使用最小权限的 Agent fallback。

请输出：
1. 业务步骤与窗口状态变化。
2. Adapter 工具清单及每个工具的结构化输入、输出、错误和后置条件。
3. Action-to-Tool 映射。
4. 完整 draft Skill YAML。
5. UIA 别名/AutomationId 收集清单。
6. 单元测试与 Windows 实机验收项。
7. 当前未知项和不能假设的界面行为。
```

必须先填写“成功后置条件”。如果该项为空，只能生成调研计划，不能直接实现 Submit。

## 13. 新应用实现检查表

- [ ] 确认应用版本、进程名和窗口结构。
- [ ] 收集主页面、编辑页、菜单和对话框的 UIA 摘要。
- [ ] 定义业务输入、可选附件和提交后置条件。
- [ ] 创建应用专用 Adapter，不把原始 UIA JSON交给主 LLM。
- [ ] 注册确定性 Action-to-Tool 映射。
- [ ] 创建 draft Skill 并设置新版本号。
- [ ] 提交前重新解析窗口身份。
- [ ] 高风险动作无 Agent fallback、无自动重试。
- [ ] 添加单元测试和 Windows 专用测试账号验收。
- [ ] 实机通过后再 validate、publish 和绑定定时 Task。
