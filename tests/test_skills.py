from __future__ import annotations

import json
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


def test_outlook_skill_is_structured_and_bounded() -> None:
    skill_path = (
        Path(__file__).parents[1]
        / "skills"
        / "user_skills"
        / "send_outlook_email.skill.yaml"
    )
    parsed = parse_skill_file(skill_path)

    assert parsed is not None
    assert parsed.document is not None
    document = parsed.document
    assert document.metadata.id == "send-email"
    assert document.metadata.version == "2.0.0"
    assert [step.action for step in document.execution.steps] == [
        "outlook.launch",
        "outlook.ensureMailView",
        "outlook.openCompose",
        "outlook.resolveCompose",
        "outlook.fillMessage",
        "outlook.addAttachments",
        "user.confirm",
        "outlook.send",
    ]
    assert document.execution.steps[2].fallback.allowed_tools == [
        "ui.inspect",
        "ui.click",
        "ui.hotkey",
        "ui.wait",
    ]
    assert document.execution.steps[-1].fallback is None
    assert document.execution.steps[-1].risk == "external_side_effect"


@pytest.mark.asyncio
async def test_outlook_skill_executes_adapter_without_agent(monkeypatch) -> None:
    skill_path = (
        Path(__file__).parents[1]
        / "skills"
        / "user_skills"
        / "send_outlook_email.skill.yaml"
    )
    document = SkillDocument.from_yaml(skill_path.read_text(encoding="utf-8"))
    calls: list[tuple[str, dict]] = []

    outputs = {
        "outlook_launch_classic": {
            "ok": True,
            "data": {"windowTitle": "Inbox - Outlook"},
            "error": None,
        },
        "outlook_open_compose": {
            "ok": True,
            "data": {"windowTitle": "Untitled - Message (HTML)"},
            "error": None,
        },
        "outlook_resolve_compose": {
            "ok": True,
            "data": {"windowTitle": "Untitled - Message (HTML)"},
            "error": None,
        },
    }

    def fake_get_tool(name: str):
        async def invoke(**parameters):
            calls.append((name, parameters))
            return outputs.get(name, {"ok": True, "data": {}, "error": None})

        return invoke

    async def reject_agent(*_):
        raise AssertionError("Deterministic Outlook path must not call the Agent")

    async def confirm(_):
        return True

    monkeypatch.setattr("skills.executor.get_tool", fake_get_tool)
    result = await SkillExecutor(
        agent_runner=reject_agent,
        confirmation_runner=confirm,
    ).execute(
        document,
        {
            "recipient": "person@example.com",
            "subject": "Status",
            "body": "Hello",
        },
    )

    assert result["success"] is True
    assert [name for name, _ in calls] == [
        "outlook_launch_classic",
        "outlook_ensure_mail_view",
        "outlook_open_compose",
        "outlook_resolve_compose",
        "outlook_fill_message",
        "outlook_add_attachments",
        "outlook_send_message",
    ]
    assert calls[1][1]["window"] == "Inbox - Outlook"
    assert calls[3][1] == {}
    assert calls[4][1] == {
        "window": "Untitled - Message (HTML)",
        "recipient": "person@example.com",
        "cc": "",
        "subject": "Status",
        "body": "Hello",
    }
    assert calls[5][1]["paths"] == []
    assert calls[-1][1]["window"] == "Untitled - Message (HTML)"


@pytest.mark.asyncio
async def test_outlook_skill_recovers_open_compose_then_returns_to_adapter(
    monkeypatch,
) -> None:
    skill_path = (
        Path(__file__).parents[1]
        / "skills"
        / "user_skills"
        / "send_outlook_email.skill.yaml"
    )
    document = SkillDocument.from_yaml(skill_path.read_text(encoding="utf-8"))
    calls: list[str] = []
    fallback_calls = []

    outputs = {
        "outlook_launch_classic": {
            "ok": True,
            "data": {"windowTitle": "Inbox - Outlook"},
            "error": None,
        },
        "outlook_resolve_compose": {
            "ok": True,
            "data": {"windowTitle": "Recovered - Message (HTML)"},
            "error": None,
        },
    }

    def fake_get_tool(name: str):
        async def invoke(**_):
            calls.append(name)
            if name == "outlook_open_compose":
                raise RuntimeError("shortcut did not create a window")
            return outputs.get(name, {"ok": True, "data": {}, "error": None})

        return invoke

    async def recover(instruction, allowed_tools):
        fallback_calls.append((instruction, allowed_tools))
        return "compose window opened and activated"

    async def confirm(_):
        return True

    monkeypatch.setattr("skills.executor.get_tool", fake_get_tool)
    result = await SkillExecutor(
        agent_runner=recover,
        confirmation_runner=confirm,
    ).execute(
        document,
        {
            "recipient": "person@example.com",
            "subject": "Status",
            "body": "Hello",
        },
    )

    assert result["success"] is True
    assert result["steps"][2]["fallback"] is True
    assert fallback_calls[0][1] == ["ui.inspect", "ui.click", "ui.hotkey", "ui.wait"]
    assert "outlook_resolve_compose" in calls
    assert calls[-1] == "outlook_send_message"


