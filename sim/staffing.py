"""Staffing of the central team (warehouse + office) of a Trạm Z network, by hand vs with agents.

Every rate below carries its source. Rates found in public data are marked SOURCED (job postings in Hải Phòng 2026,
labour law, industry benchmarks); rates that only Chợ Z can know are marked ASSUMED and listed on the final screen as
assumptions. Change a number here and both columns follow; nothing is typed into the stage.

People are whole people: minutes of work per day ÷ productive minutes of one shift × coverage, rounded UP per role.
The station person (one per shop, paid by the 5 % station share) is not in this team; their minutes are shown apart.
"""
from __future__ import annotations

import math

SOURCES = {
    "luong_toi_thieu": "Nghị định 293/2025/NĐ-CP: vùng I (Hải Phòng nội thành, Cát Hải) 5.310.000 đ/tháng từ 01/01/2026",
    "bao_hiem": "BHXH 17,5 % + BHYT 3 % + BHTN 1 % (Luật 41/2024/QH15) + kinh phí công đoàn 2 % = 23,5 % trên lương",
    "luong_hp": "tin tuyển dụng Hải Phòng 2026 trên VietnamWorks, Joboko, TopCV, 123job, CareerViet, tuyendung.winmart.vn",
    "soan_hang": "soạn hàng thủ công ngành tạp hóa 50–60 món/giờ (Wynshop ROI study), 100–200 món/giờ ở nhiều nhà bán lẻ "
                 "(StrongPoint whitepaper 2023)",
    "lien_he_khach": "liên hệ chăm sóc khách: 3,2 / 100 đơn trung vị bán lẻ (Gartner 2024, qua Stealth Agents) đến 20 / 100 "
                     "đơn ngành thực phẩm (Gorgias Ecom Lab 2026); thời gian xử lý 3–5 phút, khiếu nại 8–15 phút (HiveDesk)",
    "luoi_xe": "mua chung Trung Quốc: một xe chở 500–1.500 món cho 15–30 điểm nhận (báo cáo ngành, dfcfw 2021); "
               "trưởng điểm là chủ tiệm có sẵn, hoa hồng 10–12 % (HKUST 2025)",
    "quan_ly": "tầm quản lý 1 trưởng nhóm cho khoảng 10 người (thông lệ vận hành; giả định)",
}

# ── time ───────────────────────────────────────────────────────────────────────────────────────────────
SHIFT_MIN = 480
PRODUCTIVE = 0.85            # breaks, briefings, handovers. ASSUMED (common planning figure 80–85 %)
COVERAGE = 30 / 26 * 1.04    # open 7 days a week while a person works 26 days a month; 12 days of leave a year
DAYS_PER_MONTH = 26          # paid working days per person (brief, section 6b)

# ── pay (VND per month, Hải Phòng 2026, middle of posted ranges) ──────────────────────────────────────
EMPLOYER_FACTOR = 1 + 0.235 + 1 / 12      # insurance and union fee 23.5 %, 13th-month salary
WAGES = {
    "nhân viên soạn hàng, đóng túi": 10_000_000,     # SOURCED: công nhân kho Hải Phòng 8–13 tr, 10–12 tr
    "kiểm hàng nhập kho (QC)": 10_000_000,           # SOURCED: QC/phụ kho 10–15 tr
    "tài xế": 12_500_000,                            # SOURCED: tài xế C1/C Hải Phòng 10–14 tr (PDC 12,5–13 tr)
    "giám sát trạm": 12_000_000,                     # ASSUMED: tương đương điều phối, đi lại nhiều
    "chăm sóc khách hàng": 9_000_000,                # SOURCED: CSKH online Hải Phòng 7–12 tr, 5,5–10 tr
    "thu mua, đặt hàng nhà cung cấp": 11_000_000,    # ASSUMED: giữa kế toán và điều phối
    "điều phối xe": 13_000_000,                      # SOURCED: điều phối giao vận WinCommerce Hải Phòng 12–14 tr
    "đăng bán, nội dung": 9_000_000,                 # ASSUMED: như CSKH online
    "kế toán, đối soát": 11_000_000,                 # SOURCED: kế toán Hải Phòng 9–15 tr, công nợ 8–13 tr
    "trưởng nhóm, quản lý kho": 16_000_000,          # SOURCED: quản lý kho Hải Phòng 10–18 tr, 14 tr+
    "điều phối, chịu trách nhiệm số liệu": 13_000_000,
}

