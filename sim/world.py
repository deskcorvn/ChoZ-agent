"""One day of one Trạm Z, generated from a seed: 50 households, 30 baskets, 5 suppliers, 12 goods, 1 warehouse, 1 van.

Every number the agents and the stage use comes from here, so a grader can recompute fees and totals from
world.json alone. Names are generic on purpose (Hộ 07, Nhà cung cấp rau 2): no real household or business. The one
exception is the listing scene, which uses the producer samples in samples/products (published with permission).

Prices are whole thousands of đồng, so 10 % and its 5/2/1/2 split are exact integers; no rounding rule is needed.
"""
from __future__ import annotations

import random
from dataclasses import asdict, dataclass, field

BASKET_PRICES = (70_000, 100_000, 150_000)
SHARE = {"tram": 5, "kho_giao": 2, "gioi_thieu": 1, "cho_z": 2}     # percent of the line value; sums to the 10 % fee
FEE_PCT = sum(SHARE.values())


@dataclass
class Supplier:
    code: str
    name: str
    kind: str


@dataclass
class Good:
    sku: str
    name: str
    unit: str
    price_vnd: int
    supplier: str
    stock: int


@dataclass
class Lot:
    supplier: str
    sku: str
    label: str
    expiry: str          # YYYY-MM-DD
    temp_c: float | None  # None for goods kept at room temperature
    max_temp_c: float | None
    qty: int


@dataclass
class Line:
    sku: str
    qty: int
    price_vnd: int
    supplier: str

    @property
    def amount(self) -> int:
        return self.qty * self.price_vnd


@dataclass
class Order:
    id: str
    household: str
    basket_vnd: int
    lines: list[Line]
    house: str           # one of the five houses the stage draws (ho01-ho10 → loc_nha1 ...)
    referrer: str

    @property
    def total_vnd(self) -> int:
        return sum(line.amount for line in self.lines)


@dataclass
class World:
    seed: int
    day: str
    tram: str
    kho: str
    suppliers: list[Supplier]
    goods: list[Good]
    households: list[str]
    orders: list[Order]
    lots: list[Lot]
    laundry: list[str]
    breakfasts: int
    broken_eggs: str     # the household whose eggs arrive broken (scene nhan_hang)
    notes: dict = field(default_factory=dict)

    def good(self, sku: str) -> Good:
        return next(g for g in self.goods if g.sku == sku)

    def supplier(self, code: str) -> Supplier:
        return next(s for s in self.suppliers if s.code == code)

    def order_of(self, household: str) -> Order | None:
        return next((o for o in self.orders if o.household == household), None)

    def to_json(self) -> dict:
        data = asdict(self)
        for order, raw in zip(self.orders, data["orders"]):
            raw["total_vnd"] = order.total_vnd
        return data


SUPPLIERS = [
    Supplier("ncc1", "Nhà cung cấp đặc sản 1", "đặc sản, đồ khô"),
    Supplier("ncc2", "Hợp tác xã rau 2", "rau củ"),
    Supplier("ncc3", "Vựa thịt 3", "thịt"),
    Supplier("ncc4", "Vựa cá 4", "cá, tôm"),
    Supplier("ncc5", "Hợp tác xã trứng 5", "trứng, đậu phụ"),
]

# sku, name, unit, price, supplier, stock (what the supplier can bring tomorrow morning)
GOODS = [
    ("rau_muong", "Rau muống", "bó", 8_000, "ncc2", 40),
    ("cai_ngot", "Cải ngọt", "bó", 10_000, "ncc2", 10),
    ("ca_chua", "Cà chua", "nửa cân", 12_000, "ncc2", 40),
    ("bi_xanh", "Bí xanh", "quả", 15_000, "ncc2", 30),
    ("ba_chi", "Thịt ba chỉ", "300 g", 45_000, "ncc3", 30),
    ("nac_vai", "Thịt nạc vai", "300 g", 40_000, "ncc3", 40),
    ("ca_ro_phi", "Cá rô phi", "con", 35_000, "ncc4", 20),
    ("tom", "Tôm", "200 g", 40_000, "ncc4", 20),
    ("trung_ga", "Trứng gà", "10 quả", 30_000, "ncc5", 30),
    ("dau_phu", "Đậu phụ", "2 bìa", 10_000, "ncc5", 30),
    ("chao_hau", "Cháo hàu ăn liền", "hộp", 22_000, "ncc1", 80),
    ("banh_da_cua", "Bánh đa cua ăn liền", "hộp", 22_000, "ncc1", 80),
]

