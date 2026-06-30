"""
任务规划工具：创建计划、查询进度
"""
from __future__ import annotations

import json
from typing import Optional

from llm import get_llm_client
from .registry import tool

# 延迟导入避免循环依赖
_plan_store: Optional["TaskPlan"] = None


def set_plan_store(plan: "TaskPlan") -> None:
    """设置当前活动计划（由 AgentLoop 调用）"""
    global _plan_store
    _plan_store = plan


def get_plan_store() -> Optional["TaskPlan"]:
    """获取当前活动计划"""
    return _plan_store


@tool(description="创建任务执行计划。将用户的复杂目标拆解为可执行的步骤列表。goal 为完整的目标描述。")
async def create_plan(goal: str) -> str:
    """
    调用 LLM 生成任务计划，返回格式化的步骤列表
    """
    from agent.planner import TaskPlan, TaskStep, TaskStatus

    client = get_llm_client()

    prompt = (
        f"你是一个任务规划专家。将以下目标拆解为具体的、可执行的步骤：\n\n"
        f"目标: {goal}\n\n"
        f"要求:\n"
        f"1. 每个步骤应该是单一动作（如'打开应用'、'点击按钮'、'输入文本'）\n"
        f"2. 步骤数量适中（通常 3-15 步）\n"
        f"3. 涉及 UI 元素点击的步骤，标记 needs_vision 为 true\n"
        f"4. 不需要截图/视觉识别的步骤，标记 needs_vision 为 false\n"
        f"5. 返回 JSON 数组格式，不要其他文字:\n"
        f'[{{"description": "...", "needs_vision": true}}, ...]\n\n'
        f"示例输出:\n"
        f'[{{"description": "打开记事本应用", "needs_vision": false}}, '
        f'{{"description": "点击文本输入区域", "needs_vision": true}}, '
        f'{{"description": "输入 Hello World", "needs_vision": false}}]'
    )

    messages = [
        {"role": "system", "content": "你是一个任务规划助手，只返回 JSON 格式。"},
        {"role": "user", "content": prompt},
    ]

    try:
        response = await client.chat(messages)
        content = response.content.strip()

        # 解析 JSON
        steps_data = json.loads(content)
        steps = [
            TaskStep(
                id=i + 1,
                description=step["description"],
                status=TaskStatus.PENDING,
            )
            for i, step in enumerate(steps_data)
        ]

        # 创建计划并通过 agent.context 统一设置（它内部会同步到 plan_store）
        plan = TaskPlan(goal=goal, steps=steps)
        from agent.context import set_current_plan
        set_current_plan(plan)

        # 返回格式化的计划
        return _format_plan(plan)

    except json.JSONDecodeError as e:
        return f"计划生成失败（JSON 解析错误）: {e}\n原始响应: {content[:200]}"
    except Exception as e:
        return f"计划生成失败: {type(e).__name__}: {e}"


@tool(description="获取当前任务的执行进度和状态。返回带状态标记的步骤列表。")
async def get_plan_status() -> str:
    """
    返回当前计划的进度和状态
    """
    plan = get_plan_store()
    if plan is None:
        return "当前没有活动的任务计划"

    return _format_plan(plan)


def _format_plan(plan: "TaskPlan") -> str:
    """格式化计划为可读文本"""
    lines = [f"📋 任务计划: {plan.goal}", ""]
    lines.append(f"步骤  状态  描述")
    lines.append(f"────  ────  ──────────────────────")

    status_marks = {
        "pending": "○",
        "running": "▶",
        "done": "✓",
        "failed": "✗",
        "skipped": "⊘",
    }

    for step in plan.steps:
        mark = status_marks.get(step.status.value, "○")
        lines.append(f"  {step.id:<2}  {mark}    {step.description}")

    lines.append("")
    lines.append(f"进度: {plan.progress_text} {plan.progress_percent}%")

    return "\n".join(lines)
