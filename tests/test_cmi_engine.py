"""CMI v1 test suite — 20 tests from the validated spec + args-degradation.

Run:  ./venv/bin/python -m pytest tests/test_cmi_engine.py -v
"""
import copy
import json
import re
import sys
from pathlib import Path
from unittest import mock

import pytest

REPO = Path(__file__).resolve().parent.parent  # plugin root (conftest registers it as `cmi`)

from cmi import CMIEngine, ModelCapabilities, WorkingSet  # noqa: E402


def make_engine(model="test-model", context_length=200000):
    e = CMIEngine()
    e.update_model(model=model, context_length=context_length)
    return e


def _mk(role, content, **kw):
    m = {"role": role, "content": content}
    m.update(kw)
    return m


def build_history(n_turns=30, big_tool_result=None, image_at=None):
    msgs = [{"role": "system", "content": "You are a helpful agent."}]
    for t in range(1, n_turns + 1):
        msgs.append({"role": "user", "content": f"User message turn {t}"})
        if t == 5 and big_tool_result:
            msgs.append({
                "role": "assistant", "content": "",
                "tool_calls": [{"id": "call_big", "type": "function",
                                "function": {"name": "terminal",
                                             "arguments": json.dumps({"command": big_tool_result[0]})}}],
            })
            msgs.append({"role": "tool", "tool_call_id": "call_big",
                         "content": big_tool_result[1]})
        if image_at == t:
            msgs.append({
                "role": "assistant", "content": "",
                "tool_calls": [{"id": f"call_img_{t}", "type": "function",
                                "function": {"name": "mcp__browser_exec",
                                             "arguments": "{}"}}],
            })
            msgs.append({
                "role": "user", "content": [
                    {"type": "tool_result", "tool_use_id": f"call_img_{t}",
                     "content": [
                         {"type": "text", "text": f"screenshot {t} description"},
                         {"type": "image", "source": {"type": "base64",
                                                      "media_type": "image/png",
                                                      "data": "x" * 1000}},
                     ]},
                ],
            })
        msgs.append({"role": "assistant", "content": f"Assistant reply turn {t}"})
    return msgs


# ---------------------------------------------------------------- Test 1
def test_1_under_threshold_no_selection():
    e = make_engine()
    msgs = build_history(3)
    out = e.select_context([m for m in msgs])
    assert out is None


# ---------------------------------------------------------------- Test 2
def test_2_recent_protected():
    e = make_engine()
    big = ("npm test", "line\n" * 2000)
    msgs = build_history(3, big_tool_result=big)
    out = e.select_context([m for m in msgs])
    assert out is None
    assert e.lifecycle.decide(age=1, score=0.0, user_anchor=False,
                              represented=False, referenced=False,
                              profitable=True) == "raw"


# ---------------------------------------------------------------- Test 3
def test_3_user_constraints_untouched():
    e = make_engine()
    msgs = build_history(30)
    msgs.append({"role": "user",
                 "content": "IMPORTANT CONSTRAINT: never delete the database."})
    out = e.select_context([m for m in msgs])
    if out is not None:
        texts = [m.get("content") for m in out if m.get("role") == "user"]
        assert any("never delete the database" in str(t) for t in texts)


# ---------------------------------------------------------------- Test 4
def test_4_tool_pair_integrity():
    e = make_engine()
    big = ("cat huge.log", "x" * 20000)
    msgs = build_history(30, big_tool_result=big)
    out = e.select_context([m for m in msgs])
    assert out is not None
    calls = {}
    for m in out:
        if m.get("role") == "assistant":
            for tc in m.get("tool_calls") or []:
                calls[tc["id"]] = True
    results = set()
    for m in out:
        if m.get("role") == "tool":
            results.add(m.get("tool_call_id"))
        elif m.get("role") == "user" and isinstance(m.get("content"), list):
            for p in m["content"]:
                if isinstance(p, dict) and p.get("type") == "tool_result":
                    results.add(p.get("tool_use_id"))
    assert calls.keys() and results
    assert set(calls.keys()) == results


# ---------------------------------------------------------------- Test 5
def test_5_old_screenshot_evicted_once():
    e = make_engine()
    msgs = build_history(30, image_at=5)
    out = e.select_context([m for m in msgs])
    assert out is not None
    flat = json.dumps(out)
    assert '"type": "image"' not in flat
    assert e.telemetry["image_evictions"] >= 1
    n = e.telemetry["image_evictions"]
    e.select_context([m for m in msgs])
    assert e.telemetry["image_evictions"] == n


# ---------------------------------------------------------------- Test 6
def test_6_big_terminal_digested():
    e = make_engine()
    big = ("npm run build", ("compiling...\n" * 1500))
    msgs = build_history(30, big_tool_result=big)
    out = e.select_context([m for m in msgs])
    assert out is not None
    tool_msgs = [m for m in out if m.get("role") == "tool"]
    digested = [m for m in tool_msgs if "[CMI digest]" in str(m.get("content"))]
    assert digested
    assert len(str(digested[0]["content"])) < 800


# ---------------------------------------------------------------- Test 7
def test_7_stale_to_active_revival():
    e = make_engine()
    big = ("cat auth.ts", "export function authenticate() {}\n" + "x" * 9000)
    msgs = build_history(30, big_tool_result=big)
    out = e.select_context([m for m in msgs])
    assert out is not None
    e.working_set.note_user_mention(["auth.ts"])
    msgs2 = build_history(31, big_tool_result=big)
    e.select_context([m for m in msgs2])
    states = [e.lifecycle.state_of(k) for k in e.lifecycle._render_state]
    assert "raw" in states


# ---------------------------------------------------------------- Test 8
def test_8_modified_file_priority_persists():
    ws = WorkingSet()
    ws.note_write("src/engine.py")
    for _ in range(15):
        ws.on_turn_complete()
    assert ws.score("src/engine.py") >= 2.0


# ---------------------------------------------------------------- Test 9
def test_9_user_mention_boost():
    ws = WorkingSet()
    ws.note_read("auth.ts")
    for _ in range(30):
        ws.on_turn_complete()
    assert ws.score("auth.ts") < 1.0
    ws.note_user_mention(["auth.ts"])
    assert ws.score("auth.ts") >= 5.0


# ---------------------------------------------------------------- Test 10
def test_10_model_switch_preserves_state():
    e = make_engine(model="glm-5.3", context_length=128000)
    e.working_set.note_write("src/app.py")
    big = ("cat x.log", "y" * 20000)
    msgs = build_history(30, big_tool_result=big)
    e.select_context([m for m in msgs])
    n_states = len(e.lifecycle._render_state)
    e.update_model(model="claude-sonnet-4", context_length=200000)
    assert e.working_set.score("src/app.py") >= 2.0
    assert len(e.lifecycle._render_state) == n_states
    assert e.capabilities.context_window == 200000
    assert e.threshold_tokens == int(200000 * e.threshold_percent)


# ---------------------------------------------------------------- Test 11
def test_11_provider_switch_no_exception():
    e = make_engine()
    for provider, model, window in [
        ("zai", "glm-5.3", 128000),
        ("openrouter", "qwen/qwen3-coder", 262144),
        ("anthropic", "claude-sonnet-4", 200000),
        ("openai", "gpt-5.6", 400000),
        ("ollama", "qwen3:32b", 32768),
    ]:
        e.update_model(model=model, context_length=window, provider=provider,
                       base_url="http://localhost:11434/v1" if provider == "ollama" else "")
        assert e.capabilities is not None
        msgs = build_history(10)
        assert e.select_context([m for m in msgs]) is None


# ---------------------------------------------------------------- Test 12
def test_12_selection_then_compaction_delegation():
    e = make_engine()
    big = ("cat a.log", "z" * 20000)
    msgs = build_history(30, big_tool_result=big)
    e.select_context([m for m in msgs])
    assert any(s == "digest" for s in e.lifecycle._render_state.values())
    e._compactor = mock.MagicMock()
    e._compactor.compress.return_value = msgs[:10]
    out = e.compress(msgs, current_tokens=180000)
    e._compactor.compress.assert_called_once()
    assert out == msgs[:10]
    assert e.compression_count == 1
    states = set(e.lifecycle._render_state.values())
    assert states <= {"one_liner"}


