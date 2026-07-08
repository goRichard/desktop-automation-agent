"""Deterministic New Teams adapter backed by WinPeekaboo UIA and keyboard tools."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any, Optional

from .actions import run_actions
from .registry import tool
from .uia import parse_element_records
from .winpeekaboo import app_launch, list_elements, list_windows, window_activate


class TeamsAutomationError(RuntimeError):
    pass


_last_teams_window_key: Optional[str] = None


@tool(description="启动 New Microsoft Teams (ms-teams.exe)，返回主窗口标题。")
async def teams_launch_new() -> dict[str, Any]:
    await app_launch(name="ms-teams.exe", wait=True)
    window = _select_teams_window(await _list_window_records())
    title = _window_title(window)
    _remember_teams_window(window)
    await window_activate(title)
    return _success("launch", windowTitle=title, process="ms-teams.exe")


@tool(description="在 New Teams 主窗口使用 Ctrl+N 打开新聊天，并保持窗口前台。")
async def teams_open_new_chat(window: str) -> dict[str, Any]:
    window = await _resolve_teams_window_title(window)
    await window_activate(window)
    await run_actions(json.dumps([
        {"tool": "hotkey", "args": {"keys": "Ctrl+N"}},
        {"tool": "sleep", "args": {"seconds": 0.6}},
    ]))
    window = await _resolve_teams_window_title(window)
    return _success("open_new_chat", windowTitle=window, shortcut="Ctrl+N")


@tool(description="使用一次 UIA 扫描定位 New Teams 新聊天的收件人和消息输入框，并填写内容。")
async def teams_fill_chat(
    window: str,
    recipient: str,
    message: str,
) -> dict[str, Any]:
    try:
        window = await _resolve_teams_window_title(window)
    except Exception as error:
        raise TeamsAutomationError(
            f"teams_fill_chat stopped during window resolution: {error}"
        ) from error
    await window_activate(window)
    try:
        elements = await _scan_uia_elements(
            window,
            "Teams new chat",
            attempts=2,
        )
    except TeamsAutomationError as error:
        raise TeamsAutomationError(
            f"teams_fill_chat stopped during UIA scan: {error}"
        ) from error
    recipient_point = _control_point(
        elements,
        aliases=(
            "To", "Add people", "Enter name, email, or tag",
            "Type a name or group", "收件人", "添加人员",
        ),
        automation_ids=("people-picker-input", "new-chat-people-picker"),
        control_types=("Edit", "ComboBox"),
    )
    message_point = _control_point(
        elements,
        aliases=(
            "Type a new message", "Type a message", "Message",
            "Chat message", "键入新消息", "键入消息", "消息",
        ),
        automation_ids=("new-message", "message-compose-input"),
        control_types=("Edit", "Document", "TextBox"),
    )
    if recipient_point is None:
        raise TeamsAutomationError(
            "Teams recipient field was not found: " + _element_summary(elements)
        )
    if message_point is None:
        raise TeamsAutomationError(
            "Teams message field was not found: " + _element_summary(elements)
        )

    recipient_x, recipient_y = recipient_point
    message_x, message_y = message_point
    actions = [
        {"tool": "click", "args": {"on": f"{recipient_x},{recipient_y}"}},
        {"tool": "hotkey", "args": {"keys": "Ctrl+A"}},
        {"tool": "type_text", "args": {"text": recipient}},
        {"tool": "sleep", "args": {"seconds": 0.8}},
        {"tool": "press_key", "args": {"key": "Enter"}},
        {"tool": "sleep", "args": {"seconds": 0.4}},
        {"tool": "click", "args": {"on": f"{message_x},{message_y}"}},
        {"tool": "type_text", "args": {"text": message}},
    ]
    try:
        output = await run_actions(json.dumps(actions, ensure_ascii=False))
    except Exception as error:
        raise TeamsAutomationError(
            f"teams_fill_chat stopped during foreground input: {error}"
        ) from error
    return _success(
        "fill_chat",
        windowTitle=window,
        recipient=recipient,
        actionCount=len(actions),
        output=output,
    )


@tool(description="通过 New Teams 的 Actions and apps/Attach file 菜单添加本地附件；文件对话框使用前台键盘输入。")
async def teams_add_attachments(
    window: str,
    paths: list[str],
    timeout_seconds: float = 10.0,
) -> dict[str, Any]:
    if not paths:
        return _success("add_attachments", windowTitle=window, skipped=True, files=[])

    resolved = [str(Path(path).expanduser().resolve()) for path in paths]
    missing = [path for path in resolved if not Path(path).is_file()]
    if missing:
        raise TeamsAutomationError(f"Attachment files do not exist: {missing}")

    attached = []
    for path in resolved:
        window = await _resolve_teams_window_title(window)
        await window_activate(window)
        await _click_control(
            window,
            "Teams actions and apps",
            aliases=("Actions and apps", "Add actions and apps", "+", "操作和应用"),
            automation_ids=("actions-and-apps-button", "compose-plus-button"),
            control_types=("Button",),
        )
        await asyncio.sleep(0.25)
        before = await _list_window_records()
        before_keys = {_window_key(item) for item in before}
        await _click_control(
            window,
            "Teams attach file",
            aliases=("Attach file", "Attach", "附加文件", "附件"),
            automation_ids=("attach-file",),
            control_types=("MenuItem", "Button"),
        )
        await asyncio.sleep(0.25)
        # Some Teams builds open the file dialog directly; others show a source menu.
        dialog = await _wait_for_new_window(before_keys, 0.8)
        if dialog is None:
            await _click_control(
                window,
                "Teams upload from device",
                aliases=(
                    "Upload from this device", "Upload from my computer",
                    "Upload from this computer", "从此设备上传", "从我的电脑上传",
                ),
                automation_ids=("upload-from-device",),
                control_types=("MenuItem", "Button"),
            )
            dialog = await _wait_for_new_window(before_keys, timeout_seconds)
        if dialog is None:
            raise TeamsAutomationError("Teams attachment file dialog did not appear")

        dialog_key = _window_key(dialog)
        await run_actions(json.dumps([
            {"tool": "hotkey", "args": {"keys": "Alt+N"}},
            {"tool": "hotkey", "args": {"keys": "Ctrl+A"}},
            {"tool": "type_text", "args": {"text": path}},
            {"tool": "press_key", "args": {"key": "Enter"}},
        ], ensure_ascii=False))
        closed = await _wait_until(
            lambda records: not any(_window_key(item) == dialog_key for item in records),
            timeout_seconds,
        )
        if not closed:
            raise TeamsAutomationError("Teams attachment dialog is still open")
        attached.append(path)
        await asyncio.sleep(0.8)

    return _success("add_attachments", windowTitle=window, files=attached)


@tool(description="通过确定性 UIA 匹配定位并点击 New Teams 当前聊天的 Send 按钮。")
async def teams_send_message(window: str) -> dict[str, Any]:
    window = await _resolve_teams_window_title(window)
    await window_activate(window)
    await _click_control(
        window,
        "Teams send button",
        aliases=("Send", "Send message", "发送", "发送消息"),
        automation_ids=("send-message-button", "send-button"),
        control_types=("Button",),
    )
    return _success("send", windowTitle=window, method="uia_click")


async def _click_control(
    window: str,
    role: str,
    aliases: tuple[str, ...],
    automation_ids: tuple[str, ...],
    control_types: tuple[str, ...],
) -> None:
    elements: list[dict[str, Any]] = []
    point = None
    for attempt in range(1, 5):
        try:
            elements = await _scan_uia_elements(window, role, attempts=1)
        except TeamsAutomationError:
            elements = []
        point = _control_point(elements, aliases, automation_ids, control_types)
        if point is not None:
            break
        if attempt < 4:
            await asyncio.sleep(0.2 * attempt)
    if point is None:
        raise TeamsAutomationError(f"{role} was not found: {_element_summary(elements)}")
    await window_activate(window)
    await asyncio.sleep(0.2)
    await run_actions(json.dumps([
        {"tool": "click", "args": {"on": f"{point[0]},{point[1]}"}},
    ]))


async def _scan_uia_elements(
    window: str,
    role: str,
    attempts: int = 2,
) -> list[dict[str, Any]]:
    last_error = "empty response"
    for attempt in range(1, attempts + 1):
        try:
            elements = parse_element_records(await list_elements(window=window))
            if elements:
                return elements
            last_error = "empty element list"
        except Exception as error:
            last_error = str(error)
        if attempt < attempts:
            await asyncio.sleep(0.2 * attempt)
    raise TeamsAutomationError(f"{role} UIA scan failed after {attempts} attempts: {last_error}")


def _control_point(
    elements: list[dict[str, Any]],
    aliases: tuple[str, ...],
    automation_ids: tuple[str, ...],
    control_types: tuple[str, ...],
) -> Optional[tuple[int, int]]:
    normalized_aliases = {_normalize(item) for item in aliases}
    normalized_ids = {item.casefold() for item in automation_ids}
    normalized_types = {item.casefold() for item in control_types}
    candidates = []
    for element in elements:
        if element.get("is_visible") is False or element.get("is_enabled") is False:
            continue
        point = _element_center(element)
        if point is None:
            continue
        name = _normalize(str(element.get("name") or ""))
        automation_id = str(
            element.get("automation_id") or element.get("automationId") or ""
        ).casefold()
        control_type = str(
            element.get("control_type") or element.get("controlType") or ""
        ).casefold().rsplit(".", 1)[-1]
        score = 0
        if automation_id in normalized_ids:
            score += 1000
        if name in normalized_aliases:
            score += 800
        elif name and any(alias in name or name in alias for alias in normalized_aliases):
            score += 500
        if control_type in normalized_types:
            score += 50
        if score:
            candidates.append((score, point))
    if not candidates:
        return None
    candidates.sort(key=lambda item: item[0], reverse=True)
    return candidates[0][1]


async def _list_window_records() -> list[dict[str, Any]]:
    try:
        value = json.loads(await list_windows())
    except (TypeError, json.JSONDecodeError) as error:
        raise TeamsAutomationError(f"Invalid WinPeekaboo window response: {error}") from error
    if isinstance(value, dict):
        value = value.get("windows", [])
    if not isinstance(value, list):
        raise TeamsAutomationError("WinPeekaboo window response must be a list")
    return [item for item in value if isinstance(item, dict) and _window_title(item)]


async def _resolve_teams_window_title(preferred: Optional[str] = None) -> str:
    records = await _list_window_records()
    if _last_teams_window_key:
        remembered = [item for item in records if _window_key(item) == _last_teams_window_key]
        if remembered:
            return _window_title(remembered[0])
    window = _select_teams_window(records, preferred)
    _remember_teams_window(window)
    return _window_title(window)


def _select_teams_window(
    records: list[dict[str, Any]],
    preferred: Optional[str] = None,
) -> dict[str, Any]:
    candidates = [item for item in records if _is_teams_window(item)]
    if preferred:
        exact = [item for item in candidates if _window_title(item).casefold() == preferred.casefold()]
        if exact:
            return exact[0]
    if not candidates:
        raise TeamsAutomationError("New Teams main window was not found")
    return max(candidates, key=_window_priority)


async def _wait_for_new_window(
    before_keys: set[str],
    timeout_seconds: float,
) -> Optional[dict[str, Any]]:
    deadline = asyncio.get_running_loop().time() + max(0.1, timeout_seconds)
    while asyncio.get_running_loop().time() < deadline:
        records = await _list_window_records()
        new_windows = [item for item in records if _window_key(item) not in before_keys]
        if new_windows:
            return max(new_windows, key=_window_area)
        await asyncio.sleep(0.2)
    return None


async def _wait_until(predicate, timeout_seconds: float) -> bool:
    deadline = asyncio.get_running_loop().time() + max(0.1, timeout_seconds)
    while asyncio.get_running_loop().time() < deadline:
        if predicate(await _list_window_records()):
            return True
        await asyncio.sleep(0.2)
    return False


def _remember_teams_window(window: dict[str, Any]) -> None:
    global _last_teams_window_key
    _last_teams_window_key = _window_key(window)


def _is_teams_window(window: dict[str, Any]) -> bool:
    process = str(
        window.get("process_name") or window.get("process") or ""
    ).casefold()
    title = _window_title(window).casefold()
    is_process = any(name in process for name in ("ms-teams", "msteams", "teams"))
    is_main = "notification" not in title and "通知" not in title
    return is_process and is_main


def _window_title(window: Optional[dict[str, Any]]) -> str:
    if not window:
        return ""
    return str(window.get("title") or window.get("text") or "").strip()


def _window_key(window: dict[str, Any]) -> str:
    return str(window.get("hwnd") or f"title:{_window_title(window).casefold()}")


def _window_priority(window: dict[str, Any]) -> tuple[int, int]:
    active = any(bool(window.get(key)) for key in ("is_active", "is_foreground", "active"))
    return int(active), _window_area(window)


def _window_area(window: dict[str, Any]) -> int:
    bounds = window.get("bounds")
    if not isinstance(bounds, dict):
        return 0
    try:
        return int(bounds.get("width", 0)) * int(bounds.get("height", 0))
    except (TypeError, ValueError):
        return 0


def _element_center(element: dict[str, Any]) -> Optional[tuple[int, int]]:
    bounds = element.get("bounds")
    if not isinstance(bounds, dict):
        return None
    try:
        x = int(bounds.get("x", 0))
        y = int(bounds.get("y", 0))
        width = int(bounds.get("width", 0))
        height = int(bounds.get("height", 0))
    except (TypeError, ValueError):
        return None
    if width <= 0 or height <= 0:
        return None
    return x + width // 2, y + height // 2


def _normalize(value: str) -> str:
    return "".join(character for character in value.casefold() if character.isalnum())


def _element_summary(elements: list[dict[str, Any]], limit: int = 20) -> str:
    return json.dumps([
        {
            "name": str(item.get("name") or "")[:80],
            "automationId": str(item.get("automation_id") or item.get("automationId") or "")[:80],
            "controlType": str(item.get("control_type") or item.get("controlType") or "")[:40],
        }
        for item in elements[:limit]
    ], ensure_ascii=False)


def _success(action: str, **data: Any) -> dict[str, Any]:
    return {"ok": True, "data": {"action": action, **data}, "error": None}
