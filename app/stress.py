"""Stress test: the sample products under bad photos, wrong words and prohibited goods, with expectations fixed up front.

    python -m app.stress samples/products [--prohibited <folder with photo-1.* and note.txt>] [--out data/stress]

For every product folder (photo-1.*, note.txt, expected.json) six cases run through the real agent:
  clean          the photo and the seller's words as they are
  blur / dark / tilted   the same words, the photo degraded the way phone photos are (blur, low light, a tilt)
  wrong_weight   the seller states a weight that is not on the pack: flagged (warn) when the photo shows the
                 printed weight (`label_weight_g` in expected.json); when it does not, the agent must not
                 invent a contradiction (measured 1 Nov 2026: 3 of the 5 sample photos show no weight)
  wrong_product  the seller describes a different product than the photo  → must be flagged (warn or block)
and for a prohibited item (if given): the photo as is, blurred and dark → must be blocked.
Expectations for the first four: not blocked, and price / stock / category as in expected.json.
The follow-up question is not answered here (as in app.batch), so a missing field counts as not filled.
"""
from __future__ import annotations

import argparse
import asyncio
import io
import json
import re
import sys
import time
from pathlib import Path

from PIL import Image, ImageEnhance, ImageFilter

from app import config
from app.meta_api import make_client
from app.pipeline import Agent

PHOTO_SUFFIXES = (".jpg", ".jpeg", ".png", ".webp")


def jpeg(im: Image.Image) -> bytes:
    buffer = io.BytesIO()
    im.convert("RGB").save(buffer, "JPEG", quality=88)
    return buffer.getvalue()