# ---------------------------------------------------------------- Test 13
def test_13_cmi_crash_fails_open():
    e = make_engine()
    msgs = build_history(5)
    with mock.patch.object(e, "_select_impl", side_effect=RuntimeError("boom")):
        out = e.select_context([m for m in msgs])
    assert out is None


# ---------------------------------------------------------------- Test 14
def test_14_no_recursion_no_llm_calls(monkeypatch):
    """select_context must never reach an LLM: any auxiliary call raises."""
    aux = pytest.importorskip("agent.auxiliary_client")  # Hermes-only: standalone has no aux client

    def _boom(*a, **k):
        raise AssertionError("select_context made an LLM call")

    for name in dir(aux):
        if name.startswith("call_") or name.endswith("_llm"):
            monkeypatch.setattr(aux, name, _boom, raising=False)
    e = make_engine()
    big = ("cat b.log", "w" * 20000)
    msgs = build_history(30, big_tool_result=big)
    out = e.select_context([m for m in msgs])
    assert out is not None


# ---------------------------------------------------------------- Test 15
def test_15_cache_stability_consecutive_none():
    e = make_engine()
    msgs = build_history(30)
    for _ in range(5):
        assert e.select_context([m for m in msgs]) is None
    big = ("cat c.log", "v" * 20000)
    msgs = build_history(30, big_tool_result=big)
    out1 = e.select_context([m for m in msgs])
    assert out1 is not None
    d1 = [m for m in out1 if "[CMI digest]" in str(m.get("content"))][0]["content"]
    out2 = e.select_context([copy.deepcopy(m) for m in msgs])
    assert out2 is not None
    d2 = [m for m in out2 if "[CMI digest]" in str(m.get("content"))][0]["content"]
    assert d1 == d2


# ---------------------------------------------------------------- Test 16
def test_16_long_session_150_turns_bounded():
    e = make_engine()
    msgs = build_history(150)
    for i in range(1, 150, 10):
        e.working_set.note_read(f"src/file{i}.py")
    for _ in range(150):
        e.working_set.on_turn_complete()
    assert e.working_set.state_size() <= 50 * 3
    assert len(e.working_set.known_files()) <= 50
    out = e.select_context([m for m in msgs])
    if out is not None:
        n_user_in = sum(1 for m in msgs if m.get("role") == "user")
        n_user_out = sum(1 for m in out if m.get("role") == "user")
        assert n_user_out == n_user_in


# ---------------------------------------------------------------- Test 17
def test_17_persisted_history_untouched_checksum():
    e = make_engine()
    big = ("cat d.log", "u" * 20000)
    msgs = build_history(30, big_tool_result=big)
    snapshot = copy.deepcopy(msgs)
    out = e.select_context([m for m in msgs])
    assert out is not None
    assert msgs == snapshot


# ---------------------------------------------------------------- Test 18
def test_18_multi_compaction_coherence():
    e = make_engine()
    for cycle in range(3):
        big = (f"cat cycle{cycle}.log", "k" * 20000)
        msgs = build_history(30 + cycle, big_tool_result=big)
        e.select_context([m for m in msgs])
        e._compactor = mock.MagicMock()
        e._compactor.compress.return_value = msgs[:8]
        e.compress(msgs, current_tokens=190000)
    assert e.compression_count == 3
    states = set(e.lifecycle._render_state.values())
    assert states <= {"digest", "one_liner"}


# ---------------------------------------------------------------- Extra
def test_information_recovery_scenario_spec8():
    e = make_engine()
    auth_content = ("export function authenticate(u, p) { /* ... */ }\n"
                    "export function verifyToken(t) { /* ... */ }\n" + "// filler\n" * 500)
    big = ("read auth.ts", auth_content)
    msgs = build_history(30, big_tool_result=big)
    out = e.select_context([m for m in msgs])
    assert out is not None
    digests = [m for m in out if "[CMI" in str(m.get("content"))]
    assert digests
    e.working_set.note_user_mention(["auth.ts"])
    assert e.working_set.is_prioritary("auth.ts")


def test_capabilities_budget():
    caps = ModelCapabilities(model="m", context_window=200000, reserved_output=8192)
    b = caps.input_budget
    assert 0 < b < 200000
    caps0 = ModelCapabilities(model="m", context_window=0)
    assert caps0.input_budget >= 1


# ---------------------------------------------------------------- New: profitability + args
def test_19_digest_never_grows_small_content():
    """The regression found on a real benchmark session: a digest wrapper
    must never exceed the original content size."""
    e = make_engine()
    small = ("ls", "file1.txt\nfile2.txt")  # ~20 chars, way under any digest overhead
    msgs = build_history(30, big_tool_result=small)
    out = e.select_context([m for m in msgs])
    tool_msgs = (out or msgs)
    # find the small tool result and confirm it stayed raw (not degraded to
    # something bigger)
    for m in tool_msgs:
        if m.get("role") == "tool" and m.get("tool_call_id") == "call_big":
            assert len(str(m.get("content"))) <= len("file1.txt\nfile2.txt") + 5
            assert "[CMI" not in str(m.get("content")), \
                "small content must never be wrapped in a growing digest"


def test_20_write_file_args_degraded_when_old():
    """write_file/patch call ARGUMENTS (the file content, not the result)
    shrink once old — the largest real payload source on code-heavy sessions."""
    e = make_engine()
    file_content = "export class Foo {}\n" + "// line\n" * 200
    msgs = [{"role": "system", "content": "sys"}]
    msgs.append({"role": "user", "content": "Write foo.ts"})
    msgs.append({
        "role": "assistant", "content": "",
        "tool_calls": [{"id": "wc1", "type": "function",
                        "function": {"name": "write_file",
                                     "arguments": json.dumps({"path": "foo.ts",
                                                              "content": file_content})}}],
    })
    msgs.append({"role": "tool", "tool_call_id": "wc1", "content": "written"})
    for t in range(2, 26):
        msgs.append({"role": "user", "content": f"unrelated task {t}"})
        msgs.append({"role": "assistant", "content": f"done {t}"})

    out = e.select_context([m for m in msgs])
    assert out is not None
    write_msg = next(m for m in out if m.get("role") == "assistant"
                     and any(tc.get("id") == "wc1" for tc in (m.get("tool_calls") or [])))
    tc = next(tc for tc in write_msg["tool_calls"] if tc["id"] == "wc1")
    args = json.loads(tc["function"]["arguments"])
    # The payload key is removed, not replaced by a stub: a model that imitates this past call sends a
    # write without content, which the tool refuses, instead of writing a stub over the file.
    assert "content" not in args and args["path"] == "foo.ts"
    assert "foo.ts" in args["cmi_omitted"] and "cmi:" in args["cmi_omitted"]
    assert len(json.dumps(args)) < len(file_content)
    # tool_call id/name untouched — pairing intact
    assert tc["id"] == "wc1"
    assert tc["function"]["name"] == "write_file"
    tool_result = next(m for m in out if m.get("role") == "tool" and m.get("tool_call_id") == "wc1")
    assert tool_result["content"] == "written"


def test_21_recent_write_file_args_untouched():
    """A write_file from the current/previous turn keeps its full content —
    the agent may still need to reason about what it just wrote."""
    e = make_engine()
    file_content = "export class Foo {}\n" + "// line\n" * 200
    msgs = [{"role": "system", "content": "sys"}]
    msgs.append({"role": "user", "content": "Write foo.ts"})
    msgs.append({
        "role": "assistant", "content": "",
        "tool_calls": [{"id": "wc1", "type": "function",
                        "function": {"name": "write_file",
                                     "arguments": json.dumps({"path": "foo.ts",
                                                              "content": file_content})}}],
    })
    msgs.append({"role": "tool", "tool_call_id": "wc1", "content": "written"})
    out = e.select_context([m for m in msgs])
    assert out is None  # nothing old enough to degrade


