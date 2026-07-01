"""
任务规划数据模型：TaskPlan / TaskStep / TaskStatus
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import List, Optional


class TaskStatus(str, Enum):
    """任务步骤状态"""
    PENDING = "pending"      # 待执行 ○
    RUNNING = "running"      # 执行中 ▶
    DONE = "done"           # 已完成 ✓
    FAILED = "failed"       # 失败 ✗
    SKIPPED = "skipped"     # 已跳过 ⊘


@dataclass
class TaskStep:
    """单个任务步骤"""
    id: int                          # 步骤序号（从 1 开始）
    description: str                 # 步骤描述
    status: TaskStatus = TaskStatus.PENDING
    tool_used: Optional[str] = None  # 使用的工具名
    expected_tools: List[str] = field(default_factory=list)  # 计划要求的工具
    completed_tools: List[str] = field(default_factory=list)  # 已成功执行的工具
    result: Optional[str] = None     # 执行结果摘要
    error: Optional[str] = None      # 错误信息（失败时）


@dataclass
class TaskPlan:
    """任务执行计划"""
    goal: str                        # 用户原始目标
    steps: List[TaskStep] = field(default_factory=list)
    current_step_index: int = 0      # 当前执行步骤索引（从 0 开始）
    created_at: Optional[str] = None  # 创建时间（ISO 格式）

    def __post_init__(self):
        if self.created_at is None:
            self.created_at = datetime.now().isoformat()

    # ── 计算属性 ──────────────────────────────────────

    @property
    def is_complete(self) -> bool:
        """所有步骤是否已完成（done 或 skipped）"""
        return all(
            s.status in (TaskStatus.DONE, TaskStatus.SKIPPED)
            for s in self.steps
        )

    @property
    def progress_text(self) -> str:
        """进度文本，如 '3/10'"""
        done = sum(1 for s in self.steps if s.status == TaskStatus.DONE)
        return f"{done}/{len(self.steps)}"

    @property
    def progress_percent(self) -> int:
        """进度百分比 (0-100)"""
        if not self.steps:
            return 0
        done = sum(1 for s in self.steps if s.status == TaskStatus.DONE)
        return int(done * 100 / len(self.steps))

    @property
    def current_step(self) -> Optional[TaskStep]:
        """当前正在执行的步骤"""
        if 0 <= self.current_step_index < len(self.steps):
            return self.steps[self.current_step_index]
        return None

    # ── 状态管理方法 ──────────────────────────────────

    def mark_running(self, step_id: int) -> None:
        """标记步骤为执行中"""
        step = self._find_step(step_id)
        if step:
            step.status = TaskStatus.RUNNING
            self.current_step_index = self.steps.index(step)

    def mark_done(self, step_id: int, result: str, tool_used: Optional[str] = None) -> None:
        """标记步骤为完成"""
        step = self._find_step(step_id)
        if step:
            step.status = TaskStatus.DONE
            step.result = result
            if tool_used:
                step.tool_used = tool_used

    def mark_failed(self, step_id: int, error: str) -> None:
        """标记步骤为失败"""
        step = self._find_step(step_id)
        if step:
            step.status = TaskStatus.FAILED
            step.error = error

    def record_completed_tools(self, step_id: int, tool_names: List[str]) -> None:
        """记录当前步骤已成功执行的工具，保持首次出现顺序。"""
        step = self._find_step(step_id)
        if not step:
            return
        for tool_name in tool_names:
            if tool_name not in step.completed_tools:
                step.completed_tools.append(tool_name)

    def has_failed(self) -> bool:
        """计划中是否存在失败步骤。"""
        return any(step.status == TaskStatus.FAILED for step in self.steps)

    def mark_skipped(self, step_id: int, reason: str = "") -> None:
        """标记步骤为跳过"""
        step = self._find_step(step_id)
        if step:
            step.status = TaskStatus.SKIPPED
            step.result = reason or "已跳过"

    def advance_to_next(self) -> Optional[TaskStep]:
        """推进到下一个待执行步骤，返回该步骤或 None（全部完成）"""
        for i, step in enumerate(self.steps):
            if step.status == TaskStatus.PENDING:
                self.current_step_index = i
                return step
        self.current_step_index = len(self.steps)
        return None

    # ── 内部方法 ──────────────────────────────────────

    def _find_step(self, step_id: int) -> Optional[TaskStep]:
        """根据步骤 ID 查找步骤"""
        for step in self.steps:
            if step.id == step_id:
                return step
        return None
