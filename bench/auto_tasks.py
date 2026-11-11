"""Repeated automatic tasks (cron / kanban / oneshot): what would each lever save?

Takes REAL short sessions from state.db (per-request context growth measured with the engine's own
token estimator), adds the REAL fixed prefix (system + tool schemas, measured from request dumps), and
replays each task N times at a fixed interval against a provider-cache model with TTL expiry.
No LLM call, no network, state.db read-only, message content never printed.

    python bench/auto_tasks.py [--runs 30] [--max-calls 40] [--prefix 19300] [--tools 14900] [--skills 2600]

Levers (all applied on top of the same session shapes, so they are comparable):
  base        prefix as sent today
  skills      skills index replaced by a compact one (-skills_saved tokens)
  tools       per-job tool allowlist (keep --tools-keep of the tool-schema tokens)
  skip        runs avoided when nothing changed (--skip fraction; needs an explicit state test)
Costs are in input-token equivalents (cache read 0.1, write 1.25 for 5 min). Levers 'skills' and 'tools'
change the prefix, so their first run after the change rewrites the cache: it is paid in the totals.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE))
sys.path.insert(1, str(Path.home() / ".hermes" / "hermes-agent"))

from cmi.compat import estimate_messages_tokens_rough as _est  # noqa: E402
from sessions import _db, load_db_session, turn_prefixes  # noqa: E402

READ, WRITE = 0.1, 1.25
TTL = 300.0


def shape(sid):
    """Per-request new-token increments of one real session (first = initial user message)."""
    msgs, _ = load_db_session(sid, with_ts=True)
    if not msgs:
        return None
    cuts = turn_prefixes(msgs)
    sizes = [_est(msgs[:c]) for c in cuts]
    return [sizes[0]] + [max(b - a, 0) for a, b in zip(sizes, sizes[1:])]


def run_cost(prefix, steps, cached_prefix):
    """One run: cached_prefix tokens of the prefix are read, the rest written; history is read, new step written."""
    c = (cached_prefix * READ + (prefix - cached_prefix) * WRITE) + steps[0] * WRITE
    hist = steps[0]
    for s in steps[1:]:
        c += prefix * READ + hist * READ + s * WRITE
        hist += s
    return c


def job_cost(steps, prefix, gap_s, runs, skip=0.0, first_rewrite=0):
    # Skipped runs still elapse time: the cache is warm only if the gap since the last EXECUTED run <= TTL.
    total, warm, since, acc = 0.0, False, 0.0, 0.0
    for r in range(runs):
        since += gap_s
        acc += skip  # deterministic: skip a run each time the accumulator crosses 1
        if acc >= 1.0:
            acc -= 1.0
            continue
        cached = prefix if (warm and since <= TTL) else 0
        if r == 0 and first_rewrite:
            cached = 0
        total += run_cost(prefix, steps, cached)
        warm, since = True, 0.0
    return total


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--runs", type=int, default=30)
    ap.add_argument("--max-calls", type=int, default=40)
    ap.add_argument("--prefix", type=int, default=19300)
    ap.add_argument("--tools", type=int, default=14900)
    ap.add_argument("--skills", type=int, default=2600)
    ap.add_argument("--skills-saved", type=int, default=1800)
    ap.add_argument("--tools-keep", type=float, default=0.4)
    ap.add_argument("--skip", type=float, default=0.5)
    ap.add_argument("--sources", default="cron,oneshot,kanban,subagent")
    a = ap.parse_args()
    db = _db()
    ph = ",".join("?" * len(a.sources.split(",")))
    sids = [r[0] for r in db.execute(
        f"SELECT id FROM sessions WHERE source IN ({ph}) AND api_call_count BETWEEN 2 AND ? ORDER BY started_at",
        (*a.sources.split(","), a.max_calls))]
    shapes = [s for s in (shape(i) for i in sids) if s and len(s) > 1]
    print(f"{len(shapes)} real short sessions, prefix {a.prefix} tok (tools {a.tools}, skills idx {a.skills})")
    p_skills = a.prefix - a.skills_saved
    p_tools = a.prefix - int(a.tools * (1 - a.tools_keep))
    p_both = p_tools - a.skills_saved
    print(f"{'interval':>10} {'base':>6} {'skills':>7} {'tools':>7} {'both':>7} {'both+skip':>10}   (% of base, {a.runs} runs/job)")
    for gap, label in [(120, "2 min"), (290, "<5 min"), (1800, "30 min"), (21600, "6 h"), (86400, "1 day")]:
        base = sum(job_cost(s, a.prefix, gap, a.runs) for s in shapes)
        def pct(prefix, skip=0.0):
            # first run after the prefix change pays one cold rewrite: counted by job_cost (warm=False on run 0)
            return 100 * sum(job_cost(s, prefix, gap, a.runs, skip) for s in shapes) / base
        print(f"{label:>10} {100:5.0f}% {pct(p_skills):6.1f}% {pct(p_tools):6.1f}% {pct(p_both):6.1f}% {pct(p_both, a.skip):9.1f}%")
    # share of the fixed prefix in a typical run (why short sessions can't be saved from inside)
    fixed = sum(a.prefix * READ * (len(s) - 1) + a.prefix * WRITE for s in shapes)
    var = sum(sum(s) * WRITE + sum(sum(s[: i + 1]) * READ for i in range(len(s) - 1)) for s in shapes)
    print(f"cold daily run: fixed prefix = {100 * fixed / (fixed + var):.0f}% of the cost, conversation = {100 * var / (fixed + var):.0f}%")


if __name__ == "__main__":
    main()
