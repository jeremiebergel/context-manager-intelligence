"""Conservative read-only host identity resolution; never read global active project.

Session rows and dispatcher run/claim pins are trusted host inputs, not model tool
arguments. Their absence is not permission to guess from text or process cwd.
"""
from __future__ import annotations

from contextlib import closing
from pathlib import Path
import os
import sqlite3
import time


MAX_PARENT_DEPTH = 64


def auto_binding_enabled():
    """On by default inside Hermes (a session still binds only when its workspace folder belongs to
    exactly one Desktop project). `context.cmi.auto_project_binding: false` turns it off; any
    non-boolean value fails closed. Outside Hermes (the proxy) there is no project memory."""
    try:
        from hermes_cli.config import load_config_readonly
        cmi = load_config_readonly().get("context", {}).get("cmi", {})
        if "auto_project_binding" not in cmi:
            return True
        return cmi["auto_project_binding"] is True
    except (ImportError, AttributeError, TypeError, OSError, ValueError):
        return False


def readonly(path):
    uri = Path(path).absolute().as_uri()
    try:
        con = sqlite3.connect(uri + "?mode=ro", uri=True, timeout=0.1)
        con.execute("SELECT name FROM sqlite_master LIMIT 1")
        return con
    except sqlite3.OperationalError:
        # A WAL-flagged database with no -wal/-shm files (Hermes' projects.db after a clean
        # close) cannot be opened mode=ro: SQLite wants to create the -shm. With no WAL file
        # there is nothing unread, so an immutable read is exact.
        p = Path(path).absolute()
        if p.exists() and not Path(str(p) + "-wal").exists():
            return sqlite3.connect(uri + "?mode=ro&immutable=1", uri=True, timeout=0.1)
        raise


# Hermes Desktop chats were stored with source "desktop" until 0.22.x; from 2026-11-03 they carry
# "local" or "gui". With only "desktop" accepted, auto-binding silently matched nothing.
# The cwd-inside-a-project-folder and unambiguous-owner rules below still decide the binding.
DESKTOP_SOURCES = ("desktop", "local", "gui")


def lineage(home, session_id):
    """Snapshot exact host ancestry, including mutable source/workspace edges."""
    seen = set()
    rows = []
    with closing(readonly(Path(home) / "state.db")) as state:
        for _ in range(MAX_PARENT_DEPTH):
            if not session_id or session_id in seen:
                return None
            seen.add(session_id)
            row = state.execute("SELECT id,source,cwd,parent_session_id FROM sessions WHERE id=?",
                                (session_id,)).fetchone()
            if row is None:
                return None
            row = list(row)
            if row[1] in DESKTOP_SOURCES:  # Hermes flips gui -> local after the first message
                row[1] = "desktop"
            rows.append(row)
            if not row[3]:
                return rows
            session_id = row[3]
    return None


def _root_session(home, session_id):
    """A parent edge outranks child cwd; broken/cyclic/deep lineage fails closed."""
    rows = lineage(home, session_id)
    return tuple(rows[-1][1:]) if rows else None


def _worker_project():
    """Use the dispatcher's exact DB pin, never current/default board or board.json.

    The task's project_id is the creation-time scope. Board metadata only supplies
    defaults at task creation and may subsequently change. Run and claim pins fence
    stale workers. Delegated children reach here only via a stored kanban ancestor.
    """
    task = os.environ.get("HERMES_KANBAN_TASK")
    pin = os.environ.get("HERMES_KANBAN_DB")
    run = os.environ.get("HERMES_KANBAN_RUN_ID")
    claim = os.environ.get("HERMES_KANBAN_CLAIM_LOCK")
    if not task or not pin or not Path(pin).is_absolute() or not run or not claim:
        return None
    if not run.isascii() or not run.isdecimal() or str(int(run)) != run or int(run) <= 0:
        return None
    with closing(readonly(pin)) as tasks:
        row = tasks.execute("SELECT t.project_id,t.status,t.current_run_id,t.claim_lock,"
                            "r.status,r.claim_lock,t.claim_expires FROM tasks t JOIN task_runs r "
                            "ON r.id=t.current_run_id AND r.task_id=t.id WHERE t.id=?",
                            (task,)).fetchone()
    if (row is None or row[1] != "running" or row[2] != int(run) or row[3] != claim
            or row[4] != "running" or row[5] != claim):
        return None
    if row[6] is not None and (type(row[6]) is not int or row[6] <= time.time()):
        return None
    return row[0]


def resolve_project(home, session_id):
    """Return a registered nonarchived ID or None. No host writes or migrations."""
    if not session_id:
        return None
    try:
        row = _root_session(home, session_id)
        if row is None:
            return None
        with closing(readonly(Path(home) / "projects.db")) as projects:
            if row[0] == "kanban":
                pid = _worker_project()
                if pid and projects.execute("SELECT id FROM projects WHERE id=? AND archived=0", (pid,)).fetchone():
                    return pid
                return None
            if row[0] not in DESKTOP_SOURCES or not row[1] or not Path(row[1]).is_absolute():
                return None
            target = Path(row[1]).resolve()
            folders = projects.execute("SELECT pf.project_id,pf.path FROM project_folders pf "
                "JOIN projects p ON p.id=pf.project_id WHERE p.archived=0 "
                "UNION SELECT id,primary_path FROM projects WHERE archived=0").fetchall()
        owners = []
        for pid, path in folders:
            if not path or not Path(path).is_absolute():
                continue
            folder = Path(path).resolve()
            if target == folder or folder in target.parents:
                owners.append((len(folder.parts),pid))
        if not owners:
            return None
        longest = max(depth for depth,_ in owners)
        ids = {pid for depth,pid in owners if depth == longest}
        return next(iter(ids)) if len(ids) == 1 else None
    except (sqlite3.Error, OSError, ValueError, RuntimeError, TypeError):
        return None
