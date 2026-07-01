"""Model provider implementations behind one OpenAI-shaped runtime interface."""
from __future__ import annotations

import time
from abc import ABC, abstractmethod
from typing import Any, AsyncIterator, Optional

import httpx
from openai import AsyncAzureOpenAI, AsyncOpenAI

from config.model_provider import ModelProviderConfig, ProviderType


class ModelProvider(ABC):
    def __init__(self, config: ModelProviderConfig):
        self.config = config
        self._client: Any = None
        self._http_client: Optional[httpx.AsyncClient] = None

    @property
    @abstractmethod
    def kind(self) -> ProviderType:
        raise NotImplementedError

    @abstractmethod
    def _create_sdk_client(self) -> Any:
        raise NotImplementedError

    @property
    def client(self) -> Any:
        if self._client is None:
            self._client = self._create_sdk_client()
        return self._client

    def _new_http_client(self) -> httpx.AsyncClient:
        self._http_client = httpx.AsyncClient(
            verify=self.config.tls.verify_value,
            timeout=httpx.Timeout(30, read=120),
        )
        return self._http_client

    async def complete(self, **kwargs: Any) -> Any:
        return await self.client.chat.completions.create(**kwargs)

    async def stream(self, **kwargs: Any) -> AsyncIterator[Any]:
        result = await self.client.chat.completions.create(stream=True, **kwargs)
        async for chunk in result:
            yield chunk

    async def health_check(self, probe: str = "models") -> dict[str, Any]:
        started = time.perf_counter()
        checks = {"configuration": "passed", "tls": "not_applicable"}
        if self.config.base_url or self.config.azure_endpoint:
            checks["tls"] = "enabled" if self.config.tls.verify else "disabled"
        try:
            if probe == "configuration":
                pass
            elif probe == "models" and self.kind != ProviderType.AZURE_OPENAI:
                await self.client.models.list()
                checks["modelEndpoint"] = "passed"
            elif probe == "tool_calling":
                response = await self.complete(
                    model=self.config.model,
                    messages=[{"role": "user", "content": "Call the report_status tool."}],
                    tools=[{
                        "type": "function",
                        "function": {
                            "name": "report_status",
                            "description": "Report a health-check status",
                            "parameters": {"type": "object", "properties": {}},
                        },
                    }],
                    tool_choice="auto",
                    **self._health_token_limit(),
                )
                has_tool_call = bool(response.choices[0].message.tool_calls)
                checks["toolCalling"] = "passed" if has_tool_call else "failed"
                if not has_tool_call:
                    raise RuntimeError("Model returned no tool call")
            elif probe == "vision":
                await self.complete(
                    model=self.config.model,
                    messages=[{
                        "role": "user",
                        "content": [
                            {
                                "type": "image_url",
                                "image_url": {
                                    "url": "data:image/png;base64,"
                                    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lE"
                                    "QVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
                                },
                            },
                            {"type": "text", "text": "Reply with OK."},
                        ],
                    }],
                    **self._health_token_limit(),
                )
                checks["vision"] = "passed"
            else:
                await self.complete(
                    model=self.config.model,
                    messages=[{"role": "user", "content": "Reply with OK."}],
                    temperature=0,
                    **self._health_token_limit(),
                )
                checks["minimalRequest"] = "passed"
            return {
                "status": "healthy",
                "provider": self.kind.value,
                "model": self.config.model,
                "latencyMs": round((time.perf_counter() - started) * 1000, 1),
                "checks": checks,
            }
        except Exception as error:
            return {
                "status": "unhealthy",
                "provider": self.kind.value,
                "model": self.config.model,
                "latencyMs": round((time.perf_counter() - started) * 1000, 1),
                "checks": checks,
                "error": _sanitize_error(error),
            }

    def _health_token_limit(self) -> dict[str, int]:
        key = "max_completion_tokens" if self.kind == ProviderType.AZURE_OPENAI else "max_tokens"
        return {key: 8}

    async def close(self) -> None:
        if self._client is not None:
            await self._client.close()
        elif self._http_client is not None:
            await self._http_client.aclose()
        self._client = None
        self._http_client = None


class OpenAIProvider(ModelProvider):
    @property
    def kind(self) -> ProviderType:
        return self.config.provider

    def _create_sdk_client(self) -> AsyncOpenAI:
        base_url = self.config.base_url
        if self.kind == ProviderType.OPENAI:
            base_url = base_url or "https://api.openai.com/v1"
        elif self.kind == ProviderType.OLLAMA:
            base_url = _ollama_openai_url(base_url or "http://127.0.0.1:11434")
        return AsyncOpenAI(
            base_url=base_url,
            api_key=self.config.resolve_api_key() or "not-needed",
            http_client=self._new_http_client(),
        )


class AzureOpenAIProvider(ModelProvider):
    @property
    def kind(self) -> ProviderType:
        return ProviderType.AZURE_OPENAI

    def _create_sdk_client(self) -> AsyncAzureOpenAI:
        return AsyncAzureOpenAI(
            azure_endpoint=self.config.azure_endpoint,
            api_version=self.config.api_version,
            api_key=self.config.resolve_api_key() or "not-needed",
            http_client=self._new_http_client(),
        )


def create_provider(config: ModelProviderConfig) -> ModelProvider:
    if config.provider == ProviderType.AZURE_OPENAI:
        return AzureOpenAIProvider(config)
    return OpenAIProvider(config)


def _ollama_openai_url(value: str) -> str:
    normalized = value.rstrip("/")
    return normalized if normalized.endswith("/v1") else f"{normalized}/v1"


def _sanitize_error(error: Exception) -> str:
    text = str(error).replace("\n", " ")
    for marker in ("api_key=", "api-key=", "authorization:"):
        position = text.lower().find(marker)
        if position >= 0:
            text = text[:position] + f"{marker}<redacted>"
    return f"{type(error).__name__}: {text[:500]}"
