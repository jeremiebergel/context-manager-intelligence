"""Offline cost replay: feed stored real sessions request by request through CMIEngine.select_context
and the engine's own CostShadow, and attribute every cached-prefix rewrite to the mechanism that
caused it.

    CMI_SRC=<dir containing cmi/> python bench/cost_replay.py db:<sid> [...] [--sample N] [--out f.jsonl]

Run with the Hermes venv (pricing comes from agent.usage_pricing, like production):
    cd ~/.hermes/hermes-agent && venv/bin/python <repo>/bench/cost_replay.py ...

No LLM call, no network. Reads state.db read-only; writes only to a temporary HERMES_HOME. Message
content is never printed: only token counts and mechanism labels.

Per session (JSON line):
  cost_raw / cost_cmi   shadow cost in input-token equivalents (cache read/write ratios of the model)
  delta                 cost_cmi / cost_raw - 1
  breaks                prefix rewrites of the CMI stream: count, rewritten tokens, rewrite premium cost,
                        tokens removed by the change and its realised saving over the rest of the session
                        (removed x read x remaining requests), grouped by trigger mechanism
"""
from __future__ import annotations

import argparse
import collections
import json
import os
import sqlite3
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
SRC = Path(os.environ.get("CMI_SRC", HERE.parent))
sys.path.insert(0, str(SRC))
sys.path.insert(0, str(HERE))
sys.path.insert(1, str(Path.home() / ".hermes" / "hermes-agent"))

from cmi.engine import CMIEngine, _fingerprint  # noqa: E402
from cmi.compat import estimate_messages_tokens_rough as _est  # noqa: E402
from sessions import HOME, load, load_db_session, turn_prefixes  # noqa: E402

TTL = float(os.environ.get("CMI_CACHE_TTL", 300))


class Stream:
    """Independent shadow (same model as cmi.cost_guard, plus the provider cache TTL: after a gap
    longer than TTL nothing is cached, for the raw stream as much as for the CMI one). Identical
    for every engine version replayed, so versions are compared on the same yardstick."""

    def __init__(self, read, write):
        self.read, self.write, self.prev, self.cost = read, write, [], 0.0

    def observe(self, fps, toks, expired):
        h = 0
        if not expired:
            for a, b in zip(self.prev, fps):
                if a != b:
                    break
                h += 1
        c = sum(toks[:h]) * self.read + sum(toks[h:]) * self.write
        self.prev = list(fps)
        self.cost += c
        return c


def _kind(m) -> str:
    c = m.get("content")
    s = c if isinstance(c, str) else json.dumps(c)[:200] if c is not None else ""
    if s.startswith("[CMI digest]"):
        return "digest"
    if s.startswith("[CMI one_liner]"):
        return "one_liner"
    if "[CMI" in s[:400]:
        return "cmi_other"
    if m.get("tool_calls") and "[CMI" in json.dumps(m.get("tool_calls"))[:100000]:
        return "args"
    return "raw"


