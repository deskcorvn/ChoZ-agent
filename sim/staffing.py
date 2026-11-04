"""Staffing of the central team (warehouse + office) of a Trạm Z network, by hand vs with agents.

The flow is Chợ Z's own design and is the same in both columns: the warehouse splits goods BY STATION AND BY ITEM
(one crate per station, quantities from the picklist), never bag by bag per order; the person at each station hands
each household its items from the app's order. Pay follows the design too: coordinators and managers are full-time
staff with insurance; warehouse splitting, receiving checks and station visits are hourly jobs for young people;
the van is hired per trip from a transport partner.

Every rate carries its source. SOURCED = labour law, job postings in Hải Phòng 2026, industry benchmarks, Chợ Z's
own plan; ASSUMED = a number only Chợ Z can confirm, listed on the final screen. Change a number here and both
columns follow; nothing is typed into the stage.
"""
from __future__ import annotations

import math

SOURCES = {
    "luong_toi_thieu": "Nghị định 293/2025/NĐ-CP: vùng I (nội thành Hải Phòng, Cát Hải) 5.310.000 đ/tháng, 25.500 đ/giờ",
    "bao_hiem": "BHXH 17,5 % + BHYT 3 % + BHTN 1 % (Luật 41/2024/QH15) + kinh phí công đoàn 2 % = 23,5 % trên lương",
    "luong_hp": "tin tuyển dụng Hải Phòng 2026 trên VietnamWorks, Joboko, TopCV, 123job, CareerViet, tuyendung.winmart.vn",
    "theo_gio": "Định hướng Chợ Z: sinh viên part-time, trả lương theo giờ; 30.000 đ/giờ, trên mức tối thiểu 25.500 đ/giờ",
    "chia_theo_tram": "thiết kế Chợ Z: kho chia theo trạm, theo mặt hàng; ước 20–25 giờ công cho 600 dòng chia (bên chấm)",
    "lien_he_khach": "liên hệ chăm sóc khách: 3,2 / 100 đơn trung vị bán lẻ (Gartner 2024, qua Stealth Agents) đến 20 / 100 "
                     "đơn ngành thực phẩm (Gorgias Ecom Lab 2026); thời gian xử lý 3–5 phút, khiếu nại 8–15 phút (HiveDesk)",
    "phi": "slide tài chính Chợ Z: 50 trạm, 1.500 đơn/ngày, Giỏ trung bình 100.000 đ, 4,5 tỷ tiền hàng/tháng; phí 10 %, "
           "trạm 5 %, kho và giao hàng 2 %, bộ máy 2 %",
}

# ── time and pay ───────────────────────────────────────────────────────────────────────────────────────
OPERATING_DAYS = 30          # the network runs every day (Chợ Z plan: 1.500 đơn × 30 ngày)
SHIFT_MIN = 480
PRODUCTIVE = 0.85            # ASSUMED: breaks, briefings, handovers (common planning figure 80–85 %)
COVERAGE = 30 / 26 * 1.04    # a full-time person works 26 days a month and takes 12 days of leave a year
EMPLOYER_FACTOR = 1 + 0.235 + 1 / 12      # insurance and union fee 23.5 %, 13th-month salary (full-time only)
HOURLY_VND = 30_000          # SOURCED: Chợ Z plan; legal minimum 25.500 đ/giờ in region I
WAGES = {                    # full-time, VND a month, Hải Phòng 2026 (middle of posted ranges)
    "chăm sóc khách hàng": 9_000_000,                # SOURCED: CSKH online Hải Phòng 7–12 tr, 5,5–10 tr
    "thu mua, đặt hàng nhà cung cấp": 11_000_000,    # ASSUMED: between accountant and dispatcher
    "điều phối xe": 13_000_000,                      # SOURCED: điều phối giao vận WinCommerce Hải Phòng 12–14 tr
    "đăng bán, nội dung": 9_000_000,                 # ASSUMED: like online customer care
    "kế toán, đối soát": 11_000_000,                 # SOURCED: kế toán Hải Phòng 9–15 tr, công nợ 8–13 tr
    "điều phối, chịu trách nhiệm số liệu": 13_000_000,   # SOURCED as điều phối
    "quản lý": 16_000_000,                           # SOURCED: quản lý kho Hải Phòng 10–18 tr, 14 tr trở lên
}

