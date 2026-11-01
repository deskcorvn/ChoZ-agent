"""Per-seller access: each seller has their own token, and sees, publishes, exports and deletes only their own runs.

Tokens are stored as SHA-256 hashes in data/sellers.json ({"seller_id": "<sha256 of token>"}), so the file does not
hold anything that opens the app. A token is shown once, when it is created:

    python -m app.sellers add ba-nam          # prints a new token for seller "ba-nam"
    python -m app.sellers list

LISTING_ACCESS_TOKEN (one shared token) still works and maps to the seller id "shop".
"""
from __future__ import annotations

import contextvars
import hashlib
import hmac
import json
import os
import re
import secrets
import sys
from pathlib import Path

current: contextvars.ContextVar[str] = contextvars.ContextVar("seller", default="")


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def path_for(data_dir: Path) -> Path:
    return data_dir / "sellers.json"


def load(data_dir: Path) -> dict[str, str]:
    """seller id → token hash, from data/sellers.json plus the shared LISTING_ACCESS_TOKEN (as "shop")."""
    table = {}
    p = path_for(data_dir)
    if p.is_file():
        table.update(json.loads(p.read_text(encoding="utf-8")))
    shared = os.environ.get("LISTING_ACCESS_TOKEN", "")
    if shared:
        table.setdefault("shop", _hash(shared))
    return table


def who(table: dict[str, str], token: str | None) -> str | None:
    """The seller a token belongs to, compared in constant time against every entry, or None."""
    if not token:
        return None
    given = _hash(token)
    found = None
    for seller, digest in table.items():
        if hmac.compare_digest(given, digest):
            found = seller
    return found


def add(data_dir: Path, seller: str) -> str:
    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{1,39}", seller):
        raise ValueError("seller id: 2-40 lowercase letters, digits, - or _")
    p = path_for(data_dir)
    table = json.loads(p.read_text(encoding="utf-8")) if p.is_file() else {}
    token = secrets.token_urlsafe(24)
    table[seller] = _hash(token)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(table, indent=2), encoding="utf-8")
    return token


if __name__ == "__main__":
    from app import config
    data_dir = config.load().data_dir
    if len(sys.argv) == 3 and sys.argv[1] == "add":
        print(f"token for {sys.argv[2]} (shown once, give it to the seller):\n{add(data_dir, sys.argv[2])}")
    elif len(sys.argv) == 2 and sys.argv[1] == "list":
        print("\n".join(sorted(load(data_dir))) or "no sellers; the app is open on localhost")
    else:
        sys.exit(__doc__)
