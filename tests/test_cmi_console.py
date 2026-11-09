"""CMI Console security tests: the backend must never put a credential on the wire,
must stay read-only, and the UI bundle must keep its no-injection rules."""
from __future__ import annotations

import json
import re
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

core = pytest.importorskip("cmi_console.core", reason="Console plugin is not part of the wheel/sdist")

REPO = Path(__file__).resolve().parent.parent
FAKE_OAUTH = "sk-ant-oat01-" + "A1b2C3d4" * 10
FAKE_REFRESH = "sk-ant-ort01-" + "Z9y8X7w6" * 10
FAKE_OPAQUE = "q7Hk2Lm9Pz4Rt8Vw3Xy6Bn1Cd5Fg0Jk"  # no known prefix: only the known-secret check catches it


def _pool_entry(**kw):
    base = {
        "id": "abc123", "label": "work", "auth_type": "oauth", "priority": 0, "source": "manual",
        "access_token": FAKE_OAUTH, "refresh_token": FAKE_REFRESH, "last_status": "ok",
        "request_count": 3, "model_cooldowns": {}, "provider": "anthropic", "identity": "fp:xyz",
    }
    base.update(kw)
    return base


# --- allowlist serialisation ---------------------------------------------------------------

def test_shape_account_drops_every_unlisted_field():
    row = core.shape_account(_pool_entry(), now=0)
    body = json.dumps(row)
    assert FAKE_OAUTH not in body and FAKE_REFRESH not in body
    assert "access_token" not in row and "refresh_token" not in row and "id" not in row
    assert set(row) == {
        "key", "provider", "label", "auth_type", "source", "priority", "status", "state",
        "request_count", "last_error_code", "last_error_reason", "last_error_reset_in_s",
        "model_cooldowns", "windows", "resets_at", "tag", "email",
    }


def test_key_is_opaque_and_stable():
    a = core.shape_account(_pool_entry(), now=0)["key"]
    b = core.shape_account(_pool_entry(access_token="other"), now=0)["key"]
    assert a == b and re.fullmatch(r"[0-9a-f]{12}", a) and "xyz" not in a


@pytest.mark.parametrize("field", ["label", "last_error_reason", "source"])
def test_free_text_redacts_secret_and_email(field):
    row = core.shape_account(_pool_entry(**{field: f"alice@example.com {FAKE_OAUTH}"}), now=0)
    assert FAKE_OAUTH not in json.dumps(row)
    assert "alice@" not in json.dumps(row)
    assert "a•••@example.com" in row[field]


def test_safe_text_strips_control_and_bidi_and_bounds_length():
    assert core.safe_text("a\x00b\u202ec\x1bd") == "abcd"
    assert len(core.safe_text("x" * 500, 60)) == 60


def test_expired_cooldowns_dropped_and_values_clamped():
    row = core.shape_account(_pool_entry(model_cooldowns={"m1": 50, "m2": 200},
                                         windows=[{"label": "w", "used_percent": 180}]), now=100)
    assert [c["model"] for c in row["model_cooldowns"]] == ["m2"]
    assert row["windows"][0]["used_percent"] == 100.0


def test_unknown_state_is_normalised():
    assert core.shape_account(_pool_entry(state="<script>"), now=0)["state"] == "unknown"


# --- leak guard ------------------------------------------------------------------------------

def test_guard_blocks_known_secret_without_prefix():
    with pytest.raises(core.LeakError) as e:
        core.guard_payload({"x": {"y": [f"prefix {FAKE_OPAQUE} suffix"]}}, {FAKE_OPAQUE})
    assert FAKE_OPAQUE not in str(e.value)


@pytest.mark.parametrize("value", [
    FAKE_OAUTH, "ghp_" + "a" * 36, "AIza" + "b" * 35,
    "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dozjgNryP4J3jVmNHl0w5N_XgL0n3I9PlFUP0THsR8U",
    "Bearer abcdefghijklmnopqrstuvwxyz123456",
])
def test_guard_blocks_credential_shapes(value):
    with pytest.raises(core.LeakError):
        core.guard_payload({"note": value})


def test_guard_ignores_non_opaque_known_values():
    # env-var names / labels stored under a *token* field must not brick the dashboard
    assert core.guard_payload({"label": "ANTHROPIC_TOKEN"}, {"ANTHROPIC_TOKEN", "env:ANTHROPIC_TOKEN"})


