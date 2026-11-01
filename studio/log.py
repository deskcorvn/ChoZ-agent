"""One log line per studio step, appended to `studio/log.jsonl` (or `$STUDIO_LOG`).

Same fields as `app.pipeline.Step` plus `role` (which hat the model wore: writer, director, inspector...)
and `decision` (what the step settled: "picked 2/3", "accept", "retry"). The README's numbers (shots
accepted per round, $/spot, s/spot, frames rejected before credits were spent) are computed from this file.
"""
from __future__ import annotations

import json
import os
import time
from contextlib import asynccontextmanager
from dataclasses import asdict, dataclass, field
from pathlib import Path

from app.config import ROOT


def log_path() -> Path:
    return Path(os.environ.get("STUDIO_LOG", str(ROOT / "studio" / "log.jsonl")))


@dataclass
class Step:
    key: str                         # service name: script, shotlist, refs, ...
    title: str
    role: str = ""
    model: str = ""
    endpoint: str = ""
    status: str = "running"          # running | done | failed | skipped
    started: float = field(default_factory=time.time)
    ms: int = 0
    cost_usd: float = 0.0
    mock: bool = False
    input: str = ""
    output: dict = field(default_factory=dict)
    decision: str = ""
    error: str = ""

    def apply(self, meta: dict) -> None:
        """Take model, endpoint, usage and cost from a `MetaClient` call's meta."""
        self.model = meta.get("model", "") or self.model
        self.endpoint = meta.get("endpoint", "") or self.endpoint
        self.cost_usd = round(self.cost_usd + float(meta.get("cost_usd") or 0.0), 6)
        if meta.get("usage"):
            self.output.setdefault("usage", []).append(meta["usage"])
        if meta.get("raw"):
            self.output["raw"] = meta["raw"]

    def public(self) -> dict:
        return asdict(self)


def write(step: Step) -> None:
    path = log_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(step.public(), ensure_ascii=False, default=str) + "\n")


def tail(n: int = 200) -> list[dict]:
    path = log_path()
    if not path.is_file():
        return []
    lines = path.read_text(encoding="utf-8").splitlines()
    return [json.loads(line) for line in lines[-n:] if line.strip()]


@asynccontextmanager
async def step(key: str, title: str, role: str, describe_input: str = "", mock: bool = False):
    """Time a service step and write it to the log, whether it succeeds or fails."""
    current = Step(key=key, title=title, role=role, input=describe_input, mock=mock)
    t0 = time.perf_counter()
    try:
        yield current
    except Exception as exc:  # the log must show the failure, whatever it is
        current.status, current.error = "failed", f"{type(exc).__name__}: {exc}"[:500]
        current.ms = round((time.perf_counter() - t0) * 1000)
        write(current)
        raise
    current.ms = round((time.perf_counter() - t0) * 1000)
    if current.status == "running":
        current.status = "done"
    write(current)
