"""Read-only host identity fixtures: relevant columns match audited Hermes schemas."""
import json
import sqlite3

import pytest

from cmi.engine import CMIEngine


def host(home):
    home.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(home / "projects.db") as db:
        db.executescript("""
        CREATE TABLE projects (id TEXT PRIMARY KEY, slug TEXT NOT NULL UNIQUE,
          name TEXT NOT NULL, primary_path TEXT, created_at INTEGER NOT NULL,
          archived INTEGER NOT NULL DEFAULT 0);
        CREATE TABLE project_folders (project_id TEXT NOT NULL, path TEXT NOT NULL,
          label TEXT, is_primary INTEGER NOT NULL DEFAULT 0, added_at INTEGER NOT NULL,
          PRIMARY KEY(project_id,path));
        CREATE TABLE project_meta (key TEXT PRIMARY KEY, value TEXT);
        """)
    with sqlite3.connect(home / "state.db") as db:
        db.executescript("""
        CREATE TABLE sessions (id TEXT PRIMARY KEY, source TEXT NOT NULL,
          parent_session_id TEXT, cwd TEXT, started_at REAL NOT NULL,
          origin_json TEXT, session_key TEXT, model_config TEXT, end_reason TEXT);
        """)


def project(home, pid, path, archived=0):
    with sqlite3.connect(home / "projects.db") as db:
        db.execute("INSERT INTO projects VALUES (?,?,?,?,0,?)", (pid,pid,pid,str(path),archived))
        db.execute("INSERT INTO project_folders VALUES (?,?,NULL,1,0)", (pid,str(path)))


def session(home, sid, source="desktop", cwd=None, parent=None):
    with sqlite3.connect(home / "state.db") as db:
        db.execute("INSERT INTO sessions(id,source,cwd,parent_session_id,started_at) VALUES (?,?,?,?,0)",
                   (sid,source,str(cwd) if cwd else None,parent))


def start(home, sid, **kwargs):
    e = CMIEngine(auto_project_binding=True)
    schemas = e.get_tool_schemas()  # Actual core lifecycle: schemas first.
    e.on_session_start(sid, hermes_home=str(home), **kwargs)
    return e, schemas


def test_desktop_trusted_workspace_binds_after_lazy_row_creation(tmp_path, monkeypatch):
    monkeypatch.delenv("CMI_PROJECT_ID", raising=False)
    host(tmp_path)
    project(tmp_path,"p_a",tmp_path / "repo")
    e, schemas = start(tmp_path,"draft")
    assert e._project is None
    assert "cmi_project_publish" in {s["name"] for s in schemas}
    assert json.loads(e.handle_tool_call("cmi_project_search", {"query":"marker"})).get("error")
    session(tmp_path,"draft",cwd=tmp_path / "repo" / "src")
    e.select_context([{"role":"user","content":"marker decision"}])
    assert e._project.project == "p_a"
    assert e._project.session == "draft"
    assert e._project.memory.path == tmp_path / "cmi_project" / "memory.db"
    assert e.get_tool_schemas() == schemas
    result=json.loads(e.handle_tool_call("cmi_project_publish",dict(topic="marker",text="marker decision",
        quote="marker decision",source="user:latest")))
    assert result["revision"] == 1


@pytest.fixture(autouse=True)
def clean_identity_env(monkeypatch):
    for name in ("CMI_PROJECT_ID", "HERMES_KANBAN_TASK", "HERMES_KANBAN_DB",
                 "HERMES_KANBAN_RUN_ID", "HERMES_KANBAN_CLAIM_LOCK", "HERMES_KANBAN_BOARD"):
        monkeypatch.delenv(name, raising=False)


@pytest.mark.parametrize("source", ["cli", "terminal", "telegram", "kanban", "subagent"])
def test_unrelated_standalone_never_binds_from_cwd(tmp_path, source):
    host(tmp_path)
    project(tmp_path,"p",tmp_path / "repo")
    session(tmp_path,"s",source=source,cwd=tmp_path / "repo")
    e,_=start(tmp_path,"s")
    assert e._project is None
    assert not (tmp_path / "cmi_project").exists()


def test_parent_lineage_overrides_child_workspace_and_compression_keeps_home(tmp_path):
    host(tmp_path)
    project(tmp_path,"p",tmp_path / "repo")
    project(tmp_path,"other",tmp_path / "elsewhere")
    session(tmp_path,"parent",cwd=tmp_path / "repo")
    start(tmp_path,"parent")  # Establish plugin-owned parent policy before delegation.
    session(tmp_path,"child",source="subagent",cwd=tmp_path / "elsewhere",parent="parent")
    session(tmp_path,"grandchild",source="subagent",parent="child")
    child,_=start(tmp_path,"grandchild")
    assert child._project.project == "p"
    home = child._project.memory.path
    child.on_session_reset()
    child.on_session_start("compressed",boundary_reason="compression",old_session_id="grandchild")
    assert child._project.project == "p"
    assert child._project.memory.path == home
    assert child._project.session == "compressed"
    assert child._project.sources == {}


