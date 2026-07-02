"""Run state machine and cooperative execution controls."""
from __future__ import annotations

import asyncio
from copy import deepcopy
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
        run_type: str = "agent",
        skill_id: Optional[str] = None,
        skill_version: Optional[str] = None,
        execution_mode: Optional[str] = None,
        inputs: Optional[dict[str, Any]] = None,
    ):
        self.state = RunState(
            session_id=session_id,
            user_input=user_input,
            run_type=run_type,
            skill_id=skill_id,
            skill_version=skill_version,
            execution_mode=execution_mode,
            inputs=inputs or {},
        )
        if run_id:
            self.state.id = run_id
        self.events = event_bus or EventBus()
        self.persistence = persistence
        self._sequence = 0
        self._resume_gate = asyncio.Event()
        self._resume_gate.set()
        self._confirmation_gate = asyncio.Event()
        self._confirmation_result: Optional[bool] = None

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
        if self.state.status != RunStatus.PAUSED:
            raise RuntimeError("Run is not paused")
        self._resume_gate.set()
        await self.transition(RunStatus.RUNNING)

    async def cancel(self, reason: str = "Cancelled by user") -> None:
        if self.state.is_terminal:
            return
        self._resume_gate.set()
        self._confirmation_result = False
        self._confirmation_gate.set()
        self.state.pending_confirmation = None
        for step in self.state.steps:
            if step.status == StepStatus.RUNNING:
                step.status = StepStatus.SKIPPED
                step.finished_at = utc_now()
                step.error = reason
                await self._persist_step(step)
                await self.emit("step.skipped", {"step": step.to_dict()})
        await self.transition(RunStatus.CANCELLED, error=reason)

    async def request_confirmation(self, details: dict[str, Any]) -> bool:
        if self.state.status != RunStatus.RUNNING:
            raise RuntimeError("Confirmation can only be requested by a running Run")
        self._confirmation_result = None
        self._confirmation_gate.clear()
        self.state.pending_confirmation = details
        await self.transition(RunStatus.WAITING_USER)
        await self.emit("run.confirmation_requested", {"confirmation": details})
        await self._confirmation_gate.wait()
        if self.state.status == RunStatus.CANCELLED:
            raise RunCancelled(self.state.error or "Run cancelled")
        approved = bool(self._confirmation_result)
        self.state.pending_confirmation = None
        await self.transition(RunStatus.RUNNING)
        await self.emit("run.confirmation_resolved", {"approved": approved})
        return approved

    async def confirm(self, approved: bool) -> None:
        if self.state.status != RunStatus.WAITING_USER:
            raise RuntimeError("Run is not waiting for confirmation")
        if self._confirmation_result is not None:
            raise RuntimeError("Run confirmation has already been resolved")
        self._confirmation_result = approved
        self._confirmation_gate.set()

    async def checkpoint(self) -> None:
        if self.state.status == RunStatus.CANCELLED:
            raise RunCancelled(self.state.error or "Run cancelled")
        await self._resume_gate.wait()
        if self.state.status == RunStatus.CANCELLED:
            raise RunCancelled(self.state.error or "Run cancelled")

    async def record_model_usage(self, usage: Any) -> None:
        """Accumulate one model response and publish its Run-level usage."""
        value = usage.to_dict() if hasattr(usage, "to_dict") else dict(usage)
        cumulative = self.state.token_usage
        cumulative["model_calls"] += 1
        if value.get("reported"):
            cumulative["reported_calls"] += 1
        for key in (
            "input_tokens",
            "output_tokens",
            "total_tokens",
            "cached_input_tokens",
        ):
            cumulative[key] += max(0, int(value.get(key, 0) or 0))

        role = str(value.get("role") or "chat")
        role_usage = cumulative["by_role"].setdefault(
            role,
            {
                "model_calls": 0,
                "reported_calls": 0,
                "input_tokens": 0,
                "output_tokens": 0,
                "total_tokens": 0,
                "cached_input_tokens": 0,
            },
        )
        role_usage["model_calls"] += 1
        if value.get("reported"):
            role_usage["reported_calls"] += 1
        for key in (
            "input_tokens",
            "output_tokens",
            "total_tokens",
            "cached_input_tokens",
        ):
            role_usage[key] += max(0, int(value.get(key, 0) or 0))

        await self._persist_run()
        await self.emit(
            "run.usage",
            {
                "increment": value,
                "cumulative": deepcopy(cumulative),
            },
        )

    async def record_execution_action(self, action: dict[str, Any]) -> None:
        """Append a compact successful/failed action fact to the Run memory."""
        entry = deepcopy(action)
        self.state.execution_memory.append(entry)
        # Keep the persisted/API payload bounded during long unattended Runs.
        if len(self.state.execution_memory) > 100:
            self.state.execution_memory = self.state.execution_memory[-100:]
        await self._persist_run()
        await self.emit(
            "run.execution_memory",
            {
                "entry": entry,
                "size": len(self.state.execution_memory),
            },
        )

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
