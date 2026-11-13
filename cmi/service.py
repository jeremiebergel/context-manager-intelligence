"""`cmi-proxy install-service | uninstall-service | status` (macOS, Linux, Windows).

Keeps cmi-proxy running in the background and points the editors at it, reversibly, without admin rights:
  - macOS: a per-user LaunchAgent (loopback only, low priority, restarts on crash);
  - Linux: a systemd user unit (`systemctl --user`, restarts on failure, logs in the user journal);
  - Windows: a per-user Scheduled Task started at logon (`schtasks /SC ONLOGON`), or, when Windows
    refuses to create it, a `.cmd` in the user's Startup folder;
  - VS Code `claudeCode.environmentVariables` (Claude Code extension) and a `[model_providers.cmi]`
    block in ~/.codex/config.toml (Codex), both marked `cmi-managed`, backed up before any edit.
Nothing is overwritten: an existing setting of the same name is left alone and reported.

File contents are built by pure functions (build_plist, build_systemd_unit, build_task_command,
build_startup_cmd) so they are testable on any OS. Every external command goes through `_run` with a
list of arguments (never a shell).
"""
from __future__ import annotations

import json
import os
import plistlib
import re
import shutil
import subprocess
import sys
import time
import urllib.request
from pathlib import Path
from typing import List, Optional, Sequence, Union

from . import _fs

LABEL = "com.cmi.proxy"
UNIT = "cmi-proxy.service"
TASK = "cmi-proxy"
MARK = "cmi-managed"
CHATGPT_UPSTREAM = "https://chatgpt.com/backend-api/codex"
TR_MAX = 261  # schtasks refuses a longer /TR value


def _home() -> Path:
    return Path(os.environ.get("CMI_SERVICE_HOME") or Path.home())


def _platform(plat: Optional[str] = None) -> str:
    p = plat or sys.platform
    if p.startswith("linux"):
        return "linux"
    if p in ("win32", "cygwin", "windows"):
        return "win32"
    return p


def paths(home: Optional[Path] = None, plat: Optional[str] = None) -> dict:
    """Per-OS locations. With an explicit `home` everything lives under it (tests, dry runs);
    without one, XDG_CONFIG_HOME (Linux) and APPDATA (Windows) are honoured."""
    h = home or _home()
    plat = _platform(plat)
    xdg = Path(os.environ["XDG_CONFIG_HOME"]) if home is None and os.environ.get("XDG_CONFIG_HOME") else h / ".config"
    appdata = Path(os.environ["APPDATA"]) if home is None and os.environ.get("APPDATA") else h / "AppData" / "Roaming"
    vscode = {
        "darwin": h / "Library" / "Application Support" / "Code" / "User" / "settings.json",
        "linux": xdg / "Code" / "User" / "settings.json",
        "win32": appdata / "Code" / "User" / "settings.json",
    }.get(plat, xdg / "Code" / "User" / "settings.json")
    p = {
        "plist": h / "Library" / "LaunchAgents" / f"{LABEL}.plist",
        "unit": xdg / "systemd" / "user" / UNIT,
        "startup": appdata / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup" / f"{TASK}.cmd",
        "vscode": vscode,
        "codex": h / ".codex" / "config.toml",
        "logs": h / ".cmi" / "logs",
        "backups": h / ".cmi" / "backups",
    }
    p["service"] = {"darwin": p["plist"], "linux": p["unit"], "win32": p["startup"]}.get(plat, p["unit"])
    return p


def _private_dir(path: Path) -> None:
    """Owner-only directory: backups are copies of editor and agent settings (which may hold
    keys), logs name sessions and local paths."""
    path.mkdir(parents=True, exist_ok=True)
    try:
        _fs.chmod_private(path, 0o700)
    except OSError:
        pass


def _backup(path: Path, backups: Path) -> None:
    if path.exists():
        _private_dir(backups)
        copy = backups / f"{path.name}.{time.strftime('%Y%m%d-%H%M%S')}"
        shutil.copy2(path, copy)
        try:
            _fs.chmod_private(copy, 0o600)
        except OSError:
            pass


def _as_list(exe: Union[str, Sequence[str]]) -> List[str]:
    return [exe] if isinstance(exe, str) else list(exe)


def proxy_args(port: int) -> List[str]:
    return ["--host", "127.0.0.1", "--port", str(port), "--openai-upstream", CHATGPT_UPSTREAM]


