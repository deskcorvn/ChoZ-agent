"""Run every sample product through the agent and write a CSV report.

    python -m app.batch samples/products

Each product is a folder with photo-1.jpg … photo-3.jpg, and note.txt (what the seller says) and/or
speech.wav (the seller saying it). An optional expected.json ({"price_vnd": 120000, "stock": 50,
"category": "Thời trang nam"}) turns the report into an accuracy check for Vietnamese speech, numbers
and categories. The follow-up question is not answered here, so it shows up as "needs_answer".
"""
from __future__ import annotations

import asyncio
import csv
import json
import sys
import time
from pathlib import Path

from app import config
from app.meta_api import make_client
from app.pipeline import Agent

PHOTO_SUFFIXES = (".jpg", ".jpeg", ".png", ".webp")
CHECKED = ("price_vnd", "stock", "category")


def load_product(folder: Path) -> tuple[list[bytes], bytes | None, str, dict]:
    photos = [p.read_bytes() for p in sorted(folder.glob("photo-*")) if p.suffix.lower() in PHOTO_SUFFIXES][:3]
    audio = (folder / "speech.wav").read_bytes() if (folder / "speech.wav").is_file() else None
    note = (folder / "note.txt").read_text(encoding="utf-8-sig").strip() if (folder / "note.txt").is_file() else ""
    expected = json.loads((folder / "expected.json").read_text(encoding="utf-8-sig")) if (folder / "expected.json").is_file() else {}
    return photos, audio, note, expected


async def run_folder(folder: Path, settings: config.Settings | None = None) -> tuple[Path, list[dict]]:
    settings = settings or config.load()
    client = make_client(settings)
    agent = Agent(client, settings.data_dir / "runs")
    rows = []
    try:
        for product in sorted(p for p in folder.iterdir() if p.is_dir()):
            photos, audio, note, expected = load_product(product)
            row = {"product": product.name, "mode": "mock" if client.mock else "live"}
            try:
                run = agent.create(photos, audio, note)
            except ValueError as exc:
                rows.append({**row, "status": "skipped", "error": str(exc)})
                continue
            await agent.process(run)
            listing = run.listing or {}
            totals = run.totals()
            row.update({"run": run.id, "status": run.status, "seconds": totals["seconds"],
                        "cost_usd": totals["cost_usd"], "transcript": run.transcript,
                        "title": listing.get("title", ""), "category": listing.get("category", ""),
                        "price_vnd": listing.get("price_vnd"), "stock": listing.get("stock"),
                        "verdict": (run.verification or {}).get("verdict", ""),
                        "studio_photo": bool(run.studio_image), "error": run.error})
            for key in CHECKED:
                if key in expected:
                    row[f"{key}_ok"] = listing.get(key) == expected[key]
            rows.append(row)
    finally:
        await client.aclose()
    reports = settings.data_dir / "reports"
    reports.mkdir(parents=True, exist_ok=True)
    path = reports / f"batch-{time.strftime('%Y%m%d-%H%M%S')}.csv"
    fields = list(dict.fromkeys(key for row in rows for key in row))
    with path.open("w", newline="", encoding="utf-8-sig") as handle:   # BOM so Excel reads Vietnamese
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)
    return path, rows


def summary(rows: list[dict]) -> str:
    done = [r for r in rows if r.get("status") not in ("skipped", "failed")]
    lines = [f"{len(rows)} products, {len(done)} processed"]
    if done:
        lines.append(f"mean {sum(r['seconds'] for r in done) / len(done):.1f} s, "
                     f"mean ${sum(r['cost_usd'] for r in done) / len(done):.4f} per listing")
    for key in CHECKED:
        checked = [r[f"{key}_ok"] for r in rows if f"{key}_ok" in r]
        if checked:
            lines.append(f"{key}: {sum(checked)}/{len(checked)} correct")
    return "\n".join(lines)


if __name__ == "__main__":
    target = Path(sys.argv[1] if len(sys.argv) > 1 else "samples/products")
    report, results = asyncio.run(run_folder(target))
    print(summary(results))
    print(f"report: {report}")
