"""Cross-entry-point safety validation for unattended Tasks."""
from __future__ import annotations

from skills.executor import SkillExecutionError, SkillExecutor
from skills.repository import SkillNotFoundError, SkillRepository
from skills.schema import SkillDocument, SkillStatus

from .schema import TaskDocument


class TaskValidationError(ValueError):
    pass


def validate_task_document(document: TaskDocument, skills: SkillRepository) -> None:
    try:
        value = skills.get_version(document.skill.id, document.skill.version)
    except SkillNotFoundError as error:
        raise TaskValidationError(str(error)) from error
    if value["status"] != SkillStatus.PUBLISHED.value:
        raise TaskValidationError("Task must reference a published Skill version")
    skill = SkillDocument.model_validate(value["document"])
    try:
        SkillExecutor.validate_inputs(skill, document.parameters)
    except SkillExecutionError as error:
        raise TaskValidationError(str(error)) from error
    _validate_steps(
        skill,
        skills,
        document.permissions.external_side_effects_approved,
        set(),
    )


def _validate_steps(
    skill: SkillDocument,
    skills: SkillRepository,
    external_side_effects_approved: bool,
    visited: set[str],
) -> None:
    skill_key = f"{skill.metadata.id}@{skill.metadata.version}"
    if skill_key in visited:
        raise TaskValidationError(f"Recursive Task Skill call: {skill_key}")
    visited = {*visited, skill_key}
    for step in skill.execution.steps:
        if step.action == "user.confirm":
            can_skip = bool(
                external_side_effects_approved
                and step.policy
                and step.policy.skip_when == "unattendedApproved"
            )
            if not can_skip:
                raise TaskValidationError(
                    f"Unattended Task cannot resolve confirmation step: {step.id}"
                )
        if step.risk == "external_side_effect" and not external_side_effects_approved:
            raise TaskValidationError(f"External side effect is not approved: {step.id}")
        if step.action == "powershell.runApproved":
            raise TaskValidationError("Approved PowerShell script registry is not configured")
        if step.action == "skill.call":
            skill_id = step.parameters.get("skillId") or step.parameters.get("skill_id")
            version = step.parameters.get("version")
            if not isinstance(skill_id, str) or not isinstance(version, str):
                raise TaskValidationError(
                    f"Task skill.call must use a fixed id and version: {step.id}"
                )
            try:
                child_value = skills.get_version(skill_id, version)
            except SkillNotFoundError as error:
                raise TaskValidationError(str(error)) from error
            if child_value["status"] != SkillStatus.PUBLISHED.value:
                raise TaskValidationError(
                    f"Nested Task Skill is not published: {skill_id}@{version}"
                )
            _validate_steps(
                SkillDocument.model_validate(child_value["document"]),
                skills,
                external_side_effects_approved,
                visited,
            )
