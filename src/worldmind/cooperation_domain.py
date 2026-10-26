"""Finite v0.4 contracts. Legacy river rules are deliberately unchanged."""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Literal

from pydantic import Field, model_validator

from .domain import INITIAL_STOCK, PLACES, ROADS, GOALS, StrictModel, road

PROTOCOL = "worldmind-0.4.0/cooperation-1"
ACTORS = ("courier", "doctor", "merchant")
TERMINAL_ORDERS = ("fulfilled", "cancelled", "expired", "rejected", "withdrawn")


class CooperationConfig(StrictModel):
    scenario: Literal["cooperation"]
    policy: Literal["Full", "Local"] = "Local"
    goal: Literal["medicine_home", "herb_clinic", "medicine_clinic"] = "medicine_home"
    decision_budget: int = Field(default=3000, ge=1, le=20000)
    actor_budget: int = Field(default=15000, ge=1, le=100000)
    total_budget: int = Field(default=30000, ge=1, le=200000)
    max_ticks: int = Field(default=120, ge=1, le=500)
    evidence_ttl: int = Field(default=40, ge=2, le=500)
    order_deadline: int = Field(default=30, ge=1, le=500)
    goal_deadline: int = Field(default=60, ge=1, le=500)
    message_delay: int = Field(default=1, ge=1, le=20)
    accept_order: bool = True
    merchant_takes_at: int = Field(default=0, ge=0, le=500)


class CooperationCommand(StrictModel):
    kind: Literal["step", "block", "remove", "goal", "cancel", "order_cancel", "interrupt"]
    target: str | None = Field(default=None, max_length=64)

    @model_validator(mode="after")
    def reference(self):
        allowed = {"block": ROADS, "remove": PLACES, "goal": tuple(GOALS),
                   "interrupt": ACTORS, "order_cancel": ("supply-1",),
                   "step": (None,), "cancel": (None,)}
        if self.target not in allowed[self.kind]:
            raise ValueError("invalid command reference")
        return self


class CooperationAction(StrictModel):
    kind: Literal["move", "inspect", "take", "craft", "deliver", "give", "wait"]
    target: str

    @model_validator(mode="after")
    def reference(self):
        allowed = ACTORS if self.kind == "give" else ("self",) if self.kind == "wait" else PLACES
        if self.target not in allowed:
            raise ValueError("invalid action reference")
        return self


@dataclass(frozen=True)
class Goal:
    kind: str
    destination: str
    item: str = "herb"
    quantity: int = 1
    recipient: str | None = None
    deadline: int = 500


@dataclass(frozen=True)
class KnownState:
    location: str = "clinic"
    herb: int = 0
    medicine: int = 0
    stock: tuple[int | None, ...] = INITIAL_STOCK
    roads: tuple[bool | None, ...] = (True,) * len(ROADS)
    delivered: bool = False


@dataclass
class Actor:
    location: str = "clinic"
    herb: int = 0
    medicine: int = 0
    delivered: bool = False
    evidence: dict[str, dict] = field(default_factory=dict)
    known_orders: dict[str, dict] = field(default_factory=dict)
    present: list[str] = field(default_factory=list)
    knowledge_version: int = 0
    goal_version: int = 1
    plan_version: int = 0
    goal: Goal | None = None
    plan: list[CooperationAction] = field(default_factory=list)
    active: dict | None = None
    waiting: dict | None = None
    status: str = "ready"
    metrics: dict = field(default_factory=lambda: dict(
        expansions=0, validations=0, budget_used=0, decisions=0, repairs=0, fallbacks=0,
        actions=0, consumed=0, produced=0))
    last_repair: dict = field(default_factory=dict)
    observation_attempts: set[str] = field(default_factory=set)


def duration(action: CooperationAction) -> int:
    return 2 if action.kind in ("take", "craft", "give") else 1


def complete(state: KnownState, goal: Goal) -> bool:
    if goal.kind == "meet":
        return state.location == goal.destination
    if goal.kind == "supply":
        return state.location == goal.destination and getattr(state, goal.item) >= goal.quantity
    return state.delivered


def transition(state: KnownState, action: CooperationAction, goal: Goal) -> KnownState | None:
    """Pure role-local prediction; never reads authority or another actor."""
    k, target = action.kind, action.target
    if k == "move":
        edge = road(state.location, target)
        if edge in ROADS and state.roads[ROADS.index(edge)] is True:
            return replace(state, location=target)
    elif k == "inspect" and target == state.location:
        return state
    elif k == "wait" and target == "self":
        return state
    elif target == state.location:
        if k == "take" and state.herb < 2:
            index = PLACES.index(target)
            qty = state.stock[index]
            if qty is not None and qty > 0:
                stocks = list(state.stock)
                stocks[index] -= 1
                return replace(state, stock=tuple(stocks), herb=state.herb + 1)
        elif k == "craft" and target == "workshop" and state.herb and state.medicine < 2:
            return replace(state, herb=state.herb - 1, medicine=state.medicine + 1)
        elif k == "deliver" and goal.kind == "deliver" and target == goal.destination:
            if getattr(state, goal.item) >= goal.quantity:
                return replace(state, **{goal.item: getattr(state, goal.item) - goal.quantity}, delivered=True)
    return None


def candidates(state: KnownState, goal: Goal) -> list[CooperationAction]:
    actions = [CooperationAction(kind="move", target=p) for p in PLACES
               if road(state.location, p) in ROADS]
    kinds = ("take",) if goal.kind == "supply" else ("craft", "deliver") if goal.kind == "deliver" else ()
    return [CooperationAction(kind=k, target=state.location) for k in kinds] + actions


def dependencies(state: KnownState, action: CooperationAction) -> list[str]:
    if action.kind == "move":
        return ["road:" + road(state.location, action.target)]
    if action.kind == "take":
        return ["stock:" + action.target]
    return []
