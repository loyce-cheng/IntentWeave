"""Durable continuation, branch integrity, retries and stale request boundaries."""
from copy import deepcopy

import pytest
from fastapi.testclient import TestClient

from worldmind.api import create_app
from worldmind.domain import Config
from worldmind.engine import Command, Engine, replay
from worldmind.sdk import SDKError, WorldmindClient
from worldmind.version import SUPPORTED_RECORDING_VERSIONS
from .helpers import finish


@pytest.mark.parametrize("version", SUPPORTED_RECORDING_VERSIONS)
def test_restart_resume_active_craft_matches_uninterrupted_run(tmp_path, version):
    expected = Engine(Config(merchant_takes_at=2), version=version)
    while expected.active is None:
        expected.apply(Command(kind="step"))
        assert expected.tick < 15
    checkpoint = expected.export()
    active = deepcopy(expected.active)
    before = deepcopy(expected.logical())
    args = {"ledger_path": tmp_path / "ledger.db", "archive_path": tmp_path / "archive.db"}
    with TestClient(create_app(**args)) as transport, WorldmindClient(client=transport) as client:
        source = client.create_session()
        source.import_recording(checkpoint)
        receipt = source.save_archive("during craft")
    finish(expected)
    with TestClient(create_app(**args)) as transport, WorldmindClient(client=transport) as client:
        target = client.create_session()
        target.restore_archive(receipt)
        restored = target.resume()
        assert restored["mode"] == "live_offline" and not restored["running"]
        assert restored["actor"]["active"] == active
        assert restored["actor"]["evidence"] == before["actor"]["evidence"]
        assert restored["merchant"] == before["merchant"]
        assert restored["metrics"] == before["metrics"]
        assert restored["state_hash"] == checkpoint["final_hash"]
        while restored["status"] in ("ready", "running"):
            restored = target.step()
        assert restored["status"] == "success"
        assert restored["metrics"]["consumed"] == restored["metrics"]["produced"] == 1
        assert target.export() == expected.export()
        assert replay(target.export())[0].state_hash() == expected.state_hash()
        # The immutable source archive is still the mid-action checkpoint.
        reader = client.create_session()
        assert reader.restore_archive(receipt)["actor"]["active"] == active
        assert reader.export() == checkpoint


def test_resume_from_cursor_forks_only_verified_prefix_and_is_idempotent(tmp_path):
    original = finish(Engine()).export()
    app = create_app(ledger_path=tmp_path / "ledger.db", archive_path=tmp_path / "archive.db")
    with TestClient(app) as transport, WorldmindClient(client=transport) as client:
        session = client.create_session()
        session.import_recording(original)
        at_cursor = session.seek(2)
        revision = at_cursor["revision"]
        resumed = session.resume(command_id="resume-once", expected_revision=revision)
        assert session.resume(command_id="resume-once", expected_revision=revision) == resumed
        prefix = session.export()
        assert prefix["steps"] == original["steps"][:2]
        assert prefix["final_hash"] == original["steps"][1]["state_hash"]
        with pytest.raises(SDKError, match="revision_conflict"):
            session.resume(command_id="stale", expected_revision=revision)
        with pytest.raises(SDKError, match="command_id_reused"):
            session.command("step", command_id="resume-once", expected_revision=revision)
        session.command("goal", "herb_clinic")
        for _ in range(20):
            if session.step()["status"] == "success":
                break
        branched = session.export()
        assert branched["steps"][:2] == original["steps"][:2]
        assert branched["steps"][2]["input"] == {"kind": "goal", "target": "herb_clinic"}
        assert replay(branched)[0].status == "success"
        # Even after new work, a transport retry returns the immutable old receipt.
        assert session.resume(command_id="resume-once", expected_revision=revision) == resumed
        assert session.export() == branched
        assert session.import_recording(original)["state_hash"] == original["final_hash"]


def test_resume_conflict_and_invalid_import_leave_state_unchanged(tmp_path):
    app = create_app(ledger_path=tmp_path / "ledger.db", archive_path=tmp_path / "archive.db")
    with TestClient(app) as transport, WorldmindClient(client=transport) as client:
        session = client.create_session()
        with pytest.raises(SDKError, match="import_recording_first"):
            session.resume()
        engine = finish(Engine())
        imported = session.import_recording(engine.export())
        session.seek(1)
        before = session.view()
        with pytest.raises(SDKError, match="revision_conflict"):
            session.resume(expected_revision=imported["revision"])
        corrupt = engine.export()
        corrupt["steps"][-1]["event_hash"] = "0" * 64
        with pytest.raises(SDKError):
            session.import_recording(corrupt)
        assert session.view() == before
        wrong = client.attach_session(session.id, "wrong-token")
        with pytest.raises(SDKError) as error:
            wrong.resume(expected_revision=before["revision"])
        assert error.value.status == 404


@pytest.mark.parametrize("config", [Config(), Config(total_budget=1), Config(max_ticks=1)])
def test_resume_preserves_terminal_status_and_limits(tmp_path, config):
    expected = finish(Engine(config))
    app = create_app(ledger_path=tmp_path / "ledger.db", archive_path=tmp_path / "archive.db")
    with TestClient(app) as transport, WorldmindClient(client=transport) as client:
        session = client.create_session()
        session.import_recording(expected.export())
        session.resume()
        after = session.step()
        assert after["status"] == expected.status
        assert after["metrics"] == expected.metrics
        assert after["tick"] == expected.tick


def test_resume_preserves_short_ttl_observation_memory(tmp_path):
    expected = Engine(Config(evidence_ttl=2))
    while expected.active is None:
        expected.apply(Command(kind="step"))
        assert expected.tick < 15
    checkpoint, memory = expected.export(), set(expected.observation_attempts)
    assert memory
    finish(expected)
    app = create_app(ledger_path=tmp_path / "ledger.db", archive_path=tmp_path / "archive.db")
    with TestClient(app) as transport, WorldmindClient(client=transport) as client:
        session = client.create_session()
        session.import_recording(checkpoint)
        resumed = session.resume()
        assert resumed["actor"]["observation_attempts"] == sorted(memory)
        for _ in range(30):
            if session.step()["status"] == "success":
                break
        assert session.export() == expected.export()
