from __future__ import annotations

import yaml
from fastapi.testclient import TestClient

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
    with TestClient(create_app("secret")) as client:
        assert client.get("/health").status_code == 200
        assert client.get("/runtime/capabilities").status_code == 401

        headers = {"X-Runtime-Token": "secret"}
        capabilities = client.get("/runtime/capabilities", headers=headers)
        assert capabilities.status_code == 200
        assert capabilities.json()["desktop"]["provider"] == "winpeekaboo"
        assert "publish" in capabilities.json()["skills"]

        environment = client.get("/runtime/environment", headers=headers)
        assert environment.status_code == 200
        assert environment.json()["browser"]["channel"] == "msedge"
        assert client.get("/runs", headers=headers).status_code == 200

        missing_session = client.post(
            "/runs",
            headers=headers,
            json={"user_input": "demo", "session_id": "missing"},
        )
        assert missing_session.status_code == 404

        created = client.post("/skills", headers=headers, json=skill_value())
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
