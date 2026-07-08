"""
工具层入口：导入所有工具模块触发 @tool 装饰器注册
"""
# 导入所有工具模块（顺序触发 @tool 装饰器注册到 registry）
# scheduler_tool 延迟导入，避免循环依赖（scheduler_tool -> scheduler.engine -> memory）
from . import actions, browser, outlook, planner, system, teams, vision, winpeekaboo  # noqa: F401
from .registry import get_all_schemas, get_tool, get_tool_description, list_tools, tool

__all__ = [
    "tool",
    "get_all_schemas",
    "get_tool",
    "list_tools",
    "get_tool_description",
]
