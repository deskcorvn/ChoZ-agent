"""Append-only, hash-chained audit log of the decisions that matter: what the check decided, what was published and
on whose confirmation, what was refused, what the seller deleted.

Each line is JSON with the previous line's hash and its own: sha256(prev_hash + the line without "hash"). Editing,
deleting or reordering a line breaks the chain from that line on, and `python -m app.audit verify` says where.
It lives in data/audit/, outside the run folders, so the 7-day purge and a seller's delete do not erase the record that
something happened; it holds ids, verdicts, rule ids and guard names only, never the seller's words, photos or numbers.

    python -m app.audit verify [data/audit/audit.jsonl]
"""
from __future__ import annotations

import hashlib
import json
import sys
import threading
import time
from pathlib import Path

GENESIS = "0" * 64
_lock = threading.Lock()


def _digest(prev: str, record: dict) -> str:
    body = json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256((prev + body).encode("utf-8")).hexdigest()


def _last(path: Path) -> tuple[int, str]:
    if not path.is_file() or path.stat().st_size == 0:
        return 0, GENESIS
    with path.open("rb") as f:
        f.seek(max(0, path.stat().st_size - 8192))
        line = f.read().decode("utf-8", "replace").strip().splitlines()[-1]
    rec = json.loads(line)
    return rec["seq"], rec["hash"]


def append(path: Path, event: str, run_id: str, **fields) -> dict:
    """Add one event. Fields must not contain seller content; callers pass ids, verdicts, rule ids and counts."""
    with _lock:
        path.parent.mkdir(parents=True, exist_ok=True)
        seq, prev = _last(path)
        record = {"seq": seq + 1, "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "event": event, "run_id": run_id,
                  **fields, "prev": prev}
        record["hash"] = _digest(prev, record)
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
        return record


def verify(path: Path) -> tuple[bool, int, str]:
    """(ok, lines checked, problem). The first line that does not chain is reported."""
    prev, n = GENESIS, 0
    if not path.is_file():
        return True, 0, "no audit log yet"
    for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            return False, n, f"line {n} is not JSON"
        claimed = rec.pop("hash", None)
        if rec.get("prev") != prev:
            return False, n, f"line {n} does not follow line {n - 1} (removed, reordered or inserted)"
        if rec.get("seq") != n:
            return False, n, f"line {n} has seq {rec.get('seq')}"
        if _digest(prev, rec) != claimed:
            return False, n, f"line {n} was edited after it was written"
        prev = claimed
    return True, n, "chain intact"


def head(path: Path) -> dict:
    """What to copy off the machine (a chat message, a commit, a daily e-mail): if the chain is later rewritten, the
    copied head no longer appears in it."""
    ok, n, why = verify(path)
    return {"events": n, "head_hash": _last(path)[1] if n else GENESIS, "chain": why if ok else f"BROKEN: {why}"}


def path_for(data_dir: Path) -> Path:
    return data_dir / "audit" / "audit.jsonl"


if __name__ == "__main__":
    if len(sys.argv) < 2 or sys.argv[1] != "verify":
        sys.exit(__doc__)
    from app import config
    target = Path(sys.argv[2]) if len(sys.argv) > 2 else path_for(config.load().data_dir)
    ok, n, why = verify(target)
    print(f"{target}: {n} events, {why}")
    sys.exit(0 if ok else 1)