# What each basket holds; each list sums to its price.
BASKETS = {
    70_000: [("nac_vai", 1), ("rau_muong", 1), ("ca_chua", 1), ("dau_phu", 1)],
    100_000: [("ba_chi", 1), ("cai_ngot", 1), ("trung_ga", 1), ("bi_xanh", 1)],
    150_000: [("ca_ro_phi", 1), ("tom", 1), ("nac_vai", 1), ("rau_muong", 1), ("ca_chua", 1), ("bi_xanh", 1)],
}
BASKET_MIX = {70_000: 10, 100_000: 12, 150_000: 8}   # 30 baskets


def house_of(household: str) -> str:
    return f"loc_nha{(int(household[2:]) - 1) // 10 + 1}"


def generate(seed: int = 7) -> World:
    rng = random.Random(seed)
    goods = [Good(*g) for g in GOODS]
    price = {g.sku: g for g in goods}
    for basket, items in BASKETS.items():
        assert sum(price[s].price_vnd * q for s, q in items) == basket, basket
    households = [f"ho{i:02d}" for i in range(1, 51)]
    broken_eggs = "ho07"
    others = [h for h in households if h != broken_eggs]
    booked = sorted([broken_eggs] + rng.sample(others, 29))
    tiers = [p for p, n in BASKET_MIX.items() for _ in range(n)]
    rng.shuffle(tiers)
    # ho07 orders the 100k basket (it has the eggs).
    i = booked.index(broken_eggs)
    j = tiers.index(100_000)
    tiers[i], tiers[j] = tiers[j], tiers[i]
    extras = set(rng.sample(booked, 6))           # six households add a box of cháo hàu
    orders = []
    for n, (household, basket) in enumerate(zip(booked, tiers), start=1):
        lines = [Line(sku, qty, price[sku].price_vnd, price[sku].supplier) for sku, qty in BASKETS[basket]]
        if household in extras:
            lines.append(Line("chao_hau", 1, price["chao_hau"].price_vnd, "ncc1"))
        referrer = rng.choice(["hu_tram", "hu_tram", rng.choice([h for h in households if h != household])])
        orders.append(Order(f"DH{n:03d}", household, basket, lines, house_of(household), referrer))
    lots = [
        Lot("ncc2", "rau", "HTX rau 2, thu hoạch 03/11", "2026-11-06", None, None, 120),
        Lot("ncc3", "thit", "Vựa thịt 3, giết mổ 04/11, có tem kiểm dịch", "2026-11-06", 3.0, 4.0, 22),
        Lot("ncc4", "tom", "Vựa cá 4, tôm 200 g", "2026-11-05", 6.5, 4.0, 8),
        Lot("ncc4", "ca", "Vựa cá 4, cá rô phi", "2026-11-05", 2.5, 4.0, 8),
        Lot("ncc5", "trung", "HTX trứng 5, đóng hộp 02/11", "2026-11-25", None, None, 34),
        Lot("ncc1", "chao_hau", "Cháo hàu ăn liền, hộp 120 g", "2027-05-01", None, None, 6),
    ]
    laundry = sorted(rng.sample(booked, 3))
    return World(seed=seed, day="2026-11-04", tram="Trạm Z 01 (góc tiệm tạp hóa)", kho="Kho khu vực 1",
                 suppliers=list(SUPPLIERS), goods=goods, households=households, orders=orders, lots=lots,
                 laundry=laundry, breakfasts=20, broken_eggs=broken_eggs,
                 notes={"baskets": "30 of 50 households (60 %) book a basket: 10 × 70k, 12 × 100k, 8 × 150k",
                        "prices": "made up for the simulation, whole thousands of đồng",
                        "shortage": "cải ngọt: 10 bunches in stock for 12 baskets of 100k",
                        "cold_chain": "tôm lot arrives at 6.5 °C, above the 4 °C limit; the supplier swaps it",
                        "fee": f"{FEE_PCT} % of each completed line, split {SHARE}"})


def fee_split(amount_vnd: int) -> dict:
    """The 10 % platform fee on a completed sale and its 5/2/1/2 split, in đồng."""
    parts = {k: amount_vnd * pct // 100 for k, pct in SHARE.items()}
    return {"amount_vnd": amount_vnd, "fee_vnd": sum(parts.values()), **parts,
            "supplier_receives_vnd": amount_vnd - sum(parts.values())}
