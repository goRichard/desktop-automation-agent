"""
工具调用分发：解析 LLM 工具调用请求，执行对应工具函数，返回结果
"""
from __future__ import annotations

import asyncio
import json
import traceback
from typing import Any

from llm import ToolCall
from tools.registry import get_tool


async def execute(tool_calls: list[ToolCall]) -> list[dict[str, Any]]:
    """
    顺序执行所有工具调用，返回 OpenAI 格式的 tool result 消息列表。
    每个结果为 {"role": "tool", "tool_call_id": ..., "name": ..., "content": ...}

    注意：顺序执行而非并发，因为同一批次中的工具通常有顺序依赖
    （如 window_activate → click）。
    """
    messages = []
    for tc in tool_calls:
        result = await _execute_one(tc)
        if isinstance(result, Exception):
            content = f"工具执行出错: {type(result).__name__}: {result}"
        else:
            content = str(result) if result is not None else ""
        messages.append({
            "role": "tool",
            "tool_call_id": tc.id,
            "name": tc.name,
            "content": content,
        })
    return messages


async def _execute_one(tc: ToolCall) -> Any:
    """执行单个工具调用"""
    func = get_tool(tc.name)
    if func is None:
        return f"错误：未找到工具 '{tc.name}'。可用工具：{', '.join(_get_available_tools())}"

    try:
        result = func(**tc.arguments)
        if asyncio.iscoroutine(result):
            return await result
        return result
    except TypeError as e:
        return f"工具参数错误 ({tc.name}): {e}"
    except Exception as e:
        tb = traceback.format_exc()
        return f"工具执行失败 ({tc.name}): {type(e).__name__}: {e}\n{tb[:500]}"


def _get_available_tools() -> list[str]:
    from tools.registry import list_tools
    return list_tools()