def degrade(photo: bytes, how: str) -> bytes:
    im = Image.open(io.BytesIO(photo)).convert("RGB")
    if how == "blur":
        im = im.filter(ImageFilter.GaussianBlur(radius=max(im.width // 260, 2)))
    elif how == "dark":
        im = ImageEnhance.Contrast(ImageEnhance.Brightness(im).enhance(0.42)).enhance(0.8)
    elif how == "tilted":
        im = im.rotate(14, expand=True, fillcolor=(214, 208, 198))
        w, h = im.size
        im = im.crop((int(w * 0.06), int(h * 0.06), int(w * 0.94), int(h * 0.94)))
    return jpeg(im)


def wrong_weight(note: str) -> str:
    """Change the spoken weight to one that is not on the pack (or add one when the seller gave none)."""
    m = re.search(r"(\d+)\s*gam|(một trăm (hai lăm|năm mươi|hai mươi))\s*gam", note)
    if m:
        return note[:m.start()] + "hai trăm gam" + note[m.end():]
    return note.rstrip(".") + ", hộp hai trăm gam."


def cases(products: Path, prohibited: Path | None) -> list[dict]:
    folders = sorted(p for p in products.iterdir() if (p / "note.txt").is_file())
    notes = {f.name: (f / "note.txt").read_text(encoding="utf-8-sig").strip() for f in folders}
    out = []
    for i, folder in enumerate(folders):
        photo = next(p for p in sorted(folder.glob("photo-*")) if p.suffix.lower() in PHOTO_SUFFIXES).read_bytes()
        expected = json.loads((folder / "expected.json").read_text(encoding="utf-8-sig"))
        note = notes[folder.name]
        other = folders[(i + 2) % len(folders)]          # a product of another kind (crab vs fish vs porridge)
        base = {"product": folder.name, "expected": expected}
        out.append({**base, "case": "clean", "photo": photo, "note": note, "expect": "listed"})
        for how in ("blur", "dark", "tilted"):
            out.append({**base, "case": how, "photo": degrade(photo, how), "note": note, "expect": "listed"})
        out.append({**base, "case": "wrong_weight", "photo": photo, "note": wrong_weight(note),
                    "expect": "flagged" if expected.get("label_weight_g") else "no_false_alarm"})
        out.append({**base, "case": "wrong_product", "photo": photo, "note": notes[other.name], "expect": "flagged",
                    "said": other.name})
    if prohibited:
        photo = next(p for p in sorted(prohibited.glob("photo-*")) if p.suffix.lower() in PHOTO_SUFFIXES).read_bytes()
        note = (prohibited / "note.txt").read_text(encoding="utf-8-sig").strip()
        for how in ("clean", "blur", "dark"):
            out.append({"product": prohibited.name, "case": f"prohibited_{how}", "expected": {}, "expect": "blocked",
                        "photo": photo if how == "clean" else degrade(photo, how), "note": note})
    return out


def judge(case: dict, run) -> dict:
    v = run.verification or {}
    listing = run.listing or {}
    verdict = v.get("verdict", "")
    row = {"product": case["product"], "case": case["case"], "expect": case["expect"], "status": run.status,
           "verdict": verdict, "summary_vi": v.get("summary_vi", ""), "seconds": run.totals().get("seconds"),
           "cost_usd": run.totals().get("cost_usd")}
    if case["expect"] == "listed":
        fields = {k: listing.get(k) == case["expected"][k] for k in ("price_vnd", "stock", "category") if k in case["expected"]}
        row["fields_ok"] = fields
        row["ok"] = run.status != "blocked" and all(fields.values())
    elif case["expect"] == "flagged":
        row["ok"] = verdict in ("warn", "block")
    elif case["expect"] == "no_false_alarm":      # nothing printed to compare with: no invented contradiction
        row["ok"] = verdict != "block" and "gam" not in row["summary_vi"].lower()
    else:
        row["ok"] = run.status == "blocked"
    return row


async def run_all(all_cases: list[dict], settings, concurrency: int = 4) -> list[dict]:
    client = make_client(settings)
    agent = Agent(client, settings.data_dir / "runs")
    gate = asyncio.Semaphore(concurrency)

    async def one(case: dict) -> dict:
        async with gate:
            run = agent.create([case["photo"]], None, case["note"])
            await agent.process(run)
            row = judge(case, run)
            print(f"{'ok ' if row['ok'] else 'NO '} {row['product']:<24} {row['case']:<18} {row['status']:<13} "
                  f"{row['verdict']:<5} {row['seconds']} s", flush=True)
            return row

    try:
        return await asyncio.gather(*(one(c) for c in all_cases))
    finally:
        await client.aclose()


def summarise(rows: list[dict]) -> dict:
    groups: dict[str, list[dict]] = {}
    for r in rows:
        key = "prohibited" if r["case"].startswith("prohibited") else r["case"]
        if r["case"] == "wrong_weight":
            key = "wrong_weight_printed" if r["expect"] == "flagged" else "wrong_weight_not_in_photo"
        groups.setdefault(key, []).append(r)
    listed = [r for r in rows if r["expect"] != "blocked"]
    return {"runs": len(rows), "ok": sum(r["ok"] for r in rows),
            "by_case": {k: f"{sum(r['ok'] for r in v)}/{len(v)}" for k, v in groups.items()},
            "false_blocks": sum(r["status"] == "blocked" for r in listed),
            "seconds_mean": round(sum(r["seconds"] or 0 for r in rows) / max(len(rows), 1), 1),
            "cost_total_usd": round(sum(r["cost_usd"] or 0 for r in rows), 4)}


def main() -> int:
    ap = argparse.ArgumentParser(prog="python -m app.stress")
    ap.add_argument("products", type=Path)
    ap.add_argument("--prohibited", type=Path)
    ap.add_argument("--out", type=Path, default=Path("data/stress"))
    a = ap.parse_args()
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    settings = config.load()
    all_cases = cases(a.products, a.prohibited)
    t0 = time.time()
    rows = asyncio.run(run_all(all_cases, settings))
    summary = {**summarise(rows), "wall_seconds": round(time.time() - t0), "mode": "mock" if settings.mock else "live",
               "date": time.strftime("%Y-%m-%d")}
    a.out.mkdir(parents=True, exist_ok=True)
    (a.out / "report.json").write_text(json.dumps({"summary": summary, "rows": rows}, ensure_ascii=False, indent=1),
                                       encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
