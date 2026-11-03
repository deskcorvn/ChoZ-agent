"""The agents' tools: plain Python over the world, no model inside. Agents decide and word things; arithmetic is here.

Each tool returns a dict (what the agent reads) and has a short docstring the Claude agents see as the tool
description. The grader recomputes fees from world.json with the same fee_split(), so every đồng on screen has one
source.
"""
from __future__ import annotations

from collections import Counter, defaultdict

from sim.world import BASKETS, World, fee_split

MAX_TEMP_NOTE = "hàng lạnh (thịt, cá, tôm) phải dưới 4 °C khi nhận"
RETURN_POLICY = ("Hàng hỏng, vỡ khi giao: đổi hàng mới cùng loại trong ngày hoặc hoàn tiền dòng đó; khách gửi ảnh "
                 "hoặc trạm xác nhận. Không đổi hàng đã dùng. Mọi đổi trả, hoàn tiền phải có người trực trạm duyệt.")


class Tools:
    def __init__(self, world: World):
        self.w = world
        self.paid: set[str] = set()          # order ids paid at the station
        self.substitutions: list[dict] = []
        self.approved: set[str] = set()      # kinds of change a person has approved (humans.approve sets them)

    # ── ag_chia_don ──────────────────────────────────────────────────────────────────────────────────
    def list_baskets(self) -> dict:
        """Danh sách Giỏ đã đặt cho ngày mai: số Giỏ theo mức giá, tổng tiền, mã đơn và hộ."""
        tiers = Counter(o.basket_vnd for o in self.w.orders)
        return {"baskets": len(self.w.orders), "by_price": {str(k): v for k, v in sorted(tiers.items())},
                "extras": sum(1 for o in self.w.orders for li in o.lines if li.sku == "chao_hau"),
                "total_vnd": sum(o.total_vnd for o in self.w.orders),
                "orders": [{"id": o.id, "household": o.household, "basket_vnd": o.basket_vnd} for o in self.w.orders]}

    def stock(self, supplier: str) -> dict:
        """Tồn hàng nhà cung cấp (mã ncc1..ncc5) có thể giao sáng mai."""
        return {"supplier": supplier, "name": self.w.supplier(supplier).name,
                "goods": [{"sku": g.sku, "name": g.name, "unit": g.unit, "stock": g.stock}
                          for g in self.w.goods if g.supplier == supplier]}

    def make_picklist(self) -> dict:
        """Phiếu gom: tổng số lượng từng mặt hàng cho các đơn, theo nhà cung cấp, và mặt hàng thiếu so với tồn."""
        need: Counter[str] = Counter()
        for o in self.w.orders:
            for li in o.lines:
                need[li.sku] += li.qty
        by_supplier: dict[str, list] = defaultdict(list)
        short = []
        for sku, qty in sorted(need.items()):
            g = self.w.good(sku)
            by_supplier[g.supplier].append({"sku": sku, "name": g.name, "qty": qty, "unit": g.unit})
            if qty > g.stock:
                spare = [x for x in self.w.goods if x.supplier == g.supplier and x.sku != sku
                         and x.stock - need[x.sku] >= qty - g.stock and x.price_vnd <= g.price_vnd]
                alt = max(spare, key=lambda x: x.price_vnd) if spare else None
                short.append({"sku": sku, "name": g.name, "need": qty, "stock": g.stock, "missing": qty - g.stock,
                              "suggest": alt and {"sku": alt.sku, "name": alt.name,
                                                  "refund_vnd_each": g.price_vnd - alt.price_vnd}})
        return {"orders": len(self.w.orders), "items": len(need), "by_supplier": dict(sorted(by_supplier.items())),
                "short": short}

    def apply_substitution(self, sku: str, alt: str, count: int) -> dict:
        """Thay `count` phần `sku` bằng `alt` ở các đơn cuối danh sách. Chỉ chạy được sau khi người trực trạm duyệt."""
        if "thay_hang" not in self.approved:
            raise PermissionError("thay hàng cần người trực trạm duyệt trước")
        changed = []
        for o in reversed(self.w.orders):
            if len(changed) == count:
                break
            for li in o.lines:
                if li.sku == sku:
                    g = self.w.good(alt)
                    li.sku, li.price_vnd, li.supplier = alt, g.price_vnd, g.supplier
                    changed.append({"order": o.id, "household": o.household, "total_vnd": o.total_vnd})
                    break
        record = {"sku": sku, "alt": alt, "orders": changed}
        self.substitutions.append(record)
        return record

    # ── ag_giao_nhan ─────────────────────────────────────────────────────────────────────────────────
    def plan_route(self, stops: list[str]) -> dict:
        """Lịch xe xe1 qua các điểm dừng theo thứ tự (loc_kho, loc_duong, loc_tram ...): giờ đến từng điểm, số túi
        giao ở mỗi trạm, đồ giặt gom trên chuyến về."""
        minutes = {"loc_ncc": 0, "loc_kho": 20, "loc_duong": 5, "loc_tram": 20}
        t, legs = 6 * 60 + 30, []
        seen_tram = False
        for a, b in zip(stops, stops[1:]):
            t += max(minutes.get(b, 10), 5)
            leg = {"from": a, "to": b, "arrive": f"{t // 60:02d}:{t % 60:02d}"}
            if b == "loc_tram":
                leg.update(drop_bags=len(self.w.orders), station=self.w.tram,
                           pick_laundry=0 if not seen_tram else len(self.w.laundry))
                seen_tram = True
            if b == "loc_kho" and seen_tram:
                leg.update(bring_back_laundry=len(self.w.laundry))
            legs.append(leg)
        return {"vehicle": "xe1", "depart": "06:30", "bags": len(self.w.orders), "stations": 1,
                "laundry_households": self.w.laundry, "legs": legs}

    def laundry_pickups(self) -> dict:
        """Các hộ gửi đồ giặt hôm nay, xe gom ở trạm trên chuyến về kho."""
        return {"households": self.w.laundry, "count": len(self.w.laundry), "where": "loc_tram"}

    # ── ag_kiem_chung (warehouse) ────────────────────────────────────────────────────────────────────
    def check_lot(self, supplier: str, item: str) -> dict:
        """Kiểm lô hàng tại kho: nhãn, hạn dùng, nhiệt độ (hàng lạnh dưới 4 °C). Trả đạt hoặc loại kèm lý do."""
        lot = next(x for x in self.w.lots if x.supplier == supplier and x.sku == item)
        reasons = []
        if lot.expiry < self.w.day:
            reasons.append(f"hết hạn {lot.expiry}")
        if lot.temp_c is not None and lot.max_temp_c is not None and lot.temp_c > lot.max_temp_c:
            reasons.append(f"{lot.temp_c:g} °C, quá {lot.max_temp_c:g} °C")
        return {"supplier": supplier, "item": item, "label": lot.label, "expiry": lot.expiry, "temp_c": lot.temp_c,
                "qty": lot.qty, "result": "loại" if reasons else "đạt", "reasons": reasons}

    def replace_lot(self, supplier: str, item: str, temp_c: float) -> dict:
        lot = next(x for x in self.w.lots if x.supplier == supplier and x.sku == item)
        lot.temp_c = temp_c
        return self.check_lot(supplier, item)

    # ── ag_cham_khach ────────────────────────────────────────────────────────────────────────────────
    def order_of(self, household: str) -> dict:
        """Đơn hôm nay của một hộ (mã ho01..ho50): các dòng hàng, nhà cung cấp, tổng tiền, đã thanh toán chưa."""
        o = self.w.order_of(household)
        if o is None:
            return {"household": household, "order": None}
        return {"household": household, "order": o.id, "total_vnd": o.total_vnd, "paid": o.id in self.paid,
                "lines": [{"sku": li.sku, "name": self.w.good(li.sku).name, "qty": li.qty, "price_vnd": li.price_vnd,
                           "supplier": li.supplier} for li in o.lines]}

    def return_policy(self) -> dict:
        """Chính sách đổi trả của Chợ Z."""
        return {"policy": RETURN_POLICY}

    # ── ag_doi_soat ──────────────────────────────────────────────────────────────────────────────────
    def completed_orders(self) -> dict:
        """Đơn đã giao và thanh toán hôm nay, tổng tiền hàng theo từng nhà cung cấp."""
        done = [o for o in self.w.orders if o.id in self.paid]
        sales: Counter[str] = Counter()
        for o in done:
            for li in o.lines:
                sales[li.supplier] += li.amount
        return {"orders": len(done), "gmv_vnd": sum(o.total_vnd for o in done),
                "by_supplier": dict(sorted(sales.items()))}

    def fee_split(self, amount_vnd: int) -> dict:
        """Phí 10 % trên tiền hàng đã bán và cách chia 5 % trạm, 2 % kho và giao, 1 % người giới thiệu, 2 % Chợ Z."""
        return fee_split(int(amount_vnd))

    def settlement(self) -> dict:
        """Bảng đối soát: từng đơn hoàn thành, từng nhà cung cấp, phí và phần nhà cung cấp nhận."""
        rows = []
        for o in self.w.orders:
            if o.id not in self.paid:
                continue
            by: Counter[str] = Counter()
            for li in o.lines:
                by[li.supplier] += li.amount
            for sup, amount in sorted(by.items()):
                rows.append({"order": o.id, "household": o.household, "referrer": o.referrer, "supplier": sup,
                             **fee_split(amount)})
        per_supplier = {}
        for sup in sorted({r["supplier"] for r in rows}):
            mine = [r for r in rows if r["supplier"] == sup]
            per_supplier[sup] = {k: sum(r[k] for r in mine) for k in
                                 ("amount_vnd", "fee_vnd", "tram", "kho_giao", "gioi_thieu", "cho_z",
                                  "supplier_receives_vnd")}
        return {"rows": rows, "per_supplier": per_supplier,
                "totals": {k: sum(v[k] for v in per_supplier.values()) for k in
                           ("amount_vnd", "fee_vnd", "tram", "kho_giao", "gioi_thieu", "cho_z",
                            "supplier_receives_vnd")}}


def basket_contents() -> dict:
    return {str(k): v for k, v in BASKETS.items()}
