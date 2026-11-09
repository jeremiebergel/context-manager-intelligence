"""The Hermes symbol list stays complete, and resolves when Hermes is importable."""
import pathlib
import re

import pytest

from cmi import hermes_contract

ROOT = pathlib.Path(__file__).resolve().parents[1]
IMPORT_RE = re.compile(r"^\s*from ((?:agent|hermes_cli)\.[\w.]+) import ([^#\n]+)", re.M)


def test_every_hermes_import_is_listed():
    listed = {(m, a) for m, a, _ in hermes_contract.SYMBOLS}
    found = set()
    for pkg in ("cmi", "cmi_review", "cmi_console"):
        for f in (ROOT / pkg).rglob("*.py"):
            for mod, names in IMPORT_RE.findall(f.read_text(encoding="utf-8")):
                for n in names.strip("() ").split(","):
                    n = n.strip().split(" as ")[0]
                    if n:
                        found.add((mod, n))
    assert found - listed == set(), "add new Hermes imports to cmi/hermes_contract.py"


def test_contract_resolves_on_installed_hermes():
    from cmi.compat import HERMES
    if not HERMES:  # partial/absent Hermes (e.g. source on path without its dependencies)
        pytest.skip("Hermes not fully importable here")
    missing = hermes_contract.check()
    assert not [m for m in missing if m[2]], missing
