import json

from fastapi.testclient import TestClient

from worldmind.api import create_app
from worldmind.archives import ArchiveStore
from worldmind.domain import VERSION
from .helpers import new, mutate


def test_archive_survives_restart_with_capability_and_readonly_replay(tmp_path):
    args = {"ledger_path": tmp_path / "ledger.db", "archive_path": tmp_path / "archive.db"}
    with TestClient(create_app(**args)) as client:
        path, h, _ = new(client)
        mutate(client, path, h, {"kind": "step"})
        original = client.get(path + "/export", headers=h).json()
        receipt = client.post(path + "/archives", headers=h, json={"name": "升级回放"})
        assert receipt.status_code == 201
        saved = receipt.json()
        assert saved["schema_version"] == VERSION
        assert saved["token"] not in json.dumps(original)
    with TestClient(create_app(**args)) as client:
        assert client.get(path, headers=h).status_code == 404  # Active sessions stay ephemeral.
        path, h, _ = new(client)
        url = path + f'/archives/{saved["id"]}/restore'
        assert client.post(url, headers=h).status_code == 404
        auth = {**h, "x-archive-token": saved["token"]}
        restored = client.post(url, headers=auth)
        assert restored.status_code == 200
        assert restored.json()["mode"] == "verified_replay"
        assert restored.json()["state_hash"] == original["final_hash"]
        assert not restored.json()["running"]
        assert mutate(client, path, h, {"kind": "step"}, revision=1).status_code == 409
        assert client.post(path + "/replay", headers=h, json={"until": 0}).status_code == 200
        second = client.post(path + "/archives", headers=h, json={"name": "完整原录制"}).json()
        again = client.post(path + f'/archives/{second["id"]}/restore',
                            headers={**h, "x-archive-token": second["token"]}).json()
        assert again["replay"]["verified"] == 1
        before = again["state_hash"]
        assert client.delete(f'/api/archives/{saved["id"]}', headers=h).status_code == 404
        assert client.delete(f'/api/archives/{saved["id"]}', headers=auth).status_code == 200
        assert client.post(url, headers=auth).status_code == 404
        assert client.get(path, headers=h).json()["state_hash"] == before


def test_archive_capacity_invalid_names_and_expiry_do_not_mutate_session(tmp_path):
    clock = [100.0]
    store = ArchiveStore(tmp_path / "archives.db", max_archives=1, ttl_seconds=10,
                         clock=lambda: clock[0])
    app = create_app(ledger_path=tmp_path / "ledger.db", archive_store=store)
    with TestClient(app) as client:
        path, h, data = new(client)
        before = data["view"]["state_hash"]
        assert client.post(path + "/archives", headers=h, json={"name": "\x00"}).status_code == 422
        saved = client.post(path + "/archives", headers=h, json={"name": "valid"}).json()
        assert client.post(path + "/archives", headers=h, json={"name": "over cap"}).status_code == 429
        clock[0] = 111
        assert client.post(path + f'/archives/{saved["id"]}/restore',
                           headers={**h, "x-archive-token": saved["token"]}).status_code == 404
        assert client.get(path, headers=h).json()["state_hash"] == before
        assert client.post(path + "/archives", headers=h, json={"name": "new"}).status_code == 201


def test_control_versions_and_session_capability_not_in_repr(tmp_path):
    app = create_app(ledger_path=tmp_path / "ledger.db", archive_path=tmp_path / "archive.db")
    with TestClient(app) as client:
        path, h, data = new(client)
        s = app.state.sessions[data["id"]]
        assert data["token"] not in repr(s)
        for expected in (1, 2):
            result = client.post(path + "/control", headers=h, json={"operation": "pause"})
            assert result.json()["revision"] == expected
        old = s.view()
        s.model_status["status"] = "changed"
        assert old["model"]["status"] == "idle"
