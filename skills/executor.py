"""Deterministic Skill executor built on the existing tool registry."""
from __future__ import annotations

import asyncio
import inspect
import json
import re
from contextlib import asynccontextmanager
from typing import Any, Awaitable, Callable, Optional

from runtime.controller import RunController
from runtime.lock import desktop_execution_lock
from tools.registry import get_tool

from .schema import InputType, SkillDocument, SkillStep

AgentRunner = Callable[[str, list[str]], Awaitable[Any]]
ScriptRunner = Callable[[str, dict[str, Any]], Awaitable[Any]]

ACTION_TO_TOOL = {
    "app.launch": "app_launch",
    "app.activate": "app_switch",
    "app.quit": "app_quit",
    "ui.inspect": "list_elements",
    "ui.locate": "find_element",
    "ui.click": "find_and_click",
    "ui.type": "type_text",
    "ui.key": "press_key",
    "ui.hotkey": "hotkey",
    "ui.scroll": "scroll",
    "ui.wait": "sleep",
    "browser.navigate": "browser_navigate",
    "browser.inspect": "browser_get_state",
    "browser.click": "browser_click",
    "browser.type": "browser_type",
    "browser.key": "browser_press_key",
    "browser.scroll": "browser_scroll",
    "file.read": "read_file",
    "file.write": "write_file",
    "powershell.runApproved": "approved_powershell_script",
}

_VARIABLE = re.compile(r"{{\s*([A-Za-z0-9_.-]+)\s*}}")


class SkillExecutionError(RuntimeError):
    pass