# ---------------------------------------------------------------- New: distributed excerpt + batch
def test_22_distributed_excerpt_preserves_middle_content():
    """Regression: a fixed head-only excerpt hid a secret defined after a
    long comment block. The excerpt must sample head+middle+tail so
    information anywhere in the content has a chance to survive."""
    e = make_engine()
    big_content = "# comment\n" * 50 + "SECRET_KEY = 'abc123'\n" + "# comment\n" * 50
    msgs = [{"role": "system", "content": "sys"}]
    msgs.append({"role": "user", "content": "Lis config.py"})
    msgs.append({
        "role": "assistant", "content": "",
        "tool_calls": [{"id": "tc1", "type": "function",
                        "function": {"name": "read_file",
                                     "arguments": json.dumps({"path": "config.py"})}}],
    })
    msgs.append({"role": "tool", "tool_call_id": "tc1", "content": big_content})
    for t in range(2, 26):
        msgs.append({"role": "user", "content": f"unrelated task {t}"})
        msgs.append({"role": "assistant", "content": f"done {t}"})

    out = e.select_context([m for m in msgs])
    assert out is not None
    tool_msg = next(m for m in out if m.get("role") == "tool" and m.get("tool_call_id") == "tc1")
    content = str(tool_msg["content"])
    assert "SECRET_KEY" in content, \
        "distributed excerpt must not hide content past a fixed head cutoff"


def test_22b_excerpt_keeps_error_lines_outside_samples():
    """Live Codex test 2026-11-04: a FATAL line at 40% of a 300-line log fell between the head,
    middle and tail samples; the model then invented an answer. Error-looking lines must survive
    the excerpt, without making it larger."""
    from cmi.lifecycle import ToolResultLifecycle as LifecycleManager  # noqa: N814
    lines = [f"[{n:04d}] svc=api{n % 9} latency={n * 3 % 900}ms req=r01{n:04d}" for n in range(300)]
    lines[123] = "[0123] svc=billing FATAL needle=KIWI-5512 req=r010123"
    content = "\n".join(lines)
    for budget in (120, 300, 1000):
        ex = LifecycleManager._distributed_excerpt(content, budget)
        assert len(ex) <= budget
        assert "KIWI-5512" in ex, budget
        assert "[0000]" in ex                               # the head is still sampled
    assert "L124" in LifecycleManager._distributed_excerpt(content, 300)   # 1-based line number
    # Nothing salient: same excerpt as before (no behavior change for clean outputs).
    clean = "\n".join(lines[:123] + lines[124:])
    ex = LifecycleManager._distributed_excerpt(clean, 300)
    assert "[flagged]" not in ex and len(ex) <= 300
    assert LifecycleManager.criticality_of("exec", content) == 1.0
    assert LifecycleManager.criticality_of("exec", "thread 'main' panicked at src/x.rs") == 1.0


def test_23_batch_aggregation_works_for_any_tool_not_just_terminal():
    """The batch pass groups ANY run of small same-tool results — verified
    explicitly for search_files, not just terminal (the only tool that
    happened to repeat on a real benchmark session session). Uses content
    sizes representative of that real session (~150-250 chars/result) so
    the batch summary's own overhead has enough to compress."""
    e = make_engine()
    msgs = [{"role": "system", "content": "sys"}]
    msgs.append({"role": "user", "content": "Explore the repo"})
    for i in range(8):
        msgs.append({
            "role": "assistant", "content": "",
            "tool_calls": [{"id": f"sf{i}", "type": "function",
                            "function": {"name": "search_files",
                                         "arguments": json.dumps({"pattern": f"auth_helper_function_{i}"})}}],
        })
        msgs.append({"role": "tool", "tool_call_id": f"sf{i}",
                     "content": (
                         f"Found 3 matches for auth_helper_function_{i} in the codebase: "
                         f"src/auth/helpers.ts:{42+i}, src/middleware/verify.ts:{18+i}, "
                         f"tests/auth.spec.ts:{7+i}"
                     )})
    for t in range(2, 26):
        msgs.append({"role": "user", "content": f"unrelated task {t}"})
        msgs.append({"role": "assistant", "content": f"done {t}"})

    out = e.select_context([m for m in msgs])
    assert out is not None
    flat = json.dumps(out)
    assert "[CMI batch]" in flat
    # every original tool_call/tool_result pair still present
    ids_calls = {tc["id"] for m in out if m.get("role") == "assistant"
                for tc in (m.get("tool_calls") or [])}
    ids_results = {m.get("tool_call_id") for m in out if m.get("role") == "tool"}
    assert {f"sf{i}" for i in range(8)} <= ids_calls
    assert {f"sf{i}" for i in range(8)} <= ids_results


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))


# ------------------------------------------------ Determinism / perf contracts
def _narrated_history(n_turns=12):
    msgs = [{"role": "system", "content": "sys"}]
    for t in range(1, n_turns + 1):
        msgs.append({"role": "user", "content": f"turn {t}"})
        msgs.append({"role": "assistant",
                     "content": f"Let me look at the file for turn {t} and check what it does in detail.",
                     "tool_calls": [{"id": f"call_{t}", "type": "function",
                                     "function": {"name": "read_file", "arguments": json.dumps({"path": f"f{t}.py"})}}]})
        msgs.append({"role": "tool", "tool_call_id": f"call_{t}", "content": "x = 1\n" * 400})
        msgs.append({"role": "assistant", "content": f"done {t}"})
    return msgs


def test_24_same_history_same_output_every_call():
    """The compiled request must be byte-identical call after call on an unchanged history —
    anything else re-writes the cached prefix. Old narration stubs used to alternate
    stub/raw between consecutive calls."""
    e = make_engine()
    msgs = _narrated_history()
    outs = [json.dumps(e.select_context(msgs) or msgs, sort_keys=True) for _ in range(4)]
    assert len(set(outs)) == 1
    assert "[CMI: narration omitted]" in outs[0]


def test_25_input_never_mutated_and_untouched_messages_shared():
    e = make_engine()
    msgs = _narrated_history()
    snapshot = copy.deepcopy(msgs)
    out = e.select_context(msgs)
    assert msgs == snapshot
    assert out is not None and len(out) == len(msgs)
    # copy-on-write: user messages are never edited, so they must be the caller's objects
    assert all(o is m for o, m in zip(out, msgs) if m["role"] == "user")


def test_26_session_reset_clears_per_session_state():
    e = make_engine()
    e.select_context(build_history(n_turns=20, image_at=2))
    assert e._image_counted
    e.on_session_reset()
    assert not e._image_counted and not e._image_evicted_any


def test_27_live_config_sync_helper_matches_core():
    """tui_gateway calls agent.context_compressor._coerce_threshold_tokens_cap — CMI sits in that slot."""
    e = make_engine()
    assert e._coerce_threshold_tokens_cap(1234) == 1234
    assert e._coerce_threshold_tokens_cap(None) is None
    assert e._coerce_threshold_tokens_cap(0) is None
    assert e._coerce_threshold_tokens_cap("bad") is None


# ------------------------------------------------ Recall contracts (no silent loss)
def _read_history(path, n_turns=30, content=None):
    content = content or ("export function authenticate(user) {\n" + "  // line\n" * 600 + "}\n")
    msgs = [{"role": "system", "content": "sys"}, {"role": "user", "content": "start"}]
    msgs.append({"role": "assistant", "content": "", "tool_calls": [{"id": "call_read", "type": "function",
                 "function": {"name": "read_file", "arguments": json.dumps({"path": path})}}]})
    msgs.append({"role": "tool", "tool_call_id": "call_read", "content": content})
    for t in range(n_turns):
        msgs.append({"role": "user", "content": f"autre chose {t}"})
        msgs.append({"role": "assistant", "content": f"ok {t}"})
    return msgs, content


def test_28_user_names_old_file_full_content_same_request(monkeypatch):
    monkeypatch.setattr("cmi.engine.WARMUP_REQUESTS", 0)  # after the start-up phase a named file is sent in full
    """'reprends la modif de X' must give the model the FULL old read of X on the very first
    request of the turn, not a digest — whatever spelling the user uses for the path."""
    path = "/home/dev/project/src/auth/session.ts"
    for spelling in (path, "src/auth/session.ts", "session.ts"):
        e = make_engine()
        msgs, content = _read_history(path)
        e.on_turn_complete(msgs)
        before = e.select_context(msgs)
        assert before is not None and before[3]["content"] != content  # old read was digested
        turn = msgs + [{"role": "user", "content": f"reprends la modification de {spelling}"}]
        out = e.select_context(turn) or turn
        assert out[3]["content"] == content, spelling