def default_command(plat: Optional[str] = None) -> List[str]:
    """The command that starts the proxy: the running `cmi-proxy` script, else the one on PATH, else
    `python -m cmi.proxy` with this interpreter. On Windows `pythonw -m cmi.proxy` is preferred: it opens
    no console window."""
    plat = _platform(plat)
    if plat == "win32":
        pyw = Path(sys.executable).with_name("pythonw.exe")
        if pyw.exists():
            return [str(pyw), "-m", "cmi.proxy"]
    argv0 = Path(sys.argv[0]) if sys.argv and sys.argv[0] else None
    if argv0 and argv0.name.lower().startswith("cmi-proxy") and argv0.is_file():
        return [str(argv0.resolve())]
    found = shutil.which("cmi-proxy")
    if found:
        return [os.path.abspath(found)]
    return [sys.executable, "-m", "cmi.proxy"]


# ---- macOS: plist ---------------------------------------------------------------------------
def build_plist(exe: Union[str, Sequence[str]], port: int, logs: Path) -> dict:
    return {
        "Label": LABEL,
        "ProgramArguments": _as_list(exe) + proxy_args(port),
        "RunAtLoad": True,
        "KeepAlive": {"SuccessfulExit": False},
        "ThrottleInterval": 10,
        "ProcessType": "Background",
        "LowPriorityIO": True,
        "Nice": 10,
        "SoftResourceLimits": {"NumberOfFiles": 256},
        "StandardOutPath": str(logs / "proxy.log"),
        "StandardErrorPath": str(logs / "proxy.err"),
    }


# ---- Linux: systemd user unit ---------------------------------------------------------------
def _systemd_quote(arg: str) -> str:
    """One ExecStart word: `%` and `$` are systemd specifiers/variables, quotes keep spaces."""
    a = arg.replace("%", "%%").replace("$", "$$")
    if a and not re.search(r'[\s"\'\\;]', a):
        return a
    return '"' + a.replace("\\", "\\\\").replace('"', '\\"') + '"'


def build_systemd_unit(exe: Union[str, Sequence[str]], port: int) -> str:
    cmd = " ".join(_systemd_quote(a) for a in _as_list(exe) + proxy_args(port))
    return (
        f"# {MARK}: written by `cmi-proxy install-service`, removed by `cmi-proxy uninstall-service`\n"
        "[Unit]\n"
        "Description=CMI context proxy (loopback only)\n"
        "\n"
        "[Service]\n"
        "Type=simple\n"
        f"ExecStart={cmd}\n"
        "Environment=PYTHONUNBUFFERED=1\n"
        "Restart=on-failure\n"
        "RestartSec=10\n"
        "Nice=10\n"
        "LimitNOFILE=1024\n"
        "\n"
        "[Install]\n"
        "WantedBy=default.target\n"
    )


# ---- Windows: Scheduled Task command / Startup-folder .cmd ----------------------------------
def _win_quote(arg: str) -> str:
    if '"' in arg:
        raise ValueError(f"cannot quote an argument holding a double quote: {arg!r}")
    return f'"{arg}"' if (not arg or re.search(r"[\s&()^|<>,;=]", arg)) else arg


def build_task_command(exe: Union[str, Sequence[str]], port: int) -> str:
    """The /TR value of `schtasks /Create`: passed as ONE list argument, the task stores it verbatim."""
    return " ".join(_win_quote(a) for a in _as_list(exe) + proxy_args(port))


def build_startup_cmd(exe: Union[str, Sequence[str]], port: int) -> str:
    """Fallback when schtasks is refused: a Startup-folder script that launches the proxy detached."""
    cmd = build_task_command(exe, port).replace("%", "%%")
    return (f"@echo off\r\nrem {MARK}: starts cmi-proxy at logon; remove with `cmi-proxy uninstall-service`\r\n"
            f'start "" /B {cmd}\r\n')


# ---- VS Code (JSON with comments/trailing commas: edited as text, never re-serialized) --------
def vscode_line(port: int) -> str:
    return (f'    "claudeCode.environmentVariables": [{{ "name": "ANTHROPIC_BASE_URL", '
            f'"value": "http://127.0.0.1:{port}" }}],  // {MARK}\n')


def vscode_add(text: str, port: int) -> tuple[str, str]:
    """-> (new text, status) with status in added | present | foreign."""
    if MARK in text:
        return text, "present"
    if "claudeCode.environmentVariables" in text:
        # the user may have written the same setting by hand: that already points at us
        return text, ("present" if f"127.0.0.1:{port}" in text and "ANTHROPIC_BASE_URL" in text else "foreign")
    if not text.strip():
        text = "{\n}\n"
    end = text.rindex("}")
    head = text[:end].rstrip()
    rest = text[len(head):]  # original whitespace before the closing brace, kept as is
    if not head.endswith(("{", ",")):
        head += ","
    return head + "\n" + vscode_line(port).rstrip("\n") + rest, "added"


