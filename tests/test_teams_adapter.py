from __future__ import annotations

import json

import pytest

from tools import teams


def _window(hwnd: int, title: str, process: str = "ms-teams.exe") -> dict:
    return {
        "hwnd": hwnd,
        "title": title,
        "process_name": process,
        "bounds": {"x": 0, "y": 0, "width": 1200, "height": 800},
    }


def _element(name: str, control_type: str, x: int, y: int, automation_id: str = "") -> dict:
    return {
        "name": name,
        "control_type": control_type,
        "automation_id": automation_id,
        "bounds": {"x": x, "y": y, "width": 100, "height": 30},
    }


@pytest.fixture(autouse=True)
def reset_teams_window_identity(monkeypatch) -> None:
    monkeypatch.setattr(teams, "_last_teams_window_key", None)


def test_teams_window_selection_excludes_notification() -> None:
    notification = _window(1, "Teams notification")
    main = _window(2, "Microsoft Teams")

    assert teams._select_teams_window([notification, main]) == main


@pytest.mark.asyncio
async def test_fill_chat_uses_window_coordinates_and_foreground_actions(monkeypatch) -> None:
    batches = []

    async def fake_resolve_record(preferred=None):
        return _window(2, "Microsoft Teams")

    async def fake_activate(title):
        return "ok"

    async def fake_list_elements(window):
        raise AssertionError("teams_fill_chat must not run a UIA list_elements scan")

    async def fake_run_actions(actions):
        batches.append(json.loads(actions))
        return "ok"

    monkeypatch.setattr(teams, "_resolve_teams_window_record", fake_resolve_record)
    monkeypatch.setattr(teams, "window_activate", fake_activate)
    monkeypatch.setattr(teams, "list_elements", fake_list_elements)
    monkeypatch.setattr(teams, "run_actions", fake_run_actions)

    result = await teams.teams_fill_chat(
        window="Microsoft Teams",
        recipient="person@example.com",
        message="Status update",
    )

    actions = batches[0]
    assert result["data"]["method"] == "window_coordinates"
    assert result["data"]["recipient"] == "person@example.com"
    assert actions[0] == {"tool": "click", "args": {"on": "516,96"}}
    assert actions[2]["args"]["text"] == "person@example.com"
    assert actions[-1]["args"]["text"] == "Status update"
    assert all("window" not in action["args"] for action in actions)


@pytest.mark.asyncio
async def test_fill_chat_reports_foreground_input_stage(monkeypatch) -> None:
    async def fake_resolve_record(preferred=None):
        return _window(2, "Microsoft Teams")

    async def fake_activate(title):
        return "ok"

    async def fake_run_actions(actions):
        raise RuntimeError("input failed")

    monkeypatch.setattr(teams, "_resolve_teams_window_record", fake_resolve_record)
    monkeypatch.setattr(teams, "window_activate", fake_activate)
    monkeypatch.setattr(teams, "run_actions", fake_run_actions)

    with pytest.raises(
        teams.TeamsAutomationError,
        match="teams_fill_chat stopped during foreground input.*input failed",
    ):
        await teams.teams_fill_chat(
            window="Microsoft Teams",
            recipient="person@example.com",
            message="hello",
        )


@pytest.mark.asyncio
async def test_attachment_dialog_uses_foreground_keyboard(monkeypatch, tmp_path) -> None:
    attachment = tmp_path / "report.txt"
    attachment.write_text("report", encoding="utf-8")
    main = _window(2, "Microsoft Teams")
    dialog = _window(3, "Open", "msedgewebview2.exe")
    snapshots = iter([[main], [main], [main, dialog], [main]])
    element_responses = iter([
        [_element("Actions and apps", "Button", 20, 400)],
        [_element("Attach file", "MenuItem", 30, 350)],
    ])
    batches = []

    async def fake_list_windows():
        return json.dumps(next(snapshots))

    async def fake_list_elements(window):
        return json.dumps(next(element_responses))

    async def fake_activate(title):
        return "ok"

    async def fake_run_actions(actions):
        batches.append(json.loads(actions))
        return "ok"

    async def no_sleep(*args):
        return None

    monkeypatch.setattr(teams, "list_windows", fake_list_windows)
    monkeypatch.setattr(teams, "list_elements", fake_list_elements)
    monkeypatch.setattr(teams, "window_activate", fake_activate)
    monkeypatch.setattr(teams, "run_actions", fake_run_actions)
    monkeypatch.setattr(teams.asyncio, "sleep", no_sleep)

    result = await teams.teams_add_attachments(
        window="Microsoft Teams",
        paths=[str(attachment)],
        timeout_seconds=1,
    )

    file_actions = batches[-1]
    assert result["data"]["files"] == [str(attachment.resolve())]
    assert file_actions == [
        {"tool": "hotkey", "args": {"keys": "Alt+N"}},
        {"tool": "hotkey", "args": {"keys": "Ctrl+A"}},
        {"tool": "type_text", "args": {"text": str(attachment.resolve())}},
        {"tool": "press_key", "args": {"key": "Enter"}},
    ]


@pytest.mark.asyncio
async def test_send_uses_uia_button_and_no_confirmation_tool(monkeypatch) -> None:
    batches = []

    async def fake_resolve(preferred=None):
        return "Microsoft Teams"

    async def fake_activate(title):
        return "ok"

    async def fake_list_elements(window):
        return json.dumps([_element("Send", "Button", 900, 700, "send-button")])

    async def fake_run_actions(actions):
        batches.append(json.loads(actions))
        return "ok"

    async def no_sleep(*args):
        return None

    monkeypatch.setattr(teams, "_resolve_teams_window_title", fake_resolve)
    monkeypatch.setattr(teams, "window_activate", fake_activate)
    monkeypatch.setattr(teams, "list_elements", fake_list_elements)
    monkeypatch.setattr(teams, "run_actions", fake_run_actions)
    monkeypatch.setattr(teams.asyncio, "sleep", no_sleep)

    result = await teams.teams_send_message("Microsoft Teams")

    assert result["data"]["method"] == "uia_click"
    assert batches == [[{"tool": "click", "args": {"on": "950,715"}}]]
