"""ag_dang_ban and ag_kiem_chung on Meta: a thin wrapper that runs ChoZ-agent's own pipeline (app/pipeline.py).

Nothing is re-implemented: Agent.create + Agent.process make the listing exactly as the app does (voice → text,
listing, verify against the photos, cut-out, studio photo). Each step the pipeline logged becomes one tool event,
with that step's model, endpoint, duration, usage and cost. Steps that check (verify, catalog match) belong to
ag_kiem_chung; the rest to ag_dang_ban. Mock mode runs the same pipeline on the repo's MockMetaClient.
"""
from __future__ import annotations

import dataclasses
import json
from pathlib import Path
from typing import Any

from app import config
from app.batch import load_product
from app.meta_api import make_client
from app.pipeline import Agent, Run
from sim.schema import TEXT_MAX, Usage

ROOT = Path(__file__).resolve().parents[2]

CHECKER_STEPS = {"verify", "match"}
TOOL_NAMES = {"transcribe": "voice_to_text", "listing": "choz_listing", "verify": "verify_listing",
              "match": "catalog_match", "segment": "cut_out", "background": "studio_room", "compose": "studio_photo"}


class Lister:
    def __init__(self, day: Any):
        self.day = day
        settings = config.load()
        self.settings = dataclasses.replace(settings, mock=day.mode == "mock", data_dir=day.out)
        if day.mode == "live" and not settings.api_key:
            raise RuntimeError("MODEL_API_KEY is not set (live listing needs Meta; see sim/README.md)")
        self.client = make_client(self.settings)
        self.agent = Agent(self.client, day.out / "listing-runs")
        # The app keeps a pool of empty studio rooms Muse Image already painted (data/cache/neutral-rooms); reuse it
        # as the app does, instead of paying to paint a room for every listing of the day.
        pool = ROOT / "data" / "cache" / "neutral-rooms"
        target = day.out / "cache" / "neutral-rooms"
        if pool.is_dir():
            target.mkdir(parents=True, exist_ok=True)
            for room in pool.glob("room-*.png"):
                (target / room.name).write_bytes(room.read_bytes())

    async def close(self) -> None:
        await self.client.aclose()

    async def list(self, folder: Path, audio_path: Path | None = None) -> Run:
        """One listing from a sample folder; `audio_path` replaces the typed note with the seller saying it."""
        photos, audio, note, _ = load_product(folder)
        if audio_path is not None and audio_path.is_file():
            audio, note = audio_path.read_bytes(), ""
        run = self.agent.create(photos, audio, note)
        await self.agent.process(run)
        self.day.log({"provider": "meta", "listing_run": run.id, "status": run.status, "mode": run.mode,
                      "steps": [dataclasses.asdict(s) for s in run.steps], "verification": run.verification})
        return run

    def emit_steps(self, run: Run, photos: int, handoff_text: str) -> None:
        """One tool event per pipeline step that did work; the handoff to the checker goes before its first step."""
        handed = False
        for step in sorted(run.steps, key=lambda s: s.started):
            if step.key not in TOOL_NAMES or step.status not in ("done", "failed"):
                continue
            agent = "ag_kiem_chung" if step.key in CHECKER_STEPS else "ag_dang_ban"
            if agent == "ag_kiem_chung" and not handed:
                self.day.emit("handoff", from_="ag_dang_ban", to="ag_kiem_chung", text=handoff_text)
                handed = True
            usage = meta_usage(step, photos) if step.model else None
            if usage is not None:
                self.day.calls.append({**usage.model_dump(), "agent": agent})
            self.day.emit("tool", step=max(1, step.ms / 1000), from_=agent, usage=usage,
                          tool={"name": TOOL_NAMES[step.key], "input_summary": step.input or step.title,
                                "output_summary": summarize_step(step, run), "ok": step.status == "done"},
                          detail={"step": step.key, "title": step.title, "model": step.model or "local",
                                  "listing_run": run.id, "error": step.error or None,
                                  **({"listing": listing_detail(run)} if step.key == "listing" else {}),
                                  **({"verification": run.verification} if step.key == "verify" else {})})