def test_28b_many_named_files_are_capped(monkeypatch):
    """A message naming dozens of files must not pin every read raw (live test 2026-11-09: 125
    named paths -> 0% saved). The newest named reads stay full within HOT_FILE_TOKENS; the
    oldest are digested and keep their recall key."""
    monkeypatch.setattr("cmi.engine.WARMUP_REQUESTS", 0)
    monkeypatch.setattr("cmi.engine.HOT_FILE_TOKENS", 5000)
    paths = [f"/home/dev/project/src/mod_{i}.py" for i in range(12)]
    content = lambda i: "".join(f"def f{i}_{k}(x):\n    return x * {k}  # module {i}\n" for k in range(120))  # noqa: E731
    msgs = [{"role": "system", "content": "sys"}, {"role": "user", "content": "start"}]
    for i, p in enumerate(paths):
        msgs.append({"role": "assistant", "content": "", "tool_calls": [{"id": f"c{i}", "type": "function",
                     "function": {"name": "read_file", "arguments": json.dumps({"path": p})}}]})
        msgs.append({"role": "tool", "tool_call_id": f"c{i}", "content": content(i)})
    for t in range(30):
        msgs += [{"role": "user", "content": f"autre chose {t}"}, {"role": "assistant", "content": f"ok {t}"}]
    e = make_engine()
    e.on_turn_complete(msgs)
    e.select_context(msgs)
    turn = msgs + [{"role": "user", "content": "relis tout ça:\n" + "\n".join(paths)}]
    out = e.select_context(turn) or turn
    full = [i for i in range(12) if out[3 + 2 * i]["content"] == content(i)]
    assert full and len(full) < 12, full
    assert full == list(range(12 - len(full), 12))  # the newest ones
    assert 11 in full  # the newest is always kept, whatever the budget


def test_28c_single_named_file_over_budget_still_full(monkeypatch):
    monkeypatch.setattr("cmi.engine.WARMUP_REQUESTS", 0)
    monkeypatch.setattr("cmi.engine.HOT_FILE_TOKENS", 10)
    path = "/home/dev/project/src/auth/session.ts"
    e = make_engine()
    msgs, content = _read_history(path)
    e.on_turn_complete(msgs)
    e.select_context(msgs)
    turn = msgs + [{"role": "user", "content": f"reprends {path}"}]
    out = e.select_context(turn) or turn
    assert out[3]["content"] == content


def test_28d_named_set_fixed_for_the_turn(monkeypatch):
    """A named file read DURING the turn must not change which older named reads stay raw:
    a digest turning raw mid-turn rewrites the cache from that message on."""
    monkeypatch.setattr("cmi.engine.WARMUP_REQUESTS", 0)
    monkeypatch.setattr("cmi.engine.HOT_FILE_TOKENS", 1000)

    def body(tag, n):
        return "".join(f"def {tag}_{k}(x):\n    return x * {k}  # {tag}\n" for k in range(n))

    def call(i, p, c):
        return [{"role": "assistant", "content": "", "tool_calls": [{"id": f"c{i}", "type": "function",
                 "function": {"name": "read_file", "arguments": json.dumps({"path": p})}}]},
                {"role": "tool", "tool_call_id": f"c{i}", "content": c}]
    x, y, z = "/home/dev/p/src/x.py", "/home/dev/p/src/y.py", "/home/dev/p/src/z.py"
    cx, cy, cz = body("x", 60), body("y", 150), body("z", 8)   # ~535, ~1370, ~68 tokens
    msgs = [{"role": "system", "content": "sys"}, {"role": "user", "content": "start"}]
    msgs += call(0, x, cx) + call(1, y, cy)
    for t in range(30):
        msgs += [{"role": "user", "content": f"autre {t}"}, {"role": "assistant", "content": f"ok {t}"}]
    e = make_engine()
    e.on_turn_complete(msgs)
    e.select_context(msgs)
    turn = msgs + [{"role": "user", "content": f"regarde {x} {y} {z}"}]
    first = (e.select_context(turn) or turn)[3]["content"]
    for i in (9, 10):
        turn = turn + call(i, z, cz + "#" * i)
        assert (e.select_context(turn) or turn)[3]["content"] == first


def test_29_digest_names_the_file():
    """A digest must say WHICH file it stands for, so the model can re-read it."""
    path = "/home/dev/project/src/very/deeply/nested/module/name/handler.ts"
    e = make_engine()
    msgs, _ = _read_history(path)
    e.on_turn_complete(msgs)
    out = e.select_context(msgs)
    assert out is not None and path in out[3]["content"]


def test_30_no_marginal_digest():
    """Never swap content for a digest that saves little: a small read stays raw."""
    e = make_engine()
    msgs, content = _read_history("/p/a.py", content="x = 1\n" * 60)  # ~360 chars
    e.on_turn_complete(msgs)
    out = e.select_context(msgs) or msgs
    assert out[3]["content"] == content


def test_31_learning_is_incremental():
    """Feeding the whole history every turn (live behaviour) must not keep re-boosting old files."""
    e = make_engine()
    msgs, _ = _read_history("/p/old.py", n_turns=40)
    for k in range(5, len(msgs) + 1, 2):
        e.on_turn_complete(msgs[:k])
    assert e.working_set.score("/p/old.py") < 3.0



# ------------------------------------------------ Recall store (v1.2): nothing is ever lost
def _all_stub_keys(out):
    from cmi.store import KEY_RE
    keys = []
    for m in out:
        keys += KEY_RE.findall(json.dumps(m, ensure_ascii=False))
    return keys


def test_32_every_degradation_is_recallable_verbatim():
    """Every original CMI removes from a request comes back byte-identical through cmi_recall,
    whatever the pass (digest, write_file args, duplicate, batch, narration)."""
    e = make_engine()
    msgs = _narrated_history(12)
    big = "export const handler = 1;\n" * 400
    msgs.insert(2, {"role": "assistant", "content": "", "tool_calls": [{"id": "call_w", "type": "function",
                    "function": {"name": "write_file", "arguments": json.dumps({"path": "/p/h.ts", "content": big})}}]})
    msgs.insert(3, {"role": "tool", "tool_call_id": "call_w", "content": "written"})
    e.on_turn_complete(msgs)
    out = e.select_context(msgs)
    assert out is not None
    keys = _all_stub_keys(out)
    assert keys, "degraded content must carry recall keys"
    originals = set()
    for m in msgs:
        originals.add(e._content_text(m))
        for tc in m.get("tool_calls") or []:
            originals.update(v for v in json.loads(tc["function"]["arguments"]).values() if isinstance(v, str))
    for k in keys:
        got = e.handle_tool_call("cmi_recall", {"key": f"cmi:{k}"})
        assert got in originals, f"cmi:{k} must return an exact original"
    assert big in [e.handle_tool_call("cmi_recall", {"key": k}) for k in keys]


def test_33_no_store_no_degradation(monkeypatch):
    """If originals cannot be kept, CMI must leave the request untouched (never lose data)."""
    e = make_engine()
    monkeypatch.setattr(e.store, "put", lambda content: None)
    msgs, content = _read_history("/p/big.ts")
    e.on_turn_complete(msgs)
    out = e.select_context(msgs) or msgs
    assert out[3]["content"] == content


def test_34_recall_tool_is_exposed_and_rejects_bad_keys():
    e = make_engine()
    assert {s["name"] for s in e.get_tool_schemas()} == {"cmi_recall", "cmi_search"}
    assert "error" in json.loads(e.handle_tool_call("cmi_recall", {"key": "cmi:zzzz"}))
    assert "error" in json.loads(e.handle_tool_call("cmi_recall", {"key": "../../etc/passwd"}))



