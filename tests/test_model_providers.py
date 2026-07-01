from __future__ import annotations

import hashlib

import pytest
from pydantic import ValidationError

import yaml

from config.settings import Settings
from config.model_provider import ModelProviderConfig, ProviderType, TLSConfig
from llm.providers import OpenAIProvider, _sanitize_error


def test_provider_config_is_validated_and_public_view_hides_secret(monkeypatch) -> None:
    monkeypatch.setenv("INTERNAL_LLM_KEY", "super-secret")
    config = ModelProviderConfig.model_validate(
        {
            "provider": "openai_compatible",
            "model": "internal-model",
            "baseUrl": "https://models.example.test/v1",
            "apiKeyEnv": "INTERNAL_LLM_KEY",
            "capabilities": {"toolCalling": True},
        }
    )
    assert config.provider == ProviderType.OPENAI_COMPATIBLE
    assert config.resolve_api_key() == "super-secret"
    public = config.public_dict()
    assert public["credentialConfigured"] is True
    assert "apiKeyEnv" not in public
    assert "super-secret" not in str(public)


def test_tls_bundle_and_fingerprint_are_strict(tmp_path, monkeypatch) -> None:
    bundle = tmp_path / "internal-ca.pem"
    bundle.write_text("certificate", encoding="utf-8")
    fingerprint = hashlib.sha256(bundle.read_bytes()).hexdigest()
    tls = TLSConfig(caBundle=bundle, fingerprint=fingerprint)
    assert tls.verify_value == str(bundle)

    with pytest.raises(ValidationError, match="fingerprint"):
        TLSConfig(caBundle=bundle, fingerprint="0" * 64)
    with pytest.raises(ValidationError, match="not found"):
        TLSConfig(caBundle=tmp_path / "missing.pem")
    with pytest.raises(ValidationError, match="FLOWPILOT_ALLOW_INSECURE_TLS"):
        TLSConfig(verify=False)
    monkeypatch.setenv("FLOWPILOT_ALLOW_INSECURE_TLS", "1")
    with pytest.warns(UserWarning, match="TLS verification is disabled"):
        assert TLSConfig(verify=False).verify_value is False


@pytest.mark.asyncio
async def test_ollama_uses_openai_compatible_v1_endpoint() -> None:
    config = ModelProviderConfig(
        provider="ollama",
        model="qwen3",
        baseUrl="http://127.0.0.1:11434",
    )
    provider = OpenAIProvider(config)
    assert str(provider.client.base_url) == "http://127.0.0.1:11434/v1/"
    await provider.close()


def test_health_errors_are_bounded_and_redacted() -> None:
    error = RuntimeError("connection failed api_key=secret-value\ntrace")
    sanitized = _sanitize_error(error)
    assert "secret-value" not in sanitized
    assert "<redacted>" in sanitized


def test_settings_support_preferred_profile_models(tmp_path, monkeypatch) -> None:
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        yaml.safe_dump(
            {
                "active_profile": "ollama",
                "profiles": {
                    "ollama": {
                        "models": {
                            "chat": {
                                "provider": "ollama",
                                "model": "qwen3",
                                "baseUrl": "http://127.0.0.1:11434",
                            },
                            "vision": {
                                "provider": "ollama",
                                "model": "qwen3-vl",
                                "baseUrl": "http://127.0.0.1:11434",
                                "capabilities": {"vision": True},
                            },
                        }
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("DESKTOP_AGENT_CONFIG", str(config_path))
    settings = Settings()
    assert settings.chat_model.provider == ProviderType.OLLAMA
    assert settings.vision_model.capabilities.vision is True


def test_resolved_secret_is_private() -> None:
    config = ModelProviderConfig(
        provider="openai",
        model="test-model",
        apiKeyEnv="NOT_SET",
    )
    config.set_resolved_api_key("private-value")
    assert config.resolve_api_key() == "private-value"
    assert "private-value" not in config.model_dump_json()