def vscode_remove(text: str) -> tuple[str, bool]:
    lines = text.splitlines(keepends=True)
    kept = [ln for ln in lines if MARK not in ln]
    return "".join(kept), len(kept) != len(lines)


# ---- Codex config.toml ------------------------------------------------------------------------
def codex_add(text: str, port: int) -> tuple[str, str]:
    if MARK in text:
        return text, "present"
    if re.search(r"^\s*model_provider\s*=", text, re.M) or "[model_providers.cmi]" in text:
        return text, "foreign"
    top = f'model_provider = "cmi"  # {MARK}\n'
    block = (f"\n# {MARK} begin\n[model_providers.cmi]\nname = \"cmi\"\nbase_url = \"http://127.0.0.1:{port}\"\n"
             f"wire_api = \"responses\"\nrequires_openai_auth = true\n# {MARK} end\n")
    m = re.search(r"^\[", text, re.M)  # top-level keys must come before the first [table]
    i = m.start() if m else len(text)
    head = text[:i]
    if head and not head.endswith("\n"):
        head += "\n"
    tail = text[i:]
    if tail and not tail.endswith("\n"):
        tail += "\n"
    return head + top + tail + block, "added"


def codex_remove(text: str) -> tuple[str, bool]:
    out, skip, changed = [], False, False
    for ln in text.splitlines(keepends=True):
        if f"{MARK} begin" in ln:
            skip = changed = True
            if out and not out[-1].strip():
                out.pop()  # the blank line we put before our block
            continue
        if skip:
            if f"{MARK} end" in ln:
                skip = False
            continue
        if MARK in ln:
            changed = True
            continue
        out.append(ln)
    return "".join(out), changed


# ---- external commands (list args only; tests replace _run / _spawn) ----------------------------
def _run(cmd: List[str], timeout: float = 60) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as e:  # command missing, not executable, hung
        return subprocess.CompletedProcess(cmd, 127, "", str(e))


def _spawn(cmd: List[str]) -> None:
    """Start the proxy detached from this console (Windows fallback path)."""
    flags = 0
    for name in ("DETACHED_PROCESS", "CREATE_NEW_PROCESS_GROUP", "CREATE_NO_WINDOW"):
        flags |= getattr(subprocess, name, 0)
    subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     close_fds=True, creationflags=flags)


def _launchctl(*args: str) -> subprocess.CompletedProcess:
    return _run(["launchctl", *args])


def _systemctl(*args: str) -> subprocess.CompletedProcess:
    return _run(["systemctl", "--user", *args])


def _err(r: subprocess.CompletedProcess) -> str:
    return (r.stderr or r.stdout or f"exit code {r.returncode}").strip()


def health(port: int, timeout: float = 2.0) -> Optional[dict]:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/cmi-health", timeout=timeout) as r:
            return json.loads(r.read().decode())
    except Exception:
        return None


def _wait_health(port: int, seconds: float = 6.0) -> Optional[dict]:
    end = time.monotonic() + seconds
    while True:
        h = health(port, timeout=1.0)
        if h or time.monotonic() >= end:
            return h
        time.sleep(0.5)


def _write_service_file(path: Path, data: Union[str, bytes], backups: Path, mode: int) -> Optional[bytes]:
    """Write `path`, return its previous bytes (None if it did not exist) so a failure can roll back."""
    prev = path.read_bytes() if path.exists() else None
    path.parent.mkdir(parents=True, exist_ok=True)
    _backup(path, backups)
    path.write_bytes(data if isinstance(data, bytes) else data.encode("utf-8"))
    os.chmod(path, mode)
    return prev


def _restore(path: Path, prev: Optional[bytes]) -> None:
    if prev is None:
        path.unlink(missing_ok=True)
    else:
        path.write_bytes(prev)


# ---- per-OS service steps -------------------------------------------------------------------
def _linux_service(p: dict, cmd: List[str], port: int, manage: bool) -> tuple[bool, List[str]]:
    if manage:
        r = _systemctl("show-environment")
        if r.returncode != 0:
            return False, ["ERROR: install-service needs a systemd user session (`systemctl --user` failed: "
                           f"{_err(r)}). Nothing was changed. Without systemd (containers, WSL1), run "
                           "`cmi-proxy` yourself, e.g. from your shell profile."]
    prev = _write_service_file(p["unit"], build_systemd_unit(cmd, port), p["backups"], 0o644)
    say = [f"service file written: {p['unit']}"]
    if not manage:
        return True, say
    for step in (["daemon-reload"], ["enable", UNIT], ["restart", UNIT]):
        r = _systemctl(*step)
        if r.returncode != 0:
            if prev is None:
                _systemctl("disable", "--now", UNIT)
            _restore(p["unit"], prev)
            _systemctl("daemon-reload")
            return False, [f"ERROR: could not start the service (`systemctl --user {' '.join(step)}`: {_err(r)}); "
                           "the service file was rolled back, nothing else was changed."]
    say.append("service enabled and started (systemd user unit; logs: `journalctl --user -u cmi-proxy`)")
    say.append("It starts when you log in; to keep it running without a login session: `loginctl enable-linger`.")
    return True, say


