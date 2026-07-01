"""Run state machine and cooperative execution controls."""
from __future__ import annotations

import asyncio
from typing import Any, Optional

from .events import EventBus
from .models import (
    RunEvent,
    RunState,
    RunStatus,
    StepRunState,
    StepStatus,
    TERMINAL_RUN_STATUSES,
    utc_now,
)
from .persistence import RuntimePersistence


class RunCancelled(Exception):
    """Raised at a cooperative checkpoint after a Run is cancelled."""


_ALLOWED_TRANSITIONS: dict[RunStatus, set[RunStatus]] = {
    RunStatus.QUEUED: {RunStatus.PREPARING, RunStatus.CANCELLED, RunStatus.FAILED},
    RunStatus.PREPARING: {RunStatus.RUNNING, RunStatus.CANCELLED, RunStatus.FAILED},
    RunStatus.RUNNING: {
        RunStatus.PAUSED,
        RunStatus.WAITING_USER,
        RunStatus.SUCCEEDED,
        RunStatus.FAILED,
        RunStatus.CANCELLED,
    },
    RunStatus.PAUSED: {RunStatus.RUNNING, RunStatus.CANCELLED, RunStatus.FAILED},
    RunStatus.WAITING_USER: {RunStatus.RUNNING, RunStatus.CANCELLED, RunStatus.FAILED},
    RunStatus.SUCCEEDED: set(),
    RunStatus.FAILED: set(),
    RunStatus.CANCELLED: set(),
}


class RunController:
    def __init__(
        self,
        session_id: str,
        user_input: str,
        event_bus: Optional[EventBus] = None,
        run_id: Optional[str] = None,
        persistence: Optional[RuntimePersistence] = None,
    ):
        self.state = RunState(session_id=session_id, user_input=user_input)
        if run_id:
            self.state.id = run_id
        self.events = event_bus or EventBus()
        self.persistence = persistence
        self._sequence = 0
        self._resume_gate = asyncio.Event()
        self._resume_gate.set()

    async def initialize(self) -> None:
        await self._persist_run()
        await self.emit("run.queued", {"run": self.state.to_dict()})

    async def transition(
        self,
        status: RunStatus,
        *,
        error: Optional[str] = None,
    ) -> None:
        current = self.state.status
        if current == status:
            return
        if current == RunStatus.CANCELLED:
            raise RunCancelled(self.state.error or "Run cancelled")
        if status not in _ALLOWED_TRANSITIONS[current]:
            raise RuntimeError(f"Invalid Run transition: {current.value} -> {status.value}")

        self.state.status = status
        first_start = status == RunStatus.RUNNING and self.state.started_at is None
        if first_start:
            self.state.started_at = utc_now()
        if status in TERMINAL_RUN_STATUSES:
            self.state.finished_at = utc_now()
            self.state.error = error

        await self._persist_run()

        event_type = f"run.{status.value}"
        if status == RunStatus.RUNNING:
            event_type = "run.started" if first_start else "run.resumed"
        elif status == RunStatus.SUCCEEDED:
            event_type = "run.completed"

        await self.emit(
            event_type,
            {"status": status.value, "error": error},
        )

    async def pause(self) -> None:
        if self.state.status != RunStatus.RUNNING:
            raise RuntimeError("Only a running Run can be paused")
        self._resume_gate.clear()
        await self.transition(RunStatus.PAUSED)

    async def resume(self) -> None:
        if self.state.status not in (RunStatus.PAUSED, RunStatus.WAITING_USER):
            raise RuntimeError("Run is not paused or waiting for user input")
        self._resume_gate.set()
        await self.transition(RunStatus.RUNNING)

    async def cancel(self, reason: str = "Cancelled by user") -> None:
        if self.state.is_terminal:
            return
        self._resume_gate.set()
        for step in self.state.steps:
            if step.status == StepStatus.RUNNING:
                step.status = StepStatus.SKIPPED
                step.finished_at = utc_now()
                step.error = reason
                await self._persist_step(step)
                await self.emit("step.skipped", {"step": step.to_dict()})
        await self.transition(RunStatus.CANCELLED, error=reason)

    async def checkpoint(self) -> None:
        if self.state.status == RunStatus.CANCELLED:
            raise RunCancelled(self.state.error or "Run cancelled")
        await self._resume_gate.wait()
        if self.state.status == RunStatus.CANCELLED:
            raise RunCancelled(self.state.error or "Run cancelled")

    async def succeed(self) -> None:
        if not self.state.is_terminal:
            await self.transition(RunStatus.SUCCEEDED)

    async def fail(self, error: str) -> None:
        if not self.state.is_terminal:
            for step in self.state.steps:
                if step.status == StepStatus.RUNNING:
                    await self.finish_step(step, success=False, error=error)
            await self.transition(RunStatus.FAILED, error=error)

    async def start_step(self, name: str, tool_names: list[str]) -> StepRunState:
        await self.checkpoint()
        step = StepRunState(run_id=self.state.id, name=name, tool_names=tool_names)
        step.status = StepStatus.RUNNING
        step.started_at = utc_now()
        self.state.steps.append(step)
        await self._persist_step(step)
        await self.emit("step.started", {"step": step.to_dict()})
        return step

    async def finish_step(
        self,
        step: StepRunState,
        *,
        success: bool,
        result: Optional[str] = None,
        error: Optional[str] = None,
    ) -> None:
        if step.status != StepStatus.RUNNING:
            return
        step.status = StepStatus.SUCCEEDED if success else StepStatus.FAILED
        step.finished_at = utc_now()
        step.result = result
        step.error = error
        await self._persist_step(step)
        event_type = "step.completed" if success else "step.failed"
        await self.emit(event_type, {"step": step.to_dict()})

    async def emit(self, event_type: str, data: dict[str, Any]) -> RunEvent:
        self._sequence += 1
        event = RunEvent(
            run_id=self.state.id,
            sequence=self._sequence,
            type=event_type,
            data=data,
        )
        # Streaming text can produce thousands of tiny events. It is delivered
        # live over EventBus while the accumulated output is persisted on Run.
        if self.persistence and event_type != "run.output":
            await self.persistence.save_event(event)
        await self.events.publish(event)
        return event

    async def _persist_run(self) -> None:
        if self.persistence:
            await self.persistence.save_run(self.state)

    async def _persist_step(self, step: StepRunState) -> None:
        if self.persistence:
            await self.persistence.save_step(step)
