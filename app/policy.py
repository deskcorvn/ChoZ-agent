"""The written listing rules (policy/listing-policy.md): loaded verbatim into the verifier's prompt, and used in code
to check that every block or warning cites a rule that exists."""
from __future__ import annotations

import re
from functools import lru_cache
from pathlib import Path

POLICY_FILE = Path(__file__).resolve().parent.parent / "policy" / "listing-policy.md"
RULE = re.compile(r"^- \*\*([PWC]\d+) · ([^*]+?)\.?\*\*", re.M)


@lru_cache(maxsize=1)
def text() -> str:
    return POLICY_FILE.read_text(encoding="utf-8")


@lru_cache(maxsize=1)
def rules() -> dict[str, str]:
    """Rule id → short title, e.g. {"P1": "Prescription medicine", ...}."""
    return {m.group(1): m.group(2).strip() for m in RULE.finditer(text())}


def cited(ids: list[str]) -> tuple[list[dict], list[str]]:
    """The cited rules that exist (id + title), and the ids that do not."""
    table = rules()
    known = [{"id": i, "title": table[i]} for i in dict.fromkeys(ids) if i in table]
    unknown = [i for i in ids if i not in table]
    return known, unknown
