"""The event schema the stage replays (section 3 of the brief), and emit(): one place that writes events.

The stage maps sender codes and places to sprites, so codes and field names are fixed here and checked by
pydantic before a line is written. Metrics are running totals kept by the emitter itself, so the counters on
screen are sums of the lines above them, never numbers typed in.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator, model_validator

VN = timezone(timedelta(hours=7))

AGENTS = ("ag_dang_ban", "ag_kiem_chung", "ag_chia_don", "ag_giao_nhan", "ag_cham_khach", "ag_doi_soat")
HUMANS = ("hu_tram", "hu_kho")
SUPPLIERS = tuple(f"ncc{i}" for i in range(1, 6))
HOUSEHOLDS = tuple(f"ho{i:02d}" for i in range(1, 51))
VEHICLES = ("xe1",)
ACTORS = AGENTS + HUMANS + SUPPLIERS + HOUSEHOLDS + VEHICLES
PLACES = ("loc_ncc", "loc_kho", "loc_duong", "loc_tram", "loc_nha1", "loc_nha2", "loc_nha3", "loc_nha4", "loc_nha5",
          "loc_van_phong")
SCENES = ("dat_gio", "dang_ban", "gom_kho", "ra_tram", "nhan_hang", "doi_soat", "tong_ket")
METRIC_KEYS = ("orders", "gmv_vnd", "agent_actions", "agent_cost_usd", "agent_cost_vnd", "human_approvals",
               "human_minutes", "blocked")
TEXT_MAX = 90


class Usage(BaseModel):
    provider: Literal["claude", "meta"]
    model: str
    endpoint: str
    input_tokens: int = 0
    output_tokens: int = 0
    cached_tokens: int = 0
    cache_write_tokens: int = 0
    images: int = 0
    audio_seconds: float = 0.0
    ms: int = 0
    cost_usd: float = 0.0


class Tool(BaseModel):
    name: str
    input_summary: str
    output_summary: str
    ok: bool


class Event(BaseModel):
    id: str
    t: float
    clock: str
    wall: str
    type: Literal["scene", "say", "handoff", "tool", "approve_request", "approve", "move", "metric", "error"]
    mode: Literal["mock", "live"]
    from_: str | None = Field(default=None, alias="from")
    to: str | None = None
    text: str | None = None
    detail: dict[str, Any] | None = None
    tool: Tool | None = None
    usage: Usage | None = None
    loc: str | None = None
    metrics: dict[str, float] | None = None
    scene: str | None = None

    model_config = {"populate_by_name": True}

    @field_validator("text")
    @classmethod
    def _short(cls, v: str | None) -> str | None:
        if v is not None and len(v) > TEXT_MAX:
            raise ValueError(f"text is {len(v)} characters, the bubble holds {TEXT_MAX}: {v}")
        return v

    @model_validator(mode="after")
    def _shape(self) -> Event:
        need = {"say": ("from_", "to", "text"), "handoff": ("from_", "to", "text"), "tool": ("from_", "tool"),
                "approve_request": ("from_", "to", "text"), "approve": ("from_", "text"), "move": ("from_", "loc"),
                "metric": ("metrics",), "scene": ("scene",), "error": ("from_", "text")}[self.type]
        missing = [k.rstrip("_") for k in need if getattr(self, k) in (None, "")]
        if missing:
            raise ValueError(f"{self.type} needs {', '.join(missing)}")
        for who in (self.from_, self.to):
            if who is not None and who not in ACTORS:
                raise ValueError(f"unknown actor {who}")
        if self.loc is not None and self.loc not in PLACES:
            raise ValueError(f"unknown place {self.loc}")
        if self.scene is not None and self.scene not in SCENES:
            raise ValueError(f"unknown scene {self.scene}")
        if self.type == "move" and self.from_ in AGENTS:
            raise ValueError("agents do not move")
        if self.metrics is not None and set(self.metrics) != set(METRIC_KEYS):
            raise ValueError(f"metrics must have exactly {', '.join(METRIC_KEYS)}")
        if self.usage is not None and self.mode == "mock" and self.usage.cost_usd:
            raise ValueError("mock events carry no cost")
        return self


def clock(t: float) -> str:
    """HH:MM of simulated seconds from 00:00 of the day; the evening before is negative t (20:00 = -14400)."""
    minutes = int(t // 60) % (24 * 60)
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def at(hhmm: str, day: int = 0) -> float:
    """Simulated seconds for "HH:MM" on the day (day=-1: the evening before)."""
    h, m = hhmm.split(":")
    return day * 86400 + int(h) * 3600 + int(m) * 60


class Emitter:
    """Writes events.jsonl line by line and keeps the running totals the metric events report.

    Rules from the brief: every agent tool call and every agent say is one agent action; every approve is one human
    approval; human minutes come from the table in humans.py; a metric line after each scene and every 20 events.
    """

    def __init__(self, path: Path, mode: str, usd_vnd: int):
        self.path, self.mode, self.usd_vnd = path, mode, usd_vnd
        path.parent.mkdir(parents=True, exist_ok=True)
        self._fh = path.open("w", encoding="utf-8", newline="\n")
        self.n = 0
        self.t = -10**9
        self.since_metric = 0
        self.totals: dict[str, float] = {"orders": 0, "gmv_vnd": 0, "agent_actions": 0, "agent_cost_usd": 0.0,
                                         "human_approvals": 0, "human_minutes": 0.0, "blocked": 0}
        self.events: list[dict] = []
        self.listeners: list = []           # callables fed every event as it is written (sim/serve.py --live)

    def close(self) -> None:
        self._fh.close()

    def metrics(self) -> dict:
        m = dict(self.totals)
        m["agent_cost_usd"] = round(m["agent_cost_usd"], 6)
        m["agent_cost_vnd"] = round(m["agent_cost_usd"] * self.usd_vnd)
        m["human_minutes"] = round(m["human_minutes"], 2)
        return {k: m[k] for k in METRIC_KEYS}

    def emit(self, t: float, type: str, **fields: Any) -> dict:
        if t < self.t:
            raise ValueError(f"time went back: {t} after {self.t}")
        self.t = t
        if "from_" in fields:
            fields["from"] = fields.pop("from_")
        if isinstance(fields.get("usage"), Usage):
            fields["usage"] = fields["usage"].model_dump()
        event = Event.model_validate({"id": f"e{self.n + 1:06d}", "t": round(t, 1), "clock": clock(t),
                                      "wall": datetime.now(VN).isoformat(timespec="seconds"), "type": type,
                                      "mode": self.mode, **fields})
        self.n += 1
        if event.from_ in AGENTS and type in ("tool", "say") and not (event.detail or {}).get("auto"):
            self.totals["agent_actions"] += 1          # a read-out of the metrics (detail.auto) is not agent work
        if event.usage is not None:
            self.totals["agent_cost_usd"] += event.usage.cost_usd
        if type == "approve":
            self.totals["human_approvals"] += 1
        data = event.model_dump(by_alias=True, exclude_none=True)
        self._fh.write(json.dumps(data, ensure_ascii=False) + "\n")
        self._fh.flush()
        self.events.append(data)
        for listener in self.listeners:
            listener(data)
        if type == "metric":
            self.since_metric = 0
        else:
            self.since_metric += 1
            if self.since_metric >= 20:
                self.metric(t)
        return data

    def add(self, **amounts: float) -> None:
        """Count what the event types alone do not carry: orders paid, human minutes, blocked items."""
        for key, value in amounts.items():
            self.totals[key] += value

    def metric(self, t: float, force: bool = False) -> dict:
        if (not force and self.events and self.events[-1]["type"] == "metric"
                and self.events[-1]["metrics"] == self.metrics()):
            return self.events[-1]          # nothing changed since the last metric line
        return self.emit(t, "metric", metrics=self.metrics())
