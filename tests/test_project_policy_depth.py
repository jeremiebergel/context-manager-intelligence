"""Real SQLite continuation graphs: bounded work, fresh authority per request."""
import pytest

import cmi.project_policy as policy
from tests.test_project_autobind import clean_identity_env, session, start
from tests.test_project_policy import setup_family


def chain(home, depth):
    setup_family(home)
    parent, _ = start(home, "parent")
    previous = "parent"
    for index in range(1, depth + 1):
        sid = f"c{index}"
        session(home, sid, parent=previous, cwd=home / "repo")
        assert policy.persist(home, sid, "auto", "p", continuation_owner=previous)
        previous = sid
    return parent, previous


@pytest.mark.parametrize("depth", [3, 5, 9, 14, 20, 30])
def test_valid_chain_validation_has_bounded_graph_work(tmp_path, monkeypatch, depth):
    _, leaf = chain(tmp_path, depth)
    original = policy.lineage
    calls = []

    def count(home, sid):
        calls.append(sid)
        # Stop the old exponential implementation without a multi-hour test.
        assert len(calls) <= 4 * (depth + 1) ** 2, "exponential continuation validation"
        return original(home, sid)

    monkeypatch.setattr(policy, "lineage", count)
    evaluations = []
    evaluate = policy._evaluate_project

    def counted_evaluation(*args, **kwargs):
        evaluations.append(args[1])
        assert len(evaluations) <= depth + 1, "dependency proof recomputed"
        return evaluate(*args, **kwargs)

    monkeypatch.setattr(policy, "_evaluate_project", counted_evaluation)
    assert policy.effective_project(tmp_path, leaf) == "p"
    assert len(calls) <= depth + 1
    assert len(evaluations) <= depth + 1


@pytest.mark.parametrize("revoked", ["parent", "c10", "c20"])
def test_deep_graph_rechecks_revocations_on_next_request(tmp_path, revoked):
    _, leaf = chain(tmp_path, 20)
    assert policy.effective_project(tmp_path, leaf) == "p"
    assert policy.persist(tmp_path, revoked, "off")
    assert policy.effective_project(tmp_path, leaf) is None
    assert policy.persist(tmp_path, leaf, "explicit", "override")
    assert policy.effective_project(tmp_path, leaf, details=True) == ("override", "explicit", True)


def test_completed_proof_cannot_bypass_cycle_or_remaining_depth(tmp_path):
    _, leaf = chain(tmp_path, 3)
    with policy.registry(tmp_path) as db:
        traversal = policy._PolicyTraversal(tmp_path, db)
        assert traversal.evaluate(leaf) == "p"
        assert traversal.evaluate(leaf, seen=("parent",)) is None
        assert traversal.evaluate(leaf, seen=tuple(f"outside{i}" for i in range(62))) is None
        assert traversal.evaluate(leaf, seen=tuple(f"outside{i}" for i in range(61))) == "p"
        assert traversal.evaluate(leaf, details=True) == ("p", "auto", True)


@pytest.mark.parametrize("depth, expected", [(64, "p"), (65, None)])
def test_dependency_depth_budget_is_independent_of_host_ancestry(tmp_path, depth, expected):
    setup_family(tmp_path)
    start(tmp_path, "parent")
    previous = "parent"
    for index in range(depth):
        sid = f"independent{index}"
        session(tmp_path, sid, cwd=tmp_path / "repo")
        assert policy.persist(tmp_path, sid, "auto", "p", continuation_owner=previous)
        previous = sid
    assert policy.effective_project(tmp_path, previous) == expected


def test_continuation_dependency_cycle_fails_closed(tmp_path):
    _, leaf = chain(tmp_path, 3)
    assert policy.persist(tmp_path, "parent", "auto", "p", continuation_owner=leaf)
    assert policy.effective_project(tmp_path, leaf) is None


def test_graph_policy_reads_are_transaction_local_and_linear(tmp_path):
    _, leaf = chain(tmp_path, 30)
    with policy.registry(tmp_path) as db:
        queries = []
        db.set_trace_callback(queries.append)
        assert policy._effective_project(tmp_path, leaf, db) == "p"
        reads = [q for q in queries if q.startswith("SELECT mode,project,evidence")]
        assert len(reads) == 31


def test_bootstrap_and_continuation_flags_do_not_share_proofs(tmp_path):
    setup_family(tmp_path)
    with policy.registry(tmp_path, create=True) as db:
        traversal = policy._PolicyTraversal(tmp_path, db)
        assert traversal.evaluate("parent", bootstrap=True) == "p"
        assert traversal.evaluate("parent") is None
    start(tmp_path, "parent")
    assert policy.persist(tmp_path, "child", "off")
    with policy.registry(tmp_path) as db:
        traversal = policy._PolicyTraversal(tmp_path, db)
        assert traversal.evaluate("parent") == "p"
        assert traversal.evaluate("parent", continuations=("child",)) is None


@pytest.mark.parametrize("revoked", ["parent", "c10", "c20"])
def test_deep_engine_revocation_clears_existing_sources(tmp_path, revoked):
    chain(tmp_path, 20)
    engine, _ = start(tmp_path, "c20")
    bridge = engine._project
    engine.select_context([{"role": "user", "content": "private deep evidence"}])
    assert bridge.sources
    assert policy.persist(tmp_path, revoked, "off")
    engine.select_context([{"role": "user", "content": "private deep evidence"}])
    assert engine._project is None
    assert bridge.sources == {}


@pytest.mark.parametrize("changed", ["parent", "c10", "c20"])
def test_deep_graph_rechecks_host_metadata_on_next_request(tmp_path, changed):
    import sqlite3

    _, leaf = chain(tmp_path, 20)
    assert policy.effective_project(tmp_path, leaf) == "p"
    with sqlite3.connect(tmp_path / "state.db") as db:
        db.execute("UPDATE sessions SET cwd=? WHERE id=?",
                   (str(tmp_path / "changed"), changed))
    assert policy.effective_project(tmp_path, leaf) is None
