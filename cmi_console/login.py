"""Runs Hermes' own ``hermes auth add anthropic --type oauth`` for the Control Center button.

The plugin never performs the OAuth exchange and never sees a token: Hermes' code does (PKCE
verifier, token exchange, pool write). This module only

- starts that command as a child process and reads the authorization link it prints;
- forwards the one-time authorization code the user pastes (single use, bound to the child's PKCE
  verifier, not a stored credential) to the child's stdin;
- reports success or failure from the exit status.

One login at a time, bounded lifetime, the child's output is never returned or logged except the
authorization link. Nothing here touches the credential store.
"""
from __future__ import annotations

import atexit
import os
import re
import secrets
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

URL_RE = re.compile(r"https://claude\.ai/oauth/authorize\?[\x21-\x7e]+")
# Authorization codes look like "<code>#<state>": URL-safe characters only.
CODE_RE = re.compile(r"^[A-Za-z0-9._~#\-]{8,600}$")
ADDED_MARK = "Added anthropic OAuth credential"
ADDED_RE = re.compile(r"^Added \S+ (?:OAuth )?credential", re.M)  # any provider: "Added <slug> [OAuth ]credential #n"
ANY_URL_RE = re.compile(r"https://[\x21-\x7e]+")
USER_CODE_RE = re.compile(r"\b[A-Z0-9]{4}-[A-Z0-9]{4,6}\b")  # device-code flows print e.g. Z8J4-H8III
SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
LABEL_RE = re.compile(r"^[^\x00-\x1f\x7f]{1,40}$")  # a display name: printable, short, never a flag
KEY_RE = re.compile(r"^[\x21-\x7e]{8,512}$")
# How Hermes signs each provider in: "code" = paste the code the page shows (Claude);
# "link" = open a link (+ maybe type a code), Hermes finishes by itself; anything else = API key.
OAUTH_LINK = {"nous", "openai-codex", "xai-oauth", "qwen-oauth", "minimax-oauth"}
START_TIMEOUT_S = 12.0
EXCHANGE_TIMEOUT_S = 25.0
SESSION_TTL_S = 600.0
_MAX_LINES = 200

_lock = threading.Lock()
_cur: Optional[Dict[str, Any]] = None


class LoginError(ValueError):
    pass


def _hermes_argv(*args: str) -> List[str]:
    """Command running ``hermes <args>``. The dashboard may run under an isolated interpreter (``-I``)
    that cannot import ``hermes_cli`` by itself, so prefer the agent's own ``hermes`` launcher; otherwise
    reuse this process' interpreter with its import path (see ``_child_env``)."""
    try:
        import hermes_cli  # type: ignore

        root = Path(hermes_cli.__file__).resolve().parent.parent
        for cand in (root / "venv" / "bin" / "hermes", root / "venv" / "Scripts" / "hermes.exe"):
            if cand.is_file():
                return [str(cand), *args]
    except Exception:
        pass
    prog = "import sys; sys.argv = ['hermes'] + sys.argv[1:]; from hermes_cli.main import main; main()"
    return [sys.executable, "-c", prog, *args]


def _child_argv() -> List[str]:
    return _hermes_argv("auth", "add", "anthropic", "--type", "oauth")


def clean_label(label: Optional[str]) -> Optional[str]:
    """The optional account name, or None. Rejects control characters and anything longer than 40."""
    label = (label or "").strip()
    if not label:
        return None
    if not LABEL_RE.match(label) or label.startswith("-"):
        raise LoginError("invalid name")
    return label


def _label_args(label: Optional[str]) -> List[str]:
    label = clean_label(label)
    return ["--label", label] if label else []


def flow_for(provider: str) -> str:
    return "code" if provider == "anthropic" else "link" if provider in OAUTH_LINK else "key"


def _provider_argv(provider: str, *extra: str) -> List[str]:
    return _hermes_argv("auth", "add", provider, *extra)


def _child_env(open_browser: bool = False) -> Dict[str, str]:
    # Claude: BROWSER=true so Hermes does not open a tab by itself (the UI shows the link, the user may want a
    # private window). Other providers: Hermes opens the sign-in page itself, like the terminal command.
    env = dict(os.environ, PYTHONUNBUFFERED="1")
    if not open_browser:
        env["BROWSER"] = "true"
    try:
        import hermes_cli

        root = os.path.dirname(os.path.dirname(os.path.abspath(hermes_cli.__file__)))
        env["PYTHONPATH"] = root + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
    except Exception:
        pass
    return env


def _pump(proc: subprocess.Popen, buf: List[str]) -> None:
    try:
        for line in proc.stdout:  # type: ignore[union-attr]
            if len(buf) < _MAX_LINES:
                buf.append(line[:2000])
    except (OSError, ValueError):
        pass


def _kill_locked() -> None:
    global _cur
    cur, _cur = _cur, None
    if not cur:
        return
    proc = cur["proc"]
    try:
        if proc.poll() is None:
            proc.kill()
        proc.wait(timeout=3)
    except Exception:
        pass
    for f in (proc.stdin, proc.stdout):
        try:
            f and f.close()
        except Exception:
            pass


