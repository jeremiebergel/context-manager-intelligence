import copy
import json


from cmi.engine import CMIEngine


def engine(home, session, project=None):
    e = CMIEngine()
    e.on_session_start(session, hermes_home=str(home), project_id=project)
    e._model = "test-gpt"
    return e


def call(e, name, **args):
    return json.loads(e.handle_tool_call(name, args))


def test_project_tools_opt_in_and_evidence_comes_from_host_history(tmp_path):
    off = engine(tmp_path / "off", "off")
    assert "cmi_project_publish" not in {s["name"] for s in off.get_tool_schemas()}
    a = engine(tmp_path / "shared", "a", "cmi-pilot")
    assert "cmi_project_publish" in {s["name"] for s in a.get_tool_schemas()}
    hist = [{"role": "user", "content": "Decision: pilot marker is amber-owl-42."}]
    a.select_context(hist)
    r = call(a, "cmi_project_publish", topic="pilot.marker", text="amber-owl-42",
             quote="amber-owl-42", source="user:latest", kind="decision", expected_revision=0)
    assert r["revision"] == 1
    b = engine(tmp_path / "shared", "b", "cmi-pilot")
    hits = call(b, "cmi_project_search", query="pilot marker")["results"]
    assert hits[0]["id"] == r["id"]
    expanded = call(b, "cmi_project_get", id=r["id"], include_evidence=True)
    assert expanded["evidence"] == hist[0]["content"]
    assert expanded["session"] == "a"
    assert call(b, "cmi_project_publish", topic="fake", text="made up", quote="made up",
                source="user:latest").get("error")
    c = engine(tmp_path / "shared", "c", "another-project")
    assert call(c, "cmi_project_search", query="pilot marker")["results"] == []
    assert call(c, "cmi_project_get", id=r["id"], include_evidence=True).get("error")


def test_update_is_visible_next_request_without_rewriting_history_prefix(tmp_path):
    a = engine(tmp_path, "a", "p")
    a.select_context([{"role": "user", "content": "pilot marker amber-owl-42"}])
    first = call(a, "cmi_project_publish", topic="pilot.marker", text="amber-owl-42",
                 quote="amber-owl-42", source="user:latest")
    b = engine(tmp_path, "b", "p")
    hist = [{"role": "system", "content": "stable"},
            {"role": "user", "content": "What is the pilot marker?"},
            {"role": "assistant", "content": "I am checking."}]
    snapshot = copy.deepcopy(hist)
    out = b.select_context(hist) or hist
    assert out[:len(hist)] == hist
    assert "amber-owl-42" in out[-1]["content"]
    assert b.select_context(hist) == out
    assert hist == snapshot
    a.select_context([{"role": "user", "content": "pilot marker violet-fox-71"}])
    changed = call(a, "cmi_project_publish", topic="pilot.marker", text="violet-fox-71",
                   quote="violet-fox-71", source="user:latest", expected_revision=1)
    assert changed["revision"] == 2
    out = b.select_context(hist) or hist
    assert "violet-fox-71" in out[-1]["content"]
    assert "amber-owl-42" not in out[-1]["content"]
    conflict = call(a, "cmi_project_publish", topic="pilot.marker", text="violet-fox-71",
                    quote="violet-fox-71", source="user:latest", expected_revision=1)
    assert conflict["error"] and conflict["current_revision"] == 2
    old = call(b, "cmi_project_get", id=first["id"], include_evidence=True)
    assert old["current"] is False
    assert old["evidence"] == "pilot marker amber-owl-42"


def test_publication_rejects_missing_or_invented_quotes(tmp_path):
    a = engine(tmp_path, "a", "p")
    a.select_context([{"role": "user", "content": "actual evidence"}])
    for args in ({"quote": "invented", "source": "user:latest"},
                 {"quote": "actual", "source": "tool:does-not-exist"},
                 {"source": "user:latest"}):
        result = call(a, "cmi_project_publish", topic="claim", text="claim", **args)
        assert result.get("error")
    assert call(a, "cmi_project_search", query="claim")["results"] == []


def test_tool_evidence_available_mid_turn_and_payload_does_not_echo_itself(tmp_path):
    a = engine(tmp_path, "a", "p")
    hist = [{"role": "user", "content": "Inspect marker"},
            {"role": "assistant", "content": "", "tool_calls": [{"id": "read-1",
             "type": "function", "function": {"name": "read_file", "arguments": "{}"}}]},
            {"role": "tool", "tool_call_id": "read-1", "content": "marker is amber-owl-42"}]
    a.select_context(hist)
    r = call(a, "cmi_project_publish", topic="marker", text="amber-owl-42",
             quote="amber-owl-42", source="tool:read-1")
    assert r["id"]
    assert call(a, "cmi_project_get", id=r["id"], include_evidence=True)["evidence"] == "marker is amber-owl-42"
    # The engine's emitted memory blocks are never usable as original host evidence.
    no = call(a, "cmi_project_publish", topic="invented", text="x", quote="x", source="memory:latest")
    assert no.get("error")
