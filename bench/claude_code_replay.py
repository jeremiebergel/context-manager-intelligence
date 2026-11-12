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

and reports tokens not sent plus an Anthropic prompt-cache cost estimate (read 0.1x, write 2x for the 1-hour cache)
with and without CMI (write ratio: CMI_WRITE_RATIO, default 2.0 = the 1-hour cache Claude Code
requests; set 1.25 for the 5-minute rate).

The replay runs on the session's own clock. Each request is sent at the time its line was logged,
so the engine sees the same pauses as the real session, and the provider cache is counted as lost
for both streams (with and without CMI) when
  - the pause before the request is longer than the cache lifetime (cold_idle), or
  - Claude Code's own usage record shows the request re-wrote most of its prompt (cold_other:
    model switch, a changed system prompt or tool list, ...).
Without the clock every pause looks like zero seconds: the cache never expires, CMI never gets
its free re-compile, and the result under-states what a real session saves. CMI_REPLAY_CLOCK=0
gives that old behaviour, for comparison only.

    python bench/claude_code_replay.py                   # 4 largest sessions
    python bench/claude_code_replay.py path/to/session.jsonl
"""
from __future__ import annotations

import datetime
import hashlib
import json
import sys
import tempfile
import time
import os
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from cmi import proxy  # noqa: E402
from cmi.compat import document_tokens  # noqa: E402
from cmi.store import KEY_RE, RecallStore  # noqa: E402

WRITE_RATIO = float(os.environ.get("CMI_WRITE_RATIO", "2.0"))
# Cache lifetime the client asks for: 1 hour goes with the 2x write price, 5 minutes with 1.25x.
CACHE_TTL_S = float(os.environ.get("CMI_CACHE_TTL", "3600" if WRITE_RATIO >= 2 else "300"))
USE_CLOCK = os.environ.get("CMI_REPLAY_CLOCK", "1") != "0"
PROJECTS = Path.home() / ".claude" / "projects"
# A request that re-wrote more than half of a prompt this large had lost its cache.
COLD_MIN_TOKENS = 30_000
RANKS = ((0, 50), (50, 100), (100, 150), (150, 300), (300, None))  # request rank since the last compaction

Meta = Tuple[Optional[float], Optional[Dict[str, Any]]]  # (time logged, usage of an assistant message)


def load_session(path: Path) -> List[Dict[str, Any]]:
    """The history after the last compaction (what the next request would send)."""
    return load_segments(path)[-1]


def load_segments(path: Path) -> List[List[Dict[str, Any]]]:
    """Main-chain messages as Claude Code sends them: one assistant message per API response
    (its content blocks are logged one per line), consecutive tool results merged into one user
    message. Sidechains (subagents) and bookkeeping lines are skipped. Each compaction starts a
    new segment: long sessions are mostly made of compacted segments, so all of them are replayed."""
    return [msgs for msgs, _ in load_timed_segments(path)]


def _timestamp(d: Dict[str, Any]) -> Optional[float]:
    t = d.get("timestamp")
    if not isinstance(t, str):
        return None
    try:
        return datetime.datetime.fromisoformat(t.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def load_timed_segments(path: Path) -> List[Tuple[List[Dict[str, Any]], List[Meta]]]:
    """load_segments, plus for every message the time it was logged and, for an assistant
    message, the usage the provider reported for the request that produced it."""
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
        t = _timestamp(d)
        if role == "assistant":
            mid = m.get("id")
            if msgs and msgs[-1]["role"] == "assistant" and mid and mid == last_asst_id:
                msgs[-1]["content"].extend(blocks)
            else:
                msgs.append({"role": "assistant", "content": blocks, "_t": t, "_usage": m.get("usage")})
            last_asst_id = mid
        else:
            if msgs and msgs[-1]["role"] == "user":
                msgs[-1]["content"].extend(blocks)
                msgs[-1]["_t"] = t or msgs[-1]["_t"]
            else:
                msgs.append({"role": "user", "content": blocks, "_t": t})
            last_asst_id = None
    segments.append(_repair_pairs(msgs))
    return [([{"role": m["role"], "content": m["content"]} for m in seg],
             [(m.get("_t"), m.get("_usage")) for m in seg]) for seg in segments]


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


def _msg_shape(m):
    """Everything that must never change in a message."""
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
    return sig


def _shape(msgs):
    return [_msg_shape(m) for m in msgs]


def _illegal_edits(before, after) -> List[str]:
    bad = []
    for i, (a, b) in enumerate(zip(before, after)):
        if a is b or a == b:
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
    """Wire size with every image and PDF counted at its billed size instead of its base64 length."""
    if isinstance(x, dict):
        if x.get("type") == "image":
            return IMAGE_TOKENS * 4
        doc = document_tokens(x)
        if doc is not None:
            return doc * 4
        return sum(_chars(v) + len(k) + 4 for k, v in x.items())
    if isinstance(x, list):
        return sum(_chars(v) + 2 for v in x)
    return len(json.dumps(x, ensure_ascii=False))


def _digest(m) -> bytes:
    return hashlib.md5(json.dumps(m, sort_keys=True).encode("utf-8", "surrogatepass")).digest()


def _prefix(prev: List[Any], cur: List[Any]) -> int:
    n = 0
    for a, b in zip(prev, cur):
        if a != b:
            break
        n += 1
    return n


def _cache_lost(usage: Optional[Dict[str, Any]]) -> bool:
    """Claude Code's usage record says this request re-wrote most of its prompt."""
    if not usage:
        return False
    read = usage.get("cache_read_input_tokens") or 0
    written = usage.get("cache_creation_input_tokens") or 0
    return read + written > COLD_MIN_TOKENS and read < written


