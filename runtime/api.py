"""Local HTTP and WebSocket API for the Electron application."""
from __future__ import annotations

import asyncio
import hmac
import os
import secrets
from contextlib import asynccontextmanager
from typing import Literal, Optional

from fastapi import Depends, FastAPI, Header, HTTPException, Query, WebSocket
from fastapi import WebSocketDisconnect, status
from pydantic import BaseModel, ConfigDict, Field, SecretStr

from config import configure_secret_resolver, get_settings, reload_settings
from config.model_provider import ModelProviderConfig, ModelRole, default_capabilities
from config.service import ModelConfigurationService
from credentials import (
    CredentialStoreUnavailable,
    SecretStore,
    get_default_secret_store,
)
from llm import get_llm_client, reset_llm_client
from memory import init_db
from scheduler import shutdown_scheduler, start_scheduler
from skills import list_skills, load_skills
from skills.repository import SkillConflictError, SkillNotFoundError, SkillRepository
from skills.schema import SkillDocument

from .lock import desktop_execution_lock
from .manager import RuntimeConfigurationBusy, RuntimeManager


class CreateRunRequest(BaseModel):
    user_input: str = Field(min_length=1)
    session_id: Optional[str] = None
    confirmed_plan: Optional[str] = None


class CancelRunRequest(BaseModel):
    reason: str = "Cancelled by user"


class CredentialRequest(BaseModel):
    secret: SecretStr = Field(min_length=1)


