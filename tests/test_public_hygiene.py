"""Publication gate: tracked files must not carry machine-specific home paths, real e-mail addresses,
or the maintainer's private project/client names.

Generic patterns only in this file — a list of private names would itself leak them (and did: an
earlier version listed them split in parts). The private list lives OUTSIDE the repository, one
case-insensitive regex per line, in ``$CMI_PRIVATE_TERMS`` (default ``~/.config/cmi/private-terms.txt``).
Without it the private-name check is skipped, except when ``CMI_REQUIRE_PRIVATE_TERMS=1`` (the
export script sets it). Placeholders such as ``/Users/USER``, ``/home/dev`` and ``@example.com`` are allowed.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
HOME_PATH = re.compile(r"/(?:Users|home)/(?!(?:USER|user|dev|runner|me|you|j|alice|bob)\b)[A-Za-z][\w.-]{2,}/")
EMAIL = re.compile(r"\b[\w.+-]+@(?!(?:example\.(?:com|org|net)|users\.noreply\.github\.com|domain)\b)[\w-]+\.[\w.-]+\b")
# Hermes/CMI session and project id shapes from real runs (synthetic fixtures use other shapes).
RUN_IDS = re.compile(r"\blocal-[0-9a-f]{32,}\b|\b20\d{6}_\d{6}_[0-9a-f]{6}\b")
SKIP_SUFFIX = {".png", ".jpg", ".jpeg", ".gif", ".lock"}


def _tracked() -> list[Path]:
    if not shutil.which("git") or not (ROOT / ".git").exists():
        pytest.skip("not a git checkout")
    out = subprocess.run(["git", "ls-files"], cwd=ROOT, capture_output=True, text=True, check=True).stdout
    return [ROOT / f for f in out.splitlines() if Path(f).suffix not in SKIP_SUFFIX]


def _scan(pattern: re.Pattern) -> list[str]:
    bad = []
    for path in _tracked():
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, FileNotFoundError):
            continue
        for n, line in enumerate(text.splitlines(), 1):
            if pattern.search(line):
                bad.append(f"{path.relative_to(ROOT)}:{n}")  # no line text: it may be the secret itself
    return bad


def test_no_machine_paths_or_real_emails():
    bad = _scan(re.compile(f"{HOME_PATH.pattern}|{EMAIL.pattern}|{RUN_IDS.pattern}"))
    assert not bad, "Private-looking paths/e-mails/run ids in tracked files:\n" + "\n".join(bad[:40])


def _private_terms() -> re.Pattern | None:
    path = Path(os.environ.get("CMI_PRIVATE_TERMS") or Path.home() / ".config" / "cmi" / "private-terms.txt")
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return None
    terms = [ln.strip() for ln in lines if ln.strip() and not ln.lstrip().startswith("#")]
    return re.compile("|".join(f"(?:{t})" for t in terms), re.I) if terms else None


def test_no_private_project_or_client_names():
    pattern = _private_terms()
    if pattern is None:
        if os.environ.get("CMI_REQUIRE_PRIVATE_TERMS") == "1":
            pytest.fail("private-terms file missing: the export must check private names")
        pytest.skip("no private-terms file on this machine")
    bad = _scan(pattern)
    assert not bad, "Private project/client names in tracked files:\n" + "\n".join(bad[:40])
