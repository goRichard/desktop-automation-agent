"""
上下文组装：从 DB 加载历史消息 + 跨会话记忆 + Skills 摘要
构建 OpenAI messages 格式列表
"""
from __future__ import annotations

from typing import Any, Optional

from config import get_settings
from memory import (
    format_memories_for_prompt,
    get_messages,
    list_memories,
    messages_to_openai_format,
)
from skills.registry import find_matching_skill_async, get_skills_summary
from .planner import TaskPlan


def _trim_history(
    messages: list[dict[str, Any]],
    max_rounds: int,
) -> list[dict[str, Any]]:
    """
    裁剪旧对话轮次以控制上下文长度。
    保留 system prompt + 最近 max_rounds 轮用户对话，
    同时确保 tool_call / tool_result 配对不被割裂。
    """
    if len(messages) <= 1 or max_rounds < 1:
        return messages

    # 找到所有 user 消息的索引位置
    user_indices = [i for i, m in enumerate(messages) if m.get("role") == "user"]

    if len(user_indices) <= max_rounds:
        return messages

    # 保留 system 消息 + 最后 max_rounds 个 user 组
    keep_start = user_indices[-max_rounds]
    trimmed_count = len(user_indices) - max_rounds

    trimmed = messages[:1]  # system prompt
    trimmed.append({
        "role": "system",
        "content": (
            f"[上下文已压缩] 省略了 {trimmed_count} 轮较早的对话历史，"
            f"当前保留最近 {max_rounds} 轮对话。"
        ),
    })
    trimmed.extend(messages[keep_start:])
    return trimmed


_PLAN_FIRST_CONSTRAINT = (
    "## ⚠️ 最高优先级执行规则（覆盖所有其他规则）\n\n"
    "**以下规则优先级最高，当与其他规则冲突时，以本规则为准：**\n\n"
    "1. **严格按编号顺序执行**：从步骤 1 开始，逐步执行到最后一个步骤，不跳步、不合并、不提前执行后续步骤\n"
    "2. **单步执行原则**：每完成一个步骤后，等待该步骤的工具返回结果，确认成功后再执行下一步骤\n"
    "3. **执行失败立即上报**：已授权工具实际执行失败时，立即停止并截图报告用户，"
    "**禁止**自行尝试替代方案、重试、或切换到其他步骤\n"
    "4. **限制额外操作**：除 Runtime 提供的只读观察工具外，不执行计划以外的操作\n"
    "5. **工具选择约束（最重要）**：\n"
    "   - 如果计划步骤中指定了工具名（如 `click`、`find_and_click`、`run_actions`），**必须使用该工具**\n"
    "   - **禁止**使用计划未指定的副作用工具；Runtime 提供的只读观察工具可用于确认桌面状态\n"
    "   - **禁止**使用快捷键替代计划中指定的鼠标点击操作\n"
    "   - 例如：计划说 `click + find_and_click`，就必须用这两个工具，不能用 `press_key(Alt+N)` 替代\n"
    "6. **步骤解读约束**：每个步骤必须按照字面意思执行，不要'优化'或'改进'步骤描述\n\n"
    "**冲突解决**：当本计划与 system prompt 中的其他规则（如批量优化、元素发现优先级等）冲突时，**必须优先遵守本计划**。\n\n"
    "**违规检测**：策略越界调用不会被执行；收到纠正提示后必须改用当前提供的工具。"
)


def assemble_with_confirmed_plan(
    user_input: str,
    session_id: str,
    confirmed_plan: str,
) -> list[dict[str, Any]]:
    """
    Plan-First 模式的上下文组装：
    以用户已确认的执行计划作为最高优先级约束注入，替代 Skill 步骤详情。
    """
    settings = get_settings()
    system_parts = [settings.system_prompt.strip()]

    # Skills 摘要（仅摘要，不注入详细步骤，计划已包含具体化步骤）
    skills_summary = get_skills_summary()
    if skills_summary:
        system_parts.append(skills_summary)

    # 跨会话记忆
    memories = list_memories()
    memory_text = format_memories_for_prompt(memories)
    if memory_text:
        system_parts.append(memory_text)

    # 已确认的执行计划（最高优先级，紧靠 user 消息前注入）
    system_parts.append(
        f"## ⚠️ 当前执行计划（用户已确认，必须严格按步骤顺序执行）\n\n"
        f"{confirmed_plan}\n\n"
        f"{_PLAN_FIRST_CONSTRAINT}"
    )

    system_content = "\n\n".join(system_parts)
    messages: list[dict[str, Any]] = [{"role": "system", "content": system_content}]

    history = get_messages(session_id)
    if history:
        messages.extend(messages_to_openai_format(history))

    messages.append({"role": "user", "content": user_input})

    # 裁剪旧历史，防止上下文溢出
    return _trim_history(messages, settings.max_history_messages)


async def assemble(
    user_input: str,
    session_id: str,
    include_memories: bool = True,
) -> list[dict[str, Any]]:
    """
    组装完整的 messages 列表：
    1. System Prompt（含 Skills 摘要 + 记忆）
    2. 历史消息（从 DB 加载）
    3. 当前用户输入
    """
    settings = get_settings()

    # ── 1. System Prompt ─────────────────────────────
    system_parts = [settings.system_prompt.strip()]

    # 注入 Skills 摘要（始终注入，让模型知道有哪些 skill 可用）
    skills_summary = get_skills_summary()
    if skills_summary:
        system_parts.append(skills_summary)

    # ── 1.5 Skill 按需注入：触发词命中时注入完整执行步骤 ──
    matched_skill = await find_matching_skill_async(user_input)
    if matched_skill:
        system_parts.append(
            f"## 当前匹配技能：{matched_skill.name}\n"
            f"以下是该技能的完整执行步骤，请严格按照步骤执行：\n\n"
            f"{matched_skill.content}"
        )

    # 注入跨会话记忆
    if include_memories:
        memories = list_memories()
        memory_text = format_memories_for_prompt(memories)
        if memory_text:
            system_parts.append(memory_text)

    system_content = "\n\n".join(system_parts)
    messages: list[dict[str, Any]] = [{"role": "system", "content": system_content}]

    # ── 2. 历史消息 ───────────────────────────────────
    history = get_messages(session_id)
    if history:
        messages.extend(messages_to_openai_format(history))

    # ── 3. 当前用户输入 ───────────────────────────────
    messages.append({"role": "user", "content": user_input})

    # 裁剪旧历史，防止上下文溢出
    return _trim_history(messages, get_settings().max_history_messages)


# ══════════════════════════════════════════════════════
# 任务计划上下文管理（保留，供 LLM 主动调用 create_plan 工具时使用）
# ══════════════════════════════════════════════════════

_current_plan: Optional[TaskPlan] = None


def set_current_plan(plan: TaskPlan) -> None:
    """设置当前活动的任务计划"""
    global _current_plan
    _current_plan = plan

    # 同步到 tools.planner 的 store
    from tools.planner import set_plan_store
    set_plan_store(plan)


def get_current_plan() -> Optional[TaskPlan]:
    """获取当前活动的任务计划"""
    return _current_plan


def clear_current_plan() -> None:
    """清除当前任务计划"""
    global _current_plan
    _current_plan = None

    from tools.planner import set_plan_store
    set_plan_store(None)
