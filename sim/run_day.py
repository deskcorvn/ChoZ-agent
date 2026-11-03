"""Run one day of Trạm Z and write runs/<name>/: events.jsonl, summary.json, headcount.json, log.jsonl, world.json.

    python -m sim.run_day --mode mock --seed 7 --out runs/mock
    python -m sim.run_day --mode live --seed 7 --out runs/live_<ts>

Seven scenes from 20:00 the evening before to 22:00 (section 5 of the brief). The order of work is fixed here,
like a day's routine; what the agents read, decide and say is theirs (live) or a fixed template (mock).
"""
from __future__ import annotations

import argparse
import asyncio
import json
import re
import time
from datetime import datetime
from pathlib import Path

from app import config as app_config
from sim import humans, summary, workload
from sim.agents.base import BudgetStop, Day, Reply, env_settings
from sim.agents.dang_ban import Lister, emit_mock, outcome_of, short
from sim.schema import SUPPLIERS, VN

ROOT = Path(__file__).resolve().parents[1]
SAMPLES = {
    "chao_hau": ROOT / "samples" / "products" / "05-chao-hau-hop",
    "banh_da_cua": ROOT / "samples" / "products" / "04-banh-da-cua-hop",
    "hang_cam": ROOT / "sim" / "samples" / "hang-cam",
}
VOICE = ROOT / "sim" / "samples" / "voice" / "ncc1-chao-hau.wav"


def money(v: float) -> str:
    return f"{v:,.0f} đ".replace(",", ".")


def millions(v: float) -> str:
    return f"{v / 1e6:.1f}".replace(".", ",") + " triệu"


def digits(text: str) -> str:
    return re.sub(r"\D", "", text)


# ── scene 1 ──────────────────────────────────────────────────────────────────────────────────────────
async def dat_gio(d: Day) -> None:
    d.at("20:00", -1)
    d.emit("scene", step=0, scene="dat_gio")
    orders = sorted(d.world.orders, key=lambda o: o.household)
    chunks = [orders[i::9] for i in range(9)]
    for chunk in sorted(chunks, key=lambda c: c[0].household):
        tiers: dict[int, int] = {}
        for o in chunk:
            tiers[o.basket_vnd] = tiers.get(o.basket_vnd, 0) + 1
        parts = ", ".join(f"{n} Giỏ {p // 1000}k" for p, n in sorted(tiers.items()))
        d.emit("say", step=8 * 60, from_=chunk[0].household, to="ag_chia_don",
               text=f"{len(chunk)} hộ đặt Giỏ cho sáng mai: {parts}",
               detail={"households": [o.household for o in chunk], "orders": [o.id for o in chunk]})
    d.at("21:15", -1)
    a = await d.ask(
        "ag_chia_don",
        "21:15 tối 03/11. Gom các Giỏ khách đã đặt cho sáng 04/11: gọi list_baskets, stock cho ncc2 (rau) và "
        "make_picklist. Nếu có hàng thiếu, `text` là câu xin người trực trạm (hu_tram) duyệt phương án thay hàng "
        "mà make_picklist gợi ý, nêu số phần thiếu, hàng thay và tiền hoàn mỗi đơn. Chưa được gọi apply_substitution.",
        tools=["list_baskets", "stock", "make_picklist"],
        mock_calls=[("list_baskets", {}), ("stock", {"supplier": "ncc2"}), ("make_picklist", {})],
        mock_text=lambda o: (lambda s: f"Thiếu {s['missing']} {s['name'].lower()}. Thay {s['suggest']['name'].lower()}, "
                                       f"hoàn {money(s['suggest']['refund_vnd_each'])} mỗi đơn?")(
            o["make_picklist"][-1]["short"][0]),
        required=("list_baskets", "make_picklist"))
    short_items = d.tools.make_picklist()["short"]
    if not short_items:
        d.emit("say", from_="ag_chia_don", to="ag_giao_nhan", text=a.text, usage=a.usage)
    else:
        s = short_items[0]
        humans.approve(d, "ag_chia_don", "hu_tram", "thay_hang", a.text, "Duyệt thay hàng, nhớ báo từng hộ",
                       usage=a.usage, detail={"short": s})
        sub = {"sku": s["sku"], "alt": s["suggest"]["sku"], "count": s["missing"]}
        b = await d.ask(
            "ag_chia_don",
            f"Người trực trạm đã duyệt. Gọi apply_substitution với sku={sub['sku']}, alt={sub['alt']}, "
            f"count={sub['count']}. `messages`: một câu cho mỗi hộ bị đổi (to = mã hộ), nêu mã đơn, hàng thay và tiền "
            "hoàn. `text`: câu bàn giao phiếu gom cho ag_giao_nhan, có số đơn và số mặt hàng.",
            tools=["apply_substitution"], mock_calls=[("apply_substitution", sub)],
            mock_text=lambda o: f"Phiếu gom {len(d.world.orders)} đơn, {d.tools.make_picklist()['items']} mặt hàng, "
                                f"đã thay {sub['count']} phần. Mời lên lịch xe.",
            mock_messages=lambda o: [{"to": c["household"], "text": f"Đơn {c['order']}: hết {s['name'].lower()}, thay "
                                      f"{s['suggest']['name'].lower()}, hoàn {money(s['suggest']['refund_vnd_each'])}."}
                                     for c in o["apply_substitution"][-1]["orders"]],
            required=("apply_substitution",), listeners=("ag_giao_nhan",),
            check=lambda r, o: _households_match(r, o))
        for m in b.messages:
            d.emit("say", from_="ag_chia_don", to=m["to"], text=m["text"])
        d.emit("handoff", from_="ag_chia_don", to="ag_giao_nhan", text=b.text, usage=b.usage,
               detail={"picklist": d.tools.make_picklist()})
    d.metric()


