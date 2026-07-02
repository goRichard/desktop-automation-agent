from __future__ import annotations

import asyncio
import sqlite3

from memory import (
    get_runtime_run,
    get_runtime_run_context,
    init_db,
    list_runtime_events,
    list_runtime_steps,
)
from runtime import RunController, RunStatus, get_runtime_persistence
from llm import TokenUsage


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
        await run.record_model_usage(TokenUsage(
            input_tokens=80,
            output_tokens=20,
            total_tokens=100,
            reported=True,
            role="chat",
            model="test-model",
        ))
        await run.record_execution_action({
            "sequence": 1,
            "tool": "type_text",
            "arguments": {"text": "hello"},
            "success": True,
            "result": "typed",
        })
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
    assert '"total_tokens": 100' in context.token_usage
    assert '"tool": "type_text"' in context.execution_memory
    assert len(list_runtime_steps("persisted-run")) == 1
    assert len(list_runtime_events("persisted-run")) >= 5


def test_existing_runtime_context_table_gets_token_usage_migration(tmp_path) -> None:
    database = tmp_path / "legacy-runtime.db"
    with sqlite3.connect(database) as connection:
        connection.execute(
            """
            CREATE TABLE runtime_run_contexts (
                run_id VARCHAR NOT NULL PRIMARY KEY,
                run_type VARCHAR NOT NULL DEFAULT 'agent',
                skill_id VARCHAR,
                skill_version VARCHAR,
                execution_mode VARCHAR,
                inputs VARCHAR NOT NULL DEFAULT '{}'
            )
            """
        )

    init_db(database)

    with sqlite3.connect(database) as connection:
        columns = {
            row[1] for row in connection.execute(
                "PRAGMA table_info(runtime_run_contexts)"
            )
        }
    assert {"token_usage", "execution_memory"} <= columns
