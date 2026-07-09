from __future__ import annotations

import json

import pytest

from runtime.observation import (
    active_window_label,
    collect_runtime_observation,
    observation_summary,
)


@pytest.mark.asyncio
async def test_collect_runtime_observation_compacts_windows(monkeypatch) -> None:
    async def fake_list_windows():
        return json.dumps([
            {
                "title": "Microsoft Teams",
                "process_name": "ms-teams.exe",
                "hwnd": 100,
                "is_foreground": True,
                "bounds": {"x": 0, "y": 0, "width": 1200, "height": 800},
            },
            {
                "title": "Inbox - Outlook",
                "process_name": "outlook.exe",
                "hwnd": 200,
                "is_foreground": False,
            },
        ])

    monkeypatch.setattr("tools.winpeekaboo.list_windows", fake_list_windows)

    observation = await collect_runtime_observation()

    assert observation.error is None
    assert observation.active_window is not None
    assert observation.active_window["title"] == "Microsoft Teams"
    assert active_window_label(observation) == "Microsoft Teams / ms-teams.exe"
    summary = observation_summary(observation)
    assert "当前前台窗口：Microsoft Teams / ms-teams.exe" in summary
    assert "Inbox - Outlook / outlook.exe" in summary
