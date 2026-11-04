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

from sim import staffing

DAY_MINUTES = 480
RESERVE = 1.2
DAYS_PER_MONTH = 26
ORDERS_1 = 30
STATIONS_50, ORDERS_50 = 50, 1500
BRIEF = "giả định khởi điểm trong yêu cầu demo (mục 6b), chờ anh Hùng xác nhận"
TABLE6 = "bảng phút người duyệt, mục 6 của yêu cầu demo"

# Monthly pay per role, VND, used only to turn people into money. ASSUMPTIONS for Hải Phòng 2026, to be confirmed
# by anh Hùng; change a number and both columns of the final table follow. Fractions of a person are paid pro rata
# (hour-based), which is how a shop pays a part-time helper.
WAGES = {
    "nhân viên trạm": 7_000_000,
    "nhân viên kho": 7_000_000,
    "điều phối": 10_000_000,
    "nhân viên nội dung": 9_000_000,
    "kế toán": 10_000_000,
    "chủ kho": 10_000_000,
}
WAGE_SOURCE = "giả định lương tháng Hải Phòng 2026, chờ anh Hùng xác nhận"
# The person who still does a row's minutes when agents handle the paperwork: the approver (hu_tram = nhân viên trạm,
# hu_kho = chủ kho) or the same physical worker.
AGENT_SIDE_WHO = {"approvals:thay_hang": "nhân viên trạm", "approvals:duyet_listing": "nhân viên trạm",
                  "approvals:doi_tra": "nhân viên trạm", "approvals:nhan_hang_tram": "nhân viên trạm",
                  "handover": "nhân viên trạm", "approvals:nhan_hang_kho": "chủ kho", "approvals:doi_soat": "chủ kho",
                  "same": None, "none": None}

# Where the work happens. Station work is done by the person already at each Trạm Z (the shop owner or the young
# operator, paid by the 5 % station share), so it is measured in minutes of THAT person's day and never pooled into
# central headcount. Central work (office + warehouse) is shared by all stations, so at 50 stations it is counted in
# WHOLE people, rounded up.
PLACE = {"nhan_dat_gio": "tram", "tra_loi_khach": "tram", "bao_cao_ngay": "tram", "nhan_hang_tram": "tram",
         "giao_khach": "tram", "gom_nhu_cau": "van_phong", "dang_ban": "van_phong", "kiem_thong_tin": "van_phong",
         "lich_xe": "van_phong", "doi_soat": "van_phong", "bao_cao_ncc": "van_phong", "chia_hang": "kho",
         "nhan_hang_kho": "kho"}
# With agents a person still approves. Who, where, and how that minute scales to 50 stations:
# hu_tram (station) approves substitutions, listing warnings, returns, receiving bags; hu_kho (warehouse owner, also
# the coordinator) approves receiving goods and the settlement, once per warehouse per day.
APPROVER = {"thay_hang": ("tram", "stations"), "duyet_listing": ("tram", "listings"), "doi_tra": ("tram", "requests"),
            "nhan_hang_tram": ("tram", "stations"), "nhan_hang_kho": ("kho", "warehouses"),
            "doi_soat": ("kho", "warehouses")}
