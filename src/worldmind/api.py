"""Single-process service, session capabilities and serialized authoritative mutations."""
from __future__ import annotations

import asyncio
import hmac
import os
import secrets
import time
from collections import deque
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import Field
from starlette.middleware.trustedhost import TrustedHostMiddleware

from .domain import Config, GOAL_LABELS, LABELS, PLACES, ROADS, StrictModel, VERSION, digest
from .engine import Command, Engine, Recording, compare, replay
from .models import Ledger, ModelService
from .providers import PROFILES, ProviderError, Settings


async def computation(fn, *args):
    """Cancellation must not release the writer lock while its worker is still mutating."""
    task = asyncio.create_task(asyncio.to_thread(fn, *args))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        await task
        raise


@dataclass
class Session:
    engine: Engine
    token: str = field(default_factory=lambda: secrets.token_urlsafe(32))
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    revision: int = 0
    epoch: int = 0
    touched: float = field(default_factory=time.monotonic)
    running: bool = False
    runner: asyncio.Task | None = None
    pending: asyncio.Task | None = None
    model_requests: int = 0
    receipts: dict = field(default_factory=dict)
    model_status: dict = field(default_factory=lambda: {"status": "idle", "usage": None})
    replay_result: dict | None = None
    recording: dict | None = None

    def view(self):
        return {**self.engine.view(), "revision": self.revision, "running": self.running,
                "model": self.model_status, "replay": self.replay_result,
                "mode": "verified_replay" if self.recording else "live_offline"}


class Mutation(StrictModel):
    command_id: str = Field(min_length=1, max_length=80, pattern=r"^[a-zA-Z0-9_-]+$")
    expected_revision: int = Field(ge=0)
    command: Command


class Control(StrictModel):
    operation: Literal["run", "pause", "reset"]


class ModelRequest(StrictModel):
    provider: str = Field(max_length=32)


class ImportRequest(StrictModel):
    recording: Recording


class Cursor(StrictModel):
    until: int = Field(ge=0, le=1500)