def _windows_service(p: dict, cmd: List[str], port: int, manage: bool) -> tuple[bool, List[str]]:
    if not manage:
        return True, ["service not registered (no service manager in this mode)"]
    tr = build_task_command(cmd, port)
    why = f"the command is longer than {TR_MAX} characters"
    if len(tr) <= TR_MAX:
        r = _run(["schtasks", "/Create", "/SC", "ONLOGON", "/TN", TASK, "/TR", tr, "/RL", "LIMITED", "/F"])
        if r.returncode == 0:
            p["startup"].unlink(missing_ok=True)  # a previous fallback install
            r2 = _run(["schtasks", "/Run", "/TN", TASK])
            return True, [f"scheduled task '{TASK}' created (runs at logon)",
                          "service started" if r2.returncode == 0 else f"could not start the task: {_err(r2)}"]
        why = _err(r)
    _write_service_file(p["startup"], build_startup_cmd(cmd, port), p["backups"], 0o644)
    say = [f"schtasks refused ({why}); fallback: startup script written: {p['startup']}"]
    try:
        _spawn(cmd)
        say.append("service started")
    except OSError as e:
        say.append(f"could not start the proxy now ({e}); it will start at your next logon")
    return True, say


def install(port: int = 8787, editors: bool = True, start: bool = True, home: Optional[Path] = None,
            exe: Optional[Union[str, Sequence[str]]] = None, plat: Optional[str] = None,
            manage: Optional[bool] = None) -> List[str]:
    """`manage`: call the OS service manager (default: only for the real home, never for a test home)."""
    plat = _platform(plat)
    if plat not in ("darwin", "linux", "win32"):
        return [f"ERROR: install-service supports macOS, Linux (systemd) and Windows, not {plat}: run cmi-proxy yourself."]
    manage = (home is None) if manage is None else manage
    manage = manage and start
    p = paths(home, plat)
    cmd = _as_list(exe) if exe else default_command(plat)
    say: List[str] = []
    if plat == "darwin":
        _private_dir(p["logs"])
        p["plist"].parent.mkdir(parents=True, exist_ok=True)
        _backup(p["plist"], p["backups"])
        p["plist"].write_bytes(plistlib.dumps(build_plist(cmd, port, p["logs"])))
        os.chmod(p["plist"], 0o600)
        say.append(f"service file written: {p['plist']}")
    else:
        ok, msgs = (_linux_service if plat == "linux" else _windows_service)(p, cmd, port, manage)
        say += msgs
        if not ok:
            return say  # nothing half-done: editors are only wired once the service runs
    if editors:
        for key, add in (("vscode", vscode_add), ("codex", codex_add)):
            path = p[key]
            if key == "codex" and not path.parent.exists():
                say.append("codex: ~/.codex not found, skipped")
                continue
            if key == "vscode" and not path.parent.exists():
                say.append("vscode: settings folder not found, skipped")
                continue
            old = path.read_text(encoding="utf-8") if path.exists() else ""
            new, status = add(old, port)
            if status == "added":
                _backup(path, p["backups"])
                path.write_text(new, encoding="utf-8")
            say.append(f"{key}: {status}" + (" (an existing setting was left untouched; edit it by hand)" if status == "foreign" else ""))
    if manage and plat == "darwin":
        uid = str(os.getuid())
        _launchctl("bootout", f"gui/{uid}/{LABEL}")
        r = _launchctl("bootstrap", f"gui/{uid}", str(p["plist"]))
        say.append("service started" if r.returncode == 0 else f"could not start the service: {_err(r)}")
    if manage:
        say.append("proxy answers" if _wait_health(port) else "proxy does not answer yet: run `cmi-proxy status`")
    keys = "Cmd+Shift+P" if plat == "darwin" else "Ctrl+Shift+P"
    say.append(f"Reload VS Code ({keys} > Developer: Reload Window) and restart Codex.")
    return say