def test_guard_passes_clean_payload():
    assert json.loads(core.guard_payload({"a": 1}, {FAKE_OPAQUE})) == {"a": 1}


# --- adapter end-to-end against a fake HERMES_HOME ------------------------------------------

def _fake_home(tmp_path: Path) -> Path:
    db = tmp_path / "state.db"
    con = sqlite3.connect(db)
    con.execute("""CREATE TABLE sessions (id TEXT, source TEXT, title TEXT, model TEXT, billing_provider TEXT,
        git_repo_root TEXT, cwd TEXT, started_at REAL, last_activity_at REAL, input_tokens INT, output_tokens INT,
        cache_read_tokens INT, cache_write_tokens INT, actual_cost_usd REAL, estimated_cost_usd REAL, hidden INT)""")
    import time as _t
    now = _t.time()
    con.execute("INSERT INTO sessions VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                ("s1", "desktop", f"debug {FAKE_OAUTH} for bob@example.com", "claude", "anthropic",
                 "/x/proj", "/x/proj/sub", now - 50, now - 10, 100, 20, 1000, 0, None, 0.5, 0))
    con.execute("INSERT INTO sessions VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                ("s2", "cli", "hidden one", "m", "zai", "", "/y", now - 50, now - 10, 1, 1, 0, 0, None, 0, 1))
    con.commit()
    con.close()
    (tmp_path / "cmi_stats.jsonl").write_text(
        json.dumps({"t": now - 5, "m": "claude", "p": "anthropic", "raw": 1000, "out": 600, "n": 1, "rc": 1, "lx": 0}) + "\nnot json\n")
    return tmp_path


def test_read_sessions_is_readonly_and_sanitised(tmp_path, monkeypatch):
    from cmi_console import hermes_adapter as ad

    home = _fake_home(tmp_path)
    monkeypatch.setattr(ad, "_home", lambda: home)
    before = (home / "state.db").read_bytes()
    out = ad.read_sessions(__import__("time").time())
    assert (home / "state.db").read_bytes() == before
    assert out["total"] == 1  # hidden session excluded
    body = json.dumps(out)
    assert FAKE_OAUTH not in body and "bob@" not in body and "s1" not in [r["key"] for r in out["recent"]]
    assert out["projects"][0]["name"] == "proj"
    assert out["account_attribution"] == "unavailable"


def test_read_only_connection_refuses_writes(tmp_path, monkeypatch):
    home = _fake_home(tmp_path)
    con = sqlite3.connect(f"file:{(home / 'state.db').as_posix()}?mode=ro", uri=True)
    with pytest.raises(sqlite3.OperationalError):
        con.execute("DELETE FROM sessions")


def test_read_savings_tolerates_bad_lines(tmp_path, monkeypatch):
    from cmi_console import hermes_adapter as ad

    home = _fake_home(tmp_path)
    monkeypatch.setattr(ad, "_home", lambda: home)
    monkeypatch.setattr(ad, "_price", lambda m, p: None)
    out = ad.read_savings(__import__("time").time())
    assert out["installed"] and out["windows"]["30d"]["tokens_saved"] == 400
    assert out["windows"]["30d"]["cost_raw_usd"] is None  # unknown price -> no invented cost


def test_collect_secrets_finds_nested_token_fields():
    from cmi_console import hermes_adapter as ad

    found: set = set()
    ad._collect_secrets({"anthropic": [{"access_token": FAKE_OAUTH, "extra": {"api_key": FAKE_OPAQUE}, "label": "x"}]}, found)
    assert {FAKE_OAUTH, FAKE_OPAQUE} <= found


# --- backend surface -------------------------------------------------------------------------

def test_backend_exposes_only_get_routes():
    src = (REPO / "cmi_console" / "dashboard" / "plugin_api.py").read_text()
    routes = re.findall(r'@router\.(\w+)\("([^"]+)"', src)
    # the only writes: the plugin's own per-project model policy file
    assert {p for v, p in routes if v != "get"} == {"/accounts/first", "/accounts/name", "/accounts/reconnect", "/accounts/price",
                                                         "/accounts/add/begin", "/accounts/add/finish",
                                                             "/providers/add/begin", "/providers/add/status", "/providers/add/key",
                                                             "/login/open"}
    assert "make-first" in src
    assert "no-store" in src


def test_adapter_never_writes():
    src = (REPO / "cmi_console" / "hermes_adapter.py").read_text()
    for forbidden in ("write_credential_pool", "_save_auth_store", ".write_text(", "open(path, \"w",
                      "INSERT ", "UPDATE ", "DELETE ", "requests.", "httpx.", "urllib.request"):
        assert forbidden not in src, forbidden
    assert "mode=ro" in src


# --- UI bundle rules -------------------------------------------------------------------------

def test_ui_has_no_injection_or_network_sinks():
    for rel in ("console-ui/src/core.js", "console-ui/src/shell-hermes.js"):
        src = re.sub(r"^\s*//.*$", "", (REPO / rel).read_text(), flags=re.M)  # code only, not the rules comment
        for sink in ("dangerouslySetInnerHTML", "innerHTML", "eval(", "new Function", "fetch(",
                     "XMLHttpRequest", "WebSocket", "localStorage", "document.cookie", "window.open"):
            assert sink not in src, f"{rel}: {sink}"


def test_desktop_bundle_is_fresh_and_imports_only_sdk_and_react():
    r = subprocess.run([sys.executable, str(REPO / "console-ui" / "build.py"), "--check"], capture_output=True, text=True)
    assert r.returncode == 0, r.stdout
    bundle = (REPO / "cmi_console" / "desktop" / "plugin.js").read_text()
    imports = re.findall(r"^import .* from '([^']+)'", bundle, re.M)
    assert sorted(set(imports)) == ["@hermes/plugin-sdk", "react"]
    assert "defaultEnabled: false" in bundle


def test_guard_catches_secret_that_json_escapes():
    secret = 'ab"cd\\ef9Gh2Jk7Lm4Np'  # quote and backslash are escaped by json.dumps
    with pytest.raises(core.LeakError):
        core.guard_payload({"x": secret}, {secret})


# --- account names + make-first --------------------------------------------------------------

@pytest.fixture
def prefs(tmp_path, monkeypatch):
    from cmi_console import account_prefs

    monkeypatch.setattr(account_prefs, "_home", lambda: tmp_path)
    return account_prefs


def test_prefs_roundtrip_private_and_only_own_file(prefs, tmp_path):
    (tmp_path / "auth.json").write_text("{}")
    prefs.set_tags({"f14eba092551": "perso"})
    assert prefs.load_tags() == {"f14eba092551": "perso"}
    f = tmp_path / "cmi_console_accounts.json"
    assert __import__("os").name == "nt" or oct(f.stat().st_mode & 0o777) == "0o600"
    assert sorted(p.name for p in tmp_path.iterdir()) == ["auth.json", "cmi_console_accounts.json"]
    assert (tmp_path / "auth.json").read_text() == "{}"


@pytest.mark.parametrize("key,tag", [("../x", "perso"), ("f14eba092551", "root"), ("F14EBA092551", "pro"), ("short", "pro")])
def test_prefs_rejects_bad_input(prefs, key, tag):
    with pytest.raises(prefs.PrefsError):
        prefs.set_tags({key: tag})


def test_prefs_corrupt_file_never_wiped(prefs, tmp_path):
    f = tmp_path / "cmi_console_accounts.json"
    prefs.set_tags({"f14eba092551": "perso"})
    f.write_text("{bad")
    assert prefs.load_tags() == {}  # display: empty, no crash
    with pytest.raises(prefs.PrefsError):
        prefs.set_tags({"e800c7a05062": "pro"})
    assert f.read_text() == "{bad" and (tmp_path / "cmi_console_accounts.json.corrupt").exists()


def test_make_first_rejects_unknown_tag_and_provider(prefs):
    from cmi_console import hermes_adapter as ad

    for tag, prov in (("root", "anthropic"), ("perso", "openai-codex")):
        with pytest.raises(prefs.PrefsError):
            ad.make_first(tag, prov)


def test_make_first_uses_hermes_move_entry_not_file_edits():
    src = (REPO / "cmi_console" / "hermes_adapter.py").read_text()
    assert "move_entry" in src and "auth.json" not in src.split("def make_first")[1].split("def _identity")[0].replace("auth.json by hand", "")


def test_shell_account_switch_is_explicit_and_session_scoped():
    src = (REPO / "console-ui" / "src" / "shell-hermes.js").read_text()
    assert "switchAccount:" in src
    assert "config.set" in src and "--session" in src and "--global" not in src
    assert "info.provider !== 'anthropic'" in src


def test_account_switch_remains_visible_on_overview_and_when_pro_is_ready():
    src = (REPO / "console-ui" / "src" / "core.js").read_text()
    assert "actions: false" not in src
    assert "canPost && firstTag !== tag" not in src
    assert "H.switchTo(nm(tag))" in src


def test_make_first_retry_clears_only_selected_account(prefs, monkeypatch):
    import types
    from cmi_console import hermes_adapter as ad

    entries = [types.SimpleNamespace(id=i, priority=n) for n, i in enumerate(('personal', 'work'))]
    prefs.set_tags({core.opaque_id('anthropic', 'entry:personal'): 'perso',
                    core.opaque_id('anthropic', 'entry:work'): 'pro'})
    calls = []
    pool = types.SimpleNamespace(entries=lambda: entries,
                                 move_entry=lambda i, n: calls.append(('move', i, n)),
                                 reset_status=lambda i: calls.append(('reset', i)))
    monkeypatch.setitem(sys.modules, 'agent.credential_pool', types.SimpleNamespace(load_pool=lambda p: pool))
    monkeypatch.setitem(sys.modules, 'agent.account_usage_cache', types.SimpleNamespace(_identity_id_for=lambda p, e: 'fp:' + e.id))
    ad.make_first('pro', retry=True)
    assert calls == [('move', 'work', 0), ('reset', 'work')]
    calls.clear()
    ad.make_first('pro')
    assert calls == [('move', 'work', 0)]


def _acc(tag, used, state="ready", prov="anthropic"):
    return {"provider": prov, "tag": tag, "state": state, "windows": [{"label": "s", "used_percent": used[0]}, {"label": "w", "used_percent": used[1]}]}


def test_relogin_entry_inherits_name_from_identical_quota():
    from cmi_console import hermes_adapter as ad

    a = [_acc("pro", (46, 10)), _acc(None, (46, 10)), _acc("perso", (100, 78)), _acc(None, (100, 78))]
    ad._inherit_names(a)
    assert [x["tag"] for x in a] == ["pro", "pro", "perso", "perso"] and a[1]["tag_guess"] and "tag_guess" not in a[0]


def test_ambiguous_or_unknown_quota_stays_unnamed():
    from cmi_console import hermes_adapter as ad

    a = [_acc("pro", (0, 0)), _acc("perso", (0, 0)), _acc(None, (0, 0)), {"provider": "anthropic", "tag": None, "state": "unknown", "windows": []}]
    ad._inherit_names(a)
    assert a[2]["tag"] is None and a[3]["tag"] is None


def test_reconnect_names_only_new_unnamed_entries(prefs):
    from cmi_console import hermes_adapter as ad

    prefs.set_tags({"aaaaaaaaaaaa": "pro"})
    prefs.start_reconnect("perso", ["aaaaaaaaaaaa", "bbbbbbbbbbbb"])
    acc = [{"provider": "anthropic", "key": k, "tag": t, "source": s} for k, t, s in
           (("aaaaaaaaaaaa", "pro", "manual"), ("bbbbbbbbbbbb", None, "manual"), ("cccccccccccc", None, "manual"), ("dddddddddddd", None, "env:ANTHROPIC_TOKEN"))]
    ad._claim_new_login(acc)
    assert [a["tag"] for a in acc] == ["pro", None, "perso", None]
    assert prefs.load_tags()["cccccccccccc"] == "perso" and prefs.pending() == {}
    assert prefs.load_tags()["aaaaaaaaaaaa"] == "pro"


def test_reconnect_expires_and_survives_naming(prefs, monkeypatch):
    prefs.start_reconnect("pro", [])
    prefs.set_tags({"eeeeeeeeeeee": "pro"})  # naming must not drop the pending marker
    assert prefs.pending()["tag"] == "pro"
    import time as _t
    monkeypatch.setattr(_t, "time", lambda: 9e12)
    assert prefs.pending() == {}


def test_reconnect_never_handles_tokens():
    src = (REPO / "cmi_console" / "dashboard" / "plugin_api.py").read_text().split("class _ReconIn")[1]
    assert "oauth_login" not in src and "input(" not in src and "subprocess" not in src


def test_container_repo_is_split_into_projects():
    rows = [{"project_root": "/h/L", "cwd": f"/h/L/{n}/sub"} for n in ("A", "B", "C")] + \
           [{"project_root": "/h/L", "cwd": "/h/L"}, {"project_root": "/h/solo", "cwd": "/h/solo/x"}, {"project_root": "/h/L", "cwd": "/h/L/_raccourcis"}]
    core.split_container_repos(rows)
    assert [r["project_root"] for r in rows] == ["/h/L/A", "/h/L/B", "/h/L/C", "/h/L", "/h/solo", "/h/L"]


def test_price_roundtrip_and_validation(prefs):
    prefs.set_tags({"aaaaaaaaaaaa": "pro"})
    assert prefs.set_price("perso", 20) == {"perso": 20.0}
    assert prefs.load_tags() == {"aaaaaaaaaaaa": "pro"}  # price write keeps names
    prefs.set_tags({"bbbbbbbbbbbb": "perso"})
    assert prefs.load_prices() == {"perso": 20.0}  # name write keeps prices
    for bad in (-1, 99999, True, "20"):
        with pytest.raises(prefs.PrefsError):
            prefs.set_price("perso", bad)
    with pytest.raises(prefs.PrefsError):
        prefs.set_price("root", 5)


def test_ledger_attributes_only_unambiguous_requests(monkeypatch):
    from cmi_console import ledger

    seq = iter([{"a": 1, "b": 1}, {"a": 2, "b": 1}, {"a": 3, "b": 2}, {"a": 3, "b": 2}])
    monkeypatch.setattr(ledger, "_counts", lambda p: next(seq))
    ledger._last.clear()
    assert [ledger.attribute("anthropic") for _ in range(4)] == [None, "a", None, None]


def test_ledger_row_has_no_secret_fields_and_summary(tmp_path, monkeypatch):
    from cmi_console import ledger

    monkeypatch.setattr(ledger, "_home", lambda: tmp_path)
    monkeypatch.setattr(ledger, "attribute", lambda p: "abcdefabcdef")
    ledger.on_post_api_request(provider="anthropic", model="m", session_id="sess-1", usage={"input_tokens": 5, "output_tokens": 2},
                               api_key=FAKE_OAUTH, messages=["secret prompt"])
    f = tmp_path / "cmi_console_ledger.jsonl"
    body = f.read_text()
    assert FAKE_OAUTH not in body and "secret prompt" not in body and "sess-1" not in body
    assert __import__("os").name == "nt" or oct(f.stat().st_mode & 0o777) == "0o600"
    assert ledger.summarize(__import__("time").time())["accounts"]["abcdefabcdef"] == {"requests": 1, "tokens": 7}


def test_ledger_ignores_other_providers(tmp_path, monkeypatch):
    from cmi_console import ledger

    monkeypatch.setattr(ledger, "_home", lambda: tmp_path)
    ledger.on_post_api_request(provider="zai", usage={})
    assert not (tmp_path / "cmi_console_ledger.jsonl").exists()


def test_account_key_survives_token_rotation():
    from cmi_console import hermes_adapter as ad

    e = {"id": "4d655f"}
    k1 = core.shape_account({**_pool_entry(id="4d655f"), "key_basis": ad.entry_key_basis("anthropic", e, "fp:old")})["key"]
    k2 = core.shape_account({**_pool_entry(id="4d655f"), "key_basis": ad.entry_key_basis("anthropic", e, "fp:new")})["key"]
    assert k1 == k2
    assert ad.entry_key_basis("anthropic", {}, "fp:old") == "fp:old"  # no id: fingerprint fallback


def test_last_unnamed_login_takes_the_only_free_name():
    from cmi_console import hermes_adapter as ad

    a = [{**_acc("pro", (1, 1)), "source": "manual"}, {**_acc("perso", (3, 3)), "source": "manual"},
         {**_acc(None, (2, 2)), "source": "manual"}, {**_acc(None, (0, 0)), "source": "env:ANTHROPIC_TOKEN"}]
    ad._infer_last_name(a)
    assert a[2]["tag"] == "pro2" and a[2]["tag_guess"] and a[3]["tag"] is None
    c = [{**_acc("pro", (1, 1)), "source": "manual"}, {**_acc(None, (2, 2)), "source": "manual"}]
    ad._infer_last_name(c)
    assert c[1]["tag"] is None  # two names still free: never guess
    b = [{**_acc(None, (1, 1)), "source": "manual"}, {**_acc(None, (2, 2)), "source": "manual"}]
    ad._infer_last_name(b)
    assert [x["tag"] for x in b] == [None, None]  # two candidates: never guess
