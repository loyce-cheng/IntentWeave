"""Single-writer two-planner cooperation runtime, separately versioned from river-1."""
from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict
from time import process_time_ns
from typing import Literal

from pydantic import Field

from .domain import GOALS, INITIAL_STOCK, PLACES, ROADS, StrictModel, digest
from .planning_core import Budget
from .cooperation_domain import (ACTORS, PROTOCOL, TERMINAL_ORDERS, Actor, Goal, KnownState,
                                 CooperationAction as Action, CooperationCommand as Command,
                                 CooperationConfig, complete, transition)
from .cooperation_planner import Context, decide
from .orders import WorkOrder
from .replay import verify_replay


class CooperationEngine:
    def __init__(self, config: CooperationConfig):
        self.config = config
        self.version = PROTOCOL
        self.tick = 0
        self.status = "ready"
        self.goal = config.goal
        self.actors = {"courier": Actor(), "doctor": Actor(), "merchant": Actor(location="warehouse")}
        self.stock = list(INITIAL_STOCK)
        self.roads = [True] * len(ROADS)
        self.order = WorkOrder(config.order_deadline)
        self.claims: dict[str, dict] = {}
        self.messages: list[dict] = []
        self.events: list[dict] = []
        self.trace: list[dict] = []
        self.wait_age = {a: 0 for a in ACTORS}
        self.request_signatures = {a: None for a in ACTORS}
        self.merchant_acted = False
        self.removed = 0
        self.deliveries = {"herb": 0, "medicine": 0}
        self.cpu_ms = 0.0
        self.lineage: dict | None = None
        for aid in ACTORS:
            for r in ROADS:
                self._evidence(aid, "road:" + r, True, "briefing")
            for p, qty in zip(PLACES, INITIAL_STOCK):
                self._evidence(aid, "stock:" + p, qty, "briefing")
            self._observe(aid)
        self._known_order("doctor")
        self._send_order("courier")
        self.emit("order_proposed", order=asdict(self.order))

    @property
    def metrics(self):
        return {k: sum(a.metrics[k] for a in self.actors.values())
                for k in self.actors["courier"].metrics}

    def emit(self, event_type, **data):
        self.events.append({"seq": len(self.events), "tick": self.tick, "type": event_type, **data})

    def _evidence(self, aid, key, value, source):
        actor = self.actors[aid]
        old = actor.evidence.get(key)
        if old and old["value"] == value and old["source"] == source and old["observed_tick"] == self.tick:
            return
        actor.knowledge_version += 1
        actor.evidence[key] = {"value": value, "source": source, "observed_tick": self.tick,
                               "valid_until": self.tick + self.config.evidence_ttl,
                               "id": f"{aid}-obs-{actor.knowledge_version}"}

    def _observe(self, aid):
        actor = self.actors[aid]
        for i, key in enumerate(ROADS):
            if actor.location in key.split("|"):
                self._evidence(aid, "road:" + key, self.roads[i], "direct")
        self._evidence(aid, "stock:" + actor.location, self.stock[PLACES.index(actor.location)], "direct")
        present = [other for other in ACTORS if other != aid and self.actors[other].location == actor.location]
        if actor.present != present:
            actor.present = present
            actor.knowledge_version += 1

    def _known_order(self, aid, order=None):
        snapshot = deepcopy(order) if order is not None else asdict(self.order)
        actor = self.actors[aid]
        previous = actor.known_orders.get(snapshot["order_id"])
        if previous is None or snapshot["version"] > previous["version"]:
            actor.known_orders[snapshot["order_id"]] = snapshot
            actor.knowledge_version += 1
            if actor.status not in ("cancelled", "success"):
                actor.status = "ready"
            self.emit("order_received", actor_id=aid, status=snapshot["status"], version=snapshot["version"])

    def _send_order(self, aid):
        self.messages.append({"recipient": aid, "due_tick": self.tick + self.config.message_delay,
                              "order": asdict(self.order)})

    def _change_order(self, status, witness=None):
        self.order.change(status, self.tick)
        self.emit("order_changed", status=status, version=self.order.version, breached=self.order.breached)
        for aid in ("courier", "doctor"):
            if aid == witness:
                self._known_order(aid)
            else:
                self._send_order(aid)

    def context(self, aid="courier"):
        a = self.actors[aid]

        def value(key):
            rec = a.evidence.get(key)
            return rec["value"] if rec and rec["valid_until"] > self.tick else None

        state = KnownState(a.location, a.herb, a.medicine,
                           tuple(value("stock:" + p) for p in PLACES),
                           tuple(value("road:" + r) for r in ROADS), a.delivered)
        return Context(state, a.goal or Goal("meet", a.location), self.tick,
                       {k: v["valid_until"] for k, v in a.evidence.items()},
                       {k: v["value"] for k, v in a.evidence.items()})

    def actor_view(self, aid="courier"):
        if aid not in self.actors:
            raise ValueError("unknown_actor")
        a = self.actors[aid]
        return {"schema_version": self.version, "actor_id": aid,
                "context": {**asdict(self.context(aid)), "knowledge_version": a.knowledge_version,
                            "goal_version": a.goal_version, "present": list(a.present),
                            "orders": deepcopy(a.known_orders)},
                "evidence": deepcopy(a.evidence), "plan": [p.model_dump() for p in a.plan],
                "plan_version": a.plan_version, "active": deepcopy(a.active),
                "status": a.status, "waiting": deepcopy(a.waiting),
                "metrics": dict(a.metrics), "last_repair": deepcopy(a.last_repair)}

    def _signature(self, aid):
        a = self.actors[aid]
        return digest({"state": asdict(self.context(aid).state), "orders": a.known_orders,
                       "present": a.present, "goal": asdict(a.goal) if a.goal else None})

    def _wait(self, aid, reason, until):
        a = self.actors[aid]
        a.status = "waiting"
        a.waiting = {"reason": reason, "until": until, "fingerprint": self._signature(aid)}

    def _set_goal(self, aid, goal):
        a = self.actors[aid]
        if goal != a.goal:
            a.goal = goal
            a.goal_version += 1
            a.plan = []
            a.waiting = None
            a.observation_attempts.clear()
            a.status = "ready"

    def _intent(self, aid):
        a = self.actors[aid]
        order = a.known_orders.get("supply-1")
        if aid == "courier":
            if order is None:
                self._wait(aid, "await_order", self.config.goal_deadline + 1)
                return None
            if order["status"] == "proposed":
                # The acceptance policy has no remote stock/road access.
                if self.order.status == "proposed":
                    self._change_order("accepted" if self.config.accept_order else "rejected", aid)
                    if self.order.status == "accepted":
                        self._change_order("in_progress", aid)
                else:
                    self._known_order(aid)  # A submitted acceptance receives a legal rejection receipt.
                order = a.known_orders["supply-1"]
            if order["status"] in TERMINAL_ORDERS:
                a.status = "success" if order["status"] == "fulfilled" else "unknown"
                a.plan = []
                a.waiting = None
                return None
            if self.tick > order["deadline_tick"]:
                a.status = "unknown"
                return None
            goal = Goal("supply", order["delivery_region"], order["item"], order["quantity"],
                        order["recipient"], order["deadline_tick"] - 2)
            self._set_goal(aid, goal)
            if complete(self.context(aid).state, goal):
                if goal.recipient in a.present:
                    return Action(kind="give", target=goal.recipient)
                self._wait(aid, "await_recipient", goal.deadline + 1)
                return None
        else:
            if a.delivered:
                a.status = "success"
                return None
            if a.herb or a.medicine:
                dest, item = GOALS[self.goal]
                self._set_goal(aid, Goal("deliver", dest, item, deadline=self.config.goal_deadline))
            else:
                if order and order["status"] in ("rejected", "withdrawn", "cancelled", "expired"):
                    a.status = "unknown"
                    a.plan = []
                    a.waiting = None
                    return None
                self._set_goal(aid, Goal("meet", "workshop", deadline=self.config.goal_deadline))
                if a.location == "workshop":
                    self._wait(aid, "await_delivery", self.config.goal_deadline + 1)
                    return None
        if self.tick > a.goal.deadline:
            a.status = "unknown"
            a.plan = []
            return None
        if a.waiting and a.waiting["fingerprint"] == self._signature(aid) and self.tick < a.waiting["until"]:
            return None
        if a.status == "budget_exhausted":
            return None
        a.waiting = None
        allowance = max(0, min(self.config.decision_budget,
                               self.config.actor_budget - a.metrics["budget_used"],
                               self.config.total_budget - self.metrics["budget_used"]))
        budget = Budget(allowance)
        started = process_time_ns()
        ctx = self.context(aid)
        plan, outcome, reason, retained = decide(ctx, a.plan, self.config.policy, budget)
        if outcome == "observe":
            attempt = digest({"state": asdict(ctx.state), "goal": asdict(ctx.goal),
                              "last_values": ctx.last_values, "plan": [p.model_dump() for p in plan]})
            if attempt in a.observation_attempts:
                plan, outcome, reason = [], "unknown", "observation_cycle_without_progress"
            else:
                a.observation_attempts.add(attempt)
        self.cpu_ms += (process_time_ns() - started) / 1e6
        for name in ("expansions", "validations"):
            a.metrics[name] += getattr(budget, name)
        a.metrics["budget_used"] += budget.used
        a.last_repair = {"outcome": outcome, "reason": reason, "retained": retained,
                         "removed": max(0, len(a.plan) - retained), "budget_used": budget.used}
        if outcome != "retained":
            a.metrics["decisions"] += 1
            a.metrics["repairs"] += outcome == "patched"
            a.metrics["fallbacks"] += outcome == "fallback_full"
            a.plan_version += 1
            self.emit("planning", actor_id=aid, **a.last_repair)
        a.plan = plan
        if not plan:
            if outcome == "budget_exhausted":
                a.status = outcome
            else:
                self._wait(aid, reason or outcome, a.goal.deadline + 1)
            return None
        a.status = "running"
        return a.plan.pop(0)

    def _interrupt(self, aid, reason="interrupted"):
        a = self.actors[aid]
        if a.active:
            self.emit("action_interrupted", actor_id=aid, action=a.active["action"], reason=reason)
            a.active = None
        self.claims.pop(aid, None)
        a.plan = []
        a.waiting = None
        if a.status not in ("cancelled", "success"):
            a.status = "ready"

    def _start(self, aid, action):
        a = self.actors[aid]
        if a.active:
            return
        k = action.kind
        resource = None
        if k == "take":
            index = PLACES.index(a.location)
            reserved = sum(c["quantity"] for c in self.claims.values() if c["resource"] == "stock:" + a.location)
            legal = action.target == a.location and self.stock[index] - reserved > 0 and a.herb < 2
            resource = "stock:" + a.location
        elif k == "craft":
            legal = aid == "doctor" and a.location == action.target == "workshop" and a.herb > 0 and a.medicine < 2
            resource = "workshop:slot"
            legal &= all(c["resource"] != resource for c in self.claims.values())
        elif k == "give":
            o = self.order
            receiver = self.actors[action.target]
            legal = (aid == o.assignee and action.target == o.recipient and
                     o.status == "in_progress" and a.location == receiver.location == o.delivery_region and
                     a.herb >= o.quantity and receiver.herb + o.quantity <= 2 and self.tick + 1 <= o.deadline_tick)
            resource = "inventory:" + aid
        else:
            truth = KnownState(a.location, a.herb, a.medicine, tuple(self.stock), tuple(self.roads), a.delivered)
            goal = a.goal or Goal("meet", a.location)
            nxt = transition(truth, action, goal)
            legal = nxt is not None and (k != "deliver" or self.tick <= goal.deadline)
        if not legal:
            self.wait_age[aid] = min(self.wait_age[aid] + 1, 500)
            a.plan = []
            self._observe(aid)
            self.emit("action_denied", actor_id=aid, action=action.model_dump(), reason="action_unavailable")
            self._wait(aid, "action_unavailable", self.tick + 2)
            return
        self.wait_age[aid] = 0
        a.metrics["actions"] += 1
        if resource:
            # All-or-none reservation; an action completes strictly before lease expiry.
            self.claims[aid] = {"resource": resource, "quantity": 1, "expires": self.tick + 3}
            a.active = {"action": action.model_dump(), "due_tick": self.tick + 1,
                        "started_tick": self.tick, "order_version": self.order.version if k == "give" else None}
            if k == "craft":
                a.herb -= 1
                a.metrics["consumed"] += 1
            self.emit("action_started", actor_id=aid, action=action.model_dump())
        else:
            if k == "deliver":
                self.deliveries[goal.item] += goal.quantity
            a.location, a.herb, a.medicine, a.delivered = nxt.location, nxt.herb, nxt.medicine, nxt.delivered
            self.emit("action_completed", actor_id=aid, action=action.model_dump())
            self._observe(aid)

    def _finish(self, aid):
        a = self.actors[aid]
        action = Action.model_validate(a.active["action"])
        claim = self.claims.get(aid)
        if claim is None or claim["expires"] <= self.tick:
            self._interrupt(aid, "lease_expired")
            return
        if action.kind == "take":
            index = PLACES.index(a.location)
            if action.target != a.location or self.stock[index] < 1:
                self._interrupt(aid, "resource_unavailable")
                return
            self.stock[index] -= 1
            a.herb += 1
        elif action.kind == "craft":
            a.medicine += 1
            a.metrics["produced"] += 1
        else:
            o = self.order
            receiver = self.actors[action.target]
            if not (o.status == "in_progress" and self.tick <= o.deadline_tick and
                    a.active["order_version"] == o.version and
                    a.location == receiver.location == o.delivery_region and
                    a.herb >= o.quantity and receiver.herb + o.quantity <= 2):
                self._interrupt(aid, "handover_unavailable")
                self._known_order(aid)  # Result of the attempted transaction is observable.
                return
            a.herb -= o.quantity
            receiver.herb += o.quantity
            receiver.waiting = None
            receiver.status = "ready"
            self._change_order("fulfilled", aid)
            self._known_order(action.target)
            self.emit("resource_transferred", sender=aid, recipient=action.target, item=o.item, quantity=o.quantity)
        self.emit("action_completed", actor_id=aid, action=action.model_dump())
        a.active = None
        self.claims.pop(aid, None)
        self._observe(aid)

    def _step(self):
        if self.status not in ("ready", "running"):
            return
        if self.tick >= self.config.max_ticks:
            self._stop("tick_limit")
            return
        self.tick += 1
        self.status = "running"
        finished = set()
        for aid in ACTORS:
            a = self.actors[aid]
            if a.active and a.active["due_tick"] <= self.tick:
                self._finish(aid)
                finished.add(aid)
        ready = [m for m in self.messages if m["due_tick"] <= self.tick]
        self.messages = [m for m in self.messages if m["due_tick"] > self.tick]
        for message in ready:
            self._known_order(message["recipient"], message["order"])
        for aid in ACTORS:
            self._observe(aid)
        intents = {}
        for aid in ("courier", "doctor"):
            if aid not in finished and not self.actors[aid].active:
                action = self._intent(aid)
                if action:
                    intents[aid] = action
        if self.tick == self.config.merchant_takes_at and not self.merchant_acted:
            self.merchant_acted = True
            intents["merchant"] = Action(kind="take", target="warehouse")
        for aid, action in intents.items():
            signature = digest(action.model_dump())
            if signature != self.request_signatures[aid]:
                self.wait_age[aid] = 0
                self.request_signatures[aid] = signature
        # Rotating priority is independent of world changes and identical for both policies.
        ordered = sorted(intents, key=lambda aid: (-self.wait_age[aid], (ACTORS.index(aid) - self.tick) % len(ACTORS)))
        for aid in ordered:
            self._start(aid, intents[aid])
        if self.tick >= self.order.deadline_tick and self.order.status not in TERMINAL_ORDERS:
            self._change_order("expired")
        if self.actors["doctor"].delivered:
            self._stop("success")
        elif self.tick >= self.config.goal_deadline:
            self._stop("deadline_missed")
        elif self.metrics["budget_used"] >= self.config.total_budget:
            self._stop("budget_exhausted")
        elif all(self.actors[a].status in ("unknown", "budget_exhausted", "success")
                 for a in ("courier", "doctor")) and not self.messages:
            self._stop("budget_exhausted" if any(self.actors[a].status == "budget_exhausted"
                                                for a in ("courier", "doctor")) else "unknown")

    def _stop(self, status):
        for aid in ACTORS:
            previous = self.actors[aid].status
            self._interrupt(aid, status)
            a = self.actors[aid]
            a.status = ("success" if a.delivered or (aid == "courier" and self.order.status == "fulfilled")
                        else previous if previous in ("unknown", "budget_exhausted", "success") else status)
        self.status = status
        self.emit("scenario_finished", status=status)

    def advance(self, command, record=True):
        command = Command.model_validate(command.model_dump(exclude_none=True))
        if len(self.trace) >= 1500 or len(self.events) > 5900:
            raise ValueError("session record limit; export and reset")
        if command.kind == "step":
            self._step()
        elif command.kind == "block":
            self.roads[ROADS.index(command.target)] = False
            self.emit("intervention", kind="block", target=command.target)
        elif command.kind == "remove":
            for aid, claim in list(self.claims.items()):
                if claim["resource"] == "stock:" + command.target:
                    self._interrupt(aid, "resource_removed")
            index = PLACES.index(command.target)
            self.removed += self.stock[index]
            self.stock[index] = 0
            self.emit("intervention", kind="remove", target=command.target)
        elif command.kind == "interrupt":
            self._interrupt(command.target)
        elif command.kind == "order_cancel":
            if self.order.status not in TERMINAL_ORDERS:
                self._change_order("withdrawn" if self.order.status == "proposed" else "cancelled")
                for aid, a in self.actors.items():
                    if a.active and a.active["action"]["kind"] == "give":
                        self._interrupt(aid, "order_cancelled")
        elif command.kind == "goal":
            self._interrupt("doctor", "goal_changed")
            self.goal = command.target
            a = self.actors["doctor"]
            a.delivered = False
            a.goal = None
            a.goal_version += 1
            a.status = "ready"
            self.status = "ready"
            self.emit("goal_changed", goal=self.goal)
        elif command.kind == "cancel":
            if self.order.status not in TERMINAL_ORDERS:
                self._change_order("withdrawn" if self.order.status == "proposed" else "cancelled")
            self._stop("cancelled")
        self._invariants()
        if record:
            self.trace.append({"input": command.model_dump(exclude_none=True),
                               "state_hash": self.state_hash(), "event_hash": digest(self.events)})

    def _invariants(self):
        assert all(type(q) is int and q >= 0 for q in self.stock)
        assert all(0 <= a.herb <= 2 and 0 <= a.medicine <= 2 for a in self.actors.values())
        assert sum(INITIAL_STOCK) == (sum(self.stock) + sum(a.herb for a in self.actors.values()) +
                                     self.metrics["consumed"] + self.removed + self.deliveries["herb"])
        assert self.metrics["produced"] == sum(a.medicine for a in self.actors.values()) + self.deliveries["medicine"]
        assert self.metrics["produced"] <= self.metrics["consumed"]
        assert self.metrics["budget_used"] <= self.config.total_budget
        assert all(a.metrics["budget_used"] <= self.config.actor_budget for a in self.actors.values())
        assert all(self.actors[aid].active is not None for aid in self.claims)
        for p in PLACES:
            assert sum(c["quantity"] for c in self.claims.values() if c["resource"] == "stock:" + p) <= self.stock[PLACES.index(p)]

    def logical(self):
        return {"version": self.version, "config": self.config.model_dump(), "tick": self.tick,
                "status": self.status, "goal": self.goal,
                "actors": {aid: {**self.actor_view(aid), "observation_attempts": sorted(a.observation_attempts)}
                           for aid, a in self.actors.items()},
                "stock": self.stock, "roads": self.roads, "orders": [asdict(self.order)],
                "claims": self.claims, "messages": self.messages, "wait_age": self.wait_age,
                "request_signatures": self.request_signatures,
                "merchant_acted": self.merchant_acted, "removed": self.removed, "deliveries": self.deliveries}

    def state_hash(self):
        return digest(self.logical())

    def view(self):
        logical = deepcopy(self.logical())
        actor = self.actor_view()
        return {**logical, "world": {**actor["context"]["state"], "stock": list(self.stock), "roads": list(self.roads)},
                "actor": actor, "metrics": self.metrics, "last_repair": actor["last_repair"],
                "state_hash": digest(logical), "events": deepcopy(self.events[-150:]),
                "event_count": len(self.events), "cpu_ms": round(self.cpu_ms, 3),
                "mode": "live_offline", "trace_length": len(self.trace)}

    def apply(self, command, record=True):
        self.advance(command, record)
        return self.view()

    def export(self):
        return {"schema_version": self.version, "config": self.config.model_dump(),
                "initial_hash": CooperationEngine(self.config).state_hash(), "steps": deepcopy(self.trace),
                "final_hash": self.state_hash(), **({"lineage": dict(self.lineage)} if self.lineage else {})}


class CooperationStep(StrictModel):
    input: Command
    state_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    event_hash: str = Field(pattern=r"^[0-9a-f]{64}$")


class Lineage(StrictModel):
    parent_recording_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    cursor: int = Field(ge=0, le=1500)


class CooperationRecording(StrictModel):
    schema_version: Literal["worldmind-0.4.0/cooperation-1"]
    config: CooperationConfig
    initial_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    steps: list[CooperationStep] = Field(max_length=1500)
    final_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    lineage: Lineage | None = None


def replay(data, until=None):
    rec = CooperationRecording.model_validate(data)
    engine = CooperationEngine(rec.config)
    engine.lineage = rec.lineage.model_dump() if rec.lineage else None
    return verify_replay(engine, rec, until)
