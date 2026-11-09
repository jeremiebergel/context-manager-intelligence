"""Replay real Claude Code sessions through the proxy compile path (no network, no API key).

Claude Code keeps every session as JSONL under ~/.claude/projects/. This rebuilds the Anthropic
Messages history request by request (one request before every assistant message, as the agent
loop sends them), compiles each one exactly like cmi-proxy does, and checks on EVERY request:

  pairs      same message count/roles; every tool_use id/name and tool_result id unchanged, in order
  untouched  thinking blocks (signatures), images, cache_control and user text byte-identical;
             the only edits are CMI stubs in tool_result content, assistant narration text, or
             write-tool inputs
  stable     compiling the same request twice gives the same bytes (prompt cache)
  recall     every cmi:key in a stub returns the exact original from the store

and reports tokens not sent plus an Anthropic prompt-cache cost estimate (read 0.1x, write 1.25x)
with and without CMI.

    python bench/claude_code_replay.py                   # 4 largest sessions
    python bench/claude_code_replay.py path/to/session.jsonl
"""
from __future__ import annotations

import json
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, List

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from cmi import proxy  # noqa: E402
from cmi.store import KEY_RE, RecallStore  # noqa: E402

PROJECTS = Path.home() / ".claude" / "projects"


def load_session(path: Path) -> List[Dict[str, Any]]:
    """The history after the last compaction (what the next request would send)."""
    return load_segments(path)[-1]


def load_segments(path: Path) -> List[List[Dict[str, Any]]]:
    """Main-chain messages as Claude Code sends them: one assistant message per API response
    (its content blocks are logged one per line), consecutive tool results merged into one user
    message. Sidechains (subagents) and bookkeeping lines are skipped. Each compaction starts a
    new segment: long sessions are mostly made of compacted segments, so all of them are replayed."""
    segments: List[List[Dict[str, Any]]] = []
    msgs: List[Dict[str, Any]] = []
    last_asst_id = None
    for line in path.open(encoding="utf-8", errors="replace"):
        try:
            d = json.loads(line)
        except ValueError:
            continue
        if d.get("isSidechain"):
            continue
        if d.get("type") == "system" and d.get("subtype") == "compact_boundary":
            if msgs:
                segments.append(_repair_pairs(msgs))
            msgs, last_asst_id = [], None
            continue
        if d.get("type") not in ("user", "assistant") or d.get("isApiErrorMessage"):
            continue
        m = d.get("message") or {}
        role, content = m.get("role"), m.get("content")
        if role not in ("user", "assistant") or content in (None, "", []):
            continue
        blocks = [{"type": "text", "text": content}] if isinstance(content, str) else \
            [b for b in content if isinstance(b, dict)]
        if role == "assistant":
            mid = m.get("id")
            if msgs and msgs[-1]["role"] == "assistant" and mid and mid == last_asst_id:
                msgs[-1]["content"].extend(blocks)
            else:
                msgs.append({"role": "assistant", "content": blocks})
            last_asst_id = mid
        else:
            if msgs and msgs[-1]["role"] == "user":
                msgs[-1]["content"].extend(blocks)
            else:
                msgs.append({"role": "user", "content": blocks})
            last_asst_id = None
    segments.append(_repair_pairs(msgs))
    return segments


