"""
数据库模型定义（SQLModel）
业务表：会话、消息、任务、记忆，以及 Runtime Run/Step/Event 记录
"""
from datetime import UTC, datetime
from enum import Enum
from typing import List, Optional
from uuid import uuid4

from sqlmodel import Field, Relationship, SQLModel


def utc_now_naive() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


# ══════════════════════════════════════════════════════
# 枚举类型
# ══════════════════════════════════════════════════════

class MessageRole(str, Enum):
    system    = "system"
    user      = "user"
    assistant = "assistant"
    tool      = "tool"


class JobStatus(str, Enum):
    active  = "active"
    paused  = "paused"
    deleted = "deleted"


class ExecutionStatus(str, Enum):
    running = "running"
    success = "success"
    failed  = "failed"


class MemoryCategory(str, Enum):
    fact       = "fact"        # 事实性记忆
    preference = "preference"  # 用户偏好
    context    = "context"     # 上下文信息
    skill_hint = "skill_hint"  # 技能使用经验


# ══════════════════════════════════════════════════════
# 表 1：sessions — 会话表
# ══════════════════════════════════════════════════════

class Session(SQLModel, table=True):
    __tablename__ = "sessions"

    id: str = Field(
        default_factory=lambda: str(uuid4()),
        primary_key=True,
    )
    title: Optional[str] = Field(default=None, description="首条消息前20字（自动生成）")
    created_at: datetime = Field(default_factory=utc_now_naive)
    updated_at: datetime = Field(default_factory=utc_now_naive)

    messages: List["Message"] = Relationship(back_populates="session")


# ══════════════════════════════════════════════════════
# 表 2：messages — 消息历史表
# ══════════════════════════════════════════════════════

class Message(SQLModel, table=True):
    __tablename__ = "messages"

    id: str = Field(
        default_factory=lambda: str(uuid4()),
        primary_key=True,
    )
    session_id: str = Field(foreign_key="sessions.id", index=True)
    role: MessageRole
    content: Optional[str] = Field(default=None, description="文本内容")
    # 工具调用相关（role=assistant 且 LLM 发起工具调用时）
    tool_calls: Optional[str] = Field(
        default=None,
        description="JSON 序列化的工具调用列表 [{id, name, arguments}]",
    )
    # 工具返回结果相关（role=tool 时）
    tool_call_id: Optional[str] = Field(default=None, description="对应的 tool call id")
    tool_name: Optional[str] = Field(default=None, description="工具名称")
    # 可选统计
    token_count: Optional[int] = Field(default=None, description="token 数量，用于上下文管理")
    created_at: datetime = Field(default_factory=utc_now_naive)

    session: Optional[Session] = Relationship(back_populates="messages")


# ══════════════════════════════════════════════════════
# 表 3：scheduled_jobs — 定时任务表
# ══════════════════════════════════════════════════════

class ScheduledJob(SQLModel, table=True):
    __tablename__ = "scheduled_jobs"

    id: str = Field(
        default_factory=lambda: str(uuid4()),
        primary_key=True,
    )
    name: str = Field(description="任务描述，用户可读")
    cron_expr: str = Field(description="cron 表达式，如 '0 9 * * *'")
    skill_name: str = Field(description="触发的 skill 名称")
    params: str = Field(default="{}", description="JSON 格式的参数")
    status: JobStatus = Field(default=JobStatus.active)
    created_at: datetime = Field(default_factory=utc_now_naive)
    last_run_at: Optional[datetime] = Field(default=None)
    next_run_at: Optional[datetime] = Field(default=None)
    run_count: int = Field(default=0, description="累计执行次数")
    last_result: Optional[str] = Field(default=None, description="最近一次执行结果摘要")

    logs: List["JobExecutionLog"] = Relationship(back_populates="job")


# ══════════════════════════════════════════════════════
# 表 4：job_execution_logs — 任务执行日志表
# ══════════════════════════════════════════════════════

class JobExecutionLog(SQLModel, table=True):
    __tablename__ = "job_execution_logs"

    id: str = Field(
        default_factory=lambda: str(uuid4()),
        primary_key=True,
    )
    job_id: str = Field(foreign_key="scheduled_jobs.id", index=True)
    started_at: datetime = Field(default_factory=utc_now_naive)
    finished_at: Optional[datetime] = Field(default=None)
    status: ExecutionStatus = Field(default=ExecutionStatus.running)
    result: Optional[str] = Field(default=None, description="执行输出摘要")
    error: Optional[str] = Field(default=None, description="错误信息（失败时）")
    session_id: Optional[str] = Field(default=None, description="本次执行产生的会话 id")

    job: Optional[ScheduledJob] = Relationship(back_populates="logs")


# ══════════════════════════════════════════════════════
# 表 5：agent_memory — 跨会话记忆表
# ══════════════════════════════════════════════════════

