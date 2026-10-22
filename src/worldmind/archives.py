"""Bounded durable replay archives protected by independent capability tokens.

Archives contain validated input recordings, never live sessions or provider data.
The owning service must run these synchronous methods in its bounded worker pool.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import math
import secrets
import sqlite3
import time
import unicodedata
from contextlib import contextmanager
from pathlib import Path
from typing import Callable

from .domain import canonical
from .engine import Recording, replay


class ArchiveError(Exception):
    """A safe, stable public error code; no paths, credentials or raw data."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


class InvalidArchive(ArchiveError):
    pass


class ArchiveNotFound(ArchiveError):
    def __init__(self):
        # Missing, expired and wrong credentials deliberately share one response.
        super().__init__("archive_not_found")


class ArchiveCapacity(ArchiveError):
    def __init__(self):
        super().__init__("archive_capacity_exhausted")


class ArchiveCorrupt(ArchiveError):
    def __init__(self):
        super().__init__("archive_corrupt")


class ArchiveUnavailable(ArchiveError):
    def __init__(self, code: str = "archive_storage_unavailable"):
        super().__init__(code)


def _name(value: str) -> str:
    if not isinstance(value, str):
        raise InvalidArchive("invalid_archive_name")
    value = unicodedata.normalize("NFKC", value).strip()
    if not 1 <= len(value) <= 80 or any(unicodedata.category(c).startswith("C") for c in value):
        raise InvalidArchive("invalid_archive_name")
    return value


