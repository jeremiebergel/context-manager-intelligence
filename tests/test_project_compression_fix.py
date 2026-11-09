"""Compression continues scope only for the same owner/project/home."""
from pathlib import Path

import pytest

from cmi.engine import CMIEngine
from tests.test_project_autobind import clean_identity_env, host, project, session, start, worker


@pytest.fixture(autouse=True)
def isolated_default_home(tmp_path, monkeypatch):
    monkeypatch.setattr("cmi.engine.default_home", lambda: tmp_path / "default")


@pytest.mark.parametrize("hint", ["environment", "explicit", "explicit_without_env"])
def test_matching_project_compression_retains_home_and_anchor(tmp_path, monkeypatch, hint):
    if hint != "explicit_without_env":
        monkeypatch.setenv("CMI_PROJECT_ID", "p")
    engine = CMIEngine(auto_project_binding=False)
    home = tmp_path / "custom"
    engine.on_session_start("parent", project_id="p", hermes_home=home)
    bridge = engine._project
    bridge.capture([{"role": "user", "content": "old evidence"}])
    child_kwargs = {} if hint == "environment" else {"project_id": "p"}
    engine.on_session_start("child", boundary_reason="compression", old_session_id="parent", **child_kwargs)
    assert engine._project.memory.path == home / "cmi_project" / "memory.db"
    assert Path(engine._project_home) == home
    assert engine._auto_binding_session_id == "parent"
    assert engine._project.session == "child"
    assert bridge.sources == {} and bridge.query == ""
    assert engine._project.sources == {} and engine._project.query == ""


@pytest.mark.parametrize("hint", ["none", "environment", "explicit"])
def test_compression_runs_storage_initialization_and_child_lifecycle(tmp_path, monkeypatch, hint):
    import os

    engine = CMIEngine(auto_project_binding=False)
    home = tmp_path / "custom"
    engine.on_session_start("parent", project_id="p", hermes_home=home)
    key = engine.store.put("surviving recall evidence")
    expired = home / "cmi_store" / "expired.txt"
    expired.write_text("expired")
    os.utime(expired, (1, 1))
    if hint == "environment":
        monkeypatch.setenv("CMI_PROJECT_ID", "p")
    kwargs = {"project_id": "p"} if hint == "explicit" else {}
    engine.on_session_reset()
    engine.on_session_start("child", boundary_reason="compression", old_session_id="parent", **kwargs)
    assert not expired.exists(), "session-start retention initialization was skipped"
    assert engine.store.root == home / "cmi_store"
    assert engine.stats.path == home / "cmi_stats.jsonl"
    assert engine.index.path == home / "cmi_index.db"
    assert engine.store.get(key) == "surviving recall evidence"
    engine.on_turn_complete([{"role": "user", "content": "child evidence"}])
    engine.select_context([{"role": "user", "content": "child evidence"}])
    assert engine._project.sources == {"user:latest": "child evidence"}
    assert engine.telemetry["select_calls"] == 1
    engine.on_session_end("child", [])
    assert list(engine.stats.rows())[-1]["s"] == "child"


@pytest.mark.parametrize("same_project_hint", [False, True])
def test_repeated_same_home_is_not_a_new_compression_authority(tmp_path, same_project_hint):
    engine = CMIEngine(auto_project_binding=False)
    home = tmp_path / "custom"
    engine.on_session_start("parent", project_id="p", hermes_home=str(home))
    kwargs = {"project_id": "p"} if same_project_hint else {}
    engine.on_session_start("child", boundary_reason="compression", old_session_id="parent", hermes_home=home, **kwargs)
    assert engine._project.project == "p"
    assert engine._auto_binding_session_id == "parent"
    assert engine._project.memory.path == home / "cmi_project" / "memory.db"


