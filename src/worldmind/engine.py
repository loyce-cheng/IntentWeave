"""Single-writer deterministic river demo; privileged state never enters Context."""
from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict, replace
from time import process_time_ns
from typing import Literal

from pydantic import Field, model_validator

from .domain import (Action, Config, GOALS, INITIAL_STOCK, PLACES, ROADS, VERSION,
                     State, StrictModel, complete, dependencies, digest, transition)
from .planner import Budget, Context, Exhausted, decide, validate


class Command(StrictModel):
    kind: Literal["step", "block", "remove", "goal", "cancel", "proposal"]
    target: str | None = Field(default=None, max_length=64)
    actions: list[Action] | None = Field(default=None, max_length=30)

    @model_validator(mode="after")
    def valid(self):
        if self.kind == "block" and self.target not in ROADS:
            raise ValueError("unknown road")
        if self.kind == "remove" and self.target not in PLACES:
            raise ValueError("unknown stock location")
        if self.kind == "goal" and self.target not in GOALS:
            raise ValueError("unknown goal")
        if self.kind in ("step", "cancel", "proposal") and self.target is not None:
            raise ValueError("unexpected target")
        if (self.kind == "proposal") != (self.actions is not None):
            raise ValueError("actions required only for proposal")
        if self.kind == "proposal" and not self.actions:
            raise ValueError("empty proposal")
        return self


