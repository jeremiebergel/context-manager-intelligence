"""ToolResultLifecycle — deterministic degradation of stale tool results.

States are DERIVED per request. The persisted raw is never destroyed;
only the REQUEST rendering changes (request-only contract).
"""
from __future__ import annotations
import hashlib
import json
import re
from typing import Any, Dict, Optional

# ---- Tunable coefficients -----------------------------------------------
TR_RECENCY_CURRENT: float = 1.0
TR_RECENCY_RECENT: float = 0.5
TR_RECENT_AGE: int = 2
W_RECENCY: float = 2.0
W_RELEVANCE: float = 1.5
W_CRITICALITY: float = 1.5
W_SIZE_PENALTY: float = 1.0
W_REPRESENTED: float = -2.0
W_USER_ANCHOR: float = -3.0

TR_KEEP_THRESHOLD: float = 3.0
TR_DIGEST_THRESHOLD: float = 1.5
TR_SIZE_PENALTY_MIN_TOKENS: int = 2000

IMG_KEEP_AGE: int = 2
DIGEST_HEAD_CHARS: int = 300
DIGEST_HEAD_CHARS_TINY: int = 60

CRITICAL_TOOLS = {
    "patch", "write_file", "delegate_task", "skill_manage", "memory",
    "mcp__patch", "mcp__write_file", "mcp__delegate_task",
    "mcp__skill_manage", "mcp__memory",
}
_ERROR_MARKERS = ("error", "fail", "exception", "traceback", "fatal", "panic")
# Line-level twin of _ERROR_MARKERS for digest excerpts: word-start match, so "terror" or
# "unfailing" stay out but FAILED, Errors, panicked get in.
_ERROR_LINE = re.compile(r"(?i)\b(?:error|fail|exception|traceback|fatal|panic)")

# ---- Tool-call ARGUMENT degradation (v1.1) --------------------------------
# Distinct from tool RESULT degradation above. write_file/patch calls often
# carry the full file content as an ARGUMENT — once the write succeeds, that
# content is a pure duplicate of what's now on disk; the tool RESULT (success/
# failure) still matters and is scored separately via the logic above. This
# targets a real gisement found on code-heavy sessions (spec follow-up,
# Real code-heavy session benchmark: write_file args averaged ~1.2K tokens each while
# results were tiny confirmations).
# Write/edit tools across hosts (bare names, lowercase): Hermes, Claude Code (Write, Edit,
# MultiEdit, NotebookEdit), Codex (apply_patch), Cline/Roo (write_to_file, replace_in_file)...
CONTENT_BEARING_ARG_TOOLS = {
    "write_file", "patch", "write", "edit", "multiedit", "multi_edit", "notebookedit", "apply_patch",
    "apply_diff", "edit_file", "create_file", "write_to_file", "replace_in_file", "search_replace",
    "str_replace", "str_replace_editor", "str_replace_based_edit_tool", "insert",
}
# Argument keys that carry the actual file/text payload for these tools.
_CONTENT_ARG_KEYS = ("content", "new_string", "file_content", "text")
# Further payload keys, also shortened (Edit's old_string, editor tools, patch envelopes, MultiEdit).
_MORE_ARG_KEYS = ("old_string", "file_text", "new_str", "old_str", "new_source", "diff", "patch", "input",
                  "edits")
TOOL_ARGS_RECENT_AGE: int = 2       # same recency window as results
TOOL_ARGS_MIN_CHARS: int = 300      # profitability floor for the payload itself

# ---- Duplicate tool-result detection (v1.1) -------------------------------
# Exact content repeats (e.g. re-running `git status` or the same search)
# cost nothing extra after the first occurrence — a byte-identical payload
# carries zero new information the second time.
DUP_MIN_CHARS: int = 80              # below this, a dup reference isn't worth it

# ---- Small-result batch aggregation (v1.1) --------------------------------
# Individually tiny tool results (e.g. 100 short `terminal` calls at ~80
# chars each) are each non-profitable to digest alone, but their SUM is a
# real cost on tool-heavy/code sessions. When >= BATCH_MIN_SIZE consecutive
# old, same-tool, non-anchored results appear, they are folded into ONE
# batch summary (attached to the first item) while the rest become minimal
# stubs pointing back at it. Every tool_call/tool_result PAIR still exists —
# only their content shrinks.
BATCH_MIN_SIZE: int = 3
BATCH_MAX_TOOL_ARGS_ECHO: int = 24   # chars of arg preview kept per batch item
BATCH_MAX_CONTENT_ECHO: int = 40     # chars of result preview kept per batch item

