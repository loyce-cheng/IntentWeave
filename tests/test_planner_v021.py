"""Regressions for finite-budget repair and goal-directed observation barriers."""
from copy import deepcopy
from dataclasses import replace

import pytest

from worldmind import planner
from worldmind.domain import Action, Config, GOALS, ROADS, State
from worldmind.engine import Command, Engine, replay
from worldmind.planner import Budget, Context, decide, validate
from scripts.benchmark_v02 import CASES


class CheckedEngine(Engine):
    def _execute(self, action):
        # Independent per-action check at actual consumption time. A hypothetical
        # route must never be submitted as executable truth, even after expiry.
        assert validate(self.context(), [action], Budget(100), require_goal=False)[0]
        super()._execute(action)


def run(config, schedule=()):
    engine = CheckedEngine(config)
    for _ in range(config.max_ticks + 1):
        for tick, kind, target in schedule:
            if engine.tick == tick:
                engine.apply(Command(kind=kind, target=target))
        engine.apply(Command(kind="step"))
        if engine.status not in ("ready", "running"):
            return engine
    pytest.fail("runtime did not terminate")


@pytest.mark.parametrize("ttl", [2, 3, 4, 5])
@pytest.mark.parametrize("goal", GOALS)
@pytest.mark.parametrize("policy", ["Full", "Local"])
def test_short_evidence_reaches_goal_without_executing_unknown_or_expired_actions(ttl, goal, policy):
    engine = run(Config(evidence_ttl=ttl, goal=goal, policy=policy, max_ticks=40))
    assert engine.status == "success"
    assert engine.tick <= 16
    assert engine.metrics["consumed"] == engine.metrics["produced"]
    assert replay(engine.export())[0].state_hash() == engine.state_hash()


@pytest.mark.parametrize("ttl", [2, 3])
@pytest.mark.parametrize("policy", ["Full", "Local"])
@pytest.mark.parametrize("scenario", ["hidden_bridge", "merchant_competition"])
def test_short_evidence_with_disruptions(ttl, policy, scenario):
    overrides, schedule = CASES[scenario]
    engine = run(Config(policy=policy, evidence_ttl=ttl, max_ticks=40, **overrides), schedule)
    assert engine.status == "success"
    assert not any(e["type"] == "action_failed" for e in engine.events)


@pytest.mark.parametrize("scenario", CASES)
def test_declared_development_cases_have_no_local_budget_regression(scenario):
    overrides, schedule = CASES[scenario]
    full, local = [run(Config(policy=p, **overrides), schedule) for p in ("Full", "Local")]
    assert local.status == full.status
    assert local.tick == full.tick
    assert local.metrics["budget_used"] <= full.metrics["budget_used"]
    for engine in (full, local):
        m = engine.metrics
        assert m["budget_used"] == m["expansions"] + m["validations"]


def test_unknown_route_is_only_an_intent_and_preserves_evidence():
    roads = tuple(None if r == "bridge|home" else True for r in ROADS)
    ctx = Context(replace(State(), location="bridge", medicine=1, roads=roads),
                  "medicine_home", 10, {}, 1, 1)
    # Also hide the alternate path, so no certified complete route exists.
    roads = tuple(None if r in ("bridge|home", "ferry|home") else v for r, v in zip(ROADS, roads))
    ctx.state = replace(ctx.state, roads=roads)
    before = deepcopy(ctx)
    decision = decide(ctx, [], "Local", Budget(3000))
    assert decision.outcome == "observe"
    assert decision.plan == [Action(kind="inspect", target="bridge")]
    assert ctx == before
    assert validate(ctx, decision.plan, Budget(100))[0]


def test_failed_local_probes_share_full_frontier(monkeypatch):
    seen = set()
    from worldmind import planning_core
    original = planning_core.candidates

    def track(state):
        assert state not in seen, "a failed window restarted BFS"
        seen.add(state)
        return original(state)

    monkeypatch.setattr(planning_core, "candidates", track)
    ctx = Context(replace(State(), location="bridge", medicine=1),
                  "medicine_home", 7, {"road:bridge|home": 8}, 1, 1)
    # Retaining 28 waits cannot fit a 30-action splice or meet its TTL.
    previous = [Action(kind="move", target="home")]
    previous += [Action(kind="wait", target="self")] * 28
    previous += [Action(kind="deliver", target="home")]
    budget = Budget(300)
    plan, cut = planner.repair_or_full(ctx, previous, 0, budget)
    assert cut is None and len(plan) == 2
    assert validate(ctx, plan, Budget(100))[0]
    assert budget.used <= budget.limit


def test_short_ttl_hidden_worlds_still_have_identical_decisions_until_contact():
    a, b = Engine(Config(evidence_ttl=2)), Engine(Config(evidence_ttl=2))
    b.apply(Command(kind="block", target="bridge|home"))
    for _ in range(20):
        assert a.actor_view() == b.actor_view()
        assert a.context_digest() == b.context_digest()
        a.apply(Command(kind="step"))
        b.apply(Command(kind="step"))
        if a.world.location in ("bridge", "home"):
            assert a.actor_view() != b.actor_view()
            break
    else:
        pytest.fail("did not reach the observation boundary")


@pytest.mark.parametrize("policy", ["Full", "Local"])
@pytest.mark.parametrize("schedule", [
    [(0, "remove", "warehouse"), (0, "remove", "garden")],
    [(1, "block", "bridge|home"), (5, "block", "ferry|home")],
])
def test_short_ttl_unproductive_observation_cycles_terminate_as_unknown(policy, schedule):
    engine = run(Config(policy=policy, evidence_ttl=2, max_ticks=40), schedule)
    assert engine.status == "unknown"
    assert engine.last_repair["reason"] == "observation_cycle_without_progress"
    assert engine.tick < engine.config.max_ticks
    assert engine.observation_attempts
    assert replay(engine.export())[0].state_hash() == engine.state_hash()


def test_expired_negative_memory_cannot_prove_impossibility():
    roads = tuple(None if r == "bridge|home" else False for r in ROADS)
    ctx = Context(replace(State(), location="bridge", medicine=1, roads=roads),
                  "medicine_home", 10, {}, 1, 1, {"road:bridge|home": False})
    result = decide(ctx, [], "Local", Budget(3000))
    assert result.outcome == "observe"
    assert result.plan == [Action(kind="inspect", target="bridge")]
    assert ctx.state.roads[ROADS.index("bridge|home")] is None
