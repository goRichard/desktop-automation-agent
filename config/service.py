"""Safe persistence for model settings and managed CA bundles."""
from __future__ import annotations

import hashlib
import os
import re
import shutil
import threading
from pathlib import Path
from typing import Any

import yaml

from .model_provider import ModelProviderConfig, ModelRole


class ModelConfigurationService:
    def __init__(self, config_path: Path):
        self.config_path = Path(config_path).resolve()
        self._write_lock = threading.Lock()

    def save_model(self, role: ModelRole, config: ModelProviderConfig) -> None:
        value = config.model_dump(mode="json", by_alias=True, exclude_none=True)
        with self._write_lock:
            document = self._read()
            if "models" in document:
                models = document.setdefault("models", {})
            else:
                active_profile = document.get("active_profile", "local")
                profile = document.setdefault("profiles", {}).setdefault(active_profile, {})
                models = profile.setdefault("models", {})
                profile.pop("llm" if role == ModelRole.CHAT else "vision", None)
            models[role.value] = value
            self._write(document)

    def import_ca_bundle(self, source_path: Path, display_name: str = "internal-ca") -> dict[str, Any]:
        source = Path(source_path).expanduser().resolve()
        if not source.is_file():
            raise ValueError(f"Certificate file not found: {source}")
        if source.stat().st_size > 2 * 1024 * 1024:
            raise ValueError("CA bundle exceeds 2 MiB")
        content = source.read_bytes()
        upper = content.upper()
        if b"PRIVATE KEY" in upper:
            raise ValueError("Private keys cannot be imported as a CA bundle")
        if b"BEGIN CERTIFICATE" not in upper:
            raise ValueError("CA bundle must contain PEM certificates")

        fingerprint = hashlib.sha256(content).hexdigest()
        safe_name = re.sub(r"[^A-Za-z0-9._-]+", "-", display_name).strip("-._")
        if not safe_name:
            safe_name = "internal-ca"
        destination_dir = self.config_path.parent / "data" / "certificates"
        destination_dir.mkdir(parents=True, exist_ok=True)
        destination = destination_dir / f"{safe_name}-{fingerprint[:12]}.pem"
        if not destination.exists():
            shutil.copyfile(source, destination)
        return {
            "caBundle": str(destination.resolve()),
            "fingerprint": fingerprint,
            "size": len(content),
        }

    def _read(self) -> dict[str, Any]:
        if not self.config_path.is_file():
            raise ValueError(f"Configuration file not found: {self.config_path}")
        value = yaml.safe_load(self.config_path.read_text(encoding="utf-8")) or {}
        if not isinstance(value, dict):
            raise ValueError("Configuration root must be an object")
        return value

    def _write(self, value: dict[str, Any]) -> None:
        temporary = self.config_path.with_suffix(f"{self.config_path.suffix}.tmp")
        rendered = yaml.safe_dump(value, allow_unicode=True, sort_keys=False)
        temporary.write_text(rendered, encoding="utf-8")
        os.replace(temporary, self.config_path)
