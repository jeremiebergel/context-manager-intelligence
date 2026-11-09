"""cmi-proxy install-service: reversible editor wiring and per-OS service files.

No real launchctl/systemctl/schtasks call: `service._run` and `service._spawn` are replaced.
"""
import json
import plistlib
import subprocess

import pytest

from cmi import service

VSC = '{\n    "a": 1,\n    "b": 2,\n    }\n'  # trailing comma, as in a real settings.json
CODEX = 'model = "x"\nnotify = ["a"]\n\n[plugins."p"]\nenabled = true\n'


def test_vscode_add_is_idempotent_and_reversible():
    new, st = service.vscode_add(VSC, 8787)
    assert st == "added" and "127.0.0.1:8787" in new and service.MARK in new
    assert service.vscode_add(new, 8787) == (new, "present")
    back, changed = service.vscode_remove(new)
    assert changed and "claudeCode" not in back and '"b": 2' in back


def test_vscode_adds_missing_comma_and_leaves_foreign_setting():
    new, _ = service.vscode_add('{\n  "a": 1\n}\n', 8787)
    assert '"a": 1,\n' in new
    foreign = '{\n "claudeCode.environmentVariables": []\n}\n'
    assert service.vscode_add(foreign, 8787) == (foreign, "foreign")
    assert service.vscode_add("", 8787)[1] == "added"


def test_codex_provider_goes_before_first_table_and_round_trips():
    new, st = service.codex_add(CODEX, 8787)
    assert st == "added"
    assert new.index("model_provider") < new.index('[plugins."p"]') < new.index("[model_providers.cmi]")
    assert 'base_url = "http://127.0.0.1:8787"' in new and 'wire_api = "responses"' in new
    assert service.codex_add(new, 8787) == (new, "present")
    back, changed = service.codex_remove(new)
    assert changed and back == CODEX


def test_codex_foreign_provider_untouched():
    mine = 'model_provider = "other"\n'
    assert service.codex_add(mine, 8787) == (mine, "foreign")


def test_install_status_uninstall_in_temp_home(tmp_path):
    p = service.paths(tmp_path, "darwin")
    for k, txt in (("vscode", VSC), ("codex", CODEX)):
        p[k].parent.mkdir(parents=True)
        p[k].write_text(txt)
    out = service.install(port=8799, home=tmp_path, exe="/bin/cmi-proxy", plat="darwin")
    plist = plistlib.loads(p["plist"].read_bytes())
    assert plist["ProgramArguments"][:5] == ["/bin/cmi-proxy", "--host", "127.0.0.1", "--port", "8799"]
    assert plist["Nice"] == 10 and (__import__("os").name == "nt" or (p["plist"].stat().st_mode & 0o777) == 0o600)
    assert any("vscode: added" in x for x in out) and any("codex: added" in x for x in out)
    assert list(p["backups"].iterdir())
    st = service.status(port=1, home=tmp_path, plat="darwin")
    assert any("NOT answering" in x for x in st) and any("WARNING" in x for x in st)
    service.uninstall(home=tmp_path, plat="darwin")
    assert p["vscode"].read_text() == VSC and p["codex"].read_text() == CODEX
    assert not p["plist"].exists()
    json.dumps(service.paths(tmp_path), default=str)


def test_handwritten_equivalent_vscode_setting_counts_as_wired(tmp_path):
    hand = '{\n "claudeCode.environmentVariables": [{ "name": "ANTHROPIC_BASE_URL", "value": "http://127.0.0.1:8787" }],\n}\n'
    assert service.vscode_add(hand, 8787) == (hand, "present")
    p = service.paths(tmp_path, "darwin")
    p["vscode"].parent.mkdir(parents=True)
    p["vscode"].write_text(hand)
    assert any("vscode points at the proxy: yes" in x for x in service.status(port=8787, home=tmp_path, plat="darwin"))
    assert service.vscode_remove(hand) == (hand, False)  # never removes what the user wrote


# ---- Linux / Windows (service manager mocked) ---------------------------------------------------
class FakeRun:
    """Records every command; `fail` maps a command prefix (tuple) to (returncode, stderr)."""

    def __init__(self, fail=None, stdout=""):
        self.calls, self.fail, self.stdout = [], fail or {}, stdout

    def __call__(self, cmd, timeout=60):
        assert isinstance(cmd, list) and all(isinstance(a, str) for a in cmd)  # never a shell string
        self.calls.append(cmd)
        for prefix, (rc, err) in self.fail.items():
            if tuple(cmd[:len(prefix)]) == prefix:
                return subprocess.CompletedProcess(cmd, rc, "", err)
        return subprocess.CompletedProcess(cmd, 0, self.stdout, "")


