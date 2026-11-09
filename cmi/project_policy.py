"""Same-user, profile-local policy, not authentication or a plugin sandbox.

Only host session rows name policy targets. A private directory and serialized
SQLite transactions prevent partial updates; invalid existing stores are never
repaired implicitly. Missing parent policy is not authority to infer scope.
"""
from contextlib import closing, contextmanager
import json
import os
from pathlib import Path
import sqlite3
import stat

from . import _fs
from .project_binding import lineage, resolve_project, _worker_project, readonly


def worker_evidence(rows):
    if rows[-1][1] != "kanban":
        return None
    project = _worker_project()
    if not project:
        raise PolicyUnavailable("Missing worker authority")
    from hashlib import sha256
    pins = [os.environ.get(name) for name in (
        "HERMES_KANBAN_DB", "HERMES_KANBAN_TASK", "HERMES_KANBAN_RUN_ID")]
    return [project] + pins + [sha256(os.environ["HERMES_KANBAN_CLAIM_LOCK"].encode()).hexdigest()]


def registered(home, project):
    with closing(readonly(Path(home) / "projects.db")) as db:
        return db.execute("SELECT 1 FROM projects WHERE id=? AND archived=0", (project,)).fetchone() is not None



class PolicyUnavailable(ValueError):
    pass


def _private(path, directory=False):
    info = path.lstat()
    kind = stat.S_ISDIR if directory else stat.S_ISREG
    if (not kind(info.st_mode) or not _fs.owned_by_me(info)
            or (not _fs.IS_WINDOWS and stat.S_IMODE(info.st_mode) != (0o700 if directory else 0o600))
            or (not directory and info.st_nlink != 1)):
        raise PolicyUnavailable("Unsafe policy permissions or file type")


@contextmanager
def registry(home, create=False):
    # The existing profile directory is the stable bootstrap lock inode. Lock
    # before exposing cmi_policy or its lock file, so legitimate co-creators
    # cannot mistake a half-initialized registry for pre-existing damage.
    # No extra lock file is created that could itself be missing or partial.
    if not _fs.has_locking():
        raise PolicyUnavailable("Project memory needs file locking, which this platform does not provide")
    if _fs.IS_WINDOWS:  # a directory cannot be opened for locking there: use a small bootstrap lock file
        fd = os.open(Path(home) / ".cmi_policy_boot.lock", os.O_RDWR | os.O_CREAT | _fs.O_BINARY, 0o600)
    else:
        fd = os.open(Path(home), os.O_RDONLY | os.O_DIRECTORY | _fs.O_NOFOLLOW)
    try:
        if not _fs.lock_with_deadline(fd, 1.0):
            raise PolicyUnavailable("Profile policy lock unavailable")
        with _registry(home, create=create) as db:
            yield db
    finally:
        _fs.unlock(fd)
        os.close(fd)


@contextmanager
def _registry(home, create=False):
    directory = Path(home) / "cmi_policy"
    if create:
        directory.mkdir(mode=0o700, exist_ok=True)
    _private(directory, directory=True)
    flags = os.O_RDWR | _fs.O_NOFOLLOW | _fs.O_BINARY
    lockpath = directory / "registry.lock"
    path = directory / "sessions.db"
    if os.path.lexists(path) and not os.path.lexists(lockpath):
        raise PolicyUnavailable("Missing registry lock")
    new_lock = False
    try:
        lock = os.open(lockpath, flags | (os.O_CREAT | os.O_EXCL if create else 0), 0o600)
        new_lock = create
    except FileExistsError:
        lock = os.open(lockpath, flags)
    try:
        _private(lockpath)
        if not _fs.lock_with_deadline(lock, 1.0):
            raise PolicyUnavailable("Policy store locked")
        if not os.path.lexists(path) and not new_lock:
            raise PolicyUnavailable("Missing registry database")
        created = False
        try:
            fd = os.open(path, flags | (os.O_CREAT | os.O_EXCL if create else 0), 0o600)
            created = create
        except FileExistsError:
            fd = os.open(path, flags)
        os.close(fd)
        _private(path)
        with closing(sqlite3.connect(path.absolute().as_uri() + "?mode=rw", uri=True, timeout=0.1)) as db:
            db.execute("BEGIN IMMEDIATE")
            version = db.execute("PRAGMA user_version").fetchone()[0]
            if version == 0 and created:
                db.execute("CREATE TABLE policies (session TEXT PRIMARY KEY, mode TEXT NOT NULL, project TEXT, evidence TEXT NOT NULL)")
                db.execute("PRAGMA user_version=1")
            elif version != 1:
                raise PolicyUnavailable("Unsupported policy registry")
            try:
                yield db
                db.commit()
            except BaseException:
                db.rollback()
                raise
    finally:
        os.close(lock)


