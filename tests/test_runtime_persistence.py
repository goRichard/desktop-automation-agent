from __future__ import annotations

import asyncio

from memory import (
    get_runtime_run,
    get_runtime_run_context,
    init_db,
    list_runtime_events,
    list_runtime_steps,
)
from runtime import RunController, RunStatus, get_runtime_persistence


def test_runtime_state_is_persisted(tmp_path) -> None:
    init_db(tmp_path / "runtime.db")

    async def check() -> None:
        run = RunController(
            "session",
            "persist me",
            run_id="persisted-run",
            persistence=get_runtime_persistence(),
            run_type="skill",
            skill_id="demo-skill",
            skill_version="1.0.0",
            execution_mode="guided",
            inputs={"name": "demo"},
        )
        await run.initialize()
        await run.transition(RunStatus.PREPARING)
        await run.transition(RunStatus.RUNNING)
        step = await run.start_step("demo", ["sleep"])
        await run.finish_step(step, success=True, result="ok")
        run.state.output = "done"
        await run.succeed()

    asyncio.run(check())

    record = get_runtime_run("persisted-run")
    assert record is not None
    assert record.status == "succeeded"
    assert record.output == "done"
    context = get_runtime_run_context("persisted-run")
    assert context is not None
    assert context.skill_id == "demo-skill"
    assert context.skill_version == "1.0.0"
    assert context.inputs == '{"name": "demo"}'
    assert len(list_runtime_steps("persisted-run")) == 1
    assert len(list_runtime_events("persisted-run")) >= 5