# ---- Assistant narration compaction (v1.1) --------------------------------
# An assistant message that carries tool_calls AND throwaway lead-in prose
# ("Let me check...", "Je vais d'abord...") duplicates information the
# following tool_call already conveys. Old, non-anchored narration text is
# safe to drop; the action (tool_call name + args) remains, and the final
# text-only answer (no tool_calls) is NEVER touched — only text attached to
# an action, never a standalone reply.
NARRATION_MIN_CHARS: int = 40        # below this, nothing to gain
NARRATION_MAX_KEEP_CHARS: int = 20   # tiny anchor kept ("[...]") for readability


def _tokens_rough(text: Any) -> int:
    if not text:
        return 0
    s = text if isinstance(text, str) else json.dumps(text, ensure_ascii=False, default=str)
    return (len(s.encode("utf-8", "replace")) + 3) // 4


def _stable_id(msg_index: int, tool_call_id: str) -> str:
    raw = f"{msg_index}:{tool_call_id}"
    return hashlib.sha1(raw.encode(), usedforsecurity=False).hexdigest()[:16]



def _ck(*parts) -> str:
    """Memo key = slot + every input of the render. A slot (message index, tool-call id) can hold
    different content over time (rewinds, edits, a shared proxy session, reused `call_0` ids), so
    the key must never be the slot alone: same inputs -> same bytes, other inputs -> new render."""
    h = hashlib.sha256()
    for p in parts:
        h.update(str(p).encode("utf-8", "surrogatepass"))
        h.update(b"\0")
    return f"{parts[0]}#{h.hexdigest()[:20]}"

