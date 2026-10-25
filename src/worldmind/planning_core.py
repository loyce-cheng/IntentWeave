"""Version-stable planning primitives shared by the live and replay policies.

Keep traversal order, budget charges and legacy context fields stable: recording
hashes depend on them. Version-specific decisions belong in their policy modules.
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from typing import Callable

from .domain import Action, GOALS, PLACES, ROADS, State, candidates, complete, dependencies, road, transition


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
           cap: int | None = None, max_depth: int = 30) -> list[Action] | None:
    """Shared searcher. cap limits only local attempts within the SAME budget."""
    queue = deque([(s, [])])
    seen = {s}
    stop = min(budget.limit, budget.used + cap) if cap is not None else budget.limit
    while queue and budget.used < stop:
        current, path = queue.popleft()
        budget.spend("expansions")
        if accept(current, path) if accept else complete(current, goal):
            return path
        if len(path) >= max_depth:
            continue
        for a in candidates(current):
            nxt = transition(current, a, goal)
            if nxt is not None and nxt not in seen:
                seen.add(nxt)
                queue.append((nxt, path + [a]))
    if queue and cap is None:
        raise Exhausted
    return None


def legacy_explore(ctx: Context, budget: Budget) -> list[Action] | None:
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


@dataclass(frozen=True)
class Boundary:
    """Necessary suffix-entry conditions, never a substitute for final validation.

    This finite domain has no action that opens roads or replenishes remote stock.
    Inventory can change in a patch, so constrain its *entry* value rather than
    rejecting a repair because the current actor lacks an item.
    """
    locations: frozenset[str]
    herb: tuple[int, int]
    medicine: tuple[int, int]
    stock: tuple[int, ...]
    needs_completed_goal: bool
    possible: bool = True

    def accepts(self, state: State, goal: str) -> bool:
        return (self.possible and state.location in self.locations
                and self.herb[0] <= state.herb <= self.herb[1]
                and self.medicine[0] <= state.medicine <= self.medicine[1]
                and all(required == 0 or (qty is not None and qty >= required)
                        for required, qty in zip(self.stock, state.stock))
                and (not self.needs_completed_goal or complete(state, goal)))


def suffix_boundary(ctx: Context, suffix: list[Action], budget: Budget) -> Boundary:
    """Regress necessary location/resource conditions, charging each action read.

    Temporal conditions intentionally remain in the full validator: a new patch
    changes arrival times, and a passing gate never certifies fresh evidence.
    """
    locations, location = set(PLACES), None
    hmin, hmax, mmin, mmax, dh, dm = 0, 2, 0, 2, 0, 0
    stock = [0] * len(PLACES)
    possible, delivered, barrier = True, False, False

    def open_road(origin: str, target: str) -> bool:
        key = road(origin, target)
        return key in ROADS and ctx.state.roads[ROADS.index(key)] is True

    for i, action in enumerate(suffix):
        budget.spend()
        if action.kind == "move":
            if location is None:
                locations.intersection_update(p for p in PLACES if open_road(p, action.target))
            elif not open_road(location, action.target):
                possible = False
            location = action.target
        else:
            if action.target != "self":
                if location is None:
                    locations.intersection_update([action.target])
                    location = action.target
                elif location != action.target:
                    possible = False
            if action.kind == "take":
                stock[PLACES.index(action.target)] += 1
                hmax = min(hmax, 1 - dh)
                dh += 1
            elif action.kind == "craft":
                possible &= action.target == "workshop"
                hmin, mmax = max(hmin, 1 - dh), min(mmax, 1 - dm)
                dh, dm = dh - 1, dm + 1
            elif action.kind == "deliver":
                destination, item = GOALS[ctx.goal]
                possible &= action.target == destination
                if item == "herb":
                    hmin, dh = max(hmin, 1 - dh), dh - 1
                else:
                    mmin, dm = max(mmin, 1 - dm), dm - 1
                delivered = True
            elif action.kind == "inspect":
                possible &= i == len(suffix) - 1
                barrier = True
    # No legal patch can replenish these stocks. Do not spend a search proving it.
    possible &= all(required == 0 or (qty is not None and qty >= required)
                    for required, qty in zip(stock, ctx.state.stock))
    possible &= bool(locations) and hmin <= hmax and mmin <= mmax
    return Boundary(frozenset(locations), (hmin, hmax), (mmin, mmax), tuple(stock),
                    not (delivered or barrier), possible)
