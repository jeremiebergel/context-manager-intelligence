"""Control Center 'Add a Claude account': the login relay and the already-connected check.

No real login: the child process is a stand-in script, the pool and quota readers are faked.
"""
import sys
import textwrap

import pytest

pytest.importorskip("cmi_console.login", reason="Console plugin is not part of the wheel/sdist")
from cmi_console import core, hermes_adapter as ad, login  # noqa: E402

URL = ("https://claude.ai/oauth/authorize?code=true&client_id=00000000-0000-0000-0000-000000000000"
       "&response_type=code&redirect_uri=https%3A%2F%2Fconsole.anthropic.com%2Foauth%2Fcode%2Fcallback"
       "&scope=org%3Acreate_api_key+user%3Aprofile&code_challenge=E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM"
       "&code_challenge_method=S256&state=dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk")

FAKE_HERMES = textwrap.dedent('''
    import sys
    print("Authorize Hermes with your Claude Pro/Max subscription.", flush=True)
    print("  %s", flush=True)
    code = sys.stdin.readline().strip()
    if code == "good-code-123#state":
        print('Added anthropic OAuth credential #2: "x"', flush=True)
    else:
        print("Anthropic OAuth login did not return credentials.", flush=True)
        sys.exit(1)
''' % URL)


@pytest.fixture
def fake_login(tmp_path, monkeypatch):
    script = tmp_path / "fake_hermes.py"
    script.write_text(FAKE_HERMES)
    monkeypatch.setattr(login, "_child_argv", lambda: [sys.executable, str(script)])
    yield login
    login.cancel()


def test_start_returns_the_authorization_link_and_submit_reports_success(fake_login):
    started = fake_login.start()
    assert started["url"] == URL
    assert fake_login.submit(started["id"], "good-code-123#state") == {"ok": True}


def test_wrong_code_is_reported_as_rejected_not_added(fake_login):
    started = fake_login.start()
    assert fake_login.submit(started["id"], "wrong-code-123#state") ["reason"] == "rejected"


def test_submit_validates_the_code_and_the_login_id(fake_login):
    started = fake_login.start()
    for bad in ("", "short", "has space in it 123", "x" * 700, "a;rm -rf /"):
        with pytest.raises(fake_login.LoginError):
            fake_login.submit(started["id"], bad)
    with pytest.raises(fake_login.LoginError):
        fake_login.submit("0" * 16, "good-code-123#state")


def test_a_new_login_replaces_the_previous_one(fake_login):
    first = fake_login.start()
    second = fake_login.start()
    with pytest.raises(fake_login.LoginError):
        fake_login.submit(first["id"], "good-code-123#state")
    assert fake_login.submit(second["id"], "good-code-123#state") == {"ok": True}


def test_no_link_printed_means_a_clean_error(tmp_path, monkeypatch):
    script = tmp_path / "silent.py"
    script.write_text("import time; time.sleep(30)")
    monkeypatch.setattr(login, "_child_argv", lambda: [sys.executable, str(script)])
    monkeypatch.setattr(login, "START_TIMEOUT_S", 0.5)
    with pytest.raises(login.LoginError):
        login.start()
    assert login._cur is None


def test_the_login_link_passes_the_leak_guard():
    core.guard_payload({"id": "0123456789abcdef", "url": URL, "tag": "pro2"})


def test_login_module_never_reads_credentials():
    src = open(login.__file__).read()
    assert "auth.json" not in src and "credential_pool" not in src and "access_token" not in src


# --- add-account flow: new account vs account already connected -------------------------------

def _acct(entry_id, tag, windows):
    return {"provider": "anthropic", "key": ad._entry_key(entry_id), "tag": tag, "windows": windows}


def _w(label, reset):
    return {"label": label, "used_percent": 10.0, "resets_at": reset, "scope": "account"}


@pytest.fixture
def flow(monkeypatch, tmp_path):
    from cmi_console import account_prefs

    monkeypatch.setattr(account_prefs, "_home", lambda: tmp_path)
    state = {"ids": ["perso1", "work1"], "removed": [], "accounts": [], "submit": {"ok": True}}
    monkeypatch.setattr(ad, "_anthropic_entry_ids", lambda: list(state["ids"]))
    monkeypatch.setattr(ad, "read_providers_and_accounts", lambda now: ({"accounts": state["accounts"]}, set()))
    monkeypatch.setattr(ad, "_remove_anthropic_entries", lambda ids: state["removed"].extend(ids))
    monkeypatch.setattr(ad, "USAGE_WAIT_S", 0)
    monkeypatch.setattr(login, "start", lambda: {"id": "a" * 16, "url": URL})
    monkeypatch.setattr(login, "submit", lambda sid, code: state["submit"])
    state["prefs"] = account_prefs
    return state


