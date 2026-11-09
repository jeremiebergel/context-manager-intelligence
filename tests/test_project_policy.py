"""Plugin policy propagation over real host rows, never child workspace."""
import json
import sqlite3
from pathlib import Path

import pytest

from tests.test_project_autobind import host, project, session, start, worker, clean_identity_env


def setup_family(home):
    host(home)
    project(home, "p", home / "repo")
    project(home, "override", home / "other")
    session(home, "parent", cwd=home / "repo")
    session(home, "child", source="subagent", cwd=home / "other", parent="parent")


def test_private_parent_optout_survives_independent_child(tmp_path):
    setup_family(tmp_path)
    parent, _ = start(tmp_path, "parent")
    parent.bind_project(None)
    child, _ = start(tmp_path, "child")
    assert child._project is None


def test_private_parent_override_survives_independent_child(tmp_path):
    setup_family(tmp_path)
    parent, _ = start(tmp_path, "parent")
    parent.bind_project("override")
    child, _ = start(tmp_path, "child")
    assert child._project.project == "override"
    assert child._project.memory.path == tmp_path / "cmi_project" / "memory.db"


@pytest.mark.parametrize("change", ["claim", "run", "source", "archive", "project"])
def test_inherited_worker_override_does_not_bypass_revocation(tmp_path, monkeypatch, change):
    host(tmp_path)
    project(tmp_path, "p", tmp_path / "repo")
    project(tmp_path, "override", tmp_path / "other")
    path = worker(tmp_path, monkeypatch)
    parent, _ = start(tmp_path, "worker", project_id="p")
    session(tmp_path, "child", source="subagent", parent="worker")
    child, _ = start(tmp_path, "child")
    assert child._project.project == "p"
    bridge = child._project
    child.select_context([{"role": "user", "content": "private evidence"}])
    if change in ("claim", "run", "project"):
        with sqlite3.connect(path) as db:
            db.execute({"claim": "UPDATE tasks SET claim_lock='revoked'",
                        "run": "UPDATE tasks SET current_run_id=8",
                        "project": "UPDATE tasks SET project_id='override'"}[change])
    elif change == "source":
        with sqlite3.connect(tmp_path / "state.db") as db:
            db.execute("UPDATE sessions SET source='cli' WHERE id='worker'")
    else:
        with sqlite3.connect(tmp_path / "projects.db") as db:
            db.execute("UPDATE projects SET archived=1 WHERE id='p'")
    child.select_context([{"role": "user", "content": "private evidence"}])
    assert child._project is None
    assert bridge.sources == {}
    assert json.loads(child.handle_tool_call("cmi_project_search", {"query": "private"}))["error"]
    assert child._project is None


def test_own_worker_override_still_checks_grant(tmp_path, monkeypatch):
    host(tmp_path)
    project(tmp_path, "p", tmp_path / "repo")
    path = worker(tmp_path, monkeypatch)
    engine, _ = start(tmp_path, "worker", project_id="p")
    with sqlite3.connect(path) as db:
        db.execute("UPDATE task_runs SET status='done'")
    engine.select_context([{"role": "user", "content": "decision"}])
    assert engine._project is None


@pytest.mark.parametrize("mode", ["off", "override", "auto"])
def test_compression_preserves_policy_home_and_real_child_lineage(tmp_path, mode):
    setup_family(tmp_path)
    parent, _ = start(tmp_path, "parent")
    if mode == "off":
        parent.bind_project(None)
    elif mode == "override":
        parent.bind_project("override")
    session(tmp_path, "compressed", source="desktop", cwd=tmp_path / "wrong", parent="parent")
    parent.on_session_start("compressed", boundary_reason="compression", old_session_id="parent")
    assert Path(parent._project_home) == tmp_path
    assert parent._auto_binding_session_id == "parent"
    session(tmp_path, "descendant", source="subagent", cwd=tmp_path / "other", parent="compressed")
    child, _ = start(tmp_path, "descendant")
    if mode == "off":
        assert child._project is None
    else:
        assert child._project.project == ("override" if mode == "override" else "p")


def test_child_own_override_wins_over_parent_optout(tmp_path):
    setup_family(tmp_path)
    parent, _ = start(tmp_path, "parent", project_id=None)
    child, _ = start(tmp_path, "child", project_id="override")
    child.select_context([{"role": "user", "content": "decision"}])
    assert child._project.project == "override"