class AgentMemory(SQLModel, table=True):
    __tablename__ = "agent_memory"

    id: str = Field(
        default_factory=lambda: str(uuid4()),
        primary_key=True,
    )
    key: str = Field(unique=True, index=True, description="记忆唯一标识，如 'user.name'")
    value: str = Field(description="记忆内容")
    category: MemoryCategory = Field(default=MemoryCategory.fact)
    source_session_id: Optional[str] = Field(default=None, description="来自哪个会话")
    created_at: datetime = Field(default_factory=utc_now_naive)
    updated_at: datetime = Field(default_factory=utc_now_naive)
    expires_at: Optional[datetime] = Field(default=None, description="可选过期时间")


# ══════════════════════════════════════════════════════
# Runtime 持久化
# ══════════════════════════════════════════════════════

class RuntimeRun(SQLModel, table=True):
    __tablename__ = "runtime_runs"

    id: str = Field(primary_key=True)
    session_id: str = Field(index=True)
    user_input: str
    status: str = Field(index=True)
    created_at: str
    started_at: Optional[str] = None
    finished_at: Optional[str] = None
    error: Optional[str] = None
    output: str = ""


class RuntimeRunContext(SQLModel, table=True):
    __tablename__ = "runtime_run_contexts"

    run_id: str = Field(foreign_key="runtime_runs.id", primary_key=True)
    run_type: str = Field(default="agent", index=True)
    skill_id: Optional[str] = Field(default=None, index=True)
    skill_version: Optional[str] = None
    execution_mode: Optional[str] = None
    inputs: str = Field(default="{}", description="JSON encoded Skill inputs")
    token_usage: str = Field(default="{}", description="JSON encoded model token usage")
    execution_memory: str = Field(default="[]", description="JSON encoded action memory")


class RuntimeStepRun(SQLModel, table=True):
    __tablename__ = "runtime_step_runs"

    id: str = Field(primary_key=True)
    run_id: str = Field(foreign_key="runtime_runs.id", index=True)
    name: str
    tool_names: str = Field(default="[]", description="JSON encoded tool names")
    status: str = Field(index=True)
    started_at: Optional[str] = None
    finished_at: Optional[str] = None
    result: Optional[str] = None
    error: Optional[str] = None


class RuntimeEventRecord(SQLModel, table=True):
    __tablename__ = "runtime_events"

    id: str = Field(primary_key=True)
    run_id: str = Field(foreign_key="runtime_runs.id", index=True)
    sequence: int = Field(index=True)
    type: str = Field(index=True)
    data: str = Field(default="{}", description="JSON encoded event payload")
    timestamp: str


class RuntimeEvidenceRecord(SQLModel, table=True):
    __tablename__ = "runtime_evidence"

    id: str = Field(primary_key=True)
    run_id: str = Field(foreign_key="runtime_runs.id", index=True)
    step_id: str = Field(index=True)
    kind: str = Field(default="failure")
    path: str
    metadata_json: str = Field(default="{}")
    created_at: str


class AutomationTaskRecord(SQLModel, table=True):
    __tablename__ = "automation_tasks"

    id: str = Field(primary_key=True)
    name: str
    document: str = Field(description="JSON encoded Task document")
    cron_expr: str
    timezone: str
    skill_id: str = Field(index=True)
    skill_version: str
    status: str = Field(default="active", index=True)
    created_at: datetime = Field(default_factory=utc_now_naive)
    updated_at: datetime = Field(default_factory=utc_now_naive)
    last_run_at: Optional[datetime] = None
    next_run_at: Optional[datetime] = None
    run_count: int = 0
    last_result: Optional[str] = None


class TaskExecutionRecord(SQLModel, table=True):
    __tablename__ = "task_executions"

    id: str = Field(default_factory=lambda: str(uuid4()), primary_key=True)
    task_id: str = Field(foreign_key="automation_tasks.id", index=True)
    run_id: Optional[str] = Field(default=None, index=True)
    attempt: int = 1
    status: str = Field(default="running", index=True)
    started_at: datetime = Field(default_factory=utc_now_naive)
    finished_at: Optional[datetime] = None
    result: Optional[str] = None
    error: Optional[str] = None


class SkillRecord(SQLModel, table=True):
    __tablename__ = "skills"

    id: str = Field(primary_key=True)
    name: str = Field(index=True)
    description: str = ""
    latest_version: str
    published_version: Optional[str] = None
    created_at: datetime = Field(default_factory=utc_now_naive)
    updated_at: datetime = Field(default_factory=utc_now_naive)


class SkillVersionRecord(SQLModel, table=True):
    __tablename__ = "skill_versions"

    id: str = Field(default_factory=lambda: str(uuid4()), primary_key=True)
    skill_id: str = Field(foreign_key="skills.id", index=True)
    version: str = Field(index=True)
    status: str = Field(index=True)
    document: str = Field(description="JSON encoded Skill document")
    source_format: str = "yaml"
    created_at: datetime = Field(default_factory=utc_now_naive)
    updated_at: datetime = Field(default_factory=utc_now_naive)
    validated_at: Optional[datetime] = None
    published_at: Optional[datetime] = None


class SchemaMigration(SQLModel, table=True):
    __tablename__ = "schema_migrations"

    version: int = Field(primary_key=True)
    applied_at: datetime = Field(default_factory=utc_now_naive)