@pytest.fixture
def fake(monkeypatch):
    def make(**kw):
        f = FakeRun(**kw)
        monkeypatch.setattr(service, "_run", f)
        monkeypatch.setattr(service, "_wait_health", lambda port, seconds=6.0: {"ok": True})
        monkeypatch.setattr(service, "health", lambda port, timeout=2.0: None)
        spawned = []
        monkeypatch.setattr(service, "_spawn", spawned.append)
        f.spawned = spawned
        return f
    return make


def _seed(p):
    for k, txt in (("vscode", VSC), ("codex", CODEX)):
        p[k].parent.mkdir(parents=True)
        p[k].write_text(txt)


def test_paths_per_os(tmp_path):
    assert service.paths(tmp_path, "darwin")["vscode"] == tmp_path / "Library/Application Support/Code/User/settings.json"
    assert service.paths(tmp_path, "linux")["vscode"] == tmp_path / ".config/Code/User/settings.json"
    assert service.paths(tmp_path, "linux")["service"] == tmp_path / ".config/systemd/user/cmi-proxy.service"
    w = service.paths(tmp_path, "win32")
    assert w["vscode"] == tmp_path / "AppData/Roaming/Code/User/settings.json"
    assert w["service"] == tmp_path / "AppData/Roaming/Microsoft/Windows/Start Menu/Programs/Startup/cmi-proxy.cmd"


def test_systemd_unit_quotes_spaces_and_specifiers():
    unit = service.build_systemd_unit(["/home/dev/my env/bin/python", "-m", "cmi.proxy"], 8790)
    line = next(ln for ln in unit.splitlines() if ln.startswith("ExecStart="))
    assert line == ('ExecStart="/home/dev/my env/bin/python" -m cmi.proxy --host 127.0.0.1 --port 8790 '
                    "--openai-upstream https://chatgpt.com/backend-api/codex")
    assert "Restart=on-failure" in unit and "WantedBy=default.target" in unit
    assert service._systemd_quote("/home/dev/50%$x") == "/home/dev/50%%$$x"
    assert service._systemd_quote('a"b\\c') == '"a\\"b\\\\c"'


def test_linux_install_status_uninstall(tmp_path, fake):
    f = fake(stdout="active\n")
    p = service.paths(tmp_path, "linux")
    _seed(p)
    out = service.install(port=8791, home=tmp_path, exe="/home/dev/.local/bin/cmi-proxy", plat="linux", manage=True)
    assert f.calls == [["systemctl", "--user", "show-environment"], ["systemctl", "--user", "daemon-reload"],
                       ["systemctl", "--user", "enable", "cmi-proxy.service"],
                       ["systemctl", "--user", "restart", "cmi-proxy.service"]]
    assert "ExecStart=/home/dev/.local/bin/cmi-proxy --host 127.0.0.1 --port 8791" in p["unit"].read_text()
    assert any("vscode: added" in x for x in out) and any("proxy answers" in x for x in out)
    assert not p["plist"].exists()
    f.calls.clear()
    st = service.status(port=8791, home=tmp_path, plat="linux", manage=True)
    assert "systemd user unit: active" in st and "service file: present" in st
    f.calls.clear()
    service.uninstall(home=tmp_path, plat="linux", manage=True)
    assert f.calls == [["systemctl", "--user", "disable", "--now", "cmi-proxy.service"],
                       ["systemctl", "--user", "daemon-reload"]]
    assert not p["unit"].exists() and p["vscode"].read_text() == VSC and p["codex"].read_text() == CODEX


def test_linux_without_user_systemd_changes_nothing(tmp_path, fake):
    f = fake(fail={("systemctl", "--user", "show-environment"): (1, "Failed to connect to bus")})
    p = service.paths(tmp_path, "linux")
    _seed(p)
    out = service.install(port=8791, home=tmp_path, exe="/bin/cmi-proxy", plat="linux", manage=True)
    assert out[0].startswith("ERROR:") and "Failed to connect to bus" in out[0]
    assert len(f.calls) == 1 and not p["unit"].exists() and not (tmp_path / ".cmi").exists()
    assert p["vscode"].read_text() == VSC and p["codex"].read_text() == CODEX