@pytest.mark.parametrize("damage", ["missing", "corrupt", "mode", "directory_mode", "symlink", "record"])
def test_registry_damage_latches_bound_child_off(tmp_path, damage):
    if damage in ("mode", "directory_mode") and __import__("os").name == "nt":
        pytest.skip("POSIX permission bits do not exist on Windows")
    setup_family(tmp_path)
    start(tmp_path, "parent")
    child, _ = start(tmp_path, "child")
    path = tmp_path / "cmi_policy" / "sessions.db"
    data = path.read_bytes()
    if damage == "missing":
        path.unlink()
    elif damage == "corrupt":
        path.write_bytes(b"not sqlite")
    elif damage == "mode":
        path.chmod(0o644)
    elif damage == "directory_mode":
        path.parent.chmod(0o755)
    elif damage == "symlink":
        target = tmp_path / "target"
        target.write_bytes(data)
        target.chmod(0o600)
        path.unlink()
        path.symlink_to(target)
    else:
        with sqlite3.connect(path) as db:
            db.execute("UPDATE policies SET evidence='{}' WHERE session='parent'")
    child.select_context([{"role": "user", "content": "decision"}])
    assert child._project is None
    if path.is_symlink():
        path.unlink()
    path.write_bytes(data)
    path.chmod(0o600)
    path.parent.chmod(0o700)
    child.select_context([{"role": "user", "content": "decision"}])
    assert child._project is None


def test_missing_parent_policy_never_uses_child_cwd(tmp_path):
    setup_family(tmp_path)
    child, _ = start(tmp_path, "child")
    assert child._project is None


def test_unknown_host_id_cannot_write_policy(tmp_path):
    from cmi.project_policy import persist
    host(tmp_path)
    assert not persist(tmp_path, "invented", "explicit", "p")
    assert not (tmp_path / "cmi_policy").exists()


@pytest.mark.parametrize("backend", ["thread", "process"])
def test_concurrent_policy_writers_keep_all_records_and_private_modes(tmp_path, backend):
    from concurrent.futures import ThreadPoolExecutor
    from cmi.project_policy import persist
    import stat
    host(tmp_path)
    for index in range(24):
        session(tmp_path, str(index), cwd=tmp_path / "repo")
    def save(index):
        if backend == "thread":
            return persist(tmp_path, str(index), "off")
        import subprocess
        import sys
        result = subprocess.run([sys.executable, "-c",
            "from cmi.project_policy import persist; import sys; "
            "sys.exit(0 if persist(sys.argv[1],sys.argv[2],'off') else 1)",
            str(tmp_path), str(index)], capture_output=True, timeout=10)
        return result.returncode == 0
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(save, range(24)))
    assert all(results)
    directory = tmp_path / "cmi_policy"
    assert __import__("os").name == "nt" or stat.S_IMODE(directory.stat().st_mode) == 0o700
    for file in directory.iterdir():
        assert __import__("os").name == "nt" or stat.S_IMODE(file.stat().st_mode) == 0o600
    with sqlite3.connect(directory / "sessions.db") as db:
        assert db.execute("SELECT COUNT(*) FROM policies").fetchone()[0] == 24


def test_bound_root_missing_registry_fails_closed(tmp_path):
    setup_family(tmp_path)
    engine, _ = start(tmp_path, "parent")
    (tmp_path / "cmi_policy" / "sessions.db").unlink()
    engine.select_context([{"role": "user", "content": "decision"}])
    assert engine._project is None


def test_auto_project_change_stays_off_across_restart(tmp_path):
    setup_family(tmp_path)
    start(tmp_path, "parent")
    with sqlite3.connect(tmp_path / "projects.db") as db:
        for pid, path in (("p", tmp_path / "gone"), ("override", tmp_path / "repo")):
            db.execute("UPDATE projects SET primary_path=? WHERE id=?", (str(path), pid))
            db.execute("UPDATE project_folders SET path=? WHERE project_id=?", (str(path), pid))
    resumed, _ = start(tmp_path, "parent")
    assert resumed._project is None
    with sqlite3.connect(tmp_path / "projects.db") as db:
        for pid, path in (("p", tmp_path / "repo"), ("override", tmp_path / "other")):
            db.execute("UPDATE projects SET primary_path=? WHERE id=?", (str(path), pid))
            db.execute("UPDATE project_folders SET path=? WHERE project_id=?", (str(path), pid))
    resumed_again, _ = start(tmp_path, "parent")
    assert resumed_again._project is None


