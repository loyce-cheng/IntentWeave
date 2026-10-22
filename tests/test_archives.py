from concurrent.futures import ThreadPoolExecutor
from contextlib import closing, contextmanager
from copy import deepcopy
import hashlib
import json
import sqlite3

import pytest

from worldmind.archives import (ArchiveCapacity, ArchiveCorrupt, ArchiveNotFound,
                               ArchiveStore, ArchiveUnavailable, InvalidArchive)
from worldmind.domain import canonical
from worldmind.engine import Command, Engine, replay


@contextmanager
def database(path):
    # sqlite3's transaction context alone does not close the Windows file handle.
    with closing(sqlite3.connect(path)) as db:
        with db:
            yield db


@pytest.fixture
def recording():
    engine = Engine()
    engine.apply(Command(kind="step"))
    engine.apply(Command(kind="block", target="bridge|home"))
    return engine.export()


def test_restart_load_is_independently_verified_and_contains_no_tokens(tmp_path, recording):
    path = tmp_path / "archives.sqlite3"
    created = ArchiveStore(path).create("  断桥案例  ", recording)
    assert created["name"] == "断桥案例"
    assert created["step_count"] == 2
    assert len(created["id"]) >= 32 and len(created["token"]) >= 43
    loaded = ArchiveStore(path).load(created["id"], created["token"])
    assert loaded["recording"] == recording
    assert loaded["archive"] == {k: v for k, v in created.items() if k != "token"}
    assert replay(loaded["recording"])[1]["complete"]
    with database(path) as db:
        token_hash, payload = db.execute("SELECT token_hash,payload FROM archives").fetchone()
    assert token_hash == hashlib.sha256(created["token"].encode()).hexdigest()
    assert created["token"].encode() not in path.read_bytes()
    assert created["token"] not in json.dumps(loaded)
    assert json.loads(payload) == recording
    # No leaked SQLite handles: Windows also needs the connections closed for rename/delete.
    renamed = path.with_name("moved.sqlite3")
    path.rename(renamed)
    assert ArchiveStore(renamed).load(created["id"], created["token"])["recording"] == recording


def test_separate_credentials_and_delete(tmp_path, recording):
    store = ArchiveStore(tmp_path / "archives.sqlite3")
    first = store.create("first", recording)
    second = store.create("second", recording)
    for operation in (store.load, store.delete):
        for token in ("", first["id"], second["token"], "x" * 10000):
            with pytest.raises(ArchiveNotFound, match="^archive_not_found$"):
                operation(first["id"], token)
        with pytest.raises(ArchiveNotFound, match="^archive_not_found$"):
            operation("missing", first["token"])
    store.delete(first["id"], first["token"])
    with pytest.raises(ArchiveNotFound):
        store.load(first["id"], first["token"])
    assert store.load(second["id"], second["token"])["recording"] == recording


def test_expiration_boundary_and_capacity_reclamation(tmp_path, recording):
    clock = [1000.0]
    store = ArchiveStore(tmp_path / "archives.sqlite3", clock=lambda: clock[0],
                         ttl_seconds=10, max_archives=1)
    first = store.create("first", recording)
    clock[0] = 1009.99
    assert store.load(first["id"], first["token"])
    with pytest.raises(ArchiveCapacity):
        store.create("second", recording)
    clock[0] = 1010
    with pytest.raises(ArchiveNotFound):
        store.load(first["id"], first["token"])
    second = store.create("second", recording)
    assert store.load(second["id"], second["token"])
    clock[0] = 1020
    assert store.cleanup() == 1
    assert store.cleanup() == 0


@pytest.mark.parametrize("name", ["", "   ", "x" * 81, "line\nbreak", "zero\x00byte", "hidden\u202e", 4])
def test_name_validation(tmp_path, recording, name):
    store = ArchiveStore(tmp_path / "archives.sqlite3")
    with pytest.raises(InvalidArchive, match="^invalid_archive_name$"):
        store.create(name, recording)


def test_reject_invalid_or_secret_fields_without_persisting_them(tmp_path, recording):
    path = tmp_path / "archives.sqlite3"
    store = ArchiveStore(path)
    bad = deepcopy(recording)
    bad["api_key"] = "sensitive-test-value"
    with pytest.raises(InvalidArchive, match="^invalid_archive_recording$"):
        store.create("bad", bad)
    assert b"sensitive-test-value" not in path.read_bytes()
    bad = deepcopy(recording)
    bad["steps"][1]["state_hash"] = "0" * 64
    with pytest.raises(InvalidArchive, match="^archive_replay_diverged$"):
        store.create("bad", bad)
    with database(path) as db:
        assert db.execute("SELECT COUNT(*) FROM archives").fetchone()[0] == 0