def _begin_and_finish(flow, tag="pro2"):
    assert ad.add_account_begin(tag)["url"] == URL
    flow["ids"].append("new1")  # what Hermes does when the login succeeds
    return ad.add_account_finish("a" * 16, "good-code-123#state")


def test_a_different_account_is_added_and_named(flow):
    flow["accounts"] = [_acct("perso1", "perso", [_w("Current session", "2026-11-04T18:00:00Z")]),
                        _acct("work1", "pro", [_w("Current session", "2026-11-04T19:30:00Z")]),
                        _acct("new1", None, [_w("Current session", "2026-11-04T21:10:00Z")])]
    assert _begin_and_finish(flow) == {"status": "added", "tag": "pro2", "checked": True}
    assert flow["removed"] == []
    assert flow["prefs"].load_tags()[ad._entry_key("new1")] == "pro2"


def test_an_account_already_connected_is_refused_and_removed(flow):
    flow["accounts"] = [_acct("perso1", "perso", [_w("Current session", "2026-11-04T18:00:00Z")]),
                        _acct("work1", "pro", [_w("Current session", "2026-11-04T19:30:00Z")]),
                        _acct("new1", None, [_w("Current session", "2026-11-04T18:00:00Z")])]
    assert _begin_and_finish(flow) == {"status": "duplicate", "of": "perso"}
    assert flow["removed"] == ["new1"]
    assert ad._entry_key("new1") not in flow["prefs"].load_tags()


def test_idle_accounts_cannot_be_compared_so_it_is_added_but_flagged(flow):
    flow["accounts"] = [_acct("perso1", "perso", []), _acct("work1", "pro", []), _acct("new1", None, [])]
    assert _begin_and_finish(flow) == {"status": "added", "tag": "pro2", "checked": False}
    assert flow["removed"] == []


def test_refused_code_adds_nothing(flow):
    flow["submit"] = {"ok": False, "reason": "rejected"}
    ad.add_account_begin("pro2")
    assert ad.add_account_finish("a" * 16, "bad-code-1234#x") == {"status": "failed", "reason": "rejected", "detail": ""}
    assert flow["removed"] == [] and flow["prefs"].load_tags() == {}


def test_unknown_name_or_login_id_is_rejected(flow):
    with pytest.raises(flow["prefs"].PrefsError):
        ad.add_account_begin("boss")
    ad.add_account_begin("pro2")
    with pytest.raises(flow["prefs"].PrefsError):
        ad.add_account_finish("b" * 16, "good-code-123#state")


def test_a_second_begin_invalidates_the_first_flow(flow):
    ad.add_account_begin("pro2")
    first_state = dict(ad._add_state)
    monkey_ids = flow["ids"]
    monkey_ids.append("other")
    ad.add_account_begin("pro")  # replaces the flow: the old id must not finish anything
    assert ad._add_state["tag"] == "pro" and ad._add_state["ids"] == list(monkey_ids)
    assert first_state["tag"] == "pro2"


def test_add_flow_uses_its_own_lock_not_the_snapshot_lock():
    import inspect
    assert isinstance(ad._add_lock, type(__import__("threading").RLock()))
    src = open(ad.__file__).read().split("def add_account_finish")[1].split("def _remove_anthropic_entries")[0]
    assert "with _add_lock:" in src


# --- other providers: link/device-code login and API key --------------------------------------

DEVICE_HERMES = textwrap.dedent('''
    import sys, time
    print("Signing in to OpenAI Codex...", flush=True)
    print("  1. Open this URL in your browser:\\n     https://auth.openai.com/codex/device", flush=True)
    print("  2. Enter this code:\\n     Z8J4-H8III", flush=True)
    time.sleep(%s)
    print('Added openai-codex OAuth credential #2: "x"', flush=True)
''')


def _device(tmp_path, monkeypatch, delay):
    script = tmp_path / "device.py"
    script.write_text(DEVICE_HERMES % delay)
    monkeypatch.setattr(login, "_provider_argv", lambda provider, *extra: [sys.executable, str(script)])


def test_link_login_returns_link_and_code_then_reports_done(tmp_path, monkeypatch):
    _device(tmp_path, monkeypatch, 1.5)
    started = login.start_link("openai-codex")
    try:
        assert started["url"] == "https://auth.openai.com/codex/device"
        assert started["user_code"] == "Z8J4-H8III"
        assert login.status(started["id"]) == {"state": "waiting"}
        for _ in range(100):
            res = login.status(started["id"])
            if res["state"] != "waiting":
                break
            import time as _t
            _t.sleep(0.1)
        assert res == {"state": "done"}
    finally:
        login.cancel()


def test_link_login_rejects_claude_and_api_key_providers():
    for bad in ("anthropic", "openrouter", "bad slug; rm", ""):
        with pytest.raises(login.LoginError):
            login.start_link(bad)


