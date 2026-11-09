"""One-shot backfill: index the tool outputs already in Hermes' session DB (state.db).

Run with the Hermes venv:  python -m cmi.backfill [--days 30]
Idempotent (content-addressed keys, INSERT OR IGNORE). Read-only on state.db.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import time
from pathlib import Path

from .index import MIN_CHARS, OutputIndex
from .store import RecallStore


def backfill(home: Path, days: int = 30) -> dict:
    from .engine import _call_label
    store = RecallStore(home / "cmi_store")
    index = OutputIndex(home / "cmi_index.db")
    db = sqlite3.connect(f"file:{home / 'state.db'}?mode=ro", uri=True, timeout=30)
    since = time.time() - days * 86400
    calls = {}
    for sid, tcs in db.execute(
            "SELECT session_id, tool_calls FROM messages WHERE role='assistant' AND tool_calls IS NOT NULL "
            "AND timestamp >= ?", (since,)):
        try:
            for tc in json.loads(tcs) or []:
                fn = (tc or {}).get("function") or {}
                calls[(sid, str(tc.get("id") or ""))] = (fn.get("name") or "", fn.get("arguments") or "")
        except (ValueError, TypeError, AttributeError):
            continue
    seen = added = 0
    for sid, tcid, name, content, ts in db.execute(
            "SELECT session_id, tool_call_id, tool_name, content, timestamp FROM messages "
            "WHERE role='tool' AND timestamp >= ? AND length(content) >= ?", (since, MIN_CHARS)):
        if not content or content.startswith("[CMI"):
            continue
        seen += 1
        cname, cargs = calls.get((sid, str(tcid or "")), (name or "", ""))
        key = store.put(content)
        if key and index.add(key, content, session=sid, tool=cname, label=_call_label(cname, cargs), ts=ts):
            added += 1
    return {"outputs_seen": seen, "newly_indexed": added}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=30)
    a = ap.parse_args()
    from hermes_constants import get_hermes_home
    print(json.dumps(backfill(Path(get_hermes_home()), a.days)))


if __name__ == "__main__":
    main()
