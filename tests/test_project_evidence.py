import os
import sqlite3

import pytest

from cmi.project_memory import ProjectMemory


def test_evidence_is_exact_durable_and_project_scoped(tmp_path):
    path = tmp_path / "private" / "memory.db"
    a = ProjectMemory(path)
    original = "User decision:\nCMI pilot marker is amber-owl-42.\n"
    receipt = a.publish("p", "pilot.marker", "amber-owl-42", source="user:a/1",
                        session="a", model="gpt", kind="decision", evidence=original)
    b = ProjectMemory(path)
    recovered = b.get("p", receipt["id"], include_evidence=True)
    assert recovered["evidence"] == original
    assert recovered["evidence_sha256"]
    assert recovered["status"] == "reported"
    assert b.get("q", receipt["id"], include_evidence=True) is None
    a.publish("p", "pilot.marker", "violet-fox-71", source="user:a/2",
              expected_revision=1, evidence="Correction: violet-fox-71")
    assert b.get("p", receipt["id"], include_evidence=True)["evidence"] == original
    assert b.get("p", receipt["id"])["current"] is False
    assert b.search("p", "pilot.marker")[0]["text"] == "violet-fox-71"


def test_evidence_with_credentials_is_not_persisted(tmp_path):
    path = tmp_path / "memory.db"
    m = ProjectMemory(path)
    with pytest.raises(ValueError):
        m.publish("p", "marker", "harmless", source="tool:a",
                  evidence="PASSWORD=" + "abc123def456ghi789")
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT count(*) FROM events").fetchone()[0] == 0
        assert db.execute("SELECT count(*) FROM evidence").fetchone()[0] == 0


def test_database_and_live_wal_are_private(tmp_path):
    old = os.umask(0)
    try:
        path = tmp_path / "private" / "memory.db"
        memory = ProjectMemory(path)
        assert __import__("os").name == "nt" or path.stat().st_mode & 0o777 == 0o600
        assert __import__("os").name == "nt" or path.parent.stat().st_mode & 0o777 == 0o700
        db = memory._connect()
        try:
            db.execute("BEGIN IMMEDIATE")
            db.execute("INSERT INTO evidence VALUES (?, ?)", ("test", "data"))
            for suffix in ("-wal", "-shm"):
                assert __import__("os").name == "nt" or path.with_name(path.name + suffix).stat().st_mode & 0o777 == 0o600
        finally:
            db.rollback()
            db.close()
    finally:
        os.umask(old)
