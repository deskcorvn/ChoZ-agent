"""headcount.json: how many people the day's work takes by hand, and how many people plus agents (section 6b).

    python -m sim.workload runs/mock/events.jsonl [--norms my-norms.json] [--out headcount.json]

Three steps, all visible in the output:
1. NORMS: minutes per unit when done by hand, each with its source. The starting values are the brief's assumptions,
   to be confirmed line by line by anh Hùng; change a number (or pass --norms) and every total follows.
2. Volume of the simulated day, counted from events.jsonl (baskets, listings, orders, requests, reports ...).
   Minutes with agents are the people's minutes in the same file (approvals and hand-overs), never estimated.
3. People = minutes / 480 × 1.2 (leave and odd jobs), for 1 station (30 orders) and 50 stations (1,500 orders).

Physical work (picking at the warehouse, receiving goods, handing bags to customers) is counted on BOTH sides with
the same minutes: agents do not replace it. Driving and standing at the station are outside the table on both sides.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
from collections import Counter
from pathlib import Path

DAY_MINUTES = 480
RESERVE = 1.2
DAYS_PER_MONTH = 26
ORDERS_1 = 30
STATIONS_50, ORDERS_50 = 50, 1500
BRIEF = "giả định khởi điểm trong yêu cầu demo (mục 6b), chờ anh Hùng xác nhận"
TABLE6 = "bảng phút người duyệt, mục 6 của yêu cầu demo"

# unit volume key → how it scales from 1 station to 50 (factor applied to the day's volume)
SCALE = {"orders": ORDERS_50 / ORDERS_1, "stations": STATIONS_50, "warehouses": 1, "suppliers": 1, "listings": 1,
         "requests": ORDERS_50 / ORDERS_1}
SCALE_NOTE = {
    "orders": "theo số đơn (1.500 / 30)", "stations": "theo số trạm (50)", "warehouses": "1 kho cho cả 50 trạm",
    "suppliers": "giả định vẫn 5 nhà cung cấp (chờ xác nhận)",
    "listings": "giả định số tin đăng mỗi ngày không tăng theo số trạm (chờ xác nhận)",
    "requests": "theo số đơn (tỷ lệ khách hỏi như ngày mô phỏng)"}

# kind: "van_phong" (paperwork and coordination an agent can do) or "tay_chan" (physical, never replaced)
# with_agent: how a person's minutes are counted with agents: "approvals:<kind>" (from approve events),
#   "handover" (customers handed bags), "same" (physical work outside the events: same minutes as by hand), "none".
NORMS = [
    {"key": "nhan_dat_gio", "task": "Nhận đặt Giỏ qua tin nhắn, ghi sổ", "unit": "mỗi đơn", "minutes": 2,
     "manual_who": "nhân viên trạm", "kind": "van_phong", "volume": "baskets", "scale": "orders",
     "with_agent": "none", "agent": "ag_chia_don (khách đặt trên Mini App)", "source": BRIEF},
    {"key": "gom_nhu_cau", "task": "Gom nhu cầu thành đơn đặt nhà cung cấp", "unit": "mỗi trạm mỗi ngày", "minutes": 30,
     "manual_who": "điều phối", "kind": "van_phong", "volume": "stations", "scale": "stations",
     "with_agent": "approvals:thay_hang", "agent": "ag_chia_don", "source": BRIEF},
    {"key": "dang_ban", "task": "Đăng bán một sản phẩm (chụp, viết, nhập)", "unit": "mỗi sản phẩm", "minutes": 12,
     "manual_who": "nhân viên nội dung", "kind": "van_phong", "volume": "listings", "scale": "listings",
     "with_agent": "approvals:duyet_listing", "agent": "ag_dang_ban",
     "source": BRIEF + "; repo ChoZ-agent ghi 10-15 phút"},
    {"key": "kiem_thong_tin", "task": "Kiểm thông tin sản phẩm, hồ sơ nguồn gốc", "unit": "mỗi sản phẩm", "minutes": 5,
     "manual_who": "nhân viên nội dung", "kind": "van_phong", "volume": "listings_checked", "scale": "listings",
     "with_agent": "none", "agent": "ag_kiem_chung (duyệt listing tính ở dòng đăng bán)", "source": BRIEF},
    {"key": "lich_xe", "task": "Lên lịch xe và chuyến gom", "unit": "mỗi kho mỗi ngày", "minutes": 20,
     "manual_who": "điều phối", "kind": "van_phong", "volume": "warehouses", "scale": "warehouses",
     "with_agent": "none", "agent": "ag_giao_nhan", "source": BRIEF},
    {"key": "chia_hang", "task": "Chia hàng theo đơn tại kho", "unit": "mỗi đơn", "minutes": 1,
     "manual_who": "nhân viên kho", "kind": "tay_chan", "volume": "orders_picked", "scale": "orders",
     "with_agent": "same", "agent": "không thay (agent chỉ lên phiếu gom)", "source": BRIEF},
    {"key": "tra_loi_khach", "task": "Trả lời khách hỏi, đổi trả", "unit": "mỗi yêu cầu", "minutes": 4,
     "manual_who": "nhân viên trạm", "kind": "van_phong", "volume": "requests", "scale": "requests",
     "with_agent": "approvals:doi_tra", "agent": "ag_cham_khach", "source": BRIEF},
    {"key": "doi_soat", "task": "Đối soát đơn, tính phí, chia tiền", "unit": "mỗi đơn", "minutes": 1.5,
     "manual_who": "kế toán", "kind": "van_phong", "volume": "orders_completed", "scale": "orders",
     "with_agent": "approvals:doi_soat", "agent": "ag_doi_soat", "source": BRIEF},
    {"key": "bao_cao_ncc", "task": "Báo cáo số cho nhà cung cấp", "unit": "mỗi nhà cung cấp mỗi ngày", "minutes": 10,
     "manual_who": "kế toán", "kind": "van_phong", "volume": "supplier_reports", "scale": "suppliers",
     "with_agent": "none", "agent": "ag_doi_soat", "source": BRIEF},
    {"key": "bao_cao_ngay", "task": "Báo cáo ngày cho quản lý", "unit": "mỗi trạm mỗi ngày", "minutes": 15,
     "manual_who": "nhân viên trạm", "kind": "van_phong", "volume": "stations", "scale": "stations",
     "with_agent": "none", "agent": "ag_doi_soat", "source": BRIEF},
    {"key": "nhan_hang_kho", "task": "Nhận hàng tại kho", "unit": "mỗi kho mỗi ngày", "minutes": 10,
     "manual_who": "chủ kho", "kind": "tay_chan", "volume": "warehouses", "scale": "warehouses",
     "with_agent": "approvals:nhan_hang_kho", "agent": "không thay", "source": TABLE6 + "; cùng phút ở cả hai bên"},
    {"key": "nhan_hang_tram", "task": "Nhận hàng tại trạm", "unit": "mỗi trạm mỗi ngày", "minutes": 10,
     "manual_who": "người trực trạm", "kind": "tay_chan", "volume": "stations", "scale": "stations",
     "with_agent": "approvals:nhan_hang_tram", "agent": "không thay", "source": TABLE6 + "; cùng phút ở cả hai bên"},
    {"key": "giao_khach", "task": "Giao túi cho khách tại trạm", "unit": "mỗi khách", "minutes": 1,
     "manual_who": "người trực trạm", "kind": "tay_chan", "volume": "customers", "scale": "orders",
     "with_agent": "handover", "agent": "không thay", "source": TABLE6 + "; cùng phút ở cả hai bên"},
]


def read_events(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def volumes(events: list[dict]) -> dict:
    """What the day contained, counted from the event lines."""
    last = next(e["metrics"] for e in reversed(events) if e["type"] == "metric")
    tools = Counter(e["tool"]["name"] for e in events if e["type"] == "tool")
    baskets = sum(len((e.get("detail") or {}).get("households", [])) for e in events
                  if e["type"] == "say" and e.get("from", "").startswith("ho") and e.get("to") == "ag_chia_don")
    paid = [o for e in events if e["type"] == "say" and (e.get("detail") or {}).get("payment")
            for o in e["detail"]["orders"]]
    picked = max((e["detail"]["picklist"]["orders"] for e in events
                  if e["type"] == "handoff" and (e.get("detail") or {}).get("picklist")), default=0)
    return {
        "baskets": baskets,
        "stations": 1,
        "warehouses": 1,
        "listings": tools["choz_listing"],
        "listings_checked": tools["verify_listing"],
        "orders_picked": picked or baskets,
        "requests": sum(1 for e in events if e["type"] == "say" and e.get("from", "").startswith("ho")
                        and e.get("to") == "ag_cham_khach"),
        "orders_completed": int(last["orders"]),
        "supplier_reports": sum(1 for e in events if e["type"] == "say" and e.get("from") == "ag_doi_soat"
                                and e.get("to", "").startswith("ncc")),
        "customers": len(paid),
    }


def approvals(events: list[dict]) -> dict[str, float]:
    minutes: Counter[str] = Counter()
    for e in events:
        if e["type"] == "approve":
            d = e.get("detail") or {}
            minutes[d.get("kind", "?")] += float(d.get("minutes") or 0)
    return dict(minutes)


def people(minutes: float) -> float:
    return round(minutes / DAY_MINUTES * RESERVE, 2)


def compute(events: list[dict], norms: list[dict] | None = None, source: Path | None = None) -> dict:
    norms = norms or NORMS
    vol = volumes(events)
    appr = approvals(events)
    last = next(e["metrics"] for e in reversed(events) if e["type"] == "metric")
    handover = vol["customers"] * next((n["minutes"] for n in norms if n["with_agent"] == "handover"), 1)
    rows = []
    for n in norms:
        qty = vol[n["volume"]]
        manual = qty * n["minutes"]
        how = n["with_agent"]
        if how == "same":
            with_agent = manual
        elif how == "handover":
            with_agent = handover
        elif how.startswith("approvals:"):
            with_agent = appr.get(how.split(":", 1)[1], 0.0)
        else:
            with_agent = 0.0
        rows.append({**n, "qty": qty, "manual_min": round(manual, 2), "with_agent_min": round(with_agent, 2),
                     "scale_note": SCALE_NOTE[n["scale"]]})
    # Physical rows count the same minutes on both sides: take the larger of the two so agents never "save" them.
    for r in rows:
        if r["kind"] == "tay_chan":
            r["manual_min"] = r["with_agent_min"] = max(r["manual_min"], r["with_agent_min"])

    def scaled(r: dict, key: str) -> float:
        return r[key] * SCALE[r["scale"]]

    day_cost_vnd = float(last["agent_cost_vnd"])
    day_cost_usd = float(last["agent_cost_usd"])
    orders = max(vol["orders_completed"], 1)

    def block(factor_key: str, stations: int, order_count: int) -> dict:
        pick = (lambda r, k: r[k]) if factor_key == "1" else scaled
        manual = sum(pick(r, "manual_min") for r in rows)
        with_agent = sum(pick(r, "with_agent_min") for r in rows)
        office_m = sum(pick(r, "manual_min") for r in rows if r["kind"] == "van_phong")
        office_a = sum(pick(r, "with_agent_min") for r in rows if r["kind"] == "van_phong")
        cost_day = day_cost_vnd * order_count / orders
        return {
            "stations": stations, "orders_per_day": order_count,
            "manual_minutes": round(manual, 1), "with_agents_minutes": round(with_agent, 1),
            "manual_people": people(manual), "with_agents_people": people(with_agent),
            "manual_people_whole": math.ceil(people(manual)), "with_agents_people_whole": math.ceil(people(with_agent)),
            "office": {"manual_minutes": round(office_m, 1), "with_agents_minutes": round(office_a, 1),
                       "manual_people": people(office_m), "with_agents_people": people(office_a)},
            "physical": {"minutes": round(manual - office_m, 1), "people": people(manual - office_m),
                         "note": "tay chân: cùng số phút ở hai bên, agent không thay"},
            "agents": 6,
            "agent_cost_vnd_day": round(cost_day), "agent_cost_vnd_month": round(cost_day * DAYS_PER_MONTH),
            "agent_cost_usd_month": round(day_cost_usd * order_count / orders * DAYS_PER_MONTH, 2),
            "agent_cost_vnd_per_order": round(day_cost_vnd / orders),
            "human_minutes_saved_pct": round(100 * (manual - with_agent) / manual, 1) if manual else 0.0,
        }

    digest = hashlib.sha256(source.read_bytes()).hexdigest() if source and source.is_file() else None
    return {
        "mode": events[0]["mode"] if events else None,
        "per_tram_day": {"volumes": vol, "approval_minutes": appr, "event_human_minutes": last["human_minutes"],
                         "rows": [{k: r[k] for k in ("key", "task", "unit", "qty", "minutes", "manual_who", "kind",
                                                     "manual_min", "with_agent_min", "agent", "scale", "scale_note")}
                                  for r in rows]},
        "tram_1": block("1", 1, ORDERS_1 if vol["orders_completed"] == ORDERS_1 else vol["orders_completed"]),
        "tram_50": block("50", STATIONS_50, ORDERS_50),
        "assumptions": [{k: n[k] for k in ("key", "task", "unit", "minutes", "source")} for n in norms]
                       + [{"key": "ngay_cong", "task": "Một người một ngày", "unit": "phút", "minutes": DAY_MINUTES,
                           "source": "yêu cầu demo mục 6b"},
                          {"key": "du_phong", "task": "Hệ số nghỉ phép, việc lặt vặt", "unit": "hệ số",
                           "minutes": RESERVE, "source": "yêu cầu demo mục 6b"},
                          {"key": "ngay_thang", "task": "Ngày làm việc mỗi tháng", "unit": "ngày",
                           "minutes": DAYS_PER_MONTH, "source": "yêu cầu demo mục 6b"}],
        "note": "định mức thủ công là giả định, số có agent đo từ ngày mô phỏng",
        "rules": ["việc tay chân (chia hàng, nhận hàng, giao khách) tính cùng số phút ở cả hai bên",
                  "chở hàng, đứng trạm không nằm trong bảng ở cả hai bên",
                  "phút có agent là phút người duyệt và giao nhận, đọc từ sự kiện approve và thanh toán"],
        "computed_from": {"events": str(source) if source else None, "sha256": digest},
    }


def write(events_path: Path, out: Path, norms_path: Path | None = None) -> dict:
    norms = None
    if norms_path:
        override = {n["key"]: n["minutes"] for n in json.loads(norms_path.read_text(encoding="utf-8"))}
        norms = [{**n, "minutes": override.get(n["key"], n["minutes"]),
                  "source": n["source"] if n["key"] not in override else f"đổi theo {norms_path.name}"} for n in NORMS]
    data = compute(read_events(events_path), norms, events_path)
    out.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    return data


def main() -> None:
    ap = argparse.ArgumentParser(description="headcount.json from events.jsonl")
    ap.add_argument("events", type=Path)
    ap.add_argument("--norms", type=Path, help='JSON list of {"key": ..., "minutes": ...} replacing starting norms')
    ap.add_argument("--out", type=Path)
    args = ap.parse_args()
    data = write(args.events, args.out or args.events.with_name("headcount.json"), args.norms)
    for k in ("tram_1", "tram_50"):
        b = data[k]
        print(f"{k}: làm tay {b['manual_people']} người · với agent {b['with_agents_people']} người + {b['agents']} agent · "
              f"agent {b['agent_cost_vnd_month']:,} đ/tháng · giảm {b['human_minutes_saved_pct']} % phút công")


if __name__ == "__main__":
    main()
