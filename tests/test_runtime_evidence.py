from __future__ import annotations

import asyncio
from pathlib import Path

from memory import init_db, list_runtime_evidence
from runtime import RunController, get_runtime_persistence
from runtime.evidence import RuntimeEvidenceCollector
from skills.schema import SkillStep


def test_failure_evidence_is_written_and_audited(tmp_path: Path) -> None:
    init_db(tmp_path / "evidence.db")

    def get_fake_tool(name: str):
        if name == "capture_image":
            def capture(output: str, **_) -> str:
                Path(output).write_bytes(b"png")
                return output

            return capture
        if name == "list_elements":
            return lambda **_: '[{"name":"Send","automation_id":"send"}]'
        return None

    async def check() -> None:
        controller = RunController(
            "session",
            "evidence",
            run_id="evidence-run",
            persistence=get_runtime_persistence(),
            run_type="skill",
            skill_id="mail",
            skill_version="1.0.0",
        )
        await controller.initialize()
        collector = RuntimeEvidenceCollector(tmp_path / "artifacts", get_fake_tool)
        result = await collector.collect(
            controller,
            SkillStep(
                id="send",
                name="Send",
                action="ui.click",
                target={"window": "Outlook", "name": "Send"},
            ),
            "button not found",
            {"action": "ui.click"},
        )
        assert Path(result["path"], "metadata.json").is_file()
        assert Path(result["artifacts"]["screenshot"]).is_file()
        assert Path(result["artifacts"]["uia"]).is_file()

    asyncio.run(check())
    records = list_runtime_evidence("evidence-run")
    assert len(records) == 1
    assert records[0].step_id == "send"