def _launch(argv: List[str], url_re, with_code: bool = False, open_browser: bool = False) -> Dict[str, Any]:
    global _cur
    with _lock:
        _kill_locked()
        try:
            proc = subprocess.Popen(
                argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                env=_child_env(open_browser), cwd=os.path.expanduser("~"), text=True, bufsize=1, start_new_session=True)
        except OSError as exc:
            raise LoginError("cannot start the Hermes login") from exc
        buf: List[str] = []
        threading.Thread(target=_pump, args=(proc, buf), daemon=True).start()
        _cur = {"id": secrets.token_hex(8), "proc": proc, "at": time.time(), "buf": buf, "url": None}
        deadline = time.time() + START_TIMEOUT_S
        url = None
        while time.time() < deadline and proc.poll() is None:
            m = url_re.search("".join(buf))
            if m:
                url = m.group(0)
                break
            time.sleep(0.1)
        if not url:
            _kill_locked()
            raise LoginError("Hermes did not print an authorization link")
        _cur["url"] = url
        out: Dict[str, Any] = {"id": _cur["id"], "url": url}
        if with_code:  # device-code flows print the code to type right after the link
            time.sleep(0.4)
            m = USER_CODE_RE.search("".join(buf)) or USER_CODE_RE.search(url)
            if m:
                out["user_code"] = m.group(0)
        return out


def start() -> Dict[str, Any]:
    """Launch the Claude login; return ``{"id", "url"}`` (the link the user opens in a browser)."""
    return _launch(_child_argv(), URL_RE)


def start_link(provider: str, label: Optional[str] = None) -> Dict[str, Any]:
    """Launch another provider's OAuth login (device code / link); Hermes completes it by itself.

    Hermes opens the sign-in page in the browser of this machine, exactly like the terminal command
    (the UI has no ``window.open`` by design); the link and any code are still returned as a fallback.
    """
    if not SLUG_RE.match(provider or "") or flow_for(provider) != "link":
        raise LoginError("unsupported provider")
    extra = _label_args(label)
    return _launch(_provider_argv(provider, "--type", "oauth", *extra), ANY_URL_RE, with_code=True, open_browser=True)


def status(sid: str) -> Dict[str, Any]:
    """Progress of a link login: ``waiting`` | ``done`` | ``failed`` | ``expired``."""
    global _cur
    with _lock:
        cur = _cur
        if not cur or cur["id"] != sid:
            raise LoginError("no login in progress")
        if time.time() - cur["at"] > SESSION_TTL_S:
            _kill_locked()
            return {"state": "expired"}
        rc = cur["proc"].poll()
        if rc is None:
            return {"state": "waiting"}
        time.sleep(0.2)
        ok = rc == 0 and bool(ADDED_RE.search("".join(cur["buf"])))
        _cur = None
        return {"state": "done" if ok else "failed"}


def add_key(provider: str, key: str, label: Optional[str] = None) -> Dict[str, Any]:
    """Hand an API key to ``hermes auth add <provider> --type api-key``; the output is never returned."""
    global _cur
    key = (key or "").strip()
    if not SLUG_RE.match(provider or "") or flow_for(provider) != "key":
        raise LoginError("unsupported provider")
    if not KEY_RE.match(key):
        raise LoginError("invalid key")
    extra = _label_args(label)
    with _lock:
        _kill_locked()
        try:
            r = subprocess.run(_provider_argv(provider, "--type", "api-key", "--api-key", key, *extra), capture_output=True,
                               text=True, env=_child_env(), cwd=os.path.expanduser("~"),
                               stdin=subprocess.DEVNULL, timeout=15)
        except (OSError, subprocess.TimeoutExpired):
            return {"ok": False, "reason": "failed"}
        ok = r.returncode == 0 and bool(ADDED_RE.search(r.stdout or ""))
        return {"ok": ok} if ok else {"ok": False, "reason": "rejected"}


def _last_line(buf: List[str]) -> str:
    """Hermes' last human-readable line (why a login failed). Never a link or a long token."""
    for line in reversed(buf):
        t = re.sub(r"^.*Authorization code:\s*", "", line).strip()
        if t and "http" not in t and len(t) < 200:
            return t[:160]
    return ""


def open_in_browser(sid: str) -> bool:
    """Open the link of the login in progress in the default browser of this machine (the backend runs
    locally, like the terminal command). Only the link Hermes itself printed for *sid* is ever opened."""
    import webbrowser

    with _lock:
        cur = _cur
        url = cur.get("url") if cur and cur["id"] == sid else None
    if not url or not ANY_URL_RE.fullmatch(url) or not url.startswith("https://"):
        raise LoginError("no login in progress")
    try:
        return bool(webbrowser.open(url))
    except Exception:
        return False


def submit(sid: str, code: str) -> Dict[str, Any]:
    """Give the pasted code to the running login; ``{"ok": True}`` only if Hermes added the credential."""
    global _cur
    code = (code or "").strip()
    if not CODE_RE.match(code):
        raise LoginError("invalid code")
    with _lock:
        cur = _cur
        if cur and time.time() - cur["at"] > SESSION_TTL_S:
            _kill_locked()
            cur = None
        if not cur or cur["id"] != sid:  # a stale or forged id must not stop the login in progress
            raise LoginError("no login in progress")
        proc, buf = cur["proc"], cur.get("buf", [])
        try:
            proc.stdin.write(code + "\n")  # type: ignore[union-attr]
            proc.stdin.flush()  # type: ignore[union-attr]
            proc.stdin.close()  # type: ignore[union-attr]
            rc = proc.wait(timeout=EXCHANGE_TIMEOUT_S)
        except subprocess.TimeoutExpired:
            _kill_locked()
            return {"ok": False, "reason": "timeout"}
        except OSError:
            _kill_locked()
            return {"ok": False, "reason": "failed"}
        time.sleep(0.2)  # let the reader thread drain the last line
        ok = rc == 0 and any(ADDED_MARK in line for line in buf)
        _cur = None
        return {"ok": ok} if ok else {"ok": False, "reason": "rejected", "detail": _last_line(buf)}


def cancel() -> None:
    with _lock:
        _kill_locked()


atexit.register(cancel)
