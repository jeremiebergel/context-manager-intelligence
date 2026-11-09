"""Wire formats <-> the canonical history the engine compiles.

The engine is tuned on one shape: OpenAI-style chat where every tool result is its own message
(what Hermes sends, whatever the provider). Other wire formats are converted to that shape, the
engine compiles it, and only the parts the engine actually changed are written back. Everything
else (thinking/reasoning blocks and their signatures, cache_control, ids, unknown item types)
goes back byte-for-byte as received.

    canon, back = to_canonical(fmt, body)      # back: how to write a compiled canon into body
    new_body = back(compiled_canon)            # None when nothing changed

Formats: "openai" (chat completions, already canonical), "anthropic" (Messages: several
tool_result blocks share one user message, so each becomes its own canonical message),
"responses" (OpenAI Responses: flat items; reasoning items are never shown to the engine).
"""
from __future__ import annotations

import json
from typing import Any, Callable, Dict, List, Optional, Tuple

Canon = List[Dict[str, Any]]
Back = Callable[[Canon], Optional[Dict[str, Any]]]


def to_canonical(fmt: str, body: Dict[str, Any]) -> Tuple[Canon, Back]:
    if fmt == "anthropic":
        return _anthropic(body)
    if fmt == "responses":
        return _responses(body)
    return _chat(body)


# ---- OpenAI chat completions: identity ---------------------------------------------------

def _chat(body: Dict[str, Any]) -> Tuple[Canon, Back]:
    msgs = list(body.get("messages") or [])

    def back(out: Canon) -> Optional[Dict[str, Any]]:
        if len(out) != len(msgs) or out == msgs:
            return None
        return {**body, "messages": out}
    return msgs, back


# ---- Anthropic Messages ------------------------------------------------------------------

def _anthropic(body: Dict[str, Any]) -> Tuple[Canon, Back]:
    msgs = list(body.get("messages") or [])
    canon: Canon = []
    # For each canonical message: (source message index, source part index or -1 for "whole").
    src: List[Tuple[int, int]] = []
    for i, m in enumerate(msgs):
        parts = m.get("content") if isinstance(m, dict) else None
        results = [j for j, p in enumerate(parts or []) if isinstance(p, dict) and p.get("type") == "tool_result"] \
            if isinstance(parts, list) and m.get("role") == "user" else []
        if not results or (len(results) == 1 and len(parts) == 1):
            canon.append(m)
            src.append((i, -1))
            continue
        # One canonical message per tool_result, so the engine decides per tool. Other blocks of
        # that message (e.g. a harness reminder) are not tool output and not user words: they
        # stay out of the engine's view and go back untouched.
        assert isinstance(parts, list)
        for j in results:
            canon.append({"role": "user", "content": [parts[j]]})
            src.append((i, j))

    def back(out: Canon) -> Optional[Dict[str, Any]]:
        if len(out) != len(canon) or out == canon:
            return None
        new = list(msgs)
        for k, (i, j) in enumerate(src):
            if out[k] == canon[k]:
                continue
            if j < 0:
                new[i] = out[k]
                continue
            got = out[k].get("content")
            if not (isinstance(got, list) and len(got) == 1 and isinstance(got[0], dict)):
                continue  # unexpected shape: keep the original block
            if new[i] is msgs[i]:
                new[i] = {**msgs[i], "content": list(msgs[i]["content"])}
            new[i]["content"][j] = got[0]
        return {**body, "messages": new}
    return canon, back


# ---- OpenAI Responses --------------------------------------------------------------------

_CALLS = {"function_call": "arguments", "custom_tool_call": "input"}
_OUTPUTS = {"function_call_output", "custom_tool_call_output"}


