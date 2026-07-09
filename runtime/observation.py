"""Lightweight runtime desktop observations for model context and action memory."""
from __future__ import annotations

import inspect
import json
from dataclasses import asdict, dataclass, field
from typing import Any, Optional

from .models import utc_now


@dataclass(frozen=True)
class RuntimeObservation:
    captured_at: str
    active_window: Optional[dict[str, Any]]
    visible_windows: list[dict[str, Any]] = field(default_factory=list)
    error: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


async def collect_runtime_observation(limit: int = 8) -> RuntimeObservation:
    """Collect a compact foreground/window-list snapshot.

    This is intentionally lightweight: no screenshots, no UIA tree, no audit log.
    Failures are returned in-band so model execution can continue when window
    enumeration is temporarily unavailable.
    """
    try:
        from tools.winpeekaboo import list_windows

        result = list_windows()
        raw = await result if inspect.isawaitable(result) else str(result)
        records = _parse_window_records(raw)
        visible = [_compact_window(item) for item in records if _window_title(item)]
        active = next(
            (item for item in visible if item.get("foreground")),
            visible[0] if visible else None,
        )
        return RuntimeObservation(
            captured_at=utc_now(),
            active_window=active,
            visible_windows=visible[:limit],
        )
    except Exception as error:
        return RuntimeObservation(
            captured_at=utc_now(),
            active_window=None,
            visible_windows=[],
            error=f"{type(error).__name__}: {error}",
        )


def observation_summary(observation: Optional[RuntimeObservation]) -> str:
    if observation is None:
        return "当前窗口状态：未采集"
    if observation.error:
        return f"当前窗口状态：采集失败：{observation.error}"

    lines = []
    active = observation.active_window
    if active:
        lines.append(
            "当前前台窗口："
            f"{active.get('title') or '<无标题>'} / "
            f"{active.get('process') or '<unknown>'} / "
            f"hwnd={active.get('hwnd') or '<unknown>'}"
        )
    else:
        lines.append("当前前台窗口：未识别")

    lines.append("当前可见窗口：")
    if not observation.visible_windows:
        lines.append("- 无")
    for index, item in enumerate(observation.visible_windows, start=1):
        marker = " / foreground" if item.get("foreground") else ""
        lines.append(
            f"{index}. {item.get('title') or '<无标题>'} / "
            f"{item.get('process') or '<unknown>'}{marker}"
        )
    return "\n".join(lines)


def active_window_label(observation: Optional[RuntimeObservation]) -> Optional[str]:
    if observation is None or observation.active_window is None:
        return None
    title = observation.active_window.get("title")
    process = observation.active_window.get("process")
    if title and process:
        return f"{title} / {process}"
    return str(title or process or "") or None


def _parse_window_records(raw: str) -> list[dict[str, Any]]:
    value = json.loads(raw)
    if isinstance(value, dict):
        value = value.get("windows", [])
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]


def _compact_window(item: dict[str, Any]) -> dict[str, Any]:
    bounds = item.get("bounds")
    compact: dict[str, Any] = {
        "title": _window_title(item),
        "process": str(item.get("process_name") or item.get("process") or ""),
        "hwnd": str(item.get("hwnd") or ""),
        "foreground": _window_record_is_foreground(item),
    }
    if isinstance(bounds, dict):
        compact["bounds"] = {
            "x": bounds.get("x"),
            "y": bounds.get("y"),
            "width": bounds.get("width"),
            "height": bounds.get("height"),
        }
    if "is_minimized" in item:
        compact["minimized"] = bool(item.get("is_minimized"))
    return compact


def _window_title(item: dict[str, Any]) -> str:
    return str(item.get("title") or item.get("text") or "").strip()


def _window_record_is_foreground(item: dict[str, Any]) -> bool:
    return any(
        bool(item.get(key))
        for key in ("is_active", "is_foreground", "is_focused", "active")
    )
