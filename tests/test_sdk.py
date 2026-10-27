import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import httpx
import pytest
from fastapi.testclient import TestClient

from worldmind.api import create_app
from worldmind.domain import Config
from worldmind.sdk import ArchiveReceipt, SDKError, WorldmindClient


@pytest.fixture
def sdk(tmp_path, monkeypatch):
    monkeypatch.setenv("WORLDMIND_ENABLE_MODELS", "0")
    app = create_app(ledger_path=str(tmp_path / "ledger.sqlite3"),
                     archive_path=str(tmp_path / "archives.sqlite3"))
    with TestClient(app) as transport:
        with WorldmindClient(client=transport) as client:
            yield client


def test_sdk_delivery_and_verified_replay(sdk):
    assert sdk.health()["models_enabled"] is False
    session = sdk.create_session(Config(goal="herb_clinic"))
    for _ in range(20):
        view = session.step()
        if view["status"] == "success":
            break
    assert view["status"] == "success"
    recording = session.export()
    assert session.token not in json.dumps(recording)
    target = sdk.create_session()
    restored = target.import_recording(recording)
    assert restored["mode"] == "verified_replay"
    assert restored["state_hash"] == view["state_hash"]
    assert target.seek(0)["tick"] == 0
    assert target.seek(1)["replay"]["verified"] == 1
    with pytest.raises(SDKError) as rejected:
        target.step()
    assert rejected.value.status == 409
    assert rejected.value.detail == "replay_is_read_only"


def test_sdk_conflicts_are_not_retried_and_receipts_are_explicit(sdk):
    session = sdk.create_session()
    initial = session.view()["revision"]
    first = session.command("step", command_id="stable", expected_revision=initial)
    assert session.command("step", command_id="stable", expected_revision=initial) == first
    with pytest.raises(SDKError) as conflict:
        session.command("step", command_id="conflict", expected_revision=initial)
    assert conflict.value.status == 409
    assert conflict.value.detail == "revision_conflict"
    assert session.view()["tick"] == 1
    assert session.view()["trace_length"] == 1


def test_sdk_capabilities_and_hidden_information(sdk):
    session = sdk.create_session()
    before = session.actor()
    session.command("block", "bridge|home")
    assert session.actor() == before
    assert "world" not in before
    assert session.token not in repr(session)
    wrong = sdk.attach_session(session.id, "incorrect-token")
    with pytest.raises(SDKError) as error:
        wrong.view()
    assert error.value.status == 404
    assert wrong.token not in repr(error.value)
    session.delete()
    with pytest.raises(SDKError):
        session.view()


def test_sdk_controls_and_atomic_invalid_import(sdk):
    session = sdk.create_session()
    session.step()
    session.control("pause")
    before = session.view()
    broken = session.export()
    broken["final_hash"] = "0" * 64
    with pytest.raises(SDKError) as invalid:
        session.import_recording(broken)
    assert invalid.value.status == 422
    assert session.view()["state_hash"] == before["state_hash"]
    assert session.command("cancel")["status"] == "cancelled"
    assert session.control("reset")["tick"] == 0
    assert session.command("goal", "medicine_clinic")["goal"] == "medicine_clinic"


def test_sdk_does_not_close_borrowed_transport(tmp_path):
    app = create_app(ledger_path=str(tmp_path / "ledger.sqlite3"),
                     archive_path=str(tmp_path / "archives.sqlite3"))
    with TestClient(app) as transport:
        with WorldmindClient(client=transport) as client:
            assert client.health()["status"] == "ok"
        assert transport.get("/api/health").status_code == 200
        with pytest.raises(RuntimeError, match="closed"):
            client.health()


def test_sdk_closes_owned_transport():
    with WorldmindClient() as client:
        assert not client._client.is_closed
    assert client._client.is_closed


def test_sdk_honors_environment_proxy(monkeypatch):
    paths = []

    class Proxy(BaseHTTPRequestHandler):
        def do_GET(self):
            paths.append(self.path)
            body = b'{"status":"ok","models_enabled":false}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    # A protocol fixture verifies actual proxy routing without contacting a
    # remote Worldmind service or making a supplier/model request.
    server = HTTPServer(("127.0.0.1", 0), Proxy)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        proxy = f"http://127.0.0.1:{server.server_port}"
        for key in ("HTTP_PROXY", "http_proxy"):
            monkeypatch.setenv(key, proxy)
        for key in ("NO_PROXY", "no_proxy"):
            monkeypatch.setenv(key, "")
        with WorldmindClient("http://worldmind-proxy-target.invalid", timeout=2) as client:
            assert client.health()["status"] == "ok"
        assert paths == ["http://worldmind-proxy-target.invalid/api/health"]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)


@pytest.mark.parametrize("timeout", [0, -1, 61, float("nan"), float("inf"), True])
def test_sdk_requires_bounded_timeout(timeout):
    with pytest.raises(ValueError, match="timeout"):
        WorldmindClient(timeout=timeout)


@pytest.mark.parametrize("url", ["file:///tmp/server", "http://key:secret@localhost", "http://localhost/?token=secret"])
def test_sdk_rejects_url_credentials_and_invalid_protocol(url):
    with pytest.raises(ValueError, match="base_url"):
        WorldmindClient(url)


def test_sdk_rejects_capability_path_injection(sdk):
    with pytest.raises(ValueError, match="identifier"):
        sdk.attach_session("../model/audit", "safe-token")
    with pytest.raises(ValueError, match="token"):
        sdk.attach_session("safe-id", "header\r\ninjection")


def test_sdk_transport_errors_are_sanitized_and_not_retried():
    attempts = []
    def fail(request):
        attempts.append(request)
        raise httpx.ConnectError("sensitive-upstream-detail", request=request)
    with httpx.Client(transport=httpx.MockTransport(fail), base_url="http://testserver") as transport:
        with WorldmindClient(client=transport) as sdk:
            with pytest.raises(SDKError) as error:
                sdk.health()
    assert len(attempts) == 1
    assert error.value.status is None
    assert error.value.detail == "transport_error"
    assert "sensitive" not in repr(error.value)


def test_sdk_never_follows_capability_redirects():
    paths = []
    def redirect(request):
        paths.append(request.url.path)
        return httpx.Response(307, headers={"location": "https://other.invalid/collect"},
                              json={"detail": request.headers["X-Session-Token"]})
    with httpx.Client(transport=httpx.MockTransport(redirect), base_url="http://testserver",
                      follow_redirects=True) as transport:
        with WorldmindClient(client=transport) as sdk:
            session = sdk.attach_session("safe-id", "secret-capability")
            with pytest.raises(SDKError) as error:
                session.view()
    assert paths == ["/api/sessions/safe-id"]
    assert error.value.status == 307
    assert "secret-capability" not in str(error.value)


def test_sdk_archive_save_restore_and_delete(sdk):
    source = sdk.create_session()
    source.step()
    receipt = source.save_archive("SDK archive")
    assert isinstance(receipt, ArchiveReceipt)
    assert receipt.token not in repr(receipt)
    target = sdk.create_session()
    restored = target.restore_archive(receipt)
    assert restored["state_hash"] == source.view()["state_hash"]
    assert restored["mode"] == "verified_replay"
    assert target.seek(0)["tick"] == 0
    sdk.delete_archive(receipt)
    with pytest.raises(SDKError) as deleted:
        target.restore_archive(receipt)
    assert deleted.value.status == 404
