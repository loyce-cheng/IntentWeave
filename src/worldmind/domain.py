"""Finite shared action model. No planner has a reference to the live world."""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, replace
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

VERSION = "worldmind-0.1.0/river-1"
PLACES = ("clinic", "warehouse", "garden", "workshop", "bridge", "ferry", "home")
LABELS = dict(zip(PLACES, ("诊所", "仓库", "药圃", "工坊", "东桥", "渡口", "患者住处")))
EDGES = (
    ("clinic", "warehouse"), ("warehouse", "garden"), ("garden", "workshop"),
    ("clinic", "workshop"), ("workshop", "bridge"), ("bridge", "home"),
    ("workshop", "ferry"), ("ferry", "home"),
)
ROADS = tuple("|".join(sorted(e)) for e in EDGES)
GOALS = {
    "medicine_home": ("home", "medicine"),
    "herb_clinic": ("clinic", "herb"),
    "medicine_clinic": ("clinic", "medicine"),
}
GOAL_LABELS = {"medicine_home": "向患者送药", "herb_clinic": "向诊所送药材",
               "medicine_clinic": "向诊所送药"}
INITIAL_STOCK = (0, 1, 1, 0, 0, 0, 0)


def canonical(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def digest(value: object) -> str:
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def road(a: str, b: str) -> str:
    return "|".join(sorted((a, b)))


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class Action(StrictModel):
    kind: Literal["move", "inspect", "take", "craft", "deliver", "wait"]
    target: str = Field(max_length=32)

    @model_validator(mode="after")
    def reference(self):
        if self.target not in PLACES and not (self.kind == "wait" and self.target == "self"):
            raise ValueError("invalid action reference")
        return self


class Config(StrictModel):
    policy: Literal["Full", "Local"] = "Local"
    goal: Literal["medicine_home", "herb_clinic", "medicine_clinic"] = "medicine_home"
    decision_budget: int = Field(default=3000, ge=1, le=20000)
    total_budget: int = Field(default=30000, ge=1, le=200000)
    max_ticks: int = Field(default=120, ge=1, le=500)
    evidence_ttl: int = Field(default=40, ge=2, le=500)
    merchant_takes_at: int = Field(default=0, ge=0, le=500)


@dataclass(frozen=True)
class State:
    location: str = "clinic"
    herb: int = 0
    medicine: int = 0
    stock: tuple[int | None, ...] = INITIAL_STOCK
    roads: tuple[bool | None, ...] = (True,) * len(ROADS)
    delivered: tuple[int, ...] = (0, 0, 0)


def complete(s: State, goal: str) -> bool:
    return s.delivered[tuple(GOALS).index(goal)] > 0


def transition(s: State, action: Action, goal: str) -> State | None:
    """Identical finite rules for search, validation and authoritative execution.

    None means not established in this input state, never an oracle failure detail.
    craft here projects completion; the runtime splits consumption and completion.
    """
    kind, target = action.kind, action.target
    if kind == "move":
        key = road(s.location, target)
        if key in ROADS and s.roads[ROADS.index(key)] is True:
            return replace(s, location=target)
    elif kind in ("inspect", "wait"):
        if target == s.location or (kind == "wait" and target == "self"):
            return s
    elif target == s.location:
        if kind == "take":
            qty = s.stock[PLACES.index(target)]
            if qty is not None and qty > 0 and s.herb < 2:
                stock = list(s.stock)
                stock[PLACES.index(target)] -= 1
                return replace(s, herb=s.herb + 1, stock=tuple(stock))
        elif kind == "craft" and target == "workshop" and s.herb > 0 and s.medicine < 2:
            return replace(s, herb=s.herb - 1, medicine=s.medicine + 1)
        elif kind == "deliver":
            dest, item = GOALS[goal]
            if target == dest and getattr(s, item) > 0:
                counts = list(s.delivered)
                counts[tuple(GOALS).index(goal)] += 1
                return replace(s, **{item: getattr(s, item) - 1}, delivered=tuple(counts))
    return None


def candidates(s: State) -> list[Action]:
    """Static references + actor belief only. Unknown facts are not treated as true."""
    actions = [Action(kind="move", target=p) for p in PLACES if road(s.location, p) in ROADS]
    actions += [Action(kind=k, target=s.location) for k in ("take", "craft", "deliver")]
    return actions


def dependencies(s: State, a: Action) -> list[str]:
    if a.kind == "move":
        return ["road:" + road(s.location, a.target)]
    if a.kind == "take":
        return ["stock:" + a.target, "self:herb"]
    if a.kind == "craft":
        return ["self:herb", "self:location"]
    if a.kind == "deliver":
        return ["self:inventory", "goal"]
    return ["self:location"]
