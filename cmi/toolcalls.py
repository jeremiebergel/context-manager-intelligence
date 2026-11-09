"""Tool-call classification and argument parsing, shared by the engine passes.

Pure functions over tool names, arguments and shell commands: which tools read, write or run
shell; which paths a shell command writes; stable command keys and labels. No engine state.
"""
from __future__ import annotations

import json
import os
import re
from typing import Any, List

from .compat import _summarize_tool_result


# Tool-call ids are [A-Za-z0-9_-] tokens on every provider Hermes speaks to; tokenizing each
# message once and intersecting with the known-id set replaces an O(messages x ids) substring scan.
_ID_TOKEN_RE = re.compile(r"[A-Za-z0-9_\-]+")

# ---- Tunable budget knobs -----------------------------------------------
CMI_L1_MIN_PCT: float = 0.15
CMI_L1_MAX_PCT: float = 0.40
CMI_L2_MAX_PCT: float = 0.03
CMI_L3_MAX_PCT: float = 0.08
USER_TURN_WINDOW: int = 5

# Digest wrapper overhead: "[CMI digest] " / "[CMI one_liner] " prefix length.
# Used as the profitability floor so a digest never grows small content.
# Cache tiers (v1.4). Costs relative to an uncached input token (Anthropic: read 0.1, write 1.25).
CACHE_READ_COST: float = 0.1
CACHE_WRITE_COST: float = 1.25
# Requests over which a re-tier must pay back its one-time cache rewrite (~ one user turn).
# Provider prompt-cache lifetime (Anthropic default 5 min; hosts may request 1 h). After a longer
# gap nothing is cached, for the raw request as much as for CMI's: a re-tier is free then.
CACHE_TTL_S: float = float(os.environ.get("CMI_CACHE_TTL", "300"))
CACHE_TTL_MARGIN_S: float = 30.0
TIER_PAYBACK_REQUESTS: int = 4  # replay of 53 real sessions: 4 beat 10, 25 and 60 (-28.3% vs -27.4% held-out)
# Step aging (v1.10): inside one user turn, every STEP_EPOCH agent steps (assistant messages) count
# as one more turn of age. A single-prompt autonomous run (kanban worker, Claude Code, Codex) used to
# keep every result raw for its whole life because nothing aged without a new user message. Ages
# only move at epoch boundaries, so decisions (and the prompt cache) change in batches, never on
# every step. 0 disables it (ages by user turns only, the pre-1.10 behaviour).
STEP_EPOCH: int = int(os.environ.get("CMI_STEP_EPOCH", "8"))

# A digest must be at most this fraction of the original, else the result stays raw.
DIGEST_MAX_KEEP_RATIO: float = 0.5

_DIGEST_PREFIX_MAX_LEN = len("[CMI one_liner] cmi:0123456789ab ")


_KEY_ARGS = ("path", "file", "file_path", "command", "url", "query", "pattern")


def _summarize_with_key_arg(tool_name: str, tool_args: str, content: str) -> str:
    """Core one-liner, led by the call's identifying argument in full: the generic summarizer
    keeps 2 args cut at 40 chars (often `limit`/`offset`), which drops the file path — the one
    thing the model needs to find its way back to the content."""
    base = _summarize_tool_result(tool_name, tool_args, content)
    try:
        args = json.loads(tool_args) if tool_args else {}
    except Exception:
        args = {}
    if isinstance(args, dict):
        for k in _KEY_ARGS:
            v = args.get(k)
            if isinstance(v, str) and v and v not in base:
                return f"[{tool_name}] {k}={v[:240]} | {base}"
    return base


