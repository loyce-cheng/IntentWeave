"""Small synchronous client for the versioned IntentWeave HTTP service.

No command is retried automatically. When explicitly retrying an uncertain write,
retain both its command_id and expected_revision to use the server's receipt.
The SDK has no model-call entry point and never accepts supplier API keys.
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from typing import Any
from uuid import uuid4

import httpx

from .domain import Config
from .cooperation_domain import CooperationConfig, ACTORS


class SDKError(Exception):
    """A server rejection or transport failure; status is None before a reply."""

    def __init__(self, status: int | None, detail: str):
        self.status = status
        self.detail = detail
        super().__init__(f"IntentWeave request failed ({status or 'transport'}): {detail}")


def _identifier(value: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", value):
        raise ValueError("Invalid capability identifier")
    return value


def _token(value: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,256}", value):
        raise ValueError("Invalid capability token")
    return value


@dataclass(frozen=True)
class ArchiveReceipt:
    """Store this receipt privately; possession grants archive restore/deletion."""

    id: str
    token: str = field(repr=False)
    name: str
    schema_version: str
    created_at: float
    expires_at: float
    byte_count: int
    step_count: int

    def __post_init__(self):
        _identifier(self.id)
        _token(self.token)


class WorldmindClient:
    """HTTP client. An injected client remains owned and closed by its caller.

    ``client`` may be an httpx.Client or FastAPI TestClient for embedded tests.
    It must be dedicated to the intended IntentWeave origin. No redirects are
    followed, so capability headers are never forwarded to another host.
    """

    def __init__(self, base_url: str = "http://127.0.0.1:8000", *,
                 client: Any = None, timeout: float = 10.0):
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or not 0 < timeout <= 60:
            raise ValueError("timeout must be finite and in (0, 60] seconds")
        url = httpx.URL(base_url)
        if url.scheme not in ("http", "https") or not url.host or url.username or url.password or url.query or url.fragment:
            raise ValueError("base_url must be an HTTP(S) service URL without credentials/query/fragment")
        self._timeout = timeout
        self._owns_client = client is None
        self._client = client if client is not None else httpx.Client(
            base_url=base_url.rstrip("/"), timeout=timeout, follow_redirects=False,
        )
        self._closed = False

    def __enter__(self):
        if self._closed:
            raise RuntimeError("IntentWeave client is closed")
        return self

    def __exit__(self, exc_type, exc, tb):
        self.close()

    def close(self):
        if not self._closed and self._owns_client:
            self._client.close()
        self._closed = True

    def _request(self, method: str, path: str, *, headers: dict | None = None,
                 json: dict | None = None) -> dict:
        if self._closed:
            raise RuntimeError("IntentWeave client is closed")
        headers = headers or {}
        try:
            response = self._client.request(method, "/api" + path, headers=headers,
                                            json=json, timeout=self._timeout,
                                            follow_redirects=False)
        except httpx.RequestError:
            # RequestError can contain full URLs or headers from custom transports.
            raise SDKError(None, "transport_error") from None
        try:
            result = response.json()
        except ValueError:
            raise SDKError(response.status_code, "invalid_json_response") from None
        if not response.is_success:
            detail = result.get("detail", "http_error") if isinstance(result, dict) else "http_error"
            detail = detail if isinstance(detail, str) else "input_validation_failed"
            for value in headers.values():
                if value:
                    detail = detail.replace(value, "[redacted]")
            raise SDKError(response.status_code, detail[:512])
        if not isinstance(result, dict):
            raise SDKError(response.status_code, "invalid_response_shape")
        return result

    def health(self) -> dict:
        return self._request("GET", "/health")

    def create_session(self, config: Config | CooperationConfig | dict | None = None) -> WorldmindSession:
        payload = config.model_dump() if isinstance(config, (Config, CooperationConfig)) else (config or {})
        data = self._request("POST", "/sessions", json=payload)
        return self.attach_session(data["id"], data["token"])

    def attach_session(self, session_id: str, token: str) -> WorldmindSession:
        """Attach existing credentials without claiming that they are still valid."""
        return WorldmindSession(self, session_id, token)

    def delete_archive(self, receipt: ArchiveReceipt) -> None:
        self._request("DELETE", f"/archives/{receipt.id}",
                      headers={"X-Archive-Token": receipt.token})


@dataclass(frozen=True)
class WorldmindSession:
    _client: WorldmindClient = field(repr=False)
    id: str
    token: str = field(repr=False)

    def __post_init__(self):
        _identifier(self.id)
        _token(self.token)

    def _request(self, method: str, suffix: str = "", *, json: dict | None = None,
                 archive_token: str | None = None) -> dict:
        headers = {"X-Session-Token": self.token}
        if archive_token is not None:
            headers["X-Archive-Token"] = _token(archive_token)
        return self._client._request(method, f"/sessions/{self.id}{suffix}",
                                     headers=headers, json=json)

    def view(self) -> dict:
        return self._request("GET")

    def actor(self, actor_id: str = "courier") -> dict:
        if actor_id not in ACTORS:
            raise ValueError("unknown actor")
        return self._request("GET", "/actor" if actor_id == "courier" else "/actor?actor_id=" + actor_id)

    def command(self, kind: str, target: str | None = None, *,
                command_id: str | None = None, expected_revision: int | None = None) -> dict:
        """Read the current revision if omitted; concurrent changes still raise 409.

        For an explicit retry pass the original command_id AND expected_revision.
        Reading a fresh revision is not an automatic rebase or retry operation.
        """
        revision = self.view()["revision"] if expected_revision is None else expected_revision
        command = {"kind": kind}
        if target is not None:
            command["target"] = target
        return self._request("POST", "/commands", json={
            "command_id": command_id if command_id is not None else uuid4().hex,
            "expected_revision": revision, "command": command,
        })

    def step(self) -> dict:
        return self.command("step")

    def control(self, operation: str) -> dict:
        return self._request("POST", "/control", json={"operation": operation})

    def export(self) -> dict:
        return self._request("GET", "/export")

    def import_recording(self, recording: dict) -> dict:
        return self._request("POST", "/import", json={"recording": recording})

    def seek(self, until: int) -> dict:
        return self._request("POST", "/replay", json={"until": until})

    def resume(self, *, command_id: str | None = None,
               expected_revision: int | None = None) -> dict:
        """Continue from the verified cursor, paused, using its original protocol.

        Pass both original identifiers for an explicit idempotent retry. This
        does not replenish budgets, revive terminal runs, or resume model calls.
        """
        revision = self.view()["revision"] if expected_revision is None else expected_revision
        return self._request("POST", "/resume", json={
            "command_id": command_id if command_id is not None else uuid4().hex,
            "expected_revision": revision,
        })

    def save_archive(self, name: str) -> ArchiveReceipt:
        return ArchiveReceipt(**self._request("POST", "/archives", json={"name": name}))

    def restore_archive(self, receipt: ArchiveReceipt) -> dict:
        return self._request("POST", f"/archives/{receipt.id}/restore", archive_token=receipt.token)

    def delete(self) -> None:
        self._request("DELETE")
