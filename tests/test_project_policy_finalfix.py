"""Second-review regressions over real SQLite policies and cold engines."""
import json

import pytest

from tests.test_project_autobind import clean_identity_env, session, start  # noqa: F401 (fixture)
from tests.test_project_policy import setup_family
from tests.test_project_policy_revocation import mode  # noqa: F401 (fixture)


def test_compressed_explicit_scope_keeps_owner_dependency_after_cold_restart(tmp_path):
    setup_family(tmp_path)
    owner, _ = start(tmp_path, "parent", project_id="override")
    session(tmp_path, "compressed", parent="parent", cwd=tmp_path / "wrong")
    owner.on_session_start("compressed", boundary_reason="compression", old_session_id="parent")
    cold, _ = start(tmp_path, "compressed")
    assert cold._project.project == "override"
    session(tmp_path, "descendant", source="subagent", parent="compressed")
    child, _ = start(tmp_path, "descendant")
    assert child._project.project == "override"
    bridges = [cold._project, child._project]
    for engine in (cold, child):
        engine.select_context([{"role": "user", "content": "private evidence"}])
    independent, _ = start(tmp_path, "parent")
    independent.bind_project(None)
    for engine, bridge in zip((cold, child), bridges):
        result = json.loads(engine.handle_tool_call("cmi_project_publish", {
            "topic": "private", "text": "private evidence", "quote": "private evidence",
            "source": "user:latest"}))
        assert "error" in result
        assert engine._project is None
        assert bridge.sources == {}
    restarted, _ = start(tmp_path, "compressed")
    assert restarted._project is None
    own_intent, _ = start(tmp_path, "compressed", project_id="override")
    assert own_intent._project.project == "override"


@pytest.mark.parametrize("revoked", ["parent", "middle", "compressed"])
@pytest.mark.parametrize("binding", ["auto", "explicit"])
def test_cold_continuation_checks_each_owner_before_publish(tmp_path, revoked, binding):
    setup_family(tmp_path)
    kwargs = {"project_id": "override"} if binding == "explicit" else {}
    owner, _ = start(tmp_path, "parent", **kwargs)
    for previous, current in (("parent", "middle"), ("middle", "compressed")):
        session(tmp_path, current, parent=previous, cwd=tmp_path / "wrong")
        owner.on_session_start(current, boundary_reason="compression", old_session_id=previous)
    cold, _ = start(tmp_path, "compressed")
    bridge = cold._project
    assert bridge is not None
    cold.select_context([{"role": "user", "content": "private evidence"}])
    independent, _ = start(tmp_path, revoked)
    independent.bind_project(None)
    result = json.loads(cold.handle_tool_call("cmi_project_publish", {
        "topic": "private", "text": "private evidence", "quote": "private evidence",
        "source": "user:latest"}))
    assert "error" in result
    assert cold._project is None
    assert bridge.sources == {}
    restarted, _ = start(tmp_path, "compressed")
    assert restarted._project is None


def test_cold_continuation_cannot_skip_host_intermediate_optout(tmp_path):
    setup_family(tmp_path)
    owner, _ = start(tmp_path, "parent", project_id="override")
    session(tmp_path, "middle", source="subagent", parent="parent")
    start(tmp_path, "middle", project_id=None)
    session(tmp_path, "compressed", parent="middle", cwd=tmp_path / "wrong")
    owner.on_session_start("compressed", boundary_reason="compression", old_session_id="parent")
    assert owner._project is None
    cold, _ = start(tmp_path, "compressed")
    assert cold._project is None
