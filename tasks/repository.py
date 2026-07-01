"""Persistence facade for versioned automation Tasks."""
from __future__ import annotations

import json
from typing import Any

from memory import store

from .schema import TaskDocument, TaskStatus


class TaskNotFoundError(LookupError):
    pass


class TaskConflictError(ValueError):
    pass


class TaskRepository:
    def create(self, document: TaskDocument) -> dict[str, Any]:
        value = document.model_dump(mode="json", by_alias=True)
        try:
            record = store.create_task_record(value)
        except ValueError as error:
            raise TaskConflictError(str(error)) from error
        return self._to_dict(record)

    def update(self, task_id: str, document: TaskDocument) -> dict[str, Any]:
        if document.metadata.id != task_id:
            raise TaskConflictError("Task id cannot be changed")
        current = store.get_task_record(task_id)
        if current is None:
            raise TaskNotFoundError(f"Task not found: {task_id}")
        if current.status == TaskStatus.DELETED.value:
            raise TaskConflictError("Deleted Task cannot be updated")
        record = store.update_task_record(
            task_id, document.model_dump(mode="json", by_alias=True)
        )
        return self._to_dict(record)

    def get(self, task_id: str) -> dict[str, Any]:
        record = store.get_task_record(task_id)
        if record is None:
            raise TaskNotFoundError(f"Task not found: {task_id}")
        return self._to_dict(record)

    def get_document(self, task_id: str) -> TaskDocument:
        value = self.get(task_id)
        return TaskDocument.model_validate(value["document"])

    def list(self) -> list[dict[str, Any]]:
        return [self._to_dict(record) for record in store.list_task_records()]

    def set_status(self, task_id: str, status: TaskStatus) -> dict[str, Any]:
        current = store.get_task_record(task_id)
        if current is None:
            raise TaskNotFoundError(f"Task not found: {task_id}")
        if current.status == TaskStatus.DELETED.value and status != TaskStatus.DELETED:
            raise TaskConflictError("Deleted Task cannot be restored")
        record = store.set_task_status(task_id, status.value)
        return self._to_dict(record)

    def executions(self, task_id: str, limit: int = 50) -> list[dict[str, Any]]:
        self.get(task_id)
        return [
            {
                **record.model_dump(),
                "started_at": record.started_at.isoformat(),
                "finished_at": record.finished_at.isoformat() if record.finished_at else None,
            }
            for record in store.list_task_executions(task_id, limit)
        ]

    @staticmethod
    def _to_dict(record) -> dict[str, Any]:
        return {
            "id": record.id,
            "name": record.name,
            "status": record.status,
            "document": json.loads(record.document),
            "skillId": record.skill_id,
            "skillVersion": record.skill_version,
            "cron": record.cron_expr,
            "timezone": record.timezone,
            "createdAt": record.created_at.isoformat(),
            "updatedAt": record.updated_at.isoformat(),
            "lastRunAt": record.last_run_at.isoformat() if record.last_run_at else None,
            "nextRunAt": record.next_run_at.isoformat() if record.next_run_at else None,
            "runCount": record.run_count,
            "lastResult": record.last_result,
        }
