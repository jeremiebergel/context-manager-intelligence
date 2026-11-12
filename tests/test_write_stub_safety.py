"""A model copying CMI's short form of a past write must never put a stub in a file.

Barrier 1 (lifecycle): a shortened past write carries no payload key, so an imitation is refused by
the tool itself ("missing content"). Barrier 2 (write_guard): a payload that is CMI's short form is
blocked by a ``pre_tool_call`` hook. The Hermes tests run against the real Hermes code.
"""
import copy
import importlib.util
import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from cmi.lifecycle import ToolResultLifecycle
from cmi.write_guard import refuse_stub_write

KEY = "cmi:0123456789abcdef"
ROOT = Path(__file__).resolve().parent.parent
needs_hermes = pytest.mark.skipif(
    os.environ.get("CMI_STANDALONE") == "1" or importlib.util.find_spec("tools.file_tools") is None,
    reason="needs the Hermes core")


def _shortened(tool, args):
    return ToolResultLifecycle().render_tool_call_args_digest(
        "i1", tool_name=tool, args=args, stash=lambda v: f" {KEY}")


def _old_write_stub(path="/p/a.md", n="1,151"):  # the 1.12.0 form, built here so no file holds it whole
    return "[CMI: content " + f"omitted — already written to {path}, {n} chars. File on disk is the source of truth.] {KEY}"


def test_shortened_write_keeps_no_payload_and_says_how_to_recall():
    out = _shortened("write_file", {"path": "/p/a.md", "content": "x" * 500})
    assert set(out) == {"path", "cmi_omitted"}
    assert "content (500 chars)" in out["cmi_omitted"] and KEY in out["cmi_omitted"] and "/p/a.md" in out["cmi_omitted"]
    edit = _shortened("Edit", {"file_path": "/p/b.py", "old_string": "a" * 200, "new_string": "b" * 200})
    assert set(edit) == {"file_path", "cmi_omitted"}
    multi = _shortened("MultiEdit", {"file_path": "/p/c.py", "edits": [{"old_string": "a" * 90, "new_string": "b" * 90}]})
    assert set(multi) == {"file_path", "cmi_omitted"}


@pytest.mark.parametrize("payload", [
    _old_write_stub(),
    _old_write_stub() + "\n\nmore text after the stub",
    "# notes\n\n" + _old_write_stub() + "\n",                      # file re-assembled around a stub
    "[CMI " + "old_string omitted — 2,000 chars, applied to /p/a.md.] " + KEY,
    "[CMI digest] " + KEY + " [read_file] read /p/a.md (8,000 chars)",
    "[CMI dup] identical to an earlier [read_file] result — content unchanged",
    "[CMI batch item 2] " + KEY + " see batch summary above",
    "[CMI: narration omitted] " + KEY + " Now the README…",
])
def test_guard_blocks_a_write_of_any_cmi_stub(payload):
    payload = payload.replace("[CMI old_string", "[CMI: old_string")
    for tool, key in (("write_file", "content"), ("patch", "new_string"), ("Write", "content"),
                      ("mcp__write_file", "content")):
        out = refuse_stub_write(tool_name=tool, args={"path": "/p/a.md", key: payload})
        assert out and out["action"] == "block", (tool, payload)
    hit = refuse_stub_write(tool_name="write_file", args={"path": "/p/a.md", "content": _old_write_stub()})
    assert hit is not None and KEY in hit["message"] and "cmi_recall" in hit["message"]


@pytest.mark.parametrize("payload", [
    "real content " * 50,
    "Notes about CMI: stubs look like `[CMI digest] cmi:<key>`; see the README.\n",  # quoted, not leading
    "def test():\n    assert out.startswith(\"[CMI: content omitted\")\n",           # partial form in code
])
def test_guard_lets_real_content_through(payload):
    assert refuse_stub_write(tool_name="write_file", args={"path": "/p/a.md", "content": payload}) is None
    # Non-writing tools are never checked.
    assert refuse_stub_write(tool_name="terminal", args={"command": "[CMI digest] x"}) is None


