from copy import deepcopy
from dataclasses import replace

import pytest
from pydantic import ValidationError

from worldmind.domain import Action, Config, PLACES, ROADS, State, transition
from worldmind.engine import Command, Engine, compare, replay
from worldmind.planner import Budget, Context, decide, validate
from .helpers import finish


@pytest.mark.parametrize("goal", ["medicine_home", "herb_clinic", "medicine_clinic"])
@pytest.mark.parametrize("policy", ["Full", "Local"])
def test_normal_delivery(goal, policy):
    e = finish(Engine(Config(goal=goal, policy=policy)))
    assert e.status == "success"
    assert e.world.delivered[tuple(["medicine_home", "herb_clinic", "medicine_clinic"]).index(goal)] == 1
    assert e.world.herb >= 0
    assert replay(e.export())[0].state_hash() == e.state_hash()


def test_hidden_changes_do_not_leak():
    a, b = Engine(), Engine()
    b.apply(Command(kind="block", target="bridge|home"))
    # The bridge cannot be observed from the first three visited locations.
    assert a.request_context() == b.request_context()
    assert a.context_digest() == b.context_digest()
    for _ in range(3):
        a.apply(Command(kind="step"))
        b.apply(Command(kind="step"))
        assert a.actor_view() == b.actor_view()
    finish(a)
    finish(b)
    assert b.status == "success"
    assert b.metrics["repairs"] + b.metrics["fallbacks"] > 0


def test_hidden_stock_released_only_at_contact():
    a, b = Engine(), Engine()
    b.apply(Command(kind="remove", target="garden"))
    assert a.request_context() == b.request_context()
    for _ in range(10):
        a.apply(Command(kind="step"))
        b.apply(Command(kind="step"))
        if a.world.location == "garden":
            assert a.context().state.stock != b.context().state.stock
            break
        assert a.actor_view() == b.actor_view()
    else:
        pytest.fail("did not reach observation point")


def test_local_repair_and_fair_budget():
    pair = compare(Config())
    assert all(r["status"] == "success" for r in pair.values())
    assert pair["Local"]["metrics"]["repairs"] > 0
    for result in pair.values():
        m = result["metrics"]
        assert m["budget_used"] == m["expansions"] + m["validations"]
        assert m["budget_used"] <= 30000
        replay(result["recording"])


def test_no_roads_no_oracle_and_terminates():
    e = Engine()
    for r in ROADS:
        e.apply(Command(kind="block", target=r))
    assert e.context().state.roads != e.world.roads
    finish(e)
    assert e.status == "unknown"
    assert "no_plan" in e.last_repair["reason"]


def test_no_material_and_budget_are_not_proof_of_impossibility():
    e = Engine()
    for p in ("warehouse", "garden"):
        e.apply(Command(kind="remove", target=p))
    finish(e)
    assert e.status in ("unknown", "budget_exhausted", "tick_limit")
    small = finish(Engine(Config(decision_budget=1, total_budget=1)))
    assert small.status == "budget_exhausted"
    assert small.metrics["budget_used"] == 1


def test_cancel_crafting_does_not_refund_or_finish():
    e = Engine()
    while not e.active:
        e.apply(Command(kind="step"))
        assert e.tick < 20
    assert e.world.herb == 0 and e.world.medicine == 0
    e.apply(Command(kind="cancel"))
    e.apply(Command(kind="step"))
    assert e.active is None
    assert e.world.herb == e.world.medicine == 0
    assert e.metrics["consumed"] == 1 and e.metrics["produced"] == 0
    replay(e.export())


def test_change_goal_invalidates_generation():
    e = Engine()
    old = e.context_digest()
    e.apply(Command(kind="goal", target="herb_clinic"))
    assert old != e.context_digest()
    assert e.goal_version == 2
    assert finish(e).status == "success"


def test_fixed_merchant_competes_without_duplicate_stock():
    e = finish(Engine(Config(merchant_takes_at=2, goal="herb_clinic")))
    assert e.status == "success"
    assert e.merchant["herb"] == 1
    assert sum(e.world.stock) + e.world.herb + e.merchant["herb"] + e.world.delivered[1] == 2
    assert e.metrics["repairs"] + e.metrics["fallbacks"] >= 1
    assert replay(e.export())[0].merchant == e.merchant


def test_evidence_expiry_unknown_not_false():
    e = Engine(Config(evidence_ttl=2))
    e.tick = 3
    assert e.context().state.stock[PLACES.index("garden")] is None
    assert e.evidence["stock:garden"]["value"] == 1


def test_semantic_validation_rejects_teleport_and_bad_suffix():
    e = Engine()
    invalid = [Action(kind="move", target="home")]
    assert not validate(e.context(), invalid, Budget(100))[0]
    # A locally plausible move is not a complete goal plan.
    assert not validate(e.context(), [Action(kind="move", target="workshop")], Budget(100))[0]
    e.apply(Command(kind="proposal", actions=invalid))
    assert e.events[-1]["type"] == "proposal_rejected"
    assert e.world.location == "clinic"


@pytest.mark.parametrize("obj", [
    {"kind": "move", "target": "moon"}, {"kind": "take", "target": "garden", "quantity": -1},
    {"kind": "exec", "target": "clinic"}, {"kind": "wait", "target": 1},
])
def test_action_syntax(obj):
    with pytest.raises(ValidationError):
        Action.model_validate(obj)


def test_transition_cannot_remote_take():
    assert transition(State(), Action(kind="take", target="garden"), "medicine_home") is None


def test_replay_step_verification_and_corruption():
    e = finish(Engine())
    data = e.export()
    for i in range(len(data["steps"]) + 1):
        _, result = replay(data, i)
        assert result["verified"] == i
    for field in ("state_hash", "event_hash"):
        bad = deepcopy(data)
        bad["steps"][1][field] = "0" * 64
        with pytest.raises(ValueError, match="step 2"):
            replay(bad)
    bad = deepcopy(data)
    bad["schema_version"] = "future"
    with pytest.raises(ValueError):
        replay(bad)


def test_same_input_identical_logical_state():
    a, b = finish(Engine()), finish(Engine())
    assert a.export() == b.export()


def test_local_can_fallback_and_budget_includes_validation():
    e = Engine()
    previous = [Action(kind="take", target="garden"), Action(kind="deliver", target="home")]
    budget = Budget(3000)
    result = decide(e.context(), previous, "Local", budget)
    assert result.outcome in ("patched", "fallback_full")
    assert validate(e.context(), result.plan, Budget(3000))[0]
    assert budget.validations > 0 and budget.expansions > 0