def test_35_store_integrity_privacy_and_bounds(tmp_path):
    """Keys never resolve to other content, originals are private, the store stays bounded."""
    import os, time
    from cmi.store import RecallStore, KEY_MIN
    s = RecallStore(tmp_path / "store")
    k1 = s.put("alpha")
    assert len(k1) >= KEY_MIN and s.get(f"cmi:{k1}") == "alpha"
    # Forged prefix collision: a different file already sits at alpha's short key.
    s2 = RecallStore(tmp_path / "store2")
    s2._ensure_dir()
    (tmp_path / "store2" / f"{k1}.txt").write_text("someone else")
    k2 = s2.put("alpha")
    assert k2 != k1 and k2.startswith(k1) and s2.get(k2) == "alpha"
    assert s2.get(k1) == "someone else"
    # Privacy
    assert os.name == "nt" or (os.stat(tmp_path / "store").st_mode & 0o777) == 0o700
    assert os.name == "nt" or (os.stat(s.path_of(k1)).st_mode & 0o777) == 0o600
    # Non-UTF-8-clean text survives byte for byte
    weird = "caf\u00e9 \ud83d\ude00 \x00 end"
    assert s.get(s.put(weird)) == weird
    # Bounds: oldest first beyond the byte cap, and expired entries
    for i in range(5):
        s.put("x" * 1000 + str(i))
    old = s.path_of(s.put("stale"))
    os.utime(old, (time.time() - 90 * 86400,) * 2)
    s.prune(days=30, max_bytes=3000)
    assert not old.exists()
    assert sum(p.stat().st_size for p in (tmp_path / "store").glob("*.txt")) <= 3000


def test_36_recall_rejects_paths_and_unknown_keys(tmp_path):
    from cmi.store import RecallStore
    s = RecallStore(tmp_path / "s")
    (tmp_path / "secret.txt").write_text("nope")
    for bad in ("../secret", "cmi:../../secret", "/etc/passwd", "cmi:ZZZZZZZZZZZZ", "", "cmi:"):
        assert s.get(bad) is None



# ------------------------------------------------ Supersession (v1.3)
def _read_then(event, path="/p/src/app.ts", turns_after=1, write_path=None, write_tool="patch",
               write_result="ok"):
    body = "export function a() {}\n" * 300
    msgs = [{"role": "system", "content": "sys"}, {"role": "user", "content": "go"},
            {"role": "assistant", "content": "", "tool_calls": [{"id": "r1", "type": "function",
             "function": {"name": "read_file", "arguments": json.dumps({"path": path})}}]},
            {"role": "tool", "tool_call_id": "r1", "content": body},
            {"role": "user", "content": "next"}]
    if event == "patch":
        msgs += [{"role": "assistant", "content": "", "tool_calls": [{"id": "w1", "type": "function",
                  "function": {"name": write_tool, "arguments": json.dumps({"path": write_path or path, "old_string": "a", "new_string": "b"})}}]},
                 {"role": "tool", "tool_call_id": "w1", "content": write_result}]
    elif event == "reread":
        msgs += [{"role": "assistant", "content": "", "tool_calls": [{"id": "r2", "type": "function",
                  "function": {"name": "read_file", "arguments": json.dumps({"path": path})}}]},
                 {"role": "tool", "tool_call_id": "r2", "content": body}]
    for t in range(turns_after):
        msgs += [{"role": "user", "content": f"q{t}"}, {"role": "assistant", "content": f"a{t}"}]
    return msgs, body


def test_37_read_before_a_patch_is_marked_outdated_and_recallable():
    e = make_engine()
    msgs, body = _read_then("patch")
    e.on_turn_complete(msgs)
    out = e.select_context(msgs)
    stub = out[3]["content"]
    assert "outdated" in stub and "/p/src/app.ts" in stub
    from cmi.store import KEY_RE
    assert e.handle_tool_call("cmi_recall", {"key": "cmi:" + KEY_RE.findall(stub)[0]}) == body


def test_38_superseded_only_by_completed_turns(monkeypatch):
    monkeypatch.setattr("cmi.engine.WARMUP_REQUESTS", 0)  # mechanics test: short session, no start-up phase
    """A read obsoleted inside the CURRENT turn stays as it is until the turn ends (cache):
    the read is old enough to be digested, but the patch that obsoletes it is in the live turn.
    (Forced refresh is opt-in since 1.11: CMI_SUPERSEDED_REFRESH=1.)"""
    from cmi import engine as engine_mod
    monkeypatch.setattr(engine_mod, "SUPERSEDED_FORCES_REFRESH", True)
    e = make_engine()
    body = "export function a() {}\n" * 300
    path = "/p/src/app.ts"
    msgs = [{"role": "system", "content": "sys"}, {"role": "user", "content": "go"},
            {"role": "assistant", "content": "", "tool_calls": [{"id": "r1", "type": "function",
             "function": {"name": "read_file", "arguments": json.dumps({"path": path})}}]},
            {"role": "tool", "tool_call_id": "r1", "content": body},
            {"role": "assistant", "content": "read it"}]
    for t in range(4):
        msgs += [{"role": "user", "content": f"q{t}"}, {"role": "assistant", "content": f"a{t}"}]
    msgs += [{"role": "user", "content": "apply the fix"},
             {"role": "assistant", "content": "", "tool_calls": [{"id": "w1", "type": "function",
              "function": {"name": "patch", "arguments": json.dumps({"path": path, "old_string": "a", "new_string": "b"})}}]},
             {"role": "tool", "tool_call_id": "w1", "content": "ok"}]
    e.on_turn_complete(msgs[:-3])
    out = e.select_context(msgs) or msgs
    assert "outdated" not in out[3]["content"]
    e.on_turn_complete(msgs)
    after = msgs + [{"role": "assistant", "content": "done"}, {"role": "user", "content": "next"}]
    out2 = e.select_context(after)
    assert "outdated" in out2[3]["content"]


def test_39_same_file_read_again_supersedes_the_earlier_copy():
    e = make_engine()
    msgs, body = _read_then("reread")
    e.on_turn_complete(msgs)
    out = e.select_context(msgs)
    assert "superseded" in out[3]["content"]
    assert out[6]["content"] == body  # the newest copy stays raw


def _outdated(msgs):
    e = make_engine()
    e.on_turn_complete(msgs)
    out = e.select_context(msgs) or msgs
    return "outdated" in out[3]["content"]


def test_40_failed_or_readonly_writes_do_not_supersede():
    """A failed patch leaves the file as read; a read-only tool whose name contains 'write' or
    'edit' changes nothing. Marking the read outdated hid the only correct view of the file."""
    assert not _outdated(_read_then("patch", write_result=json.dumps({"success": False, "error": "old_string not found"}))[0])
    assert not _outdated(_read_then("patch", write_result="Error: no match")[0])
    for tool in ("overwrite_check", "preview_edit", "rewrite_history"):
        assert not _outdated(_read_then("patch", write_tool=tool)[0])
    assert _outdated(_read_then("patch", write_result=json.dumps({"success": True}))[0])


def test_41_supersession_needs_the_same_file():
    """Different anchored paths are different files even with a shared suffix; a bare file name
    does not match an absolute path from another directory."""
    assert not _outdated(_read_then("patch", path="~/x/f.py", write_path="/x/f.py")[0])
    assert not _outdated(_read_then("patch", path="config.py", write_path="/p/b/config.py")[0])
    assert _outdated(_read_then("patch", path="/p/a/src/app.ts", write_path="src/app.ts")[0])