@pytest.mark.asyncio
async def test_structured_skill_batches_keyboard_actions(monkeypatch) -> None:
    captured = {}

    async def run_actions(**parameters):
        captured.update(parameters)
        return "ok"

    monkeypatch.setattr(
        "skills.executor.get_tool",
        lambda name: run_actions if name == "run_actions" else None,
    )
    value = skill_value()
    value["execution"]["steps"] = [{
        "id": "fill",
        "name": "Fill with keyboard",
        "action": "ui.actions",
        "with": {
            "actions": [
                {"tool": "type_text", "args": {"text": "{{ input.recipient }}"}},
                {"tool": "press_key", "args": {"key": "Enter"}},
                {"tool": "press_key", "args": {"key": "Tab"}},
            ]
        },
    }]

    result = await SkillExecutor().execute(
        SkillDocument.model_validate(value),
        {"recipient": "person@example.com"},
    )

    actions = json.loads(captured["actions"])
    assert actions[0]["args"]["text"] == "person@example.com"
    assert actions[1]["args"]["key"] == "Enter"
    assert result["steps"][0]["tool"] == "run_actions"


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


@pytest.mark.asyncio
async def test_skill_executor_step_mode_and_unattended_policy(monkeypatch) -> None:
    monkeypatch.setattr("skills.executor.get_tool", lambda _: lambda **__: "ok")
    value = skill_value()
    value["execution"]["steps"] = [
        {
            "id": "send",
            "name": "Send",
            "action": "ui.click",
            "with": {"target": "Send"},
            "risk": "external_side_effect",
        }
    ]
    document = SkillDocument.model_validate(value)
    confirmations = []

    async def confirm(details):
        confirmations.append(details)
        return True

    result = await SkillExecutor(confirmation_runner=confirm).execute(
        document,
        {"recipient": "person@example.com"},
        mode="step",
    )
    assert result["success"] is True
    assert confirmations[0]["reason"] == "step_mode"

    with pytest.raises(SkillExecutionError, match="not approved"):
        await SkillExecutor().execute(
            document,
            {"recipient": "person@example.com"},
            mode="unattended",
        )

    value["execution"]["steps"].insert(
        0,
        {
            "id": "approval",
            "name": "Approve send",
            "action": "user.confirm",
            "policy": {"skipWhen": "unattendedApproved"},
        },
    )
    unattended = await SkillExecutor().execute(
        SkillDocument.model_validate(value),
        {"recipient": "person@example.com"},
        mode="unattended",
        unattended_approved=True,
    )
    assert unattended["steps"][0]["output"] == "approved"


@pytest.mark.asyncio
async def test_condition_and_fixed_nested_skill_call(monkeypatch) -> None:
    monkeypatch.setattr("skills.executor.get_tool", lambda _: lambda **__: "ok")
    child_value = skill_value("2.0.0")
    child_value["metadata"]["id"] = "child-skill"
    child_value["execution"]["steps"] = [
        {"id": "wait", "name": "Wait", "action": "ui.wait", "with": {"seconds": 0}}
    ]
    child = SkillDocument.model_validate(child_value)

    parent_value = skill_value()
    parent_value["execution"]["steps"] = [
        {
            "id": "guard",
            "name": "Check recipient",
            "action": "condition",
            "with": {
                "left": "{{ input.recipient }}",
                "operator": "contains",
                "right": "@",
            },
        },
        {
            "id": "child",
            "name": "Call child",
            "action": "skill.call",
            "with": {
                "skillId": "child-skill",
                "version": "2.0.0",
                "inputs": {"recipient": "{{ input.recipient }}"},
            },
        },
    ]
    parent = SkillDocument.model_validate(parent_value)
    resolved = []

    async def resolver(skill_id, version, mode):
        resolved.append((skill_id, version, mode.value))
        return child

    result = await SkillExecutor(skill_resolver=resolver).execute(
        parent,
        {"recipient": "person@example.com"},
    )
    assert result["success"] is True
    assert result["steps"][1]["output"]["skillId"] == "child-skill"
    assert resolved == [("child-skill", "2.0.0", "guided")]

    async def recursive_resolver(*_):
        return parent

    recursive = parent.model_copy(deep=True)
    recursive.execution.steps = [
        recursive.execution.steps[1].model_copy(
            update={
                "parameters": {
                    "skillId": parent.metadata.id,
                    "version": "1.0.0",
                    "inputs": {"recipient": "person@example.com"},
                }
            }
        )
    ]
    with pytest.raises(SkillExecutionError, match="Recursive Skill call"):
        await SkillExecutor(skill_resolver=recursive_resolver).execute(
            recursive,
            {"recipient": "person@example.com"},
        )
