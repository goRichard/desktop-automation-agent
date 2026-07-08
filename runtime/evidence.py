"""Failure evidence capture for Skill Runs."""
from __future__ import annotations

import asyncio
import inspect
import json
from pathlib import Path
from typing import Any, Callable, Optional
from uuid import uuid4

from skills.schema import SkillStep
from tools.registry import get_tool

from .controller import RunController
from .models import utc_now


class RuntimeEvidenceCollector:
    def __init__(
        self,
        base_dir: Path,
        tool_getter: Callable[[str], Optional[Callable]] = get_tool,
    ):
        self.base_dir = Path(base_dir)
        self.tool_getter = tool_getter

    async def collect(
        self,
        controller: RunController,
        step: SkillStep,
        error: str,
        details: dict[str, Any],
    ) -> dict[str, Any]:
        evidence_id = str(uuid4())
        directory = self.base_dir / controller.state.id / f"{step.id}-{evidence_id[:8]}"
        await asyncio.to_thread(directory.mkdir, parents=True, exist_ok=True)
        metadata: dict[str, Any] = {
            "evidenceId": evidence_id,
            "runId": controller.state.id,
            "stepId": step.id,
            "skillId": controller.state.skill_id,
            "skillVersion": controller.state.skill_version,
            "action": step.action,
            "error": error,
            "details": details,
            "createdAt": utc_now(),
            "artifacts": {},
        }

        screenshot_path = directory / "screen.png"
        capture_error = await self._capture_screenshot(step, screenshot_path)
        if capture_error:
            metadata["artifacts"]["screenshotError"] = capture_error
        elif screenshot_path.exists():
            metadata["artifacts"]["screenshot"] = str(screenshot_path)

        uia_result = await self._capture_uia(step)
        if uia_result:
            uia_path = directory / "uia.txt"
            await asyncio.to_thread(uia_path.write_text, uia_result, encoding="utf-8")
            metadata["artifacts"]["uia"] = str(uia_path)

        metadata_path = directory / "metadata.json"
        await asyncio.to_thread(
            metadata_path.write_text,
            json.dumps(metadata, ensure_ascii=False, indent=2, default=str),
            encoding="utf-8",
        )
        from memory import save_runtime_evidence

        await asyncio.to_thread(
            save_runtime_evidence,
            {
                "id": evidence_id,
                "run_id": controller.state.id,
                "step_id": step.id,
                "path": str(directory),
                "metadata": metadata,
                "created_at": metadata["createdAt"],
            },
        )
        result = {
            "evidenceId": evidence_id,
            "path": str(directory),
            "artifacts": metadata["artifacts"],
        }
        await controller.emit("step.evidence", result)
        return result

    async def _capture_screenshot(self, step: SkillStep, output: Path) -> Optional[str]:
        tool = self.tool_getter("capture_image")
        if tool is None:
            return "capture_image is not registered"
        kwargs: dict[str, Any] = {"output": str(output)}
        window = step.target.get("window")
        if window:
            kwargs["window"] = window
        try:
            result = await self._invoke(tool, **kwargs)
            if not output.exists():
                return f"capture_image did not create a screenshot: {result}"
            return None
        except Exception as error:
            return f"{type(error).__name__}: {error}"

    async def _capture_uia(self, step: SkillStep) -> Optional[str]:
        window = step.target.get("window")
        if not window:
            return None
        tool = self.tool_getter("list_elements")
        if tool is None:
            # Raw UIA is intentionally not Agent-facing, but remains available
            # internally as a diagnostic artifact after failures.
            from tools.winpeekaboo import list_elements
            tool = list_elements
        try:
            return str(await self._invoke(tool, window=window))
        except Exception as error:
            return f"UIA capture failed: {type(error).__name__}: {error}"

    @staticmethod
    async def _invoke(tool: Callable, **kwargs: Any) -> Any:
        if inspect.iscoroutinefunction(tool):
            return await tool(**kwargs)
        return await asyncio.to_thread(tool, **kwargs)