def _text_of(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(str(p.get("text") or "") for p in content
                         if isinstance(p, dict) and p.get("type") in ("input_text", "output_text", "text"))
    return ""


def _text_only(content: Any) -> bool:
    """A non-empty list of text blocks only: rewriting it as one text block loses nothing."""
    return isinstance(content, list) and bool(content) and all(
        isinstance(p, dict) and p.get("type") in ("input_text", "output_text", "text") for p in content)


def _responses(body: Dict[str, Any]) -> Tuple[Canon, Back]:
    items = body.get("input")
    if not isinstance(items, list) or body.get("previous_response_id"):
        # A bare string, or the server holds the history: nothing to compile.
        return [], lambda out: None
    canon: Canon = []
    # Write-back plan per canonical index: ("msg", item idx) | ("asst", msg item idx or -1,
    # [call item idxs]) | ("out", item idx) | ("ro", None).
    plan: List[Tuple[str, Any, Any]] = []
    k = 0
    n = len(items)
    while k < n:
        it = items[k]
        t = it.get("type", "message") if isinstance(it, dict) else None
        role = it.get("role") if isinstance(it, dict) else None
        if t == "message" and role == "assistant" or t in _CALLS:
            # An assistant turn: optional text message, then its calls (reasoning items between
            # them are skipped, never shown, never moved).
            msg_idx = -1
            if t == "message":
                msg_idx = k
                k += 1
            calls: List[int] = []
            while k < n and isinstance(items[k], dict) and (
                    items[k].get("type") in _CALLS or items[k].get("type") == "reasoning"):
                if items[k].get("type") in _CALLS:
                    calls.append(k)
                k += 1
            m: Dict[str, Any] = {"role": "assistant",
                                 "content": _text_of(items[msg_idx].get("content")) if msg_idx >= 0 else ""}
            if calls:
                m["tool_calls"] = [{
                    "id": str(items[c].get("call_id") or ""),
                    "type": "function",
                    "function": {"name": str(items[c].get("name") or ""),
                                 "arguments": _call_args(items[c])},
                } for c in calls]
            canon.append(m)
            plan.append(("asst", msg_idx, calls))
            continue
        if t in _OUTPUTS:
            out = it.get("output")
            canon.append({"role": "tool", "tool_call_id": str(it.get("call_id") or ""),
                          "content": out if isinstance(out, str) else _text_of(out)})
            plan.append(("out", k, None))
        elif t == "message" and role in ("user", "system", "developer"):
            canon.append({"role": "system" if role == "developer" else role, "content": _text_of(it.get("content"))})
            plan.append(("ro", None, None))
        # reasoning, web/file search calls, unknown types: invisible to the engine, kept as is.
        k += 1

    def back(out: Canon) -> Optional[Dict[str, Any]]:
        if len(out) != len(canon) or out == canon:
            return None
        new = list(items)
        changed = False
        for c, (kind, a, b) in enumerate(plan):
            if out[c] == canon[c] or kind == "ro":
                continue
            if kind == "out":
                got = out[c].get("content")
                orig = new[a].get("output")
                if not isinstance(got, str) or got == canon[c]["content"]:
                    continue
                if isinstance(orig, str):
                    new[a] = {**new[a], "output": got}
                    changed = True
                elif _text_only(orig):
                    # Codex >= 0.164 sends a list of input_text blocks; the canon joined them.
                    new[a] = {**new[a], "output": [{"type": "input_text", "text": got}]}
                    changed = True
                continue
            # assistant: narration text and/or call payloads
            if a >= 0 and out[c].get("content") != canon[c].get("content") and isinstance(out[c].get("content"), str):
                new[a] = {**new[a], "content": [{"type": "output_text", "text": out[c]["content"], "annotations": []}]}
                changed = True
            for tc_old, tc_new, idx in zip(canon[c].get("tool_calls") or [], out[c].get("tool_calls") or [], b):
                old_a = tc_old["function"]["arguments"]
                new_a = ((tc_new or {}).get("function") or {}).get("arguments")
                if isinstance(new_a, str) and new_a != old_a:
                    field = _CALLS[new[idx]["type"]]
                    new[idx] = {**new[idx], field: new_a if field == "arguments" else _custom_input(new_a)}
                    changed = True
        return {**body, "input": new} if changed else None
    return canon, back


def _call_args(item: Dict[str, Any]) -> str:
    if item.get("type") == "custom_tool_call":
        # Freeform tools (Codex apply_patch) carry raw text; present it as a JSON argument.
        return json.dumps({"input": item.get("input") or ""}, ensure_ascii=False)
    a = item.get("arguments")
    return a if isinstance(a, str) else json.dumps(a or {}, ensure_ascii=False)


def _custom_input(args: str) -> str:
    try:
        obj = json.loads(args)
    except ValueError:
        return args
    if isinstance(obj, dict) and isinstance(obj.get("input"), str):
        return obj["input"]
    return json.dumps(obj, ensure_ascii=False)


def strip_cache_control(x: Any) -> Any:
    """Moving cache breakpoints must not look like a different conversation."""
    if isinstance(x, dict):
        return {k: strip_cache_control(v) for k, v in x.items() if k != "cache_control"}
    if isinstance(x, list):
        return [strip_cache_control(v) for v in x]
    return x


__all__ = ["to_canonical", "strip_cache_control"]
