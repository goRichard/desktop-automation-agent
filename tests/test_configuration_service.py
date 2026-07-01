from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from config.model_provider import ModelProviderConfig, ModelRole
from config.service import ModelConfigurationService
from credentials import MemorySecretStore


def test_model_configuration_is_atomic_and_never_persists_resolved_secret(tmp_path: Path) -> None:
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        yaml.safe_dump(
            {
                "active_profile": "local",
                "profiles": {
                    "local": {
                        "llm": {
                            "model": "legacy",
                            "api_base": "http://127.0.0.1:1/v1",
                        },
                        "vision": {
                            "model": "vision",
                            "api_base": "http://127.0.0.1:1/v1",
                        },
                    }
                },
                "agent": {"system_prompt": "preserved"},
            }
        ),
        encoding="utf-8",
    )
    config = ModelProviderConfig(
        provider="openai_compatible",
        model="new-model",
        baseUrl="http://127.0.0.1:2/v1",
        apiKeySecret="models/chat",
    )
    config.set_resolved_api_key("must-not-be-written")

    ModelConfigurationService(config_path).save_model(ModelRole.CHAT, config)
    written = config_path.read_text(encoding="utf-8")
    parsed = yaml.safe_load(written)
    assert parsed["profiles"]["local"]["models"]["chat"]["model"] == "new-model"
    assert parsed["agent"]["system_prompt"] == "preserved"
    assert "must-not-be-written" not in written
    assert not config_path.with_suffix(".yaml.tmp").exists()


def test_ca_import_rejects_private_keys_and_copies_certificate(tmp_path: Path) -> None:
    config_path = tmp_path / "config.yaml"
    config_path.write_text("active_profile: local\nprofiles: {local: {}}\n", encoding="utf-8")
    service = ModelConfigurationService(config_path)

    private_key = tmp_path / "private.pem"
    private_key.write_text("-----BEGIN PRIVATE KEY-----\nsecret\n", encoding="utf-8")
    with pytest.raises(ValueError, match="Private keys"):
        service.import_ca_bundle(private_key)

    certificate = tmp_path / "ca.pem"
    certificate.write_text(
        "-----BEGIN CERTIFICATE-----\nTEST\n-----END CERTIFICATE-----\n",
        encoding="utf-8",
    )
    imported = service.import_ca_bundle(certificate, "Company CA")
    assert Path(imported["caBundle"]).is_file()
    assert len(imported["fingerprint"]) == 64


def test_memory_secret_store_contract() -> None:
    store = MemorySecretStore()
    assert store.get("models/chat") is None
    store.set("models/chat", "secret")
    assert store.get("models/chat") == "secret"
    assert store.delete("models/chat") is True
    assert store.delete("models/chat") is False