# ── volumes of a network of S stations (30 orders each) ───────────────────────────────────────────────
SPLIT_MIN_PER_LINE = 2.25    # ASSUMED: 20–25 h for 600 station × item lines (grader's estimate), Chợ Z to confirm
RECEIVE_HOURS = 2            # ASSUMED: hourly receiving check a day at 50 stations (weigh, labels, dates)
VISIT_HOURS = 2 * 4          # ASSUMED: two hourly station visitors, 4 h a day each
TRANSPORT_VND_MONTH = 65_000_000   # ASSUMED: hired van per trip, 60–70 tr/tháng, waiting for the partner's quote
TRANSPORT_RANGE = (60_000_000, 70_000_000)
CONTACTS_PER_100 = 10        # SOURCED range 3,2–20 per 100 orders; middle of the range
CONTACT_MIN = 5              # SOURCED 3–5 min blended, complaints 8–15
HUMAN_SHARE = 0.35           # SOURCED share of returns, refunds, complaints in e-commerce contacts (~30–45 %)
APPROVE_MIN = 2              # ASSUMED: read the agent's proposal and approve
STATIONS_PER_ROUTE = 20      # SOURCED range 15–30 stations per vehicle (grid warehouses, China)
MANAGERS = {"manual": 4, "with_agents": 2}   # Chợ Z decision 04/11: warehouse, stations, suppliers, money → 2 with agents
AVG_BASKET_VND = 100_000     # SOURCED: Chợ Z financial slide
FEE_POOL_PCT = 4             # kho và giao hàng 2 % + bộ máy 2 %


def suppliers_at(stations: int) -> int:
    return 5 if stations == 1 else 15        # ASSUMED: three times as many suppliers at 50 stations


def listings_at(stations: int) -> int:
    return 3 if stations == 1 else 15        # ASSUMED: 15 new or changed listings a day at 50 stations


def people(minutes: float) -> int:
    """Whole full-time people for this many minutes of work a day, rounded up."""
    return math.ceil(round(minutes / (SHIFT_MIN * PRODUCTIVE) * COVERAGE, 4)) if minutes > 0 else 0


def full_time(role: str, n: int) -> int:
    return int(round(n * WAGES[role] * EMPLOYER_FACTOR, -3))


def hourly(hours_day: float) -> int:
    return int(round(hours_day * HOURLY_VND * OPERATING_DAYS, -3))


def fee_pool(stations: int) -> dict:
    gmv = 30 * stations * AVG_BASKET_VND * OPERATING_DAYS
    return {"gmv_vnd_month": gmv, "pct": FEE_POOL_PCT, "vnd_month": gmv * FEE_POOL_PCT // 100,
            "text": f"Phí nuôi bộ máy trung tâm: {FEE_POOL_PCT} % của {gmv / 1e9:.1f} tỷ tiền hàng/tháng".replace(".", ","),
            "source": SOURCES["phi"]}


