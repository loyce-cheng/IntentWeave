"""In-memory session lifecycle, independent of HTTP routes and persistent archives."""
from __future__ import annotations

import asyncio
import hmac
import json
import secrets
import time
import zlib
from collections import deque
from copy import deepcopy
from dataclasses import dataclass, field

from .domain import canonical, digest
from .runtime import SessionConfig, SessionEngine, make_engine
from .cooperation import CooperationEngine


class SessionError(Exception):
    def __init__(self, status: int, code: str):
        self.status, self.code = status, code
        super().__init__(code)


@dataclass
class Session:
    engine: SessionEngine
    token: str = field(default_factory=lambda: secrets.token_urlsafe(32), repr=False)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    revision: int = 0
    epoch: int = 0
    touched: float = field(default_factory=time.monotonic)
    running: bool = False
    wakeup: asyncio.Event = field(default_factory=asyncio.Event, repr=False)
    runner: asyncio.Task | None = None
    pending: asyncio.Task | None = None
    model_requests: int = 0
    receipts: dict[str, tuple[str, bytes]] = field(default_factory=dict, repr=False)
    model_status: dict = field(default_factory=lambda: {"status": "idle", "usage": None})
    replay_result: dict | None = None
    recording: dict | None = field(default=None, repr=False)

    def view(self):
        return {**self.engine.view(), "revision": self.revision, "running": self.running,
                "model": deepcopy(self.model_status), "replay": deepcopy(self.replay_result),
                "mode": "verified_replay" if self.recording else "live_offline"}

    @property
    def can_run(self) -> bool:
        return self.recording is None and self.engine.status in ("ready", "running")

    def pause(self):
        self.running = False
        self.wakeup.set()

    def check_mutation(self, command_id: str, expected_revision: int, fingerprint: str):
        """Under the writer lock, accept an exact retry before checking revision."""
        if command_id in self.receipts:
            previous, payload = self.receipts[command_id]
            if previous != fingerprint:
                raise SessionError(409, "command_id_reused")
            return json.loads(zlib.decompress(payload))
        if len(self.receipts) >= 1500:
            raise SessionError(409, "command_limit")
        if expected_revision != self.revision:
            raise SessionError(409, "revision_conflict")
        return None

    def remember(self, command_id: str, fingerprint: str) -> dict:
        # Keep exact retry responses without retaining 1,500 nested object graphs.
        # Reading a receipt returns a fresh value, never an alias into session state.
        view = self.view()
        self.receipts[command_id] = (fingerprint, zlib.compress(canonical(view).encode(), level=1))
        return view

    def install_replay(self, engine: SessionEngine, result: dict, recording: dict):
        # Call only after full verification, under the writer lock. Late models become stale.
        self.pause()
        self.epoch += 1
        self.engine, self.replay_result, self.recording = engine, result, recording
        self.revision += 1

    def resume_replay(self):
        """Fork the already verified cursor into a paused live continuation.

        Engine.trace contains only the verified prefix, including active craft,
        evidence timestamps, budgets and merchant state. Keep its protocol; a
        version relabel would invalidate the prefix hashes. The source archive
        and the original recording are never modified.
        """
        if self.recording is None:
            raise SessionError(409, "import_recording_first")
        self.pause()
        self.epoch += 1
        if isinstance(self.engine, CooperationEngine):
            self.engine.lineage = {"parent_recording_hash": digest(self.recording),
                                   "cursor": len(self.engine.trace)}
        self.recording = self.replay_result = None
        self.model_status = {"status": "idle", "usage": None}
        self.revision += 1


class SessionRegistry:
    def __init__(self, *, max_sessions=16, creation_limit=30, ttl=3600, clock=time.monotonic):
        self.sessions: dict[str, Session] = {}
        self.retiring: set[asyncio.Task] = set()
        self.creation_times = deque()
        self.max_sessions, self.creation_limit, self.ttl = max_sessions, creation_limit, ttl
        self.clock = clock

    def remove(self, sid: str):
        s = self.sessions.pop(sid, None)
        if s is not None:
            s.pause()
            s.epoch += 1
            for task in (s.runner, s.pending):
                if task and not task.done():
                    # Deleted sessions may still own a cancelling SQLite worker.
                    # Retain it until joined; shutdown must include these tasks.
                    self.retiring.add(task)
                    task.add_done_callback(self.retiring.discard)
                    task.cancel()

    def get(self, sid: str, token: str) -> Session:
        s = self.sessions.get(sid)
        if s is None or not hmac.compare_digest(s.token.encode("utf-8"), token.encode("utf-8")):
            raise SessionError(404, "session_not_found")
        if self.clock() - s.touched > self.ttl:
            self.remove(sid)
            raise SessionError(410, "session_expired")
        s.touched = self.clock()
        return s

    def create(self, config: SessionConfig) -> tuple[str, Session]:
        now = self.clock()
        for sid, s in list(self.sessions.items()):
            if now - s.touched > self.ttl:
                self.remove(sid)
        while self.creation_times and now - self.creation_times[0] > 60:
            self.creation_times.popleft()
        if len(self.sessions) >= self.max_sessions or len(self.creation_times) >= self.creation_limit:
            raise SessionError(429, "session_limit")
        self.creation_times.append(now)
        sid = secrets.token_urlsafe(18)
        s = Session(make_engine(config), touched=now)
        self.sessions[sid] = s
        return sid, s