def test_guard_checks_multiedit_items_and_fails_open():
    edits = [{"old_string": "a", "new_string": _old_write_stub()}]
    hit = refuse_stub_write(tool_name="MultiEdit", args={"file_path": "/p", "edits": edits})
    assert hit is not None and hit["action"] == "block"
    assert refuse_stub_write(tool_name="write_file", args=None) is None
    assert refuse_stub_write(tool_name="write_file", args={"content": 3}) is None


@needs_hermes
def test_imitated_short_form_is_refused_by_the_real_hermes_tools(tmp_path):
    """Barrier 1 against the real Hermes handlers: nothing is written."""
    from tools.file_tools import _handle_patch, _handle_write_file
    target = tmp_path / "a.md"
    target.write_text("original text\n")
    write = _shortened("write_file", {"path": str(target), "content": "x" * 500})
    assert "error" in json.loads(_handle_write_file(dict(write), task_id="t"))
    patch = dict(_shortened("patch", {"path": str(target), "old_string": "o" * 200, "new_string": "y" * 200}))
    patch["old_string"] = "original text"  # imitation with a real anchor and no replacement
    assert "error" in json.loads(_handle_patch(patch, task_id="t"))
    assert target.read_text() == "original text\n"


@needs_hermes
def test_guard_runs_through_real_hermes_plugin_loading(tmp_path):
    """Barrier 2 against the real Hermes plugin loader and hook dispatch, in a fresh process."""
    home = tmp_path / "hermes_home"
    (home / "plugins").mkdir(parents=True)
    (home / "plugins" / "cmi").symlink_to(ROOT / "cmi", target_is_directory=True)
    (home / "config.yaml").write_text("context:\n  engine: cmi\nplugins:\n  enabled:\n    - cmi\n")
    probe = textwrap.dedent(f"""
        import json
        from hermes_cli.plugins import discover_plugins, _dispatch_pre_tool_call_hooks
        discover_plugins()
        stub = {_old_write_stub()!r}
        blocked, _ = _dispatch_pre_tool_call_hooks("write_file", {{"path": "/x/a.md", "content": stub}})
        allowed, _ = _dispatch_pre_tool_call_hooks("write_file", {{"path": "/x/a.md", "content": "real text"}})
        print(json.dumps({{"blocked": blocked, "allowed": allowed}}))
    """)
    env = {**os.environ, "HERMES_HOME": str(home), "PYTHONPATH": ""}
    core = Path(os.environ.get("HERMES_AGENT_DIR", Path.home() / ".hermes" / "hermes-agent"))
    r = subprocess.run([sys.executable, "-c", probe], cwd=core, env=env, capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, r.stderr[-2000:]
    out = json.loads(r.stdout.strip().splitlines()[-1])
    assert out["blocked"] and "Blocked by CMI" in out["blocked"], out
    assert out["allowed"] is None


# -- 1.12.2: the cmi_omitted note itself, and barrier 3 (flag a stub write that went through) --------

def _note():  # the real 1.12.1 note, from the real renderer
    return _shortened("write_file", {"path": "/p/a.md", "content": "x" * 500})["cmi_omitted"]


@pytest.mark.parametrize("payload", [
    lambda: _note(),
    lambda: "  " + _note() + "\nmore",
    lambda: "# notes\n\n" + _note() + "\n",            # the whole note embedded in a file
])
def test_guard_blocks_the_cmi_omitted_note_copied_into_a_payload(payload):
    for tool, key in (("write_file", "content"), ("patch", "new_string"), ("Write", "content")):
        out = refuse_stub_write(tool_name=tool, args={"path": "/p/a.md", key: payload()})
        assert out and out["action"] == "block", (tool, payload())


def test_guard_blocks_any_call_that_carries_the_cmi_omitted_key():
    from cmi.lifecycle import OMITTED_ARG_KEY
    for args in ({"path": "/p/a.md", OMITTED_ARG_KEY: _note()},
                 {"path": "/p/a.md", "content": "", OMITTED_ARG_KEY: _note()},       # empty file otherwise
                 {"path": "/p/a.md", "content": "real text", OMITTED_ARG_KEY: "x"}):
        out = refuse_stub_write(tool_name="write_file", args=args)
        assert out and out["action"] == "block" and OMITTED_ARG_KEY in out["message"], args


def test_guard_lets_a_mention_of_the_note_wording_through():
    from cmi.lifecycle import OMITTED_NOTE_TAIL
    text = "The note ends with" + OMITTED_NOTE_TAIL + " It is documented in REFERENCE.md.\n"
    assert refuse_stub_write(tool_name="write_file", args={"path": "/p/doc.md", "content": text}) is None


def _engine():
    from cmi.engine import CMIEngine
    return CMIEngine()


def _openai_session(write_content, write_result='{"bytes_written": 120}'):
    read_body = '{"content": "1|original line one\\n2|original line two", "total_lines": 2}'
    return [
        {"role": "system", "content": "s"},
        {"role": "user", "content": "update a.md"},
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": "r1", "type": "function", "function": {"name": "read_file", "arguments": json.dumps({"path": "/p/a.md"})}}]},
        {"role": "tool", "tool_call_id": "r1", "content": read_body},
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": "w1", "type": "function", "function": {"name": "write_file",
                                                         "arguments": json.dumps({"path": "/p/a.md", "content": write_content})}}]},
        {"role": "tool", "tool_call_id": "w1", "content": write_result},
    ], read_body


