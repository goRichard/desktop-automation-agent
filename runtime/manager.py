"""Manage active Agent runs for the local HTTP API."""
from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any, Optional

from .controller import RunController
from .events import EventBus
from .models import empty_token_usage
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

    async def start_skill_run(
        self,
        document: Any,
        inputs: dict[str, Any],
        mode: Any,
        *,
        session_id: Optional[str] = None,
        unattended_approved: bool = False,
        skill_resolver: Optional[Any] = None,
        timeout_seconds: Optional[int] = None,
    ) -> dict[str, Any]:
        async with self._lifecycle_lock:
            from memory import create_session, get_session
            from config import get_settings
            from skills.executor import ACTION_TO_TOOL, SkillExecutor
            from .evidence import RuntimeEvidenceCollector

            if session_id:
                if await asyncio.to_thread(get_session, session_id) is None:
                    raise LookupError(f"Session not found: {session_id}")
                resolved_session_id = session_id
            else:
                session = await asyncio.to_thread(
                    create_session, f"Skill: {document.metadata.name}"
                )
                resolved_session_id = session.id

            controller = RunController(
                session_id=resolved_session_id,
                user_input=f"Run Skill {document.metadata.id}@{document.metadata.version}",
                event_bus=self.events,
                persistence=self.persistence,
                run_type="skill",
                skill_id=document.metadata.id,
                skill_version=document.metadata.version,
                execution_mode=mode.value,
                inputs=inputs,
            )
            agent_loop_holder: dict[str, Any] = {}
            evidence_collector = RuntimeEvidenceCollector(get_settings().evidence_dir)

            async def run_agent_step(instruction: str, allowed_tools: list[str]) -> str:
                from agent import AgentLoop

                loop = agent_loop_holder.get("loop")
                if loop is None:
                    loop = AgentLoop(session_id=resolved_session_id, event_bus=self.events)
                    agent_loop_holder["loop"] = loop
                tool_names = {ACTION_TO_TOOL.get(name, name) for name in allowed_tools}
                return await loop.execute_instruction(
                    instruction,
                    controller,
                    allowed_tool_names=tool_names or None,
                )

            executor = SkillExecutor(
                agent_runner=run_agent_step,
                confirmation_runner=controller.request_confirmation,
                skill_resolver=skill_resolver,
                evidence_runner=lambda step, error, details: evidence_collector.collect(
                    controller, step, error, details
                ),
            )
            task = asyncio.create_task(
                self._consume_skill_run(
                    executor,
                    controller,
                    document,
                    inputs,
                    mode,
                    unattended_approved,
                    timeout_seconds,
                ),
                name=f"flowpilot-skill-run-{controller.state.id}",
            )
            self._runs[controller.state.id] = ManagedRun(controller, executor, task)
        await asyncio.sleep(0)
        return controller.state.to_dict()

    async def _consume_skill_run(
        self,
        executor: Any,
        controller: RunController,
        document: Any,
        inputs: dict[str, Any],
        mode: Any,
        unattended_approved: bool,
        timeout_seconds: Optional[int],
    ) -> None:
        from llm import capture_token_usage

        from .controller import RunCancelled
        from .models import RunStatus

        try:
            await controller.initialize()
            await controller.transition(RunStatus.PREPARING)
            await controller.transition(RunStatus.RUNNING)
            with capture_token_usage(controller.record_model_usage):
                result = await asyncio.wait_for(
                    executor.execute(
                        document,
                        inputs,
                        controller=controller,
                        mode=mode,
                        unattended_approved=unattended_approved,
                    ),
                    timeout=timeout_seconds or document.execution.timeout_seconds,
                )
            summary = (
                f"Skill {document.metadata.id}@{document.metadata.version} completed "
                f"with {len(result['steps'])} steps"
            )
            controller.state.output += summary
            await controller.emit("run.output", {"delta": summary})
            await controller.emit("skill.completed", {"result": result})
            if controller.persistence:
                await controller.persistence.save_run(controller.state)
            await controller.succeed()
        except RunCancelled:
            return
        except TimeoutError:
            effective_timeout = timeout_seconds or document.execution.timeout_seconds
            await controller.fail(
                f"Skill timed out after {effective_timeout} seconds"
            )
        except Exception as error:
            await controller.fail(f"{type(error).__name__}: {error}")

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

    async def confirm(self, run_id: str, approved: bool) -> dict[str, Any]:
        managed = self._require_active(run_id)
        await managed.controller.confirm(approved)
        return managed.controller.state.to_dict()

    async def wait(self, run_id: str) -> dict[str, Any]:
        managed = self._runs.get(run_id)
        if managed is None:
            value = await self.get_run(run_id)
            if value is None:
                raise LookupError(f"Run not found: {run_id}")
            return value
        await asyncio.shield(managed.task)
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

        from memory import get_runtime_run, get_runtime_run_context, list_runtime_steps

        record = await asyncio.to_thread(get_runtime_run, run_id)
        if record is None:
            return None
        steps = await asyncio.to_thread(list_runtime_steps, run_id)
        value = record.model_dump()
        context = await asyncio.to_thread(get_runtime_run_context, run_id)
        if context:
            value.update(self._context_record_to_dict(context))
        value["steps"] = [self._step_record_to_dict(step) for step in steps]
        return value

    async def list_runs(self, limit: int = 50) -> list[dict[str, Any]]:
        from memory import list_runtime_run_contexts, list_runtime_runs

        records = await asyncio.to_thread(list_runtime_runs, limit)
        contexts = await asyncio.to_thread(
            list_runtime_run_contexts, [record.id for record in records]
        )
        context_by_run = {
            context.run_id: self._context_record_to_dict(context) for context in contexts
        }
        active = {run_id: run for run_id, run in self._runs.items()}
        values = []
        for record in records:
            managed = active.get(record.id)
            value = managed.controller.state.to_dict() if managed else record.model_dump()
            if not managed and record.id in context_by_run:
                value.update(context_by_run[record.id])
            values.append(value)
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

    async def list_evidence(self, run_id: str) -> list[dict[str, Any]]:
        from memory import list_runtime_evidence

        if await self.get_run(run_id) is None:
            raise LookupError(f"Run not found: {run_id}")
        records = await asyncio.to_thread(list_runtime_evidence, run_id)
        return [
            {
                **record.model_dump(exclude={"metadata_json"}),
                "metadata": json.loads(record.metadata_json),
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

    @staticmethod
    def _context_record_to_dict(record: Any) -> dict[str, Any]:
        value = record.model_dump(exclude={"run_id", "inputs", "token_usage"})
        value["inputs"] = json.loads(record.inputs)
        value["token_usage"] = json.loads(record.token_usage) or empty_token_usage()
        return value