def test_42_cache_tiers_hold_with_per_request_clones():
    """Hermes sends fresh structural clones on every request. The compiled output must be the
    same as when the very same objects are passed (the tier memory was keyed by id() and never
    hit on clones, so every turn silently re-decided old messages and rewrote the cache)."""
    msgs = [{"role": "system", "content": "sys"}]
    for t in range(14):
        body = "\n".join(f"t{t} line {i}: value {i * 7 + t}" for i in range(150 + 40 * (t % 4)))
        msgs += [{"role": "user", "content": f"turn {t}"},
                 {"role": "assistant", "content": f"Let me run step {t}. " * 20, "tool_calls": [{"id": f"c{t}", "type": "function",
                  "function": {"name": "terminal", "arguments": json.dumps({"command": f"run {t}"})}}]},
                 {"role": "tool", "tool_call_id": f"c{t}", "content": body},
                 {"role": "assistant", "content": f"done {t}"}]
    users = [i for i, m in enumerate(msgs) if m["role"] == "user"]

    def replay(clone):
        e = make_engine()
        outs = []
        for u in users[1:]:
            e.on_turn_complete(copy.deepcopy(msgs[:u]) if clone else msgs[:u])
            for cut in (u + 1, u + 3):
                req = copy.deepcopy(msgs[:cut]) if clone else msgs[:cut]
                outs.append([json.dumps(m, sort_keys=True) for m in (e.select_context(req) or req)])
        return outs

    assert replay(clone=True) == replay(clone=False)


def test_43_store_survives_concurrent_writers_and_foreign_prune(tmp_path):
    import threading
    from cmi.store import RecallStore
    root = tmp_path / "s"
    data = "x" * 2_000_000
    keys, stores = [], [RecallStore(root) for _ in range(6)]
    ts = [threading.Thread(target=lambda s=s: keys.append(s.put(data))) for s in stores]
    [t.start() for t in ts]; [t.join() for t in ts]
    assert len(set(keys)) == 1 and None not in keys
    assert stores[0].get(keys[0]) == data
    # Another process prunes the file: the next put must restore it, not return a dead key.
    (root / f"{keys[0]}.txt").unlink()
    assert stores[1].put(data) == keys[0] and stores[1].get(keys[0]) == data


def test_44_store_refuses_symlinks(tmp_path):
    import os
    from cmi.store import RecallStore
    victim = tmp_path / "victim"; victim.mkdir()
    link = tmp_path / "store"; link.symlink_to(victim)
    assert RecallStore(link).put("secret") is None and not list(victim.iterdir())
    real = tmp_path / "real"; real.mkdir()
    secret = tmp_path / "secret.txt"; secret.write_text("PRIVATE")
    os.symlink(secret, real / "abababababab.txt")
    assert RecallStore(real).get("cmi:abababababab") is None


def test_45_shell_writes_are_detected():
    from cmi.engine import shell_written_paths as f
    cases = {
        "sed -i '' 's/a/b/' src/app.ts": ["src/app.ts"],
        "sed -i.bak -e 's/a/b/' -e 's/c/d/' a.py b.py": ["a.py", "b.py"],
        "sed -n 1,20p src/app.ts": [],
        "perl -pi -e 's/x/y/' /p/conf.yml": ["/p/conf.yml"],
        "echo hi > out.txt && cat x >> /tmp/log.txt 2>/dev/null": ["out.txt", "/tmp/log.txt"],
        "ls -la 2>&1 | tee build.log": ["build.log"],
        "cd /p && mv old.py new.py": ["old.py", "new.py"],
        "cp a.py /p/b.py": ["/p/b.py"],
        "git checkout -- engine.py store.py": ["engine.py", "store.py"],
        "grep -rn foo src | head": [],
        "FOO=1 sudo sed -i s/a/b/ /etc/hosts": ["/etc/hosts"],
        "python -c \"print('>x')\"": [],
        "echo 'unterminated": [],
    }
    for cmd, expected in cases.items():
        assert f(cmd) == expected, cmd


def test_45b_shell_write_parsing_is_cached_and_callers_get_their_own_list():
    from cmi import toolcalls
    toolcalls._shell_written_paths_cached.cache_clear()
    cmd = "sed -i '' 's/a/b/' src/app.ts && echo hi > out.txt"
    first = toolcalls.shell_written_paths(cmd)
    first.append("mutated by the caller")
    assert toolcalls.shell_written_paths(cmd) == ["src/app.ts", "out.txt"]
    info = toolcalls._shell_written_paths_cached.cache_info()
    assert (info.misses, info.hits) == (1, 1)
    assert toolcalls.shell_written_paths("echo 'unterminated") == []


def test_46_terminal_edit_supersedes_read_only_when_it_succeeded():
    def session(command, result):
        msgs, _ = _read_then("nothing")
        msgs = msgs[:4]
        msgs += [{"role": "user", "content": "edit it"},
                 {"role": "assistant", "content": "", "tool_calls": [{"id": "t1", "type": "function",
                  "function": {"name": "terminal", "arguments": json.dumps({"command": command})}}]},
                 {"role": "tool", "tool_call_id": "t1", "content": result},
                 {"role": "user", "content": "q0"}, {"role": "assistant", "content": "a0"}]
        return msgs
    ok = json.dumps({"output": "", "exit_code": 0})
    assert _outdated(session("sed -i '' 's/a/b/' /p/src/app.ts", ok))
    assert not _outdated(session("sed -i '' 's/a/b/' /p/src/app.ts", json.dumps({"output": "no such file", "exit_code": 1})))
    assert not _outdated(session("sed -n 1,5p /p/src/app.ts", ok))
    assert not _outdated(session("sed -i '' 's/a/b/' /q/src/app.ts", ok))


def _needle_session(question, needle_line="WEBHOOK_RETRY_LIMIT = 7"):
    msgs = [{"role": "system", "content": "sys"}]
    for t in range(10):
        body = "\n".join(f"t{t} row {i}: filler value {i * 3}" for i in range(300))
        if t == 1:
            body = body.replace("t1 row 150: filler value 450", needle_line)
        msgs += [{"role": "user", "content": f"turn {t}"},
                 {"role": "assistant", "content": "", "tool_calls": [{"id": f"c{t}", "type": "function",
                  "function": {"name": "terminal", "arguments": json.dumps({"command": f"run {t}"})}}]},
                 {"role": "tool", "tool_call_id": f"c{t}", "content": body},
                 {"role": "assistant", "content": f"done {t}"}]
    msgs.append({"role": "user", "content": question})
    e = make_engine()
    e.on_turn_complete(msgs[:-1])
    return e, (e.select_context(msgs) or msgs)


def test_47_proactive_lexical_recall():
    """A rare identifier quoted by the user brings its old result back whole, no tool call."""
    e, out = _needle_session("what is WEBHOOK_RETRY_LIMIT set to?")
    assert "WEBHOOK_RETRY_LIMIT = 7" in out[7]["content"] and "[CMI" not in out[7]["content"]
    # Without the identifier the same old result stays digested.
    e2, out2 = _needle_session("what is the retry limit set to?")
    assert "[CMI" in out2[7]["content"]
    # A common word is not a key: every result contains 'filler'.
    e3, out3 = _needle_session("show `filler value` again")
    assert all("[CMI" in out3[i]["content"] for i in (3, 7, 11))


def test_48_distinctive_terms():
    from cmi.engine import distinctive_terms as d
    assert set(d("change WEBHOOK_SECRET and dunningDays in `config.prod.yml`")) == {"config.prod.yml", "WEBHOOK_SECRET", "dunningDays"}
    assert d("ok thanks, go on") == []
    assert d("order 1234567 failed") == ["1234567"]


def test_49_savings_ledger_and_report(tmp_path):
    """Every completed turn appends its savings; /cmi aggregates them per period."""
    from cmi.stats import StatsLedger, report
    e, out = _needle_session("what is the retry limit set to?")
    e.stats.set_path(tmp_path / "cmi_stats.jsonl")
    e._session_id = "s1"
    e.on_turn_complete(None)
    rows = list(e.stats.rows())
    assert len(rows) == 1 and rows[0]["n"] == 1 and rows[0]["raw"] > rows[0]["out"] > 0
    e.on_turn_complete(None)  # nothing new: no empty row
    assert len(list(e.stats.rows())) == 1
    text = report(StatsLedger(tmp_path / "cmi_stats.jsonl"), "s1")
    assert "This session | 1 |" in text and "Last 30 days | 1 |" in text  # English by default
    assert "économies" not in text
    fr = report(StatsLedger(tmp_path / "cmi_stats.jsonl"), "s1", lang="fr")
    assert "Cette session | 1 |" in fr and "30 derniers jours | 1 |" in fr  # French is opt-in
    assert rows[0]["cr"] > 0 and rows[0]["cc"] > 0


