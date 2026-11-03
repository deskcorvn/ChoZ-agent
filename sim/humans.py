"""The two people: hu_tram (the young operator at the station) and hu_kho (the warehouse owner, who also coordinates).

They approve what involves money or a customer. In mock and live alike the approval is automatic two simulated
seconds after the request, but both events are always written, and each approval adds the minutes a person would
really spend on it (table from section 6 of the brief).
"""
from __future__ import annotations

from typing import Any

# Minutes of a person's time per approval or hand-over.
MINUTES = {
    "duyet_listing": 0.5,      # brief, section 6
    "nhan_hang_kho": 10,       # brief
    "nhan_hang_tram": 10,      # brief
    "khach_nhan_hang": 1,      # brief: per customer picking up
    "doi_tra": 1,              # brief
    "doi_soat": 5,             # brief
    "thay_hang": 1,            # NOT in the brief: assumed equal to a return approval; to be confirmed (sim/YEU-CAU.md)
}
APPROVE_DELAY_S = 2


def approve(day: Any, agent: str, human: str, kind: str, request: str, answer: str, detail: dict | None = None,
            usage: Any = None) -> dict:
    """approve_request from the agent, then approve from the person 2 simulated seconds later."""
    req = day.emit("approve_request", from_=agent, to=human, text=request, usage=usage,
                   detail={"kind": kind, "minutes": MINUTES[kind], **(detail or {})})
    done = day.emit("approve", step=APPROVE_DELAY_S, from_=human, to=agent, text=answer,
                    detail={"request": req["id"], "kind": kind, "minutes": MINUTES[kind], "auto": True})
    day.add(human_minutes=MINUTES[kind])
    day.tools.approved.add(kind)
    return done


def handover(day: Any, customers: int) -> None:
    """Minutes the station operator spends handing bags to customers (no approval event: it is physical work)."""
    day.add(human_minutes=MINUTES["khach_nhan_hang"] * customers)
