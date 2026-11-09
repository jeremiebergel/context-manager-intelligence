"""Regressions for project-memory pilot boundary and input handling."""
import json
import multiprocessing
import sqlite3

import pytest

from cmi.engine import CMIEngine
from cmi.project_bridge import ProjectBridge
from cmi.project_memory import SQLITE_BUSY, ProjectMemory

SQLITE_ERROR = 1
from cold_worker import cold_init_worker as _cold_init_worker


@pytest.mark.parametrize("query", [None, {}, 42, "amber\x00marker"])
def test_invalid_search_query_is_structured(tmp_path, query):
    b = ProjectBridge(tmp_path, "p", "s")
    with pytest.raises(ValueError, match="query"):
        b.memory.search("p", query)
    result = b.handle("cmi_project_search", {"query": query})
    assert isinstance(result.get("error"), str)
    assert "query" in result["error"]


@pytest.mark.parametrize("failure", ["nul", "locked"])
def test_retrieval_failure_preserves_completed_compression(tmp_path, monkeypatch, failure):
    e = CMIEngine()
    e.on_session_start("s", project_id="p", hermes_home=str(tmp_path))
    original = [{"role": "user", "content": "amber\x00marker" if failure == "nul" else "amber"}]
    completed = [{"role": "user", "content": "completed compression"}]
    monkeypatch.setattr(e, "_select_impl", lambda *args, **kwargs: completed)
    if failure == "locked":
        def locked(*args, **kwargs):
            raise sqlite3.OperationalError("database is locked")
        monkeypatch.setattr(e._project.memory, "search", locked)
    assert e.select_context(original) is completed


def test_database_operational_errors_are_structured_not_programming_errors(tmp_path, monkeypatch):
    b = ProjectBridge(tmp_path, "p", "s")
    def locked(*args, **kwargs):
        raise sqlite3.OperationalError("database is locked")
    monkeypatch.setattr(b.memory, "search", locked)
    assert "error" in b.handle("cmi_project_search", {"query": "amber"})
    def bug(*args, **kwargs):
        raise sqlite3.ProgrammingError("programming defect")
    monkeypatch.setattr(b.memory, "search", bug)
    with pytest.raises(sqlite3.ProgrammingError):
        b.handle("cmi_project_search", {"query": "amber"})
    with pytest.raises(sqlite3.ProgrammingError):
        b.prepare([])


def test_multiprocess_cold_initialization(tmp_path):
    ctx = multiprocessing.get_context("spawn")
    errors = []
    for batch in range(6):
        path = tmp_path / str(batch) / "cold.db"
        assert not path.exists()
        barrier, results = ctx.Barrier(16), ctx.Queue()
        workers = [ctx.Process(target=_cold_init_worker, args=(path, barrier, results)) for _ in range(16)]
        try:
            for worker in workers:
                worker.start()
            errors.extend(results.get(timeout=40) for _ in workers)
            for worker in workers:
                worker.join(timeout=10)
                assert worker.exitcode == 0
            if any(errors):
                break
            assert len(ProjectMemory(path).search("p", "cold")) == 5
            assert len(ProjectMemory(path).search("p", "cold", limit=100)) == 16
        finally:
            for worker in workers:
                if worker.is_alive():
                    worker.terminate()
                    worker.join(timeout=10)
            results.close()
            results.join_thread()
    assert not any(errors), errors


def test_cold_init_retry_exhaustion_is_bounded_and_closes_all(tmp_path, monkeypatch):
    real_connect = sqlite3.connect
    connections, sleeps = [], []
    class BusyConnection(sqlite3.Connection):
        closed = False
        def execute(self, statement, *args):
            error = sqlite3.OperationalError("database is locked")
            error.sqlite_errorcode = SQLITE_BUSY
            raise error
        def close(self):
            self.closed = True
            super().close()
    def connect(*args, **kwargs):
        db = real_connect(*args, **kwargs, factory=BusyConnection)
        connections.append(db)
        return db
    monkeypatch.setattr("cmi.project_memory.sqlite3.connect", connect)
    monkeypatch.setattr("cmi.project_memory.time.sleep", sleeps.append)
    with pytest.raises(sqlite3.OperationalError, match="database is locked"):
        ProjectMemory(tmp_path / "cold.db")
    assert len(connections) == 8
    assert len(sleeps) == 7
    assert all(db.closed for db in connections)


