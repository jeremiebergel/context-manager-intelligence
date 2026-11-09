"""Wire formats through the proxy compile path: Anthropic Messages (parallel tool results,
thinking signatures, cache_control) and OpenAI Responses (Codex CLI: reasoning items,
function/custom calls). The rule under test: only what the engine changed goes back; every
other byte is the client's."""
import copy
import json

from cmi import proxy
from cmi.wire import to_canonical

BIG = "export function f() {\n" + "  // line\n" * 600 + "}\n"


def _sessions(tmp_path):
    return proxy.Sessions(str(tmp_path))


def _anthropic_body():
    sig = {"type": "thinking", "thinking": "plan: read both", "signature": "EqQBCkYIBBgCKkD" + "x" * 40}
    msgs = [
        {"role": "user", "content": "start"},
        {"role": "assistant", "content": [sig,
            {"type": "tool_use", "id": "tu_a", "name": "Read", "input": {"file_path": "/p/old_a.py"}},
            {"type": "tool_use", "id": "tu_b", "name": "Read", "input": {"file_path": "/p/hot_b.py"}}]},
        {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "tu_a", "content": BIG.replace("f()", "a()")},
            {"type": "tool_result", "tool_use_id": "tu_b", "content": BIG.replace("f()", "b()"),
             "cache_control": {"type": "ephemeral"}},
            {"type": "text", "text": "<system-reminder>harness note</system-reminder>"}]},
    ]
    for t in range(30):
        msgs += [{"role": "user", "content": f"other task {t}"}, {"role": "assistant", "content": f"ok {t}"}]
    msgs.append({"role": "user", "content": "go back to hot_b.py and fix it"})
    return {"model": "claude-sonnet-5", "system": [{"type": "text", "text": "sys"}], "messages": msgs,
            "max_tokens": 1000}


def test_w1_anthropic_parallel_results_decided_per_tool(tmp_path):
    body = _anthropic_body()
    orig = copy.deepcopy(body)
    new, stats = proxy.compile_request(_sessions(tmp_path), "/v1/messages", body)
    assert body == orig                                    # input never mutated
    assert stats["changed"]
    parts = new["messages"][2]["content"]
    assert len(parts) == 3 and [p.get("tool_use_id") for p in parts[:2]] == ["tu_a", "tu_b"]
    assert parts[0]["content"].startswith("[CMI")         # cold read: digested, once
    assert parts[1]["content"] == BIG.replace("f()", "b()")  # named again by the user: raw
    assert parts[1]["cache_control"] == {"type": "ephemeral"}
    assert parts[2] == orig["messages"][2]["content"][2]   # harness text block untouched
    assert new["messages"][1]["content"][0] == orig["messages"][1]["content"][0]  # signature intact
    assert new["system"] == orig["system"] and new["max_tokens"] == 1000
    assert json.dumps(new).count("[CMI") == 1              # the digest is not duplicated


def test_w2_anthropic_deterministic_and_identity(tmp_path):
    s = _sessions(tmp_path)
    a, _ = proxy.compile_request(s, "/v1/messages", _anthropic_body())
    b, _ = proxy.compile_request(s, "/v1/messages", _anthropic_body())
    assert json.dumps(a) == json.dumps(b)                  # byte-stable: prompt cache safe
    short = {"model": "m", "messages": [{"role": "user", "content": "hi"}]}
    out, stats = proxy.compile_request(s, "/v1/messages", short)
    assert out is short and not stats["changed"]


def test_w3_session_key_ignores_moving_cache_breakpoints():
    body = _anthropic_body()
    m1 = body["messages"]
    m2 = copy.deepcopy(m1)
    m2[0] = {"role": "user", "content": [{"type": "text", "text": "start", "cache_control": {"type": "ephemeral"}}]}
    m1[0] = {"role": "user", "content": [{"type": "text", "text": "start"}]}
    assert proxy.Sessions.key("/v1/messages", body, m1) == proxy.Sessions.key("/v1/messages", body, m2)


