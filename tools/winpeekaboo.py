"""
winpeekaboo 工具层：封装全部桌面自动化原子操作
通过调用 winpeekaboo Python 库或 CLI 实现
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from typing import Any, Optional

from .registry import tool


def _run_wpb(*args: str, capture: bool = True) -> str:
    """执行 winpeekaboo 命令，返回 stdout

    注意：
    - loguru 日志走 stderr，stderr 有内容不代表失败
    - rich 异常追踪走 stdout，真正的错误信息在 stdout 里
    - 只有 returncode != 0 时才视为错误
    - 设置 NO_COLOR=1 避免 rich 在非 TTY 子进程中渲染崩溃
    """
    cmd = [sys.executable, "-m", "winpeekaboo"] + list(args)

    # 继承当前环境，但强制关闭颜色输出
    # rich 在非 TTY 子进程中渲染 ANSI/markup 时可能崩溃（特别是中文）
    env = os.environ.copy()
    env["NO_COLOR"] = "1"
    env["PYTHONUTF8"] = "1"
    env["TERM"] = "dumb"

    result = subprocess.run(
        cmd,
        capture_output=capture,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
    )
    if result.returncode != 0:
        # 1) 先从 stdout 提取真正的错误（rich traceback 或普通错误消息）
        stdout_text = (result.stdout or "").strip()
        # 2) 从 stderr 提取，过滤掉 loguru INFO/DEBUG 日志
        stderr_lines = (result.stderr or "").strip().splitlines()
        stderr_errors = [
            line for line in stderr_lines
            if not (("| INFO" in line) or ("| DEBUG" in line) or ("| WARNING" in line))
        ]
        stderr_text = "\n".join(stderr_errors).strip()

        # 优先用 stdout（rich traceback 在这里），其次用 stderr 过滤后的错误行
        err_msg = stdout_text or stderr_text or "unknown error"
        # 截取前 500 字符避免消息过长，但保留足够诊断信息
        if len(err_msg) > 500:
            err_msg = err_msg[:500] + "..."
        raise RuntimeError(f"winpeekaboo error (rc={result.returncode}): {err_msg}")
    return result.stdout.strip() if capture else ""


# ══════════════════════════════════════════════════════
# 屏幕捕获
# ══════════════════════════════════════════════════════

@tool(description="截取屏幕截图，保存到指定文件。可指定目标窗口或区域（格式：x,y,width,height）。指定窗口时自动激活并前置窗口，确保截图内容正确。返回保存的文件路径。")
def capture_image(
    output: str,
    window: Optional[str] = None,
    region: Optional[str] = None,
) -> str:
    """winpeekaboo image --output {output} [--window window] [--region region]

    指定 window 时自动执行 window activate，确保目标窗口在前台可见。
    mss 截图库只能捕获屏幕可见区域，窗口被遮挡或最小化时截图内容不正确。
    """
    # 指定窗口时自动激活前置，解决 mss 遮挡问题
    if window:
        import time
        try:
            _run_wpb("window", "activate", "--title", window)
            time.sleep(0.3)  # 等待窗口渲染到前台
        except Exception:
            pass  # 激活失败不阻断截图（降级为截当前可见内容）

    args = ["image", "--output", output]
    if window:
        args += ["--window", window]
    if region:
        args += ["--region", region]
    _run_wpb(*args)
    return output


# @tool(description="捕获屏幕并识别 UI 元素，返回 JSON 格式的元素列表（包含元素名称、类型、坐标等信息）。可指定目标窗口。")
# def see_elements(window: Optional[str] = None) -> str:
#     """winpeekaboo see --json [--window window]"""
#     args = ["see", "--json"]
#     if window:
#         args += ["--window", window]
#     return _run_wpb(*args)


# ══════════════════════════════════════════════════════
# 鼠标操作
# ══════════════════════════════════════════════════════

@tool(description="⚠️ 仅供特殊场景：右键/中键/已知坐标。常规左键点击 UI 元素（按钮、菜单、链接等）请一律使用 find_and_click，定位更准且能自动激活新窗口。本工具仅用于：(1) 右键点击 button='right'；(2) 中键点击 button='middle'；(3) 已有精确坐标时传 '100,200'。")
def click(
    on: str,
    window: Optional[str] = None,
    button: Optional[str] = None,
) -> str:
    """winpeekaboo click --on {on} [--window window] [--button button]"""
    args = ["click", "--on", on]
    if window:
        args += ["--window", window]
    if button:
        args += ["--button", button]
    _run_wpb(*args)
    return f"已点击: {on}"


@tool(description="滚动桌面鼠标滚轮（用于非浏览器窗口，如记事本、文件资源管理器、Office 等）。direction 可选 up/down/left/right，amount 为滚动格数（默认3）。可指定目标窗口。浏览器滚动请用 browser_scroll。")
def scroll(
    direction: str,
    amount: Optional[int] = None,
    window: Optional[str] = None,
) -> str:
    """winpeekaboo scroll --direction {direction} [--amount amount] [--window window]"""
    args = ["scroll", "--direction", direction]
    if amount is not None:
        args += ["--amount", str(amount)]
    if window:
        args += ["--window", window]
    _run_wpb(*args)
    return f"已滚动: {direction} x{amount or 3}"


@tool(description="拖放操作。from_ 为起点（坐标或元素名），to 为终点（坐标或元素名）。可指定目标窗口。")
def drag(
    from_: str,
    to: str,
    window: Optional[str] = None,
) -> str:
    """winpeekaboo drag --from {from_} --to {to} [--window window]"""
    args = ["drag", "--from", from_, "--to", to]
    if window:
        args += ["--window", window]
    _run_wpb(*args)
    return f"已拖放: {from_} -> {to}"


# ══════════════════════════════════════════════════════
# 键盘操作
# ══════════════════════════════════════════════════════

@tool(description="在目标窗口（或当前焦点）中输入文本。delay 为每个字符之间的延迟秒数（模拟人工打字，默认0）。")
def type_text(
    text: str,
    window: Optional[str] = None,
    delay: Optional[float] = None,
) -> str:
    """winpeekaboo type --text {text} [--window window] [--delay delay]"""
    args = ["type", "--text", text]
    if window:
        args += ["--window", window]
    if delay is not None:
        args += ["--delay", str(delay)]
    _run_wpb(*args)
    return f"已输入文本: {text[:50]}{'...' if len(text) > 50 else ''}"


@tool(description="按下单个键。key 可以是 Enter/Escape/Tab/F1-F12/Delete/BackSpace/Up/Down/Left/Right 等。")
def press_key(key: str) -> str:
    """winpeekaboo press --key {key}"""
    _run_wpb("press", "--key", key)
    return f"已按键: {key}"


_AUTO_DETECT_WINDOW_HOTKEYS = {"ctrl+n", "ctrl+shift+n", "ctrl+o"}


@tool(description="执行键盘组合键。keys 格式如 'Ctrl+C'、'Ctrl+Shift+T'、'Alt+F4'。可指定目标窗口。Ctrl+N 等新建窗口快捷键会自动检测、激活并返回新窗口标题。")
def hotkey(
    keys: str,
    window: Optional[str] = None,
    detect_new_window: Optional[bool] = None,
) -> str:
    """执行快捷键，并按需检测快捷键创建的新窗口。"""
    normalized_keys = keys.lower().replace(" ", "")
    should_detect = (
        detect_new_window
        if detect_new_window is not None
        else normalized_keys in _AUTO_DETECT_WINDOW_HOTKEYS
    )
    before = _window_snapshot() if should_detect else None

    args = ["hotkey", "--keys", keys]
    if window:
        args += ["--window", window]
    _run_wpb(*args)

    result = f"已执行组合键: {keys}"
    if should_detect and before is not None:
        new_title = _wait_for_new_window(before, source_window=window)
        if new_title:
            activated = False
            try:
                _run_wpb("window", "activate", "--title", new_title)
                activated = True
            except Exception:
                pass
            result += (
                f'\n🔄 检测到新窗口已弹出，已自动激活: "{new_title}"'
                f"\nwindow_title: {new_title}"
                f"\nwindow_activated: {str(activated).lower()}"
            )
    return result


def _window_snapshot() -> Optional[dict[str, dict[str, Any]]]:
    try:
        value = json.loads(_run_wpb("list", "windows", "--json"))
    except Exception:
        return None
    if not isinstance(value, list):
        return None
    result = {}
    for item in value:
        if not isinstance(item, dict) or not _window_record_title(item):
            continue
        if item.get("is_visible") is False:
            continue
        identity = str(item.get("hwnd") or f"title:{_window_record_title(item).lower()}")
        result[identity] = item
    return result


def _wait_for_new_window(
    before: dict[str, dict[str, Any]],
    source_window: Optional[str],
    timeout_seconds: float = 3.0,
) -> Optional[str]:
    source_process = _source_window_process(before, source_window)
    deadline = time.monotonic() + timeout_seconds
    time.sleep(0.25)
    while time.monotonic() < deadline:
        after = _window_snapshot()
        if after is None:
            return None
        candidates = [
            item
            for identity, item in after.items()
            if identity not in before
        ]
        if source_process:
            candidates = [
                item
                for item in candidates
                if not _window_record_process(item)
                or _window_record_process(item) == source_process
            ]
        if candidates:
            selected = max(candidates, key=_window_record_priority)
            return _window_record_title(selected)
        time.sleep(0.2)
    return None


def snapshot_windows() -> Optional[dict[str, dict[str, Any]]]:
    """Return normalized visible windows for other adapter modules."""
    return _window_snapshot()


def wait_for_new_window(
    before: dict[str, dict[str, Any]],
    source_window: Optional[str] = None,
    timeout_seconds: float = 3.0,
) -> Optional[str]:
    """Wait for a same-process window created after the supplied snapshot."""
    return _wait_for_new_window(before, source_window, timeout_seconds)


def _source_window_process(
    records: dict[str, dict[str, Any]],
    source_window: Optional[str],
) -> str:
    if not source_window:
        return ""
    expected = source_window.lower()
    for item in records.values():
        title = _window_record_title(item).lower()
        if expected in title or title in expected:
            return _window_record_process(item)
    return ""


def _window_record_process(item: dict[str, Any]) -> str:
    return str(item.get("process_name") or item.get("process") or "").lower()


def _window_record_title(item: dict[str, Any]) -> str:
    return str(item.get("title") or item.get("text") or "").strip()


def _window_record_priority(item: dict[str, Any]) -> tuple[int, int, int]:
    active = any(
        bool(item.get(key))
        for key in ("is_active", "is_foreground", "is_focused", "active")
    )
    bounds = item.get("bounds")
    area = 0
    if isinstance(bounds, dict):
        try:
            area = int(bounds.get("width", 0)) * int(bounds.get("height", 0))
        except (TypeError, ValueError):
            pass
    return int(active), max(0, area), len(_window_record_title(item))


# ══════════════════════════════════════════════════════
# 窗口管理
# ══════════════════════════════════════════════════════

@tool(description="激活（聚焦）指定窗口，将其置于前台。title 为窗口标题（支持部分匹配）。")
def window_activate(title: str) -> str:
    _run_wpb("window", "activate", "--title", title)
    return f"已激活窗口: {title}"


@tool(description="最小化指定窗口。title 为窗口标题。")
def window_minimize(title: str) -> str:
    _run_wpb("window", "minimize", "--title", title)
    return f"已最小化: {title}"


@tool(description="最大化指定窗口。title 为窗口标题。")
def window_maximize(title: str) -> str:
    _run_wpb("window", "maximize", "--title", title)
    return f"已最大化: {title}"


@tool(description="还原指定窗口（从最大化或最小化状态恢复）。title 为窗口标题。")
def window_restore(title: str) -> str:
    _run_wpb("window", "restore", "--title", title)
    return f"已还原窗口: {title}"


@tool(description="关闭指定窗口。title 为窗口标题。")
def window_close(title: str) -> str:
    _run_wpb("window", "close", "--title", title)
    return f"已关闭窗口: {title}"


@tool(description="移动指定窗口到屏幕指定位置。title 为窗口标题，x/y 为左上角坐标（像素）。")
def window_move(title: str, x: int, y: int) -> str:
    _run_wpb("window", "move", "--title", title, "--x", str(x), "--y", str(y))
    return f"已移动窗口 {title} 到 ({x}, {y})"


@tool(description="调整指定窗口大小。title 为窗口标题，width/height 为宽高（像素）。")
def window_resize(title: str, width: int, height: int) -> str:
    _run_wpb("window", "resize", "--title", title, "--width", str(width), "--height", str(height))
    return f"已调整窗口 {title} 大小为 {width}x{height}"


# ══════════════════════════════════════════════════════
# 应用管理
# ══════════════════════════════════════════════════════

@tool(description="启动并激活应用程序。name 为可执行文件名（如 notepad.exe）。args 为附加命令行参数。wait=True 时等待应用启动完成。启动后自动激活窗口到前台并返回窗口标题。")
def app_launch(
    name: str,
    args: Optional[str] = None,
    wait: Optional[bool] = None,
) -> str:
    """winpeekaboo app launch --name {name} [--args args] [--wait]"""
    cmd_args = ["app", "launch", "--name", name]
    if args:
        cmd_args += ["--args", args]
    if wait:
        cmd_args.append("--wait")
    _run_wpb(*cmd_args)

    # 发现窗口标题：用 list apps 按进程名过滤（比 list windows 更可靠）
    result = f"已启动应用: {name}"
    try:
        import time
        time.sleep(2.0)  # 等待窗口创建
        proc_name = name.replace(".exe", "")
        apps_json = _run_wpb("list", "apps", "--json", "--filter", proc_name)
        apps = json.loads(apps_json)
        # 找到第一个非系统桌面的可视窗口
        found_title = None
        for proc, windows in apps.items():
            for w in windows:
                t = w.get("title") or ""
                if t and t != "Program Manager":
                    found_title = t
                    break
            if found_title:
                break
        if found_title:
            # 激活窗口到前台
            _run_wpb("window", "activate", "--title", found_title)
            result += f"\nwindow_title: {found_title}"
            result += "\nwindow_activated: true"
    except Exception:
        pass  # 窗口发现/激活失败不阻断（可能是无窗口应用或启动较慢）

    return result


@tool(description="关闭/退出指定应用。name 为应用窗口标题或进程名。")
def app_quit(name: str) -> str:
    _run_wpb("app", "quit", "--name", name)
    return f"已关闭应用: {name}"


@tool(description="切换到指定应用（激活其窗口）。name 为应用窗口标题。")
def app_switch(name: str) -> str:
    _run_wpb("app", "switch", "--name", name)
    return f"已切换到应用: {name}"


# ══════════════════════════════════════════════════════
# 资源列表
# ══════════════════════════════════════════════════════

@tool(description="列出所有打开的窗口，返回 JSON 格式的窗口列表（包含标题、句柄等）。filter 为可选过滤关键字。")
def list_windows(filter: Optional[str] = None) -> str:
    args = ["list", "windows", "--json"]
    if filter:
        args += ["--filter", filter]
    return _run_wpb(*args)


@tool(description="列出所有正在运行的应用程序，返回 JSON 格式。")
def list_apps() -> str:
    return _run_wpb("list", "apps", "--json")


@tool(description="列出所有显示器/屏幕信息，返回 JSON 格式（包含分辨率、位置等）。")
def list_screens() -> str:
    return _run_wpb("list", "screens", "--json")


@tool(description="列出指定窗口的所有 UI 元素，返回 JSON 格式（含 name、control_type、automation_id、bounds 等）。用于发现窗口中的元素及其 UIA AutomationId——将 automation_id 用于 batch_locate_elements/find_and_click 可实现确定性匹配（零模型调用、跨语言兼容）。window 为目标窗口标题。")
def list_elements(window: str) -> str:
    return _run_wpb("list", "elements", "--window", window, "--json")