class ToolResultLifecycle:
    def __init__(self) -> None:
        self._digest_cache: Dict[str, str] = {}
        self._render_state: Dict[str, str] = {}
        self._represented: set = set()
        self._referenced: set = set()

    def on_compaction_boundary(self, represented_ids: Optional[set] = None) -> None:
        if represented_ids:
            self._represented |= set(represented_ids)
        for iid, state in list(self._render_state.items()):
            if state == "digest":
                self._render_state[iid] = "one_liner"

    def reset(self) -> None:
        self.__init__()

    def note_reference(self, instance_id: str) -> None:
        self._referenced.add(instance_id)

    def is_referenced(self, instance_id: str) -> bool:
        return instance_id in self._referenced

    def score(
        self,
        *,
        age: int,
        relevance: float,
        criticality: float,
        size_tokens: int,
        represented: bool,
        user_anchor: bool,
    ) -> float:
        recency = TR_RECENCY_CURRENT if age <= 0 else (
            TR_RECENCY_RECENT if age <= TR_RECENT_AGE else 0.0
        )
        s = (
            W_RECENCY * recency
            + W_RELEVANCE * relevance
            + W_CRITICALITY * criticality
            + W_SIZE_PENALTY * (1.0 if size_tokens > TR_SIZE_PENALTY_MIN_TOKENS else 0.0)
            + (W_REPRESENTED if represented else 0.0)
            + (W_USER_ANCHOR if user_anchor else 0.0)
        )
        return s

    @staticmethod
    def criticality_of(tool_name: str, content: str, exit_code: Optional[int] = None) -> float:
        name = (tool_name or "").lower()
        if name in CRITICAL_TOOLS:
            return 1.0
        text = (content or "").lower()[:20000]
        if any(m in text for m in _ERROR_MARKERS):
            return 1.0
        if exit_code is not None and exit_code != 0:
            return 0.5
        return 0.0

    def render_digest(
        self,
        instance_id: str,
        *,
        tool_name: str,
        tool_args: str,
        content: str,
        summarize_fn,
        one_liner: bool = False,
        overhead_chars: int = 0,
    ) -> str:
        """Build a memoized digest that NEVER exceeds the original content
        size (accounting for the caller's wrapping overhead, e.g. a
        "[CMI digest] " prefix). A digest that grows the payload is worse
        than doing nothing — this is the profitability guarantee.

        The excerpt is DISTRIBUTED (head + middle + tail), not a pure head
        truncation: a fixed head-only excerpt systematically hides anything
        past the first ~300 chars (found via a real test — a secret defined
        after a comment block became invisible). Distributed sampling gives
        every region of the content a chance to survive into the excerpt.
        """
        instance_id = _ck(instance_id, tool_name, tool_args, content, one_liner, overhead_chars)
        cached = self._digest_cache.get(instance_id)
        if cached is not None:
            return cached
        content = content or ""
        original_len = len(content)
        budget = max(0, original_len - overhead_chars)

        base = summarize_fn(tool_name, tool_args, content)
        if not base:
            base = f"[{tool_name}] ({original_len:,} chars result)"

        if one_liner:
            text = base.splitlines()[0][:min(400, max(budget, 1))] if base else base
        else:
            # Excerpt length adapts to the remaining budget after the
            # summary line — never fixed at DIGEST_HEAD_CHARS regardless of
            # how small the original content is.
            remaining = budget - len(base) - len("\n[excerpt] ")
            head_chars = max(0, min(DIGEST_HEAD_CHARS, remaining))
            if head_chars > 0:
                excerpt = self._distributed_excerpt(content, head_chars)
                text = f"{base}\n[excerpt] {excerpt}"
            else:
                text = base

        # Final profitability guarantee: if even the bare summary exceeds
        # budget, hard-truncate it. This is the backstop for pathological
        # summarize_fn outputs (verbose tool names/args).
        if len(text) > budget and budget > 0:
            text = text[:budget]

        self._digest_cache[instance_id] = text
        return text

    @staticmethod
    def _distributed_excerpt(content: str, budget_chars: int) -> str:
        """Sample head + middle + tail instead of a pure head truncation.

        A pure head excerpt hides everything past the budget — on real code
        (a config file with the interesting line after a long comment
        block) that reliably loses the one thing that mattered. Splitting
        the budget across three regions gives each a chance to survive.
        Below ~90 chars the split overhead isn't worth it; head-only wins.
        """
        n = len(content)
        if n <= budget_chars:
            return content
        flagged = ToolResultLifecycle._flagged_lines(content, budget_chars // 2)
        if not flagged:
            return ToolResultLifecycle._sample(content, budget_chars)
        # Error-looking lines (FATAL, Traceback...) are what an agent comes back for; a blind
        # head/middle/tail sample drops them whenever they sit between the samples.
        rest = ToolResultLifecycle._sample(content, budget_chars - len(flagged))
        return f"{rest}{flagged}"[:budget_chars]

    @staticmethod
    def _flagged_lines(content: str, cap: int) -> str:
        """Up to `cap` chars of error-looking lines, as `\\n[flagged] L<n>: <line> | ...`."""
        if cap < 40:
            return ""
        out = ""
        for no, line in enumerate(content[:200_000].splitlines(), 1):
            m = _ERROR_LINE.search(line)
            if not m:
                continue
            head = f"{out} | " if out else "\n[flagged] "
            label = f"L{no}: "
            room = min(160, cap - len(head) - len(label))
            if room < 30:
                break
            text = line.strip()
            if len(text) > room:  # keep a window around the marker, not just the line's start
                pos = m.start() - (len(line) - len(line.lstrip()))
                start = max(0, min(pos - room // 4, len(text) - room))
                text = text[start:start + room]
            out = f"{head}{label}{text}"
        return out

    @staticmethod
    def _sample(content: str, budget_chars: int) -> str:
        n = len(content)
        if n <= budget_chars:
            return content
        if budget_chars < 90:
            return content[:budget_chars]
        sep = " […] "
        each = (budget_chars - 2 * len(sep)) // 3
        if each <= 0:
            return content[:budget_chars]
        head = content[:each]
        mid_start = max(each, n // 2 - each // 2)
        middle = content[mid_start:mid_start + each]
        tail = content[-each:]
        return f"{head}{sep}{middle}{sep}{tail}"

    @staticmethod
    def is_profitable(content: str, *, overhead_chars: int = 0,
                      min_content_chars: int = 150) -> bool:
        """A tool result under this size can never be shrunk profitably —
        any digest wrapper (prefix + summary sentence) costs more than the
        raw text it would replace. Skip degradation entirely (state stays
        'raw') rather than emit a digest that grows the payload.
        """
        return len(content or "") >= min_content_chars + overhead_chars

    def decide(
        self,
        *,
        age: int,
        score: float,
        user_anchor: bool,
        represented: bool,
        referenced: bool,
        relevance_priority: bool = False,
        profitable: bool = True,
    ) -> str:
        if age <= TR_RECENT_AGE or user_anchor:
            return "raw"
        if not profitable:
            # Too small to shrink profitably — a digest wrapper would grow
            # the payload. Keep raw regardless of score (spec: never degrade
            # at a net token cost).
            return "raw"
        if score >= TR_KEEP_THRESHOLD or relevance_priority:
            return "raw"
        if score < TR_DIGEST_THRESHOLD and represented and not referenced:
            return "one_liner"
        return "digest"

    def record_state(self, instance_id: str, state: str) -> None:
        self._render_state[instance_id] = state

    def state_of(self, instance_id: str) -> str:
        return self._render_state.get(instance_id, "raw")

    @staticmethod
    def image_state(age: int, *, user_anchor: bool = False) -> str:
        if age <= IMG_KEEP_AGE or user_anchor:
            return "raw"
        return "evicted"

    @staticmethod
    def image_placeholder(instance_id: str, companion_text: str, ref: str = "") -> str:
        head = (companion_text or "")[:300].replace("\n", " ").strip()
        tail = f": screenshot context — {head}" if head else ""
        return f"[image evicted by CMI{ref} @instance {instance_id}{tail}]"

    def stats(self) -> Dict[str, int]:
        counts: Dict[str, int] = {"raw": 0, "digest": 0, "one_liner": 0}
        for state in self._render_state.values():
            counts[state] = counts.get(state, 0) + 1
        return counts

    # -- Tool-call ARGUMENT degradation (v1.1) -------------------------------

    def should_truncate_tool_call_args(
        self, tool_name: str, args: Dict[str, Any], *, age: int, user_anchor: bool,
        referenced: bool,
    ) -> bool:
        """Whether a content-bearing tool CALL argument is safe to shrink.

        Only applies to write_file/patch-family calls whose payload is a
        pure duplicate of what is now on disk. Never touches the tool_call id
        or name — only the argument text — so tool_call/tool_result pairing
        is untouched (spec §9 invariant). A referenced call (later turns
        quote it) or a recent/active one is never touched.
        """
        if user_anchor or age <= TOOL_ARGS_RECENT_AGE or referenced:
            return False
        name = (tool_name or "").lower()
        name = name.split("__")[-1] if "__" in name else name
        if name not in CONTENT_BEARING_ARG_TOOLS:
            return False
        return self._payload_size(args) >= TOOL_ARGS_MIN_CHARS

    @staticmethod
    def _payload_size(args: Dict[str, Any]) -> int:
        if not isinstance(args, dict):
            return 0
        n = 0
        for key in _CONTENT_ARG_KEYS + _MORE_ARG_KEYS:
            v = args.get(key)
            if isinstance(v, str):
                n += len(v)
            elif key == "edits" and isinstance(v, list):
                n += len(json.dumps(v, ensure_ascii=False))
        return n

    @staticmethod
    def _extract_content_payload(args: Dict[str, Any]) -> str:
        if not isinstance(args, dict):
            return ""
        for key in _CONTENT_ARG_KEYS:
            v = args.get(key)
            if isinstance(v, str) and v:
                return v
        return ""

    def render_tool_call_args_digest(
        self, instance_id: str, *, tool_name: str, args: Dict[str, Any], stash=None,
    ) -> Dict[str, Any]:
        """Return a NEW args dict with the content payload replaced by a
        compact, memoized reference (path + size) — never the tool_call
        structure itself. The written file is the durable source of truth
        on disk; this only removes the duplicate copy from context.
        """
        cache_key = _ck(f"args:{instance_id}", json.dumps(args, sort_keys=True, default=str))
        cached = self._digest_cache.get(cache_key)
        if cached is not None:
            try:
                return json.loads(cached)
            except Exception:
                pass
        out = dict(args)
        path = str(next((args[k] for k in ("path", "file_path", "file", "filename", "target_file")
                         if isinstance(args.get(k), str) and args[k]), "(unknown path)"))
        main_done = False
        for key in _CONTENT_ARG_KEYS + _MORE_ARG_KEYS:
            v = out.get(key)
            if key in _CONTENT_ARG_KEYS and main_done:
                continue  # one primary payload, as before (byte-stable stubs for existing sessions)
            if key == "edits" and isinstance(v, list) and v:
                v = json.dumps(v, ensure_ascii=False)
            if not (isinstance(v, str) and v) or len(v) < 80:
                continue
            ref = stash(v) if stash else ""
            if ref is None:  # could not keep the original: leave the payload untouched
                return dict(args)
            if key in _CONTENT_ARG_KEYS:
                out[key] = (
                    f"[CMI: content omitted — already written to {path}, "
                    f"{len(v):,} chars. File on disk is the source of truth.]{ref}"
                )
                main_done = True
            else:
                out[key] = f"[CMI: {key} omitted — {len(v):,} chars, applied to {path}.]{ref}"
        self._digest_cache[cache_key] = json.dumps(out, ensure_ascii=False)
        return out

    # -- Duplicate detection (v1.1) -------------------------------------------

    @staticmethod
    def content_hash(content: str) -> str:
        """Stable hash for exact-duplicate detection (not a security use)."""
        return hashlib.sha1((content or "").encode("utf-8", "replace"), usedforsecurity=False).hexdigest()

    def render_duplicate_reference(
        self, instance_id: str, *, first_instance_id: str, tool_name: str,
    ) -> str:
        cache_key = _ck(f"dup:{instance_id}", tool_name)
        cached = self._digest_cache.get(cache_key)
        if cached is not None:
            return cached
        text = f"[CMI dup] identical to an earlier [{tool_name}] result — content unchanged"
        self._digest_cache[cache_key] = text
        return text

    # -- Batch aggregation (v1.1) ---------------------------------------------

    def render_batch_summary(
        self, batch_id: str, *, items: "list[dict]",
    ) -> str:
        """items: list of {tool_name, tool_args, content, index} for a run of
        consecutive small same-tool results. Returns ONE compact multi-line
        summary attached to the FIRST item of the batch.

        Both the args preview AND the content preview are hard-capped
        (BATCH_MAX_TOOL_ARGS_ECHO / BATCH_MAX_CONTENT_ECHO): a batch exists
        specifically because individual items are too small to digest
        profitably, so the summary must compress harder than a naive full
        echo of every arg/result — otherwise N short items always produce a
        LONGER summary than the sum of the originals (their total is small
        precisely because they're tiny; padding each line with full JSON
        args erases the saving).
        """
        cache_key = _ck(f"batch:{batch_id}", json.dumps(items, sort_keys=True, default=str))
        cached = self._digest_cache.get(cache_key)
        if cached is not None:
            return cached
        tool_name = items[0]["tool_name"] if items else "tool"
        lines = [f"[CMI batch] {len(items)}x {tool_name}:"]
        for it in items:
            args_preview = (it.get("tool_args") or "").strip("{}\" ")[:BATCH_MAX_TOOL_ARGS_ECHO]
            content_preview = (it.get("content") or "").splitlines()[0][:BATCH_MAX_CONTENT_ECHO] \
                if it.get("content") else ""
            lines.append(f"-{args_preview}>{content_preview}")
        text = "\n".join(lines)
        self._digest_cache[cache_key] = text
        return text

    def render_batch_stub(self, instance_id: str, *, batch_id: str, position: int, ref: str = "") -> str:
        cache_key = _ck(f"batchstub:{instance_id}", batch_id, position, ref)
        cached = self._digest_cache.get(cache_key)
        if cached is not None:
            return cached
        text = f"[CMI batch item {position}]{ref} see batch summary above"
        self._digest_cache[cache_key] = text
        return text

    # -- Assistant narration (v1.1) --------------------------------------------

    @staticmethod
    def is_narration_profitable(text: str) -> bool:
        return len(text or "") >= NARRATION_MIN_CHARS

    def render_narration_stub(self, instance_id: str, *, original: str, ref: str = "") -> str:
        cache_key = _ck(f"narr:{instance_id}", original, ref)
        cached = self._digest_cache.get(cache_key)
        if cached is not None:
            return cached
        anchor = (original or "")[:NARRATION_MAX_KEEP_CHARS].rstrip()
        text = f"[CMI: narration omitted]{ref} {anchor}…" if anchor else f"[CMI: narration omitted]{ref}"
        self._digest_cache[cache_key] = text
        return text
