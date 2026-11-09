"""Project memory contracts: independent clients, no transcript transfer."""
import pytest


@pytest.mark.parametrize("field", ["project", "topic", "text", "source", "session", "model"])
@pytest.mark.parametrize("prefix", ["", "ordinary line\n"])
def test_raw_env_secrets_never_enter_events_or_fts(tmp_path, field, prefix):
    import sqlite3
    from cmi.project_memory import ProjectMemory
    path = tmp_path / "shared.db"
    memory = ProjectMemory(path)
    args = dict(project="p", topic="billing", text="Stripe", source="user:1")
    args[field] = prefix + "PASSWORD=" + "abc123def456ghi789"
    with pytest.raises(ValueError):
        memory.publish(**args)
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT count(*) FROM events").fetchone()[0] == 0
        assert db.execute("SELECT count(*) FROM memory_fts").fetchone()[0] == 0


def test_small_relevant_claim_survives_twenty_oversize_hits(tmp_path):
    from cmi.project_memory import ProjectMemory
    memory = ProjectMemory(tmp_path / "shared.db")
    for n in range(20):
        memory.publish("p", str(n), "Stripe " * 2000, source="tool:large")
    memory.publish("p", "billing", "Stripe Payment Links", source="user:1")
    assert "Payment Links" in memory.context("p", "Stripe", max_bytes=600)


@pytest.mark.parametrize("limit", [-1, 0, 1001, True, "5"])
def test_search_rejects_unbounded_or_invalid_limits(tmp_path, limit):
    from cmi.project_memory import ProjectMemory
    memory = ProjectMemory(tmp_path / "shared.db")
    with pytest.raises(ValueError):
        memory.search("p", "Stripe", limit=limit)



def test_new_client_recovers_only_the_requested_project_fact(tmp_path):
    from cmi.project_memory import ProjectMemory
    db = tmp_path / "shared.db"
    writer = ProjectMemory(db)
    receipt = writer.publish("acme-billing", "billing.mode", "Payment uses Stripe Payment Links.",
                             source="cmi:original-output", session="claude-session", model="claude",
                             kind="decision")
    reader = ProjectMemory(db)
    hits = reader.search("acme-billing", "Stripe")
    assert len(hits) == 1
    assert hits[0]["text"] == "Payment uses Stripe Payment Links."
    assert hits[0]["id"] == receipt["id"]
    assert hits[0]["session"] == "claude-session"
    assert hits[0]["source"] == "cmi:original-output"
    assert hits[0]["status"] == "reported"


def test_update_invalidates_old_search_results_without_losing_history(tmp_path):
    from cmi.project_memory import ProjectMemory
    a = ProjectMemory(tmp_path / "shared.db")
    b = ProjectMemory(tmp_path / "shared.db")
    first = a.publish("p", "billing.mode", "Stripe Payment Links", source="user:1")
    second = a.publish("p", "billing.mode", "Direct invoices only", source="user:2",
                       expected_revision=first["revision"])
    assert b.search("p", "Stripe") == []
    assert b.search("p", "invoices")[0]["id"] == second["id"]
    assert [e["text"] for e in b.history("p", "billing.mode")] == [
        "Stripe Payment Links", "Direct invoices only"]


def test_project_scope_cannot_be_starved_by_other_projects(tmp_path):
    from cmi.project_memory import ProjectMemory
    memory = ProjectMemory(tmp_path / "shared.db")
    for n in range(15):
        memory.publish("other", str(n), "Stripe Stripe Stripe", source="user:other")
    memory.publish("mine", "billing", "Stripe Payment Links", source="user:mine")
    assert [r["project"] for r in memory.search("mine", "Stripe", limit=1)] == ["mine"]
    with pytest.raises(ValueError, match="project"):
        memory.search("", "Stripe")


