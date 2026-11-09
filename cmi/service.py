"""`cmi-proxy install-service | uninstall-service | status` (macOS).

Keeps cmi-proxy running in the background and points the editors at it, reversibly:
  - a per-user LaunchAgent (no admin rights, loopback only, low priority, restarts on crash);
  - VS Code `claudeCode.environmentVariables` (Claude Code extension) and a `[model_providers.cmi]`
    block in ~/.codex/config.toml (Codex), both marked `cmi-managed`, backed up before any edit.
Nothing is overwritten: an existing setting of the same name is left alone and reported.
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
from typing import List, Optional

LABEL = "com.cmi.proxy"
MARK = "cmi-managed"
CHATGPT_UPSTREAM = "https://chatgpt.com/backend-api/codex"


def _home() -> Path:
    return Path(os.environ.get("CMI_SERVICE_HOME") or Path.home())


def paths(home: Optional[Path] = None) -> dict:
    h = home or _home()
    return {
        "plist": h / "Library" / "LaunchAgents" / f"{LABEL}.plist",
        "vscode": h / "Library" / "Application Support" / "Code" / "User" / "settings.json",
        "codex": h / ".codex" / "config.toml",
        "logs": h / ".cmi" / "logs",
        "backups": h / ".cmi" / "backups",
    }


def _backup(path: Path, backups: Path) -> None:
    if path.exists():
        backups.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, backups / f"{path.name}.{time.strftime('%Y%m%d-%H%M%S')}")


# ---- plist ------------------------------------------------------------------------------------
def build_plist(exe: str, port: int, logs: Path) -> dict:
    return {
        "Label": LABEL,
        "ProgramArguments": [exe, "--host", "127.0.0.1", "--port", str(port), "--openai-upstream", CHATGPT_UPSTREAM],
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


# ---- commands ---------------------------------------------------------------------------------
def _launchctl(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["launchctl", *args], capture_output=True, text=True)


def health(port: int, timeout: float = 2.0) -> Optional[dict]:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/cmi-health", timeout=timeout) as r:
            return json.loads(r.read().decode())
    except Exception:
        return None


def install(port: int = 8787, editors: bool = True, start: bool = True, home: Optional[Path] = None,
            exe: Optional[str] = None) -> List[str]:
    if sys.platform != "darwin" and home is None:
        return ["install-service supports macOS only for now (Linux/Windows: run cmi-proxy yourself)."]
    p = paths(home)
    exe = exe or shutil.which("cmi-proxy") or str(Path(sys.argv[0]).resolve())
    say: List[str] = []
    p["logs"].mkdir(parents=True, exist_ok=True)
    p["plist"].parent.mkdir(parents=True, exist_ok=True)
    _backup(p["plist"], p["backups"])
    p["plist"].write_bytes(plistlib.dumps(build_plist(exe, port, p["logs"])))
    os.chmod(p["plist"], 0o600)
    say.append(f"service file written: {p['plist']}")
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
    if start and home is None:
        uid = str(os.getuid())
        _launchctl("bootout", f"gui/{uid}/{LABEL}")
        r = _launchctl("bootstrap", f"gui/{uid}", str(p["plist"]))
        say.append("service started" if r.returncode == 0 else f"could not start the service: {r.stderr.strip()}")
        time.sleep(2)
        say.append("proxy answers" if health(port) else "proxy does not answer yet: run `cmi-proxy status`")
    say.append("Reload VS Code (Cmd+Shift+P > Developer: Reload Window) and restart Codex.")
    return say


def uninstall(home: Optional[Path] = None, stop: bool = True) -> List[str]:
    p = paths(home)
    say: List[str] = []
    if stop and home is None:
        _launchctl("bootout", f"gui/{os.getuid()}/{LABEL}")
        say.append("service stopped")
    if p["plist"].exists():
        p["plist"].unlink()
        say.append("service file removed")
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


def status(port: int = 8787, home: Optional[Path] = None) -> List[str]:
    p = paths(home)
    h = health(port)
    say = [f"proxy on 127.0.0.1:{port}: " + (f"running ({h.get('sessions', 0)} active conversation(s))" if h else "NOT answering")]
    say.append(f"service file: {'present' if p['plist'].exists() else 'absent'}")
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
        out = uninstall()
    else:
        out = status(port=port)
    print("\n".join(out))
    return 0
