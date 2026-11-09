"""Shared loaders for the benchmarks.

Two sources of real conversations:
- ``db:<session_id>``: Hermes' own session store (``state.db``), in the OpenAI message format the
  context engine receives in production. This is the reference source.
- a request dump JSON (``request_dump_*.json``) or any file holding a messages list.
"""
from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path
from typing import Any, Dict, List, Tuple

HOME = Path(os.environ.get("HERMES_HOME", Path.home() / ".hermes"))


def _db() -> sqlite3.Connection:
    return sqlite3.connect(f"file:{HOME / 'state.db'}?mode=ro", uri=True)


def largest_db_sessions(n: int = 5, min_messages: int = 300) -> List[str]:
    rows = _db().execute(
        """SELECT session_id FROM messages WHERE session_id NOT LIKE 'cron_%'
           GROUP BY session_id HAVING COUNT(*) >= ? ORDER BY SUM(LENGTH(content)) DESC LIMIT ?""",
        (min_messages, n)).fetchall()
    return [r[0] for r in rows]


def load_db_session(session_id: str, with_ts: bool = False):
    """The session as it happened: original rows only. Compaction archives rows (compacted=1)
    and re-inserts copies of the kept tail, and rewinds deactivate rows (active=0, compacted=0);
    keeping only the first row per id-bearing message rebuilds the real timeline."""
    msgs: List[Dict[str, Any]] = []
    seen_calls: set = set()
    seen_results: set = set()
    ts: List[float] = []
    for role, content, tool_calls, tool_call_id, active, compacted, t in _db().execute(
            "SELECT role, content, tool_calls, tool_call_id, active, compacted, timestamp FROM messages "
            "WHERE session_id=? ORDER BY id", (session_id,)):
        if not active and not compacted:
            continue  # rewound / taken back by the user
        if tool_call_id:
            if tool_call_id in seen_results:
                continue
            seen_results.add(tool_call_id)
        if tool_calls:
            try:
                ids = tuple(tc.get("id") for tc in json.loads(tool_calls))
            except (ValueError, AttributeError):
                ids = ()
            if ids and all(i in seen_calls for i in ids):
                continue
            seen_calls.update(ids)
        if role not in ("user", "assistant", "tool", "system"):
            continue
        m: Dict[str, Any] = {"role": role, "content": content or ""}
        if isinstance(content, str) and content.startswith("[{"):
            try:
                parsed = json.loads(content)
                if isinstance(parsed, list):
                    m["content"] = parsed
            except ValueError:
                pass
        if tool_calls:
            try:
                m["tool_calls"] = json.loads(tool_calls)
            except ValueError:
                pass
        if tool_call_id:
            m["tool_call_id"] = tool_call_id
        msgs.append(m)
        ts.append(float(t or 0))
    if with_ts:
        # Timestamps ride in a parallel list (never in the messages: fingerprints must not change).
        ids = {tc.get("id") for m in msgs for tc in (m.get("tool_calls") or []) if isinstance(tc, dict)}
        keep = [m["role"] != "tool" or m.get("tool_call_id") in ids for m in msgs]
        return [m for m, k in zip(msgs, keep) if k], [t for t, k in zip(ts, keep) if k]
    return _repair_pairs(msgs)


def _repair_pairs(msgs: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Drop tool results whose call is absent (interrupted turns), like Hermes' sanitizer does."""
    ids = {tc.get("id") for m in msgs for tc in (m.get("tool_calls") or []) if isinstance(tc, dict)}
    return [m for m in msgs if m["role"] != "tool" or m.get("tool_call_id") in ids]


def load(spec: str) -> Tuple[str, List[Dict[str, Any]]]:
    if spec.startswith("db:"):
        sid = spec[3:]
        return sid, load_db_session(sid)
    data = json.load(open(spec))
    msgs = data["request"]["body"]["messages"] if isinstance(data, dict) and "request" in data else data
    return Path(spec).stem, msgs


def default_specs(n: int = 5) -> List[str]:
    return [f"db:{s}" for s in largest_db_sessions(n)]


def turn_prefixes(msgs: List[Dict[str, Any]]) -> List[int]:
    """Indices where a request is sent: after each real user message and after each tool batch."""
    cuts = []
    for i, m in enumerate(msgs):
        nxt = msgs[i + 1] if i + 1 < len(msgs) else None
        if m["role"] == "user" or (m["role"] == "tool" and (nxt is None or nxt["role"] != "tool")):
            cuts.append(i + 1)
    return cuts