def uninstall(home: Optional[Path] = None, stop: bool = True, plat: Optional[str] = None,
              manage: Optional[bool] = None, port: int = 8787) -> List[str]:
    plat = _platform(plat)
    manage = ((home is None) if manage is None else manage) and stop
    p = paths(home, plat)
    say: List[str] = []
    if plat == "darwin":
        if manage:
            _launchctl("bootout", f"gui/{os.getuid()}/{LABEL}")
            say.append("service stopped")
        if p["plist"].exists():
            p["plist"].unlink()
            say.append("service file removed")
    elif plat == "linux":
        if manage:
            r = _systemctl("disable", "--now", UNIT)
            say.append("service stopped" if r.returncode == 0 else f"systemctl --user disable: {_err(r)}")
        if p["unit"].exists():
            p["unit"].unlink()
            say.append("service file removed")
        if manage:
            _systemctl("daemon-reload")
    elif plat == "win32":
        if manage:
            q = _run(["schtasks", "/Query", "/TN", TASK])
            if q.returncode == 0:
                _run(["schtasks", "/End", "/TN", TASK])
                r = _run(["schtasks", "/Delete", "/TN", TASK, "/F"])
                say.append("scheduled task removed" if r.returncode == 0 else f"schtasks /Delete: {_err(r)}")
        if p["startup"].exists():
            p["startup"].unlink()
            say.append("startup script removed")
            if manage and health(port):
                say.append("the proxy started by the startup script keeps running until you sign out "
                           "(or end pythonw.exe / cmi-proxy.exe in Task Manager)")
    for key, rem in (("vscode", vscode_remove), ("codex", codex_remove)):
        path = p[key]
        if path.exists():
            new, changed = rem(path.read_text(encoding="utf-8"))
            if changed:
                _backup(path, p["backups"])
                path.write_text(new, encoding="utf-8")
            say.append(f"{key}: " + ("setting removed" if changed else "nothing of ours found"))
    say.append("Reload VS Code and restart Codex.")
    return say


def _version_lines(h: dict, plat: str) -> List[str]:
    """The running proxy keeps the code it started with: after an upgrade it must be restarted."""
    from .versioning import running_version
    installed, running = running_version(), h.get("version")
    if running == installed:
        return [f"version: {running}"]
    if plat == "linux":
        how = f"`systemctl --user restart {UNIT}`"
    elif plat == "win32":
        how = "sign out and back in (the scheduled task starts it at logon)"
    else:
        how = f"`launchctl kickstart -k gui/$(id -u)/{LABEL}`"
    was = running or "a version older than 1.12.4"
    return [f"  WARNING: the proxy still runs {was} but CMI {installed} is installed: restart it to load "
            f"the new code ({how}; open conversations then start a fresh engine)"]


def status(port: int = 8787, home: Optional[Path] = None, plat: Optional[str] = None,
           manage: Optional[bool] = None) -> List[str]:
    plat = _platform(plat)
    manage = (home is None) if manage is None else manage
    p = paths(home, plat)
    h = health(port)
    say = [f"proxy on 127.0.0.1:{port}: " + (f"running ({h.get('sessions', 0)} active conversation(s))" if h else "NOT answering")]
    if h:
        say += _version_lines(h, plat)
    if plat == "win32":
        task = manage and _run(["schtasks", "/Query", "/TN", TASK]).returncode == 0
        say.append("service: " + ("scheduled task present" if task else
                                  "startup script present" if p["startup"].exists() else "absent"))
    else:
        say.append(f"service file: {'present' if p['service'].exists() else 'absent'}")
        if plat == "linux" and manage:
            r = _systemctl("is-active", UNIT)
            say.append(f"systemd user unit: {(r.stdout or '').strip() or _err(r)}")
    for key in ("vscode", "codex"):
        path = p[key]
        txt = path.read_text(encoding="utf-8") if path.exists() else ""
        on = MARK in txt or (key == "vscode" and "ANTHROPIC_BASE_URL" in txt and f"127.0.0.1:{port}" in txt
                             and "claudeCode.environmentVariables" in txt)
        say.append(f"{key} points at the proxy: {'yes' if on else 'no'}")
        if on and not h:
            say.append(f"  WARNING: {key} is configured but the proxy is down: that tool will fail until it is started")
    return say


def run(cmd: str, argv: List[str]) -> int:
    port = 8787
    if "--port" in argv:
        port = int(argv[argv.index("--port") + 1])
    if cmd == "install-service":
        out = install(port=port, editors="--no-editors" not in argv)
    elif cmd == "uninstall-service":
        out = uninstall(port=port)
    else:
        out = status(port=port)
    print("\n".join(out))
    return 1 if any(x.startswith("ERROR:") for x in out) else 0