@pytest.mark.parametrize("code,expected_attempts", [(SQLITE_BUSY, 2), (SQLITE_ERROR, 1)])
def test_connect_initialization_closes_failures(tmp_path, monkeypatch, code, expected_attempts):
    real_connect = sqlite3.connect
    connections = []
    class FailingConnection(sqlite3.Connection):
        closed = False
        def execute(self, statement, *args):
            if statement == "PRAGMA journal_mode=WAL" and len(connections) == 1:
                error = sqlite3.OperationalError("database is locked" if code == SQLITE_BUSY else "broken pragma")
                error.sqlite_errorcode = code
                raise error
            return super().execute(statement, *args)
        def close(self):
            self.closed = True
            super().close()
    def connect(*args, **kwargs):
        db = real_connect(*args, **kwargs, factory=FailingConnection)
        connections.append(db)
        return db
    monkeypatch.setattr("cmi.project_memory.sqlite3.connect", connect)
    if code == SQLITE_BUSY:
        ProjectMemory(tmp_path / "cold.db")
    else:
        with pytest.raises(sqlite3.OperationalError, match="broken pragma"):
            ProjectMemory(tmp_path / "cold.db")
    assert len(connections) == expected_attempts
    assert all(db.closed for db in connections)


# Host examples: conversation_compression._is_real_user_message and
# ContextCompressor._is_synthetic_compression_user_turn (no Hermes import required).
_SYNTHETIC_ROWS = [
    {"_todo_snapshot_synthetic": True}, {"_empty_recovery_synthetic": True},
    {"_verification_stop_synthetic": True}, {"_pre_verify_synthetic": True},
    {"_dropped_toolcall_nudge": True}, {"_kanban_stop_synthetic": True},
    {"_compressed_summary": True},
    {"content": "[CONTEXT SUMMARY]: old decisions"},
    {"content": "[CONTEXT COMPACTION — REFERENCE ONLY] Earlier turns were compacted into the summary below."},
    {"content": "prior context\n[END OF PRIOR CONTEXT — COMPACTION SUMMARY BELOW]\n[CONTEXT SUMMARY]: old decisions"},
    {"content": "[Your active task list was preserved across context compression]\n- task"},
    {"content": "[IMPORTANT: Background process 42 finished]"},
    {"content": "[System: Your previous response was truncated by the output length limit. Continue exactly where you left off. Do not restart or repeat prior text. Finish the answer directly.]"},
    {"content": "Continue from the compressed conversation context above. This marker exists because no human user turn was available."},
    {"content": "[System: Continue now. Execute the required tool calls and only send your final answer after completing the task.]"},
]


@pytest.mark.parametrize("text", [
    "[System: The previous response was cut off by me, not the runtime]",
    "[System: Your previous response was truncated in my reproduction]",
    "Human discussing [CONTEXT SUMMARY]: inside ordinary text",
])
def test_genuine_human_text_is_not_a_runtime_marker(tmp_path, text):
    b = ProjectBridge(tmp_path, "p", "s")
    b.capture([{"role": "user", "content": text, "_todo_snapshot_synthetic": False}])
    assert b.sources == {"user:latest": text}
    assert b.query == text


@pytest.mark.parametrize("synthetic", _SYNTHETIC_ROWS)
def test_synthetic_rows_are_not_human_evidence_or_queries(tmp_path, synthetic):
    b = ProjectBridge(tmp_path, "p", "s")
    human = {"role": "user", "content": [{"type": "image_url", "image_url": {"url": "image"}},
        {"type": "text", "text": "Human marker amber"}]}
    row = dict(role="user", content="runtime scaffolding")
    row.update(synthetic)
    b.capture([human, row])
    assert b.sources == {"user:latest": "Human marker amber"}
    assert b.query == "Human marker amber"
    assert "error" in b.handle("cmi_project_publish", dict(topic="fake", text="fake",
        quote=row["content"], source="user:latest"))
    b.capture([row])
    assert b.sources == {} and b.query == ""


