"""Make the ``cmi`` package importable and, unless CMI_STANDALONE=1, the Hermes core as ``agent.*``.

Point HERMES_AGENT_DIR at a hermes-agent checkout if it isn't at ~/.hermes/hermes-agent.
Run with that checkout's interpreter:  ~/.hermes/hermes-agent/venv/bin/python -m pytest
"""
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
# CMI_STANDALONE=1 runs the suite without Hermes on the path (the compat fallbacks), the way
# the proxy and any non-Hermes host see it.
CORE = Path(os.environ.get("HERMES_AGENT_DIR", Path.home() / ".hermes" / "hermes-agent"))
if os.environ.get("CMI_STANDALONE") != "1" and CORE.is_dir() and str(CORE) not in sys.path:
    sys.path.insert(0, str(CORE))
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(Path(__file__).parent) not in sys.path:  # spawn-safe helper modules (cold_worker.py)
    sys.path.insert(0, str(Path(__file__).parent))


import pytest  # noqa: E402


@pytest.fixture(autouse=True)
def _isolated_hermes_home(tmp_path, monkeypatch):
    """Never touch real hosted or standalone stores from tests."""
    home = str(tmp_path / "hermes_home")
    monkeypatch.setenv("HERMES_HOME", home)
    monkeypatch.setenv("CMI_HOME", home)


def pytest_collection_modifyitems(config, items):
    """The sdist ships cmi/ and tests/ only: tests that drive bench/ skip there."""
    import importlib.util
    try:
        has_bench = importlib.util.find_spec("bench.project_catalog") is not None
    except (ImportError, ValueError):
        has_bench = False
    if has_bench:
        return
    skip = pytest.mark.skip(reason="bench/ is not part of the source distribution")
    for item in items:
        if "from bench." in _source(item):
            item.add_marker(skip)


def _source(item):
    import inspect
    try:
        return inspect.getsource(item.function)
    except (OSError, TypeError, AttributeError):
        return ""


import pytest  # noqa: E402


@pytest.fixture(autouse=True)
def _project_memory_off_unless_a_test_turns_it_on(monkeypatch):
    """Project memory is ON by default inside Hermes (2026-11-09). Most tests exercise the engine
    without a project: keep them isolated from the host config. Tests that cover the default or an
    explicit setting install their own fake config module, which takes precedence."""
    try:
        import hermes_cli.config as host_config
    except Exception:
        return
    monkeypatch.setattr(host_config, "load_config_readonly",
                        lambda *a, **k: {"context": {"cmi": {"auto_project_binding": False}}}, raising=False)
