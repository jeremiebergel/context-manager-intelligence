"""Per-job tool allowlist: an automated job that needs 2 tools should not ship 40 tool schemas.

Opt-in per request through the header `x-cmi-tools-allow: read_file,terminal` (set by the job's
launcher; stripped before forwarding). No header = no change. The schemas are ~15k tokens of a
~19k-token fixed prefix, so trimming them is the biggest lever on short, repeated runs.

Fail-open rules, so a mistake can never block a session:
  * an allowlist matching no tool of the request is ignored (typo);
  * tools already called in the conversation are always kept (the history stays valid);
  * the tool named by `tool_choice` is always kept;
  * an unreadable body is left untouched.
A tool the model would want but the job did not list is simply not offered: that is the cost of
the opt-in, which is why the allowlist is declared by the job and never inferred.
"""
from __future__ import annotations

from typing import Any, Dict, Iterable, Optional, Set

HEADER = "x-cmi-tools-allow"


def parse_allow(value: Optional[str]) -> Set[str]:
    return {p.strip() for p in (value or "").split(",") if p.strip()}


def _tool_name(t: Any) -> Optional[str]:
    if not isinstance(t, dict):
        return None
    fn = t.get("function")
    name = fn.get("name") if isinstance(fn, dict) else t.get("name")
    return name if isinstance(name, str) else None


def _used_names(body: Dict[str, Any]) -> Set[str]:
    used: Set[str] = set()
    items: Iterable[Any] = [*(body.get("messages") or []), *(body.get("input") or [])] \
        if isinstance(body.get("input"), list) or isinstance(body.get("messages"), list) else []
    for m in items:
        if not isinstance(m, dict):
            continue
        if m.get("type") == "function_call" and isinstance(m.get("name"), str):  # Responses
            used.add(m["name"])
        for tc in m.get("tool_calls") or []:  # OpenAI Chat
            n = _tool_name(tc)
            if n:
                used.add(n)
        c = m.get("content")
        if isinstance(c, list):  # Anthropic
            for p in c:
                if isinstance(p, dict) and p.get("type") == "tool_use" and isinstance(p.get("name"), str):
                    used.add(p["name"])
    return used


def _choice_name(body: Dict[str, Any]) -> Optional[str]:
    tc = body.get("tool_choice")
    return _tool_name(tc) if isinstance(tc, dict) else None


def scope_tools(body: Dict[str, Any], allow: Set[str]) -> Optional[Dict[str, Any]]:
    """Return a new body with the tool list reduced, or None when nothing should change."""
    tools = body.get("tools")
    if not allow or not isinstance(tools, list) or not tools:
        return None
    keep = set(allow) | _used_names(body)
    c = _choice_name(body)
    if c:
        keep.add(c)
    names = [_tool_name(t) for t in tools]
    if not any(n in allow for n in names):  # typo guard: never strip down to nothing
        return None
    kept = [t for t, n in zip(tools, names) if n is None or n in keep]  # unnamed (opaque) tools stay
    if len(kept) == len(tools):
        return None
    # The cache breakpoint sat on the last tool: keep it on the new last tool.
    last = tools[-1]
    if isinstance(last, dict) and "cache_control" in last and kept and kept[-1] is not last \
            and isinstance(kept[-1], dict) and "cache_control" not in kept[-1]:
        kept = [*kept[:-1], {**kept[-1], "cache_control": last["cache_control"]}]
    return {**body, "tools": kept}