def create_app(*, ledger_path=None, model_service=None, model_access=None, settings_loader=None):
    path = ledger_path or os.getenv("WORLDMIND_LEDGER", "var/model-ledger.sqlite3")
    enabled = os.getenv("WORLDMIND_ENABLE_MODELS") == "1"
    access = model_access if model_access is not None else os.getenv("WORLDMIND_MODEL_ACCESS", "")
    if model_service is None:
        if enabled and (not Path(path).is_file() or len(access) < 24):
            raise RuntimeError("Model mode needs an existing persistent ledger and access token (24+ chars)")
        model_service = ModelService(Ledger(path, int(os.getenv("WORLDMIND_REQUEST_LIMIT", "100"))),
                                     enabled=enabled)
    settings_loader = settings_loader or Settings.from_env
    sessions: dict[str, Session] = {}
    creation_times = deque()
    cpu_slots = asyncio.Semaphore(4)

    @asynccontextmanager
    async def lifespan(app):
        yield
        tasks = []
        for s in sessions.values():
            s.running = False
            for task in (s.runner, s.pending):
                if task and not task.done():
                    task.cancel()
                    tasks.append(task)
        await asyncio.gather(*tasks, return_exceptions=True)

    app = FastAPI(title="Worldmind", version="0.1.0", lifespan=lifespan)
    app.state.sessions = sessions
    app.state.models = model_service
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=os.getenv(
        "WORLDMIND_HOSTS", "localhost,127.0.0.1,testserver").split(","))

    @app.middleware("http")
    async def limits(request: Request, call_next):
        origin = request.headers.get("origin")
        if origin and urlsplit(origin).netloc != request.headers.get("host"):
            return JSONResponse({"detail": "origin_not_allowed"}, status_code=403)
        chunks, size = [], 0
        async for chunk in request.stream():
            size += len(chunk)
            if size > 2_000_000:
                return JSONResponse({"detail": "request_too_large"}, status_code=413)
            chunks.append(chunk)
        request._body = b"".join(chunks)
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Cache-Control"] = "no-store"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
            "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'")
        return response

    @app.exception_handler(RequestValidationError)
    async def validation_error(request, exc):
        # FastAPI's default includes untrusted input values; return only locations/types.
        return JSONResponse({"detail": [{"loc": e["loc"], "type": e["type"]} for e in exc.errors()]},
                            status_code=422)

    @app.exception_handler(ProviderError)
    async def provider_error(request, exc):
        return JSONResponse({"detail": str(exc)}, status_code=409)

    def session(sid: str, x_session_token: str = Header(default="")):
        s = sessions.get(sid)
        if not s or not hmac.compare_digest(s.token, x_session_token):
            raise HTTPException(404, "session_not_found")
        if time.monotonic() - s.touched > 3600:
            s.running = False
            if s.pending:
                s.pending.cancel()
            del sessions[sid]
            raise HTTPException(410, "session_expired")
        s.touched = time.monotonic()
        return s

    def model_permission(x_model_access: str = Header(default="")):
        if not access or not hmac.compare_digest(access, x_model_access):
            raise HTTPException(403, "model_access_required")

    @app.get("/api/health")
    def health():
        return {"status": "ok", "version": VERSION, "models_enabled": model_service.enabled}

    @app.get("/api/catalog")
    def catalog():
        return {"places": PLACES, "labels": LABELS, "roads": ROADS, "goals": GOAL_LABELS,
                "providers": [{"id": k, "protocol": p.protocol, "verification": "protocol_mock_only"}
                              for k, p in PROFILES.items()],
                "models_enabled": model_service.enabled}

    @app.post("/api/sessions", status_code=201)
    async def create(config: Config):
        now = time.monotonic()
        for sid, s in list(sessions.items()):
            if now - s.touched > 3600:
                s.running = False
                if s.pending:
                    s.pending.cancel()
                del sessions[sid]
        while creation_times and now - creation_times[0] > 60:
            creation_times.popleft()
        if len(sessions) >= 16 or len(creation_times) >= 30:
            raise HTTPException(429, "session_limit")
        creation_times.append(now)
        sid = secrets.token_urlsafe(18)
        s = Session(Engine(config))
        sessions[sid] = s
        return {"id": sid, "token": s.token, "view": s.view()}

    @app.get("/api/sessions/{sid}")
    async def get(s: Session = Depends(session)):
        async with s.lock:
            return s.view()

    @app.get("/api/sessions/{sid}/actor")
    async def actor(s: Session = Depends(session)):
        async with s.lock:
            return s.engine.actor_view()

    @app.delete("/api/sessions/{sid}")
    async def delete(sid: str, s: Session = Depends(session)):
        async with s.lock:
            s.running = False
            s.epoch += 1
            if s.pending:
                s.pending.cancel()
            sessions.pop(sid, None)
        return {"deleted": True}

    @app.post("/api/sessions/{sid}/commands")
    async def command(m: Mutation, s: Session = Depends(session)):
        async with s.lock:
            fingerprint = digest(m.model_dump())
            if m.command_id in s.receipts:
                old, view = s.receipts[m.command_id]
                if old != fingerprint:
                    raise HTTPException(409, "command_id_reused")
                return view
            if len(s.receipts) >= 1500:
                raise HTTPException(409, "command_limit")
            if m.expected_revision != s.revision:
                raise HTTPException(409, "revision_conflict")
            if s.recording:
                raise HTTPException(409, "replay_is_read_only")
            if m.command.kind == "proposal":
                raise HTTPException(403, "proposal_requires_model_gateway")
            if s.pending is not None and m.command.kind == "step":
                raise HTTPException(409, "model_barrier")
            async with cpu_slots:
                try:
                    await computation(s.engine.apply, m.command)
                except ValueError:
                    raise HTTPException(409, "session_record_limit") from None
            s.revision += 1
            if m.command.kind in ("goal", "cancel"):
                # Keep the HTTP response task alive to audit late replies as stale.
                s.epoch += 1
                s.running = False
            view = s.view()
            s.receipts[m.command_id] = (fingerprint, view)
            return view

    async def run_session(s: Session):
        try:
            while True:
                async with s.lock:
                    if not s.running or s.recording or time.monotonic() - s.touched > 3600:
                        s.running = False
                        return
                    if s.pending is None:
                        async with cpu_slots:
                            await computation(s.engine.apply, Command(kind="step"))
                        s.revision += 1
                        if s.engine.status not in ("ready", "running"):
                            s.running = False
                            return
                await asyncio.sleep(0.25)
        except ValueError:
            s.running = False

    @app.post("/api/sessions/{sid}/control")
    async def control(c: Control, s: Session = Depends(session)):
        async with s.lock:
            if c.operation == "reset":
                s.running = False
                s.epoch += 1
                s.engine = Engine(s.engine.config)
                s.recording = s.replay_result = None
                s.revision += 1
                s.model_status = {"status": "reset", "usage": None}
            elif c.operation == "pause":
                s.running = False
            else:
                if s.recording:
                    raise HTTPException(409, "replay_is_read_only")
                s.running = True
                if s.runner is None or s.runner.done():
                    s.runner = asyncio.create_task(run_session(s))
            return s.view()

    @app.get("/api/sessions/{sid}/export")
    async def export(s: Session = Depends(session)):
        async with s.lock:
            return s.recording if s.recording else s.engine.export()

    @app.post("/api/sessions/{sid}/import")
    async def import_record(req: ImportRequest, s: Session = Depends(session)):
        # Verify before replacing any existing session state.
        data = req.recording.model_dump()
        async with s.lock:
            async with cpu_slots:
                try:
                    engine, result = await computation(replay, data)
                except ValueError as error:
                    raise HTTPException(422, str(error)) from None
            s.running = False
            s.epoch += 1
            s.engine, s.replay_result, s.recording = engine, result, data
            s.revision += 1
            return s.view()

    @app.post("/api/sessions/{sid}/replay")
    async def replay_cursor(cursor: Cursor, s: Session = Depends(session)):
        async with s.lock:
            if not s.recording:
                raise HTTPException(409, "import_recording_first")
            async with cpu_slots:
                try:
                    s.engine, s.replay_result = await computation(replay, s.recording, cursor.until)
                except ValueError as error:
                    raise HTTPException(422, str(error)) from None
            s.revision += 1
            return s.view()

    @app.post("/api/sessions/{sid}/compare")
    async def paired(s: Session = Depends(session)):
        async with cpu_slots:
            return await computation(compare, s.engine.config)

    @app.post("/api/sessions/{sid}/model", dependencies=[Depends(model_permission)])
    async def model(req: ModelRequest, s: Session = Depends(session)):
        if not model_service.enabled:
            raise ProviderError("model_calls_disabled")
        settings = settings_loader(req.provider)
        async with s.lock:
            if s.recording or s.engine.active or s.engine.status in ("cancelled", "success"):
                raise HTTPException(409, "model_not_available_in_current_state")
            if s.pending is not None:
                raise HTTPException(409, "model_already_pending")
            if s.model_requests >= 5:
                raise HTTPException(429, "session_model_limit")
            s.model_requests += 1
            context = s.engine.request_context()
            validation_context = s.engine.context()
            context_hash, epoch = s.engine.context_digest(), s.epoch
            s.model_status = {"status": "pending", "usage": None}
            task = asyncio.create_task(model_service.propose(settings, context, validation_context))
            s.pending = task
        try:
            result = await task
            async with s.lock:
                if epoch != s.epoch or context_hash != s.engine.context_digest():
                    status = "discarded_stale"
                else:
                    s.engine.apply(Command(kind="proposal", actions=result["proposal"].actions))
                    s.revision += 1
                    status = "accepted" if s.engine.events[-1]["type"] == "proposal_accepted" else "rejected"
                model_service.ledger.finish(result["request_id"], status)
                s.model_status = {"status": status, "usage": result["usage"], "source": result["source"]}
                return s.view()
        except asyncio.CancelledError:
            async with s.lock:
                s.model_status = {"status": "cancelled", "usage": None}
                return s.view()
        except ProviderError as error:
            async with s.lock:
                s.model_status = {"status": str(error), "usage": None}
            raise
        finally:
            async with s.lock:
                if s.pending is task:
                    s.pending = None

    @app.post("/api/sessions/{sid}/model/cancel")
    async def cancel_model(s: Session = Depends(session)):
        async with s.lock:
            s.epoch += 1
            if s.pending:
                s.pending.cancel()
            s.model_status = {"status": "cancelled", "usage": None}
        return {"cancelled": True}

    @app.get("/api/model/audit", dependencies=[Depends(model_permission)])
    def audit():
        return model_service.ledger.summary()

    web = Path(__file__).resolve().parents[2] / "web" / "dist"
    if web.is_dir():
        app.mount("/", StaticFiles(directory=web, html=True), name="web")
    return app