def team(stations: int, skus_per_station: int, agent_cost_vnd_month: int) -> dict:
    orders = 30 * stations
    routes = math.ceil(stations / STATIONS_PER_ROUTE)
    sup, lst = suppliers_at(stations), listings_at(stations)
    contacts = orders * CONTACTS_PER_100 / 100
    lines = stations * skus_per_station
    split_hours = lines * SPLIT_MIN_PER_LINE / 60

    # Same in both columns: physical work on the same flow, hourly or hired.
    same = [
        {"role": "chia hàng theo trạm (theo giờ)", "kind": "tay_chan", "pay": "theo giờ 30.000 đ",
         "people": None, "hours_day": round(split_hours, 1), "cost_vnd_month": hourly(split_hours),
         "how": f"{stations} trạm × {skus_per_station} mặt hàng = {lines} dòng chia × {SPLIT_MIN_PER_LINE:g} phút".replace(".", ",")},
        {"role": "kiểm hàng nhập kho (theo giờ)", "kind": "tay_chan", "pay": "theo giờ 30.000 đ",
         "people": None, "hours_day": RECEIVE_HOURS, "cost_vnd_month": hourly(RECEIVE_HOURS),
         "how": f"{RECEIVE_HOURS} giờ/ngày: cân, đo nhiệt độ, xem hạn {sup} chuyến hàng"},
        {"role": "giám sát trạm (theo giờ)", "kind": "tay_chan", "pay": "theo giờ 30.000 đ",
         "people": 2, "hours_day": VISIT_HOURS, "cost_vnd_month": hourly(VISIT_HOURS),
         "how": "2 người × 4 giờ/ngày thăm trạm"},
        {"role": "vận chuyển thuê ngoài", "kind": "tay_chan", "pay": "thuê theo chuyến",
         "people": None, "hours_day": None, "cost_vnd_month": TRANSPORT_VND_MONTH,
         "how": f"{routes} tuyến/ngày, sáng đi chiều về (gom giặt, bưu phẩm); "
                f"{TRANSPORT_RANGE[0] / 1e6:g}–{TRANSPORT_RANGE[1] / 1e6:g} tr/tháng, chờ báo giá đối tác"},
    ]
    # Office work by hand: full-time people role by role.
    office = [
        ("chăm sóc khách hàng", contacts * CONTACT_MIN,
         f"{contacts:g} lượt hỏi ({CONTACTS_PER_100}/100 đơn) × {CONTACT_MIN} phút"),
        ("thu mua, đặt hàng nhà cung cấp", stations * 30, f"{stations} trạm × 30 phút gom nhu cầu"),
        ("điều phối xe", routes * 80, f"{routes} tuyến × (20 phút lập tuyến + 60 phút theo dõi)"),
        ("đăng bán, nội dung", lst * 17, f"{lst} tin × 17 phút (viết 12 + kiểm 5)"),
        ("kế toán, đối soát", orders * 1.5 + stations * 10 + sup * 10,
         f"{orders} đơn × 1,5 phút + {stations} trạm × 10 phút đối chiếu tiền + {sup} NCC × 10 phút báo cáo"),
    ]
    manual = []
    for role, minutes, how in office:
        n = people(minutes)
        manual.append({"role": role, "kind": "van_phong", "pay": "toàn thời gian, có bảo hiểm", "people": n,
                       "minutes_day": round(minutes), "cost_vnd_month": full_time(role, n), "how": how})
    # With agents: the agents do the office work; people approve what involves money or a customer.
    agent_office_min = (contacts * HUMAN_SHARE * APPROVE_MIN + stations * 1 + sup * 5 + routes * 15 + lst * 1
                        + 5 + stations * 1)
    n_office = max(1, people(agent_office_min))
    agent = [{"role": "điều phối, chịu trách nhiệm số liệu", "kind": "van_phong", "pay": "toàn thời gian, có bảo hiểm",
              "people": n_office, "minutes_day": round(agent_office_min),
              "cost_vnd_month": full_time("điều phối, chịu trách nhiệm số liệu", n_office),
              "how": f"duyệt việc có tiền, có khách và xử lý sự cố: {agent_office_min:.0f} phút/ngày; agent làm phần còn lại"}]
    for side, rows in (("manual", manual), ("with_agents", agent)):
        n = MANAGERS[side]
        rows.append({"role": "quản lý", "kind": "quan_ly", "pay": "toàn thời gian, có bảo hiểm", "people": n,
                     "minutes_day": None, "cost_vnd_month": full_time("quản lý", n),
                     "how": "kho, trạm, nhà cung cấp, tiền" if side == "manual"
                            else "quyết định và xử lý ngoại lệ; theo dõi, báo cáo do agent làm"})
    manual = manual[:-1] + same + manual[-1:]
    agent = agent[:-1] + same + agent[-1:]

    def full_time_people(rows: list[dict]) -> int:
        return sum(r["people"] or 0 for r in rows if r["pay"].startswith("toàn thời gian"))

    m_cost = sum(r["cost_vnd_month"] for r in manual)
    a_staff = sum(r["cost_vnd_month"] for r in agent)
    pool = fee_pool(stations)
    out = {
        "stations": stations, "orders_per_day": orders, "operating_days": OPERATING_DAYS,
        "manual": manual, "with_agents": agent,
        "manual_people": full_time_people(manual), "with_agents_people": full_time_people(agent),
        "people_note": "người toàn thời gian; việc theo giờ và xe thuê tính theo giờ công, theo chuyến",
        "manual_cost_vnd_month": m_cost, "with_agents_staff_cost_vnd_month": a_staff,
        "agents": 6, "agents_cost_vnd_month": agent_cost_vnd_month,
        "with_agents_cost_vnd_month": a_staff + agent_cost_vnd_month,
        "saving_vnd_month": m_cost - a_staff - agent_cost_vnd_month,
        "office_people_manual": sum(r["people"] for r in manual if r["kind"] == "van_phong"),
        "office_people_with_agents": sum(r["people"] for r in agent if r["kind"] == "van_phong"),
        "managers": dict(MANAGERS),
        "fee_pool": pool,
        "within_fee_pool": a_staff + agent_cost_vnd_month <= pool["vnd_month"],
        "same_flow_note": "hai cột cùng luồng hàng (chia theo trạm, theo mặt hàng), cùng định mức, cùng mức lương",
    }
    out["people_summary"] = people_summary(out)
    return out


