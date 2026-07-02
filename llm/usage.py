"""Model token usage normalization and per-Run recording context."""
from __future__ import annotations

import inspect
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import asdict, dataclass
from typing import Any, Callable, Iterator, Optional


@dataclass(frozen=True)
class TokenUsage:
    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0
    cached_input_tokens: int = 0
    reported: bool = False
    role: str = "chat"
    model: str = ""

    @classmethod
    def from_sdk(cls, usage: Any, *, role: str, model: str) -> "TokenUsage":
        if usage is None:
            return cls(role=role, model=model)

        input_tokens = _integer_attribute(usage, "prompt_tokens", "input_tokens")
        output_tokens = _integer_attribute(usage, "completion_tokens", "output_tokens")
        total_tokens = _integer_attribute(usage, "total_tokens")
        if total_tokens == 0 and (input_tokens or output_tokens):
            total_tokens = input_tokens + output_tokens

        details = (
            getattr(usage, "prompt_tokens_details", None)
            or getattr(usage, "input_tokens_details", None)
        )
        cached_tokens = _integer_attribute(details, "cached_tokens") if details else 0
        return cls(
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            total_tokens=total_tokens,
            cached_input_tokens=cached_tokens,
            reported=True,
            role=role,
            model=model,
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


UsageRecorder = Callable[[TokenUsage], Any]
_usage_recorder: ContextVar[Optional[UsageRecorder]] = ContextVar(
    "flowpilot_usage_recorder",
    default=None,
)


@contextmanager
def capture_token_usage(recorder: UsageRecorder) -> Iterator[None]:
    token = _usage_recorder.set(recorder)
    try:
        yield
    finally:
        _usage_recorder.reset(token)


async def report_token_usage(usage: TokenUsage) -> None:
    recorder = _usage_recorder.get()
    if recorder is None:
        return
    result = recorder(usage)
    if inspect.isawaitable(result):
        await result


def _integer_attribute(value: Any, *names: str) -> int:
    for name in names:
        result = getattr(value, name, None)
        if result is not None:
            try:
                return max(0, int(result))
            except (TypeError, ValueError):
                return 0
    return 0
