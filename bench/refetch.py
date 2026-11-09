"""How often does an agent go back for something it already had?

In real sessions, find every tool call that repeats an earlier call's exact (tool, arguments)
pair with a result that was already in context, i.e. the agent re-fetched it. With CMI those
earlier results may have been digested; this measures, per session:

  refetch        repeated calls whose earlier result was identical (pure re-fetch)
  digested_then  of those, how many had the earlier result digested by CMI at that moment
  prefetch_hit   of those, how many CMI's anticipatory recall would have restored beforehand

That last number is the one that matters for "zero loss": a digested result the agent needed
again must come back without the agent having to ask.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from typing import Any, Dict, List

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from sessions import default_specs, load, turn_prefixes  # noqa: E402

from plugins.context_engine import load_context_engine  # noqa: E402


def _calls(msgs: List[Dict[str, Any]]):
    for i, m in enumerate(msgs):
        for tc in m.get("tool_calls") or []:
            fn = tc.get("function") or {}
            yield i, tc.get("id"), fn.get("name"), fn.get("arguments") or ""


def run(msgs: List[Dict[str, Any]]) -> Dict[str, Any]:
    results = {m.get("tool_call_id"): (i, m.get("content")) for i, m in enumerate(msgs) if m["role"] == "tool"}
    first: Dict[tuple, tuple] = {}
    repeats = []
    for i, tc_id, name, args in _calls(msgs):
        key = (name, args)
        res = results.get(tc_id)
        if key in first and res is not None:
            fi, fcontent = first[key]
            if res[1] == fcontent and isinstance(fcontent, str) and len(fcontent) > 300:
                repeats.append((i, fi))
        elif res is not None:
            first[key] = (res[0], res[1])
    if not repeats:
        return {"refetch": 0, "digested_then": 0, "prefetch_hit": 0}
    eng = load_context_engine("cmi")
    eng.on_session_start("refetch", hermes_home=tempfile.mkdtemp(prefix="cmi_refetch_"))
    eng.update_model(model="bench", context_length=200_000)
    # The request that produced the repeated call is the last prefix ending at or before it.
    cuts = turn_prefixes(msgs)
    by_call = {}
    for call_i, first_res_i in repeats:
        cut = max((c for c in cuts if c <= call_i), default=None)
        if cut is not None and first_res_i < cut:
            by_call.setdefault(cut, []).append(first_res_i)
    digested = hit = 0
    learned = 0
    for cut in cuts:
        pre = msgs[:cut]
        if pre[-1]["role"] == "user":
            if learned:
                eng.on_turn_complete(msgs[:learned])
            learned = cut - 1
        targets = by_call.get(cut)
        if not targets:
            continue
        out = eng.select_context(pre) or pre
        for fi in targets:
            if out[fi]["content"] != pre[fi]["content"]:
                digested += 1
            elif getattr(eng, "_last_prefetched", None) and fi in eng._last_prefetched:
                hit += 1
    return {"refetch": len(repeats), "digested_then": digested, "prefetch_hit": hit}


def main(argv: List[str]) -> None:
    for spec in argv or default_specs(5):
        name, msgs = load(spec)
        print(json.dumps({"session": name[:40], **run(msgs)}), flush=True)


if __name__ == "__main__":
    main(sys.argv[1:])