def test_compression_revocation_updates_original_anchor(tmp_path, monkeypatch):
    host(tmp_path)
    project(tmp_path, "p", tmp_path / "repo")
    path = worker(tmp_path, monkeypatch)
    engine, _ = start(tmp_path, "worker")
    session(tmp_path, "compressed", source="kanban", parent="worker")
    engine.on_session_start("compressed", boundary_reason="compression", old_session_id="worker")
    with sqlite3.connect(path) as db:
        db.execute("UPDATE task_runs SET status='done'")
    engine.select_context([{"role": "user", "content": "decision"}])
    assert engine._project is None
    with sqlite3.connect(path) as db:
        db.execute("UPDATE task_runs SET status='running'")
    restarted, _ = start(tmp_path, "worker")
    assert restarted._project is None


@pytest.mark.parametrize("damage", ["empty", "symlink", "hardlink", "lockmode"])
def test_bad_registry_never_repaired(tmp_path, damage):
    if damage == "lockmode" and __import__("os").name == "nt":
        pytest.skip("POSIX permission bits do not exist on Windows")
    from cmi.project_policy import persist
    setup_family(tmp_path)
    start(tmp_path, "parent")
    path = tmp_path / "cmi_policy" / "sessions.db"
    if damage == "empty":
        path.write_bytes(b"")
    elif damage == "symlink":
        target = tmp_path / "untouched"
        path.rename(target)
        path.symlink_to(target)
    elif damage == "hardlink":
        import os
        os.link(path, tmp_path / "alias")
    else:
        (path.parent / "registry.lock").chmod(0o644)
    assert persist(tmp_path, "child", "off") is False
    child, _ = start(tmp_path, "child")
    assert child._project is None
    if damage == "empty":
        assert path.read_bytes() == b""


def test_registry_sqlite_lock_fails_closed(tmp_path):
    from cmi.project_policy import persist
    setup_family(tmp_path)
    start(tmp_path, "parent")
    with sqlite3.connect(tmp_path / "cmi_policy" / "sessions.db") as db:
        db.execute("BEGIN EXCLUSIVE")
        assert persist(tmp_path, "child", "off") is False
        child, _ = start(tmp_path, "child")
        assert child._project is None


def test_profile_isolation(tmp_path):
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()
    setup_family(first)
    setup_family(second)
    parent, _ = start(first, "parent")
    parent.bind_project("override")
    child, _ = start(second, "child")
    assert child._project is None


def test_worker_override_cannot_change_granted_project(tmp_path, monkeypatch):
    host(tmp_path)
    project(tmp_path, "p", tmp_path / "repo")
    project(tmp_path, "other", tmp_path / "other")
    worker(tmp_path, monkeypatch)
    engine, _ = start(tmp_path, "worker", project_id="other")
    assert engine._project is None


def test_changed_parent_override_latches_child_off(tmp_path):
    setup_family(tmp_path)
    parent, _ = start(tmp_path, "parent")
    child, _ = start(tmp_path, "child")
    parent.bind_project("override")
    child.select_context([{"role": "user", "content": "decision"}])
    assert child._project is None
    parent.bind_project("p")
    child.select_context([{"role": "user", "content": "decision"}])
    assert child._project is None
    restarted, _ = start(tmp_path, "child")
    assert restarted._project is None


def test_explicit_compression_optout_beats_inherited_binding(tmp_path):
    setup_family(tmp_path)
    parent, _ = start(tmp_path, "parent")
    session(tmp_path, "compressed", source="desktop", parent="parent")
    parent.on_session_start("compressed", boundary_reason="compression",
                            old_session_id="parent", project_id=None, hermes_home=tmp_path)
    assert parent._project is None
    session(tmp_path, "later", source="subagent", parent="compressed")
    child, _ = start(tmp_path, "later")
    assert child._project is None



