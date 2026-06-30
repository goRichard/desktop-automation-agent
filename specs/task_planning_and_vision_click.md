# 任务规划与动态 UI 识别 Spec

## 1. 概述

### 1.1 目标
为 Desktop-Agent 增加**任务规划**和**动态 UI 识别点击**能力：
- Agent 执行复杂任务前，先拆分为可执行步骤
- 逐步执行时实时展示进度和状态
- 按需调用截图和多模态视觉模型进行 UI 元素识别与定位

### 1.2 核心原则
- **按需调用**：截图、多模态分析、UI 识别**不是固定流程**，只在需要识别动态 UI 时才调用
- **格式规范**：多模态模型返回的坐标必须为 `[cx, cy]` 格式（屏幕绝对坐标），可直接用于 `click --on "cx,cy"`
- **状态透明**：每一步执行后更新任务状态，用户始终可见当前进度

---

## 2. 数据模型

### 2.1 TaskStatus（任务状态枚举）

```python
class TaskStatus(str, Enum):
    PENDING = "pending"    # 待执行 ○
    RUNNING = "running"    # 执行中 ▶
    DONE = "done"         # 已完成 ✓
    FAILED = "failed"     # 失败 ✗
    SKIPPED = "skipped"   # 已跳过 ⊘
```

### 2.2 TaskStep（任务步骤）

```python
@dataclass
class TaskStep:
    id: int                          # 步骤序号（从 1 开始）
    description: str                 # 步骤描述（如"打开计算器应用"）
    status: TaskStatus               # 当前状态
    tool_used: Optional[str] = None  # 使用的工具名（如"find_and_click"）
    result: Optional[str] = None     # 执行结果摘要
    error: Optional[str] = None      # 错误信息（失败时）
```

### 2.3 TaskPlan（任务计划）

```python
@dataclass
class TaskPlan:
    goal: str                        # 用户原始目标
    steps: List[TaskStep]            # 步骤列表
    current_step_index: int = 0      # 当前执行步骤索引（从 0 开始）
    created_at: Optional[str] = None # 创建时间（ISO 格式）
    
    # 计算属性
    @property
    def is_complete(self) -> bool
    @property
    def progress_text(self) -> str   # "3/10"
    @property
    def progress_percent(self) -> int  # 30
    
    # 状态管理方法
    def mark_running(step_id: int)
    def mark_done(step_id: int, result: str, tool_used: str = None)
    def mark_failed(step_id: int, error: str)
    def mark_skipped(step_id: int, reason: str)
```

---

## 3. 工具设计

### 3.1 `find_and_click` — 动态 UI 识别并点击

**位置**：`tools/vision.py`

**工具签名**：
```python
@tool(description="在屏幕截图上找到指定UI元素并点击。适合按钮、链接、菜单项等动态位置元素。target 为元素描述（如'登录按钮'、'提交'、'下一步'），window 为可选的窗口标题。自动完成：截图→多模态识别→解析坐标→点击。")
async def find_and_click(target: str, window: Optional[str] = None) -> str:
```

**执行流程**：
```
1. 调用 capture_image(output=tmp_path, window=window)
   → 内部执行: winpeekaboo image --output {tmp_path} --window {window}
2. 构造多模态 prompt：
   """
   请分析截图，找到"{target}"的位置。
   要求：
   - 返回该元素的中心坐标 [cx, cy]
   - cx 和 cy 是屏幕绝对坐标（像素值）
   - 只返回 JSON 数组格式，不要其他文字
   示例输出: [450, 320]
   """
3. 调用多模态模型 vision(image_path, prompt)
4. 解析返回内容，提取 [cx, cy]
   - 使用正则: r'\[\s*(\d+)\s*,\s*(\d+)\s*\]'
   - 解析失败返回错误提示
5. 调用 click(on="{cx},{cy}")
   → 内部执行: winpeekaboo click --on "{cx},{cy}"
6. 返回结果: "成功点击 '{target}' 于坐标 ({cx}, {cy})"
```