class CertificateImportRequest(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    source_path: str = Field(alias="sourcePath", min_length=1)
    display_name: str = Field(default="internal-ca", alias="displayName")


def create_app(
    token: Optional[str] = None,
    credential_store: Optional[SecretStore] = None,
) -> FastAPI:
    runtime_token = token or os.environ.get("FLOWPILOT_RUNTIME_TOKEN") or secrets.token_urlsafe(32)
    manager = RuntimeManager()
    skill_repository = SkillRepository()
    secret_store = credential_store or get_default_secret_store()
    configure_secret_resolver(secret_store.get)
    settings = reload_settings()
    model_configurations = ModelConfigurationService(settings.config_path)
    model_settings_lock = asyncio.Lock()

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        settings = get_settings()
        init_db(settings.memory_db)
        load_skills(settings.skills_dir)
        skill_repository.import_definitions(list_skills())
        import tools  # noqa: F401
        from tools import scheduler_tool  # noqa: F401

        start_scheduler()
        try:
            yield
        finally:
            await manager.shutdown()
            await reset_llm_client()
            configure_secret_resolver(None)
            shutdown_scheduler()

    app = FastAPI(
        title="SEWC FlowPilot Runtime",
        version="0.1.0",
        lifespan=lifespan,
    )
    app.state.runtime_token = runtime_token
    app.state.runtime_manager = manager
    app.state.skill_repository = skill_repository
    app.state.credential_store = secret_store

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
            "skills": ["create", "edit_draft", "validate", "publish", "deprecate"],
            "models": [
                "inspect",
                "configure",
                "manage_credential",
                "import_ca_bundle",
                "health_check",
            ],
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

    @app.get("/models", dependencies=[Depends(require_token)])
    async def model_providers() -> dict:
        settings = get_settings()
        return {
            "chat": settings.chat_model.public_dict(),
            "vision": settings.vision_model.public_dict(),
        }

    @app.put("/models/{role}", dependencies=[Depends(require_token)])
    async def update_model_provider(role: ModelRole, config: ModelProviderConfig) -> dict:
        if "capabilities" not in config.model_fields_set:
            config.capabilities = default_capabilities(role, config.provider)
        async with model_settings_lock:
            try:
                async with manager.configuration_change():
                    await asyncio.to_thread(model_configurations.save_model, role, config)
                    await reset_llm_client()
                    refreshed = reload_settings()
                    selected = (
                        refreshed.chat_model
                        if role == ModelRole.CHAT
                        else refreshed.vision_model
                    )
                    return selected.public_dict()
            except RuntimeConfigurationBusy as error:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT, detail=str(error)
                ) from error

    @app.put("/models/{role}/credential", dependencies=[Depends(require_token)])
    async def update_model_credential(role: ModelRole, payload: CredentialRequest) -> dict:
        async with model_settings_lock:
            try:
                async with manager.configuration_change():
                    settings = get_settings()
                    config = (
                        settings.chat_model
                        if role == ModelRole.CHAT
                        else settings.vision_model
                    )
                    if not config.api_key_secret:
                        raise HTTPException(
                            status_code=status.HTTP_409_CONFLICT,
                            detail="Model configuration does not declare apiKeySecret",
                        )
                    try:
                        await asyncio.to_thread(
                            secret_store.set,
                            config.api_key_secret,
                            payload.secret.get_secret_value(),
                        )
                    except CredentialStoreUnavailable as error:
                        raise HTTPException(
                            status_code=status.HTTP_501_NOT_IMPLEMENTED, detail=str(error)
                        ) from error
                    await reset_llm_client()
                    refreshed = reload_settings()
                    selected = (
                        refreshed.chat_model
                        if role == ModelRole.CHAT
                        else refreshed.vision_model
                    )
                    return {
                        "role": role.value,
                        "credentialConfigured": bool(selected.resolve_api_key()),
                    }
            except RuntimeConfigurationBusy as error:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT, detail=str(error)
                ) from error

    @app.delete("/models/{role}/credential", dependencies=[Depends(require_token)])
    async def delete_model_credential(role: ModelRole) -> dict:
        async with model_settings_lock:
            try:
                async with manager.configuration_change():
                    settings = get_settings()
                    config = (
                        settings.chat_model
                        if role == ModelRole.CHAT
                        else settings.vision_model
                    )
                    if not config.api_key_secret:
                        raise HTTPException(
                            status_code=status.HTTP_409_CONFLICT,
                            detail="Model configuration does not declare apiKeySecret",
                        )
                    try:
                        deleted = await asyncio.to_thread(
                            secret_store.delete, config.api_key_secret
                        )
                    except CredentialStoreUnavailable as error:
                        raise HTTPException(
                            status_code=status.HTTP_501_NOT_IMPLEMENTED, detail=str(error)
                        ) from error
                    await reset_llm_client()
                    reload_settings()
                    return {
                        "role": role.value,
                        "credentialConfigured": False,
                        "deleted": deleted,
                    }
            except RuntimeConfigurationBusy as error:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT, detail=str(error)
                ) from error

    @app.post(
        "/certificates/import",
        status_code=status.HTTP_201_CREATED,
        dependencies=[Depends(require_token)],
    )
    async def import_certificate(payload: CertificateImportRequest) -> dict:
        try:
            return await asyncio.to_thread(
                model_configurations.import_ca_bundle,
                payload.source_path,
                payload.display_name,
            )
        except ValueError as error:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(error)
            ) from error

    @app.post("/models/{role}/health", dependencies=[Depends(require_token)])
    async def model_health(
        role: ModelRole,
        probe: Literal["configuration", "models", "request", "tool_calling", "vision"] = "models",
    ) -> dict:
        async with model_settings_lock:
            result = await get_llm_client().health_check(role, probe)
        if result["status"] == "unhealthy":
            raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=result)
        return result

    @app.get("/skills", dependencies=[Depends(require_token)])
    async def list_versioned_skills() -> list[dict]:
        return skill_repository.list()

    @app.post(
        "/skills",
        status_code=status.HTTP_201_CREATED,
        dependencies=[Depends(require_token)],
    )
    async def create_skill(document: SkillDocument) -> dict:
        return _skill_operation(skill_repository.create, document)

    @app.get("/skills/{skill_id}", dependencies=[Depends(require_token)])
    async def get_versioned_skill(skill_id: str) -> dict:
        return _skill_operation(skill_repository.get, skill_id)

    @app.get(
        "/skills/{skill_id}/versions/{version}",
        dependencies=[Depends(require_token)],
    )
    async def get_skill_version(skill_id: str, version: str) -> dict:
        return _skill_operation(skill_repository.get_version, skill_id, version)

    @app.put(
        "/skills/{skill_id}/versions/{version}",
        dependencies=[Depends(require_token)],
    )
    async def update_skill(skill_id: str, version: str, document: SkillDocument) -> dict:
        return _skill_operation(skill_repository.update_draft, skill_id, version, document)

    @app.post(
        "/skills/{skill_id}/versions/{version}/validate",
        dependencies=[Depends(require_token)],
    )
    async def validate_skill(skill_id: str, version: str) -> dict:
        return _skill_operation(skill_repository.validate, skill_id, version)

    @app.post(
        "/skills/{skill_id}/versions/{version}/publish",
        dependencies=[Depends(require_token)],
    )
    async def publish_skill(skill_id: str, version: str) -> dict:
        return _skill_operation(skill_repository.publish, skill_id, version)

    @app.post(
        "/skills/{skill_id}/versions/{version}/deprecate",
        dependencies=[Depends(require_token)],
    )
    async def deprecate_skill(skill_id: str, version: str) -> dict:
        return _skill_operation(skill_repository.deprecate, skill_id, version)

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


def _skill_operation(handler, *args) -> dict:
    try:
        return handler(*args)
    except SkillNotFoundError as error:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(error)) from error
    except SkillConflictError as error:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(error)) from error


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