@pytest.mark.parametrize("case", ["missing", "cycle", "ambiguous", "archived", "relative", "sibling"])
def test_unsafe_host_evidence_fails_closed(tmp_path, case):
    host(tmp_path)
    project(tmp_path,"p",tmp_path / "repo",archived=int(case == "archived"))
    if case == "ambiguous":
        project(tmp_path,"other",tmp_path / "repo")
    cwd = "repo" if case == "relative" else tmp_path / ("repository" if case == "sibling" else "repo")
    parent = "missing" if case == "missing" else "s" if case == "cycle" else None
    session(tmp_path,"s",cwd=cwd,parent=parent)
    e,_=start(tmp_path,"s")
    assert e._project is None


def test_longest_canonical_registered_folder_not_global_active(tmp_path):
    host(tmp_path)
    project(tmp_path,"outer",tmp_path / "repo")
    project(tmp_path,"inner",tmp_path / "repo" / "nested")
    with sqlite3.connect(tmp_path / "projects.db") as db:
        db.execute("INSERT INTO project_meta VALUES ('active_id','outer')")
    session(tmp_path,"s",cwd=tmp_path / "repo" / "nested" / "src")
    e,_=start(tmp_path,"s")
    assert e._project.project == "inner"


@pytest.mark.parametrize("optout", ["argument", "empty_env", "method", "compression"])
def test_explicit_optout_cannot_be_overridden_by_auto_binding(tmp_path, monkeypatch, optout):
    host(tmp_path)
    project(tmp_path,"p",tmp_path / "repo")
    session(tmp_path,"s",cwd=tmp_path / "repo")
    if optout == "empty_env":
        monkeypatch.setenv("CMI_PROJECT_ID", "")
    e,_=start(tmp_path,"s",**({"project_id":None} if optout in ("argument","compression") else {}))
    if optout == "method":
        e.bind_project(None)
    if optout == "compression":
        e.on_session_start("s",boundary_reason="compression",old_session_id="s")
    e.select_context([{"role":"user","content":"marker"}])
    assert e._project is None
    assert json.loads(e.handle_tool_call("cmi_project_search",{"query":"marker"})).get("error")


def worker(home, monkeypatch, *, pid="p", run=7, claim="grant", status="running", source="kanban"):
    # Real boards have per-board DBs, not a boards SQL table; board metadata is board.json.
    path=home / "kanban" / "boards" / "team" / "kanban.db"
    path.parent.mkdir(parents=True)
    with sqlite3.connect(path) as db:
        db.executescript("""CREATE TABLE tasks (id TEXT PRIMARY KEY, title TEXT NOT NULL,
          status TEXT NOT NULL, created_at INTEGER NOT NULL, project_id TEXT,
          current_run_id INTEGER, claim_lock TEXT, workspace_path TEXT, session_id TEXT, claim_expires INTEGER);
        CREATE TABLE task_runs (id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT NOT NULL,
          status TEXT NOT NULL, claim_lock TEXT, worker_pid INTEGER);
        """)
        db.execute("INSERT INTO tasks VALUES ('task','work',?,0,?,?,?,NULL,NULL,NULL)", (status,pid,run,claim))
        db.execute("INSERT INTO task_runs VALUES (?,'task',?,?,NULL)", (run,status,claim))
    (path.parent / "board.json").write_text(json.dumps({"slug":"team","project_id":"wrong-board-project"}))
    session(home,"worker",source=source)
    for name,value in {"HERMES_KANBAN_TASK":"task","HERMES_KANBAN_DB":str(path),
                       "HERMES_KANBAN_RUN_ID":"7","HERMES_KANBAN_CLAIM_LOCK":"grant",
                       "HERMES_KANBAN_BOARD":"team"}.items():
        monkeypatch.setenv(name,value)
    return path


def test_worker_uses_exact_pinned_db_task_and_active_grant(tmp_path, monkeypatch):
    host(tmp_path)
    project(tmp_path,"p",tmp_path / "repo")
    path=worker(tmp_path,monkeypatch)
    # Conflicting default-board task must not enter resolution.
    with sqlite3.connect(tmp_path / "kanban.db") as db:
        db.execute("CREATE TABLE tasks (id TEXT,project_id TEXT)")
        db.execute("INSERT INTO tasks VALUES ('task','wrong')")
    e,_=start(tmp_path,"worker")
    assert e._project.project == "p"
    session(tmp_path,"child",source="subagent",parent="worker")
    child,_=start(tmp_path,"child")
    assert child._project.project == "p"
    # Worker binding is a scoped grant, not a permanent task-id guess.
    with sqlite3.connect(path) as db:
        db.execute("UPDATE tasks SET current_run_id=8,claim_lock='new-grant'")
    stale,_=start(tmp_path,"worker")
    assert stale._project is None