HOURS_PER_PERSON_MONTH = 208     # 26 working days × 8 hours: one person, for counting hourly work as people


def whole(x: float) -> int:
    """People are whole people: round UP (the cautious side, more people rather than fewer)."""
    return math.ceil(round(x, 6))


def about_million(vnd: int, step: int = 1) -> int:
    """Money in headcount.json and the docs, in million đồng rounded to the nearest 1 (445, 178)."""
    return int(step * math.floor(vnd / 1_000_000 / step + 0.5))


def people_summary(t: dict) -> dict:
    """What the comparison screen shows: whole people in three groups and two monthly costs, nothing else.

    Office and managers are whole full-time people; hourly work (splitting, receiving, station visits) is turned into
    people at 208 hours a month and rounded up; the hired van is a cost, not people. Totals are the sums of the groups.
    Money is shown in million đồng rounded to 5; the exact amounts stay in the file."""
    def hourly_people(rows: list[dict]) -> float:
        return sum((r["hours_day"] or 0) * OPERATING_DAYS / HOURS_PER_PERSON_MONTH for r in rows
                   if r["pay"].startswith("theo giờ"))

    groups = []
    for key, label in (("van_phong", "Giấy tờ, điều phối, đối soát"), ("quan_ly", "Quản lý"),
                       ("tay_chan", "Chia hàng, kiểm hàng, giám sát (tại trạm, tại kho)")):
        if key == "tay_chan":
            m, a = hourly_people(t["manual"]), hourly_people(t["with_agents"])
            exact = round(m, 2)
        else:
            m = sum(r["people"] or 0 for r in t["manual"] if r["kind"] == key)
            a = sum(r["people"] or 0 for r in t["with_agents"] if r["kind"] == key)
            exact = None
        groups.append({"key": key, "label": label, "manual": whole(m), "with_agents": whole(a),
                       "same_both_sides": key == "tay_chan", "exact_people": exact})
    manual = sum(g["manual"] for g in groups)
    agent = sum(g["with_agents"] for g in groups)
    m_tr, a_tr = about_million(t["manual_cost_vnd_month"]), about_million(t["with_agents_cost_vnd_month"])
    pool_tr = about_million(t["fee_pool"]["vnd_month"])
    return {
        "groups": groups, "manual_people": manual, "with_agents_people": agent, "agents": t["agents"],
        "manual_cost_vnd_month": t["manual_cost_vnd_month"],
        "with_agents_cost_vnd_month": t["with_agents_cost_vnd_month"],
        "manual_cost_trieu": m_tr, "with_agents_cost_trieu": a_tr, "fee_pool_trieu": pool_tr,
        "fee_pool_vnd_month": t["fee_pool"]["vnd_month"], "within_fee_pool": t["within_fee_pool"],
        "hours_per_person_month": HOURS_PER_PERSON_MONTH,
        "headline": f"Cùng khối lượng việc, từ khoảng {manual} người còn khoảng {agent} người, cộng {t['agents']} "
                    "agent. Người ở lại lo hàng và khách tại trạm, tại kho.",
        "cost_line": f"Chi phí cả bộ máy: khoảng {m_tr} triệu xuống khoảng {a_tr} triệu mỗi tháng, "
                     + (f"dưới mức {pool_tr} triệu (4 % phí)" if t["within_fee_pool"] else f"vượt mức {pool_tr} triệu (4 % phí)"),
        "transport_note": "xe tải thuê ngoài là số tạm, chờ báo giá",
    }