APPROVAL_TASK = {"thay_hang": "Duyệt thay hàng thiếu (agent đề xuất)", "duyet_listing": "Duyệt tin đăng có cảnh báo"}
# With agents the office keeps at least this many people: someone owns the exceptions and answers for the numbers.
MIN_OFFICE_WITH_AGENTS = 1
STATION_SHARE_PCT = 5

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
     "manual_who": "nhân viên trạm", "kind": "tay_chan", "volume": "stations", "scale": "stations",
     "with_agent": "approvals:nhan_hang_tram", "agent": "không thay", "source": TABLE6 + "; cùng phút ở cả hai bên"},
    {"key": "giao_khach", "task": "Giao túi cho khách tại trạm", "unit": "mỗi khách", "minutes": 1,
     "manual_who": "nhân viên trạm", "kind": "tay_chan", "volume": "customers", "scale": "orders",
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

    def roles(pick, key: str, who_of) -> list[dict]:
        minutes: dict[str, float] = {}
        for r in rows:
            who = who_of(r)
            if who and pick(r, key):
                minutes[who] = minutes.get(who, 0.0) + pick(r, key)
        out = []
        for who, m in sorted(minutes.items(), key=lambda kv: -kv[1]):
            n = people(m)
            out.append({"role": who, "minutes_day": round(m, 1), "people": n, "people_whole": math.ceil(n),
                        "wage_vnd_month": WAGES[who], "cost_vnd_month": round(n * WAGES[who])})
        return out

    def block(factor_key: str, stations: int, order_count: int) -> dict:
        pick = (lambda r, k: r[k]) if factor_key == "1" else scaled
        manual = sum(pick(r, "manual_min") for r in rows)
        with_agent = sum(pick(r, "with_agent_min") for r in rows)
        roles_manual = roles(pick, "manual_min", lambda r: r["manual_who"])
        roles_agent = roles(pick, "with_agent_min",
                            lambda r: r["manual_who"] if r["with_agent"] == "same" else AGENT_SIDE_WHO.get(r["with_agent"]))
        people_cost_manual = sum(x["cost_vnd_month"] for x in roles_manual)
        people_cost_agent = sum(x["cost_vnd_month"] for x in roles_agent)
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
            "roles_manual": roles_manual, "roles_with_agents": roles_agent,
            "people_cost_vnd_month_manual": people_cost_manual,
            "people_cost_vnd_month_with_agents": people_cost_agent,
            "total_cost_vnd_month_manual": people_cost_manual,
            "total_cost_vnd_month_with_agents": people_cost_agent + round(cost_day * DAYS_PER_MONTH),
            "saving_vnd_month": people_cost_manual - people_cost_agent - round(cost_day * DAYS_PER_MONTH),
            "wage_source": WAGE_SOURCE,
        }

    view = honest_view(rows, vol, day_cost_vnd, orders, events, source.parent.parent if source else None)
    digest = hashlib.sha256(source.read_bytes()).hexdigest() if source and source.is_file() else None
    return {
        "mode": events[0]["mode"] if events else None,
        "per_tram_day": {"volumes": vol, "approval_minutes": appr, "event_human_minutes": last["human_minutes"],
                         "rows": [{k: r[k] for k in ("key", "task", "unit", "qty", "minutes", "manual_who", "kind",
                                                     "manual_min", "with_agent_min", "agent", "scale", "scale_note")}
                                  for r in rows]},
        "view": view,
        "tram_1": block("1", 1, ORDERS_1 if vol["orders_completed"] == ORDERS_1 else vol["orders_completed"]),
        "tram_50": block("50", STATIONS_50, ORDERS_50),
        "assumptions": [{k: n[k] for k in ("key", "task", "unit", "minutes", "source")} for n in norms]
                       + [{"key": "ngay_cong", "task": "Một người một ngày", "unit": "phút", "minutes": DAY_MINUTES,
                           "source": "yêu cầu demo mục 6b"},
                          {"key": "du_phong", "task": "Hệ số nghỉ phép, việc lặt vặt", "unit": "hệ số",
                           "minutes": RESERVE, "source": "yêu cầu demo mục 6b"},
                          {"key": "ngay_thang", "task": "Ngày làm việc mỗi tháng", "unit": "ngày",
                           "minutes": DAYS_PER_MONTH, "source": "yêu cầu demo mục 6b"}]
                       + [{"key": f"luong_{i}", "task": f"Lương tháng {who}", "unit": "đ/tháng", "minutes": wage,
                           "source": WAGE_SOURCE} for i, (who, wage) in enumerate(WAGES.items(), start=1)],
        "note": "định mức thủ công là giả định, số có agent đo từ ngày mô phỏng",
        "rules": ["việc tay chân (chia hàng, nhận hàng, giao khách) tính cùng số phút ở cả hai bên",
                  "chở hàng, đứng trạm không nằm trong bảng ở cả hai bên",
                  "phút có agent là phút người duyệt và giao nhận, đọc từ sự kiện approve và thanh toán"],
        "computed_from": {"events": str(source) if source else None, "sha256": digest},
    }


def last_cost_usd(events: list[dict]) -> float:
    return next(e["metrics"]["agent_cost_usd"] for e in reversed(events) if e["type"] == "metric")


def skus_per_station(events: list[dict]) -> int:
    """Distinct items on the station's picklist: one splitting line per item per station."""
    pick = next((e["detail"]["picklist"] for e in reversed(events) if e["type"] == "handoff"
                 and (e.get("detail") or {}).get("picklist")), None)
    return pick["items"] if pick else 12


def items_per_order(events: list[dict]) -> float:
    pick = next((e["detail"]["picklist"] for e in reversed(events) if e["type"] == "handoff"
                 and (e.get("detail") or {}).get("picklist")), None)
    if not pick:
        return 4.7
    items = sum(i["qty"] for v in pick["by_supplier"].values() for i in v)
    return items / max(pick["orders"], 1)