def test_w3b_same_first_prompt_new_conversation_gets_its_own_engine(tmp_path):
    """Live test 2026-11-04: two Claude Code runs with the same first prompt shared one engine;
    the second ran with the first one's learned state and was never compressed (0%)."""
    s = _sessions(tmp_path)
    a = _anthropic_body()
    _, st_a = proxy.compile_request(s, "/v1/messages", a)
    b = copy.deepcopy(a)
    b["messages"][1]["content"][1]["id"] = "tu_x"          # another run: other tool ids
    b["messages"][2]["content"][0]["tool_use_id"] = "tu_x"
    ka = proxy.Sessions.key("/v1/messages", a, a["messages"])
    kb = proxy.Sessions.key("/v1/messages", b, b["messages"])
    assert ka != kb
    _, st_b = proxy.compile_request(s, "/v1/messages", b)
    assert st_a["changed"] and st_b["changed"]
    # Old-turn thinking dropped by the client later in the same run: still the same session.
    c = copy.deepcopy(a)
    c["messages"][1]["content"] = c["messages"][1]["content"][1:]
    assert proxy.Sessions.key("/v1/messages", c, c["messages"]) == ka
    # The very first request (no assistant turn yet) does not split two runs before they differ.
    first = {"model": a["model"], "system": a["system"], "messages": a["messages"][:1]}
    assert proxy.Sessions.key("/v1/messages", first, first["messages"]) != ka
    resp = _responses_body()
    resp2 = copy.deepcopy(resp)
    resp2["input"][3]["call_id"] = resp2["input"][4]["call_id"] = "call_9"
    assert proxy.Sessions.key("/v1/responses", resp, to_canonical("responses", resp)[0]) != \
        proxy.Sessions.key("/v1/responses", resp2, to_canonical("responses", resp2)[0])


def _responses_body():
    items = [
        {"type": "message", "role": "developer", "content": [{"type": "input_text", "text": "be brief"}]},
        {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "start"}]},
        {"type": "reasoning", "id": "rs_1", "summary": [], "encrypted_content": "gAAAAB" + "z" * 60},
        {"type": "function_call", "call_id": "call_1", "name": "shell",
         "arguments": json.dumps({"command": ["cat", "/p/old_a.py"]})},
        {"type": "function_call_output", "call_id": "call_1", "output": BIG},
    ]
    for t in range(30):
        items += [{"type": "message", "role": "user", "content": [{"type": "input_text", "text": f"task {t}"}]},
                  {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": f"ok {t}"}]}]
    items.append({"type": "message", "role": "user", "content": [{"type": "input_text", "text": "next"}]})
    return {"model": "gpt-5-codex", "instructions": "you are codex", "input": items, "store": False,
            "include": ["reasoning.encrypted_content"]}


def test_w4_responses_shortens_old_output_keeps_reasoning(tmp_path):
    body = _responses_body()
    orig = copy.deepcopy(body)
    new, stats = proxy.compile_request(_sessions(tmp_path), "/v1/responses", body)
    assert body == orig and stats["changed"] and stats["route"] == "responses"
    assert len(new["input"]) == len(orig["input"])
    out_item = new["input"][4]
    assert out_item["type"] == "function_call_output" and out_item["call_id"] == "call_1"
    assert out_item["output"].startswith("[CMI") and "cmi:" in out_item["output"]
    for k in (0, 1, 2, 3):                                 # instructions, user, reasoning, call
        assert new["input"][k] == orig["input"][k]
    assert all(new["input"][k] == orig["input"][k] for k in range(5, len(orig["input"])))
    assert {k: v for k, v in new.items() if k != "input"} == {k: v for k, v in orig.items() if k != "input"}


def test_w5_responses_server_side_history_passthrough(tmp_path):
    body = _responses_body()
    body["previous_response_id"] = "resp_123"
    out, stats = proxy.compile_request(_sessions(tmp_path), "/v1/responses", body)
    assert out is body and not stats["changed"]
    s_body = {"model": "m", "input": "just a string"}
    assert proxy.compile_request(_sessions(tmp_path), "/v1/responses", s_body)[0] is s_body


def test_w6_responses_custom_tool_roundtrip():
    patch = "*** Begin Patch\n*** Update File: a.py\n@@\n-x\n+y\n*** End Patch"
    body = {"input": [{"type": "message", "role": "user", "content": "go"},
                      {"type": "custom_tool_call", "call_id": "c9", "name": "apply_patch", "input": patch},
                      {"type": "custom_tool_call_output", "call_id": "c9", "output": "Done"}]}
    canon, back = to_canonical("responses", body)
    assert canon[1]["tool_calls"][0]["function"]["name"] == "apply_patch"
    assert json.loads(canon[1]["tool_calls"][0]["function"]["arguments"]) == {"input": patch}
    assert back(copy.deepcopy(canon)) is None              # unchanged canon -> no rewrite
    edited = copy.deepcopy(canon)
    edited[1]["tool_calls"][0]["function"]["arguments"] = json.dumps({"input": "[CMI args] cmi:abc"})
    assert back(edited)["input"][1]["input"] == "[CMI args] cmi:abc"


def test_w6b_responses_list_output_rewritten():
    """Codex >= 0.164 sends tool outputs as a list of input_text blocks, not a string: CMI must
    still write its compiled text back (it used to compute it, then silently drop it)."""
    blocks = [{"type": "input_text", "text": "Script completed\nOutput:\n"},
              {"type": "input_text", "text": "line\n" * 50}]
    body = {"input": [{"type": "message", "role": "user", "content": "go"},
                      {"type": "custom_tool_call", "call_id": "c1", "name": "exec", "input": "cat a"},
                      {"type": "custom_tool_call_output", "id": "o1", "call_id": "c1", "output": blocks}]}
    canon, back = to_canonical("responses", body)
    edited = copy.deepcopy(canon)
    edited[2]["content"] = "[CMI digest] cmi:abc"
    new = back(edited)
    assert new["input"][2]["output"] == [{"type": "input_text", "text": "[CMI digest] cmi:abc"}]
    assert {k: v for k, v in new["input"][2].items() if k != "output"} == \
        {k: v for k, v in body["input"][2].items() if k != "output"}
    assert body["input"][2]["output"] is blocks                      # original not mutated
    # A list carrying anything but text (an image) is left untouched: no silent loss.
    img = blocks + [{"type": "input_image", "image_url": "data:image/png;base64,AAAA"}]
    body2 = copy.deepcopy(body)
    body2["input"][2]["output"] = img
    canon2, back2 = to_canonical("responses", body2)
    edited2 = copy.deepcopy(canon2)
    edited2[2]["content"] = "[CMI digest] cmi:abc"
    assert back2(edited2) is None


# ---- Host tool vocabularies: what makes recall/supersession work outside Hermes --------------

def _run(engine_msgs):
    from cmi import CMIEngine
    e = CMIEngine()
    e.update_model(model="m", context_length=200_000)
    e.on_turn_complete(engine_msgs[:-1])
    return e.select_context(engine_msgs) or engine_msgs


def _turns(n):
    out = []
    for t in range(n):
        out += [{"role": "user", "content": f"task {t}"}, {"role": "assistant", "content": f"ok {t}"}]
    return out


def test_w7_claude_code_edit_supersedes_read():
    """Claude Code: Read(file_path) then a successful Edit(file_path) -> the old read is outdated."""
    msgs = [{"role": "user", "content": "start"},
            {"role": "assistant", "content": [{"type": "tool_use", "id": "r1", "name": "Read",
                                               "input": {"file_path": "/repo/src/app.ts"}}]},
            {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "r1", "content": BIG}]},
            {"role": "assistant", "content": [{"type": "tool_use", "id": "e1", "name": "Edit",
             "input": {"file_path": "/repo/src/app.ts", "old_string": "a", "new_string": "b"}}]},
            {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "e1",
                                          "content": "The file /repo/src/app.ts has been updated."}]},
            *_turns(3), {"role": "user", "content": "next"}]
    out = _run(msgs)
    assert "outdated" in json.dumps(out[2])


