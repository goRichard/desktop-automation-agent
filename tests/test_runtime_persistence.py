from __future__ import annotations

import asyncio

from memory import get_runtime_run, init_db, list_runtime_events, list_runtime_steps
from runtime import RunController, RunStatus, get_runtime_persistence


def test_runtime_state_is_persisted(tmp_path) -> None:
    init_db(tmp_path / "runtime.db")

    async def check() -> None:
        run = RunController(
            "session",
            "persist me",
            run_id="persisted-run",
            persistence=get_runtime_persistence(),
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
    assert len(list_runtime_steps("persisted-run")) == 1
    assert len(list_runtime_events("persisted-run")) >= 5
