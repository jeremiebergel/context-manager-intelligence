"""CMI review: change extraction, reviewer choice, parsing, and the post->pre hook round trip."""
import json
import time

import pytest

import cmi_review
from cmi_review import core


def _call(cid, name, args):
    return {"id": cid, "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}


DIFF = "--- a//home/dev/Projects/TvApp/app/src/ui/TvScreen.kt\n+++ b//home/dev/Projects/TvApp/app/src/ui/TvScreen.kt\n@@ -1,3 +1,12 @@\n" + "".join(
    f"+line {i}\n" for i in range(10)) + " ctx\n-old\n"


def _history(extra_user_turns=0):
    msgs = [{"role": "system", "content": "sys"}, {"role": "user", "content": "old request"},
            {"role": "assistant", "content": "", "tool_calls": [_call("o1", "patch", {"path": "/home/dev/Projects/Old/a.py"})]},
            {"role": "tool", "tool_call_id": "o1", "content": json.dumps({"success": True, "diff": DIFF})},
            {"role": "user", "content": "Ajoute le zapping, sans spoiler"},
            {"role": "assistant", "content": "", "tool_calls": [
                _call("c1", "patch", {"path": "/home/dev/Projects/TvApp/app/src/ui/TvScreen.kt", "old_string": "x", "new_string": "y"}),
                _call("c2", "patch", {"path": "/home/dev/Projects/TvApp/app/src/ui/Other.kt"}),
                _call("c3", "write_file", {"path": "/home/dev/.hermes/cache/scratch/probe.py", "content": "x\n" * 50}),
                _call("c4", "write_file", {"path": "/home/dev/Projects/TvApp/README.md", "content": "doc\n" * 50}),
                _call("c5", "read_file", {"path": "/home/dev/Projects/TvApp/app/src/ui/TvScreen.kt"}),
            ]},
            {"role": "tool", "tool_call_id": "c1", "content": json.dumps({"success": True, "diff": DIFF})},
            {"role": "tool", "tool_call_id": "c2", "content": json.dumps({"success": False, "error": "Found 2 matches"})},
            {"role": "tool", "tool_call_id": "c3", "content": json.dumps({"bytes_written": 100})},
            {"role": "tool", "tool_call_id": "c4", "content": json.dumps({"bytes_written": 100})},
            {"role": "tool", "tool_call_id": "c5", "content": "file text"},
            {"role": "assistant", "content": "Zapping ajouté."}]
    return msgs


def test_extract_only_successful_code_changes_of_the_current_turn():
    turn = core.extract_turn(_history())
    assert turn.request == "Ajoute le zapping, sans spoiler"
    assert turn.claim == "Zapping ajouté."
    assert turn.paths == ["/home/dev/Projects/TvApp/app/src/ui/TvScreen.kt"]  # failed patch, scratch, .md, reads skipped
    assert turn.changed_lines == 11
    assert core.should_review(turn, core.DEFAULTS)


def test_small_change_or_no_user_message_is_not_reviewed():
    assert core.extract_turn([{"role": "assistant", "content": "hi"}]) is None
    turn = core.extract_turn(_history())
    assert not core.should_review(turn, dict(core.DEFAULTS, min_changed_lines=50))


def test_write_file_of_source_counts_lines():
    msgs = [{"role": "user", "content": "go"},
            {"role": "assistant", "content": "", "tool_calls": [_call("w", "write_file", {"path": "/p/Projects/X/src/a.ts", "content": "a\nb\nc"})]},
            {"role": "tool", "tool_call_id": "w", "content": json.dumps({"bytes_written": 5})}]
    turn = core.extract_turn(msgs)
    assert [(c.path, c.lines) for c in turn.changes] == [("/p/Projects/X/src/a.ts", 3)]


def test_secrets_are_never_sent():
    turn = core.extract_turn(_history())
    dropped = core.drop_secret_changes(turn, lambda t: "line 3" in t)
    assert dropped and not turn.changes


@pytest.mark.parametrize("writer,expected", [
    ("claude-opus-5-5", "claude-sonnet-5"),
    ("glm-5.3", "claude-opus-5-5"),
    ("claude-sonnet-5", "claude-opus-5-5"),
    ("anthropic/claude-opus-5-5", "claude-sonnet-5"),
])
def test_reviewer_is_never_the_writer(writer, expected):
    assert core.pick_reviewer(writer, core.DEFAULTS)[1] == expected


def test_reviewer_rule_matching_writer_falls_through():
    cfg = dict(core.DEFAULTS, reviewers=[["*", ["anthropic", "claude-opus-5-5"]]])
    assert core.pick_reviewer("claude-opus-5-5", cfg) == ("anthropic", "claude-sonnet-5")


def test_parse_review_is_tolerant():
    txt = 'Sure:\n```json\n{"verdict":"issues","findings":[{"severity":"LOW","file":"a","problem":"p"},' \
          '{"severity":"high","file":"b","problem":"nav stack destroyed","fix":"push"},{"severity":"high","problem":""}]}\n```'
    r = core.parse_review(txt)
    assert [f["severity"] for f in r["findings"]] == ["high", "low"]
    assert core.parse_review("no json here") is None
    assert core.parse_review('{"verdict":"ok","findings":[]}') == {"verdict": "ok", "findings": []}


def test_categorize_and_project():
    paths = ["/home/dev/Projects/TvApp/app/src/ui/TvScreen.kt"]
    assert core.categorize(paths) == "android-ui"
    assert core.categorize(["/x/src/users.service.spec.ts"]) == "tests"
    assert core.categorize(["/x/server/index.js"]) == "backend"
    assert core.project_of(paths) == "TvApp"


def test_injection_only_for_actionable():
    assert core.format_injection({"findings": [{"severity": "low", "file": "a", "problem": "p"}]}) == ""
    text = core.format_injection({"writer": "glm-5.3", "reviewer": "claude-opus-5-5", "paths": ["a"],
                                  "findings": [{"severity": "high", "file": "a.kt", "where": "open()", "problem": "replaceAll", "fix": "push"}]})
    assert text.startswith("[CMI review]") and "NOT verified" in text and "replaceAll" in text and "fix: push" in text


def _wait_for_thread():
    for _ in range(200):
        if not cmi_review._INFLIGHT:
            return
        time.sleep(0.01)


def test_round_trip_post_then_pre(monkeypatch):
    seen = {}

    def fake(provider, model, messages, timeout):
        seen["model"] = model
        seen["prompt"] = messages[1]["content"]
        return json.dumps({"verdict": "issues", "findings": [
            {"severity": "high", "file": "TvScreen.kt", "problem": "Back no longer returns to zapping", "fix": "push"},
            {"severity": "low", "file": "TvScreen.kt", "problem": "minor"}]}), {"provider": provider, "model": model}

    monkeypatch.setattr(cmi_review, "_call_reviewer", fake)
    cmi_review.on_post_llm_call(session_id="s1", model="glm-5.3", platform="desktop", conversation_history=_history())
    out = cmi_review.on_pre_llm_call(session_id="s1")  # waits for the in-flight review
    assert seen["model"] == "claude-opus-5-5"
    assert "sans spoiler" in seen["prompt"] and "+line 3" in seen["prompt"]
    assert "Back no longer returns" in out["context"] and "minor" not in out["context"]
    assert cmi_review.on_pre_llm_call(session_id="s1") is None  # one-shot
    rows = cmi_review._read_ledger()
    rev = [r for r in rows if r["kind"] == "review"][0]
    assert rev["writer"] == "glm-5.3" and rev["category"] == "android-ui" and rev["counts"] == {"high": 1, "medium": 0, "low": 1}
    assert any(r["kind"] == "delivered" for r in rows)
    assert "glm-5.3" in cmi_review._review_command("")


def test_clean_review_and_failures_inject_nothing(monkeypatch):
    monkeypatch.setattr(cmi_review, "_call_reviewer", lambda *a: ('{"verdict":"ok","findings":[]}', {}))
    cmi_review.on_post_llm_call(session_id="s2", model="claude-opus-5-5", platform="desktop", conversation_history=_history())
    assert cmi_review.on_pre_llm_call(session_id="s2") is None

    def boom(*a):
        raise RuntimeError("429")
    monkeypatch.setattr(cmi_review, "_call_reviewer", boom)
    cmi_review.on_post_llm_call(session_id="s3", model="glm-5.3", platform="telegram", conversation_history=_history())
    assert cmi_review.on_pre_llm_call(session_id="s3") is None
    errs = [r for r in cmi_review._read_ledger() if r.get("error")]
    assert errs and "429" in errs[0]["error"]


def test_cron_off_switch_and_daily_cap(monkeypatch):
    calls = []
    monkeypatch.setattr(cmi_review, "_call_reviewer", lambda *a: calls.append(1) or ('{"findings":[]}', {}))
    cmi_review.on_post_llm_call(session_id="c", model="glm-5.3", platform="cron", conversation_history=_history())
    cmi_review._review_command("off")
    cmi_review.on_post_llm_call(session_id="d", model="glm-5.3", platform="desktop", conversation_history=_history())
    _wait_for_thread()
    assert calls == []
    cmi_review._review_command("on")
    monkeypatch.setattr(cmi_review, "_reviews_today", lambda: 999)
    cmi_review.on_post_llm_call(session_id="e", model="glm-5.3", platform="desktop", conversation_history=_history())
    _wait_for_thread()
    assert calls == []


def test_gating_skips_tests_and_raises_threshold_for_low_yield_categories():
    from cmi_review import core
    cfg = dict(core.DEFAULTS)
    mk = lambda path, n: core.Turn(request="r", claim="c", changes=[core.Change(path, "patch", "x", n)])  # noqa: E731
    assert not core.should_review(mk("/p/tests/test_a.py", 50), cfg)       # test-only: skipped
    assert core.should_review(mk("/p/src/a.py", 8), cfg)                   # python: base threshold
    assert not core.should_review(mk("/p/ui/App.tsx", 10), cfg)            # web-ui needs 15
    assert core.should_review(mk("/p/ui/App.tsx", 15), cfg)
    assert core.should_review(mk("/p/tests/test_a.py", 50), {**cfg, "skip_categories": []})


def test_followup_detects_whether_flagged_files_were_touched():
    from cmi_review import core
    t = core.Turn(request="r", claim="c", changes=[core.Change("/p/src/a.py", "patch", "x", 3)])
    assert core.followup(["src/a.py"], t)["addressed"] is True
    assert core.followup(["src/b.py"], t)["addressed"] is False
    assert core.followup(["src/a.py"], None)["addressed"] is False