**错误处理**：
- 图片不存在 → 返回错误
- 多模态模型未返回坐标格式 → 返回错误 + 原始响应
- 点击失败 → 返回 winpeekaboo 错误信息

---

### 3.2 `find_element_position` — 仅识别位置不点击

**位置**：`tools/vision.py`

**工具签名**：
```python
@tool(description="在屏幕截图上找到指定UI元素的位置，返回坐标但不点击。适合需要确认元素位置或后续手动操作的场景。")
async def find_element_position(target: str, window: Optional[str] = None) -> str:
```

**执行流程**：
与 `find_and_click` 相同，但第 5 步改为返回坐标信息，不执行点击：
```
返回: "找到 '{target}'，中心坐标: ({cx}, {cy})"
```

---

### 3.3 `create_plan` — 创建任务计划

**位置**：`tools/planner.py`（新建）

**工具签名**：
```python
@tool(description="创建任务执行计划。将用户的复杂目标拆解为可执行的步骤列表。")
async def create_plan(goal: str) -> str:
```

**执行逻辑**：
1. 调用 LLM（非视觉模型）生成步骤列表
2. 构造 TaskPlan 对象并存入 Agent Context
3. 返回格式化的步骤列表（带序号和状态标记）

**System Prompt 增强**（生成计划时）：
```
你是一个任务规划专家。将用户的目标拆解为具体的、可执行的步骤。

要求：
1. 每个步骤应该是单一动作（如"打开应用"、"点击按钮"、"输入文本"）
2. 步骤数量适中（通常 3-15 步）
3. 涉及 UI 元素点击的步骤，标记为 [需视觉识别]
4. 不需要截图/视觉识别的步骤不要标记
5. 返回 JSON 数组格式：[{"description": "...", "needs_vision": true/false}]
```

---

### 3.4 `get_plan_status` — 获取当前计划状态

**位置**：`tools/planner.py`

**工具签名**：
```python
@tool(description="获取当前任务的执行进度和状态。返回带状态标记的步骤列表。")
async def get_plan_status() -> str:
```

**返回格式**：
```
📋 任务计划: 打开计算器计算 123+456

步骤  状态  描述
────  ────  ──────────────────────
  1   ✓    打开计算器应用
  2   ✓    截图识别当前界面
  3   ▶    找到并点击数字键 '1'  [需视觉识别]
  4   ○    找到并点击数字键 '2'  [需视觉识别]
  5   ○    找到并点击数字键 '3'  [需视觉识别]

进度: 2/5 ████████░░░░░░░░░░░░ 40%
```

---

## 4. Agent Loop 集成

### 4.1 修改位置
`agent/loop.py` 的 `run_stream()` 方法

### 4.2 执行流程

```
用户输入: "帮我打开计算器，输入123+456"
    ↓
┌──────────────────────────────────────────┐
│ Phase 1: 规划阶段                          │
│ 1. Agent 判断任务复杂度                    │
│    - 简单任务：直接执行（不生成计划）       │
│    - 复杂任务（多步骤）：调用 create_plan  │
│ 2. 渲染计划到 CLI                         │
└──────────────────────────────────────────┘
    ↓
┌──────────────────────────────────────────┐
│ Phase 2: 执行阶段                          │
│ 循环每一步：                               │
│   1. 更新步骤状态为 RUNNING               │
│   2. 渲染当前进度                          │
│   3. 正常 Agent Loop：                    │
│      LLM → tool call → execute           │
│   4. 更新步骤状态为 DONE/FAILED           │
│   5. 渲染更新后的进度                      │
│                                          │
│ 注意：                                    │
│ - 只有需要视觉识别的步骤才会调用截图       │
│ - 普通步骤（如 launch_app）直接执行        │
└──────────────────────────────────────────┘
    ↓
┌──────────────────────────────────────────┐
│ Phase 3: 完成阶段                          │
│ 所有步骤执行完毕：                         │
│ - 渲染最终状态                            │
│ - 返回执行总结                            │
└──────────────────────────────────────────┘
```

