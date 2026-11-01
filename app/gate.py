"""Regression gate for prompt, policy or model changes: a fresh live batch must not fall below the committed baseline.

    python -m app.gate data/reports/batch-*.csv            (exit 1 when a threshold is missed)

Thresholds live in evals/baseline.json so a change to them is reviewed like code.
"""
from __future__ import annotations

import csv
import json
import math
import sys
from pathlib import Path

BASELINE = Path(__file__).resolve().parent.parent / "evals" / "baseline.json"


def check(csv_path: Path, baseline: dict) -> list[str]:
    rows = list(csv.DictReader(csv_path.open(encoding="utf-8-sig")))
    failures = []
    for key, minimum in baseline["min_accuracy"].items():
        vals = [r[key] == "True" for r in rows if r.get(key) in ("True", "False")]
        rate = sum(vals) / len(vals) if vals else 0.0
        if rate < minimum:
            failures.append(f"{key}: {sum(vals)}/{len(vals)} = {rate:.0%} < {minimum:.0%}")
    secs = sorted(float(r["seconds"]) for r in rows if r.get("seconds"))
    p95 = secs[min(len(secs) - 1, max(0, math.ceil(0.95 * len(secs)) - 1))] if secs else 0.0
    if p95 > baseline["max_p95_seconds"]:
        failures.append(f"p95 {p95:.1f} s > {baseline['max_p95_seconds']} s")
    cost = [float(r["cost_usd"]) for r in rows if r.get("cost_usd")]
    mean = sum(cost) / len(cost) if cost else 0.0
    if mean > baseline["max_mean_cost_usd"]:
        failures.append(f"mean cost ${mean:.4f} > ${baseline['max_mean_cost_usd']}")
    failed = [r["product"] for r in rows if r.get("status") == "failed"]
    if failed:
        failures.append(f"failed runs: {', '.join(failed)}")
    return failures


if __name__ == "__main__":
    baseline = json.loads(BASELINE.read_text(encoding="utf-8"))
    problems = check(Path(sys.argv[1]), baseline)
    for p in problems:
        print("GATE:", p)
    print("gate passed" if not problems else f"gate failed ({len(problems)})")
    sys.exit(1 if problems else 0)
