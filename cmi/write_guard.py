"""Refuse a write whose payload is one of CMI's own stubs (Hermes ``pre_tool_call`` hook).

CMI shows old tool results and old write payloads in short form. A model can copy one of those short
forms into a NEW write, which would put the stub in the file instead of the content (seen live in
1.12.0: a ``[CMI: content omitted ...]`` stub with an invented recall key written as a new file).
Since 1.12.1 a shortened past write carries no payload key at all, so imitating it fails in the tool
("missing content"); this hook is the second barrier, for every other stub form: a write payload that
starts with a CMI stub, or that contains a write stub, is blocked with the way to recover the original.

Deterministic, fail-open (any error lets the call through), no I/O.
"""
from __future__ import annotations

import json
import re
from typing import Any, Dict, Iterator, Optional, Tuple

from .lifecycle import CONTENT_BEARING_ARG_TOOLS, _CONTENT_ARG_KEYS, _MORE_ARG_KEYS

# Any CMI stub head: "[CMI digest]", "[CMI one_liner]", "[CMI dup]", "[CMI batch item 3]",
# "[CMI: narration omitted]", "[CMI: content omitted — ...]", "[CMI project memory]".
_STUB_HEAD_RE = re.compile(r"\s*\[CMI[: ][^\]\n]{1,300}\]")
# The write stubs of CMI <= 1.12.0, anywhere in the payload (a file re-assembled around one).
_WRITE_STUB_RE = re.compile(
    r"\[CMI: (?:content omitted — already written to [^\n]{1,4096}?, [\d,]+ chars\. File on disk is the source of"
    r" truth\.|\w+ omitted — [\d,]+ chars, applied to [^\n]{1,4096}?\.)\]")
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
    short form; ``None`` otherwise. Only content-bearing tools are checked."""
    if not isinstance(args, dict) or _bare(tool_name) not in CONTENT_BEARING_ARG_TOOLS:
        return None
    for key, value in _payloads(args):
        m = _STUB_HEAD_RE.match(value) or _WRITE_STUB_RE.search(value)
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
    return {"action": "block",
            "message": (f"Blocked by CMI: `{key}` holds CMI's short form of earlier content "
                        f"({json.dumps(stub[:120], ensure_ascii=False)}), not the content itself; running "
                        f"{tool_name or 'this tool'} would write that stub into the file. {how[0].upper()}{how[1:]} "
                        f"and send the full content.")}
