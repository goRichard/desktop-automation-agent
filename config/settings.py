"""
配置管理：合并 config.yaml（模型/服务配置）和 .env（密钥）
"""
from __future__ import annotations

import os
import warnings
from pathlib import Path
from typing import Any, Optional

import yaml
from pydantic import PrivateAttr
from pydantic_settings import BaseSettings, SettingsConfigDict


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
    def _current_profile(self) -> dict[str, Any]:
        profiles = self._raw.get("profiles", {})
        return profiles.get(self.active_profile, {})

    def _resolve_key(self, key_env_name: str) -> str:
        """从实例属性中查找对应 env 变量的值"""
        return getattr(self, key_env_name, "") or ""

    def _resolve_cert_path(self, cert_value: str) -> Optional[str]:
        """解析证书路径：支持绝对路径和相对路径（相对于项目根目录）"""
        if not cert_value:
            return None

        cert_path = Path(cert_value)
        if cert_path.is_absolute():
            resolved = cert_path
        else:
            # 相对路径：相对于 config.yaml 所在目录（项目根）
            resolved = self._config_path.parent / cert_path

        if resolved.exists():
            return str(resolved)

        # 路径不存在，记录警告但不阻塞（回退到系统默认证书）
        warnings.warn(f"SSL cert path not found: {resolved}")
        return None

    @property
    def llm(self) -> dict[str, Any]:
        """当前 profile 的对话模型配置（含 api_key 和 ssl_cert_path 已解析）"""
        cfg = dict(self._current_profile.get("llm", {}))
        key_env = cfg.pop("api_key_env", "")
        cfg["api_key"] = self._resolve_key(key_env) if key_env else ""

        # SSL 证书：llm 级别 > profile 级别
        cert = cfg.pop("ssl_cert_path", "") or self._current_profile.get("ssl_cert_path", "")
        cfg["ssl_cert_path"] = self._resolve_cert_path(cert)

        # Azure: 保留 azure_endpoint 和 api_version 透传
        if "azure_endpoint" not in cfg:
            cfg["azure_endpoint"] = ""
        if "api_version" not in cfg:
            cfg["api_version"] = ""

        return cfg

    @property
    def vision(self) -> dict[str, Any]:
        """当前 profile 的视觉模型配置（含 api_key 和 ssl_cert_path 已解析）"""
        cfg = dict(self._current_profile.get("vision", {}))
        key_env = cfg.pop("api_key_env", "")
        cfg["api_key"] = self._resolve_key(key_env) if key_env else ""

        # SSL 证书：vision 级别 > profile 级别
        cert = cfg.pop("ssl_cert_path", "") or self._current_profile.get("ssl_cert_path", "")
        cfg["ssl_cert_path"] = self._resolve_cert_path(cert)

        # Azure: 保留 azure_endpoint 和 api_version 透传
        if "azure_endpoint" not in cfg:
            cfg["azure_endpoint"] = ""
        if "api_version" not in cfg:
            cfg["api_version"] = ""

        return cfg

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
    def skills_dir(self) -> Path:
        return self._resolve_data_path(self.agent.get("skills_dir", "./skills/user_skills"))

    @property
    def memory_db(self) -> Path:
        return self._resolve_data_path(self.agent.get("memory_db", "./data/agent.db"))

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
        provider_info = ""
        if self.llm.get("azure_endpoint"):
            provider_info = f" (Azure, deployment: {self.llm.get('model', 'N/A')})"
        return (
            f"Profile: {self.active_profile}{provider_info} | "
            f"LLM: {self.llm.get('model', 'N/A')} | "
            f"Vision: {self.vision.get('model', 'N/A')}"
        )


# 全局单例
_settings: Settings | None = None


def get_settings() -> Settings:
    global _settings
    if _settings is None:
        _settings = Settings()
    return _settings
"""
配置管理：合并 config.yaml（模型/服务配置）和 .env（密钥）
"""
