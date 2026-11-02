"""Retrieval evaluation for the catalog knowledge base (app/kb.py).

    python -m app.rag_eval build     # (once) evals/rag/queries.jsonl from live runs: titles, seller words, phrases, transcripts
    python -m app.rag_eval           # recall@1, recall@3, MRR per retriever and per query kind → evals/rag/retrieval.md

Each query has a gold record id, or "none" for products that are not in the catalog (they must not be matched
downstream; retrieval always returns candidates, so abstention is measured on the verifier's decision, in the batch).
"""
from __future__ import annotations

import csv
import json
import sys
from collections import defaultdict
from pathlib import Path

from app.kb import KnowledgeBase

ROOT = Path(__file__).resolve().parent.parent
QUERIES = ROOT / "evals" / "rag" / "queries.jsonl"
GOLD_BY_PREFIX = {"ca-ro-goi": "kt-ca-ro-goi", "ca-ro-hop": "kt-ca-ro-hop", "chao-hau-hop": "kt-chao-hau-hop",
                  "cua-goi": "kt-cua-goi", "cua-hop": "kt-cua-hop", "01-banh-da-ca-ro-goi": "kt-ca-ro-goi",
                  "02-banh-da-ca-ro-hop": "kt-ca-ro-hop", "03-banh-da-cua-goi": "kt-cua-goi",
                  "04-banh-da-cua-hop": "kt-cua-hop", "05-chao-hau-hop": "kt-chao-hau-hop"}


def gold_for(product: str) -> str:
    for prefix, gid in GOLD_BY_PREFIX.items():
        if product.startswith(prefix):
            return gid
    return "none"


def build(runs_dir: Path, catalog_dir: Path, rush_dir: Path) -> None:
    rows = []
    for f in sorted((ROOT / "evals" / "raw").rglob("*.csv")):
        for r in csv.DictReader(f.open(encoding="utf-8-sig")):
            gold = gold_for(r["product"])
            rows.append({"kind": "title", "query": r["title"], "gold": gold, "from": f"{f.parent.name}:{r['product']}"})
            run = runs_dir / r["run"] / "run.json"
            if run.is_file():
                data = json.loads(run.read_text(encoding="utf-8"))
                phrase = (data.get("listing") or {}).get("product_phrase_en")
                if phrase and phrase != "product":
                    rows.append({"kind": "photo_phrase_en", "query": phrase, "gold": gold, "from": f"run {r['run']}"})
                if data.get("transcript"):
                    rows.append({"kind": "speech", "query": data["transcript"], "gold": gold, "from": f"run {r['run']}"})
            note = catalog_dir / r["product"] / "note.txt"
            words_ = note.read_text(encoding="utf-8-sig").strip() if note.is_file() else ""
            if words_:
                rows.append({"kind": "seller_words", "query": words_, "gold": gold, "from": f"note {r['product']}"})
            # what the pipeline sends: title + the photo's English phrase + the seller's words
            phrase_ = ""
            if run.is_file():
                phrase_ = (json.loads(run.read_text(encoding="utf-8")).get("listing") or {}).get("product_phrase_en") or ""
            rows.append({"kind": "combined", "query": " ".join(x for x in (r["title"], phrase_, words_) if x),
                         "gold": gold, "from": f"{f.parent.name}:{r['product']}"})
    rush_gold = {"A": "kt-cua-hop", "B": "kt-chao-hau-hop", "C": "kt-ca-ro-hop", "D": "none", "PM": "kt-chao-hau-hop",
                 "PC": "kt-chao-hau-hop", "PI": "kt-chao-hau-hop", "PD": "none"}
    for name, gold in rush_gold.items():
        f = rush_dir / f"{name}.json"
        if f.is_file():
            run = json.loads(f.read_text(encoding="utf-8")).get("run") or {}
            if run.get("transcript"):
                rows.append({"kind": "speech", "query": run["transcript"], "gold": gold, "from": f"rush {name}"})
    # Noisy variants of the seller's words: the speech confusions seen in live runs (cháo lẩu, cháu hầu for cháo hàu,
    # của for cua) and text typed without diacritics; deterministic, so the set is reproducible.
    from app.kb import fold
    confusions = [("cháo hàu", "cháo lẩu"), ("Cháo hàu", "Cháu hầu"), ("cua đồng", "của đồng"), ("cá rô", "cá rổ"),
                  ("bánh đa", "bán đa")]
    for r in [r for r in rows if r["kind"] in ("seller_words", "title") and r["gold"] != "none"]:
        q = r["query"]
        for a, b in confusions:
            if a.lower() in q.lower():
                i = q.lower().index(a.lower())
                rows.append({"kind": "noisy_speech", "query": q[:i] + b + q[i + len(a):], "gold": r["gold"], "from": r["from"]})
                break
        rows.append({"kind": "no_diacritics", "query": fold(q), "gold": r["gold"], "from": r["from"]})
    seen, out = set(), []
    for r in rows:                                   # one row per distinct query text
        key = (r["kind"], r["query"].strip().lower())
        if r["query"].strip() and key not in seen:
            seen.add(key)
            out.append(r)
    QUERIES.parent.mkdir(parents=True, exist_ok=True)
    QUERIES.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in out), encoding="utf-8")
    print(len(out), "queries →", QUERIES)