def test_engine_flags_a_stub_write_that_went_through_and_stores_the_last_known_content():
    stub = "[CMI digest] " + KEY + " [read_file] read /p/a.md (2 lines)"
    msgs, read_body = _openai_session(stub)
    eng = _engine()
    out = eng.select_context(msgs)
    assert out is not None, "the warning is sent even when nothing else is shortened"
    flagged = out[-1]["content"]
    assert flagged.startswith('{"bytes_written": 120}') and "[CMI warning]" in flagged and "/p/a.md" in flagged
    key = flagged.split("is stored as cmi:")[1].split(";")[0].split()[0]
    assert eng.store.get(key) == read_body, "the last read is recoverable byte for byte"
    assert msgs[-1]["content"] == '{"bytes_written": 120}', "the caller's history is never mutated"
    again = _engine().select_context(msgs)
    assert again[-1]["content"] == flagged, "deterministic: the same history gives the same bytes"


def test_engine_flags_the_empty_imitation_and_skips_refused_or_blocked_writes():
    msgs, _ = _openai_session("")
    msgs[4]["tool_calls"][0]["function"]["arguments"] = json.dumps(
        {"path": "/p/a.md", "content": "", "cmi_omitted": _note()})
    assert "[CMI warning]" in _engine().select_context(msgs)[-1]["content"]
    for result in ('{"error": "write_file: missing required field \'content\'"}',
                   '{"error": "Blocked by CMI: `content` holds CMI\'s short form"}'):
        msgs, _ = _openai_session("[CMI dup] identical to an earlier result", write_result=result)
        out = _engine().select_context(msgs)
        assert out is None or "[CMI warning]" not in json.dumps(out)
    msgs, _ = _openai_session("real new content\n")
    out = _engine().select_context(msgs)
    assert out is None or "[CMI warning]" not in json.dumps(out)


def test_engine_flags_a_stub_write_in_the_anthropic_shape():
    stub = "[CMI: narration omitted] " + KEY
    msgs = [
        {"role": "user", "content": "go"},
        {"role": "assistant", "content": [{"type": "tool_use", "id": "toolu_1", "name": "Write",
                                           "input": {"file_path": "/p/b.md", "content": stub}}]},
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "toolu_1",
                                      "content": [{"type": "text", "text": "File created successfully"}]}]},
    ]
    out = _engine().select_context(msgs)
    part = out[-1]["content"][0]
    texts = [p["text"] for p in part["content"]]
    assert texts[0] == "File created successfully" and "[CMI warning]" in texts[-1]
    assert "no earlier content of /p/b.md" in texts[-1]
    part["is_error"] = True
    msgs[-1]["content"][0]["is_error"] = True
    out = _engine().select_context(msgs)
    assert out is None or "[CMI warning]" not in json.dumps(out)


