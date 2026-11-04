"""summary.json: the day's totals, cost by provider and model, latency, read back from the events just written.

Totals are the last metric line (which the emitter keeps as running sums of the lines above it); cost per model is
the sum of the usage blocks on the event lines; call counts and latency come from the raw model calls. The two cost
sums are compared, so a call whose usage never reached an event shows up as an error rather than a gap.
"""
from __future__ import annotations

import json
import statistics
from datetime import datetime
from typing import Any

from sim import prices
from sim.schema import VN


def _pct(values: list[int], q: float) -> int:
    if not values:
        return 0
    values = sorted(values)
    return values[min(len(values) - 1, int(round(q * (len(values) - 1))))]


def build(events: list[dict], calls: list[dict], *, mode: str, seed: int, usd_vnd: int, started: str,
          seconds: float, errors: int, stopped: str = "", services: dict | None = None) -> dict:
    last = next(e["metrics"] for e in reversed(events) if e["type"] == "metric")
    cost: dict[str, dict[str, dict]] = {}
    for e in events:
        u = e.get("usage")
        if not u or u["model"] == "mock":
            continue
        row = cost.setdefault(u["provider"], {}).setdefault(u["model"], {
            "calls": 0, "events": 0, "input_tokens": 0, "output_tokens": 0, "cached_tokens": 0, "images": 0,
            "audio_seconds": 0.0, "cost_usd": 0.0})
        row["events"] += 1
        for k in ("input_tokens", "output_tokens", "cached_tokens", "images"):
            row[k] += int(u.get(k) or 0)
        row["audio_seconds"] = round(row["audio_seconds"] + float(u.get("audio_seconds") or 0), 2)
        row["cost_usd"] = round(row["cost_usd"] + float(u.get("cost_usd") or 0), 6)
    latency: dict[str, dict] = {}
    by_model: dict[str, list[int]] = {}
    for c in calls:
        by_model.setdefault(c["model"], []).append(int(c["ms"]))
        row = cost.get(c["provider"], {}).get(c["model"])
        if row is not None:
            row["calls"] += 1
    for model, ms in by_model.items():
        latency[model] = {"p50": int(statistics.median(ms)), "p95": _pct(ms, 0.95), "calls": len(ms)}
    total = round(sum(r["cost_usd"] for p in cost.values() for r in p.values()), 6)
    calls_total = round(sum(float(c.get("cost_usd") or 0) for c in calls), 6)
    return {
        "mode": mode, "seed": seed, "started": started, "finished": datetime.now(VN).isoformat(timespec="seconds"),
        "seconds": seconds, "events": len(events),
        "orders": last["orders"], "gmv_vnd": last["gmv_vnd"], "agent_actions": last["agent_actions"],
        "human_approvals": last["human_approvals"], "human_minutes": last["human_minutes"], "blocked": last["blocked"],
        "cost": cost, "cost_total_usd": total, "cost_total_vnd": round(total * usd_vnd),
        "cost_calls_usd": calls_total, "cost_matches_calls": abs(total - calls_total) < 1e-6,
        "metric_cost_usd": last["agent_cost_usd"],
        "usd_vnd": usd_vnd, "usd_vnd_source": prices.USD_VND_SOURCE,
        "price_source": {"claude": prices.CLAUDE_PRICE_SOURCE, "meta": prices.META_PRICE_SOURCE},
        "latency_ms": latency, "errors": errors, "stopped": stopped or None,
        "services": services or {},
        "scenes": [e["scene"] for e in events if e["type"] == "scene"],
    }


def write(day: Any, started: str, seconds: float, stopped: str = "") -> dict:
    data = build(day.em.events, day.calls, mode=day.mode, seed=day.seed, usd_vnd=day.usd_vnd, started=started,
                 seconds=seconds, errors=day.errors, stopped=stopped,
                 services={"suat_an_sang": day.world.breakfasts, "giat_la_ho": len(day.world.laundry),
                           "buu_pham": len(day.world.parcels),
                           "note": "dịch vụ chạy trên nền tảng, chỉ là một dòng bộ đếm; không tính vào đơn và tiền hàng Giỏ"})
    (day.out / "summary.json").write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    return data
