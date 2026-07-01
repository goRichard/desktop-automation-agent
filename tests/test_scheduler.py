from __future__ import annotations

import yaml

from memory import JobStatus, create_job, get_job, init_db
from scheduler import shutdown_scheduler, start_scheduler


def test_scheduler_pauses_legacy_prompt_jobs(tmp_path, monkeypatch) -> None:
    config_path = tmp_path / "config.yaml"
    database = tmp_path / "scheduler.db"
    config_path.write_text(
        yaml.safe_dump(
            {
                "active_profile": "local",
                "profiles": {
                    "local": {
                        "llm": {"model": "dummy", "api_base": "http://127.0.0.1:1/v1"},
                        "vision": {
                            "model": "dummy",
                            "api_base": "http://127.0.0.1:1/v1",
                        },
                    }
                },
                "agent": {"memory_db": str(database)},
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("DESKTOP_AGENT_CONFIG", str(config_path))
    import config.settings as settings_module

    settings_module._settings = None
    init_db(database)
    legacy = create_job("Legacy", "0 9 * * *", "old-skill")
    try:
        start_scheduler()
        refreshed = get_job(legacy.id)
        assert refreshed is not None
        assert refreshed.status == JobStatus.paused
    finally:
        shutdown_scheduler()
