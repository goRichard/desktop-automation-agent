"""
批量确定性操作工具：一次性执行多个无需视觉反馈的桌面操作。
适用：click → type_text → press_key → sleep 等连续确定性动作。
不适用：find_and_click / analyze_image 等需要视觉模型反馈的操作。
"""
from __future__ import annotations

import asyncio
import json
from typing import Any, Optional

from .registry import tool

# 支持的确定性操作及其默认参数
_SUPPORTED_ACTIONS: dict[str, list[str]] = {
    "click":           ["on", "window", "button"],
    "type_text":       ["text", "window", "delay"],
    "press_key":       ["key"],
    "hotkey":          ["keys", "window"],
    "window_activate": ["title"],
    "sleep":           ["seconds"],
    "capture_image":   ["output", "window", "region"],
    "scroll":          ["direction", "amount", "window"],
}


# ─────────────────────────────────────────────────────────
# 内部分发函数（直接调用原始工具函数，避免 registry 查找开销）
# ─────────────────────────────────────────────────────────

async def _dispatch(action: dict[str, Any]) -> str:
    """执行单个确定性操作，返回结果文字"""
    tool_name = action.get("tool", "")
    args = action.get("args", {})

    if tool_name not in _SUPPORTED_ACTIONS:
        return f"不支持的操作: {tool_name}（仅支持: {', '.join(_SUPPORTED_ACTIONS)}）"

    # 过滤未知参数
    valid_keys = _SUPPORTED_ACTIONS[tool_name]
    filtered_args = {k: v for k, v in args.items() if k in valid_keys}

    try:
        if tool_name == "click":
            from tools.winpeekaboo import click
            return await click(**filtered_args)

        elif tool_name == "type_text":
            from tools.winpeekaboo import type_text
            return await type_text(**filtered_args)

        elif tool_name == "press_key":
            from tools.winpeekaboo import press_key
            return await press_key(**filtered_args)

        elif tool_name == "hotkey":
            from tools.winpeekaboo import hotkey
            return await hotkey(**filtered_args)

        elif tool_name == "window_activate":
            from tools.winpeekaboo import window_activate
            return await window_activate(**filtered_args)

        elif tool_name == "sleep":
            from tools.system import sleep
            result = sleep(**filtered_args)
            if asyncio.iscoroutine(result):
                return await result
            return str(result) if result is not None else ""

        elif tool_name == "capture_image":
            from tools.winpeekaboo import capture_image
            return await capture_image(**filtered_args)

        elif tool_name == "scroll":
            from tools.winpeekaboo import scroll
            return await scroll(**filtered_args)

        else:
            return f"未知操作: {tool_name}"

    except TypeError as e:
        return f"参数错误 ({tool_name}): {e}"
    except Exception as e:
        return f"执行失败 ({tool_name}): {type(e).__name__}: {e}"


# ─────────────────────────────────────────────────────────
# 公共工具
# ─────────────────────────────────────────────────────────

@tool(description="批量执行确定性桌面操作。actions 为 JSON 数组，每项包含 tool（工具名）和 args（参数字典）。适合连续无依赖操作如 click→type_text→press_key→sleep。返回每步结果。不支持需要视觉反馈的操作（find_and_click/analyze_image 等）。\n示例: [{\"tool\":\"click\",\"args\":{\"on\":\"100,200\"}},{\"tool\":\"type_text\",\"args\":{\"text\":\"Hello\"}},{\"tool\":\"press_key\",\"args\":{\"key\":\"enter\"}}]")
async def run_actions(actions: str) -> str:
    """
    批量执行确定性桌面操作。

    参数:
        actions: JSON 数组字符串，每项 {"tool": "...", "args": {...}}

    返回:
        每步执行的汇总结果
    """
    try:
        action_list = json.loads(actions)
    except json.JSONDecodeError as e:
        return f"actions 格式错误（需为 JSON 数组）: {e}"

    if not isinstance(action_list, list):
        return "actions 必须是 JSON 数组"

    if len(action_list) > 30:
        return f"操作数量过多（{len(action_list)}），最多支持 30 个"

    results: list[str] = []
    for i, action in enumerate(action_list):
        tool_name = action.get("tool", "?")
        result = await _dispatch(action)
        results.append(f"  [{i+1}] {tool_name}: {result}")

    summary = f"批量执行完成（{len(action_list)} 个操作）:\n" + "\n".join(results)
    return summary