class SkillExecutor:
    def __init__(
        self,
        agent_runner: Optional[AgentRunner] = None,
        script_runner: Optional[ScriptRunner] = None,
    ):
        self.agent_runner = agent_runner
        self.script_runner = script_runner

    async def execute(
        self,
        document: SkillDocument,
        inputs: dict[str, Any],
        controller: Optional[RunController] = None,
    ) -> dict[str, Any]:
        values = self._validate_inputs(document, inputs)
        context: dict[str, Any] = {"input": values, "steps": {}}
        results: list[dict[str, Any]] = []

        async with self._desktop_lock(controller):
            for step in document.execution.steps:
                if controller:
                    await controller.checkpoint()
                result = await self._execute_step(step, context, controller)
                results.append(result)
                context["steps"][step.id] = result
                if not result["success"] and step.on_failure == "stop":
                    raise SkillExecutionError(
                        f"Skill step failed: {step.id}: {result.get('error', 'unknown error')}"
                    )

        return {
            "skillId": document.metadata.id,
            "version": document.metadata.version,
            "success": all(item["success"] for item in results),
            "steps": results,
        }

    async def _execute_step(
        self,
        step: SkillStep,
        context: dict[str, Any],
        controller: Optional[RunController],
    ) -> dict[str, Any]:
        tool_name = "agent" if step.action == "agent" else ACTION_TO_TOOL.get(step.action)
        if tool_name is None:
            raise SkillExecutionError(f"Unsupported Skill action: {step.action}")

        runtime_step = None
        if controller:
            runtime_step = await controller.start_step(step.name, [tool_name])

        last_error: Optional[str] = None
        for attempt in range(1, step.retry.max_attempts + 1):
            try:
                output = await self._invoke(step, context, tool_name)
                await self._verify(step, output)
                result = {
                    "id": step.id,
                    "name": step.name,
                    "action": step.action,
                    "tool": tool_name,
                    "attempts": attempt,
                    "success": True,
                    "output": output,
                }
                if controller and runtime_step:
                    await controller.finish_step(
                        runtime_step,
                        success=True,
                        result=self._result_text(output),
                    )
                return result
            except Exception as error:
                last_error = str(error)
                if attempt < step.retry.max_attempts and step.retry.delay_seconds:
                    await asyncio.sleep(step.retry.delay_seconds)

        if step.fallback and self.agent_runner:
            try:
                instruction = step.fallback.instruction or (
                    f"Recover and complete failed step '{step.name}'. Error: {last_error}"
                )
                output = await self.agent_runner(
                    str(self._resolve(instruction, context)),
                    step.fallback.allowed_tools,
                )
                result = {
                    "id": step.id,
                    "name": step.name,
                    "action": step.action,
                    "tool": "agent",
                    "attempts": step.retry.max_attempts,
                    "success": True,
                    "fallback": True,
                    "output": output,
                }
                if controller and runtime_step:
                    await controller.finish_step(
                        runtime_step,
                        success=True,
                        result=self._result_text(output),
                    )
                return result
            except Exception as error:
                last_error = f"{last_error}; fallback failed: {error}"

        result = {
            "id": step.id,
            "name": step.name,
            "action": step.action,
            "tool": tool_name,
            "attempts": step.retry.max_attempts,
            "success": False,
            "error": last_error,
        }
        if controller and runtime_step:
            await controller.finish_step(runtime_step, success=False, error=last_error)
        return result

    async def _invoke(self, step: SkillStep, context: dict[str, Any], tool_name: str) -> Any:
        if step.action == "agent":
            if self.agent_runner is None:
                raise SkillExecutionError("Agent step requires an injected agent runner")
            instruction = self._resolve(step.instruction or "", context)
            return await self.agent_runner(str(instruction), step.allowed_tools)

        if step.action == "powershell.runApproved":
            if self.script_runner is None:
                raise SkillExecutionError("Approved PowerShell step requires a script registry")
            parameters = self._resolve({**step.target, **step.parameters}, context)
            script_id = parameters.pop("scriptId", None) or parameters.pop("script_id", None)
            if not script_id:
                raise SkillExecutionError("Approved PowerShell step requires scriptId")
            script_parameters = parameters.pop("parameters", parameters)
            return await self.script_runner(str(script_id), script_parameters)

        parameters = self._resolve({**step.target, **step.parameters}, context)
        parameters = self._normalize_parameters(step.action, parameters)

        tool = get_tool(tool_name)
        if tool is None:
            raise SkillExecutionError(f"Tool is not registered: {tool_name}")
        if inspect.iscoroutinefunction(tool):
            return await tool(**parameters)
        return await asyncio.to_thread(tool, **parameters)

    @staticmethod
    def _normalize_parameters(action: str, value: dict[str, Any]) -> dict[str, Any]:
        result = dict(value)
        if action == "app.launch" and "process" in result and "name" not in result:
            result["name"] = result.pop("process")
        if action == "app.activate" and "title" in result and "name" not in result:
            result["name"] = result.pop("title")
        if action == "ui.wait" and "duration" in result and "seconds" not in result:
            result["seconds"] = result.pop("duration")
        if action in {"ui.click", "ui.locate"} and "description" in result:
            result["target"] = result.pop("description")
        if action in {"ui.click", "ui.locate"}:
            if "name" in result and "target" not in result:
                result["target"] = result.pop("name")
            if "automationId" in result:
                result["automation_id"] = result.pop("automationId")
            result.pop("controlType", None)
        return result

    async def _verify(self, step: SkillStep, output: Any) -> None:
        if step.verify is None:
            return
        value = step.verify.model_dump(exclude={"type", "timeout_seconds"})
        expected = value.get("value")
        text = SkillExecutor._result_text(output)
        if step.verify.type == "result.contains" and str(expected) not in text:
            raise SkillExecutionError(f"Verification failed: result does not contain {expected!r}")
        if step.verify.type == "result.not_contains" and str(expected) in text:
            raise SkillExecutionError(f"Verification failed: result contains {expected!r}")
        if step.verify.type in {"window.exists", "window.closed"}:
            locator = step.verify.locator
            window_filter = locator.get("nameContains") or locator.get("process")
            tool = get_tool("list_windows")
            if tool is None:
                raise SkillExecutionError("Tool is not registered: list_windows")
            windows = await asyncio.to_thread(tool, filter=window_filter)
            found = bool(window_filter and str(window_filter).lower() in str(windows).lower())
            if step.verify.type == "window.exists" and not found:
                raise SkillExecutionError(f"Verification failed: window not found: {window_filter}")
            if step.verify.type == "window.closed" and found:
                raise SkillExecutionError(f"Verification failed: window still open: {window_filter}")
        elif step.verify.type not in {"result.contains", "result.not_contains"}:
            raise SkillExecutionError(f"Unsupported verification type: {step.verify.type}")

    @staticmethod
    def _validate_inputs(document: SkillDocument, supplied: dict[str, Any]) -> dict[str, Any]:
        unknown = set(supplied) - set(document.inputs)
        if unknown:
            raise SkillExecutionError(f"Unknown Skill inputs: {', '.join(sorted(unknown))}")
        values: dict[str, Any] = {}
        python_types = {
            InputType.STRING: str,
            InputType.INTEGER: int,
            InputType.NUMBER: (int, float),
            InputType.BOOLEAN: bool,
            InputType.ARRAY: list,
            InputType.OBJECT: dict,
        }
        for name, definition in document.inputs.items():
            if name in supplied:
                value = supplied[name]
            elif definition.default is not None:
                value = definition.default
            elif definition.required:
                raise SkillExecutionError(f"Missing required Skill input: {name}")
            else:
                continue
            expected = python_types[definition.type]
            if definition.type == InputType.INTEGER and isinstance(value, bool):
                valid = False
            else:
                valid = isinstance(value, expected)
            if not valid:
                raise SkillExecutionError(f"Invalid type for Skill input {name}: {definition.type.value}")
            if definition.type == InputType.ARRAY and definition.items:
                item_type = python_types[definition.items]
                if any(not isinstance(item, item_type) for item in value):
                    raise SkillExecutionError(
                        f"Invalid array item type for Skill input {name}: {definition.items.value}"
                    )
            values[name] = value
        return values

    @classmethod
    def _resolve(cls, value: Any, context: dict[str, Any]) -> Any:
        if isinstance(value, dict):
            return {key: cls._resolve(item, context) for key, item in value.items()}
        if isinstance(value, list):
            return [cls._resolve(item, context) for item in value]
        if not isinstance(value, str):
            return value
        full = _VARIABLE.fullmatch(value)
        if full:
            return cls._lookup(full.group(1), context)
        return _VARIABLE.sub(lambda match: str(cls._lookup(match.group(1), context)), value)

    @staticmethod
    def _lookup(path: str, context: dict[str, Any]) -> Any:
        value: Any = context
        for part in path.split("."):
            if not isinstance(value, dict) or part not in value:
                raise SkillExecutionError(f"Unknown Skill variable: {path}")
            value = value[part]
        return value

    @staticmethod
    def _result_text(value: Any) -> str:
        if isinstance(value, str):
            return value
        return json.dumps(value, ensure_ascii=False, default=str)

    @staticmethod
    @asynccontextmanager
    async def _desktop_lock(controller: Optional[RunController]):
        if controller is None:
            yield
            return
        async with desktop_execution_lock.hold(controller):
            yield
