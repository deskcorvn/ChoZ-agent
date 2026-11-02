"""Catalog knowledge base and retrieval: the grounding layer behind a listing (retrieval-augmented verification).

Why retrieval, and why only for the catalog: the listing rules and the category list are ~20 short items, so they go
to the verifier in full (retrieving them would only add a way to miss one). A marketplace catalog grows to thousands
of products across sellers, so it is retrieved: the verifier sees the few records that may be the same product, picks
one or says none, and code checks its facts against the listing.

Pipeline (each stage logged, see app/pipeline.py):
1. Query = what the photo reader and the seller said: draft title, attributes, the English product phrase, the words.
2. Two lexical retrievers over each record's contextual text (name + line + pack words + weights + brand + aliases,
   i.e. the record with its context prepended, as in contextual retrieval):
   - BM25 over diacritic-folded word unigrams and bigrams: exact names, pack types, "125g", brand abbreviations;
   - TF-IDF cosine over character trigrams, robust to speech slips ("cháo lẩu" for "cháo hàu") and missing diacritics.
   Measured on 112 in-catalog queries (evals/rag/retrieval.md): BM25 alone 111/112 at rank 1, trigrams 108/112,
   reciprocal-rank fusion of the two 108/112 (the trigrams pull "...125g" titles towards the cup at 150g). So BM25
   ranks, and trigrams are only a fallback when BM25 matches nothing (a query made only of misspelled words).
   The query the pipeline sends (title + photo phrase + seller words) is 27/27 at rank 1 with every method.
3. The top candidates go to the verifier (Muse Spark, which also sees the photos) as fenced data; it returns the id
   of the same product (brand, line, pack type) or "none". Code accepts only an id that was retrieved.
4. Facts are reconciled by source priority (pack label > price list): the price list's "150gr" for the oyster
   porridge box loses to the "120g" printed on it, and both stay visible with their sources.

Dense embeddings are not used: the Meta Model API has no embedding endpoint (checked 2 Nov 2026) and the catalog is
small; app/rag_eval.py measures what each retriever contributes.
"""
from __future__ import annotations

import json
import math
import os
import re
import unicodedata
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RRF_K = 60
TRIGRAM_FLOOR = 0.35      # fallback only: below this a misspelled query resembles nothing in the catalog


def fold(text: str) -> str:
    """Lowercase, no diacritics (đ → d), letters and digits only: 'Cháo Hàu 120g' → 'chao hau 120g'."""
    text = unicodedata.normalize("NFD", (text or "").lower().replace("đ", "d").replace("Đ", "d"))
    text = "".join(c for c in text if unicodedata.category(c) != "Mn")
    text = re.sub(r"(\d)\s*(gr|gam|gram)\b", r"\1g", text)          # 120 gam / 120gr / 120 g → 120g
    text = re.sub(r"(\d)\s+g\b", r"\1g", text)
    return " ".join(re.findall(r"[a-z0-9]+", text))


def words(text: str) -> list[str]:
    toks = fold(text).split()
    return toks + [f"{a}_{b}" for a, b in zip(toks, toks[1:])]


def trigrams(text: str) -> list[str]:
    s = f"  {fold(text)}  "
    return [s[i:i + 3] for i in range(len(s) - 2)]


@dataclass
class Record:
    id: str
    brand: str
    name: str
    line: str
    pack: str
    category_id: str
    facts: dict
    aliases: list[str] = field(default_factory=list)
    variants: list[str] = field(default_factory=list)
    extra: dict = field(default_factory=dict)

    def text(self) -> str:
        """The record with its context prepended (brand, line, pack, variants, aliases)."""
        weights = sorted({f"{v['value']}g" for v in self.facts.get("weight_g") or []})
        pack_words = {"hộp": "hộp cốc tô cup bowl tub", "gói": "gói túi pack packet bag"}.get(self.pack, "")
        return " ".join([self.brand, self.name, self.line, self.pack, pack_words, *weights, *self.variants, *self.aliases])

    def fact(self, key: str, priority: list[str]) -> dict | None:
        """The value of a fact by source priority, with the losing values kept for display."""
        values = self.facts.get(key) or []
        if not values:
            return None
        ranked = sorted(values, key=lambda v: priority.index(v["source"]) if v["source"] in priority else 99)
        best = dict(ranked[0])
        best["others"] = [v for v in ranked[1:] if v["value"] != best["value"]]
        return best

    def evidence(self, priority: list[str]) -> dict:
        """What the verifier and the preview see: reconciled facts with sources."""
        out = {"id": self.id, "brand": self.brand, "name": self.name, "pack": self.pack, "category_id": self.category_id}
        for key in ("weight_g", "retail_price_vnd", "units_per_carton"):
            f = self.fact(key, priority)
            if f:
                out[key] = {"value": f["value"], "source": f["source"],
                            **({"conflicting": [{"value": o["value"], "source": o["source"]} for o in f["others"]]}
                               if f["others"] else {})}
        return out


class BM25:
    def __init__(self, docs: list[list[str]], k1: float = 1.2, b: float = 0.75):
        self.docs = [Counter(d) for d in docs]
        self.lens = [len(d) for d in docs]
        self.avg = sum(self.lens) / max(len(docs), 1)
        df = Counter(t for d in self.docs for t in d)
        n = len(docs)
        self.idf = {t: math.log(1 + (n - c + 0.5) / (c + 0.5)) for t, c in df.items()}
        self.k1, self.b = k1, b

    def scores(self, query: list[str]) -> list[float]:
        out = []
        for d, length in zip(self.docs, self.lens):
            s = 0.0
            for t in set(query):
                if t in d:
                    tf = d[t]
                    s += self.idf[t] * tf * (self.k1 + 1) / (tf + self.k1 * (1 - self.b + self.b * length / self.avg))
            out.append(s)
        return out