@pytest.mark.parametrize("case", ["no_pin","relative_pin","wrong_run","wrong_claim","done", "no_project", "unknown_project", "cli", "unrelated"])
def test_worker_missing_or_mismatched_scope_fails_closed(tmp_path, monkeypatch, case):
    host(tmp_path)
    project(tmp_path,"p",tmp_path / "repo")
    worker(tmp_path,monkeypatch,run=8 if case=="wrong_run" else 7,
           claim="other" if case=="wrong_claim" else "grant",
           status="done" if case=="done" else "running",
           pid=None if case=="no_project" else "unknown" if case=="unknown_project" else "p",
           source="cli" if case=="cli" else "kanban")
    if case=="no_pin": monkeypatch.delenv("HERMES_KANBAN_DB")
    if case=="relative_pin": monkeypatch.setenv("HERMES_KANBAN_DB","kanban.db")
    if case=="unrelated": session(tmp_path,"unrelated",source="cli")
    e,_=start(tmp_path,"unrelated" if case=="unrelated" else "worker")
    assert e._project is None


def test_no_host_metadata_or_corrupt_database_creates_no_host_files(tmp_path):
    e,_=start(tmp_path,"unknown")
    assert e._project is None
    assert not (tmp_path / "state.db").exists()
    assert not (tmp_path / "projects.db").exists()
    (tmp_path / "state.db").write_text("not sqlite")
    e,_=start(tmp_path,"unknown")
    assert e._project is None


def test_config_is_opt_in_and_default_schemas_remain_unchanged(monkeypatch):
    import cmi.project_binding as binding
    monkeypatch.setattr(binding,"auto_binding_enabled",lambda:True)
    e=CMIEngine()
    assert "cmi_project_publish" in {s["name"] for s in e.get_tool_schemas()}
    off=CMIEngine(auto_project_binding=False)
    assert "cmi_project_publish" not in {s["name"] for s in off.get_tool_schemas()}


def test_bound_worker_is_revoked_before_reads_and_publishes(tmp_path, monkeypatch):
    host(tmp_path)
    project(tmp_path,"p",tmp_path / "repo")
    path=worker(tmp_path,monkeypatch)
    e,_=start(tmp_path,"worker")
    e.select_context([{"role":"user","content":"supported result"}])
    with sqlite3.connect(path) as db:
        db.execute("UPDATE tasks SET status='done'")
    result=json.loads(e.handle_tool_call("cmi_project_publish",dict(topic="result",text="supported result",
        quote="supported result",source="user:latest")))
    assert "error" in result
    assert e._project is None


def test_worker_terminal_run_record_fails_closed(tmp_path, monkeypatch):
    host(tmp_path)
    project(tmp_path,"p",tmp_path / "repo")
    path=worker(tmp_path,monkeypatch)
    with sqlite3.connect(path) as db:
        db.execute("UPDATE task_runs SET status='done'")
    e,_=start(tmp_path,"worker")
    assert e._project is None


def test_explicit_binding_is_not_revalidated_against_automatic_host_metadata(tmp_path):
    host(tmp_path)
    project(tmp_path,"p",tmp_path / "repo")
    session(tmp_path,"s",cwd=tmp_path / "repo")
    e,_=start(tmp_path,"s",project_id="explicit")
    e.select_context([{"role":"user","content":"decision"}])
    assert e._project.project == "explicit"


def test_expired_worker_grant_is_not_authority(tmp_path, monkeypatch):
    host(tmp_path)
    project(tmp_path,"p",tmp_path / "repo")
    path=worker(tmp_path,monkeypatch)
    with sqlite3.connect(path) as db:
        db.execute("UPDATE tasks SET claim_expires=1")
    e,_=start(tmp_path,"worker")
    assert e._project is None


def test_publication_instruction_does_not_require_user_save_request():
    from cmi.project_bridge import ProjectBridge
    desc=next(s["description"] for s in ProjectBridge.schemas() if s["name"]=="cmi_project_publish")
    assert "without waiting" in desc
    assert "assistant" in desc
    assert "exact quote" in desc


