"""
LLM facade：统一 OpenAI、OpenAI-compatible、Ollama 和 Azure OpenAI Provider
- 对话模型（支持 function calling）
- 视觉模型（图像输入）
"""
from __future__ import annotations

import base64
import json
from pathlib import Path
from typing import Any, AsyncGenerator, Optional

from openai.types.chat import ChatCompletion

from config import get_settings
from config.model_provider import ModelRole, ProviderType

from .providers import ModelProvider, create_provider
from .usage import TokenUsage, report_token_usage


class ToolCall:
    """解析后的工具调用"""

    def __init__(self, id: str, name: str, arguments: dict[str, Any]):
        self.id = id
        self.name = name
        self.arguments = arguments

    def __repr__(self) -> str:
        return f"ToolCall(id={self.id!r}, name={self.name!r})"


class LLMResponse:
    """统一的 LLM 响应对象"""

    def __init__(
        self,
        content: Optional[str],
        tool_calls: list[ToolCall],
        finish_reason: str,
        usage: Optional[TokenUsage] = None,
    ):
        self.content = content
        self.tool_calls = tool_calls
        self.finish_reason = finish_reason
        self.usage = usage or TokenUsage()

    @property
    def has_tool_calls(self) -> bool:
        return len(self.tool_calls) > 0


class ProviderCapabilityError(RuntimeError):
    pass


class VisionUnavailableError(ProviderCapabilityError):
    """The configured Vision provider cannot accept image input at runtime."""


def _is_unsupported_image_error(error: Exception) -> bool:
    """Recognize common OpenAI-compatible errors for text-only deployments."""
    message = str(error).casefold()
    markers = (
        "at most 0 images",
        "image input is not supported",
        "image inputs are not supported",
        "does not support image input",
        "doesn't support image input",
        "vision is not supported",
        "multimodal input is not supported",
    )
    return any(marker in message for marker in markers)


