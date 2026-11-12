"""Refuse a write whose payload is one of CMI's own stubs (Hermes ``pre_tool_call`` hook).

CMI shows old tool results and old write payloads in short form. A model can copy one of those short
forms into a NEW write, which would put the stub in the file instead of the content (seen live in
1.12.0: a ``[CMI: content omitted ...]`` stub with an invented recall key written as a new file).
Three barriers:
  1. since 1.12.1 a shortened past write carries no payload key, so imitating it fails in the tool
     ("missing content");
  2. this hook blocks a write whose payload starts with a CMI stub, embeds a write stub or the
     ``cmi_omitted`` note, or that carries the ``cmi_omitted`` key, and says how to get the original
     back (runs where the host runs plugin hooks: Hermes with the plugin enabled);
  3. everywhere else (Hermes engine-only, the proxy), the engine flags a stub write that went through
     in its own result at the next request, with the file's last known content stored under a recall
     key (``CMIEngine._flag_stub_writes``).

Deterministic, fail-open (any error lets the call through), no I/O.
"""
from __future__ import annotations

import json
import re
from typing import Any, Dict, Iterator, Optional, Tuple

from .lifecycle import (CONTENT_BEARING_ARG_TOOLS, OMITTED_ARG_KEY, OMITTED_NOTE_HEAD, OMITTED_NOTE_MID,
                        OMITTED_NOTE_TAIL, _CONTENT_ARG_KEYS, _MORE_ARG_KEYS)

# Any CMI stub head: "[CMI digest]", "[CMI one_liner]", "[CMI dup]", "[CMI batch item 3]",
# "[CMI: narration omitted]", "[CMI: content omitted — ...]", "[CMI project memory]".
_STUB_HEAD_RE = re.compile(r"\s*\[CMI[: ][^\]\n]{1,300}\]")
# The write stubs of CMI <= 1.12.0, anywhere in the payload (a file re-assembled around one).
_WRITE_STUB_RE = re.compile(
    r"\[CMI: (?:content omitted — already written to [^\n]{1,4096}?, [\d,]+ chars\. File on disk is the source of"
    r" truth\.|\w+ omitted — [\d,]+ chars, applied to [^\n]{1,4096}?\.)\]")
# The ``cmi_omitted`` note of 1.12.1+ shortened past writes: at the head of a payload, or whole (with
# its recall key) anywhere in it. Built from the wording lifecycle.py writes, so the two stay in sync.
_NOTE_HEAD_RE = re.compile(r"\s*" + re.escape(OMITTED_NOTE_HEAD) + r"[^\n]{1,4000}?" + re.escape(OMITTED_NOTE_MID))
_NOTE_RE = re.compile(re.escape(OMITTED_NOTE_HEAD) + r"[^\n]{0,4000}?cmi:[0-9a-f]{8,64}[^\n]{0,4000}?"
                      + re.escape(OMITTED_NOTE_MID) + r"[^\n]{1,4096}?" + re.escape(OMITTED_NOTE_TAIL))
_KEY_RE = re.compile(r"cmi:[0-9a-f]{8,64}")
_PAYLOAD_KEYS = _CONTENT_ARG_KEYS + _MORE_ARG_KEYS + ("file_content",)


def _bare(tool_name: str) -> str:
    name = (tool_name or "").lower()
    return name.split("__")[-1] if "__" in name else name


def _payloads(args: Dict[str, Any]) -> Iterator[Tuple[str, str]]:
    for key in _PAYLOAD_KEYS:
        value = args.get(key)
        if isinstance(value, str):
            yield key, value
        elif isinstance(value, list):  # MultiEdit "edits": [{"old_string", "new_string"}, ...]
            for item in value:
                if isinstance(item, dict):
                    for k, v in item.items():
                        if isinstance(v, str) and k in _PAYLOAD_KEYS:
                            yield f"{key}.{k}", v


def stub_in_payload(tool_name: str, args: Any) -> Optional[Tuple[str, str]]:
    """``(key, stub)`` when a write payload is (or starts with, or embeds a write stub of) CMI's own
    short form, or when the call carries the ``cmi_omitted`` note of a shortened past call (a model
    never sends that key itself: such a call is an imitation); ``None`` otherwise. Only
    content-bearing tools are checked."""
    if not isinstance(args, dict) or _bare(tool_name) not in CONTENT_BEARING_ARG_TOOLS:
        return None
    if OMITTED_ARG_KEY in args:
        return OMITTED_ARG_KEY, str(args[OMITTED_ARG_KEY])[:400]
    for key, value in _payloads(args):
        m = (_STUB_HEAD_RE.match(value) or _NOTE_HEAD_RE.match(value)
             or _WRITE_STUB_RE.search(value) or _NOTE_RE.search(value))
        if m:
            return key, value[m.start():m.end() + 80].strip()
    return None


def refuse_stub_write(tool_name: str = "", args: Any = None, **_kw: Any) -> Optional[Dict[str, str]]:
    """``pre_tool_call`` hook: block a write that would put a CMI stub in a file."""
    try:
        hit = stub_in_payload(tool_name, args)
    except Exception:
        return None
    if hit is None:
        return None
    key, stub = hit
    keys = _KEY_RE.findall(stub)
    how = (f"get the original with cmi_recall(\"{keys[0]}\") (if that key does not exist, re-read the file)"
           if keys else "re-read the file")
    if key == OMITTED_ARG_KEY:
        return {"action": "block",
                "message": (f"Blocked by CMI: this {tool_name or 'call'} copies the short form CMI shows for a past "
                            f"call (`{OMITTED_ARG_KEY}`), so it carries no real content. {how[0].upper()}{how[1:]} "
                            f"and send the full content, without `{OMITTED_ARG_KEY}`.")}
    return {"action": "block",
            "message": (f"Blocked by CMI: `{key}` holds CMI's short form of earlier content "
                        f"({json.dumps(stub[:120], ensure_ascii=False)}), not the content itself; running "
                        f"{tool_name or 'this tool'} would write that stub into the file. {how[0].upper()}{how[1:]} "
                        f"and send the full content.")}
