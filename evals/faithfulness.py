"""Claim-level faithfulness of the generated listings (the RAGAS idea, with an independent judge).

The generator is Muse Spark; the judge is Claude (another model family, so the model does not grade itself). For each
run the judge gets the seller's original photo, what the seller said, the catalog record that was matched (if any),
and the listing text; it splits title, description and attributes into atomic claims and labels each: supported by
the photo, the seller's words, the catalog record, or unsupported. Faithfulness = supported / all claims.

    python evals/faithfulness.py evals/raw/rag-catalogue/*.csv evals/raw/gate-rag/*.csv   → evals/faithfulness.md

Needs `anthropic` and ANTHROPIC_API_KEY (an eval tool, not part of the app). Runs are read from data/runs.
"""
from __future__ import annotations

import base64
import csv
import io
import json
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Literal

from PIL import Image
from pydantic import BaseModel

ROOT = Path(__file__).resolve().parent.parent
MODEL = "claude-opus-5-5"


class Claim(BaseModel):
    field: Literal["title", "description", "attribute"]
    claim: str
    support: Literal["photo", "seller_words", "catalog", "unsupported"]
    note: str


class Judgement(BaseModel):
    claims: list[Claim]


PROMPT = """You audit a marketplace listing that an AI wrote from a seller's photo and words. Split the listing's title,
description and attribute values into atomic factual claims (one fact each; skip pure style such as "mời bạn thử").
Label each claim with its support:
- "photo": visible in the photo, including text printed on the pack;
- "seller_words": stated by the seller;
- "catalog": stated in the catalog record;
- "unsupported": none of these shows it (taste, freshness, ingredients, benefits, usage claims not printed).
Generic, obviously true statements about the product type that a buyer would not rely on (e.g. "ăn liền" for an
instant product shown as such) count as photo. Be strict about specific qualities, quantities, origins and benefits."""


def photo_block(run_dir: Path) -> dict:
    im = Image.open(run_dir / "photo-1.jpg").convert("RGB")
    im.thumbnail((1024, 1024))
    buf = io.BytesIO()
    im.save(buf, "JPEG", quality=85)
    return {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg",
                                        "data": base64.standard_b64encode(buf.getvalue()).decode()}}


def judge(client, run: dict, run_dir: Path) -> dict:
    listing = run.get("listing") or {}
    cat = (listing.get("catalog") or {})
    record = cat.get("facts") and {"name": cat.get("name"), **cat["facts"]}
    text = (f"SELLER'S WORDS: {' '.join(x for x in (run.get('transcript'), run.get('note')) if x)}\n\n"
            f"CATALOG RECORD: {json.dumps(record, ensure_ascii=False) if record else 'none matched'}\n\n"
            f"LISTING:\ntitle: {listing.get('title')}\ndescription: {listing.get('description')}\n"
            f"attributes: {json.dumps(listing.get('attributes'), ensure_ascii=False)}")
    r = client.messages.parse(model=MODEL, max_tokens=4000, system=PROMPT, output_format=Judgement,
                              messages=[{"role": "user", "content": [photo_block(run_dir), {"type": "text", "text": text}]}])
    claims = [c.model_dump() for c in r.parsed_output.claims]
    return {"run": run["id"], "title": listing.get("title"), "claims": claims}


def main(csvs: list[Path]) -> None:
    import anthropic
    client = anthropic.Anthropic()
    runs = []
    for f in csvs:
        for row in csv.DictReader(f.open(encoding="utf-8-sig")):
            d = ROOT / "data" / "runs" / row["run"]
            if (d / "run.json").is_file() and row.get("status") in ("ready", "needs_answer", "published"):
                runs.append((json.loads((d / "run.json").read_text(encoding="utf-8")), d))
    with ThreadPoolExecutor(4) as pool:
        results = list(pool.map(lambda x: judge(client, *x), runs))
    claims = [c for r in results for c in r["claims"]]
    by_support = {k: sum(c["support"] == k for c in claims) for k in ("photo", "seller_words", "catalog", "unsupported")}
    by_field = {}
    for c in claims:
        f = by_field.setdefault(c["field"], [0, 0])
        f[0] += c["support"] != "unsupported"
        f[1] += 1
    clean = sum(all(c["support"] != "unsupported" for c in r["claims"]) for r in results)
    lines = ["# Faithfulness of generated listings (claim level, judge: Claude, generator: Muse Spark)", "",
             f"{len(results)} live listings, {len(claims)} atomic claims. Supported: "
             f"{len(claims) - by_support['unsupported']}/{len(claims)} "
             f"({(len(claims) - by_support['unsupported']) / max(len(claims), 1):.0%}). Listings with no unsupported "
             f"claim: {clean}/{len(results)}.", "",
             "| Support | Claims |", "|---|---|", *[f"| {k} | {v} |" for k, v in by_support.items()], "",
             "| Field | Supported |", "|---|---|", *[f"| {k} | {a}/{b} |" for k, (a, b) in by_field.items()], "",
             "Unsupported claims:", ""]
    lines += [f"- {r['title']}: “{c['claim']}” ({c['field']}) — {c['note']}"
              for r in results for c in r["claims"] if c["support"] == "unsupported"]
    out = ROOT / "evals" / "faithfulness.md"
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    (ROOT / "evals" / "raw" / "faithfulness.json").write_text(json.dumps(results, ensure_ascii=False, indent=1),
                                                               encoding="utf-8")
    print("\n".join(lines[:12]))


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    main([Path(p) for p in sys.argv[1:]])
