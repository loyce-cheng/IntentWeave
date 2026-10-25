"""Existing recordings are independent regression oracles, never rewritten to pass."""
import json
from pathlib import Path

import pytest

from worldmind import __version__
from worldmind.domain import VERSION
from worldmind.engine import Command, Engine, replay


OLD_RECORDINGS = sorted((Path(__file__).parents[1] / "docs/evidence/recordings").glob("*.json"))


def test_v02_protocol_and_relabelled_legacy_are_distinct():
    assert __version__ == "0.2.1" and "0.2.1" in VERSION
    engine = Engine()
    engine.apply(Command(kind="step"))
    assert replay(engine.export())[0].state_hash() == engine.state_hash()
    old = json.loads(OLD_RECORDINGS[0].read_text())
    old["schema_version"] = VERSION
    with pytest.raises(ValueError, match="initial hash mismatch"):
        replay(old)
    with pytest.raises(ValueError, match="unsupported recording version"):
        Engine(version="worldmind-99.0/river-1")


RECORDINGS = OLD_RECORDINGS + sorted(
    (Path(__file__).parents[1] / "docs/evidence/v0.2/recordings").glob("worldmind-0.2.0-*.json")
) + sorted(
    (Path(__file__).parents[1] / "docs/evidence/v0.2.1/recordings").glob("*.json")
)


@pytest.mark.parametrize("path", RECORDINGS, ids=lambda p: p.stem)
def test_original_recordings_and_every_cursor(path):
    original = path.read_bytes()
    data = json.loads(original)
    engine, result = replay(data)
    assert result["complete"] and engine.export() == data
    for cursor in range(len(data["steps"]) + 1):
        partial, checked = replay(data, cursor)
        assert checked["verified"] == cursor
        assert partial.version == data["schema_version"]
    assert path.read_bytes() == original
