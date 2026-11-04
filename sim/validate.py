"""Check an events.jsonl against the brief: schema, order, approvals, totals, and summary.json beside it.

    python -m sim.validate runs/mock/events.jsonl

Exit code 0 and "0 lỗi" when everything holds. Each rule below is one line of section 3, 5 or 9 of the brief; the
totals are recomputed from the lines themselves (not trusted from the metric events), and the platform fee is
recomputed from world.json for every completed order.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

from pydantic import ValidationError

from sim.schema import AGENTS, METRIC_KEYS, SCENES, Event
from sim.world import SHARE


def check(path: Path) -> tuple[list[str], dict]:
    errors: list[str] = []
    lines = path.read_text(encoding="utf-8").split("\n")
    if lines and lines[-1] == "":
        lines = lines[:-1]
    events: list[dict] = []
    for i, line in enumerate(lines, start=1):
        if not line.strip():
            errors.append(f"dòng {i}: dòng trống")
            continue
        try:
            raw = json.loads(line)
            Event.model_validate(raw)
            events.append(raw)
        except (json.JSONDecodeError, ValidationError) as exc:
            errors.append(f"dòng {i}: {str(exc).splitlines()[0][:200]}")
    if not events:
        return errors + ["không có sự kiện"], {}

    # ids and time
    for n, e in enumerate(events, start=1):
        if e["id"] != f"e{n:06d}":
            errors.append(f"{e['id']}: id phải là e{n:06d}")
            break
    for a, b in zip(events, events[1:]):
        if b["t"] < a["t"]:
            errors.append(f"{b['id']}: t giảm ({b['t']} < {a['t']})")
    modes = {e["mode"] for e in events}
    if len(modes) > 1:
        errors.append(f"trộn mode {sorted(modes)} (chỉ được khi vạch cắt, phải ghi rõ trong summary)")

    # scenes
    scenes = [e["scene"] for e in events if e["type"] == "scene"]
    if scenes != list(SCENES):
        errors.append(f"cảnh phải đủ và đúng thứ tự {list(SCENES)}, đang có {scenes}")
    if not 120 <= len(events) <= 250:
        errors.append(f"{len(events)} sự kiện, yêu cầu 120-250")

    # approvals: every request answered by exactly one approve that points to it
    requests = {e["id"]: e for e in events if e["type"] == "approve_request"}
    answered: dict[str, str] = {}
    for e in events:
        if e["type"] != "approve":
            continue
        req = (e.get("detail") or {}).get("request")
        if req not in requests:
            errors.append(f"{e['id']}: approve không trỏ tới approve_request nào")
            continue
        if req in answered:
            errors.append(f"{e['id']}: {req} được duyệt hai lần")
        answered[req] = e["id"]
        r = requests[req]
        if r["to"] != e["from"] or e["t"] < r["t"]:
            errors.append(f"{e['id']}: approve phải do {r['to']} gửi sau {req}")
        if not str(e["from"]).startswith("hu_"):
            errors.append(f"{e['id']}: chỉ người được duyệt, không phải {e['from']}")
    for rid in requests:
        if rid not in answered:
            errors.append(f"{rid}: approve_request chưa có approve")

    # recomputed totals versus the last metric line
    actions = sum(1 for e in events if e.get("from") in AGENTS and e["type"] in ("tool", "say")
                  and not (e.get("detail") or {}).get("auto"))
    closing = next((e for e in reversed(events) if e["type"] == "say" and (e.get("detail") or {}).get("auto")), None)
    cost = round(sum((e.get("usage") or {}).get("cost_usd", 0) for e in events), 6)
    approvals = sum(1 for e in events if e["type"] == "approve")
    paid = [e["detail"] for e in events if e["type"] == "say" and (e.get("detail") or {}).get("payment")]
    orders = sum(len(p["orders"]) for p in paid)
    gmv = sum(p["paid_vnd"] for p in paid)
    blocked = sum(1 for e in events if (e.get("detail") or {}).get("blocked"))
    minutes = sum(float((e.get("detail") or {}).get("minutes") or 0) for e in events if e["type"] == "approve")
    minutes += orders * 1.0          # hand-over: 1 minute per customer (sim/humans.py)
    metrics = [e for e in events if e["type"] == "metric"]
    if not metrics or metrics[-1] is not events[-1]:
        errors.append("sự kiện cuối phải là metric")
    last = metrics[-1]["metrics"] if metrics else {}
    want = {"orders": orders, "gmv_vnd": gmv, "agent_actions": actions, "agent_cost_usd": cost,
            "human_approvals": approvals, "human_minutes": round(minutes, 2), "blocked": blocked}
    for k, v in want.items():
        if abs(float(last.get(k, -1)) - float(v)) > 1e-6:
            errors.append(f"metric cuối {k}={last.get(k)} nhưng đếm từ các dòng được {v}")
    if set(last) != set(METRIC_KEYS):
        errors.append("metric cuối thiếu trường")
    if closing is not None:
        said = re.sub(r"\D", "", closing["text"])
        for key in ("orders", "agent_actions", "human_approvals"):
            if str(int(last.get(key, -1))) not in closing["text"]:
                errors.append(f"câu tổng kết không nêu {key}={last.get(key)}: {closing['text']}")
        if re.sub(r"\D", "", money_vnd := f"{last.get('agent_cost_vnd', 0):,.0f}") not in said:
            errors.append(f"câu tổng kết không khớp agent_cost_vnd={money_vnd}: {closing['text']}")
    for e in events:
        if e["mode"] == "mock" and (e.get("usage") or {}).get("cost_usd"):
            errors.append(f"{e['id']}: mock không được có chi phí")
        if e["mode"] == "live" and e.get("usage") and e["usage"].get("model") == "mock":
            errors.append(f"{e['id']}: live mà usage ghi mock")
    gap, since = 0, 0
    for e in events:
        since = 0 if e["type"] == "metric" else since + 1
        gap = max(gap, since)
    if gap > 20:
        errors.append(f"có {gap} sự kiện liền không có metric (tối đa 20)")
    say_agents = sum(1 for e in events if e["type"] == "say")
    if say_agents < 20:
        errors.append(f"chỉ {say_agents} say, yêu cầu khoảng 25")

    # content the brief requires
    if not any(e["type"] == "tool" and e["tool"]["name"] == "choz_listing" for e in events):
        errors.append("thiếu tool choz_listing (cảnh đăng bán)")
    if not blocked:
        errors.append("thiếu mặt hàng bị chặn")
    if not any(e["type"] == "approve_request" and (e.get("detail") or {}).get("kind") == "duyet_listing" for e in events):
        errors.append("thiếu cảnh báo kiểm chứng phải người duyệt")
    if not any(e["type"] == "handoff" for e in events):
        errors.append("thiếu handoff giữa agent")

    # fee recomputed from world.json for every completed order
    world_path = path.with_name("world.json")
    fees = {}
    if world_path.is_file():
        world = json.loads(world_path.read_text(encoding="utf-8"))
        settlement = next((e["detail"]["output"] for e in reversed(events) if e["type"] == "tool"
                           and e["tool"]["name"] == "settlement"), None)
        if settlement:
            rows = {(r["order"], r["supplier"]): r for r in settlement["rows"]}
            for o in world["orders"]:
                by: dict[str, int] = {}
                for li in o["lines"]:
                    by[li["supplier"]] = by.get(li["supplier"], 0) + li["qty"] * li["price_vnd"]
                for sup, amount in by.items():
                    row = rows.get((o["id"], sup))
                    if row is None:
                        continue      # world.json is the morning's world; substitutions change some lines
                    expect = {k: amount * pct // 100 for k, pct in SHARE.items()}
                    if row["amount_vnd"] == amount and any(row[k] != v for k, v in expect.items()):
                        errors.append(f"phí sai ở {o['id']}/{sup}: {row} khác {expect}")
            fees = settlement["totals"]
            if fees["fee_vnd"] != sum(fees[k] for k in SHARE) or fees["fee_vnd"] * 10 != fees["amount_vnd"]:
                errors.append(f"tổng phí không bằng 10 % hoặc không khớp 5/2/1/2: {fees}")
            if fees["amount_vnd"] != gmv:
                errors.append(f"đối soát {fees['amount_vnd']} khác tiền khách trả {gmv}")
        else:
            errors.append("thiếu tool settlement (cảnh đối soát)")

    # summary.json beside the events
    summary_path = path.with_name("summary.json")
    if summary_path.is_file():
        s = json.loads(summary_path.read_text(encoding="utf-8"))
        for k in ("orders", "gmv_vnd", "agent_actions", "human_approvals", "human_minutes", "blocked"):
            if abs(float(s.get(k, -1)) - float(last.get(k, -2))) > 1e-6:
                errors.append(f"summary.{k}={s.get(k)} khác metric cuối {last.get(k)}")
        if abs(float(s.get("cost_total_usd", -1)) - cost) > 1e-6:
            errors.append(f"summary.cost_total_usd={s.get('cost_total_usd')} khác tổng usage các dòng {cost}")
        if s.get("events") != len(events):
            errors.append(f"summary.events={s.get('events')} khác {len(events)} dòng")
        if s.get("mode") == "live" and not s.get("cost_matches_calls"):
            errors.append("chi phí trên dòng sự kiện không khớp tổng các lần gọi model")
    else:
        errors.append("thiếu summary.json cạnh events.jsonl")
    stats = {"events": len(events), "scenes": len(scenes), "says": say_agents, "approvals": approvals,
             "orders": orders, "gmv_vnd": gmv, "agent_actions": actions, "cost_usd": cost, "blocked": blocked,
             "fees": fees}
    return errors, stats


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(description="validate events.jsonl")
    ap.add_argument("events", type=Path)
    args = ap.parse_args()
    errors, stats = check(args.events)
    print(json.dumps(stats, ensure_ascii=False))
    for e in errors:
        print("LỖI", e)
    print(f"{len(errors)} lỗi")
    sys.exit(1 if errors else 0)


if __name__ == "__main__":
    main()
