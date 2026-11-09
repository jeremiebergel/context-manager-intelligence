"""cmi-proxy install-service: reversible editor wiring (no launchctl, temp home)."""
import json
import plistlib

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
    p = service.paths(tmp_path)
    for k, txt in (("vscode", VSC), ("codex", CODEX)):
        p[k].parent.mkdir(parents=True)
        p[k].write_text(txt)
    out = service.install(port=8799, home=tmp_path, exe="/bin/cmi-proxy")
    plist = plistlib.loads(p["plist"].read_bytes())
    assert plist["ProgramArguments"][:5] == ["/bin/cmi-proxy", "--host", "127.0.0.1", "--port", "8799"]
    assert plist["Nice"] == 10 and (__import__("os").name == "nt" or (p["plist"].stat().st_mode & 0o777) == 0o600)
    assert any("vscode: added" in x for x in out) and any("codex: added" in x for x in out)
    assert list(p["backups"].iterdir())
    st = service.status(port=1, home=tmp_path)
    assert any("NOT answering" in x for x in st) and any("WARNING" in x for x in st)
    service.uninstall(home=tmp_path)
    assert p["vscode"].read_text() == VSC and p["codex"].read_text() == CODEX
    assert not p["plist"].exists()
    json.dumps(service.paths(tmp_path), default=str)


def test_handwritten_equivalent_vscode_setting_counts_as_wired(tmp_path):
    hand = '{\n "claudeCode.environmentVariables": [{ "name": "ANTHROPIC_BASE_URL", "value": "http://127.0.0.1:8787" }],\n}\n'
    assert service.vscode_add(hand, 8787) == (hand, "present")
    p = service.paths(tmp_path)
    p["vscode"].parent.mkdir(parents=True)
    p["vscode"].write_text(hand)
    assert any("vscode points at the proxy: yes" in x for x in service.status(port=8787, home=tmp_path))
    assert service.vscode_remove(hand) == (hand, False)  # never removes what the user wrote
