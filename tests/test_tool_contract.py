from __future__ import annotations

import pytest

from agent import tool_dispatcher
from llm import ToolCall
from tools import winpeekaboo
from tools.registry import get_all_schemas, get_tool, get_tool_metadata, tool


def test_tool_registry_returns_the_decorated_callable() -> None:
    assert get_tool("window_activate") is winpeekaboo.window_activate


@pytest.mark.asyncio
async def test_dispatcher_preserves_structured_tool_result_metadata() -> None:
    @tool(description="Structured result test helper", name="contract_test_tool")
    def helper() -> dict:
        return {
            "ok": True,
            "data": {"value": 42},
            "error": None,
            "artifacts": [{"path": "artifact.txt"}],
            "durationMs": 12.5,
        }

    assert get_tool("contract_test_tool") is helper

    result = await tool_dispatcher.execute([
        ToolCall("call-1", "contract_test_tool", {})
    ])

    assert result[0]["success"] is True
    assert result[0]["data"] == {"value": 42}
    assert result[0]["artifacts"] == [{"path": "artifact.txt"}]
    assert result[0]["durationMs"] == 12.5


def test_tool_metadata_defaults_do_not_pollute_openai_schema() -> None:
    @tool(description="Default metadata test helper", name="default_metadata_tool")
    def helper() -> str:
        return "ok"

    metadata = get_tool_metadata("default_metadata_tool")
    assert metadata == {
        "name": "default_metadata_tool",
        "description": "Default metadata test helper",
        "risk": "low",
        "sideEffect": False,
        "requiresConfirmation": False,
        "allowedModes": ["agent", "step", "guided", "unattended"],
    }

    schema = next(
        item for item in get_all_schemas()
        if item["function"]["name"] == "default_metadata_tool"
    )
    assert "risk" not in schema["function"]
    assert "sideEffect" not in schema["function"]
    assert get_tool("default_metadata_tool") is helper


def test_first_batch_tool_risk_metadata_is_registered() -> None:
    # scheduler_tool is intentionally not imported by tools.__init__.
    import tools.scheduler_tool  # noqa: F401

    assert get_tool_metadata("run_command")["risk"] == "high"
    assert get_tool_metadata("run_command")["requiresConfirmation"] is True
    assert get_tool_metadata("write_file")["risk"] == "medium"
    assert get_tool_metadata("run_actions")["sideEffect"] is True
    assert get_tool_metadata("list_windows")["risk"] == "read"
    assert get_tool_metadata("inspect_elements")["risk"] == "read"

    outlook_send = get_tool_metadata("outlook_send_message")
    assert outlook_send["risk"] == "external_side_effect"
    assert outlook_send["sideEffect"] is True
    assert outlook_send["requiresConfirmation"] is True

    teams_send = get_tool_metadata("teams_send_message")
    assert teams_send["risk"] == "external_side_effect"
    assert teams_send["sideEffect"] is True
    assert get_tool_metadata("create_job")["risk"] == "high"
