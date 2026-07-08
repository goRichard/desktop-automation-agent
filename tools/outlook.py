"""Classic Outlook UI adapter built exclusively on WinPeekaboo-backed tools."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any, Optional

from .actions import run_actions
from .registry import tool
from .uia import UIAResponseError, parse_element_records
from .winpeekaboo import (
    app_launch,
    hotkey,
    list_elements,
    list_windows,
    window_activate,
)


class OutlookAutomationError(RuntimeError):
    pass


_COMPOSE_TITLE_HINTS = (
    "message",
    "untitled",
    "new mail",
    "new message",
    "邮件",
    "邮件 -",
    "无标题",
)

_FIELD_ALIASES = {
    "recipient": ("to", "收件人", "recipient"),
    "cc": ("cc", "抄送"),
    "subject": ("subject", "主题"),
    "body": ("message", "body", "邮件正文", "正文"),
}

# A compose window changes title after Subject is entered. Keep its stable HWND
# so later adapter calls do not depend on title text containing "Message".
_last_compose_window_key: Optional[str] = None


@tool(description="启动 Classic Outlook (outlook.exe)，返回检测到的 Outlook 主窗口标题。")
async def outlook_launch_classic() -> dict[str, Any]:
    await app_launch(name="outlook.exe", wait=True)
    windows = await _list_window_records()
    candidates = [window for window in windows if _is_outlook_window(window)]
    candidates = [window for window in candidates if not _is_compose_window(window)]
    if not candidates:
        raise OutlookAutomationError("Classic Outlook main window was not found")
    window = max(candidates, key=_window_area)
    title = _window_title(window)
    await window_activate(title)
    return _success("launch", windowTitle=title, process="outlook.exe")


@tool(description="切换 Classic Outlook 到邮件视图；使用确定性快捷键 Ctrl+1。")
async def outlook_ensure_mail_view(window: str) -> dict[str, Any]:
    await window_activate(window)
    await hotkey(keys="Ctrl+1", window=window)
    await asyncio.sleep(0.4)
    return _success("ensure_mail_view", windowTitle=window, shortcut="Ctrl+1")


@tool(description="在 Classic Outlook 主窗口使用 Ctrl+N 新建邮件，并返回新写信窗口标题。")
async def outlook_open_compose(
    window: str,
    timeout_seconds: float = 8.0,
) -> dict[str, Any]:
    before = await _list_window_records()
    before_keys = {_window_key(item) for item in before}
    await window_activate(window)
    await hotkey(keys="Ctrl+N", window=window)

    compose = await _wait_for_window(
        lambda item: (
            _window_key(item) not in before_keys
            and _is_outlook_window(item)
            and _is_compose_window(item)
        ),
        timeout_seconds,
    )
    if compose is None:
        raise OutlookAutomationError("New Outlook compose window did not appear after Ctrl+N")
    _remember_compose_window(compose)
    title = _window_title(compose)
    await window_activate(title)
    return _success("open_compose", windowTitle=title, shortcut="Ctrl+N")


@tool(description="重新解析并激活当前 Classic Outlook 写信窗口。")
async def outlook_resolve_compose() -> dict[str, Any]:
    title = await _resolve_compose_window_title()
    await window_activate(title)
    return _success("resolve_compose", windowTitle=title)


@tool(description="使用一次 UIA 扫描和一次批量动作填写 Outlook 收件人、抄送、主题和正文。")
async def outlook_fill_message(
    window: str,
    recipient: str,
    subject: str,
    body: str,
    cc: Optional[str] = None,
) -> dict[str, Any]:
    window = await _resolve_compose_window_title(window)
    await window_activate(window)
    elements = await _scan_uia_elements(window, "Outlook compose")
    required = {
        "recipient": recipient,
        "subject": subject,
        "body": body,
    }
    if cc:
        required["cc"] = cc

    points = {
        field: _field_point(elements, field)
        for field in required
    }
    missing = [field for field, point in points.items() if point is None]
    if missing:
        raise OutlookAutomationError(
            f"Outlook compose fields were not found by UIA: {', '.join(missing)}"
        )

    actions: list[dict[str, Any]] = []
    for field in ("recipient", "cc", "subject", "body"):
        if field not in required:
            continue
        x, y = points[field] or (0, 0)
        actions.extend([
            {"tool": "click", "args": {"on": f"{x},{y}", "window": window}},
            {"tool": "hotkey", "args": {"keys": "Ctrl+A", "window": window}},
            {"tool": "type_text", "args": {"text": required[field], "window": window}},
        ])
        if field in {"recipient", "cc"}:
            actions.append({"tool": "press_key", "args": {"key": "Enter"}})

    output = await run_actions(json.dumps(actions, ensure_ascii=False))
    return _success(
        "fill_message",
        windowTitle=window,
        fields=list(required),
        actionCount=len(actions),
        output=output,
    )


@tool(description="使用 Classic Outlook 键盘路径 Alt+N → A → F → Browse This PC 添加附件；文件对话框使用前台键盘输入，不按标题连接；空列表直接跳过。")
async def outlook_add_attachments(
    window: str,
    paths: list[str],
    timeout_seconds: float = 8.0,
) -> dict[str, Any]:
    if not paths:
        return _success("add_attachments", windowTitle=window, skipped=True, files=[])

    resolved = [str(Path(path).expanduser().resolve()) for path in paths]
    missing = [path for path in resolved if not Path(path).is_file()]
    if missing:
        raise OutlookAutomationError(f"Attachment files do not exist: {missing}")

    attached = []
    for path in resolved:
        # Subject 输入后窗口标题会从 Untitled 变成 "<Subject> - Message (HTML)"。
        window = await _resolve_compose_window_title(window)
        await window_activate(window)
        await _open_attachment_menu(window)

        before = await _list_window_records()
        before_keys = {_window_key(item) for item in before}
        await _click_uia_control(
            window=window,
            role="Browse This PC",
            aliases=("Browse This PC", "浏览此电脑", "瀏覽此電腦"),
            automation_ids=("BrowseThisPC",),
            control_types=("Button", "MenuItem", "ListItem"),
        )
        dialog = await _wait_for_window(
            lambda item: _window_key(item) not in before_keys,
            timeout_seconds,
        )
        dialog_title = _window_title(dialog) if dialog else None
        if not dialog_title:
            raise OutlookAutomationError("Attachment file dialog did not appear")

        await _submit_attachment_path(dialog_title, path, timeout_seconds)
        attached.append(path)

    window = await _resolve_compose_window_title(window)
    await window_activate(window)
    return _success("add_attachments", windowTitle=window, files=attached)


async def _open_attachment_menu(window: str) -> None:
    actions = [
        {"tool": "hotkey", "args": {"keys": "Alt+N", "window": window}},
        {"tool": "sleep", "args": {"seconds": 0.25}},
        {"tool": "press_key", "args": {"key": "A"}},
        {"tool": "sleep", "args": {"seconds": 0.2}},
        {"tool": "press_key", "args": {"key": "F"}},
        {"tool": "sleep", "args": {"seconds": 0.4}},
    ]
    await run_actions(json.dumps(actions, ensure_ascii=False))


async def _submit_attachment_path(
    dialog_title: str,
    path: str,
    timeout_seconds: float,
) -> None:
    records = await _list_window_records()
    dialog_keys = {
        _window_key(item)
        for item in records
        if _window_title(item).lower() == dialog_title.lower()
    }
    if not dialog_keys:
        raise OutlookAutomationError(
            f"Attachment dialog was not found before file input: {dialog_title}"
        )

    # The Windows common file dialog is already foreground after Browse This PC.
    # Do not pass its title back to WinPeekaboo: some dialog hosts cannot be
    # reconnected by title even though they accept foreground keyboard input.
    # Alt+N focuses File name; Enter invokes the default Open/Insert button.
    input_actions = [
        {"tool": "hotkey", "args": {"keys": "Alt+N"}},
        {"tool": "hotkey", "args": {"keys": "Ctrl+A"}},
        {"tool": "type_text", "args": {"text": path}},
        {"tool": "press_key", "args": {"key": "Enter"}},
    ]
    await run_actions(json.dumps(input_actions, ensure_ascii=False))

    closed = await _wait_until(
        lambda current: not any(
            _window_key(item) in dialog_keys for item in current
        ),
        timeout_seconds,
    )
    if not closed:
        raise OutlookAutomationError(
            "Attachment dialog confirmation was clicked but the dialog is still open"
        )


async def _click_uia_control(
    window: str,
    role: str,
    aliases: tuple[str, ...],
    automation_ids: tuple[str, ...],
    control_types: tuple[str, ...],
) -> None:
    elements = await _scan_uia_elements(window, role)
    point = _uia_control_point(
        elements,
        aliases=aliases,
        automation_ids=automation_ids,
        control_types=control_types,
    )
    if point is None:
        raise OutlookAutomationError(
            f"{role} was not found by deterministic UIA matching: "
            f"{_element_summary(elements)}"
        )
    x, y = point
    actions = [
        {"tool": "click", "args": {"on": f"{x},{y}", "window": window}},
        {"tool": "sleep", "args": {"seconds": 0.4}},
    ]
    await run_actions(json.dumps(actions, ensure_ascii=False))


async def _scan_uia_elements(
    window: str,
    role: str,
    attempts: int = 4,
) -> list[dict[str, Any]]:
    last_error = "no response"
    last_raw: Any = None
    for attempt in range(1, max(1, attempts) + 1):
        try:
            last_raw = await list_elements(window=window)
            elements = _parse_elements(last_raw)
            if elements:
                return elements
            last_error = "WinPeekaboo returned an empty element list"
        except Exception as error:
            last_error = str(error)
        if attempt < attempts:
            await asyncio.sleep(0.2 * attempt)
    preview = str(last_raw or "<empty>").replace("\r", " ").replace("\n", " ")[:160]
    raise OutlookAutomationError(
        f"{role} UIA scan failed after {attempts} attempts: {last_error}; "
        f"raw={preview!r}"
    )


def _uia_control_point(
    elements: list[dict[str, Any]],
    aliases: tuple[str, ...],
    automation_ids: tuple[str, ...],
    control_types: tuple[str, ...],
) -> Optional[tuple[int, int]]:
    expected_ids = {
        item.casefold(): 1100 - index * 10
        for index, item in enumerate(automation_ids)
    }
    expected_types = {
        item.casefold(): 50 - index
        for index, item in enumerate(control_types)
    }
    normalized_aliases = {_normalize_ui_text(item) for item in aliases}
    candidates = []
    for element in elements:
        if element.get("is_visible") is False or element.get("is_enabled") is False:
            continue
        point = _element_center(element)
        if point is None:
            continue
        name = _normalize_ui_text(str(element.get("name") or ""))
        automation_id = str(
            element.get("automation_id") or element.get("automationId") or ""
        ).casefold()
        control_type = str(
            element.get("control_type") or element.get("controlType") or ""
        ).casefold().rsplit(".", 1)[-1]

        score = 0
        if automation_id and automation_id in expected_ids:
            score = expected_ids[automation_id]
        elif name and name in normalized_aliases:
            score = 900
        elif name and any(
            alias in name or name in alias
            for alias in normalized_aliases
        ):
            score = 700
        if control_type in expected_types:
            score += expected_types[control_type]
        if score:
            candidates.append((score, point))
    if not candidates:
        return None
    candidates.sort(key=lambda item: item[0], reverse=True)
    if (
        len(candidates) > 1
        and candidates[0][0] == candidates[1][0]
        and candidates[0][1] != candidates[1][1]
    ):
        return None
    return candidates[0][1]


def _normalize_ui_text(value: str) -> str:
    return "".join(character for character in value.casefold() if character.isalnum())


def _element_summary(elements: list[dict[str, Any]], limit: int = 20) -> str:
    summary = []
    for element in elements[:limit]:
        summary.append({
            "name": str(element.get("name") or "")[:80],
            "automationId": str(
                element.get("automation_id") or element.get("automationId") or ""
            )[:80],
            "controlType": str(
                element.get("control_type") or element.get("controlType") or ""
            )[:40],
        })
    return json.dumps(summary, ensure_ascii=False)


@tool(description="在已确认的 Outlook 写信窗口使用 Alt+S 发送，并确认写信窗口已关闭。")
async def outlook_send_message(
    window: str,
    timeout_seconds: float = 8.0,
) -> dict[str, Any]:
    before = await _list_window_records()
    window = _select_compose_window_title(before, window)
    matching_keys = {
        _window_key(item)
        for item in before
        if _window_title(item).lower() == window.lower()
    }
    if not matching_keys:
        raise OutlookAutomationError(f"Compose window was not found: {window}")

    await window_activate(window)
    await hotkey(keys="Alt+S", window=window)
    closed = await _wait_until(
        lambda records: not any(
            _window_key(item) in matching_keys for item in records
        ),
        timeout_seconds,
    )
    if not closed:
        raise OutlookAutomationError(
            "Send shortcut was issued but the compose window is still open"
        )
    global _last_compose_window_key
    _last_compose_window_key = None
    return _success("send", windowTitle=window, shortcut="Alt+S", windowClosed=True)


async def _list_window_records() -> list[dict[str, Any]]:
    raw = await list_windows()
    try:
        value = json.loads(raw)
    except (TypeError, json.JSONDecodeError) as error:
        raise OutlookAutomationError(f"Invalid WinPeekaboo window response: {error}") from error
    if isinstance(value, dict):
        value = value.get("windows", [])
    if not isinstance(value, list):
        raise OutlookAutomationError("WinPeekaboo window response must be a list")
    return [item for item in value if isinstance(item, dict) and _window_title(item)]


async def _resolve_compose_window_title(preferred: Optional[str] = None) -> str:
    return _select_compose_window_title(await _list_window_records(), preferred)


def _select_compose_window_title(
    records: list[dict[str, Any]],
    preferred: Optional[str] = None,
) -> str:
    if _last_compose_window_key:
        remembered = [
            item for item in records
            if _window_key(item) == _last_compose_window_key
        ]
        if remembered:
            return _window_title(remembered[0])

    candidates = [
        item
        for item in records
        if _is_outlook_window(item) and _is_compose_window(item)
    ]
    if preferred:
        exact = [
            item
            for item in candidates
            if _window_title(item).lower() == preferred.lower()
        ]
        if exact:
            _remember_compose_window(exact[0])
            return _window_title(exact[0])
    if not candidates:
        detail = f" (previous title: {preferred})" if preferred else ""
        raise OutlookAutomationError(f"Classic Outlook compose window was not found{detail}")
    selected = max(candidates, key=_window_priority)
    _remember_compose_window(selected)
    return _window_title(selected)


def _remember_compose_window(window: dict[str, Any]) -> None:
    global _last_compose_window_key
    _last_compose_window_key = _window_key(window)


async def _wait_for_window(predicate, timeout_seconds: float) -> Optional[dict[str, Any]]:
    deadline = asyncio.get_running_loop().time() + max(0.1, timeout_seconds)
    while asyncio.get_running_loop().time() < deadline:
        records = await _list_window_records()
        candidates = [item for item in records if predicate(item)]
        if candidates:
            return max(candidates, key=_window_area)
        await asyncio.sleep(0.25)
    return None


async def _wait_until(predicate, timeout_seconds: float) -> bool:
    deadline = asyncio.get_running_loop().time() + max(0.1, timeout_seconds)
    while asyncio.get_running_loop().time() < deadline:
        if predicate(await _list_window_records()):
            return True
        await asyncio.sleep(0.25)
    return False


def _parse_elements(raw: Any) -> list[dict[str, Any]]:
    try:
        return parse_element_records(raw)
    except UIAResponseError as error:
        raise OutlookAutomationError(str(error)) from error


def _field_point(elements: list[dict[str, Any]], field: str) -> Optional[tuple[int, int]]:
    aliases = _FIELD_ALIASES[field]
    candidates = []
    for element in elements:
        name = str(element.get("name") or "").lower()
        automation_id = str(
            element.get("automation_id") or element.get("automationId") or ""
        ).lower()
        control_type = str(
            element.get("control_type") or element.get("controlType") or ""
        ).lower()
        if not any(alias in name or alias in automation_id for alias in aliases):
            continue
        point = _element_center(element)
        if point is None:
            continue
        score = 0
        if control_type in {"edit", "document", "textbox"}:
            score += 4
        if any(name == alias for alias in aliases):
            score += 2
        candidates.append((score, point, control_type, element))

    if candidates:
        _, point, control_type, element = max(candidates, key=lambda item: item[0])
        if control_type not in {"edit", "document", "textbox"} and field in {
            "recipient",
            "cc",
            "subject",
        }:
            bounds = _element_bounds(element)
            if bounds:
                x, y, width, height = bounds
                return x + width + 120, y + height // 2
        return point

    if field == "body":
        documents = []
        for element in elements:
            control_type = str(
                element.get("control_type") or element.get("controlType") or ""
            ).lower()
            bounds = _element_bounds(element)
            if control_type in {"document", "edit"} and bounds:
                documents.append((_bounds_area(bounds), _element_center(element)))
        if documents:
            return max(documents, key=lambda item: item[0])[1]
    return None


def _element_center(element: dict[str, Any]) -> Optional[tuple[int, int]]:
    center = element.get("center")
    if isinstance(center, (list, tuple)) and len(center) == 2:
        return int(center[0]), int(center[1])
    bounds = _element_bounds(element)
    if not bounds:
        return None
    x, y, width, height = bounds
    if width <= 0 or height <= 0:
        return None
    return x + width // 2, y + height // 2


def _element_bounds(element: dict[str, Any]) -> Optional[tuple[int, int, int, int]]:
    bounds = element.get("bounds")
    if isinstance(bounds, dict):
        try:
            return (
                int(bounds.get("x", 0)),
                int(bounds.get("y", 0)),
                int(bounds.get("width", 0)),
                int(bounds.get("height", 0)),
            )
        except (TypeError, ValueError):
            return None
    if isinstance(bounds, (list, tuple)) and len(bounds) == 4:
        try:
            left, top, right, bottom = (int(value) for value in bounds)
            return left, top, right - left, bottom - top
        except (TypeError, ValueError):
            return None
    return None


def _window_key(window: dict[str, Any]) -> str:
    return str(window.get("hwnd") or f"title:{_window_title(window).lower()}")


def _window_title(window: Optional[dict[str, Any]]) -> str:
    if not window:
        return ""
    return str(window.get("title") or window.get("text") or "").strip()


def _window_area(window: dict[str, Any]) -> int:
    bounds = _element_bounds(window)
    return _bounds_area(bounds) if bounds else 0


def _window_priority(window: dict[str, Any]) -> tuple[int, int]:
    active = any(
        bool(window.get(key))
        for key in ("is_active", "is_foreground", "is_focused", "active")
    )
    return int(active), _window_area(window)


def _bounds_area(bounds: tuple[int, int, int, int]) -> int:
    return max(0, bounds[2]) * max(0, bounds[3])


def _is_outlook_window(window: dict[str, Any]) -> bool:
    process = str(
        window.get("process_name") or window.get("process") or ""
    ).lower()
    title = _window_title(window).lower()
    return "outlook" in process or "outlook" in title


def _is_compose_window(window: dict[str, Any]) -> bool:
    title = _window_title(window).lower()
    return any(hint in title for hint in _COMPOSE_TITLE_HINTS)


def _success(action: str, **data: Any) -> dict[str, Any]:
    return {
        "ok": True,
        "data": {"action": action, **data},
        "error": None,
    }
