from __future__ import annotations

import json

import pytest

from tools import winpeekaboo


def _window(
    hwnd: int,
    title: str,
    process: str,
    *,
    active: bool = False,
) -> dict:
    return {
        "hwnd": hwnd,
        "title": title,
        "process_name": process,
        "is_visible": True,
        "is_foreground": active,
        "bounds": {"x": 0, "y": 0, "width": 1200, "height": 800},
    }


@pytest.mark.asyncio
async def test_ctrl_n_detects_activates_and_reports_new_window(monkeypatch) -> None:
    main = _window(1, "Inbox - Outlook", "OUTLOOK.EXE")
    unrelated = _window(2, "Teams notification", "ms-teams.exe", active=True)
    compose = _window(
        3,
        "Untitled - Message (HTML)",
        "OUTLOOK.EXE",
        active=True,
    )
    snapshots = iter([
        [main],
        [main, unrelated],
        [main, unrelated, compose],
    ])
    commands = []

    def fake_run_wpb(*args, capture=True):
        commands.append(args)
        if args[:3] == ("list", "windows", "--json"):
            return json.dumps(next(snapshots))
        return ""

    monkeypatch.setattr(winpeekaboo, "_run_wpb", fake_run_wpb)
    monkeypatch.setattr(winpeekaboo.time, "sleep", lambda _: None)

    result = await winpeekaboo.hotkey(
        keys="Ctrl+N",
        window="Inbox - Outlook",
    )

    assert "window_title: Untitled - Message (HTML)" in result
    assert "window_activated: true" in result
    assert commands[-1] == (
        "window",
        "activate",
        "--title",
        "Untitled - Message (HTML)",
    )


@pytest.mark.asyncio
async def test_normal_hotkey_does_not_scan_windows(monkeypatch) -> None:
    commands = []

    def fake_run_wpb(*args, capture=True):
        commands.append(args)
        return ""

    monkeypatch.setattr(winpeekaboo, "_run_wpb", fake_run_wpb)

    result = await winpeekaboo.hotkey(
        keys="Ctrl+A",
        window="Untitled - Message (HTML)",
    )

    assert result == "已执行组合键: Ctrl+A"
    assert commands == [
        (
            "hotkey",
            "--keys",
            "Ctrl+A",
            "--window",
            "Untitled - Message (HTML)",
        )
    ]
