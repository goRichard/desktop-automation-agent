# Demo-to-Task Recorder 规划

目标：用户第一次通过对话或 step 模式完成一个 demo 后，Runtime 可以把本次成功过程沉淀为
可编辑、可验证、可发布的 Skill，并进一步生成可配置 Cron 的 Task。

这不是完整审计日志功能。MVP 只记录能复用流程所需的信息：用户意图、输入参数、成功步骤、
使用的工具、窗口上下文、失败修正和用户确认。长期无人值守需要的完整 trace、截图归档和
UIA 快照可以在同一结构上扩展。

## 1. 使用闭环

```text
用户发起 demo
  -> Agent/Skill Step 模式执行
  -> 用户在失败时给反馈，Runtime 记录修正
  -> demo 成功
  -> Recorder 生成 draft Skill
  -> 用户编辑输入、步骤、风险和验证
  -> validate / guided run
  -> publish 固定版本
  -> 创建 Cron Task
```

## 2. MVP 需要记录什么

沿用现有 Run、Step、Event 和 `execution_memory`，新增一个轻量的“可复用流程草稿”聚合层。
不需要保存每步截图或完整 stdout/stderr。

### 2.1 Demo Session 元数据

```json
{
  "sourceRunId": "...",
  "userGoal": "每天给 Teams 某人发送报告",
  "applicationHints": ["teams", "outlook", "edge"],
  "createdAt": "...",
  "status": "recording | ready_for_review | converted | discarded"
}
```

### 2.2 可复用步骤片段

每个片段从成功的 `execution_memory` 和 Skill step 结果中提取：

```json
{
  "sequence": 3,
  "kind": "adapter_action",
  "tool": "teams_fill_chat",
  "arguments": {
    "recipient": "{{ input.recipient }}",
    "message": "{{ input.message }}"
  },
  "source": {
    "runStepId": "...",
    "planStepId": 3
  },
  "activeWindowBefore": "Microsoft Teams / ms-teams.exe",
  "activeWindowAfter": "Microsoft Teams / ms-teams.exe",
  "planCompliance": "compliant",
  "reusable": true
}
```

### 2.3 用户反馈和修正

失败后用户说“这里应该点 Browse this PC”或“不要 user.confirm”时，需要记录成结构化 patch hint：

```json
{
  "stepRef": "add-attachments",
  "feedback": "使用 Alt+N 然后 A+F 打开 Browse This PC",
  "appliedChange": "adapter.updated",
  "result": "success"
}
```

## 3. 从 Demo 生成 Skill 的规则

Recorder 不能简单回放每一个 click。生成 Skill 时要做抽象：

| Demo 中的动作 | 生成 Skill 时的处理 |
|---|---|
| 已有确定性 Adapter，如 `teams_fill_chat` | 保留为业务步骤 |
| `run_actions` 中连续键盘动作 | 合并为 `ui.actions` 或 Adapter 内部动作 |
| 临时观察工具，如 `list_windows`、`capture_image` | 默认不进入 Skill，仅作为调试证据 |
| 失败后重试成功的旧动作 | 只保留最终成功路径 |
| 用户输入的收件人、消息、路径 | 提取为 `inputs` |
| 发送、提交、删除等动作 | 标记 `risk: external_side_effect` |
| 非确定性视觉 fallback | 标记为待人工确认，不能直接 publish |

## 4. 生成结果

生成 draft Skill：

```yaml
apiVersion: desktop-agent/v1alpha1
kind: Skill
metadata:
  id: generated-teams-message
  version: 0.1.0
  status: draft
inputs:
  recipient:
    type: string
    required: true
  message:
    type: string
    required: true
execution:
  defaultMode: guided
  steps:
    - id: launch
      action: teams.launch
    - id: open-chat
      action: teams.openNewChat
    - id: fill
      action: teams.fillChat
    - id: send
      action: teams.send
      risk: external_side_effect
      policy:
        requireConfirmation: false
```

生成 Task 草稿：

```yaml
kind: Task
metadata:
  id: daily-teams-message
schedule:
  cron: "0 9 * * MON-FRI"
  timezone: "Asia/Shanghai"
skill:
  id: generated-teams-message
  version: 1.0.0
parameters:
  recipient: "..."
  message: "..."
execution:
  mode: unattended
permissions:
  externalSideEffectsApproved: true
```

Task 必须引用 published Skill 固定版本。Recorder 只能生成 Task draft；不能绕过 validate/publish。

## 5. Runtime/API 设计

### 5.1 新增状态

建议后续新增：

```text
demo_sessions
demo_step_fragments
demo_feedback
```

MVP 可先不建表，用 Run 的 `execution_memory` 生成一次性草稿；Electron UI 稳定后再持久化。

### 5.2 新增 API

```http
POST /runs/{run_id}/demo-recording/start
POST /runs/{run_id}/demo-recording/stop
POST /runs/{run_id}/demo-recording/convert-to-skill
POST /skills/{skill_id}/versions/{version}/create-task-draft
```

第一版可以简化为一个接口：

```http
POST /runs/{run_id}/convert-to-skill-draft
```

输入：

```json
{
  "skillId": "daily-teams-message",
  "name": "Daily Teams Message",
  "inputHints": {
    "recipient": "parameterize",
    "message": "parameterize"
  }
}
```

输出：draft Skill document。

## 6. 模型参与边界

LLM 可以做：

- 从 demo 记录中提取参数。
- 合并重复步骤。
- 生成 draft Skill YAML。
- 解释哪些步骤不适合无人值守。
- 生成用户可审阅的变更说明。

LLM 不可以直接做：

- 自动 publish。
- 自动创建 active Task。
- 自动批准外部副作用。
- 把失败路径或临时观察工具固化进 Skill。

## 7. 阶段计划

### Phase A — 轻量记录基础（当前已部分具备）

- Run 记录 `execution_memory`。
- 记录 `activeWindowBefore` / `activeWindowAfter`。
- 记录 `planCompliance`。
- 保存用户输入、Run steps 和 tool results。

### Phase B — Demo 提取器

- 从指定 Run 读取 `execution_memory`。
- 过滤观察工具和失败路径。
- 识别可参数化字段。
- 生成可审阅的流程摘要。

### Phase C — Skill Draft 生成

- 将流程摘要转换为 draft Skill YAML。
- 自动标记风险步骤。
- 对包含视觉 fallback、坐标点击、计划偏离的步骤标记 `needsReview`。

### Phase D — Task Draft 生成

- 从 published Skill 生成 Task draft。
- 提供 Cron、timezone、参数和外部副作用授权编辑。
- Task 仍走现有 `validate_task_document`。

### Phase E — Electron UI

- Demo Run 页面增加“保存为 Skill”。
- Skill 编辑器展示提取步骤、参数和风险标记。
- Task 页面从 published Skill 创建 schedule。

## 8. 验收标准

1. 用户完成一次 Teams 发送消息 demo 后，可以生成 draft Skill。
2. draft Skill 中收件人和消息被参数化，不固化测试值。
3. 临时 `list_windows`、截图、失败重试不会进入稳定步骤。
4. 发送步骤自动标记 `external_side_effect`。
5. 用户必须 validate/publish 后才能创建 unattended Task。
6. 生成的 Task draft 可通过现有 Task 校验。