### 4.3 Agent Context 扩展

`agent/context.py` 增加：
```python
class AgentContext:
    # 新增字段
    current_plan: Optional[TaskPlan] = None
    
    def set_plan(self, plan: TaskPlan)
    def update_step_status(self, step_id: int, status: TaskStatus, **kwargs)
    def get_current_step(self) -> Optional[TaskStep]
```

---

## 5. CLI 显示规范

### 5.1 进度条渲染

使用 Rich 库的 `Progress` 或自定义文本进度条：

```python
def render_progress_bar(percent: int, width: int = 20) -> str:
    filled = int(width * percent / 100)
    return f"{'█' * filled}{'░' * (width - filled)} {percent}%"
```

### 5.2 状态标记

| 状态 | 标记 | 颜色 |
|------|------|------|
| PENDING | ○ | 灰色 |
| RUNNING | ▶ | 黄色 |
| DONE | ✓ | 绿色 |
| FAILED | ✗ | 红色 |
| SKIPPED | ⊘ | 蓝色 |

### 5.3 示例输出

```
📋 任务计划: 打开 Notepad 并输入 Hello World

步骤  状态  描述
────  ────  ────────────────────────────────
  1   ✓    打开记事本应用
  2   ✓    等待窗口加载完成
  3   ▶    在文本区域输入 "Hello World"

进度: 2/3 ████████████████░░░░ 67%

─────────────────────────────────────────────

▶ 正在执行: 在文本区域输入 "Hello World"
✓ 步骤 3 完成

─────────────────────────────────────────────

✅ 任务完成！进度: 3/3 ████████████████████ 100%
```

---

## 6. 多模态输出格式规范

### 6.1 强制约束

**多模态模型必须返回**：
```
[cx, cy]
```

**示例**：
```
[450, 320]
```

### 6.2 Prompt 模板

```python
VISION_BBOX_PROMPT = """
请分析截图，找到"{target}"的位置。

要求：
1. 返回该元素的中心坐标 [cx, cy]
2. cx 和 cy 是屏幕绝对坐标（像素值）
3. 只返回 JSON 数组格式，不要其他文字
4. 坐标值必须是整数

示例输出: [450, 320]
"""
```

### 6.3 解析逻辑

```python
import re

def parse_vision_bbox(response: str) -> Optional[Tuple[int, int]]:
    """
    解析多模态模型返回的坐标
    支持格式: [450, 320] 或 [450,320]
    """
    match = re.search(r'\[\s*(\d+)\s*,\s*(\d+)\s*\]', response)
    if match:
        return int(match.group(1)), int(match.group(2))
    return None
```

---

## 7. 文件清单

### 7.1 新建文件
| 文件 | 说明 |
|------|------|
| `agent/planner.py` | TaskPlan/TaskStep/TaskStatus 数据模型 |
| `tools/planner.py` | create_plan、get_plan_status 工具 |

### 7.2 修改文件
| 文件 | 修改内容 |
|------|----------|
| `tools/vision.py` | 新增 `find_and_click`、`find_element_position` |
| `agent/loop.py` | 集成规划+执行状态展示逻辑 |
| `agent/context.py` | 增加 current_plan 字段和方法 |
| `cli/display.py` | 新增计划渲染和进度条函数 |

### 7.3 不修改文件
| 文件 | 说明 |
|------|------|
| `llm/client.py` | 无需修改（vision 方法已支持） |
| `tools/winpeekaboo.py` | 无需修改（capture_image、click 已支持） |
| 说明 | capture_image() 内部调用 winpeekaboo image 命令 |
| `config.yaml` | 无需修改 |

