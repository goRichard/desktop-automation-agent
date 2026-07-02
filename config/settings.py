"""
配置管理：合并 config.yaml（模型/服务配置）和 .env（密钥）
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Callable, Optional

import yaml
from pydantic import PrivateAttr
from pydantic_settings import BaseSettings, SettingsConfigDict

from .model_provider import (
    ModelProviderConfig,
    ModelRole,
    ProviderType,
    default_capabilities,
)


class Settings(BaseSettings):
    """
    统一配置对象，从 config.yaml + .env 合并加载。
    密钥通过 .env 提供，模型配置通过 config.yaml 提供。
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="allow",
    )

    # .env 中的密钥字段（按需读取）
    LLM_API_KEY: str = ""
    VISION_API_KEY: str = ""
    OPENAI_API_KEY: str = ""
    ANTHROPIC_API_KEY: str = ""
    AZURE_OPENAI_API_KEY: str = ""

    # config.yaml 内容（初始化后填充）
    _raw: dict[str, Any] = PrivateAttr(default_factory=dict)
    _config_path: Path = PrivateAttr(default=Path("config.yaml"))

    def model_post_init(self, __context: Any) -> None:
        """加载 config.yaml"""
        configured_path = os.environ.get("DESKTOP_AGENT_CONFIG", "config.yaml")
        self._config_path = Path(configured_path).expanduser().resolve()
        if self._config_path.exists():
            with open(self._config_path, encoding="utf-8") as f:
                self._raw = yaml.safe_load(f) or {}

    # ──────────────────────────────────────────────────
    # Profile 相关
    # ──────────────────────────────────────────────────

    @property
    def active_profile(self) -> str:
        return self._raw.get("active_profile", "local")

    @property
    def config_path(self) -> Path:
        return self._config_path

    @property
    def _current_profile(self) -> dict[str, Any]:
        profiles = self._raw.get("profiles", {})
        return profiles.get(self.active_profile, {})

    def _resolve_key(self, key_env_name: str) -> str:
        """从实例属性中查找对应 env 变量的值"""
        return os.environ.get(key_env_name, "") or getattr(self, key_env_name, "") or ""

    def _resolve_cert_path(self, cert_value: str) -> Optional[str]:
        """Resolve a CA path relative to config.yaml without silently downgrading TLS."""
        if not cert_value:
            return None

        expanded = os.path.expandvars(cert_value)
        if "${APP_DATA}" in expanded:
            app_data = os.environ.get("APPDATA", str(self._config_path.parent / "data"))
            expanded = expanded.replace("${APP_DATA}", app_data)
        cert_path = Path(expanded).expanduser()
        if cert_path.is_absolute():
            resolved = cert_path
        else:
            # 相对路径：相对于 config.yaml 所在目录（项目根）
            resolved = self._config_path.parent / cert_path

        return str(resolved)

    def _model_config(self, role: ModelRole) -> ModelProviderConfig:
        profile = self._current_profile
        models = self._raw.get("models") or profile.get("models") or {}
        legacy_key = "llm" if role == ModelRole.CHAT else "vision"
        raw = dict(models.get(role.value) or profile.get(legacy_key) or {})
        if not raw:
            raise ValueError(f"Missing model configuration for role: {role.value}")

        provider_value = raw.get("provider")
        if provider_value == "azure":
            raw["provider"] = ProviderType.AZURE_OPENAI.value
        elif not provider_value:
            endpoint = raw.get("azure_endpoint") or raw.get("azureEndpoint")
            base_url = raw.get("api_base") or raw.get("base_url") or raw.get("baseUrl") or ""
            if endpoint:
                raw["provider"] = ProviderType.AZURE_OPENAI.value
            elif "api.openai.com" in base_url:
                raw["provider"] = ProviderType.OPENAI.value
            else:
                raw["provider"] = ProviderType.OPENAI_COMPATIBLE.value

        cert = raw.pop("ssl_cert_path", "") or profile.get("ssl_cert_path", "")
        tls = dict(raw.get("tls") or {})
        ca_value = tls.get("caBundle") or tls.get("ca_bundle") or cert
        if ca_value:
            tls["caBundle"] = self._resolve_cert_path(str(ca_value))
        raw["tls"] = tls

        provider = ProviderType(raw["provider"])
        if "capabilities" not in raw:
            raw["capabilities"] = default_capabilities(role, provider).model_dump(
                mode="json", by_alias=True
            )
        config = ModelProviderConfig.model_validate(raw)
        if config.api_key_secret:
            config.set_resolved_api_key(_resolve_managed_secret(config.api_key_secret) or "")
        elif config.api_key_env:
            config.set_resolved_api_key(self._resolve_key(config.api_key_env))
        return config

    @property
    def chat_model(self) -> ModelProviderConfig:
        return self._model_config(ModelRole.CHAT)

    @property
    def vision_model(self) -> ModelProviderConfig:
        return self._model_config(ModelRole.VISION)

    @property
    def llm(self) -> dict[str, Any]:
        """Legacy dictionary view retained for CLI and evaluation compatibility."""
        return self._legacy_model_dict(self.chat_model)

    @property
    def vision(self) -> dict[str, Any]:
        """Legacy dictionary view retained for existing vision tools."""
        return self._legacy_model_dict(self.vision_model)

    @staticmethod
    def _legacy_model_dict(config: ModelProviderConfig) -> dict[str, Any]:
        return {
            "provider": config.provider.value,
            "model": config.model,
            "api_base": config.base_url,
            "azure_endpoint": config.azure_endpoint or "",
            "api_version": config.api_version or "",
            "api_key": config.resolve_api_key(),
            "ssl_cert_path": str(config.tls.ca_bundle) if config.tls.ca_bundle else None,
            "temperature": config.temperature,
            "max_tokens": config.max_tokens,
        }

    # ──────────────────────────────────────────────────
    # Agent 配置
    # ──────────────────────────────────────────────────

    @property
    def agent(self) -> dict[str, Any]:
        return self._raw.get("agent", {})

    @property
    def max_iterations(self) -> int:
        return self.agent.get("max_iterations", 100)

    @property
    def max_history_messages(self) -> int:
        return self.agent.get("max_history_messages", 40)

    @property
    def skills_dir(self) -> Path:
        return self._resolve_data_path(self.agent.get("skills_dir", "./skills/user_skills"))

    @property
    def memory_db(self) -> Path:
        return self._resolve_data_path(self.agent.get("memory_db", "./data/agent.db"))

    @property
    def evidence_dir(self) -> Path:
        return self._resolve_data_path(
            self.agent.get("evidence_dir", "./data/run_evidence")
        )

    @property
    def verification(self) -> dict[str, Any]:
        return dict(self.agent.get("verification") or {
            "mode": "checkpoint",
            "checkpointInterval": 3,
            "verifyWindowTransitions": True,
            "verifyFinalStep": True,
            "verifyHighRiskActions": True,
        })

    @property
    def browser(self) -> dict[str, Any]:
        return dict(self._raw.get("browser", {"channel": "msedge"}))

    def _resolve_data_path(self, value: str) -> Path:
        path = Path(value).expanduser()
        return path if path.is_absolute() else self._config_path.parent / path

    @property
    def system_prompt(self) -> str:
        return self.agent.get("system_prompt", "你是一个强大的 Windows 桌面自动化 Agent。")

    def summary(self) -> str:
        """返回当前配置摘要（用于启动时展示）"""
        chat = self.chat_model
        vision = self.vision_model
        return (
            f"Profile: {self.active_profile} | "
            f"Chat: {chat.provider.value}/{chat.model} | "
            f"Vision: {vision.provider.value}/{vision.model} | "
            f"Verification: {self.verification.get('mode', 'checkpoint')}"
        )


# 全局单例
_settings: Settings | None = None
_secret_resolver: Optional[Callable[[str], Optional[str]]] = None


def get_settings() -> Settings:
    global _settings
    if _settings is None:
        _settings = Settings()
    return _settings


def reload_settings() -> Settings:
    global _settings
    _settings = Settings()
    return _settings


def configure_secret_resolver(
    resolver: Optional[Callable[[str], Optional[str]]],
) -> None:
    global _secret_resolver
    _secret_resolver = resolver


def _resolve_managed_secret(secret_id: str) -> Optional[str]:
    if _secret_resolver is not None:
        return _secret_resolver(secret_id)
    from credentials import get_default_secret_store

    return get_default_secret_store().get(secret_id)
"""
配置管理：合并 config.yaml（模型/服务配置）和 .env（密钥）
"""
