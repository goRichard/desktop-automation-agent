"""Runtime primitives shared by the CLI, scheduler, and future Electron API."""

from .controller import RunCancelled, RunController
from .events import EventBus
from .lock import desktop_execution_lock
from .manager import RuntimeManager
from .models import RunEvent, RunState, RunStatus, StepRunState, StepStatus
from .persistence import RuntimePersistence, get_runtime_persistence

__all__ = [
    "EventBus",
    "RunCancelled",
    "RunController",
    "RunEvent",
    "RunState",
    "RunStatus",
    "RuntimePersistence",
    "RuntimeManager",
    "StepRunState",
    "StepStatus",
    "desktop_execution_lock",
    "get_runtime_persistence",
]
