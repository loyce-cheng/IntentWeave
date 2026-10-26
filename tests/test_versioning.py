"""Release metadata agrees; every distinct historical recording remains an oracle."""
import json
import tomllib
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from worldmind import __version__
from worldmind.api import create_app
from worldmind.domain import VERSION, canonical
from worldmind.engine import Command, Engine
from worldmind.runtime import replay
from worldmind.version import SUPPORTED_RECORDING_VERSIONS


ROOT = Path(__file__).parents[1]
# Include every evidence directory. Deduplicate the entire normalized JSON, not
# just final hashes: distinct inputs or per-step hashes must still run separately.
RECORDINGS = {}
for path in sorted((ROOT / "docs/evidence").glob("**/recordings/*.json")):
    RECORDINGS.setdefault(canonical(json.loads(path.read_bytes())), []).append(path)


def test_release_metadata_and_api_agree_without_relabelling_recordings(tmp_path):
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    package = json.loads((ROOT / "web/package.json").read_text(encoding="utf-8"))
    lock = json.loads((ROOT / "web/package-lock.json").read_text(encoding="utf-8"))
    assert __version__ == project["project"]["version"] == package["version"] == "0.4.0"
    assert lock["version"] == lock["packages"][""]["version"] == __version__
    # A release label must not change deterministic simulation semantics.
    assert VERSION == "worldmind-0.2.1/river-1"
    app = create_app(ledger_path=tmp_path / "ledger.db", archive_path=tmp_path / "archive.db")
    with TestClient(app) as client:
        health = client.get("/api/health").json()
        catalog = client.get("/api/catalog").json()
        assert health["app_version"] == catalog["app_version"] == app.version == __version__
        assert health["version"] == VERSION
        assert catalog["recording_versions"] == list(SUPPORTED_RECORDING_VERSIONS)
        assert client.get("/openapi.json").json()["info"]["version"] == __version__


def test_recording_protocol_rejects_relabelled_legacy_and_unknown_versions():
    engine = Engine()
    engine.apply(Command(kind="step"))
    assert replay(engine.export())[0].state_hash() == engine.state_hash()
    old_path = next((ROOT / "docs/evidence/recordings").glob("*.json"))
    old = json.loads(old_path.read_bytes())
    old["schema_version"] = VERSION
    with pytest.raises(ValueError, match="initial hash mismatch"):
        replay(old)
    with pytest.raises(ValueError, match="unsupported recording version"):
        Engine(version="worldmind-99.0/river-1")


@pytest.mark.parametrize("expected,paths", [
    pytest.param(expected, paths, id=paths[0].stem) for expected, paths in RECORDINGS.items()
])
def test_original_recordings_and_every_cursor(expected, paths):
    originals = {path: path.read_bytes() for path in paths}
    for original in originals.values():
        assert canonical(json.loads(original)) == expected
    data = json.loads(expected)
    engine, result = replay(data)
    assert result["complete"] and result["verified"] == len(data["steps"])
    assert engine.export() == data
    # The complete cursor was checked above; do not replay it a second time.
    for cursor in range(len(data["steps"])):
        partial, checked = replay(data, cursor)
        assert checked["verified"] == cursor
        assert partial.version == data["schema_version"]
    for path, original in originals.items():
        assert path.read_bytes() == original
