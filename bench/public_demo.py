"""Reproducible public benchmark: a synthetic coding session, no network, no API key, no private data.

The session is generated deterministically (fixed content, no randomness): an agent works on a
small project over 4 user requests, reading files, grepping, running tests, editing and re-reading.
Every request the agent would send (after each user message and each tool result) goes through the
proxy compile path, exactly as `cmi-proxy` does it. Reported:
  chars_sent / chars_original   over the whole session (what a token plan pays for, roughly)
  quota_saved, runway_x         share NOT sent and 1 / (1 - saved)
Checked (the run fails if any is false):
  pairs_intact      every tool_use id still has its tool_result in every compiled request
  recall_exact      every stub's original (file path named in the stub) equals a real tool result
  deterministic     a second run in a fresh store produces byte-identical requests
This is synthetic: it shows the mechanism and its invariants, not the savings of your own
sessions. Use bench/runway.py on your own request dumps for that.

    python bench/public_demo.py            # summary
    python bench/public_demo.py --json     # machine-readable
"""
from __future__ import annotations

import json
import re
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from cmi import engine as _engine  # noqa: E402
from cmi import proxy  # noqa: E402

# This synthetic session has 30 requests: the 100-request start-up phase (no cache rewrite on a warm
# cache) would leave it untouched, so the demo switches it off to show the mechanics.
_engine.WARMUP_REQUESTS = 0

ROUTE = "/v1/messages"


def source(name: str, n: int) -> str:
    return "".join(f"def {name}_{i}(x):\n    return x * {i} + {len(name)}  # {name} helper {i}\n"
                   for i in range(n))


def grep(term: str, n: int) -> str:
    return "".join(f"src/mod_{i % 9}.py:{10 + i}: {term} = compute_{term}({i})\n" for i in range(n))


def tests(failing: int, n: int = 120) -> str:
    out = "".join(f"tests/test_mod.py::test_case_{i} {'FAILED' if i < failing else 'PASSED'}\n"
                  for i in range(n))
    return out + f"=== {failing} failed, {n - failing} passed ===\n"


# (user message, [(tool, input, result), ...]) — 4 user requests, 26 tool calls
SCRIPT = [
    ("The tests fail after the last refactor. Find out why.", [
        ("bash", {"command": "pytest -q"}, tests(7)),
        ("read_file", {"path": "src/billing.py"}, source("billing", 160)),
        ("read_file", {"path": "src/invoice.py"}, source("invoice", 140)),
        ("grep", {"pattern": "rate"}, grep("rate", 220)),
        ("read_file", {"path": "src/tax.py"}, source("tax", 120)),
        ("bash", {"command": "git log -p -3 src/tax.py"}, source("diff", 90)),
    ]),
    ("OK, fix the rounding in tax.py and rerun the tests.", [
        ("patch", {"path": "src/tax.py", "old": "round(x, 1)", "new": "round(x, 2)"}, "patched src/tax.py (1 hunk)"),
        ("bash", {"command": "pytest -q"}, tests(2)),
        ("read_file", {"path": "src/currency.py"}, source("currency", 150)),
        ("grep", {"pattern": "to_cents"}, grep("to_cents", 180)),
        ("patch", {"path": "src/currency.py", "old": "int(v*100)", "new": "round(v*100)"}, "patched src/currency.py (1 hunk)"),
        ("bash", {"command": "pytest -q"}, tests(0)),
    ]),
    ("Now add a test for the negative-amount case in invoice.py.", [
        ("read_file", {"path": "src/invoice.py"}, source("invoice", 140)),
        ("read_file", {"path": "tests/test_invoice.py"}, source("test_invoice", 110)),
        ("write_file", {"path": "tests/test_negative.py", "content": source("test_negative", 40)}, "wrote 80 lines"),
        ("bash", {"command": "pytest -q tests/test_negative.py"}, tests(1, 40)),
        ("read_file", {"path": "src/invoice.py"}, source("invoice", 140)),
        ("patch", {"path": "src/invoice.py", "old": "if amount:", "new": "if amount is not None:"}, "patched src/invoice.py (1 hunk)"),
        ("bash", {"command": "pytest -q"}, tests(0, 160)),
    ]),
    ("Last thing: summarize what changed in billing.py and tax.py for the PR description.", [
        ("bash", {"command": "git diff --stat"}, "".join(f" src/f{i}.py | {i} +-\n" for i in range(60))),
        ("read_file", {"path": "src/tax.py"}, source("tax", 120)),
        ("grep", {"pattern": "billing"}, grep("billing", 200)),
        ("bash", {"command": "git diff src/tax.py"}, source("diff2", 70)),
        ("read_file", {"path": "CHANGELOG.md"}, source("changelog", 80)),
        ("bash", {"command": "pytest -q"}, tests(0, 160)),
        ("bash", {"command": "git status"}, "".join(f"modified: src/f{i}.py\n" for i in range(40))),
    ]),
]


