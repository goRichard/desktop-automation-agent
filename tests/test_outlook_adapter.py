from __future__ import annotations

import json

import pytest

from tools import outlook


def _window(hwnd: int, title: str, process: str = "OUTLOOK.EXE") -> dict:
    return {
        "hwnd": hwnd,
        "title": title,
        "process_name": process,
        "is_visible": True,
        "bounds": {"x": 0, "y": 0, "width": 1200, "height": 800},
    }


@pytest.mark.asyncio
async def test_open_compose_uses_shortcut_and_returns_new_window(monkeypatch) -> None:
    main = _window(1, "Inbox - Outlook")
    compose = _window(2, "Untitled - Message (HTML)")
    snapshots = iter([[main], [main, compose]])
    activations = []
    shortcuts = []

    async def fake_list_windows():
        return json.dumps(next(snapshots))

    async def fake_activate(title):
        activations.append(title)
        return "ok"

    async def fake_hotkey(keys, window=None):
        shortcuts.append((keys, window))
        return "ok"

    monkeypatch.setattr(outlook, "list_windows", fake_list_windows)
    monkeypatch.setattr(outlook, "window_activate", fake_activate)
    monkeypatch.setattr(outlook, "hotkey", fake_hotkey)

    result = await outlook.outlook_open_compose(
        window="Inbox - Outlook",
        timeout_seconds=1,
    )

    assert result["data"]["windowTitle"] == "Untitled - Message (HTML)"
    assert shortcuts == [("Ctrl+N", "Inbox - Outlook")]
    assert activations[-1] == "Untitled - Message (HTML)"


@pytest.mark.asyncio
async def test_fill_message_uses_one_uia_scan_and_one_action_batch(monkeypatch) -> None:
    elements = [
        {
            "name": "To",
            "control_type": "Button",
            "bounds": {"x": 100, "y": 100, "width": 50, "height": 30},
        },
        {
            "name": "Subject",
            "control_type": "Edit",
            "bounds": {"x": 300, "y": 180, "width": 500, "height": 30},
        },
        {
            "name": "Message",
            "control_type": "Document",
            "bounds": {"x": 200, "y": 240, "width": 800, "height": 500},
        },
    ]
    captured = {}

    async def fake_list_windows():
        return json.dumps([_window(2, "Untitled - Message")])

    async def fake_activate(title):
        return "ok"

    async def fake_list_elements(window):
        return json.dumps(elements)

    async def fake_run_actions(actions):
        captured["actions"] = json.loads(actions)
        return "batch ok"

    monkeypatch.setattr(outlook, "list_windows", fake_list_windows)
    monkeypatch.setattr(outlook, "window_activate", fake_activate)
    monkeypatch.setattr(outlook, "list_elements", fake_list_elements)
    monkeypatch.setattr(outlook, "run_actions", fake_run_actions)

    result = await outlook.outlook_fill_message(
        window="Untitled - Message",
        recipient="person@example.com",
        subject="Status",
        body="Hello",
    )

    actions = captured["actions"]
    assert result["data"]["fields"] == ["recipient", "subject", "body"]
    assert sum(action["tool"] == "type_text" for action in actions) == 3
    assert actions[0] == {
        "tool": "click",
        "args": {"on": "270,115", "window": "Untitled - Message"},
    }
    assert any(
        action["tool"] == "type_text"
        and action["args"]["text"] == "person@example.com"
        for action in actions
    )


@pytest.mark.asyncio
async def test_send_waits_for_compose_window_to_close(monkeypatch) -> None:
    compose = _window(2, "Status - Message (HTML)")
    main = _window(1, "Inbox - Outlook")
    snapshots = iter([[compose, main], [main]])
    shortcuts = []

    async def fake_list_windows():
        return json.dumps(next(snapshots))

    async def fake_activate(title):
        return "ok"

    async def fake_hotkey(keys, window=None):
        shortcuts.append((keys, window))
        return "ok"

    monkeypatch.setattr(outlook, "list_windows", fake_list_windows)
    monkeypatch.setattr(outlook, "window_activate", fake_activate)
    monkeypatch.setattr(outlook, "hotkey", fake_hotkey)

    result = await outlook.outlook_send_message(
        window="Untitled - Message (HTML)",
        timeout_seconds=1,
    )

    assert result["data"]["windowClosed"] is True
    assert result["data"]["windowTitle"] == "Status - Message (HTML)"
    assert shortcuts == [("Alt+S", "Status - Message (HTML)")]


@pytest.mark.asyncio
async def test_empty_attachment_list_is_deterministically_skipped() -> None:
    result = await outlook.outlook_add_attachments(
        window="Untitled - Message",
        paths=[],
    )
    assert result["data"]["skipped"] is True