def _repair_pairs(msgs: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Interrupted turns leave a tool_use without its result; the API would reject that request,
    so drop such tool_use blocks (the replay must stay a valid request sequence)."""
    results = {b.get("tool_use_id") for m in msgs if m["role"] == "user" for b in m["content"]
               if b.get("type") == "tool_result"}
    out = []
    for m in msgs:
        if m["role"] == "assistant":
            m = {**m, "content": [b for b in m["content"] if b.get("type") != "tool_use" or b.get("id") in results]}
            if not m["content"]:
                continue
        out.append(m)
    return out


def _shape(msgs):
    """Everything that must never change, per message."""
    out = []
    for m in msgs:
        sig = [m["role"]]
        for b in m["content"]:
            t = b.get("type")
            if t == "tool_use":
                sig.append(("tool_use", b.get("id"), b.get("name")))
            elif t == "tool_result":
                sig.append(("tool_result", b.get("tool_use_id"), bool(b.get("is_error"))))
            elif t == "text" and m["role"] == "assistant":
                sig.append(("text",))
            else:
                sig.append(json.dumps(b, sort_keys=True))     # thinking, images, user text: verbatim
            sig.append(json.dumps(b.get("cache_control"), sort_keys=True))
        out.append(sig)
    return out


def _illegal_edits(before, after) -> List[str]:
    bad = []
    for i, (a, b) in enumerate(zip(before, after)):
        if a == b:
            continue
        for x, y in zip(a["content"], b["content"]):
            if x == y:
                continue
            t = x.get("type")
            ok = (t == "tool_result" and ("[CMI" in json.dumps(y.get("content"))
                                          or "[image evicted by CMI cmi:" in json.dumps(y.get("content"))))
            ok |= (t == "text" and a["role"] == "assistant" and y.get("text", "").startswith("[CMI"))
            ok |= (t == "tool_use" and "cmi:" in json.dumps(y.get("input")))
            if not ok:
                bad.append(f"msg {i} {t}")
    return bad


IMAGE_TOKENS = 1600  # Anthropic bills images by pixels (~1.6K tokens for a large screenshot), not base64 size


def _tokens(x) -> int:
    return _chars(x) // 4


def _chars(x) -> int:
    """Wire size with every image counted at its billed size instead of its base64 length."""
    if isinstance(x, dict):
        if x.get("type") == "image":
            return IMAGE_TOKENS * 4
        return sum(_chars(v) + len(k) + 4 for k, v in x.items())
    if isinstance(x, list):
        return sum(_chars(v) + 2 for v in x)
    return len(json.dumps(x, ensure_ascii=False))


def _prefix(prev: List[str], cur: List[str]) -> int:
    n = 0
    for a, b in zip(prev, cur):
        if a != b:
            break
        n += 1
    return n


def replay(path: Path, max_requests: int = 0) -> Dict[str, Any]:
    segments = load_segments(path)
    if max_requests:
        segments = segments[-1:]
    home = tempfile.mkdtemp(prefix="cmi_cc_")
    sessions = proxy.Sessions(home)
    store = RecallStore(Path(home) / "cmi_store")
    plan = []  # (segment messages, request points); each compaction re-sends a new prefix
    for msgs in segments:
        points = [k for k, m in enumerate(msgs) if m["role"] == "assistant" and k > 0]
        if max_requests and len(points) > max_requests:
            points = points[-max_requests:]
        plan.append((msgs, points))
    raw_tok = cmi_tok = 0
    cost = {"raw": 0.0, "cmi": 0.0}
    violations: List[str] = []
    unstable = 0
    keys = set()
    t0 = time.perf_counter()
    n = -1
    for msgs, points in plan:
        prev: Dict[str, List[str]] = {"raw": [], "cmi": []}
        for i, k in enumerate(points):
            n += 1
            req = {"model": "claude-sonnet-5", "max_tokens": 32000,
                   "system": [{"type": "text", "text": "claude code system prompt"}], "messages": msgs[:k]}
            new, _ = proxy.compile_request(sessions, "/v1/messages", req)
            out = new["messages"]
            if _shape(out) != _shape(req["messages"]):
                violations.append(f"req {n}: shape changed")
            violations += [f"req {n}: {v}" for v in _illegal_edits(req["messages"], out)]
            if n % 25 == 0 or i == len(points) - 1:
                again, _ = proxy.compile_request(sessions, "/v1/messages", req)
                unstable += json.dumps(again) != json.dumps(new)
            for kind, seq in (("raw", req["messages"]), ("cmi", out)):
                ser = [json.dumps(m, sort_keys=True) for m in seq]
                sizes = [_chars(m) for m in seq]
                p = _prefix(prev[kind], ser)
                cached = sum(sizes[:p]) // 4
                fresh = sum(sizes[p:]) // 4
                cost[kind] += cached * 0.1 + fresh * 1.25
                prev[kind] = ser
            raw_tok += _tokens(req["messages"])
            cmi_tok += _tokens(out)
            keys.update(KEY_RE.findall(json.dumps(out)))
    recalled = sum(1 for key in keys if store.get(key) is not None or store.image_path(key) is not None)
    dt = time.perf_counter() - t0
    return {
        "session": path.stem[:8], "segments": len(plan), "messages": sum(len(m) for m, _ in plan),
        "requests": n + 1,
        "user_turns": sum(1 for msgs, _ in plan for m in msgs if proxy._is_user_turn(m)),
        "tokens_not_sent": f"{100 * (1 - cmi_tok / max(raw_tok, 1)):.1f}%",
        "cached_cost_delta": f"{100 * (cost['cmi'] / max(cost['raw'], 1e-9) - 1):+.1f}%",
        "violations": len(violations), "first_violations": violations[:5],
        "unstable": unstable, "stub_keys": len(keys), "recall_exact": f"{recalled}/{len(keys)}",
        "ms_per_request": round(1000 * dt / max(n + 1, 1), 1),
    }


def main(argv: List[str]) -> None:
    paths = [Path(a) for a in argv] or sorted(PROJECTS.glob("*/*.jsonl"), key=lambda p: -p.stat().st_size)[:4]
    for p in paths:
        print(json.dumps(replay(p)), flush=True)


if __name__ == "__main__":
    main(sys.argv[1:])
