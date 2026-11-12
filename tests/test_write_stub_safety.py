"""A model copying CMI's short form of a past write must never put a stub in a file.

Barrier 1 (lifecycle): a shortened past write carries no payload key, so an imitation is refused by
the tool itself ("missing content"). Barrier 2 (write_guard): a payload that is CMI's short form is
blocked by a ``pre_tool_call`` hook. The Hermes tests run against the real Hermes code.
"""
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