class LLMClient:
    """
    多后端 LLM 客户端，自动适配：
    - OpenAI 标准 API
    - OpenAI-compatible 内部网关和 vLLM
    - Ollama OpenAI-compatible endpoint
    - Azure OpenAI

    所有后端统一通过 chat() / chat_stream() / vision() / vision_for_coords() 调用。
    """

    def __init__(
        self,
        chat_provider: Optional[ModelProvider] = None,
        vision_provider: Optional[ModelProvider] = None,
    ):
        self._settings = get_settings()
        self.chat_provider = chat_provider or create_provider(self._settings.chat_model)
        self.vision_provider = vision_provider or create_provider(self._settings.vision_model)
        self._vision_unavailable_reason: Optional[str] = None

    async def chat(
        self,
        messages: list[dict[str, Any]],
        tools: Optional[list[dict[str, Any]]] = None,
    ) -> LLMResponse:
        """非流式对话，支持 function calling"""
        cfg = self.chat_provider.config
        if tools and not cfg.capabilities.tool_calling:
            raise ProviderCapabilityError(
                f"Provider {cfg.provider.value}/{cfg.model} does not declare toolCalling support"
            )

        kwargs: dict[str, Any] = {
            "model": cfg.model,
            "messages": messages,
            "temperature": cfg.temperature,
        }
        # GPT-5.2+ (Azure 新模型) 要求用 max_completion_tokens，旧模型用 max_tokens
        _max = cfg.max_tokens
        if cfg.provider == ProviderType.AZURE_OPENAI:
            kwargs["max_completion_tokens"] = _max
        else:
            kwargs["max_tokens"] = _max

        if tools:
            kwargs["tools"] = tools
            kwargs["tool_choice"] = "auto"

        response: ChatCompletion = await self.chat_provider.complete(**kwargs)
        parsed = self._parse_response(response)
        await report_token_usage(parsed.usage)
        return parsed

    async def chat_stream(
        self,
        messages: list[dict[str, Any]],
        tools: Optional[list[dict[str, Any]]] = None,
    ) -> AsyncGenerator[str, None]:
        """流式对话，yield 文本 token（最终回复阶段使用）"""
        cfg = self.chat_provider.config
        if tools and not cfg.capabilities.tool_calling:
            raise ProviderCapabilityError(
                f"Provider {cfg.provider.value}/{cfg.model} does not declare toolCalling support"
            )

        kwargs: dict[str, Any] = {
            "model": cfg.model,
            "messages": messages,
            "temperature": cfg.temperature,
        }
        _max = cfg.max_tokens
        if cfg.provider == ProviderType.AZURE_OPENAI:
            kwargs["max_completion_tokens"] = _max
        else:
            kwargs["max_tokens"] = _max
        if tools:
            kwargs["tools"] = tools
            kwargs["tool_choice"] = "auto"

        stream_usage: Optional[TokenUsage] = None
        async for chunk in self.chat_provider.stream(**kwargs):
            if getattr(chunk, "usage", None) is not None:
                stream_usage = TokenUsage.from_sdk(
                    chunk.usage,
                    role=ModelRole.CHAT.value,
                    model=cfg.model,
                )
            if not chunk.choices:  # Azure 最后一个 chunk 可能 choices 为空
                continue
            delta = chunk.choices[0].delta
            if delta.content:
                yield delta.content
        if stream_usage is not None:
            await report_token_usage(stream_usage)

    async def vision(self, image_path: str, prompt: str) -> str:
        """调用视觉模型分析图像"""
        self._require_vision()
        b64, mime, _, _ = self._prepare_image_for_vision(image_path)
        return await self._call_vision(b64, mime, prompt)

    async def vision_for_coords(self, image_path: str, prompt: str) -> tuple[str, float, float]:
        """调用视觉模型定位 UI 元素，返回 (模型响应, x缩放比, y缩放比)。"""
        self._require_vision()
        b64, mime, scale_x, scale_y = self._prepare_image_for_vision(image_path)
        text = await self._call_vision(b64, mime, prompt)
        return text, scale_x, scale_y

    def ensure_vision_available(self) -> None:
        """Fail before screenshot work when Vision is disabled or rejected at runtime."""
        self._require_vision()

    async def _call_vision(self, b64: str, mime: str, prompt: str) -> str:
        """底层调用视觉模型"""
        cfg = self.vision_provider.config

        try:
            response: ChatCompletion = await self.vision_provider.complete(
                model=cfg.model,
                messages=[
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "image_url",
                                "image_url": {"url": f"data:{mime};base64,{b64}"},
                            },
                            {"type": "text", "text": prompt},
                        ],
                    }
                ],
            )
        except Exception as error:
            if not _is_unsupported_image_error(error):
                raise
            reason = (
                f"Provider {cfg.provider.value}/{cfg.model} rejected image input; "
                "configure a multimodal model for the Vision role"
            )
            self._vision_unavailable_reason = reason
            raise VisionUnavailableError(reason) from error
        await report_token_usage(TokenUsage.from_sdk(
            response.usage,
            role=ModelRole.VISION.value,
            model=cfg.model,
        ))
        return response.choices[0].message.content or ""

    def _require_vision(self) -> None:
        cfg = self.vision_provider.config
        if not cfg.capabilities.vision:
            raise ProviderCapabilityError(
                f"Provider {cfg.provider.value}/{cfg.model} does not declare vision support"
            )
        reason = getattr(self, "_vision_unavailable_reason", None)
        if reason:
            raise VisionUnavailableError(reason)

    def public_config(self) -> dict[str, dict]:
        return {
            ModelRole.CHAT.value: self.chat_provider.config.public_dict(),
            ModelRole.VISION.value: self.vision_provider.config.public_dict(),
        }

    async def health_check(self, role: ModelRole, probe: str = "models") -> dict[str, Any]:
        provider = self.chat_provider if role == ModelRole.CHAT else self.vision_provider
        return await provider.health_check(probe)

    async def close(self) -> None:
        await self.chat_provider.close()
        if self.vision_provider is not self.chat_provider:
            await self.vision_provider.close()

    def _parse_response(self, response: ChatCompletion) -> LLMResponse:
        """解析 OpenAI ChatCompletion 响应为统一格式"""
        choice = response.choices[0]
        message = choice.message
        finish_reason = choice.finish_reason or "stop"

        tool_calls: list[ToolCall] = []
        if message.tool_calls:
            for tc in message.tool_calls:
                try:
                    arguments = json.loads(tc.function.arguments or "{}")
                except json.JSONDecodeError:
                    arguments = {}
                tool_calls.append(ToolCall(
                    id=tc.id,
                    name=tc.function.name,
                    arguments=arguments,
                ))

        if tool_calls and finish_reason != "tool_calls":
            finish_reason = "tool_calls"

        return LLMResponse(
            content=message.content,
            tool_calls=tool_calls,
            finish_reason=finish_reason,
            usage=TokenUsage.from_sdk(
                response.usage,
                role=ModelRole.CHAT.value,
                model=self.chat_provider.config.model,
            ),
        )

    @staticmethod
    def _prepare_image_for_vision(image_path: str, max_long_side: int = 1024) -> tuple[str, str, float, float]:
        """读取图片并自动压缩。返回 (base64_str, mime_type, scale_x, scale_y)。"""
        from PIL import Image
        import io

        img = Image.open(image_path)
        orig_w, orig_h = img.size

        # 如果图片已经足够小（< 512KB），直接用原始格式
        raw_size = Path(image_path).stat().st_size
        if raw_size < 512 * 1024 and max(img.size) <= max_long_side:
            image_bytes = Path(image_path).read_bytes()
            b64 = base64.b64encode(image_bytes).decode()
            suffix = Path(image_path).suffix.lower()
            mime = {
                ".png": "image/png",
                ".jpg": "image/jpeg",
                ".jpeg": "image/jpeg",
                ".webp": "image/webp",
            }.get(suffix, "image/png")
            return b64, mime, 1.0, 1.0

        # 需要缩放：等比缩放到最长边 max_long_side
        new_w, new_h = orig_w, orig_h
        if max(orig_w, orig_h) > max_long_side:
            scale = max_long_side / max(orig_w, orig_h)
            new_w = int(orig_w * scale)
            new_h = int(orig_h * scale)
            img = img.resize((new_w, new_h), Image.LANCZOS)

        # 转为 RGB 并编码为 JPEG
        if img.mode in ("RGBA", "P"):
            img = img.convert("RGB")

        buffer = io.BytesIO()
        img.save(buffer, format="JPEG", quality=85)
        b64 = base64.b64encode(buffer.getvalue()).decode()

        scale_x = orig_w / new_w
        scale_y = orig_h / new_h

        return b64, "image/jpeg", scale_x, scale_y


# 全局单例
_client: Optional[LLMClient] = None


def get_llm_client() -> LLMClient:
    global _client
    if _client is None:
        _client = LLMClient()
    return _client


async def reset_llm_client() -> None:
    global _client
    if _client is not None:
        await _client.close()
    _client = None
