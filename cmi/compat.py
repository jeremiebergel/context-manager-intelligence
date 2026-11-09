"""Host bindings. Inside Hermes Agent, CMI uses the host's classes; anywhere else (the proxy,
another harness, tests without Hermes) it falls back to small portable equivalents.

Only three host symbols matter: the ContextEngine base class, the rough token estimator, and the
one-line tool summarizer. Compaction (summarizing a full window) is a host feature: without
Hermes there is no compactor and CMI only does per-request selection.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Dict, List, Optional

try:  # Hermes Agent
    from agent.context_engine import ContextEngine
    from agent.model_metadata import estimate_messages_tokens_rough as _host_estimate
    HERMES = True
except ImportError:  # standalone
    HERMES = False

    class ContextEngine:  # type: ignore[no-redef]
        """Minimal stand-in for Hermes' ContextEngine ABC."""

        last_prompt_tokens = 0
        last_completion_tokens = 0
        last_total_tokens = 0
        threshold_tokens = 0
        context_length = 0
        compression_count = 0
        threshold_percent = 0.75
        protect_first_n = 3
        protect_last_n = 6

        def on_session_start(self, session_id: str, **kwargs: Any) -> None:
            pass

        def on_session_end(self, session_id: str, messages: List[Dict[str, Any]]) -> None:
            pass

        def on_session_reset(self) -> None:
            self.last_prompt_tokens = self.last_completion_tokens = self.last_total_tokens = 0
            self.compression_count = 0

        def get_status(self) -> Dict[str, Any]:
            return {"last_prompt_tokens": self.last_prompt_tokens, "context_length": self.context_length}

    def _host_estimate(messages: List[Dict[str, Any]], **_: Any) -> int:  # type: ignore[misc]
        """~4 chars per token over the JSON wire shape; images at a flat 1,600 tokens."""
        total = 0
        for m in messages:
            images = 0
            if isinstance(m, dict) and isinstance(m.get("content"), list):
                parts = []
                for p in m["content"]:
                    if isinstance(p, dict) and p.get("type") in ("image", "image_url", "input_image"):
                        images += 1
                    else:
                        parts.append(p)
                m = {**m, "content": parts}
            total += len(json.dumps(m, ensure_ascii=False, default=str)) // 4 + images * 1600
        return total

_IMAGE_TYPES = ("image", "image_url", "input_image")
_NESTED_IMAGE_TOKENS = 1600


def _drop_nested_images(parts: Any) -> Any:
    """(parts without images found BELOW the top content level, count). Top-level images are left
    to the host estimator (it prices them, calibrated in Hermes)."""
    count = 0
    out = []
    changed = False
    for p in parts:
        if isinstance(p, dict):
            for k in ("content", "output"):
                inner = p.get(k)
                if isinstance(inner, list):
                    kept = [x for x in inner if not (isinstance(x, dict) and x.get("type") in _IMAGE_TYPES)]
                    deeper, n = _drop_nested_images(kept)
                    n += len(inner) - len(kept)
                    if n:
                        p = {**p, k: deeper}
                        count += n
                        changed = True
        out.append(p)
    return (out if changed else parts), count


def estimate_messages_tokens_rough(messages: List[Dict[str, Any]], **kw: Any) -> int:
    """Host estimate, with images nested in tool results (Anthropic tool_result, Responses
    function_call_output) priced at their billed size instead of their base64
    length. Counted as text, a screenshot weighed ~100K tokens instead of ~1.6K, and cache tiers that
    evict screenshots looked far more profitable than they are (real session replay 2026-11-04).
    Images at the top level of a message (incl. OpenAI `tool` messages) are already priced by the
    host estimator; tests/test_wire.py::test_w12 checks both shapes."""
    fixed = []
    nested = 0
    for m in messages:
        c = m.get("content") if isinstance(m, dict) else None
        if isinstance(c, list):
            parts, n = _drop_nested_images(c)
            if n:
                m = {**m, "content": parts}
                nested += n
        fixed.append(m)
    return _host_estimate(fixed, **kw) + nested * _NESTED_IMAGE_TOKENS


try:
    from agent.context_compressor import _summarize_tool_result  # private: richest one-liners
except ImportError:
    def _summarize_tool_result(tool_name: str, tool_args: str, tool_content: str) -> str:
        first = next((ln.strip() for ln in (tool_content or "").splitlines() if ln.strip()), "")
        return f"[{tool_name}] {len(tool_content or '')} chars: {first[:160]}"


def default_home() -> Path:
    """Where CMI keeps its store when the host gives no explicit home."""
    if HERMES:
        from hermes_constants import get_hermes_home
        return Path(get_hermes_home())
    return Path(os.environ.get("CMI_HOME") or Path.home() / ".cmi")


def coerce_positive_int(value: Any) -> Optional[int]:
    """Same normalization as Hermes' ContextCompressor._coerce_max_tokens."""
    try:
        ivalue = int(value) if value is not None else 0
    except (TypeError, ValueError):
        return None
    return ivalue if ivalue > 0 else None