def meta_usage(step: Any, photos: int) -> Usage | None:
    if step.model.startswith("cache"):
        return None                       # a reused studio room: no call, no cost
    raw = (step.output or {}).get("usage") or {}
    return Usage(provider="meta", model=step.model, endpoint=step.endpoint or "",
                 input_tokens=int(raw.get("prompt_tokens") or 0), output_tokens=int(raw.get("completion_tokens") or 0),
                 cached_tokens=int(((raw.get("prompt_tokens_details") or {}).get("cached_tokens")) or 0),
                 images=int(raw.get("images") or (photos if step.key in ("listing", "verify") else 0)),
                 audio_seconds=float(raw.get("audio_seconds") or 0.0), ms=int(step.ms),
                 cost_usd=0.0 if step.mock else float(step.cost_usd))


def listing_detail(run: Run) -> dict:
    listing = run.listing or {}
    return {k: listing.get(k) for k in ("title", "price_vnd", "stock", "unit", "category", "attributes", "missing")}


def summarize_step(step: Any, run: Run) -> str:
    listing, v = run.listing or {}, run.verification or {}
    if step.key == "transcribe":
        return (run.transcript or "")[:160]
    if step.key == "listing":
        return f"{listing.get('title', '')}, {listing.get('price_vnd') or 0:,} đ, tồn {listing.get('stock')}".replace(",", ".")
    if step.key == "verify":
        bad = [c for c in v.get("checks", []) if c.get("status") == "contradicted"]
        word = {"pass": "khớp ảnh", "warn": "có chỗ lệch", "block": "không đăng"}.get(v.get("verdict", ""), v.get("verdict", ""))
        return f"{word}: {len(v.get('checks', []))} điểm đối chiếu, {len(bad)} lệch" + (
            " (mặt hàng không được bán)" if v.get("prohibited") else "")
    if step.key == "match":
        return str(((run.listing or {}).get("catalog") or {}).get("name") or "không có trong danh mục")
    return step.title[:160]


def short(text: str) -> str:
    text = " ".join(text.split())
    return text if len(text) <= TEXT_MAX else text[:TEXT_MAX - 1].rstrip() + "…"


def contradiction(run: Run) -> dict | None:
    return next((c for c in (run.verification or {}).get("checks", []) if c.get("status") == "contradicted"), None)


def dump(run: Run) -> str:
    return json.dumps(run.public(), ensure_ascii=False, default=str)


# ── what the scene shows after a listing, from a live run or from the mock script ────────────────────────
def outcome_of(run: Run) -> dict:
    v, listing = run.verification or {}, run.listing or {}
    bad = contradiction(run)
    return {"run": run.id, "status": run.status, "verdict": v.get("verdict"), "title": listing.get("title") or "tin",
            "price_vnd": listing.get("price_vnd") or 0, "stock": listing.get("stock"),
            "checks": len(v.get("checks", [])), "check": bad, "summary_vi": v.get("summary_vi") or "",
            "rules": sorted(set(v.get("rule_ids") or [])), "reason": v.get("prohibited_reason") or "",
            "seconds": run.totals()["seconds"], "error": run.error}


