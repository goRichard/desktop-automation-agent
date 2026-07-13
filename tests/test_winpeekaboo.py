from __future__ import annotations

import json
import subprocess

import pytest

from tools import winpeekaboo


def test_winpeekaboo_command_timeout_is_reported(monkeypatch) -> None:
    captured = {}

    def fake_run(*args, **kwargs):
        captured.update(kwargs)
        raise subprocess.TimeoutExpired(args[0], kwargs["timeout"])

    monkeypatch.setattr(winpeekaboo.subprocess, "run", fake_run)

    with pytest.raises(RuntimeError, match="timed out after 3.0s during list elements"):
        winpeekaboo._run_wpb(
            "list",
            "elements",
            "--window",
            "Microsoft Teams",
            timeout_seconds=3.0,
        )

    assert captured["timeout"] == 3.0


@pytest.mark.asyncio
async def test_window_activate_fails_when_verification_is_not_foreground(monkeypatch) -> None:
    commands = []
    windows = [
        _window(1, "Microsoft Teams", "ms-teams.exe", active=False),
        _window(2, "Inbox - Outlook", "OUTLOOK.EXE", active=True),
    ]

    def fake_run_wpb(*args, capture=True, timeout_seconds=None):
        commands.append(args)
        if args[:3] == ("list", "windows", "--json"):
            return json.dumps(windows)
        return ""

    monkeypatch.setattr(winpeekaboo, "_run_wpb", fake_run_wpb)
    monkeypatch.setattr(winpeekaboo.time, "sleep", lambda _: None)

    with pytest.raises(RuntimeError, match="reported activate success.*not foreground"):
        await winpeekaboo.window_activate("Microsoft Teams")

    assert commands[:2] == [
        ("window", "restore", "--title", "Microsoft Teams"),
        ("window", "activate", "--title", "Microsoft Teams"),
    ]


@pytest.mark.asyncio
async def test_raw_element_scan_uses_bounded_timeout(monkeypatch) -> None:
    captured = {}

    def fake_run_wpb(*args, capture=True, timeout_seconds=None):
        captured["args"] = args
        captured["timeout"] = timeout_seconds
        return "[]"

    monkeypatch.setattr(winpeekaboo, "_run_wpb", fake_run_wpb)

    assert await winpeekaboo.list_elements("Microsoft Teams") == "[]"
    assert captured["timeout"] == 8.0


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

    def fake_run_wpb(*args, capture=True, timeout_seconds=None):
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

    def fake_run_wpb(*args, capture=True, timeout_seconds=None):
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