def test_w8_codex_argv_rerun_and_apply_patch():
    """Codex: shell argv ["bash","-lc",cmd] reruns supersede; apply_patch headers mark reads modified."""
    def call(cid, name, args):
        return {"role": "assistant", "content": "", "tool_calls": [{"id": cid, "type": "function",
                "function": {"name": name, "arguments": json.dumps(args)}}]}
    log = "PASS test_a\n" * 400
    patch = "*** Begin Patch\n*** Update File: src/a.py\n@@\n-x\n+y\n*** End Patch"
    msgs = [{"role": "user", "content": "start"},
            call("c1", "shell", {"command": ["bash", "-lc", "pytest -q"]}),
            {"role": "tool", "tool_call_id": "c1", "content": log},
            call("c2", "read_file", {"path": "src/a.py"}),
            {"role": "tool", "tool_call_id": "c2", "content": BIG},
            call("c3", "apply_patch", {"input": patch}),
            {"role": "tool", "tool_call_id": "c3", "content": "Success. Updated the following files:\nM src/a.py"},
            call("c4", "shell", {"command": ["bash", "-lc", "pytest -q 2>&1 | tail -5"]}),
            {"role": "tool", "tool_call_id": "c4", "content": log},
            *_turns(3), {"role": "user", "content": "next"}]
    out = _run(msgs)
    assert "same command was run again" in out[2]["content"]
    assert "outdated" in out[4]["content"]


