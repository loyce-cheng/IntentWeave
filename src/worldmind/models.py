"""Optional HTTP proposals with durable quotas, validated cache and redacted audit."""
from __future__ import annotations

import asyncio
import json
import sqlite3
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

import httpx

from .domain import canonical, digest
from .planner import Budget, Context, Exhausted, validate
from .providers import (ProviderError, Settings, build_request, parse_proposal,
                        parse_response, strict_json, usage_of)


class Ledger:
    def __init__(self, path: str, limit: int = 100):
        if not 0 <= limit <= 10000:
            raise ValueError("invalid request quota")
        self.path, self.limit = path, limit
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        with self.db() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS requests (
                  id TEXT PRIMARY KEY, created REAL NOT NULL, provider TEXT NOT NULL,
                  request_hash TEXT NOT NULL, charged INTEGER NOT NULL,
                  status TEXT NOT NULL, usage TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS cache (
                  key TEXT PRIMARY KEY, created REAL NOT NULL, proposal TEXT NOT NULL);
            """)
            db.execute("UPDATE requests SET status='abandoned_on_restart' WHERE status='pending'")

    @contextmanager
    def db(self):
        db = sqlite3.connect(self.path, timeout=5)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def reserve(self, provider: str, key: str, charged=True):
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            count = db.execute("SELECT COALESCE(SUM(charged),0) FROM requests").fetchone()[0]
            # Audit/cache are bounded even when every request is a cache hit.
            total = db.execute("SELECT COUNT(*) FROM requests").fetchone()[0]
            if (charged and count >= self.limit) or total >= 50000:
                raise ProviderError("request_quota_exhausted")
            request_id = uuid.uuid4().hex
            db.execute("INSERT INTO requests VALUES(?,?,?,?,?,?,?)",
                       (request_id, time.time(), provider, key, int(charged), "pending",
                        canonical({"known": False, "input_tokens": None, "output_tokens": None, "cost": None})))
        return request_id

    def finish(self, request_id: str, status: str, usage: dict | None = None):
        with self.db() as db:
            if usage is None:
                db.execute("UPDATE requests SET status=? WHERE id=?", (status, request_id))
            else:
                db.execute("UPDATE requests SET status=?, usage=? WHERE id=?",
                           (status, canonical(usage), request_id))

    def get_cache(self, key):
        with self.db() as db:
            row = db.execute("SELECT proposal FROM cache WHERE key=? AND created>?",
                             (key, time.time() - 86400)).fetchone()
        return parse_proposal(row[0]) if row else None

    def put_cache(self, key, proposal):
        with self.db() as db:
            db.execute("DELETE FROM cache WHERE created<?", (time.time() - 86400,))
            db.execute("INSERT OR REPLACE INTO cache VALUES(?,?,?)",
                       (key, time.time(), canonical(proposal.model_dump())))

    def summary(self):
        with self.db() as db:
            rows = db.execute("SELECT * FROM requests ORDER BY created DESC LIMIT 100").fetchall()
            count = db.execute("SELECT COALESCE(SUM(charged),0) FROM requests").fetchone()[0]
        return {"limit": self.limit, "reserved_requests": count,
                "remaining": max(0, self.limit - count),
                "requests": [{**dict(r), "usage": json.loads(r["usage"])} for r in rows]}


class ModelService:
    def __init__(self, ledger: Ledger, *, enabled=False, transport=None, concurrency=2):
        self.ledger = ledger
        self.enabled = enabled
        self.transport = transport
        self.semaphore = asyncio.Semaphore(concurrency)

    async def propose(self, settings: Settings, context: dict, validation_context: Context):
        if not self.enabled:
            raise ProviderError("model_calls_disabled")
        url, headers, body = build_request(settings, context)
        key = digest({"provider": settings.provider, "protocol": settings.profile.protocol,
                      "endpoint": url, "body": body, "schema": "worldmind-0.1.0",
                      "workspace": settings.workspace_id})
        if self.semaphore.locked():
            raise ProviderError("model_concurrency_limit")
        async with self.semaphore:
            cached = self.ledger.get_cache(key)
            request_id = self.ledger.reserve(settings.provider, key, charged=cached is None)
            usage = None
            try:
                if cached:
                    proposal = cached
                    source = "validated_cache"
                else:
                    source = "provider_response"
                    async with asyncio.timeout(settings.timeout):
                        async with httpx.AsyncClient(
                            transport=self.transport, timeout=settings.timeout, follow_redirects=False,
                            limits=httpx.Limits(max_connections=2, max_keepalive_connections=0)
                        ) as client:
                            async with client.stream("POST", url, headers=headers, json=body) as response:
                                if response.status_code != 200:
                                    # Do not log response body, URL, or exception text.
                                    raise ProviderError("provider_http_error")
                                length = response.headers.get("content-length")
                                if length and (not length.isdigit() or int(length) > settings.max_response_bytes):
                                    raise ProviderError("response_too_large")
                                chunks, size = [], 0
                                async for chunk in response.aiter_bytes():
                                    size += len(chunk)
                                    if size > settings.max_response_bytes:
                                        raise ProviderError("response_too_large")
                                    chunks.append(chunk)
                    try:
                        data = strict_json(b"".join(chunks).decode())
                    except (ValueError, UnicodeError, RecursionError):
                        raise ProviderError("invalid_response_json") from None
                    if not isinstance(data, dict):
                        raise ProviderError("invalid_provider_response")
                    usage = usage_of(settings.profile.protocol, data)
                    proposal, usage = parse_response(settings.profile.protocol, data)
                try:
                    valid, _, _ = validate(validation_context, proposal.actions, Budget(1000))
                except Exhausted:
                    valid = False
                if not valid:
                    raise ProviderError("semantic_rejection")
                self.ledger.put_cache(key, proposal)
                self.ledger.finish(request_id, source, usage)
                return {"request_id": request_id, "proposal": proposal, "source": source,
                        "usage": usage or {"known": False, "input_tokens": None, "output_tokens": None, "cost": None}}
            except asyncio.CancelledError:
                self.ledger.finish(request_id, "cancelled", usage)
                raise
            except (TimeoutError, httpx.TimeoutException):
                self.ledger.finish(request_id, "timeout", usage)
                raise ProviderError("provider_timeout") from None
            except httpx.HTTPError:
                self.ledger.finish(request_id, "network_error", usage)
                raise ProviderError("provider_network_error") from None
            except ProviderError as error:
                self.ledger.finish(request_id, str(error), usage)
                raise
