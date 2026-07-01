"""Manage active Agent runs for the local HTTP API."""
from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any, Optional

from .controller import RunController
from .events import EventBus
from .persistence import RuntimePersistence, get_runtime_persistence


class RuntimeConfigurationBusy(RuntimeError):
    pass


@dataclass
class ManagedRun:
    controller: RunController
    loop: Any
    task: asyncio.Task[None]


class RuntimeManager:
    def __init__(
        self,
        event_bus: Optional[EventBus] = None,
        persistence: Optional[RuntimePersistence] = None,
    ):
        self.events = event_bus or EventBus(history_limit=5000)
        self.persistence = persistence or get_runtime_persistence()
        self._runs: dict[str, ManagedRun] = {}
        self._lifecycle_lock = asyncio.Lock()

    async def start_run(
        self,
        user_input: str,
        session_id: Optional[str] = None,
        confirmed_plan: Optional[str] = None,
    ) -> dict[str, Any]:
        async with self._lifecycle_lock:
            from agent import AgentLoop
            from memory import get_session

            if session_id and await asyncio.to_thread(get_session, session_id) is None:
                raise LookupError(f"Session not found: {session_id}")

            loop = AgentLoop(session_id=session_id, event_bus=self.events)
            controller = RunController(
                session_id=loop.session_id,
                user_input=user_input,
                event_bus=self.events,
                persistence=self.persistence,
            )
            task = asyncio.create_task(
                self._consume_run(loop, controller, user_input, confirmed_plan),
                name=f"flowpilot-run-{controller.state.id}",
            )
            self._runs[controller.state.id] = ManagedRun(controller, loop, task)
        await asyncio.sleep(0)
        return controller.state.to_dict()

    async def _consume_run(
        self,
        loop: Any,
        controller: RunController,
        user_input: str,
        confirmed_plan: Optional[str],
    ) -> None:
        try:
            async for _ in loop.run_stream(
                user_input=user_input,
                confirmed_plan=confirmed_plan,
                run_controller=controller,
            ):
                pass
        except Exception:
            # AgentLoop already records the failure on the controller.
            return

    async def pause(self, run_id: str) -> dict[str, Any]:
        managed = self._require_active(run_id)
        await managed.controller.pause()
        return managed.controller.state.to_dict()

    async def resume(self, run_id: str) -> dict[str, Any]:
        managed = self._require_active(run_id)
        await managed.controller.resume()
        return managed.controller.state.to_dict()

    async def cancel(self, run_id: str, reason: str) -> dict[str, Any]:
        managed = self._require_active(run_id)
        await managed.controller.cancel(reason)
        return managed.controller.state.to_dict()

    def get_active(self, run_id: str) -> Optional[ManagedRun]:
        return self._runs.get(run_id)

    @property
    def has_active_runs(self) -> bool:
        return any(not run.task.done() for run in self._runs.values())

    @asynccontextmanager
    async def configuration_change(self):
        """Block new Runs while model configuration and providers are swapped."""
        async with self._lifecycle_lock:
            if self.has_active_runs:
                raise RuntimeConfigurationBusy(
                    "Model settings cannot change while a Run is active"
                )
            yield

    async def get_run(self, run_id: str) -> Optional[dict[str, Any]]:
        managed = self._runs.get(run_id)
        if managed:
            return managed.controller.state.to_dict()

        from memory import get_runtime_run, list_runtime_steps

        record = await asyncio.to_thread(get_runtime_run, run_id)
        if record is None:
            return None
        steps = await asyncio.to_thread(list_runtime_steps, run_id)
        value = record.model_dump()
        value["steps"] = [self._step_record_to_dict(step) for step in steps]
        return value

    async def list_runs(self, limit: int = 50) -> list[dict[str, Any]]:
        from memory import list_runtime_runs

        records = await asyncio.to_thread(list_runtime_runs, limit)
        active = {run_id: run for run_id, run in self._runs.items()}
        values = []
        for record in records:
            managed = active.get(record.id)
            values.append(
                managed.controller.state.to_dict() if managed else record.model_dump()
            )
        return values

    async def list_events(
        self,
        run_id: str,
        after_sequence: int = 0,
    ) -> list[dict[str, Any]]:
        from memory import list_runtime_events

        records = await asyncio.to_thread(list_runtime_events, run_id, after_sequence)
        return [
            {
                **record.model_dump(exclude={"data"}),
                "data": json.loads(record.data),
            }
            for record in records
        ]

    async def shutdown(self) -> None:
        active = [run for run in self._runs.values() if not run.task.done()]
        for run in active:
            await run.controller.cancel("Runtime shutting down")
        if active:
            tasks = [run.task for run in active]
            try:
                await asyncio.wait_for(
                    asyncio.gather(*tasks, return_exceptions=True),
                    timeout=10,
                )
            except TimeoutError:
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)

    def _require_active(self, run_id: str) -> ManagedRun:
        managed = self._runs.get(run_id)
        if managed is None or managed.task.done():
            raise LookupError(f"Run is not active: {run_id}")
        return managed

    @staticmethod
    def _step_record_to_dict(record: Any) -> dict[str, Any]:
        value = record.model_dump(exclude={"tool_names"})
        value["tool_names"] = json.loads(record.tool_names)
        return value
