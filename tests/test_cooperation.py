"""Cooperation behavior, resource accounting, observation boundaries and exact continuation."""
from copy import deepcopy
from dataclasses import asdict

import pytest
from fastapi.testclient import TestClient

from worldmind.api import create_app
from worldmind.archives import ArchiveStore
from worldmind.cooperation import CooperationEngine, CooperationRecording
from worldmind.cooperation_domain import (CooperationConfig, CooperationCommand as Command,
                                          CooperationAction as Action, PROTOCOL)
from worldmind.domain import canonical, digest, GOALS
from worldmind.orders import WorkOrder
from worldmind.runtime import replay, compare
from worldmind.sdk import WorldmindClient, SDKError


def engine(**options):
    return CooperationEngine(CooperationConfig(scenario="cooperation", **options))


def finish(e):
    for _ in range(e.config.max_ticks + 2):
        if e.status not in ("ready", "running"):
            return e
        e.advance(Command(kind="step"))
    pytest.fail("cooperation did not terminate")


@pytest.mark.parametrize("policy", ("Full", "Local"))
@pytest.mark.parametrize("goal", tuple(GOALS))
def test_two_planners_deliver_real_resources(policy, goal):
    e = finish(engine(policy=policy, goal=goal))
    assert e.status == "success"
    assert e.order.status == "fulfilled" and e.order.fulfilled_tick <= e.config.order_deadline
    assert e.actors["doctor"].delivered
    assert all(e.actors[a].metrics["decisions"] > 0 for a in ("doctor", "courier"))
    transfers = [x for x in e.events if x["type"] == "resource_transferred"]
    assert len(transfers) == 1 and transfers[0]["quantity"] == 1
    assert e.deliveries[GOALS[goal][1]] == 1
    assert not e.claims and all(a.active is None for a in e.actors.values())
    assert replay(e.export())[0].export() == e.export()


def test_hidden_changes_do_not_change_either_actor_context_or_decision():
    original, hidden = engine(), engine()
    hidden.advance(Command(kind="block", target="bridge|home"))
    hidden.advance(Command(kind="remove", target="garden"))
    for _ in range(2):
        for aid in ("courier", "doctor"):
            assert original.actor_view(aid) == hidden.actor_view(aid)
        original.advance(Command(kind="step"))
        hidden.advance(Command(kind="step"))
    for aid in ("courier", "doctor"):
        view = canonical(hidden.actor_view(aid))
        assert all(key not in view for key in ('"seq"', '"state_hash"', '"claims"', '"messages"'))


def test_delayed_order_is_not_inventory_and_waiting_does_not_search():
    e = engine(message_delay=5)
    for _ in range(4):
        e.advance(Command(kind="step"))
        assert e.actors["doctor"].herb == e.actors["doctor"].medicine == 0
        assert not e.actors["courier"].known_orders
    decisions = e.actors["doctor"].metrics["decisions"]
    e.advance(Command(kind="step"))
    assert e.actors["doctor"].metrics["decisions"] == decisions
    assert e.actors["doctor"].known_orders["supply-1"]["status"] == "proposed"
    assert e.actors["courier"].known_orders["supply-1"]["status"] == "in_progress"
    finish(e)
    assert e.status == "success"


def test_competing_requests_reserve_only_one_unit_then_release():
    e = engine()
    e.actors["courier"].location = "warehouse"
    action = Action(kind="take", target="warehouse")
    e._start("courier", action)
    e._start("merchant", action)
    assert list(e.claims) == ["courier"]
    assert e.stock[1] == 1  # Reservation is not another copy or early consumption.
    assert e.actors["merchant"].active is None
    e._interrupt("courier")
    e._start("merchant", action)
    e.tick += 1
    e._finish("merchant")
    assert e.stock[1] == 0 and e.actors["merchant"].herb == 1 and not e.claims
    e._invariants()


def test_competitor_uses_same_arbiter_and_actor_recovers_from_denial():
    e = engine(policy="Full", merchant_takes_at=2)
    e.advance(Command(kind="block", target="garden|warehouse"))
    finish(e)
    assert e.status == "success" and e.actors["merchant"].herb == 1
    assert any(x["type"] == "action_denied" and x["actor_id"] == "courier" for x in e.events)
    assert e.deliveries["medicine"] == 1 and e.stock == [0] * 7
    assert replay(e.export())[0].export() == e.export()


def test_late_old_order_message_cannot_replace_fulfillment():
    e = engine(message_delay=5)
    old = asdict(e.order)
    finish(e)
    before = e.actor_view("doctor")
    e._known_order("doctor", old)
    assert e.actor_view("doctor") == before


@pytest.mark.parametrize("point", ("take", "give", "craft"))
def test_every_active_action_restores_without_duplicate_effects(tmp_path, point):
    e = engine()
    for _ in range(30):
        e.advance(Command(kind="step"))
        if any(a.active and a.active["action"]["kind"] == point for a in e.actors.values()):
            break
    else:
        pytest.fail("missing action phase")
    source = e.export()
    store = ArchiveStore(tmp_path / "archives.db")
    receipt = store.create(point, source)
    restored, result, recording = ArchiveStore(tmp_path / "archives.db").restore(receipt["id"], receipt["token"])
    assert result["complete"] and recording == source
    assert restored.state_hash() == e.state_hash()
    assert finish(restored).export() == finish(e).export()
    assert store.load(receipt["id"], receipt["token"])["recording"] == source