def test_w9_claude_code_old_edit_write_payloads_shortened_and_recallable(tmp_path):
    from cmi import CMIEngine
    from cmi.store import KEY_RE
    old, new_s, body = "a" * 900, "b" * 900, "c" * 3000
    msgs = [{"role": "user", "content": "start"},
            {"role": "assistant", "content": [
                {"type": "tool_use", "id": "w1", "name": "Write", "input": {"file_path": "/r/x.ts", "content": body}},
                {"type": "tool_use", "id": "e1", "name": "Edit",
                 "input": {"file_path": "/r/y.ts", "old_string": old, "new_string": new_s}},
                {"type": "tool_use", "id": "m1", "name": "MultiEdit",
                 "input": {"file_path": "/r/z.ts", "edits": [{"old_string": old, "new_string": new_s}]}}]},
            {"role": "user", "content": [{"type": "tool_result", "tool_use_id": i, "content": "ok"} for i in ("w1", "e1", "m1")]},
            *_turns(4), {"role": "user", "content": "next"}]
    e = CMIEngine()
    e.on_session_start("s", hermes_home=str(tmp_path))
    e.update_model(model="m", context_length=200_000)
    out = e.select_context(msgs)
    calls = {b["id"]: b["input"] for b in out[1]["content"]}
    assert calls["w1"]["file_path"] == "/r/x.ts" and calls["w1"]["content"].startswith("[CMI")
    assert calls["e1"]["old_string"].startswith("[CMI: old_string omitted") and calls["e1"]["new_string"].startswith("[CMI")
    assert isinstance(calls["m1"]["edits"], str) and "cmi:" in calls["m1"]["edits"]
    originals = {e.store.get(k) for k in KEY_RE.findall(json.dumps(out[1]))}
    assert {body, old, new_s, json.dumps([{"old_string": old, "new_string": new_s}])} <= originals


def test_w10_anthropic_advisor_server_tool_blocks_untouched(tmp_path):
    """Claude Code `--advisor`: the server-executed advisor call and its result live in assistant
    turns. Even when old and large, they must go back byte-for-byte (the API replays them)."""
    advice = "Advisor: start from store.py; check MAX_BYTES. " + "Consider the retention rule. " * 200
    use = {"type": "server_tool_use", "id": "srvtoolu_01", "name": "advisor", "input": {}}
    res = {"type": "advisor_tool_result", "tool_use_id": "srvtoolu_01",
           "content": {"type": "advisor_result", "text": advice}}
    body = _anthropic_body()
    body["messages"][1]["content"] = [body["messages"][1]["content"][0], use, res,
                                      *body["messages"][1]["content"][1:]]
    orig = copy.deepcopy(body)
    new, stats = proxy.compile_request(_sessions(tmp_path), "/v1/messages", body)
    assert stats["changed"]                                  # the old read is still digested
    assert new["messages"][2]["content"][0]["content"].startswith("[CMI")
    assert new["messages"][1] == orig["messages"][1]         # advisor turn byte-identical


def test_w11_anthropic_old_narration_turn_with_advisor_keeps_advisor_blocks(tmp_path):
    advice = "Advisor: " + "keep the store layout stable. " * 200
    turn = {"role": "assistant", "content": [
        {"type": "text", "text": "Let me look at this first. " * 40},
        {"type": "server_tool_use", "id": "srvtoolu_02", "name": "advisor", "input": {}},
        {"type": "advisor_tool_result", "tool_use_id": "srvtoolu_02",
         "content": {"type": "advisor_result", "text": advice}},
        {"type": "text", "text": "Plan noted."}]}
    body = _anthropic_body()
    body["messages"][4] = turn                               # an old assistant turn
    orig = copy.deepcopy(body)
    new, _ = proxy.compile_request(_sessions(tmp_path), "/v1/messages", body)
    blocks = new["messages"][4]["content"]
    assert blocks[1] == orig["messages"][4]["content"][1]
    assert blocks[2] == orig["messages"][4]["content"][2]


def test_w12_nested_images_estimated_at_billed_size_not_base64():
    """Replay of a real Claude Code session 2026-11-04: screenshots ride INSIDE tool_result blocks.
    Counted at base64 length the estimate was 4.3x the billed size, so tiers that evict them looked
    worth 200K tokens (real: ~20K) and fired, rewriting the cache at a loss (+15% cost)."""
    from cmi.compat import estimate_messages_tokens_rough as est
    shot = {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "A" * 400_000}}
    anth = {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t1", "content": [shot]}]}
    resp = {"role": "tool", "tool_call_id": "c1",
            "content": [{"type": "input_image", "image_url": "data:image/png;base64," + "A" * 400_000}]}
    for m in (anth, resp):
        assert 1_000 <= est([m]) < 2_000  # host-calibrated or flat 1,600, est([m])
