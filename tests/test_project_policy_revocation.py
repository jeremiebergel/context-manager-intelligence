"""Durable revocations and real independent-engine interleavings."""
import json
import sqlite3

import pytest

from tests.test_project_autobind import clean_identity_env, session, start
from tests.test_project_policy import setup_family


def mode(home, sid):
    with sqlite3.connect(home / "cmi_policy" / "sessions.db") as db:
        return db.execute("SELECT mode,project FROM policies WHERE session=?", (sid,)).fetchone()


def test_restart_never_exposes_transient_off_to_concurrent_child(tmp_path, monkeypatch):
    import cmi.project_policy as policy
    setup_family(tmp_path)
    start(tmp_path, "parent")
    child, _ = start(tmp_path, "child")
    original = policy.persist
    observed = []

    def interleave(home, sid, policy_mode, project=None, **kwargs):
        result = original(home, sid, policy_mode, project, **kwargs)
        if sid == "parent":
            observed.append(mode(tmp_path, sid))
            child.select_context([{"role": "user", "content": "decision"}])
        return result

    monkeypatch.setattr(policy, "persist", interleave)
    resumed, _ = start(tmp_path, "parent")
    assert resumed._project.project == "p"
    assert child._project is not None, observed
    assert all(row[0] != "off" for row in observed)


@pytest.mark.parametrize("sid", ["parent", "child"])
def test_explicit_override_survives_cold_restart(tmp_path, sid):
    setup_family(tmp_path)
    parent, _ = start(tmp_path, "parent")
    if sid == "child":
        parent.bind_project(None)
    owner, _ = start(tmp_path, sid, project_id="override")
    assert mode(tmp_path, sid) == ("explicit", "override")
    resumed, _ = start(tmp_path, sid)
    resumed.select_context([{"role": "user", "content": "decision"}])
    assert resumed._project is not None
    assert resumed._project.project == "override"
    assert resumed._policy_mode == "explicit"
    assert mode(tmp_path, sid) == ("explicit", "override")


@pytest.mark.parametrize("existing", [False, True])
def test_owner_optout_between_read_and_reconstruction_is_not_overwritten(tmp_path, monkeypatch, existing):
    import cmi.project_policy as policy
    setup_family(tmp_path)
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event
    if existing:
        start(tmp_path, "parent")
    original = policy.effective_project
    ready, release = Event(), Event()
    fired = False

    def interleave(home, sid, **kwargs):
        nonlocal fired
        decision = original(home, sid, **kwargs)
        if not fired and sid == "parent":
            fired = True
            ready.set()
            assert release.wait(5), "owner never released stale reconstruction"
        return decision

    monkeypatch.setattr(policy, "effective_project", interleave)
    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(start, tmp_path, "parent")
        try:
            assert ready.wait(5), "resumed engine never reached stale read"
            start(tmp_path, "parent", project_id=None)
            assert mode(tmp_path, "parent") == ("off", None)
        finally:
            release.set()
        resumed, _ = pending.result(timeout=5)
    assert fired
    assert mode(tmp_path, "parent") == ("off", None)
    assert resumed._project is None


def test_missing_lock_with_intact_optout_is_not_bootstrap(tmp_path):
    setup_family(tmp_path)
    owner, _ = start(tmp_path, "parent")
    owner.bind_project(None)
    path = tmp_path / "cmi_policy" / "sessions.db"
    before = path.read_bytes()
    lock = path.parent / "registry.lock"
    lock.unlink()
    resumed, _ = start(tmp_path, "parent")
    assert resumed._project is None
    assert mode(tmp_path, "parent") == ("off", None)
    assert not lock.exists()
    assert path.read_bytes() == before


@pytest.mark.parametrize("revoked", ["compressed", "middle", "parent"])
def test_compression_revalidates_every_durable_owner_before_publish(tmp_path, revoked):
    setup_family(tmp_path)
    engine, _ = start(tmp_path, "parent")
    session(tmp_path, "middle", parent="parent", cwd=tmp_path / "wrong")
    engine.on_session_start("middle", boundary_reason="compression", old_session_id="parent")
    session(tmp_path, "compressed", parent="middle", cwd=tmp_path / "wrong")
    engine.on_session_start("compressed", boundary_reason="compression", old_session_id="middle")
    bridge = engine._project
    engine.select_context([{"role": "user", "content": "private evidence"}])
    assert bridge.sources
    independent, _ = start(tmp_path, revoked)
    independent.bind_project(None)
    result = json.loads(engine.handle_tool_call("cmi_project_publish", {
        "topic": "private", "text": "private evidence", "quote": "private evidence",
        "source": "user:latest"}))
    assert "error" in result
    assert engine._project is None
    assert bridge.sources == {}
    assert mode(tmp_path, revoked) == ("off", None)


def test_stale_reconstruction_failure_does_not_replace_new_explicit_intent(tmp_path, monkeypatch):
    setup_family(tmp_path)
    owner, _ = start(tmp_path, "parent")
    from cmi.project_bridge import ProjectBridge
    original = ProjectBridge.__init__
    fired = False

    def interleave(bridge, *args, **kwargs):
        nonlocal fired
        original(bridge, *args, **kwargs)
        if not fired:
            fired = True
            owner.bind_project("override")

    monkeypatch.setattr(ProjectBridge, "__init__", interleave)
    resumed, _ = start(tmp_path, "parent")
    assert fired
    assert resumed._project is None
    assert mode(tmp_path, "parent") == ("explicit", "override")
    fresh, _ = start(tmp_path, "parent")
    assert fresh._project.project == "override"


def test_compression_checks_host_intermediate_not_seen_by_engine(tmp_path):
    setup_family(tmp_path)
    engine, _ = start(tmp_path, "parent")
    session(tmp_path, "middle", source="subagent", parent="parent")
    start(tmp_path, "middle", project_id=None)
    session(tmp_path, "compressed", parent="middle", cwd=tmp_path / "wrong")
    engine.on_session_start("compressed", boundary_reason="compression", old_session_id="parent")
    assert engine._project is None
    assert mode(tmp_path, "middle") == ("off", None)
