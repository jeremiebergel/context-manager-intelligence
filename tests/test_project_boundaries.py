import json

from cmi.engine import CMIEngine
from cmi.project_bridge import ProjectBridge


def test_environment_opt_in_precedes_schema_registration(monkeypatch, tmp_path):
    monkeypatch.setenv("CMI_PROJECT_ID", "explicit-project")
    e = CMIEngine()
    assert "cmi_project_publish" in {s["name"] for s in e.get_tool_schemas()}
    e.on_session_start("real-session", hermes_home=str(tmp_path))
    assert e._project.project == "explicit-project"
    assert e._project.session == "real-session"
    assert e._project.memory.path == tmp_path / "cmi_project" / "memory.db"


def test_reset_discards_source_registry_but_preserves_project_records(tmp_path):
    e = CMIEngine()
    e.on_session_start("a", hermes_home=str(tmp_path), project_id="p")
    e.select_context([{"role": "user", "content": "original decision"}])
    r = json.loads(e.handle_tool_call("cmi_project_publish", dict(
        topic="decision", text="original decision", quote="original decision", source="user:latest")))
    assert r["id"]
    e.on_session_reset()
    assert e._project.sources == {}
    assert e._project.query == ""
    assert e._project.memory.get("p", r["id"])


def test_source_registry_drops_history_that_was_removed_by_compaction(tmp_path):
    b = ProjectBridge(tmp_path, "p", "s")
    b.capture([{"role": "user", "content": "old"},
               {"role": "tool", "tool_call_id": "old-tool", "content": "old evidence"}])
    b.capture([{"role": "user", "content": "new"}])
    assert b.sources == {"user:latest": "new"}


def test_get_returns_paginated_exact_evidence_and_integrity_check(tmp_path):
    import sqlite3
    b = ProjectBridge(tmp_path, "p", "s")
    evidence = "quote\n" + "abc " * 4000
    b.capture([{"role": "user", "content": evidence}])
    r = b.handle("cmi_project_publish", dict(topic="quote", text="quote", quote="quote", source="user:latest"))
    one = b.handle("cmi_project_get", dict(id=r["id"], include_evidence=True))
    two = b.handle("cmi_project_get", dict(id=r["id"], include_evidence=True, offset=one["next_offset"]))
    assert one["evidence"] + two["evidence"] == evidence
    assert two["next_offset"] is None
    with sqlite3.connect(b.memory.path) as db:
        db.execute("UPDATE evidence SET content='tampered'")
    assert b.handle("cmi_project_get", dict(id=r["id"], include_evidence=True)).get("error")


def test_injected_memory_is_counted_in_cost_shadow(tmp_path):
    e = CMIEngine()
    e.on_session_start("a", hermes_home=str(tmp_path), project_id="p")
    e.select_context([{"role": "user", "content": "marker test"}])
    e.handle_tool_call("cmi_project_publish", dict(topic="marker", text="marker test",
        quote="marker test", source="user:latest"))
    e.select_context([{"role": "user", "content": "marker"}])
    assert e.telemetry["project_memory_tokens"] > 0
    assert e.cost.cmi.tokens > e.cost.raw.tokens


def test_compression_child_keeps_explicit_project_binding(tmp_path):
    e = CMIEngine()
    e.on_session_start("parent", hermes_home=str(tmp_path), project_id="explicit-project")
    path = e._project.memory.path
    e.on_session_start("child", boundary_reason="compression", old_session_id="parent")
    assert e._project is not None
    assert e._project.project == "explicit-project"
    assert e._project.memory.path == path
    assert e._project.session == "child"


def test_tight_budget_does_not_inject_memory(tmp_path):
    e = CMIEngine()
    e.on_session_start("a", hermes_home=str(tmp_path), project_id="p")
    e.select_context([{"role": "user", "content": "marker test"}])
    e.handle_tool_call("cmi_project_publish", dict(topic="marker", text="marker test",
        quote="marker test", source="user:latest"))
    messages = [{"role": "user", "content": "marker"}]
    assert (e.select_context(messages, budget_tokens=1) or messages) == messages