class _ClockedSessions(proxy.Sessions):
    """Engines read the replay clock instead of the wall clock."""

    def __init__(self, home: str, clock) -> None:
        super().__init__(home)
        self._replay_clock = clock

    def get(self, *args, **kwargs):
        entry = super().get(*args, **kwargs)
        entry[0]._clock = self._replay_clock
        return entry


def _pct_saved(raw: float, cmi: float) -> str:
    return f"{100 * (1 - cmi / max(raw, 1)):.1f}%"


def _pct_delta(raw: float, cmi: float) -> str:
    return f"{100 * (cmi / max(raw, 1e-9) - 1):+.1f}%"


def replay(path: Path, max_requests: int = 0, clock: Optional[bool] = None) -> Dict[str, Any]:
    clock = USE_CLOCK if clock is None else clock
    segments = load_timed_segments(path)
    if max_requests:
        segments = segments[-1:]
    home = tempfile.mkdtemp(prefix="cmi_cc_")
    now = [0.0]
    sessions = _ClockedSessions(home, lambda: now[0]) if clock else proxy.Sessions(home)
    store = RecallStore(Path(home) / "cmi_store")
    cache_control = {"type": "ephemeral", "ttl": "1h"} if CACHE_TTL_S >= 3600 else {"type": "ephemeral"}
    plan = []  # (segment messages, their meta, request points); each compaction re-sends a new prefix
    for msgs, meta in segments:
        points = [k for k, m in enumerate(msgs) if m["role"] == "assistant" and k > 0]
        if max_requests and len(points) > max_requests:
            points = points[-max_requests:]
        plan.append((msgs, meta, points))
    raw_tok = cmi_tok = 0
    cost = {"raw": 0.0, "cmi": 0.0}
    ranks = [[0, 0, 0.0, 0.0] for _ in RANKS]  # raw tokens, CMI tokens, raw cost, CMI cost
    cold_idle = cold_other = 0
    billed = billed_raw = 0
    violations: List[str] = []
    unstable = 0
    keys = set()
    t0 = time.perf_counter()
    n = -1
    for msgs, meta, points in plan:
        # Unchanged messages come back as the same objects: digest and measure each one once.
        raw_sig = [_digest(m) for m in msgs]
        raw_size = [_chars(m) for m in msgs]
        prev: Dict[str, List[bytes]] = {"raw": [], "cmi": []}
        last_t: Optional[float] = None
        for i, k in enumerate(points):
            n += 1
            t = meta[k - 1][0] or meta[k][0] or last_t  # sent once the message before it is logged
            usage = meta[k][1]
            idle = bool(clock and t is not None and last_t is not None and t - last_t > CACHE_TTL_S)
            lost = bool(clock and i > 0 and not idle and _cache_lost(usage))
            cold_idle += idle
            cold_other += lost
            if t is not None:
                now[0], last_t = t, t
            req = {"model": "claude-sonnet-5", "max_tokens": 32000,
                   "system": [{"type": "text", "text": "claude code system prompt", "cache_control": cache_control}],
                   "messages": msgs[:k]}
            new, _ = proxy.compile_request(sessions, "/v1/messages", req)
            out = new["messages"]
            changed = [j for j, o in enumerate(out[:k]) if o is not msgs[j]]
            if len(out) != k or any(_msg_shape(out[j]) != _msg_shape(msgs[j]) for j in changed):
                violations.append(f"req {n}: shape changed")
            violations += [f"req {n}: {v}" for v in _illegal_edits(req["messages"], out)]
            if n % 25 == 0 or i == len(points) - 1:
                again, _ = proxy.compile_request(sessions, "/v1/messages", req)
                unstable += json.dumps(again) != json.dumps(new)
            cmi_sig, cmi_size = raw_sig[:len(out)], raw_size[:len(out)]
            for j in changed:
                cmi_sig[j], cmi_size[j] = _digest(out[j]), _chars(out[j])
                keys.update(KEY_RE.findall(json.dumps(out[j])))
            spent = {}
            for kind, sig, sizes in (("raw", raw_sig[:k], raw_size[:k]), ("cmi", cmi_sig, cmi_size)):
                p = 0 if (idle or lost) else _prefix(prev[kind], sig)
                cached = sum(sizes[:p]) // 4
                fresh = sum(sizes[p:]) // 4
                spent[kind] = cached * 0.1 + fresh * WRITE_RATIO
                cost[kind] += spent[kind]
                prev[kind] = sig
            raw_req, cmi_req = sum(raw_size[:k]) // 4, sum(cmi_size) // 4
            raw_tok += raw_req
            cmi_tok += cmi_req
            if usage:
                billed += sum(usage.get(f) or 0 for f in
                              ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens"))
                billed_raw += raw_req
            for r, (lo, hi) in zip(ranks, RANKS):
                if lo <= i and (hi is None or i < hi):
                    r[0] += raw_req
                    r[1] += cmi_req
                    r[2] += spent["raw"]
                    r[3] += spent["cmi"]
    recalled = sum(1 for key in keys if store.get(key) is not None or store.image_path(key) is not None)
    dt = time.perf_counter() - t0
    return {
        "session": path.stem[:8], "segments": len(plan), "messages": sum(len(m) for m, _, _ in plan),
        "requests": n + 1,
        "user_turns": sum(1 for msgs, _, _ in plan for m in msgs if proxy._is_user_turn(m)),
        "tokens_not_sent": _pct_saved(raw_tok, cmi_tok),
        "cached_cost_delta": _pct_delta(cost["raw"], cost["cmi"]),
        "clock": bool(clock), "cold_idle": cold_idle, "cold_other": cold_other,
        # How much of the really billed input the replay sees (the system prompt and the tool
        # list are not in the transcript, so a saving on the real bill is diluted by this much).
        "replayed_share_of_billed_input": f"{100 * billed_raw / billed:.0f}%" if billed else None,
        "by_request_rank": {
            f"{lo}-{hi}" if hi else f"{lo}+": {"tokens_not_sent": _pct_saved(r[0], r[1]),
                                               "cached_cost_delta": _pct_delta(r[2], r[3])}
            for r, (lo, hi) in zip(ranks, RANKS) if r[0]},
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
