from __future__ import annotations

from types import SimpleNamespace

import pytest

from agent.loop import AgentLoop
from agent.planner import TaskStatus
from llm import LLMResponse, ToolCall
from runtime import RunController, RunStatus


def _loop_with_plan(plan_text: str) -> AgentLoop:
    loop = AgentLoop.__new__(AgentLoop)
    loop._plan = AgentLoop._parse_plan(plan_text)
    assert loop._plan is not None
    return loop


def _result(name: str, success: bool = True) -> dict:
    return {
        "name": name,
        "content": "ok" if success else "failed",
        "success": success,
    }


def test_plan_exposes_required_and_observation_tools_only() -> None:
    loop = _loop_with_plan("1. 启动 Outlook（app_launch）")
    schemas = [
        {"function": {"name": "app_launch"}},
        {"function": {"name": "list_windows"}},
        {"function": {"name": "hotkey"}},
    ]

    exposed = {
        schema["function"]["name"]
        for schema in loop._tools_for_current_plan_step(schemas)
    }

    assert exposed == {"app_launch", "list_windows"}


def test_observation_does_not_complete_required_plan_action() -> None:
    loop = _loop_with_plan("1. 启动 Outlook（app_launch）")
    observation = ToolCall("observe", "list_windows", {})

    assert loop._begin_plan_step([observation]) is None
    assert loop._advance_plan([observation], [_result("list_windows")]) is None
    assert loop._plan.current_step is not None
    assert loop._plan.current_step.status == TaskStatus.RUNNING

    launch = ToolCall("launch", "app_launch", {"name": "outlook.exe"})
    assert loop._begin_plan_step([launch]) is None
    assert loop._advance_plan([launch], [_result("app_launch")]) is None
    assert loop._plan.is_complete is True


def test_plan_rejects_side_effect_tool_not_declared_by_step() -> None:
    loop = _loop_with_plan("1. 启动 Outlook（app_launch）")

    error = loop._begin_plan_step([
        ToolCall("unexpected", "hotkey", {"keys": "Ctrl+N"})
    ])

    assert error is not None
    assert "要求工具 ['app_launch']" in error
    assert "hotkey" in error


def test_plan_without_recognized_tool_is_rejected() -> None:
    loop = _loop_with_plan("1. 打开目标应用（不存在的工具）")

    error = loop._begin_plan_step([
        ToolCall("unexpected", "list_windows", {})
    ])

    assert error == "步骤 1 未声明有效工具，无法安全执行"


@pytest.mark.asyncio
async def test_policy_violation_gets_one_correction_then_plan_continues(monkeypatch) -> None:
    class FakeLLM:
        def __init__(self):
            self.calls: list[set[str]] = []

        async def chat(self, messages, tools=None):
            self.calls.append({
                schema["function"]["name"] for schema in (tools or [])
            })
            if len(self.calls) == 1:
                # Simulate a provider returning a hidden/hallucinated tool.
                return LLMResponse(
                    None,
                    [ToolCall("wrong", "hotkey", {"keys": "Ctrl+N"})],
                    "tool_calls",
                )
            if len(self.calls) == 2:
                return LLMResponse(
                    None,
                    [ToolCall("right", "app_launch", {"name": "outlook.exe"})],
                    "tool_calls",
                )
            return LLMResponse("done", [], "stop")

        async def chat_stream(self, messages, tools=None):
            raise AssertionError("final response must not trigger a duplicate model request")
            yield  # pragma: no cover

    async def fake_execute(tool_calls):
        return [{
            "role": "tool",
            "tool_call_id": tool_calls[0].id,
            "name": tool_calls[0].name,
            "content": "ok",
            "success": True,
            "error": None,
            "data": None,
        }]

    monkeypatch.setattr(
        "agent.loop.ctx.assemble_with_confirmed_plan",
        lambda *_: [{"role": "user", "content": "test"}],
    )
    monkeypatch.setattr("agent.loop.ctx.set_current_plan", lambda _: None)
    monkeypatch.setattr("agent.loop.save_message", lambda *_, **__: None)
    monkeypatch.setattr("agent.loop.update_session_title", lambda *_, **__: None)
    monkeypatch.setattr("agent.loop.tool_dispatcher.execute", fake_execute)
    monkeypatch.setattr("agent.loop.AgentLoop._verification_reason", lambda *_: None)

    loop = AgentLoop.__new__(AgentLoop)
    loop.settings = SimpleNamespace(max_iterations=5)
    loop.llm = FakeLLM()
    loop.session_id = "session"
    loop._turn_count = 1
    loop._plan = None

    controller = RunController("session", "test")
    await controller.initialize()
    await controller.transition(RunStatus.PREPARING)
    await controller.transition(RunStatus.RUNNING)

    output = ""
    async for token in loop._execute_stream(
        user_input="test",
        confirmed_plan="1. 启动 Outlook（app_launch）",
        controller=controller,
    ):
        output += token

    assert output == "done"
    assert controller.state.status == RunStatus.SUCCEEDED
    assert [step.status.value for step in controller.state.steps] == [
        "failed",
        "succeeded",
    ]
    assert "hotkey" not in loop.llm.calls[0]
    assert "app_launch" in loop.llm.calls[0]
    assert loop._plan.is_complete is True