def test_single_and_aggregate_byte_limits(tmp_path, recording):
    length = len(canonical(recording).encode())
    small = ArchiveStore(tmp_path / "small.sqlite3", max_recording_bytes=length - 1)
    with pytest.raises(InvalidArchive, match="^archive_recording_too_large$"):
        small.create("too large", recording)
    store = ArchiveStore(tmp_path / "total.sqlite3", max_total_bytes=length)
    saved = store.create("exact boundary", recording)
    assert saved["byte_count"] == length
    with pytest.raises(ArchiveCapacity):
        store.create("over budget", recording)
    store.delete(saved["id"], saved["token"])
    assert store.create("space reclaimed", recording)


@pytest.mark.parametrize("tamper", ["bytes", "forged_hash", "byte_count", "schema", "name", "step_count"])
def test_corrupt_rows_fail_closed(tmp_path, recording, tamper):
    path = tmp_path / "archives.sqlite3"
    store = ArchiveStore(path)
    saved = store.create("intact", recording)
    with database(path) as db:
        if tamper in ("bytes", "forged_hash"):
            bad = deepcopy(recording)
            bad["final_hash"] = "0" * 64
            payload = canonical(bad).encode()
            db.execute("UPDATE archives SET payload=?", (payload,))
            if tamper == "forged_hash":
                # A valid envelope hash cannot bypass deterministic replay validation.
                db.execute("UPDATE archives SET payload_hash=?", (hashlib.sha256(payload).hexdigest(),))
        elif tamper == "byte_count":
            db.execute("UPDATE archives SET byte_count=0")
        elif tamper == "schema":
            db.execute("UPDATE archives SET schema_version='future'")
        elif tamper == "name":
            db.execute("UPDATE archives SET name='' ")
        else:
            db.execute("UPDATE archives SET step_count=0")
    with pytest.raises(ArchiveCorrupt, match="^archive_corrupt$"):
        store.load(saved["id"], saved["token"])
    store.delete(saved["id"], saved["token"])


def test_concurrent_reservations_cannot_overfill(tmp_path, recording):
    path = tmp_path / "archives.sqlite3"
    store = ArchiveStore(path, max_archives=2)

    def reserve(i):
        try:
            return store.create(str(i), recording)
        except ArchiveCapacity:
            return None

    with ThreadPoolExecutor(max_workers=6) as pool:
        outcomes = list(pool.map(reserve, range(10)))
    assert sum(outcome is not None for outcome in outcomes) == 2
    assert ArchiveStore(path, max_archives=2)
    with pytest.raises(ArchiveCapacity):
        store.create("still full after restart", recording)


def test_corrupt_byte_counter_cannot_bypass_aggregate_quota(tmp_path, recording):
    path = tmp_path / "archives.sqlite3"
    store = ArchiveStore(path, max_total_bytes=len(canonical(recording).encode()))
    store.create("first", recording)
    with database(path) as db:
        db.execute("UPDATE archives SET byte_count=0")
    with pytest.raises(ArchiveCapacity):
        store.create("cannot overfill", recording)


@pytest.mark.parametrize("column", ["payload", "name", "token_hash", "payload_hash"])
def test_oversized_corrupt_fields_fail_closed(tmp_path, recording, column):
    path = tmp_path / "archives.sqlite3"
    store = ArchiveStore(path)
    created = store.create("valid", recording)
    with database(path) as db:
        # Column is a fixed test parameter, never untrusted SQL input.
        db.execute(f"UPDATE archives SET {column}=zeroblob(2000001)")
    with pytest.raises(ArchiveCorrupt):
        store.load(created["id"], created["token"])


@pytest.mark.parametrize("clock_value", [True, float("nan"), float("inf"), 10**1000])
def test_invalid_clock_fails_closed_without_writing(tmp_path, recording, clock_value):
    path = tmp_path / "archives.sqlite3"
    store = ArchiveStore(path, clock=lambda: clock_value)
    with pytest.raises(ArchiveUnavailable, match="^archive_clock_unavailable$"):
        store.create("clock failure", recording)
    with database(path) as db:
        assert db.execute("SELECT COUNT(*) FROM archives").fetchone()[0] == 0


def test_storage_schema_or_file_corruption_errors_are_sanitized(tmp_path):
    path = tmp_path / "private-path.sqlite3"
    path.write_bytes(b"not a sqlite database")
    with pytest.raises(ArchiveUnavailable, match="^archive_storage_unavailable$"):
        ArchiveStore(path)
    path.unlink()
    with database(path) as db:
        db.execute("PRAGMA user_version=900")
    with pytest.raises(ArchiveUnavailable, match="^archive_schema_unsupported$"):
        ArchiveStore(path)


@pytest.mark.parametrize("kwargs", [{"ttl_seconds": 0}, {"ttl_seconds": float("inf")},
                                   {"max_archives": 0}, {"max_total_bytes": True},
                                   {"max_recording_bytes": 2_000_001}])
def test_invalid_storage_limits(tmp_path, kwargs):
    with pytest.raises(ValueError):
        ArchiveStore(tmp_path / "archives.sqlite3", **kwargs)
