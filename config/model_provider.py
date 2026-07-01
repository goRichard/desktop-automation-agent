"""Validated model provider configuration and secret resolution."""
from __future__ import annotations

import hashlib
import os
import warnings
from enum import Enum
from pathlib import Path
from typing import Optional

from pydantic import AliasChoices, BaseModel, ConfigDict, Field, PrivateAttr, model_validator


class ProviderType(str, Enum):
    OPENAI = "openai"
    OPENAI_COMPATIBLE = "openai_compatible"
    OLLAMA = "ollama"
    AZURE_OPENAI = "azure_openai"


class ModelRole(str, Enum):
    CHAT = "chat"
    VISION = "vision"


class ProviderCapabilities(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="forbid")

    chat: bool = True
    streaming: bool = True
    tool_calling: bool = Field(default=False, alias="toolCalling")
    vision: bool = False
    json_output: bool = Field(default=False, alias="jsonOutput")


class TLSConfig(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="forbid")

    verify: bool = True
    ca_bundle: Optional[Path] = Field(default=None, alias="caBundle")
    fingerprint: Optional[str] = None

    @model_validator(mode="after")
    def validate_tls(self) -> "TLSConfig":
        if not self.verify:
            if os.environ.get("FLOWPILOT_ALLOW_INSECURE_TLS") != "1":
                raise ValueError(
                    "TLS verify=false requires FLOWPILOT_ALLOW_INSECURE_TLS=1 in development"
                )
            warnings.warn(
                "TLS verification is disabled; unattended execution must reject this provider",
                stacklevel=2,
            )
        if self.fingerprint and not self.ca_bundle:
            raise ValueError("CA bundle fingerprint requires caBundle")
        if self.ca_bundle:
            if not self.ca_bundle.is_file():
                raise ValueError(f"CA bundle not found: {self.ca_bundle}")
            if self.fingerprint:
                expected = self.fingerprint.lower().replace(":", "")
                actual = hashlib.sha256(self.ca_bundle.read_bytes()).hexdigest()
                if expected != actual:
                    raise ValueError("CA bundle fingerprint does not match configured fingerprint")
        return self

    @property
    def verify_value(self) -> bool | str:
        if not self.verify:
            return False
        return str(self.ca_bundle) if self.ca_bundle else True


class ModelProviderConfig(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="forbid")
    _resolved_api_key: str = PrivateAttr(default="")

    provider: ProviderType
    model: str = Field(min_length=1)
    base_url: Optional[str] = Field(
        default=None,
        validation_alias=AliasChoices("baseUrl", "base_url", "api_base"),
        serialization_alias="baseUrl",
    )
    azure_endpoint: Optional[str] = Field(
        default=None,
        validation_alias=AliasChoices("azureEndpoint", "azure_endpoint"),
        serialization_alias="azureEndpoint",
    )
    api_version: Optional[str] = Field(
        default=None,
        validation_alias=AliasChoices("apiVersion", "api_version"),
        serialization_alias="apiVersion",
    )
    api_key_env: Optional[str] = Field(
        default=None,
        validation_alias=AliasChoices("apiKeyEnv", "api_key_env"),
        serialization_alias="apiKeyEnv",
    )
    api_key_secret: Optional[str] = Field(
        default=None,
        validation_alias=AliasChoices("apiKeySecret", "api_key_secret"),
        serialization_alias="apiKeySecret",
    )
    temperature: float = Field(default=0.7, ge=0, le=2)
    max_tokens: int = Field(
        default=4096,
        ge=1,
        validation_alias=AliasChoices("maxTokens", "max_tokens"),
        serialization_alias="maxTokens",
    )
    tls: TLSConfig = Field(default_factory=TLSConfig)
    capabilities: ProviderCapabilities = Field(default_factory=ProviderCapabilities)

    @model_validator(mode="after")
    def validate_provider_fields(self) -> "ModelProviderConfig":
        if self.provider == ProviderType.AZURE_OPENAI:
            if not self.azure_endpoint or not self.api_version:
                raise ValueError("Azure OpenAI requires azureEndpoint and apiVersion")
        elif self.provider == ProviderType.OPENAI_COMPATIBLE and not self.base_url:
            raise ValueError("OpenAI-compatible provider requires baseUrl")
        return self

    def public_dict(self) -> dict:
        """Return UI-safe configuration without resolved credentials."""
        value = self.model_dump(mode="json", by_alias=True, exclude_none=True)
        value["credentialConfigured"] = bool(self.resolve_api_key())
        value.pop("apiKeyEnv", None)
        value.pop("apiKeySecret", None)
        return value

    def resolve_api_key(self) -> str:
        """Environment is the development fallback until Credential Manager is wired."""
        if self._resolved_api_key:
            return self._resolved_api_key
        if self.api_key_env:
            return os.environ.get(self.api_key_env, "")
        return ""

    def set_resolved_api_key(self, value: str) -> None:
        """Inject a secret resolved by Settings or the future Credential Manager adapter."""
        self._resolved_api_key = value


def default_capabilities(role: ModelRole, provider: ProviderType) -> ProviderCapabilities:
    return ProviderCapabilities(
        chat=True,
        streaming=True,
        toolCalling=role == ModelRole.CHAT and provider != ProviderType.OLLAMA,
        vision=role == ModelRole.VISION,
        jsonOutput=provider in {ProviderType.OPENAI, ProviderType.AZURE_OPENAI},
    )
