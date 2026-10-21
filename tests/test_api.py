import asyncio
import json

import httpx
import pytest
from fastapi.testclient import TestClient

from worldmind.api import create_app
from worldmind.engine import Engine
from worldmind.models import Ledger, ModelService
from .test_models import proposal_for, response_for, settings


@pytest.fixture
def client(tmp_path):
    app = create_app(ledger_path=str(tmp_path / "ledger.db"))
    with TestClient(app) as c:
        yield c


def new(client):
    response = client.post("/api/sessions", json={})
    assert response.status_code == 201
    data = response.json()
    return f'/api/sessions/{data["id"]}', {"x-session-token": data["token"]}, data


def mutate(client, path, headers, command, revision=0, command_id="a"):
    return client.post(path + "/commands", headers=headers, json={
        "command_id": command_id, "expected_revision": revision, "command": command})


def test_session_auth_and_information_boundary(client):
    path, headers, _ = new(client)
    assert client.get(path).status_code == 404
    view = client.get(path, headers=headers).json()
    actor = client.get(path + "/actor", headers=headers).json()
    assert "world" in view and "world" not in actor
    assert "state_hash" not in json.dumps(actor)
    p2, h2, _ = new(client)
    mutate(client, path, headers, {"kind": "block", "target": "bridge|home"})
    assert client.get(path, headers=headers).json()["world"] != client.get(p2, headers=h2).json()["world"]
    assert client.get(path + "/actor", headers=headers).json() == client.get(p2 + "/actor", headers=h2).json()


def test_idempotency_conflict_and_pause_reset(client):
    path, h, _ = new(client)
    first = mutate(client, path, h, {"kind": "step"})
    assert first.status_code == 200
    assert mutate(client, path, h, {"kind": "step"}).json() == first.json()
    assert mutate(client, path, h, {"kind": "cancel"}).status_code == 409
    assert mutate(client, path, h, {"kind": "step"}, command_id="b").status_code == 409
    mutate(client, path, h, {"kind": "step"}, revision=1, command_id="b")
    # Stored idempotency receipt is an immutable response, not a mutable metrics reference.
    assert mutate(client, path, h, {"kind": "step"}).json() == first.json()
    reset = client.post(path + "/control", headers=h, json={"operation": "reset"})
    assert reset.json()["tick"] == 0
    assert reset.json()["revision"] == 3
    assert client.post(path + "/control", headers=h, json={"operation": "pause"}).status_code == 200


def test_import_replay_corruption_is_atomic(client):
    path, h, _ = new(client)
    mutate(client, path, h, {"kind": "step"})
    recording = client.get(path + "/export", headers=h).json()
    good_hash = client.get(path, headers=h).json()["state_hash"]
    bad = json.loads(json.dumps(recording))
    bad["steps"][0]["event_hash"] = "0" * 64
    assert client.post(path + "/import", headers=h, json={"recording": bad}).status_code == 422
    assert client.get(path, headers=h).json()["state_hash"] == good_hash
    good = client.post(path + "/import", headers=h, json={"recording": recording})
    assert good.json()["replay"]["complete"]
    rewind = client.post(path + "/replay", headers=h, json={"until": 0}).json()
    assert rewind["tick"] == 0 and rewind["mode"] == "verified_replay"
    assert mutate(client, path, h, {"kind": "step"}, revision=rewind["revision"], command_id="b").status_code == 409


def test_input_limits_no_secret_echo_and_origin(client):
    path, h, _ = new(client)
    assert mutate(client, path, h, {"kind": "teleport", "secret": "do-not-echo"}).status_code == 422
    response = client.post("/api/sessions", json={"api_key": "do-not-echo"})
    assert "do-not-echo" not in response.text
    assert client.post("/api/sessions", content="x" * 2_000_001).status_code == 413
    assert client.post("/api/sessions", headers={"origin": "https://evil.invalid"}, json={}).status_code == 403
    assert client.get("/api/model/audit").status_code == 403
    assert client.post(path + "/model", headers=h, json={"provider": "deepseek"}).status_code == 403
    assert mutate(client, path, h, {"kind": "proposal", "actions":[{"kind":"wait","target":"self"}]}).status_code == 403


async def test_concurrent_mutations_have_one_writer(tmp_path):
    app = create_app(ledger_path=str(tmp_path / "ledger.db"))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://testserver") as client:
        data = (await client.post("/api/sessions", json={})).json()
        path = f'/api/sessions/{data["id"]}'
        headers = {"x-session-token": data["token"]}
        replies = await asyncio.gather(*[
            client.post(path + "/commands", headers=headers, json={"command_id": str(i),
                "expected_revision": 0, "command": {"kind": "step"}}) for i in range(4)])
        assert sorted(r.status_code for r in replies) == [200, 409, 409, 409]
        assert (await client.get(path, headers=headers)).json()["tick"] == 1