def _norm(rows):
    """Stored evidence may predate source normalisation (gui/local -> desktop)."""
    from .project_binding import DESKTOP_SOURCES
    return [[r[0], "desktop" if r[1] in DESKTOP_SOURCES else r[1], *r[2:]] for r in (rows or []) if isinstance(r, (list, tuple))]


def persist(home, sid, mode, project=None, *, only_if_absent=False, continuation_owner=None):
    """Return False on lazy host row absence or any unsafe storage/evidence."""
    try:
        rows = lineage(home, sid)
        if not rows:
            return False
        if mode not in ("auto", "explicit", "off") or (mode == "explicit" and (not isinstance(project, str) or not project.strip())):
            return False
        evidence = {"rows": rows, "worker": worker_evidence(rows) if mode != "off" else None}
        if continuation_owner is not None:
            if not isinstance(continuation_owner, str) or not continuation_owner or continuation_owner == sid:
                return False
            owner_rows = lineage(home, continuation_owner)
            if not owner_rows:
                return False
            # This link comes only from the host compression boundary, never
            # model input. Pin its identity as well as its current policy.
            evidence["continuation"] = {"session": continuation_owner, "rows": owner_rows,
                                        "worker": worker_evidence(owner_rows) if mode != "off" else None}
        with registry(home, create=True) as db:
            db.execute("INSERT OR IGNORE INTO policies VALUES (?,?,?,?)" if only_if_absent
                       else "INSERT OR REPLACE INTO policies VALUES (?,?,?,?)",
                       (sid, mode, project, json.dumps(evidence)))
        return True
    except (sqlite3.Error, OSError, ValueError, TypeError):
        return False


def invalidate_auto(home, sessions):
    """Latch failed automatic authority, never replace explicit user intent.

    Conditional updates under the registry transaction are the CAS equivalent:
    reconstruction can insert absent rows; invalidation can only revoke auto.
    Neither operation can replace a committed explicit binding or opt-out.
    """
    try:
        with registry(home) as db:
            for sid in set(sessions):
                db.execute("UPDATE policies SET mode='off', project=NULL WHERE session=? AND mode='auto'", (sid,))
    except (sqlite3.Error, OSError, ValueError, TypeError):
        pass


def effective_project(home, sid, *, bootstrap=False, details=False, continuations=()):
    """Nearest explicit policy wins, subject to durable continuation owners."""
    try:
        with registry(home) as db:
            return _effective_project(home, sid, db, bootstrap=bootstrap,
                                      details=details, continuations=continuations)
    except FileNotFoundError:
        if os.path.lexists(Path(home) / "cmi_policy"):
            return None
        try:
            rows = lineage(home, sid)
            project = resolve_project(home, sid) if bootstrap and rows and len(rows) == 1 else None
            return (project, "auto", False) if project and details else project
        except (sqlite3.Error, OSError, ValueError, TypeError):
            return None
    except (sqlite3.Error, OSError, ValueError, TypeError):
        return None


class _PolicyTraversal:
    """One registry transaction only; completed proofs carry their path bounds.

    A sid-only cache would hide cycles or reuse a shallow proof beyond the
    remaining depth budget. Each successful proof records every required
    dependency and its longest dependency path, checked again on every reuse.
    Failures are never cached. Host rows and policy reads are local to this
    traversal, not retained across requests or registry transactions.
    """
    def __init__(self, home, db):
        self.home, self.db = home, db
        self.completed = {}
        self.rows = {}
        self.policies = {}
        self.frames = []
        self.work = 0

    def lineage(self, sid):
        if sid not in self.rows:
            self.rows[sid] = lineage(self.home, sid)
        return self.rows[sid]

    def policy(self, sid):
        if sid not in self.policies:
            self.policies[sid] = self.db.execute(
                "SELECT mode,project,evidence FROM policies WHERE session=?", (sid,)).fetchone()
        return self.policies[sid]

    def evaluate(self, sid, *, bootstrap=False, details=False, continuations=(), seen=()):
        key = (sid, bootstrap, details, frozenset(continuations))
        proof = self.completed.get(key)
        if proof is None:
            self.work += 1
            # Bound broad graphs as well as the existing 64-edge path budget.
            if self.work > 4096:
                raise PolicyUnavailable("Policy dependency graph too large")
            frame = [set(), 0]
            self.frames.append(frame)
            try:
                value = _evaluate_project(self.home, sid, self.db, bootstrap=bootstrap,
                                          details=details, continuations=continuations,
                                          seen=seen, traversal=self)
            finally:
                self.frames.pop()
            if value is None:
                return None
            proof = (value, frozenset(frame[0]), frame[1])
            self.completed[key] = proof
        value, dependencies, height = proof
        if dependencies.intersection(seen) or len(seen) + height > 64:
            return None
        if self.frames:
            frame = self.frames[-1]
            frame[0].update(dependencies)
            frame[0].add(sid)
            frame[1] = max(frame[1], height + 1)
        return value