@pytest.fixture(autouse=True)
def scratch_default_home(tmp_path, monkeypatch):
    # Standalone default_home is ~/.cmi, not HERMES_HOME. Never probe the real store.
    monkeypatch.setattr("cmi.engine.default_home", lambda: tmp_path / "hermes_home")


@pytest.mark.parametrize("via_session", [False, True])
def test_failed_rebind_disables_old_project(tmp_path, monkeypatch, via_session):
    e = CMIEngine()
    e.on_session_start("old", hermes_home=str(tmp_path), project_id="old-project")
    e._project.capture([{"role": "user", "content": "old evidence"}])
    old = e._project
    monkeypatch.setenv("CMI_PROJECT_ID", "old-project")
    blocked = tmp_path / "blocked"
    blocked.write_text("not a directory")
    with pytest.raises(OSError):
        if via_session:
            e.on_session_start("new", hermes_home=str(blocked), project_id="new-project")
        else:
            e.bind_project("new-project", home=blocked)
    assert e._project is None
    assert old.sources == {} and old.query == ""
    assert not any(s["name"].startswith("cmi_project_") for s in e.get_tool_schemas())
    assert "error" in json.loads(e.handle_tool_call("cmi_project_search", {"query": "old"}))


@pytest.mark.parametrize("via_session", [False, True])
def test_explicit_disable_overrides_schema_environment(tmp_path, monkeypatch, via_session):
    monkeypatch.setenv("CMI_PROJECT_ID", "env-project")
    e = CMIEngine()
    if via_session:
        e.on_session_start("s", hermes_home=str(tmp_path), project_id=None)
    else:
        e.bind_project(None)
    assert e._project is None
    assert not any(s["name"].startswith("cmi_project_") for s in e.get_tool_schemas())


@pytest.mark.parametrize("child_kwargs", [{}, {"project_id": "env-project"}])
def test_env_compression_child_preserves_matching_parent_home(tmp_path, monkeypatch, child_kwargs):
    monkeypatch.setenv("CMI_PROJECT_ID", "env-project")
    e = CMIEngine()
    custom = tmp_path / "custom"
    e.on_session_start("parent", hermes_home=str(custom))
    e.on_session_start("child", boundary_reason="compression", old_session_id="parent", **child_kwargs)
    assert e._project.project == "env-project"
    assert e._project.memory.path == custom / "cmi_project" / "memory.db"
    assert e._project.session == "child"


@pytest.mark.parametrize("child_kwargs,expected", [
    ({"hermes_home": "new"}, "env-project"),
    ({"project_id": "changed"}, "changed"),
    ({"project_id": None}, None),
    ({"old_session_id": "unrelated"}, "env-project"),
])
def test_child_binding_honors_host_changes(tmp_path, monkeypatch, child_kwargs, expected):
    monkeypatch.setenv("CMI_PROJECT_ID", "env-project")
    e = CMIEngine()
    e.on_session_start("parent", hermes_home=str(tmp_path / "custom"))
    kwargs = dict(boundary_reason="compression", old_session_id="parent")
    kwargs.update(child_kwargs)
    if kwargs.get("hermes_home") == "new":
        kwargs["hermes_home"] = str(tmp_path / "new")
    e.on_session_start("child", **kwargs)
    if expected is None:
        assert e._project is None
    else:
        assert e._project.project == expected
        home = tmp_path / "new" if "hermes_home" in kwargs else tmp_path / "hermes_home"
        assert e._project.memory.path == home / "cmi_project" / "memory.db"


def test_contention_detection_without_errorcode_python310():
    """Python 3.10 errors carry no sqlite_errorcode: fall back on SQLite's message."""
    from cmi.project_memory import _is_contention
    assert _is_contention(sqlite3.OperationalError("database is locked"))
    assert _is_contention(sqlite3.OperationalError("database table is locked"))
    assert not _is_contention(sqlite3.OperationalError("broken pragma"))
