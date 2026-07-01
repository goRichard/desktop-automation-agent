"""Async persistence adapter for Runtime state."""
from __future__ import annotations

import asyncio

from .models import RunEvent, RunState, StepRunState


class RuntimePersistence:
    """Persist Runtime objects without blocking the Agent event loop."""

    async def save_run(self, state: RunState) -> None:
        from memory import upsert_runtime_run

        await asyncio.to_thread(upsert_runtime_run, state.to_dict())

    async def save_step(self, step: StepRunState) -> None:
        from memory import upsert_runtime_step

        await asyncio.to_thread(upsert_runtime_step, step.to_dict())

    async def save_event(self, event: RunEvent) -> None:
        from memory import save_runtime_event

        await asyncio.to_thread(save_runtime_event, event.to_dict())


_persistence = RuntimePersistence()


def get_runtime_persistence() -> RuntimePersistence:
    return _persistence
