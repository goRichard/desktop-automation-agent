"""Local HTTP and WebSocket API for the Electron application."""
from __future__ import annotations

import hmac
import os
import secrets
from contextlib import asynccontextmanager
from typing import Optional

from fastapi import Depends, FastAPI, Header, HTTPException, Query, WebSocket
from fastapi import WebSocketDisconnect, status
from pydantic import BaseModel, Field

from config import get_settings
from memory import init_db
from scheduler import shutdown_scheduler, start_scheduler
from skills import load_skills

from .lock import desktop_execution_lock
from .manager import RuntimeManager


class CreateRunRequest(BaseModel):
    user_input: str = Field(min_length=1)
    session_id: Optional[str] = None
    confirmed_plan: Optional[str] = None


class CancelRunRequest(BaseModel):
    reason: str = "Cancelled by user"


def create_app(token: Optional[str] = None) -> FastAPI:
    runtime_token = token or os.environ.get("FLOWPILOT_RUNTIME_TOKEN") or secrets.token_urlsafe(32)
    manager = RuntimeManager()

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        settings = get_settings()
        init_db(settings.memory_db)
        load_skills(settings.skills_dir)
        import tools  # noqa: F401
        from tools import scheduler_tool  # noqa: F401

        start_scheduler()
        try:
            yield
        finally:
            await manager.shutdown()
            shutdown_scheduler()

    app = FastAPI(
        title="SEWC FlowPilot Runtime",
        version="0.1.0",
        lifespan=lifespan,
    )
    app.state.runtime_token = runtime_token
    app.state.runtime_manager = manager

    async def require_token(
        x_runtime_token: Optional[str] = Header(default=None),
        authorization: Optional[str] = Header(default=None),
    ) -> None:
        supplied = x_runtime_token
        if authorization and authorization.lower().startswith("bearer "):
            supplied = authorization[7:].strip()
        if not supplied or not hmac.compare_digest(supplied, runtime_token):
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED)

    @app.get("/health")
    async def health() -> dict:
        return {"status": "ok", "service": "flowpilot-runtime"}

    @app.get("/runtime/capabilities", dependencies=[Depends(require_token)])
    async def capabilities() -> dict:
        return {
            "runs": ["start", "pause", "resume", "cancel", "history"],
            "events": ["history", "websocket"],
            "desktop": {"provider": "winpeekaboo", "maxConcurrentRuns": 1},
            "browser": {"provider": "playwright", "channel": "msedge"},
        }

    @app.get("/runtime/environment", dependencies=[Depends(require_token)])
    async def environment() -> dict:
        settings = get_settings()
        return {
            "profile": settings.active_profile,
            "database": str(settings.memory_db),
            "skillsDirectory": str(settings.skills_dir),
            "browser": settings.browser,
            "desktopLockOwner": desktop_execution_lock.owner_run_id,
        }

    @app.post(
        "/runs",
        status_code=status.HTTP_202_ACCEPTED,
        dependencies=[Depends(require_token)],
    )
    async def create_run(payload: CreateRunRequest) -> dict:
        try:
            return await manager.start_run(
                user_input=payload.user_input,
                session_id=payload.session_id,
                confirmed_plan=payload.confirmed_plan,
            )
        except LookupError as error:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(error))

    @app.get("/runs", dependencies=[Depends(require_token)])
    async def list_runs(limit: int = Query(default=50, ge=1, le=200)) -> list[dict]:
        return await manager.list_runs(limit)

    @app.get("/runs/{run_id}", dependencies=[Depends(require_token)])
    async def get_run(run_id: str) -> dict:
        value = await manager.get_run(run_id)
        if value is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
        return value

    @app.get("/runs/{run_id}/events", dependencies=[Depends(require_token)])
    async def get_run_events(
        run_id: str,
        after: int = Query(default=0, ge=0),
    ) -> list[dict]:
        if await manager.get_run(run_id) is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
        return await manager.list_events(run_id, after)

    @app.post("/runs/{run_id}/pause", dependencies=[Depends(require_token)])
    async def pause_run(run_id: str) -> dict:
        return await _control(manager.pause, run_id)

    @app.post("/runs/{run_id}/resume", dependencies=[Depends(require_token)])
    async def resume_run(run_id: str) -> dict:
        return await _control(manager.resume, run_id)

    @app.post("/runs/{run_id}/cancel", dependencies=[Depends(require_token)])
    async def cancel_run(run_id: str, payload: CancelRunRequest) -> dict:
        try:
            return await manager.cancel(run_id, payload.reason)
        except LookupError as error:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(error))
        except RuntimeError as error:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(error))

    @app.websocket("/events")
    async def events_socket(
        websocket: WebSocket,
        run_id: Optional[str] = None,
        after: int = 0,
        token: Optional[str] = None,
    ) -> None:
        header_token = websocket.headers.get("x-runtime-token")
        supplied = token or header_token
        if not supplied or not hmac.compare_digest(supplied, runtime_token):
            await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
            return

        await websocket.accept()
        queue = _event_queue(manager, run_id)
        unsubscribe = queue[1]
        event_queue = queue[0]
        last_sequence = after
        try:
            if run_id:
                for event in await manager.list_events(run_id, after):
                    last_sequence = max(last_sequence, event["sequence"])
                    await websocket.send_json(event)

            while True:
                event = await event_queue.get()
                if run_id and event.sequence <= last_sequence:
                    continue
                if run_id:
                    last_sequence = event.sequence
                await websocket.send_json(event.to_dict())
        except WebSocketDisconnect:
            pass
        finally:
            unsubscribe()

    return app


async def _control(handler, run_id: str) -> dict:
    try:
        return await handler(run_id)
    except LookupError as error:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(error))
    except RuntimeError as error:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(error))


def _event_queue(manager: RuntimeManager, run_id: Optional[str]):
    import asyncio

    queue: asyncio.Queue = asyncio.Queue(maxsize=1000)

    def receive(event) -> None:
        if run_id and event.run_id != run_id:
            return
        if queue.full():
            try:
                queue.get_nowait()
            except asyncio.QueueEmpty:
                pass
        queue.put_nowait(event)

    return queue, manager.events.subscribe(receive)


def main() -> None:
    import uvicorn

    configured_token = os.environ.get("FLOWPILOT_RUNTIME_TOKEN")
    app = create_app(configured_token)
    if configured_token is None:
        print(f"FLOWPILOT_RUNTIME_TOKEN={app.state.runtime_token}")
    port = int(os.environ.get("FLOWPILOT_RUNTIME_PORT", "8765"))
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="info")


if __name__ == "__main__":
    main()