class Engine:
    def __init__(self, config: Config | None = None):
        self.config = config or Config()
        self.world = State()
        self.tick = 0
        self.goal = self.config.goal
        self.goal_version = 1
        self.plan_version = 0
        self.generation = 0
        self.knowledge_version = 0
        self.plan: list[Action] = []
        self.status = "ready"
        self.active: dict | None = None
        self.merchant = {"location": "warehouse", "herb": 0, "policy": "fixed", "acted": False}
        self.evidence: dict[str, dict] = {}
        self.events: list[dict] = []
        self.trace: list[dict] = []
        self.metrics = dict(expansions=0, validations=0, budget_used=0, decisions=0,
                            repairs=0, fallbacks=0, actions=0, consumed=0, produced=0)
        self.cpu_ms = 0.0
        self.last_repair: dict = {}
        # Scenario-granted, dated delivery briefing; independent of actual hidden state.
        for r in ROADS:
            self._evidence("road:" + r, True, "briefing")
        for i, p in enumerate(PLACES):
            self._evidence("stock:" + p, INITIAL_STOCK[i], "briefing")
        self.observe()

    def emit(self, event_type: str, **data):
        self.events.append({"seq": len(self.events), "tick": self.tick, "type": event_type, **data})

    def _evidence(self, key, value, source):
        old = self.evidence.get(key)
        record = {"value": value, "observed_tick": self.tick,
                  "valid_until": self.tick + self.config.evidence_ttl,
                  "source": source, "id": f"obs-{self.knowledge_version+1}"}
        if old and old["value"] == value and old["source"] == source and old["observed_tick"] == self.tick:
            return
        self.knowledge_version += 1
        self.evidence[key] = record

    def observe(self):
        loc = self.world.location
        for i, key in enumerate(ROADS):
            if loc in key.split("|"):
                self._evidence("road:" + key, self.world.roads[i], "direct")
        self._evidence("stock:" + loc, self.world.stock[PLACES.index(loc)], "direct")

    def context(self) -> Context:
        def value(key):
            rec = self.evidence.get(key)
            return rec["value"] if rec and rec["valid_until"] > self.tick else None
        # Only own confirmed state and legally acquired evidence enter the planner.
        known = State(location=self.world.location, herb=self.world.herb, medicine=self.world.medicine,
                      stock=tuple(value("stock:" + p) for p in PLACES),
                      roads=tuple(value("road:" + r) for r in ROADS),
                      delivered=self.world.delivered)
        return Context(known, self.goal, self.tick,
                       {k: v["valid_until"] for k, v in self.evidence.items()},
                       self.knowledge_version, self.goal_version)

    def actor_view(self) -> dict:
        return {"schema_version": VERSION, "actor_id": "courier",
                "context": asdict(self.context()), "evidence": deepcopy(self.evidence),
                "plan": [a.model_dump() for a in self.plan], "plan_version": self.plan_version,
                "generation": self.generation, "active": deepcopy(self.active)}

    def request_context(self) -> dict:
        # Stable public map, not true road states. No event seq/hash or merchant state.
        return {**self.actor_view(), "map": {"places": list(PLACES), "roads": list(ROADS)},
                "action_schema": Action.model_json_schema(), "goal_rules": GOALS,
                "rules": "take: local herb; craft: workshop, 1 herb -> 1 medicine, 2 ticks; "
                         "deliver: selected goal destination and item; move: adjacent known open road; "
                         "inspect: current place, observation barrier; wait: self, 1 tick"}

    def context_digest(self) -> str:
        return digest(self.request_context())

    def _budget(self) -> Budget:
        return Budget(max(0, min(self.config.decision_budget,
                                self.config.total_budget - self.metrics["budget_used"])))

    def _charge(self, budget: Budget):
        self.metrics["expansions"] += budget.expansions
        self.metrics["validations"] += budget.validations
        self.metrics["budget_used"] += budget.used

    def _plan(self):
        budget = self._budget()
        start = process_time_ns()
        result = decide(self.context(), self.plan, self.config.policy, budget)
        self.cpu_ms += (process_time_ns() - start) / 1e6
        self._charge(budget)
        if result.outcome != "retained":
            self.metrics["decisions"] += 1
            self.metrics["repairs"] += result.outcome == "patched"
            self.metrics["fallbacks"] += result.outcome == "fallback_full"
            self.plan_version += 1
            self.generation += 1
            self.last_repair = {**asdict(result), "plan": [a.model_dump() for a in result.plan],
                                "budget_used": budget.used, "budget_limit": budget.limit,
                                "expansions": budget.expansions, "validations": budget.validations}
            self.emit("planning", **self.last_repair)
        self.plan = result.plan
        if not self.plan:
            self.status = "success" if complete(self.world, self.goal) else result.outcome

    def _interrupt(self):
        if self.active:
            self.emit("interrupted", action=self.active, refund=0)
            self.active = None
        self.plan = []
        self.plan_version += 1
        self.generation += 1

    def _execute(self, a: Action):
        # Same action model, authoritative state. Contact failures refresh only local evidence.
        nxt = transition(self.world, a, self.goal)
        if nxt is None:
            self.observe()
            self.emit("action_failed", action=a.model_dump(), code="ACTION_UNAVAILABLE")
            return
        self.metrics["actions"] += 1
        self.emit("action_started", action=a.model_dump())
        if a.kind == "craft":
            self.world = replace(self.world, herb=self.world.herb - 1)
            self.metrics["consumed"] += 1
            self.active = {"action": a.model_dump(), "due_tick": self.tick + 1}
            return
        self.world = nxt
        if a.kind == "inspect":
            self.observe()
        self.emit("action_completed", action=a.model_dump())

    def _step(self):
        if self.status in ("success", "unknown", "budget_exhausted", "tick_limit", "cancelled"):
            return
        if self.tick >= self.config.max_ticks:
            self.status = "tick_limit"
            return
        self.tick += 1
        self.status = "running"
        # Fixed rule merchant competes via an atomic finite resource update, before courier.
        if self.config.merchant_takes_at == self.tick and not self.merchant["acted"]:
            idx = PLACES.index("warehouse")
            stock = list(self.world.stock)
            if stock[idx] > 0:
                stock[idx] -= 1
                self.world = replace(self.world, stock=tuple(stock))
                self.merchant["herb"] += 1
            self.merchant["acted"] = True
            self.emit("merchant_rule", outcome="took" if self.merchant["herb"] else "empty")
        if self.active:
            if self.active["due_tick"] <= self.tick:
                self.world = replace(self.world, medicine=self.world.medicine + 1)
                self.metrics["produced"] += 1
                self.emit("action_completed", action=self.active["action"])
                self.active = None
            self.observe()
            return
        self.observe()
        if complete(self.world, self.goal):
            self.status = "success"
            return
        self._plan()
        if self.plan:
            a = self.plan.pop(0)
            self._execute(a)
            self.generation += 1
            self.observe()
            if complete(self.world, self.goal):
                self.status = "success"

    def apply(self, command: Command, record: bool = True) -> dict:
        if len(self.trace) >= 1500 or len(self.events) >= 6000:
            raise ValueError("session record limit; export and reset")
        if command.kind == "step":
            self._step()
        elif command.kind == "block":
            states = list(self.world.roads)
            states[ROADS.index(command.target)] = False
            self.world = replace(self.world, roads=tuple(states))
            self.emit("intervention", kind="block", target=command.target)
            # No observation, generation, status, or knowledge change from hidden truth.
        elif command.kind == "remove":
            stock = list(self.world.stock)
            index = PLACES.index(command.target)
            removed = stock[index]
            stock[index] = 0
            self.world = replace(self.world, stock=tuple(stock))
            self.emit("intervention", kind="remove", target=command.target, quantity=removed)
        elif command.kind == "goal":
            self._interrupt()
            self.goal = command.target
            self.goal_version += 1
            self.status = "ready"
            self.emit("goal_changed", goal=self.goal, version=self.goal_version)
        elif command.kind == "cancel":
            self._interrupt()
            self.status = "cancelled"
            self.emit("cancelled")
        elif command.kind == "proposal":
            budget = self._budget()
            try:
                ok, _, reason = validate(self.context(), command.actions, budget)
            except Exhausted:
                ok, reason = False, "budget_exhausted"
            self._charge(budget)
            if not ok or self.active or self.status == "cancelled":
                self.emit("proposal_rejected", reason=reason if not ok else "actor_busy_or_cancelled")
            else:
                self.plan = command.actions
                self.plan_version += 1
                self.generation += 1
                self.status = "ready"
                self.emit("proposal_accepted", actions=[a.model_dump() for a in self.plan])
        self._invariants()
        if record:
            self.trace.append({"input": command.model_dump(exclude_none=True),
                               "state_hash": self.state_hash(), "event_hash": digest(self.events)})
        return self.view()

    def _invariants(self):
        assert 0 <= self.world.herb <= 2 and 0 <= self.world.medicine <= 2
        assert all(type(q) is int and q >= 0 for q in self.world.stock)
        assert self.metrics["produced"] <= self.metrics["consumed"]
        assert self.metrics["budget_used"] <= self.config.total_budget

    def logical(self):
        return {"version": VERSION, "config": self.config.model_dump(), "world": asdict(self.world),
                "tick": self.tick, "actor": self.actor_view(), "merchant": self.merchant,
                "goal": self.goal, "status": self.status, "metrics": self.metrics,
                "last_repair": self.last_repair}

    def state_hash(self):
        return digest(self.logical())

    def view(self):
        return {**deepcopy(self.logical()), "state_hash": self.state_hash(), "events": deepcopy(self.events[-150:]),
                "event_count": len(self.events), "cpu_ms": round(self.cpu_ms, 3),
                "mode": "live_offline", "trace_length": len(self.trace)}

    def export(self):
        return {"schema_version": VERSION, "config": self.config.model_dump(),
                "initial_hash": Engine(self.config).state_hash(), "steps": deepcopy(self.trace),
                "final_hash": self.state_hash()}


