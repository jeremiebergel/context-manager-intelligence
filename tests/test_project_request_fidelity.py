"""Request-fidelity guidance contracts, not claims about model compliance."""
import json

import pytest

from cmi.project_bridge import ProjectBridge


def test_all_native_read_schemas_explain_request_fidelity():
    reads = [s for s in ProjectBridge.schemas() if s["name"] != "cmi_project_publish"]
    assert len(reads) == 3
    for schema in reads:
        description = schema["description"]
        for phrase in ("bare or quoted", "punctuation", "related candidates", "clarification",
                       "indistinguishable", "natural-language paraphrases", "requested detail"):
            assert phrase in description, (schema["name"], phrase)


@pytest.mark.parametrize("question", [
    "Explain shard:drain", "Explain `shard/drain`", 'Explique « shard-drain ».',
    "How do we empty a shard safely?", "Which current panel is it?",
])
def test_native_feedback_keeps_related_candidates_and_source_bytes(tmp_path, question):
    bridge = ProjectBridge(tmp_path, "p", "s")
    source = "Drain shards one at a time.\nDo not infer a timing guarantee. 😀"
    receipt = bridge.memory.publish("p", "shard.drain", "Drain shards one at a time.",
                                    source="test:source", evidence=source)
    bridge.capture([{"role": "user", "content": question}])
    # Reformulated searches may discover a related record; they do not establish intent.
    search = bridge.handle("cmi_project_search", {"query": "shard drain"})
    assert search["results"][0]["topic"] == "shard.drain"
    assert bridge.query == question
    opened = bridge.handle("cmi_project_get", {"id": receipt["id"], "include_evidence": True})
    assert opened["evidence"] == source
    assert opened["topic"] == "shard.drain"
    assert opened["current"] is True
    for result in (search, opened):
        guidance = result["request_guidance"]
        assert "original user request" in guidance
        assert "never silently substitute" in guidance
        assert "clarification" in guidance
        assert "Even one result" in guidance
        assert "complete exact evidence" in guidance
        assert "unsupported detail" in guidance
    assert "request_guidance" not in bridge.memory.get("p", receipt["id"], include_evidence=True)


def test_driver_captures_public_original_question_on_every_fresh_bridge(tmp_path, monkeypatch):
    from bench.project_catalog import prepare, tool_step, load, dump

    directory = tmp_path / "run"
    prepare(directory)
    public = load(directory / "questions.json")["cases"][0]
    captured = []
    capture = ProjectBridge.capture

    def observe(self, messages):
        capture(self, messages)
        captured.append((self.query, dict(self.sources)))

    monkeypatch.setattr(ProjectBridge, "capture", observe)
    # A poisoned judge-only question must never be used as host request text.
    private = load(directory / "judge-private.json")
    private["cases"][0]["question"] = "PRIVATE JUDGE SENTINEL"
    dump(directory / "judge-private.json", private)
    for name, arguments in [("cmi_project_topics", {}),
                            ("cmi_project_search", {"query": "rewritten tool query"})]:
        tool_step(directory, dict(case_id=public["case_id"], name=name, arguments=arguments))
    assert captured == [(public["question"], {"user:latest": public["question"]})] * 2


def test_prospective_set_freezes_separate_sources_and_judge_without_prompt_leakage(tmp_path):
    import hashlib
    from bench.project_catalog import load, encoded, tool_step, READ_TOOLS, MAX_CALLS, MAX_RESULT_BYTES
    from bench.project_request_fidelity import prepare

    directory = tmp_path / "prospective"
    manifest = prepare(directory)
    public = load(directory / "questions.json")
    private = load(directory / "judge-private.json")
    frozen = load(directory / "fixture-request-fidelity-frozen.json")
    assert manifest["cases"] == len(public["cases"]) == len(private["cases"]) == 16
    assert manifest["model_calls"] == 0
    assert manifest["max_calls_per_case"] == MAX_CALLS == 12
    assert manifest["max_tool_result_bytes_per_case"] == MAX_RESULT_BYTES == 45000
    assert manifest["fixture_sha256"] == hashlib.sha256(encoded(frozen)).hexdigest()
    assert manifest["judge_sha256"] == hashlib.sha256(encoded(private)).hexdigest()
    assert all(set(c) == {"case_id", "question"} for c in public["cases"])
    assert {s["name"] for s in load(directory / "schemas.json")} == READ_TOOLS
    prompt_material = json.dumps(public) + json.dumps(load(directory / "schemas.json"))
    assert all(c["expected_id"] not in prompt_material for c in private["cases"] if c["expected_id"])
    for case in private["cases"]:
        if case["expected_id"] is None:
            continue
        opened = tool_step(directory, dict(case_id=case["case_id"], name="cmi_project_get",
            arguments=dict(id=case["expected_id"], include_evidence=True)))["result"]
        assert opened["current"]
        assert opened["evidence_sha256"] == case["evidence_sha256"]
        source = dict(frozen["facts"])[case["expected_topic"]]
        assert opened["evidence"] == source
    with pytest.raises(FileExistsError):
        prepare(directory)