---

## 8. 边界情况处理

### 8.1 多模态模型返回非坐标格式
```
错误处理：
1. 尝试用正则解析，失败则重试一次
2. 重试 prompt: "请只返回坐标格式 [cx, cy]，不要其他内容"
3. 仍然失败 → 返回错误："无法识别目标位置，请提供更详细的描述"
```

### 8.2 坐标超出屏幕范围
```
验证逻辑：
1. 获取屏幕尺寸（通过 winpeekaboo see 或系统 API）
2. 检查 cx, cy 是否在 [0, screen_width] × [0, screen_height] 范围内
3. 超出范围 → 返回错误："识别坐标 ({cx}, {cy}) 超出屏幕范围"
```

### 8.3 简单任务不需要计划
```
判断逻辑：
- 如果用户输入是简单指令（如"打开计算器"、"截个图"）
- Agent 直接执行，不生成计划
- 只有多步骤任务（2步以上）才生成计划
```

### 8.4 执行中途失败
```
处理策略：
1. 当前步骤标记为 FAILED
2. Agent 尝试重试或跳过
3. 用户可选择：
   - 继续执行后续步骤
   - 终止任务
   - 修改后重新规划
```

---

## 9. 示例场景

### 场景 1: 打开计算器计算

**用户输入**：
```
帮我打开计算器，计算 123+456，然后截图保存结果
```

**生成计划**：
```
📋 任务计划: 打开计算器计算 123+456 并保存结果

步骤  状态  描述              类型
────  ────  ────────────────  ──────────
  1   ✓    打开计算器应用       常规操作
  2   ✓    等待窗口加载         等待
  3   ▶    点击数字键 '1'      [需视觉识别]
  4   ○    点击数字键 '2'      [需视觉识别]
  5   ○    点击数字键 '3'      [需视觉识别]
  6   ○    点击 '+' 按钮       [需视觉识别]
  7   ○    点击数字键 '4'      [需视觉识别]
  8   ○    点击数字键 '5'      [需视觉识别]
  9   ○    点击数字键 '6'      [需视觉识别]
 10   ○    点击 '=' 按钮       [需视觉识别]
 11   ○    截图保存结果        常规操作

进度: 2/11 ████░░░░░░░░░░░░░░░░ 18%
```

### 场景 2: 简单任务（无需计划）

**用户输入**：
```
截个图
```

**执行流程**：
```
直接调用 capture_image 工具，不生成计划
```

### 场景 3: 填写表单

**用户输入**：
```
在浏览器中打开登录页面，输入用户名 alice 和密码 123456，然后点击登录
```

**生成计划**：
```
📋 任务计划: 填写登录表单并提交

步骤  状态  描述              类型
────  ────  ────────────────  ──────────
  1   ✓    打开浏览器           常规操作
  2   ✓    导航到登录页面       常规操作
  3   ✓    等待页面加载         等待
  4   ▶    找到用户名输入框     [需视觉识别]
  5   ○    输入 "alice"        常规操作
  6   ○    找到密码输入框       [需视觉识别]
  7   ○    输入 "123456"       常规操作
  8   ○    找到并点击登录按钮   [需视觉识别]

进度: 3/8 ██████████░░░░░░░░░░ 38%
```

---

## 10. 验收标准

- [ ] `TaskPlan` 数据模型可正常创建和更新状态
- [ ] `find_and_click` 工具可成功识别并点击指定 UI 元素
- [ ] `create_plan` 工具生成的计划格式正确
- [ ] `get_plan_status` 返回带状态标记的步骤列表
- [ ] CLI 正确渲染进度条和步骤状态
- [ ] 多模态模型返回坐标格式严格验证
- [ ] 简单任务不生成计划
- [ ] 复杂任务自动拆分步骤
- [ ] 执行失败时正确标记并提示用户
- [ ] 截图和多模态调用仅在需要时触发（按需调用）
