"""Resource ownership and avoided work, with protocol behavior as the oracle."""
import asyncio
import gc
import threading
import weakref

import pytest

from worldmind.api import Control, Mutation, create_app
from worldmind.archives import ArchiveStore
from worldmind.domain import Config, canonical
from worldmind.engine import Command, Engine, compare, replay
from worldmind.sessions import Session
from worldmind.tasks import computation


def endpoint(app, path):
    return next(route.endpoint for route in app.routes if route.path == path)


def test_batch_replay_does_not_build_presentation_snapshots(monkeypatch):
    engine = Engine()
    for _ in range(5):
        engine.advance(Command(kind="step"))
    recording = engine.export()

    def unused(*args):
        raise AssertionError("batch replay must not build discarded UI snapshots")

    monkeypatch.setattr(Engine, "view", unused)
    restored, result = replay(recording)
    assert restored.export() == recording and result["complete"]
    assert compare(Config(), commands=[])["Full"]["tick"] == 9
    assert compare(Config())["Full"]["tick"] == 11


def test_snapshot_and_receipt_do_not_alias_mutable_state():
    session = Session(Engine())
    for _ in range(8):
        session.engine.advance(Command(kind="step"))
    session.revision = 8
    first = session.remember("step", "fingerprint")
    expected = canonical(first)
    payload = session.receipts["step"][1]
    assert len(payload) < len(expected.encode()) // 2
    first["metrics"]["budget_used"] = -1
    first["actor"]["evidence"].clear()
    first["events"].clear()
    restored = session.check_mutation("step", 0, "fingerprint")
    assert canonical(restored) == expected
    restored["model"]["status"] = "mutated"
    assert canonical(session.check_mutation("step", 0, "fingerprint")) == expected
    assert session.engine.metrics["budget_used"] > 0
    assert session.engine.evidence


async def test_cancelled_command_commits_revision_and_exact_retry_receipt(tmp_path, monkeypatch):
    app = create_app(ledger_path=tmp_path / "ledger.db", archive_path=tmp_path / "archive.db")
    _, session = app.state.registry.create(Config())
    command = endpoint(app, "/api/sessions/{sid}/commands")
    request = Mutation(command_id="one", expected_revision=0, command=Command(kind="step"))
    entered, release = threading.Event(), threading.Event()
    advance = session.engine.advance

    def blocked(command):
        entered.set()
        assert release.wait(5)
        advance(command)

    monkeypatch.setattr(session.engine, "advance", blocked)
    task = asyncio.create_task(command(request, session))
    try:
        assert await asyncio.to_thread(entered.wait, 5)
        for _ in range(3):
            task.cancel()
            await asyncio.sleep(0)
            assert session.lock.locked()
    finally:
        release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    retry = await command(request, session)
    assert retry["revision"] == session.revision == 1
    assert retry["trace_length"] == len(session.engine.trace) == 1
    assert await command(request, session) == retry


async def test_pending_runner_sleeps_until_notified_and_terminal_run_does_no_work(tmp_path):
    class CountedEvent(asyncio.Event):
        waits = 0

        async def wait(self):
            self.waits += 1
            return await super().wait()

    app = create_app(ledger_path=tmp_path / "ledger.db", archive_path=tmp_path / "archive.db")
    _, session = app.state.registry.create(Config())
    control = endpoint(app, "/api/sessions/{sid}/control")
    session.wakeup = CountedEvent()
    session.pending = asyncio.create_task(asyncio.Event().wait())
    try:
        await control(Control(operation="run"), session)
        await asyncio.sleep(0.6)
        assert session.wakeup.waits == 1
        assert session.engine.tick == 0
        await control(Control(operation="pause"), session)
        await asyncio.wait_for(session.runner, 1)
        assert not session.running
    finally:
        session.pending.cancel()
        await asyncio.gather(session.pending, return_exceptions=True)
        session.pending = None
    session.engine.advance(Command(kind="cancel"))
    before = session.engine.export()
    await control(Control(operation="run"), session)
    assert not session.running and session.runner.done()
    assert session.engine.export() == before


async def test_deleted_session_workers_are_joined_and_references_released(tmp_path):
    app = create_app(ledger_path=tmp_path / "ledger.db", archive_path=tmp_path / "archive.db")
    registry = app.state.registry
    entered, release = threading.Event(), threading.Event()

    def work():
        entered.set()
        assert release.wait(5)

    async with app.router.lifespan_context(app):
        sid, session = registry.create(Config())
        session_ref = weakref.ref(session)
        session.pending = asyncio.create_task(computation(work))
        try:
            assert await asyncio.to_thread(entered.wait, 5)
            registry.remove(sid)
            assert len(registry.retiring) == 1
            del session
            release.set()
        finally:
            release.set()
    assert not registry.sessions and not registry.retiring
    gc.collect()
    assert session_ref() is None


def test_archive_restore_verifies_only_once(tmp_path, monkeypatch):
    import worldmind.archives as archives

    store = ArchiveStore(tmp_path / "archives.db")
    engine = Engine()
    engine.advance(Command(kind="step"))
    receipt = store.create("one", engine.export())
    calls = []
    original = archives.replay

    def tracked(data):
        calls.append(data)
        return original(data)

    monkeypatch.setattr(archives, "replay", tracked)
    restored, checked, recording = store.restore(receipt["id"], receipt["token"])
    assert len(calls) == 1 and checked["complete"]
    assert restored.export() == recording == engine.export()


async def test_cancel_after_proposal_write_keeps_revision_and_audit_consistent(tmp_path, monkeypatch):
    import httpx

    from worldmind.api import ModelRequest
    from worldmind.models import Ledger, ModelService
    from .helpers import proposal_for, response_for, settings

    entered, release = threading.Event(), threading.Event()
    ledger = Ledger(str(tmp_path / "ledger.db"))

    async def handler(request):
        return httpx.Response(200, json=response_for("chat", proposal_for(Engine())))

    app = create_app(model_service=ModelService(ledger, enabled=True, transport=httpx.MockTransport(handler)),
                     settings_loader=settings, archive_path=tmp_path / "archive.db")
    _, session = app.state.registry.create(Config())
    advance = session.engine.advance

    def blocked(command):
        entered.set()
        assert release.wait(5)
        advance(command)

    monkeypatch.setattr(session.engine, "advance", blocked)
    model = endpoint(app, "/api/sessions/{sid}/model")
    task = asyncio.create_task(model(ModelRequest(provider="deepseek"), session))
    try:
        assert await asyncio.to_thread(entered.wait, 5)
        task.cancel()
        await asyncio.sleep(0)
        assert session.lock.locked()
    finally:
        release.set()
    result = await task
    assert result["model"]["status"] == ledger.summary()["requests"][0]["status"] == "accepted"
    assert result["revision"] == session.revision == 1
    assert session.pending is None and session.wakeup.is_set()
