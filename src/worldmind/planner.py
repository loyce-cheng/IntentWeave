"""Deterministic bounded BFS and continuous-window repair over ActorContext only."""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field, replace
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
    # Remembered observations rank exploratory routes only. They never turn an
    # expired fact back into a known precondition or extend its validity.
    last_values: dict[str, int | bool] = field(default_factory=dict)


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


def observation_prefix(ctx: Context, path: list[Action], index: int,
                       budget: Budget) -> list[Action]:
    """The caller has certified path[:index]; never install its hypothetical tail."""
    prefix, state = path[:index], ctx.state
    for action in prefix:
        state = transition(state, action, ctx.goal)
    budget.spend()  # certify the local inspection appended to the checked prefix
    return prefix + [Action(kind="inspect", target=state.location)]


def known_goal_possible(ctx: Context, budget: Budget) -> bool:
    """Necessary reachability/resource test shared by Full and Local.

    Each visited map node is charged. False only rules out a plan in the current
    belief; unknown roads/stock still warrant goal-directed observation.
    """
    # The static river map is connected. With every edge known open there is
    # no graph traversal to do (nor any graph expansion to charge).
    connected = all(value is True for value in ctx.state.roads)
    reached = set(PLACES) if connected else {ctx.state.location}
    queue = deque([] if connected else [ctx.state.location])
    while queue:
        place = queue.popleft()
        budget.spend("expansions")
        for i, key in enumerate(ROADS):
            ends = key.split("|")
            if place in ends and ctx.state.roads[i] is True:
                other = ends[1] if ends[0] == place else ends[0]
                if other not in reached:
                    reached.add(other)
                    queue.append(other)
    destination, item = GOALS[ctx.goal]
    has_herb = ctx.state.herb > 0 or any(
        p in reached and qty is not None and qty > 0
        for p, qty in zip(PLACES, ctx.state.stock))
    return complete(ctx.state, ctx.goal) or (destination in reached and (
        has_herb if item == "herb" else
        ctx.state.medicine > 0 or (has_herb and "workshop" in reached)))


def explore(ctx: Context, budget: Budget) -> list[Action] | None:
    """Select useful observations using an explicitly hypothetical goal route.

    Unknowns may guide *where to inspect*, never an executable action. Validate
    against the unchanged real belief and stop BEFORE the first unknown/expired
    dependency. Known false roads and empty stocks are never relaxed. Unlike the
    old nearest-unknown rule, expired irrelevant stocks cannot distract a courier
    already carrying medicine from reaching the patient.
    """
    optimistic_roads = tuple(True if v is None else v for v in ctx.state.roads)
    cautious_roads = tuple(ctx.last_values.get("road:" + key) is not False if v is None else v
                           for key, v in zip(ROADS, ctx.state.roads))
    remembered_stock = tuple((1 if ctx.last_values.get("stock:" + place, 1) > 0 else 0)
                             if v is None else v for place, v in zip(PLACES, ctx.state.stock))
    optimistic_stock = tuple(1 if v is None else v for v in ctx.state.stock)
    # Fresh sources first, then remembered sources, then other unknown sources.
    # Prefer routes avoiding a previously observed closure; if those fail, also
    # inspect that route. Expired negative evidence must not prove impossibility.
    tried = set()
    for roads, stock in ((cautious_roads, ctx.state.stock),
                         (cautious_roads, remembered_stock),
                         (cautious_roads, optimistic_stock),
                         (optimistic_roads, optimistic_stock)):
        hypothetical = replace(ctx.state, roads=roads, stock=stock)
        if hypothetical == ctx.state or hypothetical in tried:
            continue
        tried.add(hypothetical)
        path = search(hypothetical, ctx.goal, budget)
        if path is not None:
            valid, index, _ = validate(ctx, path, budget)
            return path if valid else observation_prefix(ctx, path, index, budget)
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


def repair_or_full(ctx: Context, previous: list[Action], dirty: int,
                   budget: Budget) -> tuple[list[Action] | None, int | None]:
    """Probe suffixes on ONE BFS frontier, then continue it as Full on failure.

    Boundary construction and failed splice checks share a quarter-budget cap.
    Expansions are never repeated at another window or when falling back. A full
    solution found along the way wins immediately. Final splice validation still
    checks all resources, time and evidence, including the new patch.
    """
    local_end = budget.used + (budget.limit - budget.used) // 4
    boundaries = []
    original_limit = budget.limit
    try:
        budget.limit = local_end
        for r in sorted({min(len(previous), dirty + 1),
                         min(len(previous), dirty + 3)}, reverse=True):
            suffix = previous[r:]
            if suffix:
                boundary = suffix_boundary(ctx, suffix, budget)
                if boundary.possible:
                    boundaries.append((r, suffix, boundary))
    except Exhausted:
        pass
    finally:
        budget.limit = original_limit
    chosen = None

    def accept(state, path):
        nonlocal chosen
        if complete(state, ctx.goal):
            return True
        if budget.used >= local_end:
            return False
        for r, suffix, boundary in boundaries:
            if len(path) + len(suffix) > 30 or not boundary.accepts(state, ctx.goal):
                continue
            try:
                budget.limit = local_end
                valid, _, _ = validate(ctx, path + suffix, budget)
            except Exhausted:
                return False
            finally:
                budget.limit = original_limit
            if valid:
                chosen = r
                return True
        return False

    path = search(ctx.state, ctx.goal, budget, accept)
    return (path + previous[chosen:] if chosen is not None else path), chosen


def decide(ctx: Context, previous: list[Action], policy: str, budget: Budget) -> Decision:
    reason, dirty = "", 0
    try:
        if previous:
            valid, dirty, reason = validate(ctx, previous, budget)
            if valid:
                return Decision(previous, "retained", reason, retained=len(previous))
            if reason.startswith("evidence_expired:"):
                # Keep the certified progress; repair cannot make evidence fresher.
                return Decision(observation_prefix(ctx, previous, dirty, budget),
                                "observe", reason, len(previous))
        path, cut = None, None
        if known_goal_possible(ctx, budget):
            if previous and policy == "Local":
                path, cut = repair_or_full(ctx, previous, dirty, budget)
            else:
                path = search(ctx.state, ctx.goal, budget)
        if cut is not None:
            return Decision(path, "patched", reason, cut, len(previous) - cut, [0, cut])
        if path is not None:
            valid, index, validation_reason = validate(ctx, path, budget)
            if valid:
                return Decision(path, "fallback_full" if previous and policy == "Local" else "full",
                                reason or validation_reason, len(previous))
            return Decision(observation_prefix(ctx, path, index, budget),
                            "observe", validation_reason, len(previous))
        observation = explore(ctx, budget)
        if observation:
            return Decision(observation, "observe", "goal-directed observation", len(previous))
        return Decision([], "unknown", "no_plan_in_current_belief", len(previous))
    except Exhausted:
        return Decision([], "budget_exhausted", "shared decision budget exhausted", len(previous))
