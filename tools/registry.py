"""
工具注册系统：@tool 装饰器，自动生成 OpenAI-compatible JSON Schema
"""
from __future__ import annotations

import asyncio
import inspect
import json
from functools import wraps
from typing import Any, Callable, Optional, get_type_hints

# 全局工具注册表
_tools: dict[str, dict[str, Any]] = {}
_tool_funcs: dict[str, Callable] = {}


def _python_type_to_json_schema(annotation) -> dict[str, Any]:
    """将 Python 类型注解转换为 JSON Schema 类型"""
    import typing
    origin = getattr(annotation, "__origin__", None)

    if annotation is str or annotation == "str":
        return {"type": "string"}
    elif annotation is int:
        return {"type": "integer"}
    elif annotation is float:
        return {"type": "number"}
    elif annotation is bool:
        return {"type": "boolean"}
    elif origin is list:
        args = getattr(annotation, "__args__", (Any,))
        return {"type": "array", "items": _python_type_to_json_schema(args[0])}
    elif origin is dict:
        return {"type": "object"}
    elif annotation is type(None):
        return {"type": "null"}
    elif origin is typing.Union:
        args = [a for a in annotation.__args__ if a is not type(None)]
        if len(args) == 1:
            return _python_type_to_json_schema(args[0])
        return {"type": "string"}
    else:
        return {"type": "string"}


def tool(description: str, name: Optional[str] = None):
    """
    装饰器：将函数注册为 Agent 可调用工具，自动生成 OpenAI function calling schema。

    用法：
        @tool(description="截取屏幕截图")
        async def capture_image(output: str, window: str = None) -> str:
            ...
    """
    def decorator(func: Callable) -> Callable:
        tool_name = name or func.__name__

        # 构建参数 schema
        sig = inspect.signature(func)
        try:
            hints = get_type_hints(func)
        except Exception:
            hints = {}

        properties: dict[str, Any] = {}
        required: list[str] = []

        for param_name, param in sig.parameters.items():
            if param_name in ("self", "cls"):
                continue

            annotation = hints.get(param_name, str)
            schema = _python_type_to_json_schema(annotation)

            # 从 docstring 或参数默认值提取描述
            properties[param_name] = schema

            # 没有默认值的参数为必填
            if param.default is inspect.Parameter.empty:
                required.append(param_name)

        tool_schema = {
            "type": "function",
            "function": {
                "name": tool_name,
                "description": description,
                "parameters": {
                    "type": "object",
                    "properties": properties,
                    "required": required,
                },
            },
        }

        _tools[tool_name] = tool_schema
        _tool_funcs[tool_name] = func

        @wraps(func)
        async def wrapper(*args, **kwargs):
            if asyncio.iscoroutinefunction(func):
                return await func(*args, **kwargs)
            else:
                loop = asyncio.get_event_loop()
                return await loop.run_in_executor(None, lambda: func(*args, **kwargs))

        wrapper._is_tool = True
        wrapper._tool_name = tool_name
        return wrapper

    return decorator


def get_all_schemas() -> list[dict[str, Any]]:
    """返回所有已注册工具的 OpenAI JSON Schema 列表"""
    return list(_tools.values())


def get_tool(name: str) -> Optional[Callable]:
    """根据名称获取工具函数"""
    return _tool_funcs.get(name)


def list_tools() -> list[str]:
    """返回所有已注册工具的名称列表"""
    return list(_tools.keys())


def get_tool_description(name: str) -> str:
    """获取工具的描述文本"""
    schema = _tools.get(name)
    if schema:
        return schema["function"]["description"]
    return ""
