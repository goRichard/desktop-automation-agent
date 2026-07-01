"""Versioned Skill document schema used by the editor and runtime."""
from __future__ import annotations

import re
from enum import Enum
from typing import Any, Literal, Optional

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator


class SkillStatus(str, Enum):
    DRAFT = "draft"
    TESTING = "testing"
    VALIDATED = "validated"
    PUBLISHED = "published"
    DEPRECATED = "deprecated"


class ExecutionMode(str, Enum):
    STEP = "step"
    GUIDED = "guided"
    UNATTENDED = "unattended"


class InputType(str, Enum):
    STRING = "string"
    INTEGER = "integer"
    NUMBER = "number"
    BOOLEAN = "boolean"
    ARRAY = "array"
    OBJECT = "object"


class SkillMetadata(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(pattern=r"^[a-z0-9][a-z0-9._-]*$")
    name: str = Field(min_length=1, max_length=120)
    version: str = Field(pattern=r"^\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?$")
    status: SkillStatus = SkillStatus.DRAFT
    description: str = ""
    tags: list[str] = Field(default_factory=list)
    triggers: list[str] = Field(default_factory=list)


class ApplicationRequirement(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    process: Optional[str] = None
    package: Optional[str] = None
    required: bool = True


class SkillInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: InputType = InputType.STRING
    description: str = ""
    required: bool = False
    default: Any = None
    items: Optional[InputType] = None


class RetryPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    max_attempts: int = Field(default=1, alias="maxAttempts", ge=1, le=10)
    delay_seconds: float = Field(default=0, alias="delaySeconds", ge=0, le=60)


class VerificationRule(BaseModel):
    model_config = ConfigDict(extra="allow", populate_by_name=True)

    type: str
    timeout_seconds: float = Field(default=5, alias="timeoutSeconds", ge=0, le=120)
    locator: dict[str, Any] = Field(default_factory=dict)


class FallbackDefinition(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    type: Literal["agent"] = "agent"
    instruction: Optional[str] = None
    allowed_tools: list[str] = Field(default_factory=list, alias="allowedTools")


class StepPolicy(BaseModel):
    model_config = ConfigDict(extra="allow", populate_by_name=True)

    skip_when: Optional[str] = Field(default=None, alias="skipWhen")


class SkillStep(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
    name: str = Field(min_length=1)
    action: str = Field(min_length=1)
    parameters: dict[str, Any] = Field(default_factory=dict, alias="with")
    target: dict[str, Any] = Field(default_factory=dict)
    instruction: Optional[str] = None
    allowed_tools: list[str] = Field(default_factory=list, alias="allowedTools")
    fallback: Optional[FallbackDefinition] = None
    policy: Optional[StepPolicy] = None
    verify: Optional[VerificationRule] = None
    retry: RetryPolicy = Field(default_factory=RetryPolicy)
    on_failure: Literal["stop", "continue"] = Field(default="stop", alias="onFailure")
    risk: Literal["low", "medium", "high", "external_side_effect"] = "low"


class SkillExecution(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    default_mode: ExecutionMode = Field(default=ExecutionMode.GUIDED, alias="defaultMode")
    timeout_seconds: int = Field(default=300, alias="timeoutSeconds", ge=1, le=86400)
    steps: list[SkillStep] = Field(min_length=1)

    @model_validator(mode="after")
    def unique_step_ids(self) -> "SkillExecution":
        ids = [step.id for step in self.steps]
        if len(ids) != len(set(ids)):
            raise ValueError("Skill step ids must be unique")
        return self


class SkillDocument(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    api_version: Literal["desktop-agent/v1alpha1"] = Field(
        default="desktop-agent/v1alpha1", alias="apiVersion"
    )
    kind: Literal["Skill"] = "Skill"
    metadata: SkillMetadata
    applications: list[ApplicationRequirement] = Field(default_factory=list)
    inputs: dict[str, SkillInput] = Field(default_factory=dict)
    execution: SkillExecution

    def to_yaml(self) -> str:
        value = self.model_dump(mode="json", by_alias=True, exclude_none=True)
        return yaml.safe_dump(value, allow_unicode=True, sort_keys=False)

    @classmethod
    def from_yaml(cls, raw: str) -> "SkillDocument":
        value = yaml.safe_load(raw)
        if not isinstance(value, dict):
            raise ValueError("Skill YAML root must be an object")
        return cls.model_validate(value)


def normalize_skill_id(value: str) -> str:
    """Create a stable editor-safe id for imported legacy Markdown skills."""
    normalized = re.sub(r"[^a-z0-9._-]+", "-", value.lower()).strip("-._")
    return normalized or "imported-skill"