# Mock: the same tool events with fixed content and zero usage; the stage and the validator see the same shape.
MOCK = {
    "chao_hau": {"steps": [("voice_to_text", "ag_dang_ban", "6 giây nói", "Cháo hàu ăn liền đặc sản Hải Phòng, hộp 150 "
                            "gam, giá 22 nghìn một hộp, còn 80 hộp"),
                           ("choz_listing", "ag_dang_ban", "1 ảnh + lời nói", "Cháo hàu ăn liền đặc sản Hải Phòng hộp "
                            "120g, 22.000 đ, tồn 80"),
                           ("verify_listing", "ag_kiem_chung", "listing + 1 ảnh", "có chỗ lệch: 7 điểm đối chiếu, 1 lệch"),
                           ("cut_out", "ag_dang_ban", "ảnh hộp", "Cut out product"),
                           ("studio_photo", "ag_dang_ban", "ảnh tách nền", "Compose studio photo (local)")],
                 "outcome": {"status": "ready", "verdict": "warn", "title": "Cháo hàu ăn liền đặc sản Hải Phòng hộp 120g",
                             "price_vnd": 22000, "stock": 80, "checks": 7,
                             "check": {"claim": "Seller said 150g vs photo 120g", "status": "contradicted"},
                             "summary_vi": "Anh nói 150 g, nhãn ghi 120 g. Lấy theo nhãn nhé?", "seconds": 13.0}},
    "banh_da_cua": {"steps": [("choz_listing", "ag_dang_ban", "1 ảnh + chữ", "Bánh đa cua đồng Hải Phòng hộp, 22.000 đ, "
                               "tồn 80"),
                              ("verify_listing", "ag_kiem_chung", "listing + 1 ảnh", "khớp ảnh: 5 điểm đối chiếu, 0 lệch"),
                              ("catalog_match", "ag_kiem_chung", "danh mục mẫu", "Bánh đa cua đồng ăn liền, hộp"),
                              ("cut_out", "ag_dang_ban", "ảnh hộp", "Cut out product"),
                              ("studio_photo", "ag_dang_ban", "ảnh tách nền", "Compose studio photo (local)")],
                    "outcome": {"status": "ready", "verdict": "pass", "title": "Bánh đa cua đồng Hải Phòng hộp",
                                "price_vnd": 22000, "stock": 80, "checks": 5, "check": None, "seconds": 12.9}},
    "nuoc_mam": {"steps": [("choz_listing", "ag_dang_ban", "1 ảnh + chữ", "Nước mắm Quang Hải cá thu 28 độ đạm chai 650 ml, "
                            "65.000 đ, tồn 60"),
                           ("verify_listing", "ag_kiem_chung", "listing + 1 ảnh", "khớp ảnh: 5 điểm đối chiếu, 0 lệch"),
                           ("cut_out", "ag_dang_ban", "ảnh chai", "Cut out product"),
                           ("studio_photo", "ag_dang_ban", "ảnh tách nền", "Compose studio photo (local)")],
                 "outcome": {"status": "ready", "verdict": "pass", "title": "Nước mắm Quang Hải cá thu 28 độ đạm chai 650 ml",
                             "price_vnd": 65000, "stock": 60, "checks": 5, "check": None, "seconds": 12.4}},
    "thuoc_la": {"steps": [("choz_listing", "ag_dang_ban", "1 ảnh + chữ", "Thuốc lá bao mềm, 25.000 đ, tồn 50"),
                           ("verify_listing", "ag_kiem_chung", "listing + 1 ảnh", "không đăng: mặt hàng không được bán")],
                 "outcome": {"status": "blocked", "verdict": "block", "title": "Thuốc lá bao mềm", "price_vnd": 25000,
                             "stock": 50, "checks": 2, "check": None, "rules": ["P3"],
                             "reason": "thuốc lá: nền tảng không đăng bán", "seconds": 4.0}},
}


def emit_mock(day: Any, key: str, handoff_text: str) -> dict:
    handed = False
    for name, agent, inp, out in MOCK[key]["steps"]:
        if agent == "ag_kiem_chung" and not handed:
            day.emit("handoff", from_="ag_dang_ban", to="ag_kiem_chung", text=handoff_text)
            handed = True
        day.emit("tool", step=3, from_=agent, usage=Usage(provider="meta", model="mock", endpoint="mock"),
                 tool={"name": name, "input_summary": inp, "output_summary": out, "ok": True},
                 detail={"sample": key, "mock": True})
    return {"run": f"mock-{key}", "summary_vi": "", "rules": [], "reason": "", "error": "", **MOCK[key]["outcome"]}
