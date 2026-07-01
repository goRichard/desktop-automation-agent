from __future__ import annotations

import pytest
from pydantic import ValidationError

from memory import init_db
from tasks import TaskConflictError, TaskDocument, TaskRepository, TaskStatus


def task_value() -> dict:
    return {
        "apiVersion": "desktop-agent/v1alpha1",
        "kind": "Task",
        "metadata": {"id": "weekday-report", "name": "Weekday report"},
        "schedule": {
            "cron": "0 9 * * 1-5",
            "timezone": "Asia/Shanghai",
            "misfirePolicy": "run_once",
            "maxConcurrentRuns": 1,
        },
        "skill": {"id": "daily-report", "version": "1.0.0"},
        "parameters": {"recipient": "team@example.com"},
        "execution": {"mode": "unattended", "timeoutSeconds": 300, "retries": 1},
        "permissions": {"externalSideEffectsApproved": True},
    }


def test_task_schema_validates_cron_timezone_and_unattended_mode() -> None:
    document = TaskDocument.model_validate(task_value())
    assert document.execution.mode == "unattended"

    invalid = task_value()
    invalid["schedule"]["timezone"] = "Invalid/Timezone"
    with pytest.raises(ValidationError, match="Unknown timezone"):
        TaskDocument.model_validate(invalid)

    invalid = task_value()
    invalid["schedule"]["cron"] = "bad cron"
    with pytest.raises(ValidationError, match="Invalid cron"):
        TaskDocument.model_validate(invalid)


def test_task_repository_lifecycle(tmp_path) -> None:
    init_db(tmp_path / "tasks.db")
    repository = TaskRepository()
    document = TaskDocument.model_validate(task_value())
    created = repository.create(document)
    assert created["status"] == "active"
    assert created["skillVersion"] == "1.0.0"

    with pytest.raises(TaskConflictError):
        repository.create(document)

    paused = repository.set_status("weekday-report", TaskStatus.PAUSED)
    assert paused["status"] == "paused"
    document.metadata.name = "Updated report"
    updated = repository.update("weekday-report", document)
    assert updated["name"] == "Updated report"