def scene_costs(events: list[dict], usd_vnd: float) -> dict[str, float]:
    """Agents' cost of each scene of a run, in đồng, from the usage on its event lines."""
    out: dict[str, float] = {}
    scene = ""
    for e in events:
        if e["type"] == "scene":
            scene = e["scene"]
        u = e.get("usage")
        if u and u.get("model") != "mock":
            out[scene] = out.get(scene, 0.0) + float(u.get("cost_usd") or 0) * usd_vnd
    return out


AGENT_DAYS_PER_MONTH = 30      # the agents work every day the network is open


def agent_cost_model(events: list[dict], usd_vnd: float, runs_dir: Path | None) -> dict:
    """Agents' cost per day for 50 stations (1,500 orders), from measured runs, scene by scene.

    - Baskets and settlement read every order, so their cost grows with orders: fitted as fixed + per order over this
      run and the live runs with 5× and 10× the orders (runs/scale_x5, runs/scale_x10).
    - Listing, lot checks and van routes grow with listings, suppliers and routes (staffing.py drivers).
    - Customer care grows with customer contacts: measured cost of one case × contacts a day (benchmark rate).
    """
    from sim import staffing as st
    base = scene_costs(events, usd_vnd)
    orders = sum(len((e.get("detail") or {}).get("orders", [])) for e in events
                 if e["type"] == "say" and (e.get("detail") or {}).get("payment")) or 30
    pts = [(orders, base.get("dat_gio", 0) + base.get("doi_soat", 0), "live_final")]
    if runs_dir is not None:
        for d in sorted(runs_dir.glob("scale_x*")):
            f = d / "events.jsonl"
            sm = d / "summary.json"
            if f.is_file() and sm.is_file() and json.loads(sm.read_text(encoding="utf-8")).get("mode") == "live":
                ev = read_events(f)
                sc = scene_costs(ev, usd_vnd)
                n = sum(len((e.get("detail") or {}).get("orders", [])) for e in ev
                        if e["type"] == "say" and (e.get("detail") or {}).get("payment"))
                pts.append((n, sc.get("dat_gio", 0) + sc.get("doi_soat", 0), d.name))
    xs = [x for x, _, _ in pts]
    if len(set(xs)) >= 2:
        k = len(pts)
        mx, my = sum(xs) / k, sum(y for _, y, _ in pts) / k
        slope = sum((x - mx) * (y - my) for x, y, _ in pts) / sum((x - mx) ** 2 for x in xs)
        fixed = my - slope * mx
    else:
        fixed, slope = 0.0, pts[0][1] / max(orders, 1)
    contacts_1 = sum(1 for e in events if e["type"] == "say" and str(e.get("from", "")).startswith("ho")
                     and e.get("to") == "ag_cham_khach") or 1
    stations = 50
    routes = -(-stations // st.STATIONS_PER_ROUTE)
    parts = {
        "gom đơn, đối soát (theo số đơn)": max(fixed + slope * ORDERS_50, 0.0),
        "đăng bán, kiểm chứng (theo số tin)": base.get("dang_ban", 0) * st.listings_at(stations) / st.listings_at(1),
        "kiểm lô ở kho (theo số nhà cung cấp)": base.get("gom_kho", 0) * st.suppliers_at(stations) / st.suppliers_at(1),
        "lộ trình xe (theo số tuyến)": base.get("ra_tram", 0) * routes,
        "chăm sóc khách (theo lượt hỏi)": base.get("nhan_hang", 0) / contacts_1
                                         * ORDERS_50 * st.CONTACTS_PER_100 / 100,
    }
    day = sum(parts.values())
    return {"fit_points": [{"orders": x, "cost_vnd_day": round(y), "run": r} for x, y, r in pts],
            "fixed_vnd_day": round(fixed), "per_order_vnd": round(slope, 1),
            "parts_vnd_day": {k: round(v) for k, v in parts.items()},
            "cost_vnd_day_1500": round(day), "days_per_month": AGENT_DAYS_PER_MONTH,
            "cost_vnd_month_1500": int(round(day * AGENT_DAYS_PER_MONTH, -3)),
            "cost_vnd_per_order_1500": round(day / ORDERS_50),
            "method": "đo từ các lượt chạy thật (30, 150, 300 đơn), cộng theo từng khâu"}


def honest_view(rows: list[dict], vol: dict, day_cost_vnd: float, orders: int, events: list[dict],
                runs_dir: Path | None = None) -> dict:
    """The final screen's numbers, in units a jury can picture.

    - At each station: minutes per day of the person already there (no wage on either side: the same person works in
      both cases, paid by the station's 5 % share). Agents shorten that person's Chợ Z paperwork.
    - Central team (office + warehouse) for 50 stations: whole people, rounded up; the office is rounded as one pool
      (one person can do several office jobs), which is the conservative choice for the manual side.
    - Money compares the central team only, plus the agents' bill; station people are outside both columns.
    """
    def split_agent(r: dict) -> list[tuple[str, str, float]]:
        """(place, scale, minutes) of the person who still works on this row when agents are used."""
        how = r["with_agent"]
        if not r["with_agent_min"]:
            return []
        if how == "same":
            return [(PLACE[r["key"]], r["scale"], r["with_agent_min"])]
        if how == "handover":
            return [("tram", "orders", r["with_agent_min"])]
        kind = how.split(":", 1)[1]
        place, scale = APPROVER[kind]
        return [(place, scale, r["with_agent_min"])]

    station_rows = []
    for r in rows:
        if PLACE[r["key"]] == "tram" or any(p == "tram" for p, _, _ in split_agent(r)):
            agent_min = sum(m for p, _, m in split_agent(r) if p == "tram")
            kind = r["with_agent"].split(":", 1)[-1]
            task = r["task"] if PLACE[r["key"]] == "tram" else APPROVAL_TASK.get(kind, r["task"])
            station_rows.append({"task": task, "manual_min": r["manual_min"] if PLACE[r["key"]] == "tram" else 0.0,
                                 "with_agent_min": round(agent_min, 2), "kind": r["kind"]})
    st_manual = sum(x["manual_min"] for x in station_rows)
    st_agent = sum(x["with_agent_min"] for x in station_rows)
    share_day = 0
    settle = next((e["detail"]["output"] for e in reversed(events) if e["type"] == "tool"
                   and e["tool"]["name"] == "settlement"), None)
    if settle:
        share_day = int(settle["totals"]["tram"])

    def central(scale_to_50: bool) -> dict:
        factor = (lambda sc: SCALE[sc]) if scale_to_50 else (lambda sc: 1)
        man: dict[str, float] = {}
        agt: dict[str, float] = {}
        for r in rows:
            place = PLACE[r["key"]]
            if place != "tram":
                man[r["manual_who"]] = man.get(r["manual_who"], 0.0) + r["manual_min"] * factor(r["scale"])
            for p, sc, m in split_agent(r):
                if p == "kho":
                    who = r["manual_who"] if r["with_agent"] == "same" else "chủ kho"
                    agt[who] = agt.get(who, 0.0) + m * factor(sc)
        return {"manual": man, "agent": agt}

    def whole(minutes: float) -> int:
        return math.ceil(round(minutes / DAY_MINUTES * RESERVE, 4)) if minutes > 0 else 0

    def team(c: dict) -> dict:
        office_roles = ("điều phối", "nhân viên nội dung", "kế toán")
        office_fte = {k: round(v / DAY_MINUTES * RESERVE, 2) for k, v in c["manual"].items() if k in office_roles}
        office_min = sum(v for k, v in c["manual"].items() if k in office_roles)
        office_people = whole(office_min)
        office_wage = (sum(office_fte[k] * WAGES[k] for k in office_fte) / sum(office_fte.values())) if office_fte else 0
        kho_staff_m = whole(c["manual"].get("nhân viên kho", 0.0))
        kho_staff_a = whole(c["agent"].get("nhân viên kho", 0.0))
        manual_rows = [{"role": "Văn phòng (điều phối, đăng bán, kế toán)", "people": office_people,
                        "detail": office_fte, "cost_vnd_month": int(round(office_people * office_wage, -3))},
                       {"role": "Chủ kho", "people": 1, "cost_vnd_month": WAGES["chủ kho"]},
                       {"role": "Nhân viên kho (chia hàng)", "people": kho_staff_m,
                        "cost_vnd_month": kho_staff_m * WAGES["nhân viên kho"]}]
        agent_rows = [{"role": "Điều phối, chịu trách nhiệm số liệu", "people": MIN_OFFICE_WITH_AGENTS,
                       "cost_vnd_month": MIN_OFFICE_WITH_AGENTS * WAGES["điều phối"]},
                      {"role": "Chủ kho (duyệt nhận hàng, duyệt đối soát)", "people": 1,
                       "minutes_day": round(c["agent"].get("chủ kho", 0.0), 1), "cost_vnd_month": WAGES["chủ kho"]},
                      {"role": "Nhân viên kho (chia hàng)", "people": kho_staff_a,
                       "cost_vnd_month": kho_staff_a * WAGES["nhân viên kho"]}]
        return {"manual": manual_rows, "with_agents": agent_rows,
                "manual_people": sum(x["people"] for x in manual_rows),
                "with_agents_people": sum(x["people"] for x in agent_rows),
                "manual_cost_vnd_month": sum(x["cost_vnd_month"] for x in manual_rows),
                "with_agents_staff_cost_vnd_month": sum(x["cost_vnd_month"] for x in agent_rows),
                "office_minutes_day_manual": round(office_min, 1)}

    c1 = central(False)
    model = agent_cost_model(events, day_cost_vnd / max(float(last_cost_usd(events)), 1e-9), runs_dir)
    office_1 = sum(v for k, v in c1["manual"].items() if k in ("điều phối", "nhân viên nội dung", "kế toán"))
    return {
        "station": {
            "who": "người trực Trạm Z (chủ tiệm hoặc bạn trẻ đã có ở tiệm); cùng một người ở cả hai cách vận hành",
            "rows": station_rows,
            "manual_minutes_day": round(st_manual, 1), "with_agents_minutes_day": round(st_agent, 1),
            "share_pct": STATION_SHARE_PCT, "share_vnd_day": share_day,
            "share_vnd_month": share_day * DAYS_PER_MONTH,
            "note": "không tính lương ở cả hai bên: trạm được trả bằng 5 % phí trên đơn đã giao",
        },
        "central_1": {
            "office_minutes_day_manual": round(office_1, 1), "office_minutes_day_with_agents": 0.0,
            "kho_minutes_day": round(c1["manual"].get("nhân viên kho", 0) + c1["manual"].get("chủ kho", 0), 1),
            "approval_minutes_day_with_agents": round(c1["agent"].get("chủ kho", 0.0), 1),
            "note": "1 trạm chưa đủ việc cho một bộ máy riêng; tính bằng phút mỗi ngày",
        },
        "central_50": {**staffing.team(50, skus_per_station(events), model["cost_vnd_month_1500"]),
                       "rounding": "người toàn thời gian làm tròn lên theo từng vai (ca 480 phút làm hiệu quả 85 %, hệ số "
                                   "phủ ca 1,2); việc theo giờ tính theo giờ công; xe thuê theo chuyến",
                       "agent_cost_model": model},
        "assumptions": staffing.assumptions(),
        "agent_cost_vnd_per_order": round(day_cost_vnd / orders),
        "excluded": ["người trực 50 trạm (ở cả hai bên, trả bằng 5 % phí)", "xe, xăng dầu", "mặt bằng kho",
                     "nhận và giao hàng ở trạm"],
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
    v = data["view"]
    st, c50 = v["station"], v["central_50"]
    print(f"mỗi trạm: {st['manual_minutes_day']} → {st['with_agents_minutes_day']} phút/ngày của người trực trạm; "
          f"trạm nhận {st['share_vnd_day']:,} đ/ngày")
    print(f"bộ máy 50 trạm: {c50['manual_people']} người ({c50['manual_cost_vnd_month']:,} đ/tháng) → "
          f"{c50['with_agents_people']} người + 6 agent ({c50['with_agents_cost_vnd_month']:,} đ/tháng); "
          f"ít hơn {c50['saving_vnd_month']:,} đ/tháng")
    for side in ("manual", "with_agents"):
        print("   " + side + ": " + "; ".join(f"{x['role']} {x['people'] or ''}{' ' + str(x['hours_day']) + 'h' if x.get('hours_day') else ''} {x['cost_vnd_month'] / 1e6:.1f}tr" for x in c50[side]))
    print(f"   {c50['fee_pool']['text']} = {c50['fee_pool']['vnd_month']:,} đ; trong phí: {c50['within_fee_pool']}")
    mo = c50["agent_cost_model"]
    print(f"   agent: {mo['fit_points']} → cố định {mo['fixed_vnd_day']:,} + {mo['per_order_vnd']} đ/đơn; "
          f"{mo['parts_vnd_day']} → {mo['cost_vnd_month_1500']:,} đ/tháng ({mo['cost_vnd_per_order_1500']} đ/đơn)")
    for k in ("tram_1", "tram_50"):
        b = data[k]
        print(f"{k}: làm tay {b['manual_people']} người ({b['people_cost_vnd_month_manual']:,} đ/tháng) · với agent "
              f"{b['with_agents_people']} người ({b['people_cost_vnd_month_with_agents']:,} đ) + {b['agents']} agent "
              f"({b['agent_cost_vnd_month']:,} đ) · giảm {b['human_minutes_saved_pct']} % phút công")
        for side in ("roles_manual", "roles_with_agents"):
            print("   " + side + ": " + "; ".join(f"{x['role']} {x['people']}" for x in b[side]))


if __name__ == "__main__":
    main()