@needs_hermes
def test_flagged_write_round_trip_restores_the_file_with_the_real_hermes_tools(tmp_path):
    """End to end on the real Hermes handlers: the stub write lands, CMI flags it at the next request
    with a recall key, and writing the recalled content back restores the file."""
    from tools.file_tools import _handle_read_file, _handle_write_file
    target = tmp_path / "a.md"
    target.write_text("line one\nline two\n")
    read_body = _handle_read_file({"path": str(target)}, task_id="t")
    stub = "[CMI digest] " + KEY + " [read_file] read " + str(target)
    wrote = _handle_write_file({"path": str(target), "content": stub}, task_id="t")
    assert target.read_text() == stub  # the damage this barrier exists for
    msgs = [
        {"role": "user", "content": "edit"},
        {"role": "assistant", "content": "", "tool_calls": [{"id": "r", "type": "function", "function": {
            "name": "read_file", "arguments": json.dumps({"path": str(target)})}}]},
        {"role": "tool", "tool_call_id": "r", "content": read_body},
        {"role": "assistant", "content": "", "tool_calls": [{"id": "w", "type": "function", "function": {
            "name": "write_file", "arguments": json.dumps({"path": str(target), "content": stub})}}]},
        {"role": "tool", "tool_call_id": "w", "content": wrote},
    ]
    eng = _engine()
    flagged = eng.select_context(msgs)[-1]["content"]
    key = flagged.split("is stored as cmi:")[1].split(";")[0].split()[0]
    recalled = json.loads(eng.store.get(key))["content"]
    restored = "".join(line.split("|", 1)[1] + "\n" for line in recalled.splitlines())
    _handle_write_file({"path": str(target), "content": restored}, task_id="t")
    assert target.read_text() == "line one\nline two\n"


@pytest.mark.parametrize("route", ["/v1/messages", "/v1/chat/completions", "/v1/responses"])
def test_proxy_flags_a_stub_write_on_every_wire(tmp_path, route):
    """Barrier 3 through the proxy (Claude Code, OpenAI clients, Codex): the warning reaches the wire."""
    from cmi import proxy
    stub = "[CMI digest] " + KEY + " [Read] read /p/c.md"
    if route == "/v1/messages":
        body = {"model": "claude-sonnet-5-5", "max_tokens": 10, "messages": [
            {"role": "user", "content": "go"},
            {"role": "assistant", "content": [{"type": "tool_use", "id": "toolu_9", "name": "Write",
                                               "input": {"file_path": "/p/c.md", "content": stub}}]},
            {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "toolu_9", "content": "ok"}]}]}
    elif route == "/v1/chat/completions":
        body = {"model": "gpt-6.1-sol", "messages": [
            {"role": "user", "content": "go"},
            {"role": "assistant", "content": None, "tool_calls": [{"id": "c9", "type": "function", "function": {
                "name": "write_file", "arguments": json.dumps({"path": "/p/c.md", "content": stub})}}]},
            {"role": "tool", "tool_call_id": "c9", "content": "ok"}]}
    else:
        body = {"model": "gpt-6.1-sol", "input": [
            {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "go"}]},
            {"type": "function_call", "call_id": "c9", "name": "write_file",
             "arguments": json.dumps({"path": "/p/c.md", "content": stub})},
            {"type": "function_call_output", "call_id": "c9", "output": "ok"}]}
    new, stats = proxy.compile_request(proxy.Sessions(str(tmp_path)), route, copy.deepcopy(body))
    sent = json.dumps(new, ensure_ascii=False)
    assert "[CMI warning]" in sent and "/p/c.md now holds that stub" in sent, (route, stats)
    assert json.dumps(body) != sent


def test_engine_does_not_flag_a_stub_write_already_repaired_by_a_later_full_write():
    msgs, _ = _openai_session("[CMI dup] identical to an earlier result")
    msgs += [
        {"role": "assistant", "content": "", "tool_calls": [{"id": "w2", "type": "function", "function": {
            "name": "write_file", "arguments": json.dumps({"path": "/p/a.md", "content": "fixed text\n"})}}]},
        {"role": "tool", "tool_call_id": "w2", "content": '{"bytes_written": 11}'},
    ]
    out = _engine().select_context(msgs)
    assert out is None or "[CMI warning]" not in json.dumps(out)