def test_repeated_compression_keeps_trusted_anchor_and_checks_revocation(tmp_path):
    host(tmp_path)
    project(tmp_path,"p",tmp_path / "repo")
    session(tmp_path,"s",cwd=tmp_path / "repo")
    e,_=start(tmp_path,"s")
    for old,new in [("s","compressed1"),("compressed1","compressed2")]:
        e.on_session_start(new,old_session_id=old,boundary_reason="compression")
        e.select_context([{"role":"user","content":"current task"}])
        assert e._project.project == "p"
        assert e._project_automatic
    with sqlite3.connect(tmp_path / "projects.db") as db:
        db.execute("UPDATE projects SET archived=1")
    e.select_context([{"role":"user","content":"current task"}])
    assert e._project is None


def test_registered_secondary_folder_is_host_evidence(tmp_path):
    host(tmp_path)
    project(tmp_path,"p",tmp_path / "primary")
    with sqlite3.connect(tmp_path / "projects.db") as db:
        db.execute("INSERT INTO project_folders VALUES (?, ?, NULL, 0, 0)",("p",str(tmp_path / "secondary")))
    session(tmp_path,"s",cwd=tmp_path / "secondary" / "nested")
    e,_=start(tmp_path,"s")
    assert e._project.project == "p"


def test_child_of_unrelated_cli_does_not_bind_by_its_own_workspace(tmp_path):
    host(tmp_path)
    project(tmp_path,"p",tmp_path / "repo")
    session(tmp_path,"parent",source="cli",cwd=tmp_path / "repo")
    session(tmp_path,"child",source="subagent",cwd=tmp_path / "repo",parent="parent")
    e,_=start(tmp_path,"child")
    assert e._project is None


@pytest.mark.parametrize("value,expected",[(True,True),(False,False),("true",False),(1,False)])
def test_config_boolean_false_and_non_boolean_values_turn_it_off(monkeypatch,value,expected):
    import sys
    import types
    config=types.ModuleType("hermes_cli.config")
    config.load_config_readonly=lambda: {"context":{"cmi":{"auto_project_binding":value}}}
    monkeypatch.setitem(sys.modules,"hermes_cli.config",config)
    assert CMIEngine()._auto_project_binding is expected


def test_default_is_on_inside_hermes_when_the_key_is_absent_and_off_outside(monkeypatch):
    import sys
    import types
    config=types.ModuleType("hermes_cli.config")
    config.load_config_readonly=lambda: {"context":{"engine":"cmi"}}
    monkeypatch.setitem(sys.modules,"hermes_cli.config",config)
    assert CMIEngine()._auto_project_binding is True
    monkeypatch.setitem(sys.modules,"hermes_cli.config",None)  # standalone / proxy: import fails
    assert CMIEngine()._auto_project_binding is False


def test_current_hermes_desktop_sources_bind_but_other_sources_do_not(tmp_path, monkeypatch):
    """Hermes >= 0.22.x stores Desktop chats as source 'local'/'gui' (not 'desktop'): auto-binding
    matched nothing for them (live check 2026-11-05). Cron, telegram, oneshot stay unbound."""
    from cmi.project_binding import resolve_project
    monkeypatch.delenv("CMI_PROJECT_ID", raising=False)
    host(tmp_path)
    project(tmp_path, "p_a", tmp_path / "repo")
    for sid, source, expected in (("d", "desktop", "p_a"), ("l", "local", "p_a"), ("g", "gui", "p_a"),
                                  ("c", "cron", None), ("t", "telegram", None), ("o", "oneshot", None)):
        session(tmp_path, sid, source=source, cwd=tmp_path / "repo" / "src")
        assert resolve_project(tmp_path, sid) == expected, source
    session(tmp_path, "elsewhere", source="local", cwd=tmp_path / "other")
    assert resolve_project(tmp_path, "elsewhere") is None


def test_binding_survives_hermes_flipping_source_gui_to_local(tmp_path, monkeypatch):
    """Live incident 2026-11-05: Desktop creates the session as 'gui', Hermes rewrites it to 'local'
    after the first message. The stored evidence no longer matched and CMI switched project memory
    off mid-conversation ('Project memory is not enabled for this session' on publish)."""
    monkeypatch.delenv("CMI_PROJECT_ID", raising=False)
    host(tmp_path)
    project(tmp_path, "p_a", tmp_path / "repo")
    session(tmp_path, "s1", source="gui", cwd=tmp_path / "repo")
    e, _ = start(tmp_path, "s1")
    assert "not enabled" not in e.handle_tool_call("cmi_project_search", {"query": "x"})
    with sqlite3.connect(tmp_path / "state.db") as db:
        db.execute("UPDATE sessions SET source='local' WHERE id='s1'")
    out = e.handle_tool_call("cmi_project_search", {"query": "x"})
    assert "not enabled" not in out, out
    # a fresh engine for the same session (new request / restart) still resolves the project
    e2, _ = start(tmp_path, "s1")
    assert "not enabled" not in e2.handle_tool_call("cmi_project_search", {"query": "x"})
