"""Host bindings. Inside Hermes Agent, CMI uses the host's classes; anywhere else (the proxy,
another harness, tests without Hermes) it falls back to small portable equivalents.

Only three host symbols matter: the ContextEngine base class, the rough token estimator, and the
one-line tool summarizer. Compaction (summarizing a full window) is a host feature: without
Hermes there is no compactor and CMI only does per-request selection.
"""
from __future__ import annotations

import base64
import json
import os
import re
import zlib
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

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


_PDF_PAGE_TOKENS = 2000        # Anthropic bills a PDF page as its text plus one page image
_PDF_BYTES_PER_PAGE = 100_000  # used only when the page count cannot be read
_PDF_PAGE_RE = re.compile(rb"/Type\s*/Page(?![A-Za-z])")
# PDF 1.5+ writers pack page objects in compressed object streams (/Type /ObjStm, Flate).
_PDF_OBJSTM_RE = re.compile(rb"/Type\s*/ObjStm\b[^>]*>>\s*stream\r?\n")
_PDF_FIRST_RE = re.compile(rb"/First\s+(\d+)")
_PDF_LENGTH_RE = re.compile(rb"/Length\s+(\d+)(?!\s+\d+\s+R)")  # direct /Length only, not "/Length 12 0 R"
_PDF_OBJ_RE = re.compile(rb"(\d+)\s+\d+\s+obj\b")
_PDF_INFLATE_MAX = 16_000_000  # bytes inflated per document, bounds a hostile stream
_document_tokens_cache: Dict[Any, int] = {}


def _pdf_objstm_bytes(raw: bytes, m: "re.Match[bytes]") -> Tuple[Optional[bytes], Optional[int]]:
    """The compressed bytes and /First of the object stream whose dictionary ends at ``m``. Its
    dictionary is read from its own "N 0 obj" header, never from a previous object; the bytes are
    sliced by a direct /Length (a stream may contain the text "endstream"), else up to "endstream"."""
    owners = list(_PDF_OBJ_RE.finditer(raw, max(0, m.start() - 4096), m.start()))
    head = raw[owners[-1].end() if owners else max(0, m.start() - 2048):m.end()]
    first = _PDF_FIRST_RE.search(head)
    length = _PDF_LENGTH_RE.search(head)
    if length and m.end() + int(length.group(1)) <= len(raw):
        data: Optional[bytes] = raw[m.end():m.end() + int(length.group(1))]
    else:
        end = raw.find(b"endstream", m.end())
        data = raw[m.end():end] if end >= 0 else None
    return data, int(first.group(1)) if first else None


def _pdf_pages(raw: bytes) -> int:
    """Distinct page objects in a PDF, visible or packed in Flate object streams. Counted by object
    number, so a page rewritten by an incremental update (arXiv stamps, signed PDFs) counts once.
    A stream that does not inflate is skipped; never raises."""
    seen: set = set()
    spans: List[Tuple[int, int]] = []  # object-stream bodies: their bytes are not visible objects
    budget = _PDF_INFLATE_MAX
    for m in _PDF_OBJSTM_RE.finditer(raw):
        if budget <= 0:
            break
        data, base = _pdf_objstm_bytes(raw, m)
        if data is None:
            continue
        spans.append((m.end(), m.end() + len(data)))
        try:
            inflated = zlib.decompressobj().decompress(data, budget)
        except Exception:
            continue
        budget -= len(inflated)
        try:
            nums = [int(x) for x in inflated[:base].split()] if base else []
        except ValueError:
            nums = []
        if base and nums and len(nums) % 2 == 0:
            offs = nums[1::2] + [len(inflated) - base]
            for k, num in enumerate(nums[0::2]):
                if _PDF_PAGE_RE.search(inflated, base + offs[k], base + offs[k + 1]):
                    seen.add(num)
        else:  # header unreadable: count the matches, without dedupe
            seen.update(("packed", m.start(), i) for i, _ in enumerate(_PDF_PAGE_RE.finditer(inflated)))
    for m in _PDF_PAGE_RE.finditer(raw):
        if any(a <= m.start() < b for a, b in spans):
            continue
        owners = list(_PDF_OBJ_RE.finditer(raw, max(0, m.start() - 4096), m.start()))
        seen.add(int(owners[-1].group(1)) if owners else ("raw", m.start()))
    return len(seen)


def document_tokens(part: Any) -> Optional[int]:
    """Billed size of a base64 document block (a PDF attached to the conversation), or None for
    any other part. A PDF is billed per page, not by its base64 length: counted as text, a 4 MB
    PDF weighed ~1M tokens instead of ~60K (real Claude Code session replay 2026-11-11)."""
    if not (isinstance(part, dict) and part.get("type") == "document"):
        return None
    src = part.get("source")
    data = src.get("data") if isinstance(src, dict) and src.get("type") == "base64" else None
    if not isinstance(data, str):
        return None
    key = (len(data), hash(data))  # the whole text: two PDFs can share their first and last bytes
    tokens = _document_tokens_cache.get(key)
    if tokens is None:
        try:
            pages = _pdf_pages(base64.b64decode(data, validate=False))
        except Exception:
            pages = 0
        # No page object found at all (encrypted or unusual file): fall back on the file size.
        pages = pages or max(1, len(data) * 3 // 4 // _PDF_BYTES_PER_PAGE)
        tokens = pages * _PDF_PAGE_TOKENS
        if len(_document_tokens_cache) >= 256:
            _document_tokens_cache.clear()
        _document_tokens_cache[key] = tokens
    return tokens


def _drop_documents(parts: Any) -> Any:
    """(parts without base64 documents at any level, their billed tokens)."""
    tokens = 0
    out = []
    changed = False
    for p in parts:
        t = document_tokens(p)
        if t is not None:
            tokens += t
            changed = True
            continue
        if isinstance(p, dict):
            for k in ("content", "output"):
                inner = p.get(k)
                if isinstance(inner, list):
                    deeper, n = _drop_documents(inner)
                    if n:
                        p = {**p, k: deeper}
                        tokens += n
                        changed = True
        out.append(p)
    return (out if changed else parts), tokens


def estimate_messages_tokens_rough(messages: List[Dict[str, Any]], **kw: Any) -> int:
    """Host estimate, with images nested in tool results (Anthropic tool_result, Responses
    function_call_output) priced at their billed size instead of their base64
    length. Counted as text, a screenshot weighed ~100K tokens instead of ~1.6K, and cache tiers that
    evict screenshots looked far more profitable than they are (real session replay 2026-11-04).
    Images at the top level of a message (incl. OpenAI `tool` messages) are already priced by the
    host estimator; tests/test_wire.py::test_w12 checks both shapes. Base64 documents (PDF) are
    priced per page at every level: no host estimator knows them."""
    fixed = []
    nested = 0
    documents = 0
    for m in messages:
        c = m.get("content") if isinstance(m, dict) else None
        if isinstance(c, list):
            parts, n = _drop_nested_images(c)
            parts, d = _drop_documents(parts)
            if n or d:
                m = {**m, "content": parts}
                nested += n
                documents += d
        fixed.append(m)
    return _host_estimate(fixed, **kw) + nested * _NESTED_IMAGE_TOKENS + documents


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
