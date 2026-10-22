"""In-memory session lifecycle, independent of HTTP routes and persistent archives."""
from __future__ import annotations

import asyncio
import hmac
import secrets
import time
from collections import deque
from copy import deepcopy
from dataclasses import dataclass, field

from .domain import Config
from .engine import Engine


class SessionError(Exception):
    def __init__(self, status: int, code: str):
        self.status, self.code = status, code
        super().__init__(code)


@dataclass
class Session:
    engine: Engine
    token: str = field(default_factory=lambda: secrets.token_urlsafe(32), repr=False)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    revision: int = 0
    epoch: int = 0
    touched: float = field(default_factory=time.monotonic)
    running: bool = False
    runner: asyncio.Task | None = None
    pending: asyncio.Task | None = None
    model_requests: int = 0
    receipts: dict = field(default_factory=dict, repr=False)
    model_status: dict = field(default_factory=lambda: {"status": "idle", "usage": None})
    replay_result: dict | None = None
    recording: dict | None = field(default=None, repr=False)

    def view(self):
        return {**self.engine.view(), "revision": self.revision, "running": self.running,
                "model": deepcopy(self.model_status), "replay": deepcopy(self.replay_result),
                "mode": "verified_replay" if self.recording else "live_offline"}

    def install_replay(self, engine: Engine, result: dict, recording: dict):
        # Call only after full verification, under the writer lock. Late models become stale.
        self.running = False
        self.epoch += 1
        self.engine, self.replay_result, self.recording = engine, result, recording
        self.revision += 1


class SessionRegistry:
    def __init__(self, *, max_sessions=16, creation_limit=30, ttl=3600, clock=time.monotonic):
        self.sessions: dict[str, Session] = {}
        self.creation_times = deque()
        self.max_sessions, self.creation_limit, self.ttl = max_sessions, creation_limit, ttl
        self.clock = clock

    def remove(self, sid: str):
        s = self.sessions.pop(sid, None)
        if s is not None:
            s.running = False
            s.epoch += 1
            if s.pending:
                s.pending.cancel()

    def get(self, sid: str, token: str) -> Session:
        s = self.sessions.get(sid)
        if s is None or not hmac.compare_digest(s.token.encode("utf-8"), token.encode("utf-8")):
            raise SessionError(404, "session_not_found")
        if self.clock() - s.touched > self.ttl:
            self.remove(sid)
            raise SessionError(410, "session_expired")
        s.touched = self.clock()
        return s

    def create(self, config: Config) -> tuple[str, Session]:
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
        s = Session(Engine(config), touched=now)
        self.sessions[sid] = s
        return sid, s