def test_49b_ledger_row_records_the_running_version(tmp_path, monkeypatch):
    """Each ledger row says which CMI ran it, and /cmi says when the files on disk are another
    version: an update without a restart kept old code running for hours, unseen (2026-11-11)."""
    import cmi
    from cmi import versioning
    from cmi.stats import StatsLedger, report
    led = StatsLedger(tmp_path / "cmi_stats.jsonl")
    led.add("s1", "m", 100, 60, 1)
    assert list(led.rows())[0]["v"] == cmi.__version__
    text = report(led, "s1")
    assert f"CMI {cmi.__version__}" in text and "restart" not in text
    disk = tmp_path / "__init__.py"
    disk.write_text('"""doc"""\n__version__ = "9.9.9"\n', encoding="utf-8")
    monkeypatch.setattr(versioning, "_INIT", disk)
    assert versioning.stale_install() == "9.9.9"
    text = report(led, "s1")
    assert f"CMI 9.9.9 is installed, but this process still runs {cmi.__version__}" in text
    assert "hermes gateway restart" in text and "This session | 1 |" in text
    assert "9.9.9 est installé" in report(led, "s1", lang="fr")
    from cmi.engine import CMIEngine
    st = CMIEngine().get_status()["cmi"]
    assert st["version"] == cmi.__version__ and st["installed_version"] == "9.9.9"
    monkeypatch.setattr(versioning, "_INIT", tmp_path / "missing.py")  # unreadable: never a false alarm
    assert versioning.stale_install() is None and "restart" not in report(led, "s1")


def _run_session(commands, outputs, turns_after=4):
    msgs = [{"role": "system", "content": "sys"}]
    for t, (cmd, out) in enumerate(zip(commands, outputs)):
        msgs += [{"role": "user", "content": f"turn {t}"},
                 {"role": "assistant", "content": "", "tool_calls": [{"id": f"c{t}", "type": "function",
                  "function": {"name": "terminal", "arguments": json.dumps({"command": cmd})}}]},
                 {"role": "tool", "tool_call_id": f"c{t}", "content": json.dumps({"output": out, "exit_code": 0})},
                 {"role": "assistant", "content": f"done {t}"}]
    for t in range(turns_after):
        msgs += [{"role": "user", "content": f"q{t}"}, {"role": "assistant", "content": f"a{t}"}]
    msgs.append({"role": "user", "content": "next"})
    e = make_engine()
    e.on_turn_complete(msgs[:-1])
    return e, (e.select_context(msgs) or msgs)


def test_50_rerun_command_supersedes_its_old_output():
    """pytest run twice: the first output is stale (and stays recallable); a different command
    or a first run is untouched. A trailing `| tail -N` is the same run."""
    fail = "\n".join(f"test_{i} FAILED assert {i} == {i + 1}" for i in range(120))
    ok = "\n".join(f"test_{i} PASSED" for i in range(120))
    e, out = _run_session(["cd /p && pytest -q 2>&1 | tail -80", "cd /p &&  pytest -q", "cd /p && pytest -q tests/a.py"],
                          [fail, ok, ok])
    assert "superseded: the same command was run again" in out[3]["content"]
    assert "rerun" not in out[7]["content"] and "superseded" not in out[11]["content"]
    from cmi.engine import command_key
    assert command_key("npm test 2>&1 | tail -n 50") == command_key("npm  test") != command_key("npm test -- a")


def test_51_background_start_is_not_a_snapshot():
    e = make_engine()
    msgs = [{"role": "system", "content": "s"}]
    for t in range(2):
        msgs += [{"role": "user", "content": f"t{t}"},
                 {"role": "assistant", "content": "", "tool_calls": [{"id": f"b{t}", "type": "function",
                  "function": {"name": "terminal", "arguments": json.dumps({"command": "npm run dev", "background": True})}}]},
                 {"role": "tool", "tool_call_id": f"b{t}", "content": json.dumps({"session_id": f"proc_{t}", "exit_code": 0})},
                 {"role": "assistant", "content": "ok"}]
    msgs.append({"role": "user", "content": "x"})
    assert e._superseded(msgs, len(msgs) - 1) == {}


def test_52_cost_shadow_prices_cache_reads_and_rewrites():
    """The shadow bills repeated prefixes as cache reads and changed ones as writes, for both
    the raw and the CMI stream, so /cmi can show the real (cache-aware) cost difference."""
    from cmi.cost_guard import CostShadow
    c = CostShadow()
    raw, cmi = [], []
    for i in range(20):
        raw.append(i)
        cmi = [1000 + i * 10 + k for k in range(len(raw))]  # CMI rewrites its whole prefix
        c.observe(raw, [100] * len(raw), cmi, [90] * len(cmi))
    assert c.saving_ratio() < 0
    c2 = CostShadow()
    for i in range(20):
        ids = list(range(i + 1))
        c2.observe(ids, [100] * len(ids), ids, [60] * len(ids))
    assert 0.3 < c2.saving_ratio() < 0.5


def test_53_no_cache_premium_always_tiers():
    """On providers without a cache write premium, a tier costs nothing: always worth it."""
    e = make_engine()
    e.cost.read = e.cost.write = 1.0
    assert e._tier_pays_back(delta=1, rewrite=10 ** 6)
    e.cost.read, e.cost.write = 0.1, 1.25
    assert not e._tier_pays_back(delta=1, rewrite=10 ** 6)
    assert not e._tier_pays_back(delta=10 ** 5, rewrite=10 ** 5)  # brand-new session: no tier yet
    e._requests = 40  # a session that has already run 40 requests (expected ~20 more)
    assert e._tier_pays_back(delta=10 ** 5, rewrite=10 ** 5)


def test_54_cross_session_search_and_recall(tmp_path):
    """Session A runs a command; session B (new engine, same home) finds it by an identifier
    and a fragment, and recalls it verbatim."""
    home = tmp_path / "home"
    out_a = "\n".join(f"route /api/v{i} p99={i * 3}ms" for i in range(60)) + "\nroute /checkout p99=412ms WEBHOOK_MAX_RETRIES=7"
    a = make_engine()
    a.on_session_start("sess-A", hermes_home=str(home))
    msgs = [{"role": "system", "content": "s"}, {"role": "user", "content": "measure"},
            {"role": "assistant", "content": "", "tool_calls": [{"id": "t1", "type": "function",
             "function": {"name": "terminal", "arguments": json.dumps({"command": "./bench.sh --prod"})}}]},
            {"role": "tool", "tool_call_id": "t1", "content": out_a},
            {"role": "assistant", "content": "done"}]
    a.on_turn_complete(msgs)
    b = make_engine()
    b.on_session_start("sess-B", hermes_home=str(home))
    for q in ("checkout p99", "WEBHOOK_MAX", "bench prod"):
        res = json.loads(b.handle_tool_call("cmi_search", {"query": q}))["results"]
        assert res and res[0]["session"] == "sess-A", q
    hit = json.loads(b.handle_tool_call("cmi_search", {"query": "checkout p99"}))["results"][0]
    assert hit["label"] == "terminal: ./bench.sh --prod" and "412ms" in hit["snippet"]
    assert b.handle_tool_call("cmi_recall", {"key": hit["key"]}) == out_a
    assert json.loads(b.handle_tool_call("cmi_search", {"query": "zzzqqq"}))["results"] == []


def test_55_index_skips_small_and_forgets_pruned(tmp_path):
    from cmi.index import OutputIndex
    from cmi.store import RecallStore
    home = tmp_path / "h"
    store = RecallStore(home / "cmi_store")
    ix = OutputIndex(home / "cmi_index.db")
    big = "alpha beta gamma " * 100
    k = store.put(big)
    ix.add(k, big, session="s", tool="terminal", label="terminal: x")
    assert ix.search("gamma")
    (home / "cmi_store" / f"{k}.txt").unlink()
    assert ix.search("gamma", exists=lambda key: store.get(key) is not None) == []
    assert not ix.has(k)
    e = make_engine()
    e.on_session_start("s", hermes_home=str(home))
    e.on_turn_complete([{"role": "user", "content": "x"},
                        {"role": "assistant", "content": "", "tool_calls": [{"id": "a", "type": "function",
                         "function": {"name": "terminal", "arguments": "{}"}}]},
                        {"role": "tool", "tool_call_id": "a", "content": "short output"}])
    assert json.loads(e.handle_tool_call("cmi_search", {"query": "short output"}))["results"] == []