def test_working_set_has_a_hard_size_budget_and_no_unrelated_transcript(tmp_path):
    from cmi.project_memory import ProjectMemory
    memory = ProjectMemory(tmp_path / "shared.db")
    memory.publish("p", "billing", "Stripe Payment Links", source="user:1")
    memory.publish("p", "ui", "Sidebar animation", source="user:2")
    memory.publish("p", "large", "Stripe " * 2000, source="tool:3")
    context = memory.context("p", "Stripe", max_bytes=600)
    assert "Stripe Payment Links" in context
    assert "Sidebar" not in context
    assert len(context.encode("utf-8")) <= 600
    assert context == memory.context("p", "Stripe", max_bytes=600)
    assert memory.context("p", "Stripe", max_bytes=0) == ""


@pytest.mark.parametrize("change", [
    {"project": ""}, {"source": ""}, {"topic": ""}, {"text": ""},
    {"kind": "verified"}, {"expected_revision": -1},
    {"text": "sk-" + "a1" * 20}, {"source": "sk-" + "a1" * 20},
    {"project": "sk-" + "a1" * 20},
])
def test_rejects_invalid_or_secret_claims_before_persistence(tmp_path, change):
    from cmi.project_memory import ProjectMemory
    memory = ProjectMemory(tmp_path / "shared.db")
    args = dict(project="p", topic="billing", text="Stripe", source="user:1")
    args.update(change)
    with pytest.raises(ValueError):
        memory.publish(**args)
    assert memory.search("p", "Stripe") == []


def test_database_symlink_is_refused(tmp_path):
    from cmi.project_memory import ProjectMemory
    target = tmp_path / "target.db"
    link = tmp_path / "link.db"
    link.symlink_to(target)
    with pytest.raises(ValueError):
        ProjectMemory(link)
    assert not target.exists()


def test_two_real_processes_cannot_silently_overwrite_each_other(tmp_path):
    import subprocess
    import sys
    from concurrent.futures import ThreadPoolExecutor
    from pathlib import Path
    from cmi.project_memory import ProjectMemory
    path = tmp_path / "shared.db"
    memory = ProjectMemory(path)
    memory.publish("p", "billing", "Original value", source="user:seed")
    script = '''
import sys
from pathlib import Path
from cmi.project_memory import ProjectMemory
try:
    e = ProjectMemory(Path(sys.argv[1])).publish("p", "billing", sys.argv[2],
         source="tool:" + sys.argv[2], session=sys.argv[2], expected_revision=1)
    print("published")
except ValueError as e:
    if "Revision conflict" not in str(e): raise
    print("conflict")
'''
    def run(name):
        return subprocess.run([sys.executable, "-c", script, str(path), name],
                              cwd=Path(__file__).parents[1], capture_output=True,
                              text=True, check=True).stdout.strip()
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(run, ["codex-session", "claude-session"]))
    assert sorted(results) == ["conflict", "published"]
    assert len(memory.history("p", "billing")) == 2


def test_session_adapter_uses_shared_memory_without_mutating_history(tmp_path):
    from cmi.project_session import ProjectSession
    import copy
    path = tmp_path / "shared.db"
    claude = ProjectSession(path, project="acme-billing", session="a", model="claude")
    codex = ProjectSession(path, project="acme-billing", session="b", model="gpt")
    claude.publish("billing", "Stripe Payment Links", source="user:a/1", kind="decision")
    messages = [{"role": "system", "content": "Stable prefix"},
                {"role": "user", "content": "Stripe"}]
    before = copy.deepcopy(messages)
    selected = codex.prepare(messages, max_bytes=600)
    assert messages == before
    assert selected[0] == messages[0]
    assert "Stripe Payment Links" in selected[1]["content"]
    assert selected == codex.prepare(messages, max_bytes=600)
    claude.publish("billing", "Stripe invoices", source="user:a/2", expected_revision=1)
    selected = codex.prepare(messages, max_bytes=600)
    assert "Stripe invoices" in selected[1]["content"]
    assert "Payment Links" not in selected[1]["content"]
    unrelated = ProjectSession(path, project="other-app", session="c", model="gpt")
    assert unrelated.prepare(messages) == messages
