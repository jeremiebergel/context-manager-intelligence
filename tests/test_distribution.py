"""Artifact checks enabled by CMI_DIST_DIR; ordinary source tests need no build tools."""
from email.parser import Parser
import os
from pathlib import Path
import tarfile
import zipfile

import pytest


@pytest.fixture
def distributions():
    target = os.environ.get("CMI_DIST_DIR")
    if not target:
        pytest.skip("set CMI_DIST_DIR to check built wheel and sdist")
    assert target is not None
    root = Path(target).resolve()
    wheels = list(root.glob("*.whl"))
    sdists = list(root.glob("*.tar.gz"))
    assert len(wheels) == len(sdists) == 1, "use a clean output directory"
    return wheels[0], sdists[0]


def test_wheel_metadata_and_license(distributions):
    wheel, _ = distributions
    with zipfile.ZipFile(wheel) as archive:
        names = archive.namelist()
        metadata_path, = [name for name in names if name.endswith(".dist-info/METADATA")]
        metadata = Parser().parsestr(archive.read(metadata_path).decode())
        assert metadata["Name"] == "cmi-context"
        assert metadata["License-Expression"] == "MIT"
        assert "LICENSE" in metadata.get_all("License-File", [])
        assert metadata["Requires-Python"] == ">=3.10"
        license_path = metadata_path.rsplit("/", 1)[0] + "/licenses/LICENSE"
        assert archive.read(license_path) == (Path(__file__).resolve().parents[1] / "LICENSE").read_bytes()
        for package in ("cmi", "cmi_review"):
            assert package + "/__init__.py" in names
            assert package + "/plugin.yaml" in names
        entries = archive.read(metadata_path.rsplit("/", 1)[0] + "/entry_points.txt").decode()
        assert "cmi-proxy = cmi.proxy:main" in entries


def test_sdist_fixtures_and_disclosures(distributions):
    _, sdist = distributions
    source = Path(__file__).resolve().parents[1]
    with tarfile.open(sdist) as archive:
        names = archive.getnames()
        roots = {name.split("/", 1)[0] for name in names}
        assert len(roots) == 1
        prefix = roots.pop() + "/"
        paths = {name[len(prefix):] for name in names if name.startswith(prefix)}
        for required in ("LICENSE", "pyproject.toml", "MANIFEST.in", "README.md",
                         "SECURITY.md", "PROVENANCE.md", "tests/conftest.py"):
            assert required in paths
        for fixture in (source / "tests").glob("*.py"):
            assert "tests/" + fixture.name in paths
        fixture_file = archive.extractfile(prefix + "tests/conftest.py")
        assert fixture_file is not None
        conftest = fixture_file.read()
        assert conftest == (source / "tests/conftest.py").read_bytes()
        assert b"CMI_HOME" in conftest and b"HERMES_HOME" in conftest
        assert not any("__pycache__" in name or name.endswith(".pyc") for name in names)
