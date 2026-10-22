"""Deterministic bounded BFS and continuous-window repair over ActorContext only."""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from typing import Callable

from ..domain import Action, PLACES, ROADS, State, candidates, complete, dependencies, transition


class Exhausted(Exception):
    pass


@dataclass
class Budget:
    limit: int
    used: int = 0
    expansions: int = 0
    validations: int = 0

    def spend(self, kind: str = "validations"):
        if self.used >= self.limit:
            raise Exhausted
        self.used += 1
        setattr(self, kind, getattr(self, kind) + 1)


@dataclass
class Context:
    state: State
    goal: str
    tick: int
    expires: dict[str, int]
    knowledge_version: int
    goal_version: int


def validate(ctx: Context, plan: list[Action], budget: Budget,
             require_goal: bool = True) -> tuple[bool, int, str]:
    s, tick = ctx.state, ctx.tick
    for i, a in enumerate(plan):
        budget.spend()
        # Predicted consumption changes amounts, but cannot refresh old remote evidence.
        for key in dependencies(s, a):
            if key in ctx.expires and ctx.expires[key] <= tick:
                return False, i, "evidence_expired:" + key
        nxt = transition(s, a, ctx.goal)
        if nxt is None:
            return False, i, "precondition:" + ",".join(dependencies(s, a))
        if a.kind == "inspect":
            return (i == len(plan) - 1), i, "observation_barrier"
        s = nxt
        tick += 2 if a.kind == "craft" else 1
    return (not require_goal or complete(s, ctx.goal)), len(plan), "goal"


def search(s: State, goal: str, budget: Budget,
           accept: Callable[[State, list[Action]], bool] | None = None,
           cap: int | None = None) -> list[Action] | None:
    """Shared searcher. cap limits only local attempts within the SAME budget."""
    queue = deque([(s, [])])
    seen = {s}
    stop = min(budget.limit, budget.used + cap) if cap is not None else budget.limit
    while queue and budget.used < stop:
        current, path = queue.popleft()
        budget.spend("expansions")
        if accept(current, path) if accept else complete(current, goal):
            return path
        if len(path) >= 30:
            continue
        for a in candidates(current):
            nxt = transition(current, a, goal)
            if nxt is not None and nxt not in seen:
                seen.add(nxt)
                queue.append((nxt, path + [a]))
    if queue and cap is None:
        raise Exhausted
    return None


def explore(ctx: Context, budget: Budget) -> list[Action] | None:
    """Reach a known frontier then inspect; never predict an unknown observation."""
    def frontier(s: State, path: list[Action]) -> bool:
        budget.spend()
        return s.stock[PLACES.index(s.location)] is None or any(
            s.location in key.split("|") and s.roads[i] is None for i, key in enumerate(ROADS))
    path = search(ctx.state, ctx.goal, budget, frontier)
    if path is not None:
        end = ctx.state
        for a in path:
            end = transition(end, a, ctx.goal)
        return path + [Action(kind="inspect", target=end.location)]
    return None


@dataclass
class Decision:
    plan: list[Action] = field(default_factory=list)
    outcome: str = "unknown"
    reason: str = ""
    removed: int = 0
    retained: int = 0
    window: list[int] | None = None


def decide(ctx: Context, previous: list[Action], policy: str, budget: Budget) -> Decision:
    result = Decision()
    try:
        if previous:
            valid, dirty, reason = validate(ctx, previous, budget)
            if valid:
                return Decision(previous, "retained", reason, retained=len(previous))
            result.reason = reason
            if policy == "Local":
                # Replace [0,r): completed actions are outside the remaining plan.
                # Whole-suffix validation is charged for every proposed splice.
                local_end = min(budget.limit, budget.used + max(1, (budget.limit-budget.used)//4))
                for r in sorted(set([min(len(previous), dirty + 1),
                                     min(len(previous), dirty + 3), len(previous)])):
                    suffix = previous[r:]
                    allowance = local_end - budget.used
                    if allowance <= 0:
                        break
                    def accept(s, path):
                        if budget.used >= local_end:
                            return False
                        ok, _, _ = validate(ctx, path + suffix, budget)
                        return ok
                    original_limit = budget.limit
                    try:
                        budget.limit = local_end
                        patch = search(ctx.state, ctx.goal, budget, accept, allowance)
                    except Exhausted:
                        patch = None
                    finally:
                        budget.limit = original_limit
                    if patch is not None:
                        return Decision(patch + suffix, "patched", reason, r, len(suffix), [0, r])
                result.outcome = "fallback_full"
        path = search(ctx.state, ctx.goal, budget)
        if path is not None:
            valid, index, reason = validate(ctx, path, budget)
            if valid:
                return Decision(path, "fallback_full" if previous and policy == "Local" else "full",
                                result.reason or reason, len(previous))
            # Expiring remote evidence: certify only a prefix up to an observation.
            prefix = path[:index]
            s = ctx.state
            for a in prefix:
                s = transition(s, a, ctx.goal)
            return Decision(prefix + [Action(kind="inspect", target=s.location)],
                            "observe", reason, len(previous))
        observation = explore(ctx, budget)
        if observation:
            return Decision(observation, "observe", "unknown facts", len(previous))
        return Decision([], "unknown", "no_plan_in_current_belief", len(previous))
    except Exhausted:
        return Decision([], "budget_exhausted", "shared decision budget exhausted", len(previous))
