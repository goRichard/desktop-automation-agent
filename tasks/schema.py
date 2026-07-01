"""Versioned Task schema for scheduled unattended Skill execution."""
from __future__ import annotations

from enum import Enum
from typing import Any, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from apscheduler.triggers.cron import CronTrigger
from pydantic import BaseModel, ConfigDict, Field, model_validator


class TaskStatus(str, Enum):
    ACTIVE = "active"
    PAUSED = "paused"
    DELETED = "deleted"


class TaskMetadata(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(pattern=r"^[a-z0-9][a-z0-9._-]*$")
    name: str = Field(min_length=1, max_length=120)


class TaskSchedule(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    cron: str
    timezone: str = "Asia/Shanghai"
    misfire_policy: Literal["run_once", "skip"] = Field(
        default="run_once", alias="misfirePolicy"
    )
    max_concurrent_runs: Literal[1] = Field(default=1, alias="maxConcurrentRuns")

    @model_validator(mode="after")
    def validate_schedule(self) -> "TaskSchedule":
        try:
            timezone = ZoneInfo(self.timezone)
        except ZoneInfoNotFoundError as error:
            raise ValueError(f"Unknown timezone: {self.timezone}") from error
        try:
            CronTrigger.from_crontab(self.cron, timezone=timezone)
        except ValueError as error:
            raise ValueError(f"Invalid cron expression: {self.cron}") from error
        return self


class TaskSkillReference(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    version: str = Field(pattern=r"^\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?$")


class TaskExecution(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    mode: Literal["unattended"] = "unattended"
    timeout_seconds: int = Field(default=300, alias="timeoutSeconds", ge=1, le=86400)
    retries: int = Field(default=0, ge=0, le=5)


class TaskPermissions(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    external_side_effects_approved: bool = Field(
        default=False, alias="externalSideEffectsApproved"
    )


class TaskDocument(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    api_version: Literal["desktop-agent/v1alpha1"] = Field(
        default="desktop-agent/v1alpha1", alias="apiVersion"
    )
    kind: Literal["Task"] = "Task"
    metadata: TaskMetadata
    schedule: TaskSchedule
    skill: TaskSkillReference
    parameters: dict[str, Any] = Field(default_factory=dict)
    execution: TaskExecution = Field(default_factory=TaskExecution)
    permissions: TaskPermissions = Field(default_factory=TaskPermissions)