class TfIdf:
    def __init__(self, docs: list[list[str]]):
        n = len(docs)
        df = Counter(t for d in docs for t in set(d))
        self.idf = {t: math.log((1 + n) / (1 + c)) + 1 for t, c in df.items()}
        self.vecs = [self._vec(d) for d in docs]

    def _vec(self, toks: list[str]) -> dict[str, float]:
        tf = Counter(toks)
        v = {t: c * self.idf.get(t, 0.0) for t, c in tf.items()}
        norm = math.sqrt(sum(x * x for x in v.values())) or 1.0
        return {t: x / norm for t, x in v.items()}

    def scores(self, query: list[str]) -> list[float]:
        q = self._vec(query)
        return [sum(w * d.get(t, 0.0) for t, w in q.items()) for d in self.vecs]


def ranks(scores: list[float]) -> list[int]:
    """1-based rank per document (ties share the better rank); documents with score 0 get no rank (0)."""
    order = sorted(range(len(scores)), key=lambda i: -scores[i])
    out = [0] * len(scores)
    for r, i in enumerate(order, start=1):
        if scores[i] > 0:
            out[i] = r
    return out


class KnowledgeBase:
    def __init__(self, records: list[Record], priority: list[str]):
        self.records = records
        self.priority = priority
        self.by_id = {r.id: r for r in records}
        self.bm25 = BM25([words(r.text()) for r in records])
        self.grams = TfIdf([trigrams(r.text()) for r in records])

    @classmethod
    def load(cls, paths: list[Path] | None = None) -> KnowledgeBase:
        if paths is None:
            env = os.environ.get("LISTING_KB")
            paths = [Path(p) for p in env.split(os.pathsep)] if env else sorted((ROOT / "samples" / "kb").glob("*.json"))
        records, priority = [], ["pack_label", "price_list"]
        for p in paths:
            data = json.loads(Path(p).read_text(encoding="utf-8"))
            priority = data.get("source_priority", priority)
            for r in data["records"]:
                known = {k: r.get(k) for k in ("id", "brand", "name", "line", "pack", "category_id", "facts")}
                records.append(Record(**known, aliases=r.get("aliases", []), variants=r.get("variants", []),
                                      extra={k: v for k, v in r.items() if k not in known and k not in ("aliases", "variants")}))
        return cls(records, priority)

    def search(self, query: str, k: int = 3, method: str = "bm25+fallback") -> list[dict]:
        """Top-k records with each retriever's rank. `method`: bm25+fallback (default) | bm25 | trigram | hybrid (RRF)."""
        b = ranks(self.bm25.scores(words(query)))
        gs = self.grams.scores(trigrams(query))
        g = ranks(gs)
        if method == "bm25+fallback":
            if any(b):
                method = "bm25"
            elif max(gs, default=0) >= TRIGRAM_FLOOR:
                method = "trigram"
            else:
                return []                     # nothing in the catalog resembles it: no candidates, nothing to force
        rows = []
        for i, rec in enumerate(self.records):
            parts = {"bm25": b[i], "trigram": g[i]}
            use = [parts["bm25"]] if method == "bm25" else [parts["trigram"]] if method == "trigram" else [b[i], g[i]]
            score = sum(1 / (RRF_K + r) for r in use if r)
            if score:
                rows.append({"id": rec.id, "score": round(score, 5), "ranks": parts})
        rows.sort(key=lambda r: -r["score"])
        return rows[:k]

    def candidates(self, query: str, k: int = 3) -> list[dict]:
        """What the verifier gets: retrieved records with reconciled facts and their sources."""
        return [{**self.by_id[h["id"]].evidence(self.priority), "retrieval": h} for h in self.search(query, k)]


_KB: KnowledgeBase | None = None


def default() -> KnowledgeBase:
    global _KB
    if _KB is None:
        _KB = KnowledgeBase.load()
    return _KB


WEIGHT = re.compile(r"(\d{2,4})\s*(?:g|gr|gam|gram)\b", re.I)


DIGITS = {"không": 0, "một": 1, "mốt": 1, "hai": 2, "ba": 3, "bốn": 4, "tư": 4, "năm": 5, "lăm": 5, "sáu": 6,
          "bảy": 7, "tám": 8, "chín": 9}
SPOKEN_WEIGHT = re.compile(r"((?:(?:không|một|mốt|hai|ba|bốn|tư|năm|lăm|sáu|bảy|tám|chín|mười|mươi|trăm|linh|lẻ)\s+){1,6})"
                           r"(?:gam|gram|gờ ram|g)" + r"", re.I)


def vi_number(words_: str) -> int | None:
    """Spoken Vietnamese 1-999: 'một trăm năm mươi' → 150, 'một trăm hai lăm' → 125, 'tám mươi' → 80."""
    value, cur, seen = 0, 0, False
    for t in words_.lower().split():
        if t in DIGITS:
            if cur:                      # 'hai lăm' after 'trăm': tens and units without 'mươi'
                value += cur * 10
            cur, seen = DIGITS[t], True
        elif t == "mười":
            value, cur, seen = value + 10, 0, True
        elif t == "mươi":
            value, cur = value + cur * 10, 0
        elif t == "trăm":
            value, cur = value + cur * 100, 0
        elif t in ("linh", "lẻ"):
            continue
    return value + cur if seen else None


def weight_in(texts: list[str]) -> int | None:
    """Grams named in the text: digits ('120g', '150 gam') or spoken Vietnamese ('một trăm năm mươi gam')."""
    for t in texts:
        m = WEIGHT.search(t or "")
        if m:
            return int(m.group(1))
        m = SPOKEN_WEIGHT.search(t or "")
        if m:
            n = vi_number(m.group(1))
            if n:
                return n
    return None
