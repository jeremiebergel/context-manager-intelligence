"""Regressions from the independent review of 2026-11-04."""
from cmi.lifecycle import ToolResultLifecycle


def test_narration_stub_memo_is_keyed_by_content_not_slot():
    lc = ToolResultLifecycle()
    a = lc.render_narration_stub("narr:7", original="ALPHA plan " * 50, ref=" cmi:aaa")
    b = lc.render_narration_stub("narr:7", original="BETA plan " * 50, ref=" cmi:bbb")
    assert "ALPHA" in a and "BETA" in b and "ALPHA" not in b
    assert lc.render_narration_stub("narr:7", original="ALPHA plan " * 50, ref=" cmi:aaa") == a


def test_digest_memo_is_keyed_by_content_reused_call_ids():
    lc = ToolResultLifecycle()
    f = lambda n, a, c: f"[{n}] {c[:20]}"  # noqa: E731
    one = lc.render_digest("call_0", tool_name="t", tool_args="{}", content="first " * 200, summarize_fn=f)
    two = lc.render_digest("call_0", tool_name="t", tool_args="{}", content="second " * 200, summarize_fn=f)
    assert one != two and "second" in two
