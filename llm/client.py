"""
LLM 客户端层：支持 OpenAI / Azure OpenAI / vLLM
- 对话模型（支持 function calling）
- 视觉模型（图像输入）
"""
from __future__ import annotations

import base64
import json
from pathlib import Path
from typing import Any, AsyncGenerator, Optional

import httpx
from openai import AsyncAzureOpenAI, AsyncOpenAI
from openai.types.chat import ChatCompletion

from config import get_settings


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

    def __init__(self, content: Optional[str], tool_calls: list[ToolCall], finish_reason: str):
        self.content = content
        self.tool_calls = tool_calls
        self.finish_reason = finish_reason

    @property
    def has_tool_calls(self) -> bool:
        return len(self.tool_calls) > 0


class LLMClient:
    """
    多后端 LLM 客户端，自动适配：
    - OpenAI 标准 API（AsyncOpenAI）
    - Azure OpenAI（AsyncAzureOpenAI，通过 azure_endpoint 自动检测）
    - vLLM 本地部署（AsyncOpenAI + 自定义 base_url）

    所有后端统一通过 chat() / chat_stream() / vision() / vision_for_coords() 调用。
    """

    def __init__(self):
        self._settings = get_settings()
        self._llm_client: Any = None
        self._vision_client: Any = None

    @staticmethod
    def _build_http_client(ssl_cert_path: Optional[str]) -> Optional["httpx.AsyncClient"]:
        """构建自定义 httpx.AsyncClient（带 SSL 证书验证）。"""
        if not ssl_cert_path:
            return None
        import httpx
        return httpx.AsyncClient(verify=ssl_cert_path, timeout=httpx.Timeout(120.0))

    @staticmethod
    def _build_timeout() -> "httpx.Timeout":
        """统一的 HTTP 超时配置：连接 30s，读取 120s。"""
        import httpx
        return httpx.Timeout(30.0, read=120.0)

    def _is_azure(self, cfg: dict[str, Any]) -> bool:
        return bool(cfg.get("azure_endpoint", ""))

    def _create_client(self, cfg: dict[str, Any]) -> Any:
        """根据配置创建 AsyncOpenAI 或 AsyncAzureOpenAI 客户端。"""
        http_client = self._build_http_client(cfg.get("ssl_cert_path"))

        if self._is_azure(cfg):
            kwargs: dict[str, Any] = {
                "azure_endpoint": cfg["azure_endpoint"],
                "api_version": cfg["api_version"],
                "api_key": cfg["api_key"] or "not-needed",
            }
            if http_client:
                kwargs["http_client"] = http_client
            else:
                kwargs["http_client"] = httpx.AsyncClient(timeout=self._build_timeout())
            return AsyncAzureOpenAI(**kwargs)
        else:
            kwargs: dict[str, Any] = {
                "base_url": cfg.get("api_base") or None,
                "api_key": cfg.get("api_key") or "not-needed",
            }
            if http_client:
                kwargs["http_client"] = http_client
            else:
                kwargs["http_client"] = httpx.AsyncClient(timeout=self._build_timeout())
            return AsyncOpenAI(**kwargs)

    def _get_llm_client(self):
        if self._llm_client is None:
            self._llm_client = self._create_client(self._settings.llm)
        return self._llm_client

    def _get_vision_client(self):
        if self._vision_client is None:
            self._vision_client = self._create_client(self._settings.vision)
        return self._vision_client

    async def chat(
        self,
        messages: list[dict[str, Any]],
        tools: Optional[list[dict[str, Any]]] = None,
    ) -> LLMResponse:
        """非流式对话，支持 function calling"""
        cfg = self._settings.llm
        client = self._get_llm_client()

        kwargs: dict[str, Any] = {
            "model": cfg["model"],
            "messages": messages,
            "temperature": cfg.get("temperature", 0.7),
        }
        # GPT-5.2+ (Azure 新模型) 要求用 max_completion_tokens，旧模型用 max_tokens
        _max = cfg.get("max_tokens", 4096)
        if self._is_azure(cfg):
            kwargs["max_completion_tokens"] = _max
        else:
            kwargs["max_tokens"] = _max

        if tools:
            kwargs["tools"] = tools
            kwargs["tool_choice"] = "auto"

        response: ChatCompletion = await client.chat.completions.create(**kwargs)
        return self._parse_response(response)

    async def chat_stream(
        self,
        messages: list[dict[str, Any]],
        tools: Optional[list[dict[str, Any]]] = None,
    ) -> AsyncGenerator[str, None]:
        """流式对话，yield 文本 token（最终回复阶段使用）"""
        cfg = self._settings.llm
        client = self._get_llm_client()

        kwargs: dict[str, Any] = {
            "model": cfg["model"],
            "messages": messages,
            "temperature": cfg.get("temperature", 0.7),
            "stream": True,
        }
        _max = cfg.get("max_tokens", 4096)
        if self._is_azure(cfg):
            kwargs["max_completion_tokens"] = _max
        else:
            kwargs["max_tokens"] = _max
        if tools:
            kwargs["tools"] = tools
            kwargs["tool_choice"] = "auto"

        stream = await client.chat.completions.create(**kwargs)
        async for chunk in stream:
            if not chunk.choices:  # Azure 最后一个 chunk 可能 choices 为空
                continue
            delta = chunk.choices[0].delta
            if delta.content:
                yield delta.content

    async def vision(self, image_path: str, prompt: str) -> str:
        """调用视觉模型分析图像"""
        b64, mime, _, _ = self._prepare_image_for_vision(image_path)
        return await self._call_vision(b64, mime, prompt)

    async def vision_for_coords(self, image_path: str, prompt: str) -> tuple[str, float, float]:
        """调用视觉模型定位 UI 元素，返回 (模型响应, x缩放比, y缩放比)。"""
        b64, mime, scale_x, scale_y = self._prepare_image_for_vision(image_path)
        text = await self._call_vision(b64, mime, prompt)
        return text, scale_x, scale_y

    async def _call_vision(self, b64: str, mime: str, prompt: str) -> str:
        """底层调用视觉模型"""
        cfg = self._settings.vision
        client = self._get_vision_client()

        response: ChatCompletion = await client.chat.completions.create(
            model=cfg["model"],
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
        return response.choices[0].message.content or ""

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
"""
LLM 客户端层：基于 OpenAI Python SDK，支持自定义 base_url 调用本地/云端服务
本地部署的 vLLM 和云端服务均满足 OpenAI API 格式，直接用 openai 库即可
"""
