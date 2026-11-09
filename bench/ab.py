"""A/B one CMI source tree against real sessions with runway.replay.

    CMI_SRC=<dir containing cmi/> python bench/ab.py db:<sid> [db:<sid> ...]
    CMI_SRC=... python bench/ab.py kanban:5      # the 5 largest kanban sessions

Loads ``cmi`` from CMI_SRC (default: this repo), never the live plugin, so two trees can be
compared on the same sessions. One JSON line per session.
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
SRC = Path(os.environ.get("CMI_SRC", HERE.parent))
sys.path.insert(0, str(SRC))
sys.path.insert(0, str(HERE))

from cmi.engine import CMIEngine  # noqa: E402
import runway  # noqa: E402
from sessions import HOME, load  # noqa: E402


def specs(argv):
    out = []
    for a in argv:
        if a.startswith("kanban:"):
            db = sqlite3.connect(f"file:{HOME / 'state.db'}?mode=ro", uri=True)
            rows = db.execute("SELECT id FROM sessions WHERE source='kanban' ORDER BY message_count DESC LIMIT ?",
                              (int(a.split(":")[1]),)).fetchall()
            out += [f"db:{r[0]}" for r in rows]
        else:
            out.append(a)
    return out


if __name__ == "__main__":
    for spec in specs(sys.argv[1:]):
        name, msgs = load(spec)
        print(json.dumps({"session": name[:40], **runway.replay(msgs, engine=CMIEngine())}), flush=True)