# ── volumes of a network of S stations (30 orders each) ───────────────────────────────────────────────
STATIONS_PER_ROUTE = 20      # SOURCED range 15–30 stations per vehicle
ROUTE_DRIVE_MIN = 240        # ASSUMED: outbound round + return round with laundry and parcels, Hải Phòng city
CONTACTS_PER_100 = 10        # SOURCED range 3,2–20 per 100 orders; middle of the range
CONTACT_MIN = 5              # SOURCED 3–5 min blended, complaints 8–15
HUMAN_SHARE = 0.35           # SOURCED share of returns, refunds, complaints in e-commerce contacts (~30–45 %)
APPROVE_MIN = 2              # ASSUMED: read the agent's proposal and approve


def suppliers_at(stations: int) -> int:
    return 5 if stations == 1 else 15        # ASSUMED: three times as many suppliers at 50 stations


def listings_at(stations: int) -> int:
    return 3 if stations == 1 else 15        # ASSUMED: 15 new or changed listings a day at 50 stations


# ── the work, per day (minutes) ───────────────────────────────────────────────────────────────────────
def work(stations: int, items_per_order: float) -> list[dict]:
    """Rows: role, kind (office: agents do it, a person approves; physical: same on both sides), manual and with-agent
    minutes per day, and how each was computed."""
    orders = 30 * stations
    routes = math.ceil(stations / STATIONS_PER_ROUTE)
    sup, lst = suppliers_at(stations), listings_at(stations)
    contacts = orders * CONTACTS_PER_100 / 100
    pick_min = items_per_order * 60 / 100        # 100 items an hour, the middle of 50–200
    return [
        {"role": "nhân viên soạn hàng, đóng túi", "kind": "tay_chan", "manual": orders * pick_min,
         "agent": orders * pick_min, "how": f"{orders} đơn × {items_per_order:.1f} món ÷ 100 món/giờ".replace(".", ",")},
        {"role": "kiểm hàng nhập kho (QC)", "kind": "tay_chan", "manual": sup * 15, "agent": sup * 15,
         "how": f"{sup} chuyến hàng nhà cung cấp × 15 phút (cân, đo nhiệt độ, xem hạn)"},
        {"role": "tài xế", "kind": "tay_chan", "manual": routes * ROUTE_DRIVE_MIN, "agent": routes * ROUTE_DRIVE_MIN,
         "how": f"{routes} tuyến × {ROUTE_DRIVE_MIN} phút (đi giao, về gom giặt và bưu phẩm)", "per_route": routes},
        {"role": "giám sát trạm", "kind": "tay_chan", "manual": stations * 10, "agent": stations * 10,
         "how": f"{stations} trạm × 1 lần thăm/tuần × 60 phút"},
        {"role": "chăm sóc khách hàng", "kind": "van_phong", "manual": contacts * CONTACT_MIN,
         "agent": contacts * HUMAN_SHARE * APPROVE_MIN,
         "how": f"{contacts:g} lượt hỏi ({CONTACTS_PER_100}/100 đơn) × {CONTACT_MIN} phút; có agent: "
                f"{HUMAN_SHARE:.0%} cần người duyệt × {APPROVE_MIN} phút"},
        {"role": "thu mua, đặt hàng nhà cung cấp", "kind": "van_phong", "manual": stations * 30,
         "agent": stations * 1 + sup * 5,
         "how": f"{stations} trạm × 30 phút gom nhu cầu; có agent: duyệt thay hàng 1 phút/trạm + {sup} nhà cung cấp × 5 phút"},
        {"role": "điều phối xe", "kind": "van_phong", "manual": routes * 80, "agent": routes * 15,
         "how": f"{routes} tuyến × (20 phút lập tuyến + 60 phút theo dõi); có agent: 15 phút xử lý sự cố/tuyến"},
        {"role": "đăng bán, nội dung", "kind": "van_phong", "manual": lst * 17, "agent": lst * 1,
         "how": f"{lst} tin × 17 phút (viết 12 + kiểm 5); có agent: 1 phút duyệt/tin"},
        {"role": "kế toán, đối soát", "kind": "van_phong",
         "manual": orders * 1.5 + stations * 10 + sup * 10, "agent": 5 + stations * 1,
         "how": f"{orders} đơn × 1,5 phút + {stations} trạm × 10 phút đối chiếu tiền + {sup} NCC × 10 phút báo cáo; "
                "có agent: 5 phút duyệt + 1 phút/trạm xem chênh lệch"},
    ]


def people(minutes: float) -> int:
    return math.ceil(round(minutes / (SHIFT_MIN * PRODUCTIVE) * COVERAGE, 4)) if minutes > 0 else 0


def cost(role: str, n: int) -> int:
    return int(round(n * WAGES[role] * EMPLOYER_FACTOR, -3))