def requests():
    """Yield the body of every request the agent sends, in order."""
    msgs, n = [], 0
    for user, calls in SCRIPT:
        msgs.append({"role": "user", "content": user})
        for tool, inp, result in calls:
            yield {"model": "demo-model", "max_tokens": 1024, "system": "You are a coding agent.",
                   "messages": json.loads(json.dumps(msgs))}
            n += 1
            tid = f"toolu_{n:03d}"
            msgs.append({"role": "assistant", "content": [
                {"type": "text", "text": f"Step {n}: running {tool}."},
                {"type": "tool_use", "id": tid, "name": tool, "input": inp}]})
            msgs.append({"role": "user", "content": [{"type": "tool_result", "tool_use_id": tid, "content": result}]})
        yield {"model": "demo-model", "max_tokens": 1024, "system": "You are a coding agent.",
               "messages": json.loads(json.dumps(msgs))}
        msgs.append({"role": "assistant", "content": [{"type": "text", "text": "Done with this request."}]})


def run(home: str):
    sessions = proxy.Sessions(home)
    sent, orig, outs = 0, 0, []
    for body in requests():
        new, _ = proxy.compile_request(sessions, ROUTE, body)
        orig += len(json.dumps(body, ensure_ascii=False))
        out = json.dumps(new, ensure_ascii=False, sort_keys=True)
        sent += len(json.dumps(new, ensure_ascii=False))
        outs.append(out)
    return sent, orig, outs


def check_pairs(outs) -> bool:
    for out in outs:
        msgs = json.loads(out)["messages"]
        uses = {b["id"] for m in msgs if isinstance(m["content"], list) for b in m["content"] if b.get("type") == "tool_use"}
        res = {b["tool_use_id"] for m in msgs if isinstance(m["content"], list) for b in m["content"] if b.get("type") == "tool_result"}
        if uses != res:
            return False
    return True


def check_recall(outs):
    real = {r for _, calls in SCRIPT for _, _, r in calls}
    paths = set()
    for out in outs:
        # Paths sit inside a JSON string: separators on Windows appear as doubled backslashes.
        paths.update(m.replace("\\\\", "\\") for m in re.findall(r'full text: ((?:[^)"\\]|\\\\)+)\)', out))
    exact = sum(1 for p in paths if Path(p).is_file() and Path(p).read_text(encoding="utf-8") in real)
    return len(paths), exact


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="cmi_demo_a_") as a, tempfile.TemporaryDirectory(prefix="cmi_demo_b_") as b:
        sent, orig, outs = run(a)
        stubs, exact = check_recall(outs)
        _, _, outs2 = run(b)
        saved = 1 - sent / orig
        res = {
            "requests": len(outs),
            "chars_original": orig,
            "chars_sent": sent,
            "quota_saved": round(saved, 3),
            "runway_x": round(1 / (1 - saved), 2),
            "stubs_recallable": f"{exact}/{stubs}",
            "pairs_intact": check_pairs(outs),
            "recall_exact": stubs > 0 and exact == stubs,
            "deterministic": [re.sub(r"cmi_demo_[ab]_\w+", "", o) for o in outs]
                             == [re.sub(r"cmi_demo_[ab]_\w+", "", o) for o in outs2],
        }
    ok = res["pairs_intact"] and res["recall_exact"] and res["deterministic"]
    if "--json" in sys.argv:
        print(json.dumps(res, indent=2))
    else:
        print("(demo: start-up phase disabled, a 30-request session would otherwise stay untouched)")
        print(f"{res['requests']} requests, {orig:,} -> {sent:,} chars sent: "
              f"{res['quota_saved']:.0%} not sent (runway x{res['runway_x']})")
        print(f"stubs recallable exactly: {res['stubs_recallable']}; pairs intact: {res['pairs_intact']}; "
              f"deterministic: {res['deterministic']}")
        print("ALL OK" if ok else "FAILED")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