def _households_match(r: Reply, o: dict) -> str:
    want = {c["household"] for c in o.get("apply_substitution", [{"orders": []}])[-1]["orders"]}
    got = {m.get("to") for m in r.messages}
    return "" if want == got else f"messages phải gửi đúng các hộ {sorted(want)}"


# ── scene 2 ──────────────────────────────────────────────────────────────────────────────────────────
async def dang_ban(d: Day) -> None:
    d.at("05:00")
    d.emit("scene", step=0, scene="dang_ban")
    lister = Lister(d) if d.mode == "live" else None
    items = [  # (sample, supplier, what the supplier says, detail)
        ("chao_hau", "ncc1", "Cháo hàu ăn liền, hộp 150 gam, 22 nghìn, còn 80 hộp",
         {"photos": 1, "audio_seconds": round(VOICE.stat().st_size / 32000, 1) if VOICE.is_file() else 0,
          "sample": "samples/products/05-chao-hau-hop", "voice": "sim/samples/voice/ncc1-chao-hau.wav"}),
        ("banh_da_cua", "ncc1", "Bánh đa cua hộp, 22 nghìn một hộp, còn 80 hộp",
         {"photos": 1, "sample": "samples/products/04-banh-da-cua-hop"}),
        ("hang_cam", "ncc3", "Rượu ngâm mật gấu, chai 500 ml, 350 nghìn, còn 10 chai",
         {"photos": 1, "sample": "sim/samples/hang-cam"}),
    ]
    try:
        for key, supplier, words, detail in items:
            d.emit("say", from_=supplier, to="ag_dang_ban", text=words, detail=detail)
            if lister is None:
                outcome = emit_mock(d, key, "Kiểm giúp tin vừa đăng với 1 ảnh")
            else:
                run = await lister.list(SAMPLES[key], VOICE if key == "chao_hau" else None)
                checks = len((run.verification or {}).get("checks", []))
                lister.emit_steps(run, 1, f"Kiểm giúp {checks} claim với 1 ảnh" if checks else "Kiểm giúp tin này, 1 ảnh")
                outcome = outcome_of(run)
            _after_listing(d, outcome, supplier)
    finally:
        if lister is not None:
            await lister.close()
    d.metric()


def _after_listing(d: Day, o: dict, supplier: str) -> None:
    title = short(o["title"])[:40]
    if o["status"] == "blocked":
        rules = ", ".join(o.get("rules") or []) or "hàng cấm"
        d.emit("say", from_="ag_kiem_chung", to=supplier, text=short(f"Chặn tin: {title} là hàng cấm ({rules}). Không đăng."),
               detail={"blocked": True, "listing_run": o["run"], "reason": o.get("reason"), "rules": o.get("rules")})
        d.add(blocked=1)
        return
    if o["verdict"] == "warn" or o.get("check"):
        said = short(o.get("summary_vi") or (o.get("check") or {}).get("note") or "Lời nói và ảnh có điểm lệch")
        d.emit("say", from_="ag_kiem_chung", to=supplier, text=said, detail={"check": o.get("check"), "verdict": o["verdict"]})
        humans.approve(d, "ag_kiem_chung", "hu_tram", "duyet_listing",
                       short(f"Tin {title}: {o['checks']} claim, có lệch. Duyệt đăng theo nhãn?"),
                       "Duyệt, đăng theo nhãn", detail={"listing_run": o["run"], "check": o.get("check")})
        return
    if o["status"] in ("ready", "needs_answer"):
        d.emit("say", from_="ag_dang_ban", to=supplier,
               text=short(f"Đã lên tin {title}, {money(o['price_vnd'])}, tồn {o['stock']}, {o['seconds']:g} giây"),
               detail={"listing_run": o["run"], "status": o["status"]})
        return
    d.emit("error", from_="ag_dang_ban", text=short(f"Tin {title} lỗi: {o.get('error')}"), detail={"listing_run": o["run"]})
    d.errors += 1


