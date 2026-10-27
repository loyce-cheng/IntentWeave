"""Authoritative finite work orders. Messages carry snapshots, not live references."""
from dataclasses import asdict, dataclass

from .domain import digest
from .cooperation_domain import TERMINAL_ORDERS


@dataclass
class WorkOrder:
    deadline_tick: int
    order_id: str = "supply-1"
    issuer: str = "doctor"
    assignee: str = "courier"
    item: str = "herb"
    quantity: int = 1
    delivery_region: str = "workshop"
    recipient: str = "doctor"
    revocation_policy: str = "either_party_before_fulfillment"
    version: int = 1
    status: str = "proposed"
    accepted_terms_hash: str | None = None
    fulfilled_tick: int | None = None
    breached: bool = False

    def terms(self):
        data = asdict(self)
        for key in ("version", "status", "accepted_terms_hash", "fulfilled_tick", "breached"):
            data.pop(key)
        return data

    def change(self, status: str, tick: int):
        allowed = {"proposed": ("accepted", "rejected", "withdrawn", "expired"),
                   "accepted": ("in_progress", "cancelled", "expired"),
                   "in_progress": ("fulfilled", "cancelled", "expired")}
        if self.status in TERMINAL_ORDERS or status not in allowed[self.status]:
            raise ValueError("invalid_order_transition")
        if status in ("accepted", "fulfilled") and tick > self.deadline_tick:
            raise ValueError("order_deadline")
        if status == "accepted":
            self.accepted_terms_hash = digest(self.terms())
        if self.accepted_terms_hash and self.accepted_terms_hash != digest(self.terms()):
            raise ValueError("order_terms_changed")
        if status == "fulfilled":
            self.fulfilled_tick = tick
        if status == "expired":
            self.breached = self.accepted_terms_hash is not None
        self.status = status
        self.version += 1
