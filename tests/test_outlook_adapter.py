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


@pytest.fixture(autouse=True)
def reset_compose_window_identity(monkeypatch) -> None:
    monkeypatch.setattr(outlook, "_last_compose_window_key", None)


@pytest.mark.parametrize(
    "payload",
    [
        {"data": {"elements": [{"name": "Subject", "control_type": "Edit"}]}},
        {"elements": {"items": [{"name": "Subject", "control_type": "Edit"}]}},
        {
            "elements": {
                "subject": {
                    "name": "Subject",
                    "control_type": "Edit",
                }
            }
        },
        {
            "data": json.dumps({
                "elements": [{"name": "Subject", "control_type": "Edit"}],
            })
        },
        json.dumps([{"name": "Subject", "control_type": "Edit"}]),
    ],
)
def test_parse_elements_accepts_supported_winpeekaboo_wrappers(payload) -> None:
    elements = outlook._parse_elements(json.dumps(payload))

    assert elements == [{"name": "Subject", "control_type": "Edit"}]


def test_file_dialog_uia_prefers_ordered_automation_id() -> None:
    elements = [
        {
            "name": "File name:",
            "control_type": "Edit",
            "automation_id": "1001",
            "bounds": {"x": 100, "y": 100, "width": 300, "height": 30},
        },
        {
            "name": "File name:",
            "control_type": "ComboBox",
            "automation_id": "FileNameControlHost",
            "bounds": {"x": 80, "y": 90, "width": 350, "height": 50},
        },
    ]

    point = outlook._uia_control_point(
        elements,
        aliases=("File name",),
        automation_ids=("FileNameControlHost", "1001"),
        control_types=("Edit", "ComboBox"),
    )

    assert point == (255, 115)


@pytest.mark.asyncio
async def test_outlook_uia_scan_retries_empty_transient_response(monkeypatch) -> None:
    responses = iter([
        "",
        json.dumps([{
            "name": "Browse This PC",
            "control_type": "MenuItem",
            "bounds": {"x": 10, "y": 20, "width": 100, "height": 30},
        }]),
    ])

    async def fake_list_elements(window):
        return next(responses)

    async def no_sleep(_):
        return None

    monkeypatch.setattr(outlook, "list_elements", fake_list_elements)
    monkeypatch.setattr(outlook.asyncio, "sleep", no_sleep)

    elements = await outlook._scan_uia_elements("Message", "Browse This PC")

    assert elements[0]["name"] == "Browse This PC"


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
    assert shortcuts == [("Alt+S", None)]


@pytest.mark.asyncio
async def test_send_tracks_compose_by_handle_when_title_is_subject_only(monkeypatch) -> None:
    compose = _window(2, "Quarterly Status")
    main = _window(1, "Inbox - Outlook")
    snapshots = iter([[compose, main], [main]])
    activations = []
    shortcuts = []
    outlook._last_compose_window_key = "2"

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

    result = await outlook.outlook_send_message(
        window="Untitled - Message (HTML)",
        timeout_seconds=1,
    )

    assert result["data"]["windowTitle"] == "Quarterly Status"
    assert activations == ["Quarterly Status"]
    assert shortcuts == [("Alt+S", None)]
    assert outlook._last_compose_window_key is None


@pytest.mark.asyncio
async def test_send_reports_new_blocking_dialog(monkeypatch) -> None:
    compose = _window(2, "Status - Message (HTML)")
    main = _window(1, "Inbox - Outlook")
    blocker = _window(3, "Check Names", "OUTLOOK.EXE")
    calls = 0

    async def fake_list_windows():
        nonlocal calls
        calls += 1
        records = [compose, main] if calls == 1 else [compose, main, blocker]
        return json.dumps(records)

    async def fake_activate(title):
        return "ok"

    async def fake_hotkey(keys, window=None):
        return "ok"

    monkeypatch.setattr(outlook, "list_windows", fake_list_windows)
    monkeypatch.setattr(outlook, "window_activate", fake_activate)
    monkeypatch.setattr(outlook, "hotkey", fake_hotkey)

    with pytest.raises(outlook.OutlookAutomationError, match="Check Names"):
        await outlook.outlook_send_message(
            window="Status - Message (HTML)",
            timeout_seconds=0.1,
        )


@pytest.mark.asyncio
async def test_empty_attachment_list_is_deterministically_skipped() -> None:
    result = await outlook.outlook_add_attachments(
        window="Untitled - Message",
        paths=[],
    )
    assert result["data"]["skipped"] is True


@pytest.mark.asyncio
async def test_attachment_uses_shortcuts_and_refreshes_subject_window_title(
    monkeypatch,
    tmp_path,
) -> None:
    attachment = tmp_path / "report.txt"
    attachment.write_text("report", encoding="utf-8")
    compose_title = "Quarterly Status - Message (HTML)"
    dialog_title = "Insert File"
    resolve_calls = []
    action_batches = []
    activations = []
    snapshots = iter([
        [_window(2, compose_title)],
        [_window(2, compose_title), _window(3, dialog_title, "OUTLOOK.EXE")],
        [_window(2, compose_title), _window(3, dialog_title, "OUTLOOK.EXE")],
        [_window(2, compose_title)],
    ])

    async def fake_resolve(preferred=None):
        resolve_calls.append(preferred)
        return compose_title

    async def fake_activate(title):
        activations.append(title)
        return "ok"

    async def fake_run_actions(actions):
        action_batches.append(json.loads(actions))
        return "batch ok"

    async def fake_list_records():
        return next(snapshots)

    async def fake_list_elements(window):
        if window == compose_title:
            return json.dumps([{
                "name": "Browse This PC",
                "control_type": "MenuItem",
                "bounds": {"x": 100, "y": 120, "width": 180, "height": 30},
            }])
        raise AssertionError("Insert File dialog must not be connected through UIA")

    monkeypatch.setattr(outlook, "_resolve_compose_window_title", fake_resolve)
    monkeypatch.setattr(outlook, "window_activate", fake_activate)
    monkeypatch.setattr(outlook, "run_actions", fake_run_actions)
    monkeypatch.setattr(outlook, "_list_window_records", fake_list_records)
    monkeypatch.setattr(outlook, "list_elements", fake_list_elements)

    result = await outlook.outlook_add_attachments(
        window="Untitled - Message (HTML)",
        paths=[str(attachment)],
        timeout_seconds=1,
    )

    menu_actions = action_batches[0]
    assert menu_actions[0] == {
        "tool": "hotkey",
        "args": {"keys": "Alt+N", "window": compose_title},
    }
    assert [
        action["args"]["key"]
        for action in menu_actions
        if action["tool"] == "press_key"
    ] == ["A", "F"]
    assert action_batches[1][0] == {
        "tool": "click",
        "args": {"on": "190,135", "window": compose_title},
    }
    assert action_batches[2][0] == {
        "tool": "hotkey",
        "args": {"keys": "Alt+N"},
    }
    assert action_batches[2][-1] == {
        "tool": "press_key",
        "args": {"key": "Enter"},
    }
    assert action_batches[2][2]["args"]["text"] == str(attachment.resolve())
    assert all(
        "window" not in action["args"]
        for action in action_batches[2]
    )
    assert resolve_calls[0] == "Untitled - Message (HTML)"
    assert result["data"]["windowTitle"] == compose_title
    assert dialog_title not in activations
    assert activations[-1] == compose_title
