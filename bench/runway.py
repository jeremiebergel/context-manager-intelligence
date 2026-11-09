"""Replay real sessions request by request, exactly like a live agent loop, and measure what CMI changes.

The agent sends one request after every user message and after every batch of tool results.
For each of those points the benchmark:
  1. takes the conversation prefix that was sent then
  2. lets CMI compile it (select_context) -> what would actually be sent with CMI
  3. lets CMI learn after each user turn (on_turn_complete), as the host does

Reported per session:
  quota_saved      share of input tokens NOT sent over the whole session (what a plan pays for)
  runway_x         how much more work fits in the same token budget (1 / (1 - saved))
  saturation       user turn at which the request first exceeds the compaction threshold
  cost_delta       prompt-cache-aware cost estimate (cache read 0.1x, write 1.25x)
  cache_hit        share of each request served from the previous request's prefix

Sources (see bench/sessions.py): Hermes' session store by default (the exact format the engine
gets in production), or request dumps / message-list JSON files.

    python bench/runway.py                     # 5 largest sessions in state.db
    python bench/runway.py db:<session_id>     # one session
    python bench/runway.py path/to/dump.json   # a request dump or messages list
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from typing import Any, Dict, List

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from sessions import default_specs, load, turn_prefixes  # noqa: E402

from agent.model_metadata import estimate_messages_tokens_rough as est  # noqa: E402
from plugins.context_engine import load_context_engine  # noqa: E402

WINDOWS = {"128K": 96_000, "200K": 150_000}  # compaction at 75% of the window
CACHE_READ, CACHE_WRITE = 0.10, 1.25


class _Fingerprints:
    """Message -> canonical JSON, memoized by object identity (history messages are reused
    across requests; CMI shares untouched ones), so prefix comparison stays linear."""

    def __init__(self) -> None:
        self._memo: Dict[int, tuple] = {}

    def __call__(self, m: Dict[str, Any]) -> str:
        hit = self._memo.get(id(m))
        if hit is not None and hit[0] is m:
            return hit[1]
        s = json.dumps(m, sort_keys=True, ensure_ascii=False)
        self._memo[id(m)] = (m, s)
        return s


def replay(msgs: List[Dict[str, Any]], engine=None) -> Dict[str, Any]:
    eng = engine or load_context_engine("cmi")
    eng.on_session_start("bench", hermes_home=tempfile.mkdtemp(prefix="cmi_bench_"))
    eng.update_model(model="bench", context_length=200_000)
    fp = _Fingerprints()
    A: List[int] = []
    B: List[int] = []
    turn_of: List[int] = []
    cost = {"A": 0.0, "B": 0.0}
    hits = {"A": 0, "B": 0}
    prev: Dict[str, List[str]] = {"A": [], "B": []}
    user_turn = 0
    learned = 0
    for cut in turn_prefixes(msgs):
        pre = msgs[:cut]
        if pre[-1]["role"] == "user":
            user_turn += 1
            if learned:
                eng.on_turn_complete(msgs[:learned])  # the previous turn is over: learn, like the host
            learned = cut - 1
        out = eng.select_context(pre) or pre
        a, b = est(pre), est(out)
        A.append(a)
        B.append(b)
        turn_of.append(user_turn)
        for k, req, tot in (("A", pre, a), ("B", out, b)):
            cur = [fp(m) for m in req]
            n = 0
            for x, y in zip(prev[k], cur):
                if x != y:
                    break
                n += 1
            hit = est(req[:n]) if n else 0
            hits[k] += hit
            cost[k] += hit * CACHE_READ + (tot - hit) * CACHE_WRITE
            prev[k] = cur
    saved = 1 - sum(B) / sum(A)
    res: Dict[str, Any] = {
        "messages": len(msgs), "user_turns": user_turn, "requests": len(A),
        "quota_saved": f"{100 * saved:.1f}%",
        "runway_x": round(1 / (1 - saved), 2) if saved < 1 else None,
        "cost_delta_cached": f"{100 * (cost['B'] / cost['A'] - 1):+.1f}%",
        "cache_hit": [f"{100 * hits['A'] / sum(A):.0f}%", f"{100 * hits['B'] / sum(B):.0f}%"],
    }
    for name, thr in WINDOWS.items():
        res[f"saturation_{name}"] = [next((turn_of[i] for i, v in enumerate(A) if v > thr), None),
                                     next((turn_of[i] for i, v in enumerate(B) if v > thr), None)]
    return res


def main(argv: List[str]) -> None:
    for spec in argv or default_specs(5):
        name, msgs = load(spec)
        print(json.dumps({"session": name[:40], **replay(msgs)}), flush=True)


if __name__ == "__main__":
    main(sys.argv[1:])
