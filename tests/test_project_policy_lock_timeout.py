"""A stalled policy writer must not hang a context-engine request."""
import pytest

pytest.importorskip("fcntl", reason="this test holds the lock with POSIX flock from a subprocess")
import subprocess
import sys

from tests.test_project_autobind import host


def test_profile_bootstrap_lock_has_bounded_wait(tmp_path):
    host(tmp_path)
    holder_code = (
        "import fcntl,os,sys; "
        "fd=os.open(sys.argv[1],os.O_RDONLY|os.O_DIRECTORY); "
        "fcntl.flock(fd,fcntl.LOCK_EX); "
        "print('locked',flush=True); sys.stdin.read(1)"
    )
    probe_code = (
        "import sys; from cmi.project_policy import registry,PolicyUnavailable; "
        "\ntry:\n with registry(sys.argv[1],create=True): pass\n"
        "except PolicyUnavailable: sys.exit(0)\n"
        "else: sys.exit(2)\n"
    )
    holder = subprocess.Popen(
        [sys.executable, "-c", holder_code, str(tmp_path)],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True,
    )
    try:
        assert holder.stdout.readline().strip() == "locked"
        probe = subprocess.run(
            [sys.executable, "-c", probe_code, str(tmp_path)],
            capture_output=True, text=True, timeout=3,
        )
        assert probe.returncode == 0, probe.stderr
        assert not (tmp_path / "cmi_policy").exists()
    finally:
        holder.communicate("x", timeout=3)