# Supersession (v1.3): an old file read is obsolete once the same file was modified or read again.
_READ_TOOLS = {"read_file", "read", "view", "view_file", "open_file", "cat", "readfile", "read_text_file"}
# Exact names only: substring hints marked reads outdated after read-only tools like
# `overwrite_check` or `preview_edit`.
_WRITE_TOOLS = {
    "write_file", "write", "patch", "apply_patch", "apply_diff", "edit", "edit_file", "multi_edit",
    "multiedit", "str_replace", "str_replace_editor", "str_replace_based_edit_tool", "create_file",
    "replace", "replace_in_file", "search_replace", "write_to_file", "insert",
}
_EXIT_CODE_RE = re.compile(r'"exit_code"\s*:\s*(-?\d+)')
_SHELL_TOOLS = {"terminal", "bash", "shell", "run_command", "execute_command", "run_terminal_cmd", "exec",
                "exec_command", "local_shell", "run_shell_command", "shell_command"}
_FAILED_RESULT_RE = re.compile(
    r'^\s*(?:\{\s*"success"\s*:\s*false|\{\s*"error"\s*:|error\b|failed\b)', re.IGNORECASE)
_PATH_ARG_KEYS = ("path", "file_path", "file", "filename", "target_file")
SUPERSEDED_NOTE = {
    "modified": "(outdated: this file was modified later; re-read it for the current content)",
    "reread": "(superseded: this file was read again later)",
    "rerun": "(superseded: the same command was run again later; its newer output is further down)",
}
# Tools whose output is a snapshot of state: running the same call again makes the old output stale.
_SNAPSHOT_TOOLS = _SHELL_TOOLS | {"search_files", "grep", "glob", "list_files", "list_dir", "ls",
                                  "web_extract", "web_fetch", "fetch", "browser_snapshot", "process"}
_PIPE_TAIL_RE = re.compile(r"\s*(?:2>&1\s*)?\|\s*(?:tail|head)(?:\s+-n)?(?:\s+-?\d+)?\s*$")


def command_key(cmd: str) -> str:
    """Same run for supersession: whitespace collapsed, a trailing `| tail -N` / `| head -N`
    (only the view of the output differs) and `2>&1` dropped."""
    c = " ".join((cmd or "").split())
    prev = None
    while prev != c:
        prev = c
        c = _PIPE_TAIL_RE.sub("", c).strip()
        c = re.sub(r"\s+2>&1$", "", c)
    return c


def _bare_tool_name(name: str) -> str:
    n = (name or "").lower()
    return n.split("__")[-1] if "__" in n else n


def _path_arg(args: Any) -> str:
    if isinstance(args, dict):
        for k in _PATH_ARG_KEYS:
            v = args.get(k)
            if isinstance(v, str) and v.strip():
                return v.strip()
    return ""


_SHELLS = {"bash", "sh", "zsh", "/bin/bash", "/bin/sh", "/bin/zsh", "/usr/bin/bash", "/usr/bin/env"}
_PATCH_FILE_RE = re.compile(r"^\*\*\* (?:Update|Add|Delete) File: (.+?)\s*$|^\*\*\* Move to: (.+?)\s*$", re.M)


def _command_arg(args: Any) -> str:
    """The shell command of a call, as one string. Codex sends argv lists (["bash", "-lc", "..."])."""
    if not isinstance(args, dict):
        return ""
    v = args.get("command") or args.get("cmd") or ""
    if isinstance(v, list) and all(isinstance(x, str) for x in v):
        if len(v) >= 3 and v[0] in _SHELLS and v[-2] in ("-c", "-lc", "-ic"):
            return v[-1]
        return " ".join(v)
    return v if isinstance(v, str) else ""


def _write_paths(args: Any) -> List[str]:
    """Files a write/patch call targets: its path argument, or the headers of a patch envelope
    (Codex apply_patch: '*** Update File: src/a.py')."""
    p = _path_arg(args)
    if p:
        return [p]
    if isinstance(args, dict):
        for k in ("input", "patch", "diff", "content"):
            v = args.get(k)
            if isinstance(v, str) and "*** " in v:
                return [a or b for a, b in _PATCH_FILE_RE.findall(v)]
    return []


def _fingerprint(x: Any) -> int:
    """Content hash of a JSON-shaped message, stable across structural clones. Leaf strings are
    shared by clones and cache their own hash, so this costs one pass over the dict tree."""
    if isinstance(x, dict):
        return hash(tuple((k, _fingerprint(v)) for k, v in x.items()))
    if isinstance(x, list):
        return hash(tuple(_fingerprint(v) for v in x))
    try:
        return hash(x)
    except TypeError:
        return hash(repr(x))