def test_add_key_validates_provider_and_key():
    with pytest.raises(login.LoginError):
        login.add_key("openai-codex", "sk-whatever-123456")  # OAuth provider
    with pytest.raises(login.LoginError):
        login.add_key("openrouter", "short")
    with pytest.raises(login.LoginError):
        login.add_key("openrouter", "has space in the key 123")


def test_add_key_reports_success_from_hermes_output(monkeypatch):
    class R:
        returncode, stdout = 0, 'Added openrouter credential #1: "x"\n'
    monkeypatch.setattr(login.subprocess, "run", lambda *a, **k: R())
    assert login.add_key("openrouter", "or-key-12345678") == {"ok": True}
    R.returncode = 1
    assert login.add_key("openrouter", "or-key-12345678") == {"ok": False, "reason": "rejected"}


def test_provider_flows(monkeypatch):
    assert [login.flow_for(p) for p in ("anthropic", "openai-codex", "nous", "openrouter", "zai")] == \
        ["code", "link", "link", "key", "key"]


def test_adapter_provider_key_flow_detects_the_new_entry(monkeypatch):
    ids = {"openrouter": ["a"]}
    monkeypatch.setattr(ad, "_pool_ids", lambda p: list(ids.get(p, [])))
    monkeypatch.setattr(ad, "provider_flows", lambda: {"openrouter": "key", "openai-codex": "link"})

    def fake_add(provider, key, label=None):
        ids[provider].append("b")
        return {"ok": True}
    monkeypatch.setattr(login, "add_key", fake_add)
    assert ad.add_provider_key("openrouter", "or-key-12345678") == {"status": "added", "provider": "openrouter"}
    with pytest.raises(Exception):
        ad.add_provider_key("openai-codex", "or-key-12345678")  # not a key provider
    with pytest.raises(Exception):
        ad.add_provider_key("unknown", "or-key-12345678")


def test_label_is_validated_and_passed_to_hermes(monkeypatch):
    assert login.clean_label("  Mon Codex ") == "Mon Codex"
    assert login.clean_label("") is None and login.clean_label(None) is None
    for bad in ("--api-key x", "a\nb", "x" * 41, "-n"):
        with pytest.raises(login.LoginError):
            login.clean_label(bad)
    seen = {}

    class R:
        returncode, stdout = 0, 'Added openrouter credential #1: "x"\n'

    def fake_run(argv, **k):
        seen["argv"] = argv
        return R()
    monkeypatch.setattr(login.subprocess, "run", fake_run)
    login.add_key("openrouter", "or-key-12345678", "Perso OR")
    assert seen["argv"][-2:] == ["--label", "Perso OR"]
    with pytest.raises(login.LoginError):
        login.add_key("openrouter", "or-key-12345678", "--oops")


def test_display_name_roundtrip(tmp_path, monkeypatch):
    from cmi_console import account_prefs as prefs
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(prefs, "_home", lambda: tmp_path)
    assert prefs.set_name("pro2", "  Mon compte client ") == {"pro2": "Mon compte client"}
    assert prefs.load_names() == {"pro2": "Mon compte client"}
    for bad in ("x" * 41, "a\nb"):
        with pytest.raises(prefs.PrefsError):
            prefs.set_name("pro", bad)
    with pytest.raises(prefs.PrefsError):
        prefs.set_name("nope", "x")
    assert prefs.set_name("pro2", "") == {}


def test_hermes_argv_never_depends_on_pythonpath():
    argv = login._hermes_argv("auth", "add", "anthropic", "--type", "oauth")
    assert argv[-5:] == ["auth", "add", "anthropic", "--type", "oauth"]
    assert argv[0].endswith("hermes") or argv[1] == "-c"


def test_open_in_browser_only_opens_the_link_hermes_printed(monkeypatch):
    import webbrowser
    opened = []
    monkeypatch.setattr(webbrowser, "open", lambda u: opened.append(u) or True)
    monkeypatch.setattr(login, "_cur", {"id": "abc", "url": "https://claude.ai/oauth/authorize?x=1", "proc": None, "at": 0, "buf": []})
    with pytest.raises(login.LoginError):
        login.open_in_browser("other")
    assert login.open_in_browser("abc") is True and opened == ["https://claude.ai/oauth/authorize?x=1"]
    monkeypatch.setattr(login, "_cur", {"id": "abc", "url": "file:///etc/passwd", "proc": None, "at": 0, "buf": []})
    with pytest.raises(login.LoginError):
        login.open_in_browser("abc")


def test_last_line_gives_the_reason_without_links():
    buf = ["Authorization code: Anthropic OAuth login did not return credentials.\n", "  https://x.y/z\n"]
    assert login._last_line(buf) == "Anthropic OAuth login did not return credentials."