@pytest.mark.parametrize("change", ["home", "project", "env_project", "unrelated", "not_compression", "optout", "empty_env"])
def test_scope_changes_never_inherit_parent_home_or_anchor(tmp_path, monkeypatch, change):
    monkeypatch.setenv("CMI_PROJECT_ID", "p")
    engine = CMIEngine(auto_project_binding=False)
    custom = tmp_path / "custom"
    engine.on_session_start("parent", hermes_home=custom)
    old = engine._project
    old.capture([{"role": "user", "content": "parent private evidence"}])
    kwargs = dict(boundary_reason="compression", old_session_id="parent")
    expected_home = tmp_path / "default"
    expected_project = "p"
    if change == "home":
        expected_home = tmp_path / "new"
        kwargs["hermes_home"] = expected_home
    elif change == "project":
        kwargs["project_id"] = expected_project = "other"
    elif change == "env_project":
        monkeypatch.setenv("CMI_PROJECT_ID", "other")
        expected_project = "other"
    elif change == "unrelated":
        kwargs["old_session_id"] = "someone_else"
    elif change == "not_compression":
        kwargs["boundary_reason"] = "new_session"
    elif change == "optout":
        kwargs["project_id"] = expected_project = None
    else:
        monkeypatch.setenv("CMI_PROJECT_ID", "")
        expected_project = None
    engine.on_session_start("child", **kwargs)
    assert engine._auto_binding_session_id == "child"
    assert Path(engine._project_home) == expected_home
    assert engine.store.root == expected_home / "cmi_store"
    assert engine.index.path == expected_home / "cmi_index.db"
    assert engine.stats.path == expected_home / "cmi_stats.jsonl"
    assert old.sources == {} and old.query == ""
    if expected_project is None:
        assert engine._project is None
    else:
        assert engine._project.project == expected_project
        assert engine._project.memory.path == expected_home / "cmi_project" / "memory.db"


@pytest.mark.parametrize("new_project", ["p", "other"])
def test_unrelated_automatic_session_clears_old_bridge_evidence(tmp_path, new_project):
    home = tmp_path / "custom"
    host(home)
    project(home, "p", home / "repo")
    project(home, "other", home / "other_repo")
    session(home, "parent", cwd=home / "repo")
    session(home, "unrelated", cwd=home / ("repo" if new_project == "p" else "other_repo"))
    engine, _ = start(home, "parent")
    engine.select_context([{"role": "user", "content": "parent-only private evidence"}])
    old = engine._project
    assert old.sources
    engine.on_session_start("unrelated", hermes_home=home,
                            boundary_reason="compression", old_session_id="someone_else")
    assert engine._auto_binding_session_id == "unrelated"
    assert engine._project.project == new_project
    assert engine._project is not old
    assert engine._project.sources == {} and engine._project.query == ""
    assert old.sources == {} and old.query == ""


@pytest.mark.parametrize("hint", ["environment", "explicit"])
@pytest.mark.parametrize("revoked", [False, True])
def test_matching_hint_keeps_worker_policy_checks_and_source_flags(tmp_path, monkeypatch, hint, revoked):
    import json
    import sqlite3

    home = tmp_path / "custom"
    host(home)
    project(home, "p", home / "repo")
    path = worker(home, monkeypatch)
    engine, _ = start(home, "worker")
    assert engine._project_automatic is True
    engine.select_context([{"role": "user", "content": "private worker evidence"}])
    old = engine._project
    session(home, "compressed", source="kanban", parent="worker")
    if revoked:
        with sqlite3.connect(path) as db:
            db.execute("UPDATE tasks SET claim_lock='revoked'")
    if hint == "environment":
        monkeypatch.setenv("CMI_PROJECT_ID", "p")
    kwargs = {"project_id": "p"} if hint == "explicit" else {}
    engine.on_session_start("compressed", boundary_reason="compression", old_session_id="worker", **kwargs)
    assert Path(engine._project_home) == home
    assert engine._auto_binding_session_id == "worker"
    assert old.sources == {} and old.query == ""
    if revoked:
        assert engine._project is None
        assert "error" in json.loads(engine.handle_tool_call("cmi_project_search", {"query": "private"}))
    else:
        assert engine._project.project == "p"
        assert engine._project_automatic is True
        assert engine._policy_mode == "auto"
        with sqlite3.connect(path) as db:
            db.execute("UPDATE tasks SET claim_lock='revoked'")
        assert "error" in json.loads(engine.handle_tool_call("cmi_project_search", {"query": "private"}))
        assert engine._project is None
