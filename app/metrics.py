"""Aggregate observability over the run logs: what the agent did, how fast, at what cost, and how often a guard fired.

Every run already keeps a step log (model, endpoint, ms, cost, decision). This folds them into one view per day:
runs, status and verdict mix, p50/p95 agent time, cost, model calls by model, guard hits (contact data masked,
instructions ignored), rules cited, failures by kind. Served at GET /api/metrics and /metrics.
"""
from __future__ import annotations

import json
import math
import re
import time
from collections import Counter, defaultdict
from pathlib import Path

from app import audit


def _pct(values: list[float], q: float) -> float:
    if not values:
        return 0.0
    s = sorted(values)
    return round(s[min(len(s) - 1, max(0, math.ceil(q * len(s)) - 1))], 1)


def collect(runs_dir: Path, days: int = 7) -> dict:
    cutoff = time.time() - days * 86400
    per_day: dict[str, dict] = defaultdict(lambda: {"runs": 0, "seconds": [], "cost": 0.0})
    status, verdict, models, guards, rules, failures = Counter(), Counter(), Counter(), Counter(), Counter(), Counter()
    total = 0
    for path in runs_dir.glob("*/run.json"):
        if path.stat().st_mtime < cutoff:
            continue
        try:
            run = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        total += 1
        day = time.strftime("%Y-%m-%d", time.localtime(run.get("created") or path.stat().st_mtime))
        totals = run.get("totals") or {}
        d = per_day[day]
        d["runs"] += 1
        if totals.get("seconds"):
            d["seconds"].append(float(totals["seconds"]))
        d["cost"] += float(totals.get("cost_usd") or 0.0)
        status[run.get("status", "?")] += 1
        v = run.get("verification") or {}
        if v.get("verdict"):
            verdict[v["verdict"]] += 1
        for r in v.get("rules") or []:
            rules[r["id"]] += 1
        for s in run.get("steps") or []:
            if s.get("model") and s.get("status") == "done":
                models[s["model"]] += 1
            if s.get("key") in ("privacy", "injection", "fallback"):
                guards[s["key"]] += 1
            if s.get("status") == "failed":
                err = s.get("error", "")
                kind = re.search(r"\[(\w+)\]", err)
                failures[kind.group(1) if kind else s.get("key", "?")] += 1
    days_out = [{"day": k, "runs": v["runs"], "p50_s": _pct(v["seconds"], .5), "p95_s": _pct(v["seconds"], .95),
                 "cost_usd": round(v["cost"], 4), "cost_per_run": round(v["cost"] / max(v["runs"], 1), 4)}
                for k, v in sorted(per_day.items())]
    return {"window_days": days, "runs": total, "status": dict(status), "verdict": dict(verdict),
            "model_calls": dict(models.most_common()), "guards": dict(guards), "rules_cited": dict(rules.most_common()),
            "failures": dict(failures), "per_day": days_out,
            "audit": audit.head(audit.path_for(runs_dir.parent))}


PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width">
<title>Agent metrics</title><style>
body{font:15px/1.45 "Segoe UI",system-ui,sans-serif;background:#f6f1e7;color:#1f1a14;margin:0;padding:24px}
h1{font-size:22px;margin:0 0 4px}p{color:#6f665a;margin:0 0 18px}.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(260px,1fr));gap:14px}
.card{background:#fffaf0;border:1px solid #dfd4c0;border-radius:14px;padding:14px 16px}h2{font-size:14px;margin:0 0 8px;color:#0f766e}
table{border-collapse:collapse;width:100%}td,th{text-align:left;padding:4px 6px;border-bottom:1px solid #eee3cf}th{color:#6f665a;font-weight:600}
td.n{text-align:right;font-variant-numeric:tabular-nums}</style></head><body>
<h1>Agent metrics</h1><p id="sub">Aggregated from the run logs on this server.</p><div class="grid" id="grid"></div>
<script>
const T=(title,obj)=>`<div class="card"><h2>${title}</h2><table>${Object.entries(obj).map(([k,v])=>`<tr><td>${k}</td><td class="n">${v}</td></tr>`).join('')||'<tr><td>none</td></tr>'}</table></div>`;
fetch('/api/metrics').then(r=>r.json()).then(m=>{
 document.getElementById('sub').textContent=`${m.runs} runs in the last ${m.window_days} days, from each run's step log (model, ms, cost).`;
 const days=`<div class="card" style="grid-column:1/-1"><h2>Per day</h2><table><tr><th>day</th><th>runs</th><th>p50 s</th><th>p95 s</th><th>cost $</th><th>$ / run</th></tr>${m.per_day.map(d=>`<tr><td>${d.day}</td><td class="n">${d.runs}</td><td class="n">${d.p50_s}</td><td class="n">${d.p95_s}</td><td class="n">${d.cost_usd}</td><td class="n">${d.cost_per_run}</td></tr>`).join('')}</table></div>`;
 document.getElementById('grid').innerHTML=days+T('Status',m.status)+T('Verdict',m.verdict)+T('Model calls',m.model_calls)+T('Guards fired',m.guards)+T('Rules cited',m.rules_cited)+T('Failures by kind',m.failures)+T('Audit log',m.audit);
});
</script></body></html>"""
