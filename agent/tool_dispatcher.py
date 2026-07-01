"""
工具调用分发：解析 LLM 工具调用请求，执行对应工具函数，返回结果
"""
from __future__ import annotations

import asyncio
import json
import traceback
from dataclasses import dataclass
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
        outcome = await _execute_one(tc)
        messages.append({
            "role": "tool",
            "tool_call_id": tc.id,
            "name": tc.name,
            "content": outcome.content,
            "success": outcome.success,
            "error": outcome.error,
            "data": outcome.data,
        })
    return messages


def rejected(tool_calls: list[ToolCall], error: str) -> list[dict[str, Any]]:
    """为被执行策略拒绝的调用生成完整 tool results，保持消息历史合法。"""
    return [
        {
            "role": "tool",
            "tool_call_id": tc.id,
            "name": tc.name,
            "content": f"工具调用被拒绝: {error}",
            "success": False,
            "error": error,
            "data": None,
        }
        for tc in tool_calls
    ]


def to_openai_messages(results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """去除 Runtime 元数据，只保留 OpenAI tool message 允许的字段。"""
    return [
        {
            "role": "tool",
            "tool_call_id": result["tool_call_id"],
            "name": result["name"],
            "content": result["content"],
        }
        for result in results
    ]


_ERROR_PREFIXES = (
    "错误：",
    "错误:",
    "工具执行失败",
    "工具参数错误",
    "工具执行出错",
    "❌",
)


def _looks_like_error(content: str) -> bool:
    """集中兼容尚未迁移为结构化结果的旧工具返回值。"""
    normalized = content.lstrip()
    return normalized.startswith(_ERROR_PREFIXES) or " 失败:" in normalized


@dataclass(frozen=True)
class _ExecutionOutcome:
    success: bool
    content: str
    error: str | None = None
    data: Any = None


def _normalize_result(result: Any) -> _ExecutionOutcome:
    """兼容旧字符串工具，并接受新工具返回的 {ok, data, error} 结构。"""
    if isinstance(result, dict) and "ok" in result:
        success = bool(result["ok"])
        content = json.dumps(result, ensure_ascii=False, default=str)
        error = None if success else str(result.get("error") or content)
        return _ExecutionOutcome(success, content, error, result.get("data"))

    content = str(result) if result is not None else ""
    success = not _looks_like_error(content)
    return _ExecutionOutcome(success, content, None if success else content)


async def _execute_one(tc: ToolCall) -> _ExecutionOutcome:
    """执行单个工具调用"""
    func = get_tool(tc.name)
    if func is None:
        error = f"未找到工具 '{tc.name}'。可用工具：{', '.join(_get_available_tools())}"
        return _ExecutionOutcome(False, f"错误：{error}", error)

    try:
        result = func(**tc.arguments)
        if asyncio.iscoroutine(result):
            result = await result
        return _normalize_result(result)
    except TypeError as e:
        error = f"工具参数错误 ({tc.name}): {e}"
        return _ExecutionOutcome(False, error, error)
    except Exception as e:
        tb = traceback.format_exc()
        error = f"工具执行失败 ({tc.name}): {type(e).__name__}: {e}"
        return _ExecutionOutcome(False, f"{error}\n{tb[:500]}", error)


def _get_available_tools() -> list[str]:
    from tools.registry import list_tools
    return list_tools()
