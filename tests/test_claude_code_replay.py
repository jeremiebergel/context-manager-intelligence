"""bench/claude_code_replay.py replays a session on its own clock: a pause longer than the cache
lifetime, or a request Claude Code recorded as re-writing its prompt, loses the provider cache
for both streams. Without it the bench never sees the free re-compile CMI gets after a pause."""
import datetime
import json

import pytest

bench = pytest.importorskip("bench.claude_code_replay", reason="bench/ is not shipped in the sdist")

T0 = datetime.datetime(2026, 11, 1, 9, 0, tzinfo=datetime.timezone.utc)


def _line(kind, message, at, **extra):
    return json.dumps({"type": kind, "timestamp": (T0 + datetime.timedelta(seconds=at)).isoformat().replace("+00:00", "Z"),
                       "message": message, **extra})


def _session(path, gap_before_turn=None, gap_s=7200, lost_at_turn=None, turns=12):
    lines, at = [], 0
    for t in range(turns):
        at += gap_s if t == gap_before_turn else 60
        body = "\n".join(f"t{t} line {i}: value {i * 7 + t}" for i in range(400))
        lost = t == lost_at_turn
        usage = {"input_tokens": 3, "cache_read_input_tokens": 0 if lost else 40_000,
                 "cache_creation_input_tokens": 60_000 if lost else 500}
        lines += [
            _line("user", {"role": "user", "content": f"turn {t}"}, at),
            _line("assistant", {"role": "assistant", "id": f"m{t}a", "usage": usage, "content": [
                {"type": "tool_use", "id": f"tu{t}", "name": "Bash", "input": {"command": f"run {t}"}}]}, at + 5),
            _line("user", {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": f"tu{t}", "content": body}]}, at + 10),
            _line("assistant", {"role": "assistant", "id": f"m{t}b", "usage": {**usage, "cache_read_input_tokens": 40_000,
                                                                               "cache_creation_input_tokens": 500},
                                "content": [{"type": "text", "text": f"done {t}"}]}, at + 15),
        ]
    path.write_text("\n".join(lines) + "\n")
    return path


def _cost(result):
    return float(result["cached_cost_delta"].rstrip("%"))


def test_timestamps_and_usage_are_loaded_with_the_messages(tmp_path):
    p = _session(tmp_path / "s.jsonl", turns=2)
    (msgs, meta), = bench.load_timed_segments(p)
    assert bench.load_segments(p) == [msgs]
    assert [m["role"] for m in msgs] == ["user", "assistant", "user", "assistant"] * 2
    assert all(set(m) == {"role", "content"} for m in msgs)       # nothing extra reaches the proxy
    assert [round(t - T0.timestamp()) for t, _ in meta] == [60, 65, 70, 75, 120, 125, 130, 135]
    assert meta[1][1]["cache_read_input_tokens"] == 40_000 and meta[0][1] is None


def test_a_pause_longer_than_the_cache_lifetime_is_a_free_recompile(tmp_path):
    p = _session(tmp_path / "s.jsonl", gap_before_turn=8)
    timed = bench.replay(p, clock=True)
    flat = bench.replay(p, clock=False)
    for r in (timed, flat):
        assert r["violations"] == 0 and r["unstable"] == 0, r
        assert r["recall_exact"] == f"{r['stub_keys']}/{r['stub_keys']}"
    assert (timed["cold_idle"], timed["cold_other"]) == (1, 0)
    assert (flat["cold_idle"], flat["cold_other"]) == (0, 0) and flat["clock"] is False
    # The short session is inside the proxy's start-up phase: without the pause nothing is
    # shortened; with it the history is re-compiled while the cache is lost anyway.
    assert flat["stub_keys"] == 0 and _cost(flat) == 0
    assert timed["stub_keys"] > 0 and _cost(timed) < 0


def test_a_short_pause_keeps_the_cache(tmp_path):
    r = bench.replay(_session(tmp_path / "s.jsonl", gap_before_turn=8, gap_s=1800), clock=True)
    assert (r["cold_idle"], r["cold_other"], r["stub_keys"]) == (0, 0, 0)


def test_a_recorded_cache_loss_is_charged_to_both_streams(tmp_path):
    r = bench.replay(_session(tmp_path / "s.jsonl", lost_at_turn=6), clock=True)
    assert (r["cold_idle"], r["cold_other"]) == (0, 1)
    # The engine cannot know about it (no pause), so it changes nothing: same cost both ways.
    assert r["stub_keys"] == 0 and _cost(r) == 0
    assert r["replayed_share_of_billed_input"] is not None
    assert "0-50" in r["by_request_rank"]