def replay(msgs, model="", provider="", detail=False, ts=None):
    eng = CMIEngine(auto_project_binding=False)
    now = [0.0]
    if hasattr(eng, "_clock"):
        eng._clock = lambda: now[0]
    home = tempfile.mkdtemp(prefix="cmi_cost_replay_", dir=os.environ.get("TMPDIR"))
    eng.on_session_start("replay", hermes_home=home)
    eng.update_model(model=model or "claude-opus-5-5", context_length=1_000_000, provider=provider or "anthropic")
    cuts = turn_prefixes(msgs)
    total = len(cuts)
    rd, wr = eng.cost.read, eng.cost.write
    sraw, scmi = Stream(rd, wr), Stream(rd, wr)
    over = 0
    worst_req = 0.0
    expired_n = 0
    last_t = None
    prev_fps, prev_toks, prev_kinds = [], [], []
    learned = 0
    mech = collections.defaultdict(collections.Counter)
    events = []
    for r, cut in enumerate(cuts):
        pre = msgs[:cut]
        if pre[-1]["role"] == "user":
            if learned:
                eng.on_turn_complete(msgs[:learned])
            learned = cut - 1
        t = ts[cut - 1] if ts else 0.0
        now[0] = t
        expired = last_t is not None and t - last_t > TTL
        expired_n += expired
        last_t = t
        tiers0 = eng.telemetry.get("tiers", 0)
        out = eng.select_context(pre) or pre
        fps = [_fingerprint(m) for m in out]
        toks = [_est([m]) for m in out]
        rc = sraw.observe([_fingerprint(m) for m in pre], [_est([m]) for m in pre], expired)
        cc = scmi.observe(fps, toks, expired)
        if cc > rc * 1.000001 + 1:
            over += 1
            worst_req = max(worst_req, cc / rc - 1 if rc else 0)
        if expired:
            prev_fps = []  # the whole prefix is rewritten anyway: not a CMI-caused break
        k = next((i for i, (a, b) in enumerate(zip(prev_fps, fps)) if a != b), None)
        if k is not None and k < len(prev_fps):
            rewrite = sum(toks[k:len(prev_fps)])
            removed = sum(prev_toks[i] - toks[i] for i in range(k, min(len(prev_fps), len(fps))) if prev_fps[i] != fps[i])
            tiered = eng.telemetry.get("tiers", 0) > tiers0
            at_turn = getattr(eng, "_user_idx", -1) == len(pre) - 1
            if tiered:
                trig = "tier@turn" if at_turn else "tier@epoch"
            elif k in eng._must_refresh:
                trig = "refresh_superseded" if k in eng._superseded_now else "refresh_hot"
            else:
                trig = "other"
            change = f"{prev_kinds[k]}->{_kind(out[k])}"
            remaining = total - r - 1
            prem = rewrite * (wr - rd)
            gain = max(removed, 0) * rd * remaining
            c = mech[trig]
            c["n"] += 1
            c["rewrite_tok"] += rewrite
            c["premium"] += prem
            c["removed_tok"] += removed
            c["gain"] += gain
            c["net_negative"] += gain < prem
            mech[f"change:{change}"]["n"] += 1
            if detail:
                events.append({"r": r, "trig": trig, "k": k, "len": len(prev_fps), "rewrite": rewrite,
                               "removed": removed, "remaining": remaining, "prem": round(prem),
                               "gain": round(gain), "change": change})
        prev_fps, prev_toks, prev_kinds = fps, toks, [_kind(m) for m in out]
    res = {"requests": total, "messages": len(msgs), "read": rd, "write": wr,
           "cost_raw": round(sraw.cost), "cost_cmi": round(scmi.cost),
           "delta": round(scmi.cost / sraw.cost - 1, 4) if sraw.cost else 0.0,
           "ttl_expired": expired_n, "req_over_raw": over, "worst_req_over": round(worst_req, 3),
           "engine_shadow_delta": round(eng.cost.cmi.cost / eng.cost.raw.cost - 1, 4) if eng.cost.raw.cost else 0.0,
           "tok_raw": eng.cost.raw.tokens, "tok_cmi": eng.cost.cmi.tokens,
           "ctx_saved": round(1 - eng.cost.cmi.tokens / eng.cost.raw.tokens, 4) if eng.cost.raw.tokens else 0.0,
           "tiers": eng.telemetry.get("tiers", 0),
           "breaks": {k: dict(v) for k, v in mech.items()}}
    for k, v in eng.telemetry.items():
        if k.startswith("cost_guard_"):
            res[k] = v
    if detail:
        res["events"] = events
    return res


def _ledger():
    S = collections.defaultdict(collections.Counter)
    M = {}
    for line in open(HOME / "cmi_stats.jsonl"):
        d = json.loads(line)
        c = S[d["s"]]
        for f in ("n", "cr", "cc"):
            c[f] += d.get(f, 0) or 0
        M[d["s"]] = (d.get("m") or "", d.get("p") or "")
    return S, M


def sample(n_per_cell: int):
    """Stratified sample of ledger sessions still in state.db: length bucket x provider, the
    largest-cost sessions of each cell (deterministic)."""
    S, M = _ledger()
    db = sqlite3.connect(f"file:{HOME / 'state.db'}?mode=ro", uri=True)
    have = {r[0] for r in db.execute("SELECT DISTINCT session_id FROM messages")}
    cells = collections.defaultdict(list)
    for s, c in S.items():
        if not c["cr"] or s not in have:
            continue
        n = c["n"]
        b = "1-10" if n <= 10 else "11-30" if n <= 30 else "31-80" if n <= 80 else "81+"
        cells[(b, M[s][1])].append((c["cr"], s))
    out = []
    for key in sorted(cells):
        out += [s for _, s in sorted(cells[key], reverse=True)[:n_per_cell]]
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("specs", nargs="*")
    ap.add_argument("--sample", type=int, default=0, help="per (length bucket x provider) cell")
    ap.add_argument("--out")
    ap.add_argument("--detail", action="store_true")
    a = ap.parse_args()
    _, M = _ledger()
    specs = list(a.specs) + [f"db:{s}" for s in (sample(a.sample) if a.sample else [])]
    seen = set()
    fh = open(a.out, "a") if a.out else None
    for spec in specs:
        if spec in seen:
            continue
        seen.add(spec)
        sid = spec[3:] if spec.startswith("db:") else spec
        model, prov = M.get(sid, ("", ""))
        ts = None
        if spec.startswith("db:"):
            msgs, ts = load_db_session(sid, with_ts=True)
        else:
            _, msgs = load(spec)
        if not msgs:
            continue
        res = {"session": sid, "model": model, "provider": prov, **replay(msgs, model, prov, a.detail, ts)}
        line = json.dumps(res)
        print(line, flush=True)
        if fh:
            fh.write(line + "\n")
            fh.flush()


if __name__ == "__main__":
    main()