def team(stations: int, items_per_order: float, agent_cost_vnd_month: int) -> dict:
    rows = work(stations, items_per_order)
    manual, agent = [], []
    for r in rows:
        n_m = math.ceil(r["per_route"] * COVERAGE) if "per_route" in r else people(r["manual"])
        manual.append({"role": r["role"], "kind": r["kind"], "people": n_m, "minutes_day": round(r["manual"]),
                       "how": r["how"], "cost_vnd_month": cost(r["role"], n_m)})
        if r["kind"] == "tay_chan":
            agent.append({**manual[-1], "minutes_day": round(r["agent"])})
    office_min = sum(r["agent"] for r in rows if r["kind"] == "van_phong")
    n_office = max(1, people(office_min))
    agent.insert(0, {"role": "điều phối, chịu trách nhiệm số liệu", "kind": "van_phong", "people": n_office,
                     "minutes_day": round(office_min),
                     "how": "duyệt việc có tiền, có khách; xử lý sự cố (agent làm phần còn lại)",
                     "cost_vnd_month": cost("điều phối, chịu trách nhiệm số liệu", n_office)})
    for side in (manual, agent):
        staff = sum(x["people"] for x in side)
        leads = math.ceil(staff / 10)
        side.append({"role": "trưởng nhóm, quản lý kho", "kind": "quan_ly", "people": leads, "minutes_day": None,
                     "how": "1 trưởng nhóm cho khoảng 10 người", "cost_vnd_month": cost("trưởng nhóm, quản lý kho", leads)})
    m_people, a_people = sum(x["people"] for x in manual), sum(x["people"] for x in agent)
    m_cost, a_cost = sum(x["cost_vnd_month"] for x in manual), sum(x["cost_vnd_month"] for x in agent)
    return {
        "stations": stations, "orders_per_day": 30 * stations, "items_per_order": round(items_per_order, 2),
        "manual": manual, "with_agents": agent,
        "manual_people": m_people, "with_agents_people": a_people,
        "manual_cost_vnd_month": int(m_cost), "with_agents_staff_cost_vnd_month": int(a_cost),
        "agents": 6, "agents_cost_vnd_month": agent_cost_vnd_month,
        "with_agents_cost_vnd_month": int(a_cost + agent_cost_vnd_month),
        "saving_vnd_month": int(m_cost - a_cost - agent_cost_vnd_month),
        "office_people_manual": sum(x["people"] for x in manual if x["kind"] == "van_phong"),
        "office_people_with_agents": sum(x["people"] for x in agent if x["kind"] == "van_phong"),
        "physical_people": sum(x["people"] for x in manual if x["kind"] == "tay_chan"),
    }


def assumptions() -> list[dict]:
    return [
        {"what": "Một ca", "value": f"{SHIFT_MIN} phút, làm hiệu quả {PRODUCTIVE:.0%}", "source": "ASSUMED"},
        {"what": "Hệ số phủ ca", "value": f"{COVERAGE:.2f} (mở 7 ngày/tuần, 26 công/tháng, 12 ngày phép)",
         "source": "Bộ luật Lao động 2019 (phép năm 12 ngày)"},
        {"what": "Chi phí doanh nghiệp trên lương", "value": f"× {EMPLOYER_FACTOR:.3f}", "source": SOURCES["bao_hiem"]
         + "; lương tháng 13"},
        {"what": "Lương theo vai", "value": "; ".join(f"{k} {v / 1e6:g} tr" for k, v in WAGES.items()),
         "source": SOURCES["luong_hp"]},
        {"what": "Soạn hàng", "value": "100 món/giờ", "source": SOURCES["soan_hang"]},
        {"what": "Khách liên hệ", "value": f"{CONTACTS_PER_100}/100 đơn × {CONTACT_MIN} phút, {HUMAN_SHARE:.0%} cần người",
         "source": SOURCES["lien_he_khach"]},
        {"what": "Xe", "value": f"{STATIONS_PER_ROUTE} trạm/tuyến, {ROUTE_DRIVE_MIN} phút/tuyến", "source": SOURCES["luoi_xe"]},
        {"what": "Nhà cung cấp, tin đăng ở 50 trạm", "value": "15 nhà cung cấp, 15 tin/ngày", "source": "ASSUMED, chờ Chợ Z"},
        {"what": "Đối chiếu tiền trạm", "value": "10 phút/trạm/ngày", "source": "ASSUMED"},
        {"what": "Thu mua, đối soát đơn, đăng bán", "value": "30 phút/trạm; 1,5 phút/đơn; 17 phút/tin",
         "source": "định mức khởi điểm trong yêu cầu demo (mục 6b), chờ Chợ Z"},
        {"what": "Quản lý", "value": "1 trưởng nhóm / 10 người", "source": SOURCES["quan_ly"]},
    ]
