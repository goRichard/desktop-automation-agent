from __future__ import annotations

import time

import yaml
from fastapi.testclient import TestClient

from credentials import MemorySecretStore
from tests.test_skills import skill_value


def test_runtime_api_authentication_and_lifespan(tmp_path, monkeypatch) -> None:
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        yaml.safe_dump(
            {
                "active_profile": "local",
                "profiles": {
                    "local": {
                        "llm": {
                            "model": "dummy",
                            "api_base": "http://127.0.0.1:1/v1",
                        },
                        "vision": {
                            "model": "dummy",
                            "api_base": "http://127.0.0.1:1/v1",
                        },
                    }
                },
                "agent": {
                    "memory_db": str(tmp_path / "api.db"),
                    "skills_dir": str(tmp_path / "skills"),
                    "system_prompt": "test",
                },
                "browser": {"channel": "msedge"},
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("DESKTOP_AGENT_CONFIG", str(config_path))

    import config.settings as settings_module
    from runtime.api import create_app

    settings_module._settings = None
    credential_store = MemorySecretStore()
    with TestClient(create_app("secret", credential_store=credential_store)) as client:
        assert client.get("/health").status_code == 200
        assert client.get("/runtime/capabilities").status_code == 401

        headers = {"X-Runtime-Token": "secret"}
        capabilities = client.get("/runtime/capabilities", headers=headers)
        assert capabilities.status_code == 200
        assert capabilities.json()["desktop"]["provider"] == "winpeekaboo"
        assert "publish" in capabilities.json()["skills"]
        assert "manage_credential" in capabilities.json()["models"]
        assert "run" in capabilities.json()["tasks"]

        environment = client.get("/runtime/environment", headers=headers)
        assert environment.status_code == 200
        assert environment.json()["browser"]["channel"] == "msedge"
        assert environment.json()["verification"]["mode"] == "checkpoint"
        assert client.get("/runs", headers=headers).status_code == 200

        models = client.get("/models", headers=headers)
        assert models.status_code == 200
        assert models.json()["chat"]["provider"] == "openai_compatible"
        assert "apiKeyEnv" not in models.json()["chat"]
        model_health = client.post(
            "/models/chat/health?probe=configuration",
            headers=headers,
        )
        assert model_health.status_code == 200
        assert model_health.json()["status"] == "healthy"

        updated_model = {
            "provider": "openai_compatible",
            "model": "updated-model",
            "baseUrl": "http://127.0.0.1:2/v1",
            "apiKeySecret": "models/chat",
        }
        updated = client.put("/models/chat", headers=headers, json=updated_model)
        assert updated.status_code == 200
        assert updated.json()["model"] == "updated-model"
        assert updated.json()["capabilities"]["toolCalling"] is True
        assert updated.json()["credentialConfigured"] is False

        credential = client.put(
            "/models/chat/credential",
            headers=headers,
            json={"secret": "managed-secret-value"},
        )
        assert credential.status_code == 200
        assert credential.json()["credentialConfigured"] is True
        assert credential_store.get("models/chat") == "managed-secret-value"
        assert "managed-secret-value" not in config_path.read_text(encoding="utf-8")

        ca_source = tmp_path / "source-ca.pem"
        ca_source.write_text(
            "-----BEGIN CERTIFICATE-----\nTEST\n-----END CERTIFICATE-----\n",
            encoding="utf-8",
        )
        imported = client.post(
            "/certificates/import",
            headers=headers,
            json={"sourcePath": str(ca_source), "displayName": "internal"},
        )
        assert imported.status_code == 201
        assert imported.json()["fingerprint"]

        missing_session = client.post(
            "/runs",
            headers=headers,
            json={"user_input": "demo", "session_id": "missing"},
        )
        assert missing_session.status_code == 404

        executable_skill = skill_value()
        executable_skill["execution"]["steps"] = [
            {
                "id": "wait",
                "name": "Short wait",
                "action": "ui.wait",
                "with": {"seconds": 0},
            }
        ]
        created = client.post("/skills", headers=headers, json=executable_skill)
        assert created.status_code == 201
        assert created.json()["status"] == "draft"

        validated = client.post(
            "/skills/send-outlook-mail/versions/1.0.0/validate",
            headers=headers,
        )
        assert validated.status_code == 200
        published = client.post(
            "/skills/send-outlook-mail/versions/1.0.0/publish",
            headers=headers,
        )
        assert published.status_code == 200
        assert published.json()["status"] == "published"

        skill = client.get("/skills/send-outlook-mail", headers=headers)
        assert skill.status_code == 200
        assert skill.json()["publishedVersion"] == "1.0.0"

        invalid_inputs = client.post(
            "/runs",
            headers=headers,
            json={"skillId": "send-outlook-mail", "inputs": {}, "mode": "guided"},
        )
        assert invalid_inputs.status_code == 422

        skill_run = client.post(
            "/runs",
            headers=headers,
            json={
                "skillId": "send-outlook-mail",
                "skillVersion": "1.0.0",
                "inputs": {"recipient": "person@example.com"},
                "mode": "guided",
            },
        )
        assert skill_run.status_code == 202
        guided_run = _wait_for_status(client, headers, skill_run.json()["id"], "succeeded")
        assert guided_run["run_type"] == "skill"
        assert guided_run["skill_version"] == "1.0.0"
        assert guided_run["token_usage"]["model_calls"] == 0
        assert guided_run["execution_memory"][0]["tool"] == "sleep"
        assert guided_run["steps"][0]["tool_names"] == ["sleep"]

        step_run = client.post(
            "/runs",
            headers=headers,
            json={
                "skillId": "send-outlook-mail",
                "inputs": {"recipient": "person@example.com"},
                "mode": "step",
            },
        )
        assert step_run.status_code == 202
        step_completed = _wait_for_status(
            client,
            headers,
            step_run.json()["id"],
            "succeeded",
        )
        assert step_completed["pending_confirmation"] is None

        task_document = {
            "apiVersion": "desktop-agent/v1alpha1",
            "kind": "Task",
            "metadata": {"id": "daily-mail", "name": "Daily mail"},
            "schedule": {
                "cron": "0 0 1 1 *",
                "timezone": "UTC",
                "misfirePolicy": "run_once",
                "maxConcurrentRuns": 1,
            },
            "skill": {"id": "send-outlook-mail", "version": "1.0.0"},
            "parameters": {"recipient": "person@example.com"},
            "execution": {"mode": "unattended", "timeoutSeconds": 30, "retries": 0},
            "permissions": {"externalSideEffectsApproved": False},
        }
        task = client.post("/tasks", headers=headers, json=task_document)
        assert task.status_code == 201
        assert task.json()["skillVersion"] == "1.0.0"
        assert task.json()["nextRunAt"] is not None

        referenced = client.post(
            "/skills/send-outlook-mail/versions/1.0.0/deprecate",
            headers=headers,
        )
        assert referenced.status_code == 409

        task_run = client.post("/tasks/daily-mail/run", headers=headers)
        assert task_run.status_code == 202
        _wait_for_status(client, headers, task_run.json()["id"], "succeeded")
        executions = _wait_for_task_execution(client, headers, "daily-mail")
        assert executions[0]["run_id"] == task_run.json()["id"]

        paused = client.post("/tasks/daily-mail/pause", headers=headers)
        assert paused.status_code == 200
        assert paused.json()["status"] == "paused"
        enabled = client.post("/tasks/daily-mail/enable", headers=headers)
        assert enabled.status_code == 200
        assert enabled.json()["status"] == "active"


def _wait_for_status(client, headers: dict, run_id: str, expected: str) -> dict:
    deadline = time.monotonic() + 2
    last = None
    while time.monotonic() < deadline:
        response = client.get(f"/runs/{run_id}", headers=headers)
        assert response.status_code == 200
        last = response.json()
        if last["status"] == expected:
            return last
        time.sleep(0.01)
    raise AssertionError(f"Run {run_id} did not reach {expected}; last={last}")


def _wait_for_task_execution(client, headers: dict, task_id: str) -> list[dict]:
    deadline = time.monotonic() + 2
    last = []
    while time.monotonic() < deadline:
        response = client.get(f"/tasks/{task_id}/executions", headers=headers)
        assert response.status_code == 200
        last = response.json()
        if last and last[0]["status"] != "running":
            return last
        time.sleep(0.01)
    raise AssertionError(f"Task {task_id} execution did not finish; last={last}")