# ── scene 3 ──────────────────────────────────────────────────────────────────────────────────────────
LOTS = [("ncc2", "rau"), ("ncc3", "thit"), ("ncc4", "tom"), ("ncc4", "ca"), ("ncc5", "trung"), ("ncc1", "chao_hau")]


async def gom_kho(d: Day) -> None:
    d.at("05:30")
    d.emit("scene", step=0, scene="gom_kho")
    a = await d.ask(
        "ag_giao_nhan",
        "05:30 sáng 04/11. Báo chủ kho (hu_kho) danh sách gom hôm nay: gọi make_picklist. `text`: số đơn, số mặt "
        "hàng và 2-3 mặt hàng nhiều nhất kèm số lượng.",
        tools=["make_picklist"], mock_calls=[("make_picklist", {})],
        mock_text=lambda o: (lambda p: f"Kho gom {p['orders']} đơn, {p['items']} mặt hàng: " + ", ".join(
            f"{x['qty']} {x['name'].lower()}" for x in sorted(
                [i for v in p["by_supplier"].values() for i in v], key=lambda x: -x["qty"])[:3]))(o["make_picklist"][-1]),
        required=("make_picklist",))
    d.emit("say", from_="ag_giao_nhan", to="hu_kho", text=a.text, usage=a.usage)
    for code in SUPPLIERS:
        d.emit("move", step=2 * 60, from_=code, loc="loc_duong")
        d.emit("move", step=2 * 60, from_=code, loc="loc_kho")
    lots = ", ".join(f"({s}, {i})" for s, i in LOTS)
    k = await d.ask(
        "ag_kiem_chung",
        f"06:00. Sáu lô vừa về kho: {lots}. Gọi check_lot cho từng lô. `text`: tóm tắt số lô đạt và số lô loại. "
        "`messages`: với mỗi lô loại, một câu gửi nhà cung cấp của lô đó (to = mã ncc) yêu cầu đổi lô, nêu lý do có số.",
        tools=["check_lot"], mock_calls=[("check_lot", {"supplier": s, "item": i}) for s, i in LOTS],
        mock_text=lambda o: (lambda bad: f"{len(LOTS) - len(bad)}/{len(LOTS)} lô đạt; loại {len(bad)} lô "
                                         + ", ".join(f"{b['item']} {b['supplier']}" for b in bad))(
            [x for x in o["check_lot"] if x["result"] == "loại"]),
        mock_messages=lambda o: [{"to": x["supplier"], "text": f"Lô {x['item']} {x['reasons'][0]}. Đổi lô khác trước 06:20."}
                                 for x in o["check_lot"] if x["result"] == "loại"],
        required=("check_lot",), check=_all_lots_checked)
    d.emit("say", from_="ag_kiem_chung", to="hu_kho", text=k.text, usage=k.usage)
    for m in k.messages:
        d.emit("say", from_="ag_kiem_chung", to=m["to"], text=m["text"])
    failed = [x for x in k.outputs.get("check_lot", []) if x["result"] == "loại"]
    for x in {(f["supplier"], f["item"]) for f in failed}:
        d.tools.replace_lot(x[0], x[1], 2.0)
        d.emit("say", step=10 * 60, from_=x[0], to="ag_kiem_chung", text=f"Đã đổi lô {x[1]} mới, đo 2 °C",
               detail={"lot": x[1], "temp_c": 2.0})
    if failed:
        again = sorted({(f["supplier"], f["item"]) for f in failed})
        r = await d.ask(
            "ag_kiem_chung",
            "Nhà cung cấp đã đổi lô: " + ", ".join(f"({s}, {i})" for s, i in again) + ". Gọi check_lot lại cho lô "
            "đó. `text`: câu xin chủ kho nhận hàng vào kho, có số lô đạt.",
            tools=["check_lot"], mock_calls=[("check_lot", {"supplier": s, "item": i}) for s, i in again],
            mock_text=lambda o: f"Lô đổi đạt. Đủ {len(LOTS)}/{len(LOTS)} lô, mời chủ kho nhận vào kho.",
            required=("check_lot",))
        request, usage = r.text, r.usage
    else:
        request, usage = f"Đủ {len(LOTS)}/{len(LOTS)} lô đạt, mời chủ kho nhận vào kho.", None
    humans.approve(d, "ag_kiem_chung", "hu_kho", "nhan_hang_kho", request, f"Nhận {len(LOTS)} lô vào kho",
                   usage=usage, detail={"lots": [f"{s}:{i}" for s, i in LOTS]})
    for code in SUPPLIERS:
        d.emit("move", step=60, from_=code, loc="loc_ncc")
    d.metric()


