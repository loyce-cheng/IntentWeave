"""Bounded Full/Local planning over independent actor contexts, with observation barriers."""
from __future__ import annotations

from dataclasses import dataclass, replace
from heapq import heappop, heappush
from itertools import count

from .domain import PLACES, ROADS
from .planning_core import Budget, Exhausted
from .cooperation_domain import (CooperationAction as Action, Goal, KnownState,
                                 candidates, complete, dependencies, duration, transition)


@dataclass(frozen=True)
class Context:
    state: KnownState
    goal: Goal
    tick: int
    expires: dict[str, int]
    last_values: dict[str, int | bool]


def validate(ctx: Context, plan: list[Action], budget: Budget):
    state, elapsed = ctx.state, 0
    for i, action in enumerate(plan):
        budget.spend()
        for key in dependencies(state, action):
            if ctx.expires.get(key, 0) <= ctx.tick + elapsed:
                return False, i, "stale:" + key
        nxt = transition(state, action, ctx.goal)
        if nxt is None:
            return False, i, "precondition"
        elapsed += duration(action)
        if ctx.tick + elapsed - 1 > ctx.goal.deadline:
            return False, i, "deadline"
        if action.kind == "inspect":
            return i == len(plan) - 1, i, "observation_barrier"
        state = nxt
    return complete(state, ctx.goal), len(plan), "goal"


def search(ctx: Context, budget: Budget, suffixes=(), local_end=0):
    """One cost-ordered frontier for both repair and Full fallback.

    Earliest arrival dominates later visits to the same state: this finite model
    has only upper deadlines/expiry, no generated waiting or future replenishment.
    """
    serial = count()
    queue = [(0, next(serial), ctx.state, [])]
    seen = {ctx.state: 0}
    while queue:
        elapsed, _, state, path = heappop(queue)
        if elapsed != seen[state]:
            continue
        budget.spend("expansions")
        if ctx.tick + elapsed - 1 > ctx.goal.deadline:
            continue
        if complete(state, ctx.goal):
            return path, 0
        if budget.used < local_end:
            for suffix in suffixes:
                if len(path) + len(suffix) > 30:
                    continue
                limit = budget.limit
                try:
                    budget.limit = min(limit, local_end)
                    valid, _, _ = validate(ctx, path + suffix, budget)
                    if valid:
                        return path + suffix, len(suffix)
                except Exhausted:
                    break
                finally:
                    budget.limit = limit
        if len(path) >= 30:
            continue
        for action in candidates(state, ctx.goal):
            nxt = transition(state, action, ctx.goal)
            cost = elapsed + duration(action)
            if nxt is not None and cost < seen.get(nxt, 10**9):
                seen[nxt] = cost
                heappush(queue, (cost, next(serial), nxt, path + [action]))
    return None, 0


def prefix(ctx: Context, plan: list[Action], index: int, budget: Budget):
    state = ctx.state
    for action in plan[:index]:
        state = transition(state, action, ctx.goal)
    budget.spend()
    candidate = plan[:index] + [Action(kind="inspect", target=state.location)]
    # An inspection is still an action and must fit the same deadline.
    ok, _, _ = validate(ctx, candidate, budget)
    return candidate if ok else []


def decide(ctx: Context, previous: list[Action], policy: str, budget: Budget):
    reason, dirty = "", 0
    try:
        if previous:
            valid, dirty, reason = validate(ctx, previous, budget)
            if valid:
                return previous, "retained", reason, len(previous)
            if reason.startswith("stale:"):
                return prefix(ctx, previous, dirty, budget), "observe", reason, 0
        suffixes = []
        local_end = budget.used + (budget.limit - budget.used) // 4
        if policy == "Local" and previous:
            for end in sorted({min(len(previous), dirty + 1), min(len(previous), dirty + 3)}):
                if previous[end:]:
                    suffixes.append(previous[end:])
        plan, retained = search(ctx, budget, suffixes, local_end)
        if plan is not None:
            valid, index, why = validate(ctx, plan, budget)
            if valid:
                return plan, "patched" if retained else "fallback_full" if suffixes else "full", reason, retained
            return prefix(ctx, plan, index, budget), "observe", why, 0
        # Hypothetical facts choose where to inspect, never authorize execution.
        state = ctx.state
        tried = set()
        for remember in (True, False):
            roads = tuple((ctx.last_values.get("road:" + key) is not False if remember else True)
                          if v is None else v for key, v in zip(ROADS, state.roads))
            stock = tuple((int(ctx.last_values.get("stock:" + p, 1) > 0) if remember else 1)
                          if v is None else v for p, v in zip(PLACES, state.stock))
            hypothetical = replace(state, roads=roads, stock=stock)
            if hypothetical == state or hypothetical in tried:
                continue
            tried.add(hypothetical)
            plan, _ = search(replace(ctx, state=hypothetical), budget)
            if plan is not None:
                valid, index, why = validate(ctx, plan, budget)
                if valid:
                    return plan, "full", why, 0
                return prefix(ctx, plan, index, budget), "observe", why, 0
        return [], "unknown", "no_plan_in_current_belief", 0
    except Exhausted:
        return [], "budget_exhausted", "shared_decision_budget", 0