def _effective_project(home, sid, db, *, bootstrap=False, details=False,
                       continuations=(), seen=()):
    return _PolicyTraversal(home, db).evaluate(sid, bootstrap=bootstrap, details=details,
                                              continuations=continuations, seen=seen)


def _evaluate_project(home, sid, db, *, bootstrap=False, details=False,
                      continuations=(), seen=(), traversal):
    # Follow dependency edges within the same serialized policy snapshot.
    rows = traversal.lineage(sid)
    if not rows:
        return None
    policies = []
    continuation_policies = []
    def authorized(policy, project):
        evidence = json.loads(policy[2])
        if "continuation" not in evidence:
            return True  # Existing schema-v1 records need no migration.
        dependency = evidence["continuation"]
        if not isinstance(dependency, dict):
            return False
        owner = dependency.get("session")
        if not isinstance(owner, str) or not owner or owner in seen or len(seen) >= 64:
            return False
        # A host may insert intermediate sessions the engine never saw. Their
        # optouts still constrain a continuation; own fresh explicit intent has
        # no continuation link and intentionally does not enter this check.
        for row in evidence["rows"][1:]:
            if row[0] == owner:
                break
            intermediate = traversal.policy(row[0])
            if intermediate and (row[0] in seen or traversal.evaluate(
                    row[0], seen=(*seen, row[0])) != project):
                return False
        owner_rows = traversal.lineage(owner)
        if (not owner_rows or _norm(dependency.get("rows")) != _norm(owner_rows)
                or dependency.get("worker") != worker_evidence(owner_rows)):
            return False
        return traversal.evaluate(owner, seen=(*seen, owner)) == project

    def result(project, selected=()):
        if any(p[0] == "off" or p[1] != project or not authorized(p, project)
               for p in (*continuation_policies, *selected)):
            return None
        if not project or not details:
            return project
        own = next((p for i, p in policies if i == 0), None)
        return project, own[0] if own else "auto", own is not None
    continuation_rows = {}
    for current in set(continuations) - {sid}:
        current_rows = traversal.lineage(current)
        if not current_rows:
            continuation_rows[current] = None
            continue
        for index, row in enumerate(current_rows):
            if row[0] == sid:
                break
            continuation_rows[row[0]] = current_rows[index:]
    for current, current_rows in continuation_rows.items():
        policy = traversal.policy(current)
        if policy:
            evidence = json.loads(policy[2])
            if (not current_rows or policy[0] == "off" or _norm(evidence.get("rows")) != _norm(current_rows)
                    or evidence.get("worker") != worker_evidence(current_rows)):
                return None
            continuation_policies.append(policy)
    for index, row in enumerate(rows):
        policy = traversal.policy(row[0])
        if policy:
            evidence = json.loads(policy[2])
            if (not isinstance(evidence, dict) or _norm(evidence.get("rows")) != _norm(rows[index:])
                    or policy[0] not in ("auto", "explicit", "off")):
                return None
            if policy[0] != "off" and evidence.get("worker") != worker_evidence(rows):
                return None
            policies.append((index, policy))
    if not policies:
        return result(resolve_project(home, sid)) if bootstrap and len(rows) == 1 else None
    worker = _worker_project() if rows[-1][1] == "kanban" else None
    if rows[-1][1] == "kanban" and not worker:
        return None
    for index, (mode, project, _) in policies:
        if mode == "off":
            return None
        if mode == "explicit":
            if not isinstance(project, str) or not project.strip():
                return None
            if index > 0 and any(i < index and p[0] == "auto" and p[1] != project for i, p in policies):
                return None
            if worker and worker != project:
                return None
            return result(project, [p for i, p in policies if i <= index]) if (index == 0 and rows[-1][1] != "kanban") or registered(home, project) else None
    if len(rows) > 1 and not any(index > 0 for index, _ in policies):
        return None
    resolved = resolve_project(home, sid)
    return result(resolved, [p for _, p in policies]) if resolved and all(policy[1] == resolved for _, policy in policies) else None
