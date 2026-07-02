from __future__ import annotations

import pytest

from agent.loop import (
    AgentLoop,
    _append_verification,
    _verification_target_window,
    _verification_wait_seconds,
)
from llm import ToolCall


def test_verification_prefers_new_window_reported_by_tool() -> None:
    calls = [
        ToolCall(
            "click",
            "find_and_click",
            {"target": "New Email", "window": "Inbox - Outlook"},
        )
    ]
    results = [{
        "content": (
            "✅ 成功点击 'New Email'\n"
            '🔄 检测到新窗口已弹出，已自动激活: "Untitled - Message"'
        )
    }]

    assert _verification_target_window(calls, results) == "Untitled - Message"


def test_transition_without_new_title_keeps_current_foreground_window() -> None:
    calls = [
        ToolCall(
            "click",
            "find_and_click",
            {"target": "Next", "window": "Original Window"},
        )
    ]

    assert _verification_target_window(calls, [{"content": "clicked"}]) is None


def test_non_transition_action_can_reactivate_declared_window() -> None:
    calls = [
        ToolCall(
            "type",
            "type_text",
            {"text": "hello", "window": "Untitled - Message"},
        )
    ]

    assert _verification_target_window(calls, [{"content": "typed"}]) == (
        "Untitled - Message"
    )


def test_verification_warning_is_advisory_but_explicit_failure_stops() -> None:
    warning = [{"content": "clicked", "success": True, "error": None}]
    _append_verification(warning, "[屏幕观察] ⚠️ 无法确定：窗口仍在加载")
    assert warning[0]["success"] is True
    assert warning[0]["error"] is None

    failure = [{"content": "clicked", "success": True, "error": None}]
    _append_verification(failure, "[屏幕观察] ❌ 不符合预期：目标窗口未出现")
    assert failure[0]["success"] is False
    assert "目标窗口未出现" in failure[0]["error"]


def test_window_transition_uses_longer_stabilization_delay() -> None:
    assert _verification_wait_seconds([
        ToolCall("launch", "app_launch", {"name": "outlook.exe"})
    ]) == 2.0
    assert _verification_wait_seconds([
        ToolCall("type", "type_text", {"text": "hello"})
    ]) == 0.5


@pytest.mark.asyncio
async def test_verify_step_uses_current_step_and_new_window(monkeypatch) -> None:
    loop = AgentLoop.__new__(AgentLoop)
    loop._plan = AgentLoop._parse_plan(
        "1. 点击 New Email 并打开写信窗口（find_and_click）\n"
        "2. 填写收件人（type_text）"
    )
    assert loop._plan is not None
    captured = {}

    async def fake_verify(expected, window=None, wait_seconds=1.0):
        captured.update({
            "expected": expected,
            "window": window,
            "wait_seconds": wait_seconds,
        })
        return "✅ 符合预期：写信窗口已打开"

    monkeypatch.setattr("tools.vision.verify_action_result", fake_verify)
    result = await loop._verify_step(
        [ToolCall(
            "click",
            "find_and_click",
            {"target": "New Email", "window": "Inbox - Outlook"},
        )],
        [{
            "content": (
                "clicked\n"
                '🔄 检测到新窗口已弹出，已自动激活: "Untitled - Message"'
            )
        }],
    )

    assert "点击 New Email" in captured["expected"]
    assert "填写收件人" not in captured["expected"]
    assert captured["window"] == "Untitled - Message"
    assert captured["wait_seconds"] == 1.2
    assert "[验证截图目标] Untitled - Message" in result
