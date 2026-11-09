"""Cross-session index of tool outputs: ``$HERMES_HOME/cmi_index.db`` (SQLite FTS5).

Hermes' own session search indexes user and assistant text only; tool outputs (file reads,
command logs, API responses: most of what a session actually learned) are invisible to it. Every
tool output worth keeping is stored verbatim in the recall store and indexed here with where it
came from, so ``cmi_search`` can find the exact output of a past session and ``cmi_recall`` can
return it word for word.

Two FTS tables: ``unicode61`` for words (ranked with bm25) and ``trigram`` for identifiers and
fragments (``WEBHOOK_MAX``, ``p99``, ``/checkout``) that the word tokenizer splits apart.
WAL + busy timeout: desktop, gateway and cron write concurrently.
"""
from __future__ import annotations

from . import _fs

import re
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from .secrets import contains_secret, guard_enabled

MIN_CHARS = 400          # below this, the output is already cheap to keep in context
MAX_INDEXED_CHARS = 200_000
_SCHEMA = """
CREATE TABLE IF NOT EXISTS docs (
    key TEXT PRIMARY KEY, session TEXT, tool TEXT, label TEXT, ts REAL, chars INTEGER);
CREATE VIRTUAL TABLE IF NOT EXISTS docs_fts USING fts5(key UNINDEXED, body, tokenize='unicode61');
CREATE VIRTUAL TABLE IF NOT EXISTS docs_tri USING fts5(key UNINDEXED, body, tokenize='trigram');
CREATE INDEX IF NOT EXISTS docs_ts ON docs(ts);
"""
_WORD_RE = re.compile(r"[\w./:-]{2,}", re.UNICODE)


class OutputIndex:
    def __init__(self, path: Optional[Path] = None) -> None:
        self.path = path
        self._local = threading.local()

    def set_path(self, path: Path) -> None:
        if path != self.path:
            self.path = path
            self._local = threading.local()

    def _db(self) -> Optional[sqlite3.Connection]:
        if self.path is None:
            return None
        con = getattr(self._local, "con", None)
        if con is None:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            if self.path.is_symlink():
                return None
            con = sqlite3.connect(str(self.path), timeout=10)
            con.execute("PRAGMA journal_mode=WAL")
            con.execute("PRAGMA synchronous=NORMAL")
            con.executescript(_SCHEMA)
            _fs.chmod_private(self.path, 0o600)
            self._local.con = con
        return con

    def has(self, key: str) -> bool:
        con = self._db()
        return bool(con and con.execute("SELECT 1 FROM docs WHERE key=?", (key,)).fetchone())

    def add(self, key: str, content: str, *, session: str = "", tool: str = "", label: str = "",
            ts: Optional[float] = None) -> bool:
        con = self._db()
        if con is None or not key or not content:
            return False
        if guard_enabled() and (contains_secret(label) or contains_secret(content)):
            return False  # the command line itself can carry a token (curl -H, export ...)
        body = content[:MAX_INDEXED_CHARS]
        try:
            with con:
                cur = con.execute("INSERT OR IGNORE INTO docs VALUES (?,?,?,?,?,?)",
                                  (key, session, tool, label[:300], ts or time.time(), len(content)))
                if cur.rowcount:
                    # The call that produced the output is searchable too ("the bench from last week").
                    text = f"{label}\n{body}"
                    con.execute("INSERT INTO docs_fts(key, body) VALUES (?,?)", (key, text))
                    con.execute("INSERT INTO docs_tri(key, body) VALUES (?,?)", (key, text))
            return bool(cur.rowcount)
        except sqlite3.Error:
            return False

    def forget(self, keys) -> None:
        con = self._db()
        if con is None:
            return
        keys = list(keys)
        with con:
            for k in keys:
                con.execute("DELETE FROM docs WHERE key=?", (k,))
                con.execute("DELETE FROM docs_fts WHERE key=?", (k,))
                con.execute("DELETE FROM docs_tri WHERE key=?", (k,))

    def purge_secrets(self) -> int:
        """Forget rows whose label or text holds a credential (indexes written before the guard)."""
        con = self._db()
        if con is None:
            return 0
        bad = [k for k, b in con.execute("SELECT key, body FROM docs_fts") if contains_secret(b)]
        if bad:
            self.forget(bad)
            self.compact()
        return len(bad)

    def compact(self) -> None:
        """Rewrite the FTS segments and the file so deleted rows leave no bytes behind."""
        con = self._db()
        if con is None:
            return
        try:
            for t in ("docs_fts", "docs_tri"):
                con.execute(f"INSERT INTO {t}({t}) VALUES('optimize')")
            con.commit()
            con.execute("VACUUM")
            con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        except sqlite3.Error:
            pass

    def search(self, query: str, *, limit: int = 5, exclude_session: str = "",
               exists=None) -> List[Dict[str, Any]]:
        """Best matches, newest first among equals. ``exists(key)`` drops (and forgets) rows whose
        original was pruned from the store, so a hit is always recallable."""
        con = self._db()
        if con is None:
            return []
        terms = [t for t in _WORD_RE.findall(query or "")][:12]
        if not terms:
            return []
        rows: Dict[str, float] = {}
        quoted = [f'"{t.replace(chr(34), "")}"' for t in terms]
        attempts = [("docs_fts", " AND ".join(quoted)), ("docs_fts", " OR ".join(quoted))]
        tri = [q for t, q in zip(terms, quoted) if len(t) >= 3]
        if tri:
            attempts.insert(1, ("docs_tri", " AND ".join(tri)))
        for table, match in attempts:
            try:
                found = con.execute(
                    f"SELECT key, bm25({table}) FROM {table} WHERE {table} MATCH ? ORDER BY bm25({table}) LIMIT ?",
                    (match, limit * 6)).fetchall()
            except sqlite3.Error:
                continue
            for k, score in found:
                rows.setdefault(k, score)
            if len(rows) >= limit:
                break
        out, dead = [], []
        for key, score in sorted(rows.items(), key=lambda kv: kv[1]):
            meta = con.execute("SELECT session, tool, label, ts, chars FROM docs WHERE key=?", (key,)).fetchone()
            if not meta or (exclude_session and meta[0] == exclude_session):
                continue
            if exists is not None and not exists(key):
                dead.append(key)
                continue
            body = con.execute("SELECT body FROM docs_fts WHERE key=?", (key,)).fetchone()
            out.append({"key": key, "session": meta[0], "tool": meta[1], "label": meta[2],
                        "ts": meta[3], "chars": meta[4], "snippet": _snippet(body[0] if body else "", terms)})
            if len(out) >= limit:
                break
        if dead:
            self.forget(dead)
        return out


def _snippet(body: str, terms: List[str], width: int = 240) -> str:
    """The passage holding the most distinct query terms (then the rarest ones), so the snippet
    shows the answer, not the first incidental match."""
    low = body.lower()
    terms = [t.lower() for t in terms if t]
    positions = [(p, t) for t in terms for p in _find_all(low, t)]
    if not positions:
        start = 0
    else:
        freq = {t: max(1, low.count(t)) for t in terms}
        best, start = -1.0, 0
        for p, _ in positions:
            window = low[max(0, p - width // 3): p + width]
            score = sum(1 / freq[t] + 10 for t in terms if t in window)
            if score > best:
                best, start = score, max(0, p - width // 3)
    text = " ".join(body[start:start + width].split())
    return ("…" if start else "") + text + ("…" if start + width < len(body) else "")


def _find_all(text: str, term: str, cap: int = 200):
    i, n = text.find(term), 0
    while i >= 0 and n < cap:
        yield i
        i, n = text.find(term, i + 1), n + 1
