"""In-memory Runtime domain models.

Persistence is intentionally kept outside these dataclasses so the same state
machine can later be backed by SQLite and streamed over WebSocket.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Optional
from uuid import uuid4


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def empty_token_usage() -> dict[str, Any]:
    return {
        "model_calls": 0,
        "reported_calls": 0,
        "input_tokens": 0,
        "output_tokens": 0,
        "total_tokens": 0,
        "cached_input_tokens": 0,
        "by_role": {},
    }


class RunStatus(str, Enum):
    QUEUED = "queued"
    PREPARING = "preparing"
    RUNNING = "running"
    WAITING_USER = "waiting_user"
    PAUSED = "paused"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


class StepStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    SKIPPED = "skipped"


TERMINAL_RUN_STATUSES = {
    RunStatus.SUCCEEDED,
    RunStatus.FAILED,
    RunStatus.CANCELLED,
}


@dataclass
class StepRunState:
    run_id: str
    name: str
    tool_names: list[str]
    id: str = field(default_factory=lambda: str(uuid4()))
    status: StepStatus = StepStatus.PENDING
    started_at: Optional[str] = None
    finished_at: Optional[str] = None
    result: Optional[str] = None
    error: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["status"] = self.status.value
        return value


@dataclass
class RunState:
    session_id: str
    user_input: str
    run_type: str = "agent"
    skill_id: Optional[str] = None
    skill_version: Optional[str] = None
    execution_mode: Optional[str] = None
    inputs: dict[str, Any] = field(default_factory=dict)
    pending_confirmation: Optional[dict[str, Any]] = None
    id: str = field(default_factory=lambda: str(uuid4()))
    status: RunStatus = RunStatus.QUEUED
    created_at: str = field(default_factory=utc_now)
    started_at: Optional[str] = None
    finished_at: Optional[str] = None
    error: Optional[str] = None
    output: str = ""
    token_usage: dict[str, Any] = field(default_factory=empty_token_usage)
    steps: list[StepRunState] = field(default_factory=list)

    @property
    def is_terminal(self) -> bool:
        return self.status in TERMINAL_RUN_STATUSES

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["status"] = self.status.value
        value["steps"] = [step.to_dict() for step in self.steps]
        return value


@dataclass(frozen=True)
class RunEvent:
    run_id: str
    sequence: int
    type: str
    data: dict[str, Any]
    id: str = field(default_factory=lambda: str(uuid4()))
    timestamp: str = field(default_factory=utc_now)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
