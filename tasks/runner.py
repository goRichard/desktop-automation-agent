"""Execute scheduled Tasks through the versioned Skill Runtime."""
from __future__ import annotations

import asyncio
import logging
from typing import Any

from memory import store
from runtime.manager import RuntimeManager
from skills.executor import SkillExecutor
from skills.repository import SkillRepository
from skills.schema import ExecutionMode, SkillDocument, SkillStatus

from .repository import TaskRepository
from .schema import TaskStatus

logger = logging.getLogger(__name__)


class TaskRunner:
    def __init__(
        self,
        manager: RuntimeManager,
        tasks: TaskRepository,
        skills: SkillRepository,
    ):
        self.manager = manager
        self.tasks = tasks
        self.skills = skills
        self._watchers: set[asyncio.Task] = set()

    async def start(self, task_id: str, *, wait: bool = False) -> dict[str, Any]:
        task_value = self.tasks.get(task_id)
        if task_value["status"] == TaskStatus.DELETED.value:
            raise RuntimeError("Deleted Task cannot run")
        document = self.tasks.get_document(task_id)
        return await self._start_attempt(document, attempt=1, wait=wait)

    async def run_scheduled(self, task_id: str) -> dict[str, Any]:
        task_value = self.tasks.get(task_id)
        if task_value["status"] != TaskStatus.ACTIVE.value:
            raise RuntimeError("Scheduled Task is not active")
        document = self.tasks.get_document(task_id)
        last_state: dict[str, Any] = {}
        for attempt in range(1, document.execution.retries + 2):
            last_state = await self._start_attempt(
                document, attempt=attempt, wait=True, update_task_info=False
            )
            if last_state["status"] == "succeeded":
                await self._update_task_info(task_id, last_state)
                return last_state
        await self._update_task_info(task_id, last_state)
        return last_state

    async def _start_attempt(
        self,
        task: Any,
        *,
        attempt: int,
        wait: bool,
        update_task_info: bool = True,
    ) -> dict[str, Any]:
        execution = await asyncio.to_thread(
            store.start_task_execution, task.metadata.id, attempt
        )

        async def resolve_nested(
            skill_id: str,
            version: str,
            mode: ExecutionMode,
        ) -> SkillDocument:
            value = self.skills.get_version(skill_id, version)
            if value["status"] != SkillStatus.PUBLISHED.value:
                raise RuntimeError(f"Nested Task Skill is not published: {skill_id}@{version}")
            return SkillDocument.model_validate(value["document"])

        try:
            skill_value = self.skills.get_version(task.skill.id, task.skill.version)
            if skill_value["status"] != SkillStatus.PUBLISHED.value:
                raise RuntimeError(
                    f"Task requires published Skill: {task.skill.id}@{task.skill.version}"
                )
            skill = SkillDocument.model_validate(skill_value["document"])
            SkillExecutor.validate_inputs(skill, task.parameters)
            state = await self.manager.start_skill_run(
                skill,
                task.parameters,
                ExecutionMode.UNATTENDED,
                unattended_approved=task.permissions.external_side_effects_approved,
                skill_resolver=resolve_nested,
                timeout_seconds=task.execution.timeout_seconds,
            )
            await asyncio.to_thread(
                store.attach_task_execution_run, execution.id, state["id"]
            )
        except Exception as error:
            await asyncio.to_thread(
                store.finish_task_execution,
                execution.id,
                "failed",
                error=f"{type(error).__name__}: {error}",
            )
            raise

        if wait:
            return await self._finish_attempt(
                task.metadata.id,
                execution.id,
                state["id"],
                update_task_info,
            )
        watcher = asyncio.create_task(
            self._finish_attempt(task.metadata.id, execution.id, state["id"], True),
            name=f"flowpilot-task-watch-{execution.id}",
        )
        self._watchers.add(watcher)
        watcher.add_done_callback(self._watcher_done)
        return state

    def _watcher_done(self, watcher: asyncio.Task) -> None:
        self._watchers.discard(watcher)
        if not watcher.cancelled() and watcher.exception():
            logger.error("Task execution watcher failed: %s", watcher.exception())

    async def shutdown(self) -> None:
        if self._watchers:
            await asyncio.gather(*list(self._watchers), return_exceptions=True)

    async def _finish_attempt(
        self,
        task_id: str,
        execution_id: str,
        run_id: str,
        update_task_info: bool,
    ) -> dict[str, Any]:
        state = await self.manager.wait(run_id)
        succeeded = state["status"] == "succeeded"
        error = state.get("error")
        result = state.get("output") or error or state["status"]
        await asyncio.to_thread(
            store.finish_task_execution,
            execution_id,
            "success" if succeeded else "failed",
            result=result if succeeded else None,
            error=None if succeeded else error,
        )
        if update_task_info:
            await self._update_task_info(task_id, state)
        return state

    async def _update_task_info(self, task_id: str, state: dict[str, Any]) -> None:
        result = state.get("output") or state.get("error") or state["status"]
        await asyncio.to_thread(
            store.update_task_run_info,
            task_id,
            result=result,
            next_run_at=self._next_run_time(task_id),
        )

    @staticmethod
    def _next_run_time(task_id: str):
        try:
            from scheduler.engine import get_scheduler

            scheduled = get_scheduler().get_job(f"task:{task_id}")
            return scheduled.next_run_time if scheduled else None
        except Exception:
            return None