# ---------------------------------------------------------------- Test 58-60: credentials
def _fake_key():
    # Built from parts: secret scanners flag literal key-shaped strings in source files.
    return "sk-" + "ant-" + "api03-" + "Zx9" * 12


def test_58_secret_never_written_to_disk(tmp_path):
    from cmi.store import RecallStore
    from cmi.secrets import contains_secret
    st = RecallStore(tmp_path / "store")
    env = "DATABASE_URL=postgres://x\nANTHROPIC_API_KEY=" + _fake_key() + "\n" + "pad\n" * 500
    assert contains_secret(env)
    assert st.put(env) is None and st.put(env) is None
    assert not list((tmp_path / "store").glob("*.txt"))
    assert st.put("plain build log\n" * 100)  # ordinary content still stored


def test_59_secret_detector_precision():
    from cmi.secrets import contains_secret
    pem = "-----BEGIN " + "RSA PRIVATE KEY-----\nMIIE..."
    assert contains_secret(pem)
    assert contains_secret("export GITHUB_TOKEN=" + "gh" + "p_" + "a1B2" * 9)
    assert contains_secret("STRIPE_SECRET_KEY='" + "abc123XYZ" * 3 + "'")
    for benign in ("token_count = 1234", "max_tokens: 4096", "password field is required",
                   "sk-learn is a library", "SECRET_SAUCE=yes", "commit 1a2b3c4d5e6f7a8b9c0d"):
        assert not contains_secret(benign), benign


def test_60_secret_result_stays_raw_and_purge(tmp_path):
    from cmi.store import RecallStore
    key_line = "OPENAI_API_KEY=" + "sk-" + "proj-" + "Qw7" * 14 + "\n"
    secret_out = "export function authenticate(user) {\n" + "  // line\n" * 300 + key_line + "  // line\n" * 300 + "}\n"
    plain_out = secret_out.replace(key_line, "OPENAI_MODEL=gpt\n")
    for content, shortened in ((plain_out, True), (secret_out, False)):  # control, then credential
        e = CMIEngine()
        home = tmp_path / ("plain" if shortened else "secret")
        e.on_session_start("s", hermes_home=str(home))
        e.update_model(model="m", context_length=200_000)
        msgs, _ = _read_history("/srv/app/.env", content=content)
        e.on_turn_complete(msgs)
        out = e.select_context(msgs) or msgs
        assert (out[3]["content"] != content) is shortened
        files = list((home / "cmi_store").glob("*.txt")) if (home / "cmi_store").exists() else []
        assert not any("Qw7Qw7" in p.read_text() for p in files)   # never on disk (store)
        assert files or not shortened                                 # the control did store
        for db in home.glob("cmi_index.db*"):
            assert b"Qw7Qw7" not in db.read_bytes()                  # nor in the search index
    # Stores written before the guard get purged.
    store = tmp_path / "old" / "cmi_store"
    st = RecallStore(store)
    keep = st.put("harmless\n" * 50)
    (store / "aaaaaaaaaaaa.txt").write_text(secret_out)
    assert st.purge_secrets() == 1
    assert not (store / "aaaaaaaaaaaa.txt").exists() and st.get(keep)


def test_61_index_refuses_secret_in_command_label(tmp_path):
    from cmi.index import OutputIndex
    tok = "gh" + "p_" + "a1B2" * 9
    ix = OutputIndex(tmp_path / "cmi_index.db")
    assert not ix.add("k1" * 6, "build ok\n" * 100, label=f"terminal: curl -H 'Authorization: token {tok}' api")
    assert ix.add("k2" * 6, "build ok\n" * 100, label="terminal: make build")
    assert [r["key"] for r in ix.search("build")] == ["k2" * 6]


def test_62_evicted_image_is_kept_as_image_file(tmp_path):
    """Zero loss for screenshots too: the evicted image is saved as an image file, its key is in
    the stub, cmi_recall points to it; an image that cannot be decoded is never evicted."""
    import base64
    from cmi.store import KEY_RE
    png = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==")
    e = make_engine()
    e.on_session_start("s", hermes_home=str(tmp_path))
    msgs = build_history(30, image_at=5)
    for m in msgs:
        for p in m.get("content") or [] if isinstance(m.get("content"), list) else []:
            for ip in (p.get("content") if isinstance(p.get("content"), list) else [p]):
                if isinstance(ip, dict) and ip.get("type") == "image":
                    ip["source"]["data"] = base64.b64encode(png).decode()
    out = e.select_context(msgs)
    stub = next(s for s in re.findall(r"\[image evicted by CMI[^\]]*\]", json.dumps(out)))
    key = KEY_RE.search(stub).group(1)
    assert e.store.image_path(key).read_bytes() == png
    assert str(e.store.image_path(key)) in e.handle_tool_call("cmi_recall", {"key": f"cmi:{key}"})
    # undecodable image: kept
    bad = build_history(30, image_at=5)
    for m in bad:
        for p in m.get("content") or [] if isinstance(m.get("content"), list) else []:
            for ip in (p.get("content") if isinstance(p.get("content"), list) else [p]):
                if isinstance(ip, dict) and ip.get("type") == "image":
                    ip["source"]["data"] = "not base64 !!"
    out2 = make_engine().select_context(bad) or bad
    assert '"type": "image"' in json.dumps(out2)


def test_63_user_pasted_image_never_evicted():
    img = {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "x" * 1000}}
    msgs = [{"role": "user", "content": [{"type": "text", "text": "make it look like this"}, img]}]
    for t in range(30):
        msgs += [{"role": "assistant", "content": f"ok {t}"}, {"role": "user", "content": f"next {t}"}]
    out = make_engine().select_context(msgs) or msgs
    assert out[0] == msgs[0]


def test_one_hour_cache_prices_writes_at_2x():
    """Claude Code asks for a 1-hour cache: writes cost 2x input there, not 1.25x. A tier priced
    at the 5-minute rate under-estimates its rewrite and can end costlier (seen live)."""
    e = make_engine()
    assert e.cost.write == pytest.approx(1.25)
    e.cache_ttl_s = 3600
    assert e.cost.write == pytest.approx(2.0)
    e.cache_ttl_s = 300
    assert e.cost.write == pytest.approx(1.25)
    e.cache_ttl_s = 3600
    e.cost.set_pricing("unknown-model")  # pricing reloaded: the 1 h TTL still applies
    assert e.cost.write == pytest.approx(2.0)


def test_one_hour_cache_tightens_the_payback_rule():
    e = make_engine()
    e._requests = 100
    delta, rewrite = 4000, 10000  # 0.1*35*4000 = 14000: pays back at 1.25x (11500), not at 2x (19000)
    assert e._tier_pays_back(delta, rewrite)
    e.cache_ttl_s = 3600
    assert not e._tier_pays_back(delta, rewrite)


def test_22c_excerpt_keeps_one_line_per_distinct_error_template():
    """240 repeats of one warning-style error must not crowd out the one different failure at the
    bottom (a lint run with 3 errors among 240 warnings): one flagged line per template, with a count."""
    from cmi.lifecycle import ToolResultLifecycle as LifecycleManager  # noqa: N814
    noisy = [f"src/a{n}.py:{n}:1: error: unused import 'os{n}' (0x{n:04x})" for n in range(240)]
    rare = ["tests/test_x.py:9: FAILED assertion boom", "Traceback (most recent call last):"]
    content = "\n".join(noisy + ["ok line"] * 40 + rare)
    ex = LifecycleManager._distributed_excerpt(content, 400)
    assert len(ex) <= 400
    assert "FAILED assertion boom" in ex and "Traceback" in ex
    assert "(x240)" in ex  # the repeated template shows once, with its count
    assert ex.count("unused import") <= 4  # the 3 samples plus the flagged template, not 240 lines