def _all_lots_checked(r: Reply, o: dict) -> str:
    seen = {(x["supplier"], x["item"]) for x in o.get("check_lot", [])}
    missing = [f"{s}/{i}" for s, i in LOTS if (s, i) not in seen]
    return f"chưa kiểm lô {', '.join(missing)}" if missing else ""


# ── scene 4 ──────────────────────────────────────────────────────────────────────────────────────────
ROUTE = ["loc_kho", "loc_duong", "loc_tram", "loc_duong", "loc_kho"]


async def ra_tram(d: Day) -> None:
    d.at("06:30")
    d.emit("scene", step=0, scene="ra_tram")
    bags = len(d.world.orders)
    g = await d.ask(
        "ag_giao_nhan",
        f"06:30. Lên lịch xe1: gọi plan_route với stops {ROUTE} và laundry_pickups. `text`: báo người trực trạm "
        "(hu_tram) giờ xe tới trạm và số túi. `messages`: đúng một câu cho xe1 (to = xe1) dặn chuyến về ghé trạm gom "
        "đồ giặt, nêu số hộ.",
        tools=["plan_route", "laundry_pickups"],
        mock_calls=[("plan_route", {"stops": ROUTE}), ("laundry_pickups", {})],
        mock_text=lambda o: f"Xe1 tới trạm {o['plan_route'][-1]['legs'][1]['arrive']}, {bags} túi cho {bags} đơn",
        mock_messages=lambda o: [{"to": "xe1", "text": f"Chuyến về ghé trạm gom đồ giặt {o['laundry_pickups'][-1]['count']} "
                                                       "hộ, mang về kho."}],
        required=("plan_route", "laundry_pickups"),
        check=lambda r, o: "" if [m.get("to") for m in r.messages] == ["xe1"] else "messages phải có đúng 1 câu cho xe1")
    d.emit("say", from_="ag_giao_nhan", to="hu_tram", text=g.text, usage=g.usage)
    for m in g.messages:
        d.emit("say", from_="ag_giao_nhan", to=m["to"], text=m["text"])
    d.emit("move", step=5 * 60, from_="xe1", loc="loc_duong")
    d.emit("move", step=20 * 60, from_="xe1", loc="loc_tram", detail={"bags": bags})
    humans.approve(d, "ag_giao_nhan", "hu_tram", "nhan_hang_tram", f"Xe1 đã tới trạm: {bags} túi cho {bags} đơn. Nhận?",
                   f"Nhận đủ {bags} túi", detail={"bags": bags})
    laundry = d.world.laundry
    d.emit("move", step=10 * 60, from_="xe1", loc="loc_duong",
           detail={"laundry_bags": len(laundry), "households": laundry})
    d.emit("move", step=20 * 60, from_="xe1", loc="loc_kho", detail={"laundry_bags": len(laundry)})
    d.metric()


# ── scene 5 ──────────────────────────────────────────────────────────────────────────────────────────
async def nhan_hang(d: Day) -> None:
    d.at("16:30")
    d.emit("scene", step=0, scene="nhan_hang")
    for i in range(1, 6):
        house = f"loc_nha{i}"
        orders = sorted((o for o in d.world.orders if o.house == house), key=lambda o: o.household)
        if not orders:
            continue
        rep = orders[0].household
        d.emit("move", step=15 * 60, from_=rep, loc="loc_tram")
        paid = sum(o.total_vnd for o in orders)
        d.emit("say", from_=rep, to="hu_tram", text=f"Nhà {i}: {len(orders)} hộ nhận Giỏ, trả {money(paid)}",
               detail={"payment": True, "orders": [o.id for o in orders], "households": [o.household for o in orders],
                       "paid_vnd": paid, "invoice": True})
        d.tools.paid.update(o.id for o in orders)
        d.add(orders=len(orders), gmv_vnd=paid)
        humans.handover(d, len(orders))
        if d.world.broken_eggs in {o.household for o in orders}:
            await _broken_eggs(d)
        d.emit("move", from_=rep, loc=house)
    d.metric()


