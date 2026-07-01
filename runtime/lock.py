"""Process-wide desktop execution lock.

A threading lock is used because APScheduler may run Agent loops in different
threads and event loops. Acquisition is cooperative so queued Runs can still be
cancelled before they obtain the interactive desktop.
"""
from __future__ import annotations

import asyncio
import threading
from contextlib import asynccontextmanager
from typing import AsyncIterator

from .controller import RunController


class DesktopExecutionLock:
    def __init__(self):
        self._lock = threading.Lock()
        self._owner_guard = threading.Lock()
        self._owner_run_id: str | None = None

    @property
    def owner_run_id(self) -> str | None:
        with self._owner_guard:
            return self._owner_run_id

    async def acquire(self, controller: RunController) -> None:
        while not self._lock.acquire(blocking=False):
            await controller.checkpoint()
            await asyncio.sleep(0.1)
        with self._owner_guard:
            self._owner_run_id = controller.state.id

    def release(self, run_id: str) -> None:
        with self._owner_guard:
            if self._owner_run_id != run_id:
                raise RuntimeError(
                    f"Run {run_id} cannot release desktop owned by {self._owner_run_id}"
                )
            self._owner_run_id = None
        self._lock.release()

    @asynccontextmanager
    async def hold(self, controller: RunController) -> AsyncIterator[None]:
        await self.acquire(controller)
        try:
            yield
        finally:
            self.release(controller.state.id)


desktop_execution_lock = DesktopExecutionLock()
