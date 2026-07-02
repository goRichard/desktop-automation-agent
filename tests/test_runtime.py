from __future__ import annotations

import asyncio

from runtime import (
    EventBus,
    RunCancelled,
    RunController,
    RunStatus,
    StepStatus,
    desktop_execution_lock,
)
from llm import TokenUsage


def test_run_state_and_event_sequence() -> None:
    async def check() -> None:
        seen = []
        bus = EventBus()
        bus.subscribe(lambda event: seen.append(event))
        run = RunController("session", "demo", bus, run_id="run-state")

        await run.initialize()
        await run.transition(RunStatus.PREPARING)
        await run.transition(RunStatus.RUNNING)
        step = await run.start_step("launch", ["app_launch"])
        await run.finish_step(step, success=True, result="ok")
        await run.succeed()

        assert run.state.status == RunStatus.SUCCEEDED
        assert [event.sequence for event in seen] == list(range(1, len(seen) + 1))
        assert seen[-1].type == "run.completed"

    asyncio.run(check())


def test_pause_resume_blocks_checkpoint() -> None:
    async def check() -> None:
        run = RunController("session", "demo", run_id="run-pause")
        await run.initialize()
        await run.transition(RunStatus.PREPARING)
        await run.transition(RunStatus.RUNNING)
        await run.pause()

        checkpoint = asyncio.create_task(run.checkpoint())
        await asyncio.sleep(0)
        assert not checkpoint.done()

        await run.resume()
        await checkpoint

    asyncio.run(check())


def test_desktop_lock_serializes_runs() -> None:
    async def check() -> None:
        order = []
        first = RunController("s1", "first", run_id="run-first")
        second = RunController("s2", "second", run_id="run-second")
        await first.initialize()
        await second.initialize()

        async def hold_first() -> None:
            async with desktop_execution_lock.hold(first):
                order.append("first-enter")
                await asyncio.sleep(0.05)
                order.append("first-exit")

        async def hold_second() -> None:
            await asyncio.sleep(0.01)
            async with desktop_execution_lock.hold(second):
                order.append("second-enter")

        await asyncio.gather(hold_first(), hold_second())
        assert order == ["first-enter", "first-exit", "second-enter"]
        assert desktop_execution_lock.owner_run_id is None

    asyncio.run(check())


def test_cancel_marks_active_step_and_breaks_checkpoint() -> None:
    async def check() -> None:
        run = RunController("session", "demo", run_id="run-cancel")
        await run.initialize()
        await run.transition(RunStatus.PREPARING)
        await run.transition(RunStatus.RUNNING)
        step = await run.start_step("type", ["type_text"])

        await run.cancel("stop")

        assert run.state.status == RunStatus.CANCELLED
        assert step.status == StepStatus.SKIPPED
        try:
            await run.checkpoint()
        except RunCancelled as error:
            assert str(error) == "stop"
        else:
            raise AssertionError("cancelled checkpoint did not raise")

    asyncio.run(check())


def test_runtime_persistence_hooks() -> None:
    class FakePersistence:
        def __init__(self):
            self.runs = []
            self.steps = []
            self.events = []

        async def save_run(self, state) -> None:
            self.runs.append(state.to_dict())

        async def save_step(self, step) -> None:
            self.steps.append(step.to_dict())

        async def save_event(self, event) -> None:
            self.events.append(event.to_dict())

    async def check() -> None:
        persistence = FakePersistence()
        run = RunController(
            "session",
            "demo",
            run_id="run-persistence",
            persistence=persistence,
        )
        await run.initialize()
        await run.transition(RunStatus.PREPARING)
        await run.transition(RunStatus.RUNNING)
        step = await run.start_step("launch", ["app_launch"])
        await run.finish_step(step, success=True, result="ok")
        await run.succeed()

        assert persistence.runs[-1]["status"] == "succeeded"
        assert persistence.steps[-1]["status"] == "succeeded"
        assert persistence.events[-1]["type"] == "run.completed"

    asyncio.run(check())


def test_confirmation_requires_explicit_resolution() -> None:
    async def check() -> None:
        run = RunController("session", "demo", run_id="run-confirm")
        await run.initialize()
        await run.transition(RunStatus.PREPARING)
        await run.transition(RunStatus.RUNNING)

        request = asyncio.create_task(
            run.request_confirmation({"stepId": "send", "risk": "external_side_effect"})
        )
        await asyncio.sleep(0)
        assert run.state.status == RunStatus.WAITING_USER
        assert run.state.pending_confirmation["stepId"] == "send"

        try:
            await run.resume()
        except RuntimeError as error:
            assert str(error) == "Run is not paused"
        else:
            raise AssertionError("resume bypassed explicit confirmation")

        await run.confirm(True)
        assert await request is True
        assert run.state.status == RunStatus.RUNNING
        assert run.state.pending_confirmation is None

    asyncio.run(check())


def test_model_usage_is_accumulated_and_emitted() -> None:
    async def check() -> None:
        run = RunController("session", "demo", run_id="run-usage")
        await run.initialize()
        await run.record_model_usage(TokenUsage(
            input_tokens=100,
            output_tokens=20,
            total_tokens=120,
            cached_input_tokens=25,
            reported=True,
            role="chat",
            model="model-a",
        ))
        await run.record_model_usage(TokenUsage(
            reported=False,
            role="vision",
            model="model-b",
        ))

        usage = run.state.token_usage
        assert usage["model_calls"] == 2
        assert usage["reported_calls"] == 1
        assert usage["total_tokens"] == 120
        assert usage["cached_input_tokens"] == 25
        assert usage["by_role"]["chat"]["input_tokens"] == 100
        assert usage["by_role"]["vision"]["model_calls"] == 1
        usage_events = [
            event for event in run.events.history(run.state.id)
            if event.type == "run.usage"
        ]
        assert len(usage_events) == 2
        assert usage_events[0].data["cumulative"]["model_calls"] == 1
        assert usage_events[1].data["cumulative"]["model_calls"] == 2

    asyncio.run(check())


def test_execution_actions_are_remembered_and_emitted() -> None:
    async def check() -> None:
        run = RunController("session", "demo", run_id="run-memory")
        await run.initialize()
        await run.record_execution_action({
            "sequence": 1,
            "planStepId": 2,
            "tool": "type_text",
            "arguments": {"text": "hello"},
            "success": True,
            "result": "typed",
        })

        assert run.state.execution_memory[0]["tool"] == "type_text"
        events = [
            event for event in run.events.history(run.state.id)
            if event.type == "run.execution_memory"
        ]
        assert len(events) == 1
        assert events[0].data["entry"]["arguments"]["text"] == "hello"

    asyncio.run(check())
