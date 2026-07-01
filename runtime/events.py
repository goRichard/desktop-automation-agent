"""Async event bus used by Runtime and future WebSocket consumers."""
from __future__ import annotations

import asyncio
import inspect
from collections import deque
from collections.abc import Awaitable, Callable
from typing import Optional

from .models import RunEvent


EventSubscriber = Callable[[RunEvent], Optional[Awaitable[None]]]


class EventBus:
    def __init__(self, history_limit: int = 1000):
        self._subscribers: set[EventSubscriber] = set()
        self._history: deque[RunEvent] = deque(maxlen=history_limit)

    def subscribe(self, subscriber: EventSubscriber) -> Callable[[], None]:
        self._subscribers.add(subscriber)

        def unsubscribe() -> None:
            self._subscribers.discard(subscriber)

        return unsubscribe

    async def publish(self, event: RunEvent) -> None:
        self._history.append(event)
        awaitables = []
        for subscriber in tuple(self._subscribers):
            try:
                result = subscriber(event)
                if inspect.isawaitable(result):
                    awaitables.append(result)
            except Exception:
                # A UI subscriber must never break the automation Run.
                continue
        if awaitables:
            await asyncio.gather(*awaitables, return_exceptions=True)

    def history(self, run_id: Optional[str] = None) -> list[RunEvent]:
        if run_id is None:
            return list(self._history)
        return [event for event in self._history if event.run_id == run_id]
