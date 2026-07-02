from __future__ import annotations

from types import SimpleNamespace

import pytest

from agent.loop import (
    AgentLoop,
    _append_verification,
    _execution_memory_summary,
    _messages_with_execution_memory,
    _sanitize_action_value,
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
        "window_transition",
        [{
            "sequence": 1,
            "planStepId": 1,
            "tool": "app_launch",
            "arguments": {"name": "outlook.exe"},
            "success": True,
        }],
    )

    assert "点击 New Email" in captured["expected"]
    assert "填写收件人" not in captured["expected"]
    assert captured["window"] == "Untitled - Message"
    assert captured["wait_seconds"] == 1.2
    assert "app_launch" in captured["expected"]
    assert "[验证截图目标] Untitled - Message" in result


def test_checkpoint_policy_skips_each_text_action_and_verifies_periodically() -> None:
    loop = AgentLoop.__new__(AgentLoop)
    loop.settings = SimpleNamespace(verification={
        "mode": "checkpoint",
        "checkpointInterval": 3,
        "verifyWindowTransitions": True,
        "verifyFinalStep": False,
        "verifyHighRiskActions": True,
    })
    loop._plan = AgentLoop._parse_plan(
        "1. 填写收件人（type_text）\n"
        "2. 填写主题（type_text）\n"
        "3. 填写正文（type_text）\n"
        "4. 检查草稿（list_windows）"
    )
    assert loop._plan is not None

    call = [ToolCall("type", "type_text", {"text": "value"})]
    result = [{"content": "typed", "success": True}]
    assert loop._verification_reason(call, result) is None

    loop._plan.current_step_index = 2
    assert loop._verification_reason(call, result) == "periodic_checkpoint"

    loop.settings.verification = {"mode": "all"}
    assert loop._verification_reason(call, result) == "all_actions"
    loop.settings.verification = {"mode": "off"}
    assert loop._verification_reason(call, result) is None


def test_checkpoint_policy_verifies_new_windows_and_high_risk_actions() -> None:
    loop = AgentLoop.__new__(AgentLoop)
    loop.settings = SimpleNamespace(verification={"mode": "checkpoint"})
    loop._plan = AgentLoop._parse_plan(
        "1. 新建邮件（find_and_click）\n2. 发送邮件（find_and_click）"
    )
    assert loop._plan is not None

    new_window = [ToolCall("new", "find_and_click", {"target": "New Email"})]
    result = [{"content": "检测到新窗口已弹出", "success": True}]
    assert loop._verification_reason(new_window, result) == "window_transition"

    loop._plan.current_step_index = 1
    send = [ToolCall("send", "find_and_click", {"target": "Send"})]
    assert loop._verification_reason(send, [{"content": "clicked", "success": True}]) == (
        "high_risk"
    )


def test_execution_memory_is_compact_and_redacts_secrets() -> None:
    sanitized = _sanitize_action_value({
        "text": "hello",
        "api_key": "private",
        "nested": {"password": "private"},
    })
    assert sanitized["text"] == "hello"
    assert sanitized["api_key"] == "<redacted>"
    assert sanitized["nested"]["password"] == "<redacted>"

    memory = [{
        "sequence": 1,
        "planStepId": 2,
        "tool": "type_text",
        "arguments": sanitized,
        "success": True,
    }]
    summary = _execution_memory_summary(memory)
    assert "type_text" in summary
    assert "hello" in summary
    assert "private" not in summary

    messages = _messages_with_execution_memory(
        [{"role": "system", "content": "base"}, {"role": "user", "content": "go"}],
        memory,
    )
    assert "当前 Run 的执行记忆" in messages[0]["content"]
    assert messages[1]["content"] == "go"