def test_handover_cancel_and_craft_interrupt_do_not_refund_or_transfer():
    e = engine()
    while not (e.actors["courier"].active and e.actors["courier"].active["action"]["kind"] == "give"):
        assert e.tick < 30
        e.advance(Command(kind="step"))
    e.advance(Command(kind="order_cancel", target="supply-1"))
    assert e.order.status == "cancelled" and not e.claims
    finish(e)
    assert not e.actors["doctor"].delivered
    assert e.actors["courier"].herb == 1
    assert not any(x["type"] == "resource_transferred" for x in e.events)
    assert replay(e.export())[0].export() == e.export()

    e = engine()
    while not e.actors["doctor"].active:
        assert e.tick < 30
        e.advance(Command(kind="step"))
    assert e.metrics["consumed"] == 1
    e.advance(Command(kind="interrupt", target="doctor"))
    finish(e)
    assert e.metrics["produced"] == 0 and e.metrics["consumed"] == 1
    assert not e.claims
    assert replay(e.export())[0].export() == e.export()


def test_deadline_includes_completion_tick_and_late_delivery_is_not_success():
    baseline = finish(engine())
    due = baseline.order.fulfilled_tick
    exact = finish(engine(order_deadline=due))
    assert exact.order.status == "fulfilled" and exact.order.fulfilled_tick == due
    early = finish(engine(order_deadline=due - 1))
    assert early.order.status == "expired" and early.order.breached
    assert early.status != "success"
    assert not early.claims


@pytest.mark.parametrize("options", ({"evidence_ttl": 2}, {"message_delay": 7},
                                    {"merchant_takes_at": 1}, {"actor_budget": 1},
                                    {"total_budget": 1}, {"accept_order": False}, {"max_ticks": 3}))
def test_bounded_variations_are_replayable(options):
    for policy in ("Full", "Local"):
        e = finish(engine(policy=policy, **options))
        assert e.tick <= e.config.max_ticks
        assert not e.claims
        assert e.metrics["budget_used"] <= e.config.total_budget
        assert all(a.metrics["budget_used"] <= e.config.actor_budget for a in e.actors.values())
        assert replay(e.export())[0].export() == e.export()
        if set(options) <= {"evidence_ttl", "message_delay", "merchant_takes_at"}:
            assert e.status == "success"


def test_corrupt_recording_and_relabelled_protocol_are_rejected():
    data = finish(engine()).export()
    bad = deepcopy(data)
    bad["steps"][1]["input"] = {"kind": "remove", "target": "warehouse"}
    with pytest.raises(ValueError, match="divergence"):
        replay(bad)
    bad = deepcopy(data)
    bad["schema_version"] = "worldmind-0.2.1/river-1"
    with pytest.raises(ValueError):
        replay(bad)
    for cursor in range(len(data["steps"]) + 1):
        partial, _ = replay(data, cursor)
        for step in data["steps"][cursor:]:
            partial.advance(Command.model_validate(step["input"]))
        assert partial.export() == data


def test_order_terms_and_terminal_states_cannot_be_rewritten():
    order = WorkOrder(20)
    order.change("accepted", 1)
    order.quantity = 2
    with pytest.raises(ValueError, match="terms_changed"):
        order.change("in_progress", 2)
    order.quantity = 1
    order.change("cancelled", 2)
    frozen = asdict(order)
    with pytest.raises(ValueError, match="invalid_order_transition"):
        order.change("fulfilled", 3)
    assert asdict(order) == frozen


def test_sdk_api_archive_branch_idempotency_and_scenario_dispatch(tmp_path):
    app = create_app(ledger_path=tmp_path / "ledger.db", archive_path=tmp_path / "archives.db")
    with TestClient(app) as transport, WorldmindClient(client=transport) as client:
        source = client.create_session(CooperationConfig(scenario="cooperation"))
        first = source.step()
        original_recording = source.export()
        assert set(first["actors"]) == {"courier", "doctor", "merchant"}
        assert source.actor("doctor")["actor_id"] == "doctor"
        receipt = source.save_archive("cooperation")
        target = client.create_session()
        target.restore_archive(receipt)
        assert target.view()["version"] == PROTOCOL
        target.seek(0)
        revision = target.view()["revision"]
        resumed = target.resume(command_id="resume-coop", expected_revision=revision)
        assert target.resume(command_id="resume-coop", expected_revision=revision) == resumed
        result = target.command("step", command_id="step-coop", expected_revision=resumed["revision"])
        assert target.command("step", command_id="step-coop", expected_revision=resumed["revision"]) == result
        assert result["state_hash"] == first["state_hash"]
        assert target.export()["lineage"] == {"parent_recording_hash": digest(original_recording), "cursor": 0}
        for _ in range(30):
            if target.view()["status"] != "running":
                break
            target.step()
        assert target.view()["status"] == "success"
        assert replay(target.export())[1]["complete"]
        assert source.export() == original_recording
        river = client.create_session()
        with pytest.raises(SDKError) as error:
            river.command("order_cancel", "supply-1")
        assert error.value.status == 422
        # Exact retries retain their original receipt after changing scenario.
        cancelled = source.command("order_cancel", "supply-1", command_id="cancel-order", expected_revision=1)
        source.import_recording(river.export())
        assert source.command("order_cancel", "supply-1", command_id="cancel-order", expected_revision=1) == cancelled
        for s in (source, target, river):
            s.delete()


def test_paired_comparison_has_same_config_budget_and_external_inputs():
    results = compare(CooperationConfig(scenario="cooperation"))
    for result in results.values():
        assert result["status"] == "success"
        rec = CooperationRecording.model_validate(result["recording"])
        assert rec.steps[1].input == Command(kind="block", target="bridge|home")
        assert replay(result["recording"])[1]["complete"]
    full, local = (results[p]["recording"]["config"] for p in ("Full", "Local"))
    assert {k: v for k, v in full.items() if k != "policy"} == {k: v for k, v in local.items() if k != "policy"}
