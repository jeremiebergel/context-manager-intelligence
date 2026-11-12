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


def _forced_refresh(first_rendering, fresh_rendering, superseded=False):
    """Two requests on a warm cache; message 2 is one the current turn needs in full
    (``_must_refresh``), or, with ``superseded``, a read made obsolete by a later edit under the
    opt-in CMI_SUPERSEDED_REFRESH. Returns what the second request sends for it."""
    e = make_engine()
    e.warmup_requests = 0
    full = {"role": "tool", "tool_call_id": "c1", "content": "full result " * 60}
    stub = {"role": "tool", "tool_call_id": "c1", "content": "[CMI dup] cmi:aaaaaaaaaaaa identical to an earlier result"}
    pick = {"full": full, "stub": stub}
    head = [{"role": "user", "content": "q1"},
            {"role": "assistant", "content": "", "tool_calls": [{"id": "c1", "type": "function",
             "function": {"name": "terminal", "arguments": "{}"}}]},
            full, {"role": "assistant", "content": "a1"}]
    tail = [{"role": "user", "content": "q2"}, {"role": "assistant", "content": "a2"},
            {"role": "user", "content": "q3"}]

    def send(msgs, rendering, must):
        e._user_idx = len(msgs) - 1
        e._must_refresh = must
        e._superseded_forced = set(must) if superseded else set()
        compiled = list(msgs)
        compiled[2] = pick[rendering]
        out = e._apply_cache_tiers(msgs, compiled, len(msgs) - 1)
        return (out or msgs)[2]

    send(head + tail[:1], first_rendering, set())
    return send(head + tail, fresh_rendering, {2}), full, stub


def test_forced_refresh_restores_a_shortened_result():
    sent, full, _ = _forced_refresh("stub", "full")
    assert sent["content"] == full["content"]


def test_forced_refresh_never_shortens_a_result_already_sent_in_full():
    """Real Claude Code session, 2026-11-11: a result the turn needed in full was already cached
    in full, a later pass collapsed it as a duplicate, and the forced refresh sent that stub the
    moment the start-up phase ended: 195K tokens of cache re-written to remove 59."""
    sent, full, _ = _forced_refresh("full", "stub")
    assert sent["content"] == full["content"]


def test_opt_in_superseded_refresh_still_replaces_a_read_sent_in_full():
    """CMI_SUPERSEDED_REFRESH=1: an obsolete read sent in full takes its fresh (outdated-marked)
    rendering at once. The already-sent-in-full exception is for the turn-needs-it-in-full path only."""
    sent, _, stub = _forced_refresh("full", "stub", superseded=True)
    assert sent["content"] == stub["content"]
