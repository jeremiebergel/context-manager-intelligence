"""Forced legitimate bootstrap interleavings; no timing sleeps or repair."""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event, current_thread
import sqlite3

import pytest

from cmi import project_policy as policy
from tests.test_project_autobind import host, session


def test_co_creator_cannot_overtake_unlocked_initial_registry(tmp_path, monkeypatch):
    host(tmp_path)
    for sid in ("creator", "contender"):
        session(tmp_path, sid, cwd=tmp_path / "repo")
    creator_opened_lock, release_creator, contender_at_bootstrap = Event(), Event(), Event()
    original_open = policy.os.open
    creator_thread = None

    def controlled_open(path, flags, *args, **kwargs):
        fd = original_open(path, flags, *args, **kwargs)
        if current_thread() is creator_thread and Path(path).name == "registry.lock":
            creator_opened_lock.set()
            assert release_creator.wait(5), "creator was never released"
        elif current_thread() is not creator_thread and (Path(path) == tmp_path or Path(path).name == ".cmi_policy_boot.lock"):
            # Atomic bootstrap implementations serialize before publishing the
            # registry directory. Signal the attempt, not successful acquisition.
            contender_at_bootstrap.set()
        return fd

    monkeypatch.setattr(policy.os, "open", controlled_open)

    def create():
        nonlocal creator_thread
        creator_thread = current_thread()
        return policy.persist(tmp_path, "creator", "off")

    def contend():
        try:
            return policy.persist(tmp_path, "contender", "off")
        finally:
            contender_at_bootstrap.set()

    with ThreadPoolExecutor(max_workers=2) as pool:
        creator = pool.submit(create)
        try:
            assert creator_opened_lock.wait(5)
            contender = pool.submit(contend)
            assert contender_at_bootstrap.wait(5)
        finally:
            release_creator.set()
        assert creator.result(timeout=5)
        assert contender.result(timeout=5)
    with sqlite3.connect(tmp_path / "cmi_policy" / "sessions.db") as db:
        assert db.execute("SELECT session FROM policies ORDER BY session").fetchall() == [
            ("contender",), ("creator",)]


@pytest.mark.parametrize("missing", ["sessions.db", "registry.lock"])
def test_partial_existing_registry_is_not_co_creation(tmp_path, missing):
    host(tmp_path)
    session(tmp_path, "owner", cwd=tmp_path / "repo")
    assert policy.persist(tmp_path, "owner", "off")
    directory = tmp_path / "cmi_policy"
    (directory / missing).unlink()
    before = {p.name: p.read_bytes() for p in directory.iterdir()}
    assert policy.persist(tmp_path, "owner", "explicit", "p") is False
    assert {p.name: p.read_bytes() for p in directory.iterdir()} == before