class ReplayStep(StrictModel):
    input: Command
    state_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    event_hash: str = Field(pattern=r"^[0-9a-f]{64}$")


class Recording(StrictModel):
    schema_version: Literal["worldmind-0.1.0/river-1"]
    config: Config
    initial_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    steps: list[ReplayStep] = Field(max_length=1500)
    final_hash: str = Field(pattern=r"^[0-9a-f]{64}$")


def replay(data: dict, until: int | None = None) -> tuple[Engine, dict]:
    rec = Recording.model_validate(data)
    e = Engine(rec.config)
    if rec.initial_hash != e.state_hash():
        raise ValueError("initial hash mismatch")
    count = len(rec.steps) if until is None else until
    if type(count) is not int or not 0 <= count <= len(rec.steps):
        raise ValueError("invalid replay cursor")
    for i, step in enumerate(rec.steps[:count]):
        e.apply(step.input)
        if step.state_hash != e.state_hash() or step.event_hash != digest(e.events):
            raise ValueError(f"replay divergence at step {i + 1}")
    if count == len(rec.steps) and e.state_hash() != rec.final_hash:
        raise ValueError("final hash mismatch")
    return e, {"verified": count, "total": len(rec.steps), "complete": count == len(rec.steps),
               "mode": "verified_replay"}


def compare(config: Config, commands: list[tuple[int, Command]] | None = None) -> dict:
    """Fixed external tick schedule, identical action model and budget accounting."""
    results = {}
    for policy in ("Full", "Local"):
        e = Engine(config.model_copy(update={"policy": policy}))
        for _ in range(config.max_ticks + 1):
            for tick, command in commands or [(1, Command(kind="block", target="bridge|home"))]:
                if tick == e.tick:
                    e.apply(command)
            e.apply(Command(kind="step"))
            if e.status not in ("ready", "running"):
                break
        results[policy] = {"status": e.status, "tick": e.tick, "metrics": e.metrics,
                           "cpu_ms": e.cpu_ms, "recording": e.export()}
    return results
