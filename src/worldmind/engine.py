"""Single-writer deterministic river demo; privileged state never enters Context."""
from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict, replace
from time import process_time_ns
from typing import Literal

from pydantic import Field, model_validator

from .domain import (Action, Config, GOALS, INITIAL_STOCK, PLACES, ROADS, VERSION,
                     State, StrictModel, complete, digest, transition)
from .planner import Budget, Context
from . import planner
from .compat import planner_v01, planner_v02
from .replay import verify_replay
from .version import (LEGACY_RECORDING_VERSION, V02_RECORDING_VERSION,
                      RIVER_RECORDING_VERSIONS as SUPPORTED_RECORDING_VERSIONS)


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
    def __init__(self, config: Config | None = None, *, version: str = VERSION):
        if version not in SUPPORTED_RECORDING_VERSIONS:
            raise ValueError("unsupported recording version")
        self.version = version
        self._planner = {LEGACY_RECORDING_VERSION: planner_v01,
                         V02_RECORDING_VERSION: planner_v02}.get(version, planner)
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
        self.observation_attempts: set[str] = set()
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
        context = self._planner.Context(known, self.goal, self.tick,
                       {k: v["valid_until"] for k, v in self.evidence.items()},
                       self.knowledge_version, self.goal_version)
        if self._planner is planner:
            context.last_values = {k: v["value"] for k, v in self.evidence.items()}
        return context

    def actor_view(self) -> dict:
        view = {"schema_version": self.version, "actor_id": "courier",
                "context": asdict(self.context()), "evidence": deepcopy(self.evidence),
                "plan": [a.model_dump() for a in self.plan], "plan_version": self.plan_version,
                "generation": self.generation, "active": deepcopy(self.active)}
        if self._planner is planner:
            view["observation_attempts"] = sorted(self.observation_attempts)
        return view

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
        return self._planner.Budget(max(0, min(self.config.decision_budget,
                                self.config.total_budget - self.metrics["budget_used"])))

    def _charge(self, budget: Budget):
        self.metrics["expansions"] += budget.expansions
        self.metrics["validations"] += budget.validations
        self.metrics["budget_used"] += budget.used

    def _plan(self):
        budget = self._budget()
        start = process_time_ns()
        context = self.context()
        result = self._planner.decide(context, self.plan, self.config.policy, budget)
        if self._planner is planner and result.outcome == "observe":
            # Re-reading unchanged facts is not new information. Stop repeated
            # observation routes after returning to the same actor-known state.
            # Neither privileged world state nor merely newer timestamps can
            # trigger/clear this guard; unknown is not a proof of impossibility.
            attempt = digest({"state": asdict(context.state), "goal": context.goal,
                              "goal_version": context.goal_version,
                              "last_values": context.last_values,
                              "plan": [a.model_dump() for a in result.plan]})
            if attempt in self.observation_attempts:
                result = planner.Decision([], "unknown", "observation_cycle_without_progress", len(self.plan))
            else:
                self.observation_attempts.add(attempt)
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
        self.observation_attempts.clear()

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
        """Execute a command and return an independent presentation snapshot."""
        self.advance(command, record)
        return self.view()

    def advance(self, command: Command, record: bool = True) -> None:
        """Execute/record without building an unused UI snapshot in batch callers."""
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
                ok, _, reason = self._planner.validate(self.context(), command.actions, budget)
            except self._planner.Exhausted:
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

    def _invariants(self):
        assert 0 <= self.world.herb <= 2 and 0 <= self.world.medicine <= 2
        assert all(type(q) is int and q >= 0 for q in self.world.stock)
        assert self.metrics["produced"] <= self.metrics["consumed"]
        assert self.metrics["budget_used"] <= self.config.total_budget

    def logical(self):
        return {"version": self.version, "config": self.config.model_dump(), "world": asdict(self.world),
                "tick": self.tick, "actor": self.actor_view(), "merchant": self.merchant,
                "doctor": {"location": "clinic", "policy": "fixed_receiver",
                           "received_herb": self.world.delivered[1], "received_medicine": self.world.delivered[2]},
                "goal": self.goal, "status": self.status, "metrics": self.metrics,
                "last_repair": self.last_repair}

    def state_hash(self):
        return digest(self.logical())

    def view(self):
        logical = deepcopy(self.logical())
        return {**logical, "state_hash": digest(logical), "events": deepcopy(self.events[-150:]),
                "event_count": len(self.events), "cpu_ms": round(self.cpu_ms, 3),
                "mode": "live_offline", "trace_length": len(self.trace)}

    def export(self):
        return {"schema_version": self.version, "config": self.config.model_dump(),
                "initial_hash": Engine(self.config, version=self.version).state_hash(), "steps": deepcopy(self.trace),
                "final_hash": self.state_hash()}


class ReplayStep(StrictModel):
    input: Command
    state_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    event_hash: str = Field(pattern=r"^[0-9a-f]{64}$")


class Recording(StrictModel):
    schema_version: Literal["worldmind-0.1.0/river-1", "worldmind-0.2.0/river-1", "worldmind-0.2.1/river-1"]
    config: Config
    initial_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    steps: list[ReplayStep] = Field(max_length=1500)
    final_hash: str = Field(pattern=r"^[0-9a-f]{64}$")


def replay(data: dict, until: int | None = None) -> tuple[Engine, dict]:
    rec = Recording.model_validate(data)
    e = Engine(rec.config, version=rec.schema_version)
    return verify_replay(e, rec, until)


def compare(config: Config, commands: list[tuple[int, Command]] | None = None) -> dict:
    """Compatibility entry point for the shared scenario comparison."""
    from .runtime import compare as compare_scenarios
    return compare_scenarios(config, commands)