async def _broken_eggs(d: Day) -> None:
    ho = d.world.broken_eggs
    d.emit("say", from_=ho, to="ag_cham_khach", text="Hộp trứng của tôi vỡ 3 quả, đổi được không?",
           detail={"photo": False})
    c = await d.ask(
        "ag_cham_khach",
        f"17:00. Hộ {ho} báo: hộp trứng gà vỡ 3 quả khi nhận ở trạm. Trạm còn 2 hộp trứng dự phòng. Gọi order_of "
        "và return_policy. `text`: câu xin người trực trạm (hu_tram) duyệt cách xử lý, có mã đơn và số tiền dòng trứng.",
        tools=["order_of", "return_policy"],
        mock_calls=[("order_of", {"household": ho}), ("return_policy", {})],
        mock_text=lambda o: (lambda od: f"{od['order']} của {ho}: trứng {money(30_000)} vỡ 3 quả. Đổi hộp mới?")(
            o["order_of"][-1]),
        required=("order_of",))
    humans.approve(d, "ag_cham_khach", "hu_tram", "doi_tra", c.text, "Duyệt theo đề xuất", usage=c.usage,
                   detail={"household": ho, "order": c.out("order_of").get("order") if "order_of" in c.outputs else None})
    r = await d.ask(
        "ag_cham_khach",
        f"Người trực trạm đã duyệt đề xuất. `text`: trả lời hộ {ho}, nói rõ cách xử lý và nhận ở đâu, có số.",
        tools=[], mock_calls=[], mock_text=lambda o: "Trạm đổi hộp trứng 10 quả mới cho chị, nhận ngay tại quầy.")
    d.emit("say", from_="ag_cham_khach", to=ho, text=r.text, usage=r.usage)


# ── scene 6 ──────────────────────────────────────────────────────────────────────────────────────────
async def doi_soat(d: Day) -> None:
    d.at("21:00")
    d.emit("scene", step=0, scene="doi_soat")
    o = await d.ask(
        "ag_doi_soat",
        "21:00. Đối soát ngày 04/11: gọi completed_orders và settlement. `text`: câu xin chủ kho (hu_kho) duyệt trước "
        "khi gửi số, có số đơn, tiền hàng và tổng phí. `messages`: đúng 5 câu, mỗi câu cho một nhà cung cấp ncc1..ncc5 "
        "(to = mã ncc), có tiền hàng đã bán, phí 10 % và số nhà cung cấp nhận, viết tiền dạng 1.234.000 đ.",
        tools=["completed_orders", "settlement", "fee_split"],
        mock_calls=[("completed_orders", {}), ("settlement", {})],
        mock_text=lambda x: (lambda c, s: f"{c['orders']} đơn, {money(c['gmv_vnd'])}, phí {money(s['totals']['fee_vnd'])}. "
                                          "Duyệt gửi số cho 5 NCC?")(x["completed_orders"][-1], x["settlement"][-1]),
        mock_messages=lambda x: [{"to": k, "text": f"Bán {money(v['amount_vnd'])}, phí {money(v['fee_vnd'])}, "
                                                   f"anh/chị nhận {money(v['supplier_receives_vnd'])}"}
                                 for k, v in x["settlement"][-1]["per_supplier"].items()],
        required=("completed_orders", "settlement"), check=_settlement_numbers)
    sett = d.tools.settlement()
    humans.approve(d, "ag_doi_soat", "hu_kho", "doi_soat", o.text, "Duyệt, gửi số cho 5 nhà cung cấp", usage=o.usage,
                   detail={"totals": sett["totals"]})
    for m in o.messages:
        d.emit("say", from_="ag_doi_soat", to=m["to"], text=m["text"],
               detail={"settlement": sett["per_supplier"].get(m["to"])})
    d.metric()


