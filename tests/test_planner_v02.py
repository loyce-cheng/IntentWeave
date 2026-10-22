"""Behavioral checks for boundary-guided repair, using the shared action model as oracle."""
from dataclasses import replace
import random

import pytest

from worldmind.compat import planner_v01
from worldmind.domain import Action, PLACES, ROADS, State, candidates, transition
from worldmind.planner import Budget, Context, decide, suffix_boundary, validate


def context(state=None, expires=None):
    return Context(state or State(), "medicine_home", 7, expires or {}, 1, 1)


def bridge_context(expires=None):
    roads = list(State().roads)
    roads[ROADS.index("bridge|home")] = False
    return context(replace(State(), location="bridge", medicine=1, roads=tuple(roads)), expires)


def delivery_suffix():
    return [Action(kind="move", target="home"), Action(kind="deliver", target="home")]


def test_boundary_guided_splice_reduces_repeated_validation_without_changing_result():
    ctx = bridge_context()
    old_budget, budget = planner_v01.Budget(3000), Budget(3000)
    old = planner_v01.decide(ctx, delivery_suffix(), "Local", old_budget)
    new = decide(ctx, delivery_suffix(), "Local", budget)
    assert new.outcome == old.outcome == "patched"
    assert new.plan == old.plan
    assert validate(ctx, new.plan, Budget(3000))[0]
    assert budget.used < old_budget.used
    assert budget.validations < old_budget.validations
    assert budget.used == budget.expansions + budget.validations


def test_boundary_requires_entry_resources_not_resources_at_search_start():
    ctx = context(replace(State(), location="garden"))
    previous = [Action(kind="move", target="clinic"),  # no direct edge from garden
                Action(kind="craft", target="workshop"),
                Action(kind="move", target="bridge"), *delivery_suffix()]
    result = decide(ctx, previous, "Local", Budget(3000))
    assert result.outcome == "patched"
    assert Action(kind="take", target="garden") in result.plan
    assert validate(ctx, result.plan, Budget(3000))[0]


def test_whole_splice_still_checks_evidence_expired_during_detour():
    ctx = bridge_context({"road:ferry|workshop": 8})
    result = decide(ctx, delivery_suffix(), "Local", Budget(3000))
    assert result.outcome == "observe"
    assert result.plan[-1].kind == "inspect"
    assert validate(ctx, result.plan, Budget(3000))[0]
    assert result.reason.startswith("evidence_expired:")


def test_boundary_does_not_certify_temporal_or_goal_validity():
    ctx = context(replace(State(), location="workshop", herb=1), {"road:bridge|workshop": 8})
    suffix = [Action(kind="craft", target="workshop"),
              Action(kind="move", target="bridge"), *delivery_suffix()]
    budget = Budget(100)
    gate = suffix_boundary(ctx, suffix, budget)
    assert gate.accepts(ctx.state, ctx.goal)
    assert budget.validations == len(suffix)
    assert not validate(ctx, suffix, Budget(100))[0]
    unfinished = suffix_boundary(ctx, [Action(kind="wait", target="self")], Budget(100))
    assert not unfinished.accepts(ctx.state, ctx.goal)


def test_suffix_constraints_reject_capacity_resource_and_stock_conflicts():
    ctx = context(replace(State(), location="workshop", herb=1, medicine=2))
    suffix = [Action(kind="craft", target="workshop"),
              Action(kind="move", target="bridge"), *delivery_suffix()]
    assert not suffix_boundary(ctx, suffix, Budget(100)).accepts(ctx.state, ctx.goal)
    overdrawn = [Action(kind="take", target="garden"),
                 Action(kind="take", target="garden"), Action(kind="inspect", target="garden")]
    assert not suffix_boundary(context(), overdrawn, Budget(100)).possible
    unknown = replace(State(), stock=(0, None, None, 0, 0, 0, 0))
    assert not suffix_boundary(context(unknown), overdrawn[1:], Budget(100)).possible


def test_empty_retained_suffix_does_not_duplicate_full_search():
    ctx = context()
    previous = [Action(kind="move", target="home")]
    full_budget, local_budget = Budget(3000), Budget(3000)
    full = decide(ctx, previous, "Full", full_budget)
    local = decide(ctx, previous, "Local", local_budget)
    assert local.outcome == "fallback_full"
    assert full.plan == local.plan
    assert full_budget.used == local_budget.used


@pytest.mark.parametrize("limit", [1, 2, 4, 8, 16, 32, 64])
def test_boundary_work_and_fallback_stay_inside_one_budget(limit):
    ctx, budget = bridge_context(), Budget(limit)
    result = decide(ctx, delivery_suffix(), "Local", budget)
    assert budget.limit == limit  # local allowance never leaks into the caller
    assert budget.used <= limit
    assert budget.used == budget.expansions + budget.validations
    if result.plan:
        assert validate(ctx, result.plan, Budget(3000))[0]
    else:
        assert result.outcome in ("unknown", "budget_exhausted")


def test_splice_respects_total_plan_length_cap():
    previous = [Action(kind="move", target="home")]
    previous += [Action(kind="wait", target="self")] * 28
    previous += [Action(kind="deliver", target="home")]
    ctx = bridge_context()
    result = decide(ctx, previous, "Local", Budget(3000))
    assert result.plan and len(result.plan) <= 30
    assert validate(ctx, result.plan, Budget(3000))[0]


def test_boundary_never_rejects_valid_continuations_in_frozen_finite_sample():
    """Generate valid trajectories with transition(), independently of regression rules."""
    rng = random.Random(20261021)
    for _ in range(120):
        initial = State(location=rng.choice(PLACES), herb=rng.randrange(3),
                        medicine=rng.randrange(3), stock=tuple(rng.randrange(3) for _ in PLACES),
                        roads=tuple(rng.choice((False, True, True)) for _ in ROADS))
        state, plan, states = initial, [], [initial]
        for _ in range(rng.randrange(3, 13)):
            legal = [(a, transition(state, a, "medicine_home")) for a in candidates(state)]
            legal = [(a, s) for a, s in legal if s is not None]
            if not legal:
                break
            action, state = rng.choice(legal)
            plan.append(action)
            states.append(state)
        plan.append(Action(kind="inspect", target=state.location))
        ctx = context(initial)
        assert validate(ctx, plan, Budget(3000))[0]
        split = rng.randrange(len(plan))
        gate = suffix_boundary(ctx, plan[split:], Budget(3000))
        assert gate.accepts(states[split], ctx.goal), (initial, plan, split)