def assumptions() -> list[dict]:
    return [
        {"what": "Luồng hàng", "value": "kho chia theo trạm, theo mặt hàng; trạm giao từng hộ theo đơn trên app",
         "source": "thiết kế Chợ Z (chủ dự án chốt 04/11)"},
        {"what": "Chia hàng ở kho", "value": f"{SPLIT_MIN_PER_LINE:g} phút một dòng (trạm × mặt hàng)".replace(".", ","),
         "source": SOURCES["chia_theo_tram"]},
        {"what": "Trả lương", "value": "điều phối, quản lý, văn phòng: toàn thời gian có bảo hiểm; chia hàng, kiểm hàng, "
                                       "giám sát trạm: theo giờ 30.000 đ; xe: thuê theo chuyến",
         "source": SOURCES["theo_gio"]},
        {"what": "Chi phí trên lương toàn thời gian", "value": f"× {EMPLOYER_FACTOR:.3f}".replace(".", ","),
         "source": SOURCES["bao_hiem"] + "; lương tháng 13"},
        {"what": "Lương toàn thời gian", "value": "; ".join(f"{k} {v / 1e6:g} tr".replace(".", ",") for k, v in WAGES.items()),
         "source": SOURCES["luong_hp"]},
        {"what": "Khách liên hệ", "value": f"{CONTACTS_PER_100}/100 đơn × {CONTACT_MIN} phút, {HUMAN_SHARE:.0%} cần người",
         "source": SOURCES["lien_he_khach"]},
        {"what": "Vận chuyển", "value": "60–70 triệu/tháng, 20 trạm/tuyến", "source": "chờ báo giá đối tác vận chuyển"},
        {"what": "Quản lý", "value": "4 người làm tay, 2 người có agent", "source": "chủ dự án chốt 04/11"},
        {"what": "Ngày vận hành", "value": f"{OPERATING_DAYS} ngày/tháng", "source": SOURCES["phi"]},
        {"what": "Nhà cung cấp, tin đăng ở 50 trạm", "value": "15 nhà cung cấp, 15 tin/ngày", "source": "giả định, chờ Chợ Z"},
        {"what": "Thu mua, đối soát, đăng bán", "value": "30 phút/trạm; 1,5 phút/đơn + 10 phút/trạm; 17 phút/tin",
         "source": "định mức khởi điểm trong yêu cầu demo (mục 6b), chờ Chợ Z"},
    ]
