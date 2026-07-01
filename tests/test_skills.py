from __future__ import annotations

from pathlib import Path

import pytest

from memory import init_db
from skills.executor import SkillExecutionError, SkillExecutor
from skills.parser import parse_skill_file
from skills.repository import SkillConflictError, SkillRepository
from skills.schema import SkillDocument


def skill_value(version: str = "1.0.0") -> dict:
    return {
        "apiVersion": "desktop-agent/v1alpha1",
        "kind": "Skill",
        "metadata": {
            "id": "send-outlook-mail",
            "name": "Send Outlook mail",
            "version": version,
            "status": "draft",
            "description": "Send a mail with Classic Outlook",
            "triggers": ["send mail"],
        },
        "applications": [{"id": "outlook", "process": "outlook.exe"}],
        "inputs": {
            "recipient": {"type": "string", "required": True},
            "attempts": {"type": "integer", "default": 1},
        },
        "execution": {
            "defaultMode": "guided",
            "timeoutSeconds": 120,
            "steps": [
                {
                    "id": "activate",
                    "name": "Activate Outlook",
                    "action": "app.activate",
                    "with": {"title": "Outlook"},
                },
                {
                    "id": "recipient",
                    "name": "Enter recipient",
                    "action": "ui.type",
                    "with": {"text": "{{ input.recipient }}"},
                    "verify": {"type": "result.contains", "value": "typed"},
                },
            ],
        },
    }


def test_schema_and_legacy_markdown_compatibility(tmp_path: Path) -> None:
    document = SkillDocument.model_validate(skill_value())
    round_trip = SkillDocument.from_yaml(document.to_yaml())
    assert round_trip.metadata.id == "send-outlook-mail"
    assert round_trip.execution.steps[1].parameters["text"] == "{{ input.recipient }}"

    spec_shape = skill_value()
    spec_shape["inputs"]["attachments"] = {
        "type": "array",
        "items": "string",
        "required": False,
    }
    spec_shape["execution"]["steps"][0].update(
        {
            "retry": {"maxAttempts": 2},
            "onFailure": "stop",
            "fallback": {"type": "agent", "allowedTools": ["ui.inspect"]},
            "policy": {"skipWhen": "unattendedApproved"},
            "risk": "external_side_effect",
        }
    )
    assert SkillDocument.model_validate(spec_shape).inputs["attachments"].items.value == "string"

    legacy = tmp_path / "legacy.skill.md"
    legacy.write_text(
        "---\nname: Daily Report\nversion: '1.2'\ntriggers: [daily report]\n---\n# Steps\nDo it",
        encoding="utf-8",
    )
    parsed = parse_skill_file(legacy)
    assert parsed is not None
    assert parsed.document is not None
    assert parsed.document.metadata.version == "1.2.0"
    assert parsed.document.execution.steps[0].action == "agent"

    init_db(tmp_path / "imports.db")
    repository = SkillRepository()
    assert repository.import_definitions([parsed]) == 1
    assert repository.import_definitions([parsed]) == 0
    assert repository.get("daily-report")["versions"][0]["sourceFormat"] == "markdown"


def test_skill_repository_lifecycle(tmp_path: Path) -> None:
    init_db(tmp_path / "skills.db")
    repository = SkillRepository()
    document = SkillDocument.model_validate(skill_value())

    created = repository.create(document)
    assert created["status"] == "draft"
    assert repository.get("send-outlook-mail")["publishedVersion"] is None

    validated = repository.validate("send-outlook-mail", "1.0.0")
    assert validated["status"] == "validated"
    published = repository.publish("send-outlook-mail", "1.0.0")
    assert published["status"] == "published"
    assert repository.get("send-outlook-mail")["publishedVersion"] == "1.0.0"

    with pytest.raises(SkillConflictError):
        repository.update_draft("send-outlook-mail", "1.0.0", document)

    deprecated = repository.deprecate("send-outlook-mail", "1.0.0")
    assert deprecated["status"] == "deprecated"
    assert repository.get("send-outlook-mail")["publishedVersion"] is None


@pytest.mark.asyncio
async def test_skill_executor_resolves_inputs_and_requires_approved_scripts(monkeypatch) -> None:
    calls: list[tuple[str, dict]] = []

    def fake_get_tool(name: str):
        def invoke(**kwargs):
            calls.append((name, kwargs))
            return "typed successfully" if name == "type_text" else "activated"

        return invoke

    monkeypatch.setattr("skills.executor.get_tool", fake_get_tool)
    result = await SkillExecutor().execute(
        SkillDocument.model_validate(skill_value()),
        {"recipient": "person@example.com"},
    )
    assert result["success"] is True
    assert calls == [
        ("app_switch", {"name": "Outlook"}),
        ("type_text", {"text": "person@example.com"}),
    ]

    unsafe = skill_value()
    unsafe["execution"]["steps"] = [
        {
            "id": "unsafe",
            "name": "Unsafe command",
            "action": "powershell.runApproved",
            "with": {"scriptId": "collect_report_files", "parameters": {}},
        }
    ]
    with pytest.raises(SkillExecutionError, match="script registry"):
        await SkillExecutor().execute(
            SkillDocument.model_validate(unsafe),
            {"recipient": "person@example.com"},
        )


@pytest.mark.asyncio
async def test_skill_executor_uses_declared_agent_fallback(monkeypatch) -> None:
    monkeypatch.setattr("skills.executor.get_tool", lambda _: lambda **__: (_ for _ in ()).throw(
        RuntimeError("locator failed")
    ))
    value = skill_value()
    value["execution"]["steps"] = [
        {
            "id": "recover",
            "name": "Recover locator",
            "action": "ui.click",
            "with": {"target": "New Email"},
            "fallback": {
                "type": "agent",
                "instruction": "Recover {{ input.recipient }}",
                "allowedTools": ["ui.inspect", "ui.click"],
            },
        }
    ]
    fallback_calls = []

    async def agent_runner(instruction, allowed_tools):
        fallback_calls.append((instruction, allowed_tools))
        return "recovered"

    result = await SkillExecutor(agent_runner=agent_runner).execute(
        SkillDocument.model_validate(value),
        {"recipient": "person@example.com"},
    )
    assert result["steps"][0]["fallback"] is True
    assert fallback_calls == [("Recover person@example.com", ["ui.inspect", "ui.click"])]
