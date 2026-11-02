"""What produced a run: short hashes of the prompts, the written rules and the catalog, stamped on every run, its
trace and every batch report, so a result can always be tied to the exact instructions and knowledge behind it."""
from __future__ import annotations

import hashlib
from functools import lru_cache
from pathlib import Path

from app import policy, prompts

ROOT = Path(__file__).resolve().parent.parent


def _h(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:10]


@lru_cache(maxsize=1)
def current() -> dict[str, str]:
    kb_text = "".join(p.read_text(encoding="utf-8") for p in sorted((ROOT / "samples" / "kb").glob("*.json")))
    return {"prompt_listing": _h(prompts.LISTING), "prompt_verify": _h(prompts.VERIFY),
            "prompt_answer": _h(prompts.SALE_FACTS), "policy": _h(policy.text()), "catalog": _h(kb_text)}