class ArchiveStore:
    """Single-host SQLite archive repository with transactionally enforced quotas.

    Limits count canonical recording bytes, not SQLite indexes or free pages.
    Constructing another store for the same file preserves archives and credentials.
    There is intentionally no public listing or access-token recovery operation.
    """

    def __init__(self, path: str | Path, *, ttl_seconds: float = 7 * 86400,
                 max_archives: int = 64, max_total_bytes: int = 64 * 1024 * 1024,
                 max_recording_bytes: int = 2_000_000,
                 clock: Callable[[], float] = time.time):
        if (not isinstance(ttl_seconds, (int, float)) or isinstance(ttl_seconds, bool)
                or not math.isfinite(ttl_seconds) or ttl_seconds <= 0):
            raise ValueError("invalid archive lifetime")
        for value, upper in ((max_archives, 10000), (max_total_bytes, 1024**3),
                             (max_recording_bytes, 2_000_000)):
            if type(value) is not int or not 1 <= value <= upper:
                raise ValueError("invalid archive capacity")
        if str(path) == ":memory:":
            raise ValueError("archives require a persistent file")
        self.path = str(path)
        self.ttl_seconds = ttl_seconds
        self.max_archives = max_archives
        self.max_total_bytes = max_total_bytes
        self.max_recording_bytes = max_recording_bytes
        self.clock = clock
        try:
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        except OSError:
            raise ArchiveUnavailable() from None
        with self._db() as db:
            version = db.execute("PRAGMA user_version").fetchone()[0]
            if version not in (0, 1):
                raise ArchiveUnavailable("archive_schema_unsupported")
            db.executescript("""
                CREATE TABLE IF NOT EXISTS archives (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    token_hash TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    expires_at REAL NOT NULL,
                    schema_version TEXT NOT NULL,
                    step_count INTEGER NOT NULL,
                    byte_count INTEGER NOT NULL,
                    payload_hash TEXT NOT NULL,
                    payload BLOB NOT NULL
                );
                CREATE INDEX IF NOT EXISTS archive_expiry ON archives(expires_at);
                PRAGMA user_version = 1;
            """)

    @contextmanager
    def _db(self):
        db = None
        try:
            db = sqlite3.connect(self.path, timeout=5)
            db.row_factory = sqlite3.Row
            with db:
                yield db
        except sqlite3.Error:
            raise ArchiveUnavailable() from None
        finally:
            if db is not None:
                db.close()

    def _now(self) -> float:
        try:
            now = self.clock()
            valid = type(now) in (int, float) and math.isfinite(now)
        except (OSError, ValueError, OverflowError):
            raise ArchiveUnavailable("archive_clock_unavailable") from None
        if not valid:
            raise ArchiveUnavailable("archive_clock_unavailable")
        return now

    @staticmethod
    def _metadata(row) -> dict:
        return {key: row[key] for key in (
            "id", "name", "schema_version", "created_at", "expires_at", "step_count", "byte_count")}

    def _recording(self, data: dict) -> tuple[dict, bytes]:
        try:
            value = Recording.model_validate(data).model_dump(exclude_none=True)
            payload = canonical(value).encode("utf-8")
        except (ValueError, TypeError, RecursionError, UnicodeError):
            raise InvalidArchive("invalid_archive_recording") from None
        if len(payload) > self.max_recording_bytes:
            raise InvalidArchive("archive_recording_too_large")
        try:
            replay(value)
        except (ValueError, TypeError, RecursionError):
            raise InvalidArchive("archive_replay_diverged") from None
        return value, payload

    def create(self, name: str, recording: dict) -> dict:
        """Validate fully, then atomically reserve storage and return one access token."""
        name = _name(name)
        data, payload = self._recording(recording)
        archive_id = secrets.token_urlsafe(24)
        token = secrets.token_urlsafe(32)
        token_hash = hashlib.sha256(token.encode("ascii")).hexdigest()
        now = self._now()
        expiry = now + self.ttl_seconds
        if not math.isfinite(expiry):
            raise ArchiveUnavailable("archive_clock_unavailable")
        metadata = {"id": archive_id, "name": name, "schema_version": data["schema_version"],
                    "created_at": now, "expires_at": expiry,
                    "step_count": len(data["steps"]), "byte_count": len(payload)}
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("DELETE FROM archives WHERE expires_at <= ?", (now,))
            count, used = db.execute(
                "SELECT COUNT(*), COALESCE(SUM(length(payload)), 0) FROM archives").fetchone()
            if count >= self.max_archives or used + len(payload) > self.max_total_bytes:
                raise ArchiveCapacity()
            db.execute("INSERT INTO archives VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", (
                archive_id, name, token_hash, now, expiry, data["schema_version"],
                len(data["steps"]), len(payload), hashlib.sha256(payload).hexdigest(), payload))
        return {**metadata, "token": token}

    @staticmethod
    def _authenticate(row, token: str):
        if not isinstance(token, str) or not 1 <= len(token) <= 128:
            raise ArchiveNotFound()
        # Even a nonexistent id does a fixed-size comparison.
        try:
            presented = hashlib.sha256(token.encode("utf-8")).hexdigest()
        except UnicodeError:
            raise ArchiveNotFound() from None
        expected = row["token_hash"] if row is not None else "0" * 64
        if not isinstance(expected, str) or len(expected) != 64 or any(c not in "0123456789abcdef" for c in expected):
            raise ArchiveCorrupt()
        if not hmac.compare_digest(presented, expected) or row is None:
            raise ArchiveNotFound()

    def load(self, archive_id: str, token: str) -> dict:
        """Read-only retrieval; revalidate integrity and every replay step on each load."""
        if not isinstance(archive_id, str) or not 1 <= len(archive_id) <= 128:
            raise ArchiveNotFound()
        with self._db() as db:
            row = db.execute("""SELECT id,
                             CASE WHEN length(name) <= 80 THEN name ELSE NULL END AS name,
                             CASE WHEN length(token_hash) = 64 THEN token_hash ELSE NULL END AS token_hash,
                             created_at,expires_at,
                             CASE WHEN length(schema_version) <= 64 THEN schema_version ELSE NULL END AS schema_version,
                             step_count,byte_count,
                             CASE WHEN length(payload_hash) = 64 THEN payload_hash ELSE NULL END AS payload_hash,
                             CASE WHEN length(payload) <= ? THEN payload ELSE NULL END AS payload
                             FROM archives WHERE id = ? AND expires_at > ?""",
                             (self.max_recording_bytes, archive_id, self._now())).fetchone()
        self._authenticate(row, token)
        try:
            payload = row["payload"]
            if (not isinstance(payload, bytes) or len(payload) > self.max_recording_bytes
                    or len(payload) != row["byte_count"]
                    or not hmac.compare_digest(hashlib.sha256(payload).hexdigest(), row["payload_hash"])):
                raise ArchiveCorrupt()
            data = json.loads(payload)
            value, normalized = self._recording(data)
            if (normalized != payload or row["schema_version"] != value["schema_version"]
                    or row["step_count"] != len(value["steps"])
                    or _name(row["name"]) != row["name"]
                    or not math.isfinite(row["created_at"]) or not math.isfinite(row["expires_at"])
                    or row["expires_at"] <= row["created_at"]):
                raise ArchiveCorrupt()
        except (InvalidArchive, ValueError, TypeError, RecursionError, UnicodeError):
            raise ArchiveCorrupt() from None
        return {"archive": self._metadata(row), "recording": value}

    def delete(self, archive_id: str, token: str) -> None:
        """Delete only with its independent capability; session credentials cannot substitute."""
        if not isinstance(archive_id, str) or not 1 <= len(archive_id) <= 128:
            raise ArchiveNotFound()
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("""SELECT CASE WHEN length(token_hash) = 64 THEN token_hash ELSE NULL END
                             AS token_hash FROM archives WHERE id = ? AND expires_at > ?""",
                             (archive_id, self._now())).fetchone()
            self._authenticate(row, token)
            db.execute("DELETE FROM archives WHERE id = ?", (archive_id,))

    def cleanup(self) -> int:
        """Remove expired rows; callers may schedule this without disclosing metadata."""
        with self._db() as db:
            return db.execute("DELETE FROM archives WHERE expires_at <= ?", (self._now(),)).rowcount
