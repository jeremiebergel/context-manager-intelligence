"""v1.10 step aging: one user prompt, many agent steps (kanban worker, Claude Code, Codex)."""
import json
import re

from cmi import CMIEngine
from cmi import engine as engine_mod
from cmi.engine import turn_starts


def _engine():
    e = CMIEngine()
    e.update_model(model="test-model", context_length=200000)
    return e


def _autonomous(steps, size=6000):
    """System + ONE user prompt + `steps` read_file calls on distinct files, each with a big result."""
    msgs = [{"role": "system", "content": "You are an agent."},
            {"role": "user", "content": "Run the whole E2E suite and fix what breaks."}]
    for s in range(steps):
        cid = f"call_{s}"
        msgs.append({"role": "assistant", "content": "",
                     "tool_calls": [{"id": cid, "type": "function", "function": {
                         "name": "read_file", "arguments": json.dumps({"path": f"/repo/src/mod{s}.py"})}}]})
        body = "".join(f"line {i} of mod{s}: value_{s}_{i} = {i * s}\n" for i in range(size // 30))
        msgs.append({"role": "tool", "tool_call_id": cid, "content": body})
    return msgs


def _texts(out):
    return [m.get("content") if isinstance(m.get("content"), str) else json.dumps(m.get("content")) for m in out]


def test_turn_starts_epochs():
    msgs = _autonomous(20)
    starts = turn_starts(msgs, epoch=8)
    # user prompt, then the assistant messages opening steps 9 and 17
    asst = [i for i, m in enumerate(msgs) if m["role"] == "assistant"]
    assert starts == [1, asst[8], asst[16]]
    assert turn_starts(msgs, epoch=0) == [1]


def test_long_single_prompt_turn_now_shrinks_old_results(monkeypatch):
    msgs = _autonomous(40)
    monkeypatch.setattr(engine_mod, "STEP_EPOCH", 0)
    assert _engine().select_context(list(msgs)) is None  # pre-1.10: nothing ever ages

    monkeypatch.setattr(engine_mod, "STEP_EPOCH", 8)
    e = _engine()
    out = e.select_context(list(msgs))
    assert out is not None and len(out) == len(msgs)
    texts = _texts(out)
    tool_idx = [i for i, m in enumerate(msgs) if m["role"] == "tool"]
    degraded = [i for i in tool_idx if texts[i].startswith("[CMI")]
    assert degraded, "old results of a long autonomous turn must be digested"
    # The last 3 epochs (up to 24 steps) stay raw: the agent's working memory is untouched.
    assert all(texts[i] == msgs[i]["content"] for i in tool_idx[-17:])
    # Pairing kept: same roles, same tool_call ids.
    assert [m["role"] for m in out] == [m["role"] for m in msgs]
    assert [m.get("tool_call_id") for m in out] == [m.get("tool_call_id") for m in msgs]
    # Zero loss: every digest carries a key that recalls the exact original.
    for i in degraded:
        key = re.search(r"cmi:([0-9a-f]{10,})", texts[i]).group(1)
        assert e.handle_tool_call("cmi_recall", {"key": f"cmi:{key}"}).find(msgs[i]["content"][:200]) >= 0


def test_step_aging_is_deterministic_and_cache_stable_within_an_epoch(monkeypatch):
    monkeypatch.setattr(engine_mod, "WARMUP_REQUESTS", 0)  # mechanics test: short session, no start-up phase
    monkeypatch.setattr(engine_mod, "STEP_EPOCH", 8)
    msgs = _autonomous(60)
    e = _engine()
    # Replay request by request like the host: one request after every tool result.
    outs = []
    for cut in range(4, len(msgs) + 1, 2):
        outs.append(_texts(e.select_context(list(msgs[:cut])) or msgs[:cut]))
    # Same history twice in a row (a retry) -> byte-identical.
    again = _texts(e.select_context(list(msgs)) or msgs)
    assert again == outs[-1]
    # The prefix only changes at epoch boundaries: count rewrites of earlier messages.
    rewrites = 0
    for prev, cur in zip(outs, outs[1:]):
        if cur[:len(prev)] != prev:
            rewrites += 1
    assert 0 < rewrites <= 60 // 8, rewrites


def test_user_turn_boundaries_unchanged_for_short_turns(monkeypatch):
    """Turns of fewer than STEP_EPOCH steps never get an epoch start: same as before 1.10."""
    msgs = [{"role": "system", "content": "s"}]
    for t in range(6):
        msgs.append({"role": "user", "content": f"q{t}"})
        for s in range(3):
            msgs.append({"role": "assistant", "content": "", "tool_calls": [
                {"id": f"c{t}{s}", "type": "function", "function": {"name": "terminal", "arguments": "{}"}}]})
            msgs.append({"role": "tool", "tool_call_id": f"c{t}{s}", "content": "x"})
        msgs.append({"role": "assistant", "content": "done"})
    assert turn_starts(msgs, epoch=8) == [i for i, m in enumerate(msgs) if m["role"] == "user"]


def test_warmup_keeps_a_short_session_byte_stable_while_the_same_session_tiers_without_it(monkeypatch):
    """Replay of 26 real sessions: three short ones (41-63 requests) cost +16..+20% because tiers and
    refreshes rewrote the provider cache before it could pay back. Until WARMUP_REQUESTS requests CMI
    keeps what was already sent byte for byte (a file the user names is still refreshed in full)."""
    monkeypatch.setattr(engine_mod, "STEP_EPOCH", 8)
    msgs = _autonomous(60)

    def rewrites(warmup):
        monkeypatch.setattr(engine_mod, "WARMUP_REQUESTS", warmup)
        e = _engine()
        outs = [_texts(e.select_context(list(msgs[:cut])) or msgs[:cut]) for cut in range(4, len(msgs) + 1, 2)]
        return sum(cur[:len(prev)] != prev for prev, cur in zip(outs, outs[1:]))

    assert rewrites(0) > 0
    assert rewrites(100) == 0