def evaluate(kb: KnowledgeBase, rows: list[dict]) -> dict:
    res: dict = defaultdict(lambda: defaultdict(lambda: {"n": 0, "r1": 0, "r3": 0, "mrr": 0.0}))
    misses = []
    for row in rows:
        if row["gold"] == "none":
            continue
        for method in ("bm25", "trigram", "hybrid", "bm25+fallback"):
            hits = [h["id"] for h in kb.search(row["query"], k=len(kb.records), method=method)]
            rank = hits.index(row["gold"]) + 1 if row["gold"] in hits else 0
            for kind in (row["kind"], "all"):
                m = res[method][kind]
                m["n"] += 1
                m["r1"] += rank == 1
                m["r3"] += 1 <= rank <= 3
                m["mrr"] += 1 / rank if rank else 0
            if method == "bm25+fallback" and rank != 1:
                misses.append({**row, "rank": rank, "top": hits[:3]})
    return {"metrics": res, "misses": misses}


def report(result: dict, n_none: int) -> str:
    lines = ["# Catalog retrieval (app/kb.py), offline", "",
             "Recall@1 / Recall@3 / MRR on queries whose product is in the catalog; out-of-catalog queries "
             f"({n_none}) are not scored here: matching or abstaining is the verifier's decision, measured live.", "",
             "| Retriever | Query kind | n | Recall@1 | Recall@3 | MRR |", "|---|---|---|---|---|---|"]
    for method in ("bm25", "trigram", "hybrid", "bm25+fallback"):
        for kind, m in sorted(result["metrics"][method].items(), key=lambda kv: (kv[0] != "all", kv[0])):
            n = m["n"]
            lines.append(f"| {method} | {kind} | {n} | {m['r1']}/{n} | {m['r3']}/{n} | {m['mrr'] / n:.3f} |")
    if result["misses"]:
        lines += ["", "Misses at rank 1 of the retriever in use (bm25+fallback):", ""]
        lines += [f"- {m['kind']}: “{m['query'][:80]}” gold {m['gold']} at rank {m['rank']}, top {m['top']}"
                  for m in result["misses"]]
    return "\n".join(lines) + "\n"


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    if sys.argv[1:2] == ["build"]:
        build(ROOT / "data" / "runs", Path("F:/_tmp/catalog-eval"), Path("F:/Gifty/video-contest/rushes"))
        sys.exit(0)
    rows = [json.loads(line) for line in QUERIES.read_text(encoding="utf-8").splitlines() if line.strip()]
    result = evaluate(KnowledgeBase.load(), rows)
    text = report(result, sum(r["gold"] == "none" for r in rows))
    (ROOT / "evals" / "rag" / "retrieval.md").write_text(text, encoding="utf-8")
    print(text)
