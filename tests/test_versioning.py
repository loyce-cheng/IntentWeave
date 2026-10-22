"""Existing recordings are independent regression oracles, never rewritten to pass."""
import json
from pathlib import Path

import pytest

from worldmind import __version__
from worldmind.domain import VERSION
from worldmind.engine import Command, Engine, replay
from worldmind.version import LEGACY_RECORDING_VERSION


OLD_RECORDINGS = sorted((Path(__file__).parents[1] / "docs/evidence/recordings").glob("*.json"))


@pytest.mark.parametrize("path", OLD_RECORDINGS, ids=lambda p: p.stem)
def test_original_v01_recordings_and_every_cursor(path):
    original = path.read_bytes()
    data = json.loads(original)
    assert data["schema_version"] == LEGACY_RECORDING_VERSION
    engine, result = replay(data)
    assert result["complete"] and engine.export() == data
    for cursor in range(len(data["steps"]) + 1):
        partial, checked = replay(data, cursor)
        assert checked["verified"] == cursor
        assert partial.version == LEGACY_RECORDING_VERSION
    assert path.read_bytes() == original


def test_v02_protocol_and_relabelled_legacy_are_distinct():
    assert __version__ == "0.2.0" and "0.2.0" in VERSION
    engine = Engine()
    engine.apply(Command(kind="step"))
    assert replay(engine.export())[0].state_hash() == engine.state_hash()
    old = json.loads(OLD_RECORDINGS[0].read_text())
    old["schema_version"] = VERSION
    with pytest.raises(ValueError, match="initial hash mismatch"):
        replay(old)
    with pytest.raises(ValueError, match="unsupported recording version"):
        Engine(version="worldmind-99.0/river-1")