@pytest.mark.parametrize("intervention", ["goal", "reset", "cancel", "hidden"])
async def test_late_model_response_lifecycle(intervention, tmp_path):
    began, release = asyncio.Event(), asyncio.Event()
    async def handler(req):
        began.set()
        await release.wait()
        return httpx.Response(200, json=response_for("chat", proposal_for(Engine())))
    ledger = Ledger(str(tmp_path / "ledger.db"))
    models = ModelService(ledger, enabled=True, transport=httpx.MockTransport(handler))
    app = create_app(model_service=models, model_access="test-access", settings_loader=settings)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://testserver") as c:
        data = (await c.post("/api/sessions", json={})).json()
        path = f'/api/sessions/{data["id"]}'
        h = {"x-session-token": data["token"], "x-model-access": "test-access"}
        pending = asyncio.create_task(c.post(path + "/model", headers=h, json={"provider": "deepseek"}))
        await began.wait()
        barrier = await c.post(path + "/commands", headers=h, json={
            "command_id": "barrier", "expected_revision": 0, "command": {"kind": "step"}})
        assert barrier.status_code == 409
        if intervention == "reset":
            await c.post(path + "/control", headers=h, json={"operation": "reset"})
        else:
            command = {"kind": "goal", "target": "herb_clinic"} if intervention == "goal" else (
                {"kind": "cancel"} if intervention == "cancel" else {"kind": "block", "target": "bridge|home"})
            await c.post(path + "/commands", headers=h, json={
                "command_id": "change", "expected_revision": 0, "command": command})
        release.set()
        result = await pending
        assert result.status_code == 200
        expected = "accepted" if intervention == "hidden" else "discarded_stale"
        assert result.json()["model"]["status"] == expected
        assert ledger.summary()["requests"][0]["status"] == expected
        exported = await c.get(path + "/export", headers=h)
        assert "fixture-secret" not in exported.text


async def test_cancel_model_request_and_run_pause(tmp_path):
    began = asyncio.Event()
    async def handler(req):
        began.set()
        await asyncio.Event().wait()
    ledger = Ledger(str(tmp_path / "ledger.db"))
    app = create_app(model_service=ModelService(ledger, enabled=True, transport=httpx.MockTransport(handler)),
                     model_access="test-access", settings_loader=settings)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://testserver") as c:
        data = (await c.post("/api/sessions", json={})).json()
        path, h = f'/api/sessions/{data["id"]}', {"x-session-token": data["token"], "x-model-access": "test-access"}
        task = asyncio.create_task(c.post(path + "/model", headers=h, json={"provider":"deepseek"}))
        await began.wait()
        await c.post(path + "/model/cancel", headers=h)
        assert (await task).json()["model"]["status"] == "cancelled"
        assert ledger.summary()["requests"][0]["status"] == "cancelled"
        await c.post(path + "/control", headers=h, json={"operation": "run"})
        await asyncio.sleep(0.35)
        paused = (await c.post(path + "/control", headers=h, json={"operation": "pause"})).json()
        assert paused["tick"] > 0
        await asyncio.sleep(0.3)
        assert (await c.get(path, headers=h)).json()["tick"] == paused["tick"]


def test_enabled_models_fail_closed_without_ledger(tmp_path, monkeypatch):
    monkeypatch.setenv("WORLDMIND_ENABLE_MODELS", "1")
    monkeypatch.setenv("WORLDMIND_MODEL_ACCESS", "a" * 24)
    with pytest.raises(RuntimeError, match="ledger"):
        create_app(ledger_path=str(tmp_path / "missing.db"))


def test_session_capacity_and_server_configuration_not_browser_controlled(client):
    for _ in range(16):
        assert client.post('/api/sessions', json={}).status_code == 201
    assert client.post('/api/sessions', json={}).status_code == 429
    assert client.post('/api/sessions', json={'endpoint':'http://internal.invalid'}).status_code == 422


async def test_cancelled_worker_finishes_before_releasing_writer():
    import threading
    from worldmind.api import computation
    entered, release = threading.Event(), threading.Event()
    events = []
    def work():
        entered.set()
        release.wait(2)
        events.append('committed')
    task = asyncio.create_task(computation(work))
    await asyncio.to_thread(entered.wait, 2)
    task.cancel()
    await asyncio.sleep(0.01)
    assert not task.done()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert events == ['committed']


async def test_per_session_model_limit_survives_reset(tmp_path):
    async def handler(req):
        return httpx.Response(200, json=response_for('chat', proposal_for(Engine())))
    ledger = Ledger(str(tmp_path / 'ledger.db'))
    service = ModelService(ledger, enabled=True, transport=httpx.MockTransport(handler))
    app = create_app(model_service=service, model_access='test-access', settings_loader=settings)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url='http://testserver') as c:
        data = (await c.post('/api/sessions', json={})).json()
        path = f'/api/sessions/{data["id"]}'
        headers = {'x-session-token':data['token'], 'x-model-access':'test-access'}
        for _ in range(5):
            response = await c.post(path+'/model', headers=headers, json={'provider':'deepseek'})
            assert response.status_code == 200
            assert response.json()['model']['status'] == 'accepted'
        await c.post(path+'/control', headers=headers, json={'operation':'reset'})
        assert (await c.post(path+'/model', headers=headers, json={'provider':'deepseek'})).status_code == 429
        assert ledger.summary()['reserved_requests'] == 5
