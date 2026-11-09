"""v1.2 zero-loss check on real sessions: every stub's cmi:key returns an exact original,
output is deterministic, the input is untouched, and a fresh engine (process restart)
produces byte-identical stubs (prompt cache survives restarts)."""
import glob, json, os, tempfile, time
from pathlib import Path
STORE_HOME = tempfile.mkdtemp(prefix="cmi_v12_")  # engine loads from the real HERMES_HOME; originals go here
from plugins.context_engine import load_context_engine
from importlib import import_module

dumps = sorted(glob.glob(os.path.expanduser("~/.hermes/sessions/request_dump_*.json")),
               key=os.path.getsize, reverse=True)[:5]
tot_keys = tot_ok = 0
for p in dumps:
    msgs = json.load(open(p))["request"]["body"]["messages"]
    snap = json.dumps(msgs, sort_keys=True)
    def run():
        e = load_context_engine("cmi")
        e.on_session_start("bench", hermes_home=STORE_HOME)
        e.update_model(model="bench", context_length=200_000)
        e.on_turn_complete(msgs)
        t = time.perf_counter(); out = e.select_context(msgs); ms = (time.perf_counter() - t) * 1000
        return e, out, ms
    e, out, ms = run()
    out2 = e.select_context(msgs)
    _, out3, _ = run()                      # fresh engine = process restart
    KEY_RE = import_module(type(e).__module__.rsplit(".", 1)[0] + ".store").KEY_RE
    originals = set()
    for m in msgs:
        originals.add(e._content_text(m))
        for part in (m.get("content") if isinstance(m.get("content"), list) else []):
            if isinstance(part, dict) and part.get("type") == "tool_use":
                originals.update(v for v in (part.get("input") or {}).values() if isinstance(v, str))
        for tc in m.get("tool_calls") or []:
            try:
                originals.update(v for v in json.loads(tc["function"]["arguments"]).values() if isinstance(v, str))
            except Exception:
                pass
    keys = sorted(set(KEY_RE.findall(json.dumps(out, ensure_ascii=False))))
    ok = sum(1 for k in keys if e.handle_tool_call("cmi_recall", {"key": "cmi:" + k}) in originals)
    tot_keys += len(keys); tot_ok += ok
    print(json.dumps({"session": os.path.basename(p)[13:35], "msgs": len(msgs), "stub_keys": len(keys),
                      "recall_exact": ok, "deterministic": json.dumps(out) == json.dumps(out2),
                      "same_after_restart": json.dumps(out) == json.dumps(out3),
                      "input_untouched": json.dumps(msgs, sort_keys=True) == snap, "ms": round(ms)}), flush=True)
store = Path(STORE_HOME) / "cmi_store"
size = sum(f.stat().st_size for f in store.glob("*.txt"))
print(f"TOTAL keys={tot_keys} recall_exact={tot_ok} store_files={len(list(store.glob('*.txt')))} store_MB={size/1e6:.1f}")