def _clone(x: Any) -> Any:
    if isinstance(x, dict):
        return {k: _clone(v) for k, v in x.items()}
    if isinstance(x, list):
        return [_clone(v) for v in x]
    return x


_SHELL_SEP = {"&&", "||", ";", "|", "&", "\n"}
_REDIRECT_RE = re.compile(r"^(?:\d?>>?|&>>?)(.*)$")


def shell_written_paths(command: str) -> List[str]:
    """Files a shell command writes in place: sed/perl -i, > and >> redirections, tee, mv/cp
    targets (and mv sources, which disappear), rm, truncate, git checkout/restore -- <files>.
    Deliberately conservative: an unparsable command yields nothing (the read then stays
    current, which is what happened before this existed)."""
    import shlex
    try:
        lex = shlex.shlex(command, posix=True, punctuation_chars=";&|")
        lex.whitespace_split = True
        toks = list(lex)
    except ValueError:
        return []
    out: List[str] = []
    cmds, cur = [], []
    for t in toks:
        if t in _SHELL_SEP or (t and set(t) <= set(";&|")):
            if cur:
                cmds.append(cur)
            cur = []
        else:
            cur.append(t)
    if cur:
        cmds.append(cur)
    for c in cmds:
        # redirections anywhere in the command
        rest = []
        k = 0
        while k < len(c):
            m = _REDIRECT_RE.match(c[k])
            if m and not c[k].startswith(("2>", "2>>")):
                target = m.group(1) or (c[k + 1] if k + 1 < len(c) else "")
                if not m.group(1):
                    k += 1
                if target and target not in ("/dev/null", "&1", "&2") and not target.startswith("&"):
                    out.append(target)
            elif m:
                if not m.group(1):
                    k += 1
            else:
                rest.append(c[k])
            k += 1
        while rest and (rest[0] in ("sudo", "env", "command") or ("=" in rest[0] and not rest[0].startswith("-"))):
            rest = rest[1:]
        if not rest:
            continue
        prog, args = rest[0].rsplit("/", 1)[-1], rest[1:]
        plain = [a for a in args if not a.startswith("-")]
        in_place = any(a.startswith("-i") or (prog == "perl" and a.startswith("-") and not a.startswith("--") and "i" in a[1:])
                       for a in args)
        if prog in ("sed", "gsed", "perl") and in_place:
            # first non-option is the script (unless given via -e), the rest are files
            has_e = any(a == "-e" or a.startswith("-e") for a in args)
            files, skip_next = [], False
            for a in args:
                if skip_next:
                    skip_next = False
                    continue
                if a in ("-e", "-f"):
                    skip_next = True
                    continue
                if a.startswith("-") or a == "":
                    continue
                files.append(a)
            out += files if has_e else files[1:]
        elif prog == "tee":
            out += plain
        elif prog in ("mv", "rm", "unlink", "truncate", "shred"):
            out += plain
        elif prog in ("cp", "install", "rsync", "ln") and len(plain) >= 2:
            out.append(plain[-1])
        elif prog == "git" and args[:1] and args[0] in ("checkout", "restore") and "--" in args:
            out += args[args.index("--") + 1:]
    return [o for o in out if o and not o.startswith("$") and "*" not in o]


def _call_label(name: str, args: Any) -> str:
    """One line saying what produced an output: `terminal: pytest -q`, `read_file: src/app.ts`."""
    try:
        a = json.loads(args) if isinstance(args, str) else (args or {})
    except ValueError:
        a = {}
    if not isinstance(a, dict):
        a = {}
    detail = (a.get("command") or a.get("cmd") or _path_arg(a) or a.get("url") or a.get("query")
              or a.get("pattern") or "")
    if not detail and a:
        detail = json.dumps(a, ensure_ascii=False)[:200]
    return f"{_bare_tool_name(name) or 'tool'}: {' '.join(str(detail).split())[:250]}"
