"""Cold-cache tiers: after a gap longer than the provider cache TTL, nothing is cached for the
raw request either, so CMI re-compiles the whole history for free."""
import json

from cmi import engine as engine_mod
from cmi.cost_guard import CostShadow
from tests.test_cmi_engine import make_engine


def _session():
    msgs = [{"role": "system", "content": "sys"}]
    for t in range(12):
        body = "\n".join(f"t{t} line {i}: value {i * 7 + t}" for i in range(200))
        msgs += [{"role": "user", "content": f"turn {t}"},
                 {"role": "assistant", "content": "", "tool_calls": [{"id": f"c{t}", "type": "function",
                  "function": {"name": "terminal", "arguments": json.dumps({"command": f"run {t}"})}}]},
                 {"role": "tool", "tool_call_id": f"c{t}", "content": body},
                 {"role": "assistant", "content": f"done {t}"}]
    return msgs


def _replay(gap_s, monkeypatch):
    monkeypatch.setattr(engine_mod, "TIER_PAYBACK_REQUESTS", 0)  # warm tiers never pay back
    e = make_engine()
    now = [1000.0]
    e._clock = lambda: now[0]
    msgs = _session()
    sent = 0
    for u in [i for i, m in enumerate(msgs) if m["role"] == "user"][1:]:
        e.on_turn_complete(msgs[:u])
        now[0] += gap_s
        out = e.select_context(msgs[:u + 1]) or msgs[:u + 1]
        sent += len(json.dumps(out))
    return e, sent


def test_cold_gap_tiers_for_free(monkeypatch):
    warm, warm_sent = _replay(60, monkeypatch)
    cold, cold_sent = _replay(600, monkeypatch)
    assert not warm.telemetry.get("tiers_cold")
    assert cold.telemetry.get("tiers_cold", 0) > 0
    assert cold_sent < warm_sent


def test_cost_shadow_cold_request_has_no_cache_hit():
    s = CostShadow()
    s.read, s.write = 0.1, 1.25
    s.observe([1, 2], [100, 100], [1, 2], [100, 100])
    s.observe([1, 2, 3], [100, 100, 100], [1, 2, 3], [100, 100, 100], cold=True)
    assert s.raw.cost == s.cmi.cost == 250 + 375


def test_proxy_reads_requested_cache_ttl():
    from cmi.proxy import _request_cache_ttl
    one_h = {"system": [{"type": "text", "text": "s", "cache_control": {"type": "ephemeral", "ttl": "1h"}}],
             "messages": []}
    five = {"system": [{"type": "text", "text": "s", "cache_control": {"type": "ephemeral"}}], "messages": []}
    assert _request_cache_ttl(one_h, 300) == 3600
    assert _request_cache_ttl(five, 3600) == 300
    assert _request_cache_ttl({"messages": []}, 123) == 123


def test_fork_reuses_parent_renderings_so_the_cache_hits():
    """Hermes' background review replays the parent's history in a new engine. It must send the
    parent's exact bytes for that history (otherwise the whole cached prefix is rewritten)."""
    msgs = _session()
    parent = make_engine()
    users = [i for i, m in enumerate(msgs) if m["role"] == "user"]
    out = None
    for u in users[1:]:
        parent.on_turn_complete(msgs[:u])
        out = parent.select_context(msgs[:u + 1]) or msgs[:u + 1]
    n = users[-1] + 1
    history = msgs[:n] + [{"role": "assistant", "content": "final answer"}]
    fork = make_engine()
    fork_req = history + [{"role": "user", "content": "Review the conversation above."}]
    fork_out = fork.select_context(fork_req) or fork_req
    assert fork.telemetry.get("adopted_parent") == 1
    assert [json.dumps(m, sort_keys=True) for m in fork_out[:n]] == [json.dumps(m, sort_keys=True) for m in out]


def test_unrelated_engine_does_not_adopt():
    a = make_engine()
    msgs = _session()
    a.select_context(msgs[:9])
    b = make_engine()
    other = [{"role": "system", "content": "other"}, {"role": "user", "content": "hi"}]
    b.select_context(other)
    assert not b.telemetry.get("adopted_parent")