def test_linux_failed_start_rolls_back(tmp_path, fake):
    f = fake(fail={("systemctl", "--user", "restart"): (1, "Job failed")})
    p = service.paths(tmp_path, "linux")
    _seed(p)
    out = service.install(port=8791, home=tmp_path, exe="/bin/cmi-proxy", plat="linux", manage=True)
    assert out[-1].startswith("ERROR:") and "rolled back" in out[-1]
    assert ["systemctl", "--user", "disable", "--now", "cmi-proxy.service"] in f.calls
    assert f.calls[-1] == ["systemctl", "--user", "daemon-reload"]
    assert not p["unit"].exists() and p["vscode"].read_text() == VSC


def test_linux_failure_makes_cli_exit_nonzero(monkeypatch, capsys):
    monkeypatch.setattr(service, "install", lambda **kw: ["ERROR: no bus"])
    assert service.run("install-service", []) == 1
    monkeypatch.setattr(service, "install", lambda **kw: ["ok"])
    assert service.run("install-service", []) == 0


def test_windows_task_command_is_one_quoted_argument():
    exe = ["C:\\Users\\dev\\App Data\\Python\\pythonw.exe", "-m", "cmi.proxy"]
    tr = service.build_task_command(exe, 8787)
    assert tr.startswith('"C:\\Users\\dev\\App Data\\Python\\pythonw.exe" -m cmi.proxy --host 127.0.0.1')
    cmd = service.build_startup_cmd(["C:\\x\\50%\\pythonw.exe"], 8787)
    assert cmd.startswith("@echo off\r\n") and 'start "" /B C:\\x\\50%%\\pythonw.exe --host' in cmd
    with pytest.raises(ValueError):
        service.build_task_command(['C:\\a"b.exe'], 8787)


def test_windows_schtasks_install_and_uninstall(tmp_path, fake):
    f = fake()
    p = service.paths(tmp_path, "win32")
    _seed(p)
    exe = ["C:\\Py thon\\pythonw.exe", "-m", "cmi.proxy"]
    out = service.install(port=8787, home=tmp_path, exe=exe, plat="win32", manage=True)
    create = f.calls[0]
    assert create[:5] == ["schtasks", "/Create", "/SC", "ONLOGON", "/TN"] and create[-1] == "/F"
    assert create[create.index("/TR") + 1] == service.build_task_command(exe, 8787)
    assert f.calls[1] == ["schtasks", "/Run", "/TN", "cmi-proxy"]
    assert not p["startup"].exists() and not f.spawned
    assert any("scheduled task" in x for x in out) and any("vscode: added" in x for x in out)
    assert "service: scheduled task present" in service.status(home=tmp_path, plat="win32", manage=True)
    f.calls.clear()
    service.uninstall(home=tmp_path, plat="win32", manage=True)
    assert ["schtasks", "/Delete", "/TN", "cmi-proxy", "/F"] in f.calls
    assert p["vscode"].read_text() == VSC and p["codex"].read_text() == CODEX


def test_windows_refused_schtasks_falls_back_to_startup_folder(tmp_path, fake):
    f = fake(fail={("schtasks", "/Create"): (1, "ERROR: Access is denied."),
                   ("schtasks", "/Query"): (1, "ERROR: The system cannot find the file specified.")})
    p = service.paths(tmp_path, "win32")
    exe = ["C:\\Python\\pythonw.exe", "-m", "cmi.proxy"]
    out = service.install(port=8787, home=tmp_path, exe=exe, plat="win32", manage=True, editors=False)
    assert "Access is denied" in out[0] and p["startup"].exists() and f.spawned == [exe]
    assert "--port 8787" in p["startup"].read_text()
    assert "service: startup script present" in service.status(home=tmp_path, plat="win32", manage=True)
    service.uninstall(home=tmp_path, plat="win32", manage=True)
    assert not p["startup"].exists() and not any(c[:2] == ["schtasks", "/Delete"] for c in f.calls)


def test_too_long_task_command_uses_startup_folder_without_calling_schtasks(tmp_path, fake):
    f = fake()
    exe = ["C:\\" + "d" * 300 + "\\pythonw.exe"]
    out = service.install(home=tmp_path, exe=exe, plat="win32", manage=True, editors=False)
    assert not any(c[:2] == ["schtasks", "/Create"] for c in f.calls) and "longer than 261" in out[0]
    assert service.paths(tmp_path, "win32")["startup"].exists()


def test_test_home_never_calls_a_service_manager(tmp_path, fake):
    f = fake()
    for plat in ("darwin", "linux", "win32"):
        service.install(home=tmp_path / plat, exe="/bin/cmi-proxy", plat=plat)
        service.status(home=tmp_path / plat, plat=plat)
        service.uninstall(home=tmp_path / plat, plat=plat)
    assert f.calls == [] and f.spawned == []
