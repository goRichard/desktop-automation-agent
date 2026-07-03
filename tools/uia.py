"""Shared WinPeekaboo UIA response parsing and element normalization."""
from __future__ import annotations

import json
import re
from typing import Any, Optional


class UIAResponseError(ValueError):
    pass


def parse_element_records(raw: Any) -> list[dict[str, Any]]:
    value = raw
    try:
        for _ in range(3):
            if not isinstance(value, str):
                break
            value = json.loads(value)
    except (TypeError, json.JSONDecodeError) as error:
        raise UIAResponseError(f"Invalid WinPeekaboo element response: {error}") from error

    elements = _extract_element_records(value)
    if elements is None:
        raise UIAResponseError(
            "Unsupported WinPeekaboo element response shape: "
            f"{response_shape(value)}"
        )
    return elements


def normalize_element_records(raw: Any) -> list[dict[str, Any]]:
    normalized = []
    for index, item in enumerate(parse_element_records(raw)):
        bounds = normalize_bounds(item.get("bounds"))
        center = normalize_center(item.get("center"), bounds)
        normalized.append({
            **item,
            "element_key": f"E{index + 1:04d}",
            "name": str(item.get("name") or "").strip(),
            "control_type": normalize_control_type(
                item.get("control_type") or item.get("controlType")
            ),
            "automation_id": str(
                item.get("automation_id") or item.get("automationId") or ""
            ).strip(),
            "bounds": bounds,
            "center": center,
            "is_visible": item.get("is_visible", item.get("isVisible", True)),
            "is_enabled": item.get("is_enabled", item.get("isEnabled", True)),
        })
    return normalized


def normalize_control_type(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return "Unknown"
    text = re.sub(r"^ControlType[.:]", "", text, flags=re.IGNORECASE)
    return text.rsplit(".", 1)[-1]


def normalize_bounds(value: Any) -> Optional[dict[str, int]]:
    if isinstance(value, dict):
        try:
            x = int(value.get("x", value.get("left", 0)))
            y = int(value.get("y", value.get("top", 0)))
            width_value = value.get("width")
            height_value = value.get("height")
            width = (
                int(width_value)
                if width_value is not None
                else int(value.get("right", x)) - x
            )
            height = (
                int(height_value)
                if height_value is not None
                else int(value.get("bottom", y)) - y
            )
            return {
                "x": x,
                "y": y,
                "width": width,
                "height": height,
            }
        except (TypeError, ValueError):
            return None
    if isinstance(value, (list, tuple)) and len(value) == 4:
        try:
            left, top, right, bottom = (int(item) for item in value)
        except (TypeError, ValueError):
            return None
        return {
            "x": left,
            "y": top,
            "width": right - left,
            "height": bottom - top,
        }
    return None


def normalize_center(
    value: Any,
    bounds: Optional[dict[str, int]],
) -> Optional[tuple[int, int]]:
    if isinstance(value, (list, tuple)) and len(value) == 2:
        try:
            return int(value[0]), int(value[1])
        except (TypeError, ValueError):
            return None
    if isinstance(value, dict):
        try:
            return int(value["x"]), int(value["y"])
        except (KeyError, TypeError, ValueError):
            return None
    if not bounds or bounds["width"] <= 0 or bounds["height"] <= 0:
        return None
    return (
        bounds["x"] + bounds["width"] // 2,
        bounds["y"] + bounds["height"] // 2,
    )


def response_shape(value: Any) -> str:
    if isinstance(value, dict):
        return f"object keys={list(value)[:10]}"
    if isinstance(value, list):
        return f"array length={len(value)}"
    return type(value).__name__


def _extract_element_records(
    value: Any,
    depth: int = 0,
) -> Optional[list[dict[str, Any]]]:
    if depth > 5:
        return None
    if isinstance(value, str):
        try:
            decoded = json.loads(value)
        except json.JSONDecodeError:
            return None
        return _extract_element_records(decoded, depth + 1)
    if isinstance(value, list):
        if not value:
            return []
        records = [
            item
            for item in value
            if isinstance(item, dict) and _looks_like_element_record(item)
        ]
        if records:
            return records
        combined: list[dict[str, Any]] = []
        for item in value:
            nested = _extract_element_records(item, depth + 1)
            if nested:
                combined.extend(nested)
        return combined or None
    if not isinstance(value, dict):
        return None
    if _looks_like_element_record(value):
        return [value]

    for key in ("elements", "items", "data", "result", "value", "records"):
        if key not in value:
            continue
        nested = _extract_element_records(value[key], depth + 1)
        if nested is not None:
            return nested

    records = [
        item
        for item in value.values()
        if isinstance(item, dict) and _looks_like_element_record(item)
    ]
    if records:
        return records

    combined = []
    for item in value.values():
        if not isinstance(item, (dict, list)):
            continue
        nested = _extract_element_records(item, depth + 1)
        if nested:
            combined.extend(nested)
    return combined or None


def _looks_like_element_record(value: dict[str, Any]) -> bool:
    return bool(
        {
            "name",
            "control_type",
            "controlType",
            "automation_id",
            "automationId",
            "bounds",
            "center",
        }
        & set(value)
    )