def _settlement_numbers(r: Reply, o: dict) -> str:
    sett = o.get("settlement", [None])[-1]
    if not sett:
        return "chưa gọi settlement"
    per = sett["per_supplier"]
    got = {m.get("to"): m.get("text", "") for m in r.messages}
    if set(got) != set(per):
        return f"messages phải có đúng một câu cho mỗi {sorted(per)}"
    wrong = [k for k, v in per.items() if str(v["supplier_receives_vnd"]) not in digits(got[k])]
    return f"số nhà cung cấp nhận sai ở {', '.join(wrong)}" if wrong else ""


# ── scene 7 ──────────────────────────────────────────────────────────────────────────────────────────
async def tong_ket(d: Day) -> None:
    d.at("22:00")
    d.emit("scene", step=0, scene="tong_ket")
    m = d.metric()["metrics"]
    numbers = (f"{m['orders']:g} đơn, {millions(m['gmv_vnd'])} tiền hàng, agent làm {m['agent_actions']:g} việc hết "
               f"{money(m['agent_cost_vnd'])}, người duyệt {m['human_approvals']:g} lần")
    t = await d.ask(
        "ag_doi_soat",
        f"22:00. Tổng kết ngày cho người trực trạm (hu_tram). Số liệu cả ngày (đã tính sẵn, giữ nguyên): {numbers}. "
        "`text` theo mẫu 'Hôm nay N đơn, X tiền hàng, agent làm A việc hết C, anh duyệt M lần'. Không gọi công cụ.",
        tools=[], mock_calls=[],
        mock_text=lambda o: f"Hôm nay {m['orders']:g} đơn, {millions(m['gmv_vnd'])} tiền hàng, agent làm "
                            f"{m['agent_actions']:g} việc hết {money(m['agent_cost_vnd'])}, anh duyệt {m['human_approvals']:g} lần")
    d.emit("say", from_="ag_doi_soat", to="hu_tram", text=t.text, usage=t.usage, detail={"metrics": m})
    d.metric()


SCENES = [("dat_gio", "ag_chia_don", dat_gio), ("dang_ban", "ag_dang_ban", dang_ban), ("gom_kho", "ag_giao_nhan", gom_kho),
          ("ra_tram", "ag_giao_nhan", ra_tram), ("nhan_hang", "ag_cham_khach", nhan_hang),
          ("doi_soat", "ag_doi_soat", doi_soat), ("tong_ket", "ag_doi_soat", tong_ket)]


async def run(mode: str, seed: int, out: Path, only: list[str] | None = None, on_event=None) -> dict:
    settings = env_settings()
    app_config.load()                  # reads .env.local (MODEL_API_KEY, ANTHROPIC_API_KEY if present)
    d = Day(out, mode, seed, settings["usd_vnd"], settings["max_run_usd"], settings["claude_model"])
    if on_event is not None:
        d.em.listeners.append(on_event)
    (out / "world.json").write_text(json.dumps(d.world.to_json(), ensure_ascii=False, indent=1), encoding="utf-8")
    started, t0 = datetime.now(VN).isoformat(timespec="seconds"), time.perf_counter()
    stopped = ""
    try:
        for name, owner, scene in SCENES:
            if only and name not in only:
                continue
            try:
                await scene(d)
            except BudgetStop as exc:
                stopped = str(exc)
                d.emit("error", from_=owner, text="Dừng: chạm trần chi phí MAX_RUN_USD", detail={"error": stopped})
                d.errors += 1
                d.metric()
                break
            except Exception as exc:       # one scene failing must not lose the day: log it and go on
                if mode == "mock":
                    raise
                d.emit("error", from_=owner, text=f"Cảnh {name} lỗi: {type(exc).__name__}"[:90],
                       detail={"error": f"{type(exc).__name__}: {exc}"[:500]})
                d.errors += 1
                d.metric()
    finally:
        d.close()
    result = summary.write(d, started, round(time.perf_counter() - t0, 2), stopped)
    workload.write(out / "events.jsonl", out / "headcount.json")
    return result


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--mode", choices=("mock", "live"), default="mock")
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--out", type=Path, default=None)
    ap.add_argument("--only", nargs="*", help="run only these scenes (for testing a scene live)")
    args = ap.parse_args()
    out = args.out or ROOT / "runs" / (args.mode if args.mode == "mock" else f"live_{time.strftime('%Y%m%d-%H%M%S')}")
    result = asyncio.run(run(args.mode, args.seed, out, args.only))
    print(json.dumps({k: result[k] for k in ("mode", "events", "orders", "gmv_vnd", "agent_actions", "human_approvals",
                                             "cost_total_usd", "errors", "seconds")}, ensure_ascii=False))
    print(f"→ {out}")


if __name__ == "__main__":
    main()
