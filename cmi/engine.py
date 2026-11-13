"""CMIEngine — Context Manager Intelligence.

Compiles each outgoing request (select_context): what is sent is shortened, never lost. In Hermes,
full-window compaction is delegated to the built-in ContextCompressor; standalone (proxy, other
harnesses) there is no compactor. Provider/model-agnostic. No LLM call in select_context().
"""
from __future__ import annotations

import copy
import json
import os
import logging
import re
import sqlite3
import time
import weakref
from typing import Any, Dict, List, Optional, Tuple

from .compat import (
    HERMES, ContextEngine, coerce_positive_int, default_home,
    estimate_messages_tokens_rough,
)
from .capabilities import ModelCapabilities
from .lifecycle import ToolResultLifecycle, _tokens_rough
from .cost_guard import CostShadow
from .index import MIN_CHARS as INDEX_MIN_CHARS, OutputIndex
from .stats import StatsLedger, report as stats_report
from .versioning import running_version, stale_install
from .working_set import KnownIndex, WorkingSet, extract_paths, match_known
from .store import RecallStore

logger = logging.getLogger(__name__)

_STALE_WARNED = False


def _warn_if_stale_install() -> None:
    """Log once per process when the CMI on disk is not the one running (update without restart)."""
    global _STALE_WARNED
    if _STALE_WARNED:
        return
    disk = stale_install()
    if disk:
        _STALE_WARNED = True
        logger.warning("CMI %s is installed but this process still runs %s: restart Hermes to load it",
                       disk, running_version())

from .toolcalls import (  # noqa: F401 (re-exported: tests and plugins import them from here)
    _ID_TOKEN_RE, CMI_L1_MIN_PCT, CMI_L1_MAX_PCT, CMI_L2_MAX_PCT, CMI_L3_MAX_PCT, USER_TURN_WINDOW,
    CACHE_READ_COST, CACHE_WRITE_COST, CACHE_TTL_S, CACHE_TTL_MARGIN_S, TIER_PAYBACK_REQUESTS, STEP_EPOCH, DIGEST_MAX_KEEP_RATIO,
    _DIGEST_PREFIX_MAX_LEN, _KEY_ARGS, _summarize_with_key_arg, _READ_TOOLS, _WRITE_TOOLS,
    _EXIT_CODE_RE, _SHELL_TOOLS, _FAILED_RESULT_RE, _PATH_ARG_KEYS, SUPERSEDED_NOTE,
    _SNAPSHOT_TOOLS, _PIPE_TAIL_RE, command_key, _bare_tool_name, _path_arg, _SHELLS,
    _PATCH_FILE_RE, _command_arg, _write_paths, _fingerprint, _clone, _SHELL_SEP, _REDIRECT_RE,
    shell_written_paths, _call_label,
)

# Proactive lexical recall: at most this many tokens of old results restored per request, and a
# term found in more results than this is too common to point at one of them.
LEXICAL_RECALL_TOKENS = 12000
LEXICAL_MAX_RESULTS = 2
# Files the user names come back in full, newest first, up to this many tokens per request (the
# newest one always, whatever its size). Without a cap a message listing 125 paths kept all 125
# reads raw for the whole turn and CMI saved 0% (long live test, 2026-11-09).
try:
    HOT_FILE_TOKENS = int(os.environ.get("CMI_HOT_FILE_TOKENS", "32000"))
except ValueError:  # a bad value must not break the engine import
    HOT_FILE_TOKENS = 32000
_TERM_RE = re.compile(
    r"`([^`\n]{4,80})`"                               # `quoted code`
    r"|[\"«“]([^\"»”\n]{6,80})[\"»”]"                    # "quoted text"
    r"|\b([A-Za-z_][A-Za-z0-9]*(?:_[A-Za-z0-9]+)+)\b"   # snake_case / SCREAMING_CASE
    r"|\b([a-z]+[A-Z][A-Za-z0-9]*|[A-Z][a-z0-9]+[A-Z][A-Za-z0-9]*)\b"  # camelCase / PascalCase
    r"|\b(\d{5,})\b"                                  # ids, ports, amounts
    r"|\b([A-Z][A-Z0-9]+-\d+)\b"                        # ticket ids: OPS-4471
    r"|(?<![\w/])(/[A-Za-z][\w.-]*(?:/[\w.-]+)*)"      # URL / file paths: /checkout
)


def distinctive_terms(text: str) -> List[str]:
    out = []
    for m in _TERM_RE.finditer(text or ""):
        t = next(g for g in m.groups() if g)
        if len(t) >= 5 and t not in out and t.count("/") < 8:
            out.append(t)
    return out[:20]


def turn_starts(msgs: List[Any], epoch: Optional[int] = None) -> List[int]:
    """Indices where an age unit begins: every real user message, plus (step aging) the assistant
    message that opens step ``epoch * k + 1`` of a user turn. Derived from history alone, so a
    start once present stays present and ages only grow."""
    epoch = STEP_EPOCH if epoch is None else epoch
    out: List[int] = []
    steps = 0
    for i, m in enumerate(msgs):
        if _is_user_turn(m):
            out.append(i)
            steps = 0
        elif isinstance(m, dict) and m.get("role") == "assistant":
            steps += 1
            if epoch > 0 and steps > 1 and (steps - 1) % epoch == 0 and out:
                out.append(i)
    return out


def _is_user_turn(m: Any) -> bool:
    """A real user turn. Anthropic's wire format carries tool results in role=user messages;
    counting those as turns would age everything by tool calls instead of by conversation."""
    if not isinstance(m, dict) or m.get("role") != "user":
        return False
    c = m.get("content")
    if isinstance(c, list):
        return not any(isinstance(p, dict) and p.get("type") == "tool_result" for p in c)
    return True


# Engines alive in this process. A host fork (Hermes background review, /btw) replays the parent's
# history in a NEW engine; without the parent's tier state it compiles differently and rewrites
# the whole cached prefix (measured: ~22% of all cache writes). See _adopt_parent_state.
_LIVE_ENGINES: "weakref.WeakSet[Any]" = weakref.WeakSet()

TIER_LINDY = float(os.environ.get("CMI_TIER_LINDY", "0.35"))  # 0.5 (replay 2026-11-04, 64 sessions) -> 0.35 (replay 2026-11-05, 9 long Claude Code sessions: no loss-making rewrite)
# Start-up phase: until a session has made this many requests CMI does not rewrite the provider cache
# (no tier, no refresh of stale reads or named files). A short session cannot repay a rewrite, and the forced refreshes
# ("quality beats cache") ignored the payback rule: in a replay of 26 real sessions three short ones
# (41-63 requests) cost +16% to +20% because of them. A cold cache is still compiled (rewriting is free).
# 100 -> 30 (2026-11-11, forced refreshes fixed in 1.11.6): replay of 305 real Hermes sessions, total modelled cost
# -27.1% -> -28.6%, tokens saved 31.2% -> 35.3% (GPT-6.1 30% -> 43%), both halves alike; confirmed on 81 unused
# sessions (-34.2% -> -35.0%). A few short sessions that stop right after a rewrite end above raw (0.06% of the total).
WARMUP_REQUESTS = int(os.environ.get("CMI_WARMUP_REQUESTS", "30"))
# Through the proxy (Claude Code, Codex: 1-hour cache, write = 2x input) a rewrite is dearer and sessions of
# 100-130 requests are common: replay of 14 real Claude Code sessions, 4 cost more with a 100-request wait
# (+3% to +12%), none with 150 (those sessions are then left untouched). 150 -> 30 (2026-11-11, after the 1.11.6
# forced-refresh fix): replay of 35 real Claude Code sessions, none above raw, 10 cheaper, 1 within 0.1 pt;
# sessions of 107-130 requests go from untouched to -11%..-32%.
PROXY_TIER_LINDY = float(os.environ.get("CMI_PROXY_TIER_LINDY", "0.2"))  # 0.35 left a 538-request Claude Code session at +3.3% cost, 0.2: -0.2%
PROXY_WARMUP_REQUESTS = int(os.environ.get("CMI_PROXY_WARMUP_REQUESTS", "30"))
SUPERSEDED_FORCES_REFRESH = os.environ.get("CMI_SUPERSEDED_REFRESH", "0") == "1"


def _hermes_cache_ttl(default: float) -> float:
    """Hermes' prompt_caching.cache_ttl ('5m' or '1h'); the default if unreadable."""
    try:
        from hermes_cli.config import load_config_readonly
        ttl = str(((load_config_readonly() or {}).get("prompt_caching") or {}).get("cache_ttl") or "")
    except Exception:
        return default
    return {"5m": 300.0, "1h": 3600.0}.get(ttl.strip().lower(), default)


class CMIEngine(ContextEngine):
    """Deterministic context selection + delegated compaction."""

    @property
    def cache_ttl_s(self) -> float:
        return self._cache_ttl_s

    @cache_ttl_s.setter
    def cache_ttl_s(self, seconds: float) -> None:
        # The TTL decides both when the cache is cold and what a cache write costs (1 h = 2x input
        # on Anthropic, 5 min = 1.25x): a tier priced at the 5-minute rate under a 1-hour cache
        # under-estimates its rewrite by ~65% (seen live on Claude Code, which asks for 1 h).
        self._cache_ttl_s = float(seconds)
        cost = self.__dict__.get("cost")
        if cost is not None:
            cost.set_cache_ttl(self._cache_ttl_s)

    def __init__(self, *, auto_project_binding=None) -> None:
        # ABC-required token state
        self.last_prompt_tokens: int = 0
        self.last_completion_tokens: int = 0
        self.last_total_tokens: int = 0
        self.threshold_tokens: int = 0
        self.context_length: int = 0
        self.compression_count: int = 0
        self.threshold_percent: float = 0.75
        self.protect_first_n: int = 3
        self.protect_last_n: int = 20
        self.model_thresholds: dict = {}
        self._config_threshold_percent: float = 0.75

        # CMI Context State (provider-agnostic)
        self.capabilities: Optional[ModelCapabilities] = None
        self.working_set = WorkingSet()
        self.lifecycle = ToolResultLifecycle()
        self._model = ""
        self._provider = ""
        self._session_id = ""
        self.stats = StatsLedger()
        self.cost = CostShadow()
        self.index = OutputIndex()
        self._indexed_upto = 0
        self._req_fps = None
        self._flushed = (0, 0, 0, 0, 0, 0.0, 0.0)
        self._base_url = ""
        self._api_key = ""
        self._provider = ""
        self._api_mode = ""
        self._image_evicted_any = False
        self._image_counted: set = set()
        self._learned_upto = 0
        self._known_index: Optional[KnownIndex] = None
        # Cache tiers (v1.4): on for providers with prompt caching; see _apply_cache_tiers.
        self.cache_mode = True
        # False inside Hermes (the cmi_recall tool exists); True behind the proxy, where the
        # model can only get originals back by reading the file the stub points to.
        self.recall_via_file = False
        self._sent: Dict[Tuple[int, int], Optional[Dict[str, Any]]] = {}
        self._frozen_upto = 0
        # Cold-cache detection: wall time of the previous compiled request (injectable clock).
        self._clock = time.time
        self.cache_ttl_s = CACHE_TTL_S
        self._last_req_t: Optional[float] = None
        self._cold = False
        self.tier_lindy = TIER_LINDY
        self.warmup_requests = WARMUP_REQUESTS  # the proxy raises it (PROXY_WARMUP_REQUESTS)
        self._requests = 0
        self._last_fps: List[int] = []
        self._adopted = False
        _LIVE_ENGINES.add(self)
        self._superseded_now: Dict[int, str] = {}
        self._must_refresh: set = set()
        # Subset of _must_refresh forced by the opt-in CMI_SUPERSEDED_REFRESH: these replace even a
        # rendering sent in full (the full text is now wrong); the others only restore content.
        self._superseded_forced: set = set()
        self._hot_hit = False
        self._hot_hits: set = set()
        # Every original the engine takes out of a request is kept verbatim here; its key goes
        # into the stub, so nothing the model saw is ever lost (see store.py).
        self.store = RecallStore()
        self._DIGEST_WRAPPER_OVERHEAD = _DIGEST_PREFIX_MAX_LEN
        self._project = None
        self._project_binding_initialized = False
        from .project_binding import auto_binding_enabled
        self._auto_project_binding = (auto_binding_enabled() if auto_project_binding is None
                                      else auto_project_binding is True)
        self._project_optout = False
        self._project_home = None
        self._project_automatic = False
        self._auto_binding_session_id = None
        self._policy_mode = None
        self._policy_pending = False
        self._policy_reconstruction = False
        self._compression_sessions = []

        # Delegated compactor
        self._compactor: Optional[Any] = None  # Hermes ContextCompressor, when hosted by Hermes

        # Telemetry
        self.telemetry: Dict[str, Any] = {
            "raw_context_tokens": 0,
            "compiled_context_tokens": 0,
            "tokens_saved": 0,
            "digest_count": 0,
            "one_liner_count": 0,
            "image_evictions": 0,
            "tool_result_reductions": {},
            "compression_count": 0,
            "select_calls": 0,
            "select_none_returns": 0,
            "cache_hit_ratio": None,
        }

    def bind_project(self, project_id, *, home=None, _automatic=False):
        """Explicit policy changes are durable once the real host row exists."""
        self._project_binding_initialized = True
        if self._project is not None:
            self._project.sources = {}
            self._project.query = ""
        self._project = None
        self._project_optout = True
        self._project_automatic = False
        self._project_home = home or self._project_home or default_home()
        if not _automatic and project_id is not None:
            self._auto_binding_session_id = self._session_id
        if not project_id:
            self._policy_reconstruction = False
            self._policy_mode = "off"
            self._policy_pending = True
            self._save_policy()
            return
        # Construct locally before committing one final policy. Never expose a
        # temporary off row to independently running descendants.
        from .project_bridge import ProjectBridge
        self._project = ProjectBridge(self._project_home, project_id, self._session_id, self._model)
        self._project_optout = False
        self._project_automatic = _automatic
        self._policy_reconstruction = _automatic
        self._policy_mode = "auto" if _automatic else "explicit"
        self._policy_pending = True
        self._save_policy()
        if not _automatic and self._auto_project_binding and self._session_id:
            self._try_auto_bind()

    def _save_policy(self):
        if self._policy_pending and self._session_id:
            from .project_policy import persist
            home = self._project_home or default_home()
            anchor = self._auto_binding_session_id
            if (anchor and anchor != self._session_id and self._policy_mode == "off"
                    and not self._policy_reconstruction):
                # Compression is the same owner, not delegation: revoke its
                # original anchor too so a resumed owner cannot revive scope.
                persist(home, anchor, "off")
            if persist(home, self._session_id, self._policy_mode,
                       self._project.project if self._project else None,
                       only_if_absent=self._policy_reconstruction,
                       continuation_owner=(self._compression_sessions[-2]
                                           if self._policy_reconstruction and len(self._compression_sessions) >= 2
                                           else None)):
                self._policy_pending = False

    def _disable_project_locally(self):
        """Failure scrubs scope; it is not a user-issued durable opt-out."""
        from .project_policy import invalidate_auto
        invalidate_auto(self._project_home or default_home(),
                        [self._session_id, self._auto_binding_session_id])
        if self._project is not None:
            self._project.sources = {}
            self._project.query = ""
        self._project = None
        self._project_optout = True
        self._project_automatic = False
        self._policy_mode = "off"
        self._policy_pending = False

    def _try_auto_bind(self):
        self._save_policy()
        if not self._auto_project_binding or self._project_optout:
            return
        if self._project is not None and not self._project_automatic and self._policy_pending:
            # Legacy explicit standalone use has no host lineage to revalidate;
            # a known host session cannot bypass an unavailable policy store.
            from .project_binding import readonly
            from contextlib import closing
            from pathlib import Path
            try:
                with closing(readonly(Path(self._project_home or default_home()) / "state.db")) as db:
                    if not db.execute("SELECT 1 FROM sessions WHERE id=?",
                                      (self._auto_binding_session_id or self._session_id,)).fetchone():
                        return
            except FileNotFoundError:
                return
            except (sqlite3.Error, OSError, ValueError, TypeError):
                self._disable_project_locally()
                return
        from .project_policy import effective_project
        decision = effective_project(self._project_home or default_home(),
                                     self._auto_binding_session_id or self._session_id,
                                     bootstrap=self._policy_mode is None, details=True,
                                     continuations=self._compression_sessions)
        project = decision[0] if decision else None
        if project is None and self._project is None:
            from .project_binding import lineage
            try:
                if lineage(self._project_home or default_home(), self._session_id):
                    self._disable_project_locally()
            except (sqlite3.Error, OSError, ValueError, TypeError):
                pass
        if self._project is not None:
            if project == self._project.project:
                return
            self._disable_project_locally()
            return
        if project:
            try:
                if decision[2]:
                    # Reconstruction is not an explicit policy mutation.
                    from .project_bridge import ProjectBridge
                    self._project = ProjectBridge(self._project_home, project, self._session_id, self._model)
                    self._policy_mode = decision[1]
                    self._project_automatic = decision[1] == "auto"
                    self._policy_pending = False
                else:
                    self.bind_project(project, home=self._project_home, _automatic=True)
                if self._policy_pending or effective_project(
                        self._project_home, self._auto_binding_session_id or self._session_id,
                        continuations=self._compression_sessions
                ) != project:
                    self._disable_project_locally()
            except (OSError, ValueError, sqlite3.Error):
                logger.warning("CMI automatic project storage unavailable", exc_info=True)
                self._disable_project_locally()

    # Core's live compression-config sync (tui_gateway/session_compression.py) calls this
    # on whatever object sits at agent.context_compressor, assuming a ContextCompressor.
    # When CMI is the active context engine it occupies that slot too, so it must expose
    # the same normalization helper (identical semantics: positive int or None).
    _coerce_threshold_tokens_cap = staticmethod(coerce_positive_int)

    @property
    def name(self) -> str:
        return "cmi"

    def on_session_start(self, session_id: str, **kwargs: Any) -> None:
        from pathlib import Path

        _warn_if_stale_install()
        project_id = kwargs.get("project_id", os.environ.get("CMI_PROJECT_ID"))
        project_home = kwargs.get("hermes_home")
        inherited_anchor = self._auto_binding_session_id or self._session_id
        previous_session_id = self._session_id
        has_project_hint = "project_id" in kwargs or "CMI_PROJECT_ID" in os.environ
        if (previous_session_id and kwargs.get("boundary_reason") == "compression"
                and kwargs.get("old_session_id") == previous_session_id
                and ("hermes_home" not in kwargs or
                     (project_home is not None and self._project_home is not None
                      and Path(project_home) == Path(self._project_home)))
                and (not has_project_hint or
                     (self._project is not None and project_id == self._project.project))):
            # Compression is a continuation, not a new source of binding authority.
            project_home = self._project_home or default_home()
            self._compression_sessions.extend([previous_session_id, session_id])
            self._session_id = session_id or ""
            self._auto_binding_session_id = inherited_anchor
            self._project_home = project_home
            if self._project is not None:
                self._project.session = self._session_id
                self._project.sources = {}
                self._project.query = ""
            self._policy_pending = self._policy_mode is not None
            self._policy_reconstruction = True
            self._try_auto_bind()
        else:
            self._compression_sessions = []
            self._session_id = session_id or ""
            self._auto_binding_session_id = self._session_id
            self._project_home = project_home or default_home()
            if has_project_hint:
                self.bind_project(project_id, home=project_home)
            elif self._auto_project_binding and not self._project_optout:
                if self._project is not None:
                    self._project.sources = {}
                    self._project.query = ""
                self._project = None
                self._project_automatic = False
                # A different owner must resolve its own durable policy; the
                # previous owner's mode is not authority for this session.
                self._policy_mode = None
                self._policy_pending = False
                self._try_auto_bind()
            else:
                self.bind_project(None, home=project_home)
        # Continuation must still initialize storage and run retention under
        # the resolved home, rather than skipping the session-start lifecycle.
        home = self._project_home
        if home:
            self.store.set_root(Path(home) / "cmi_store")
            self.store.prune()
            self.stats.set_path(Path(home) / "cmi_stats.jsonl")
            self.index.set_path(Path(home) / "cmi_index.db")

    def stats_report(self) -> str:
        return stats_report(self._stats_ledger(), self._session_id, self._pending_stats())

    def _stats_ledger(self) -> StatsLedger:
        if self.stats.path is None:
            try:
                from hermes_constants import get_hermes_home
                self.stats.set_path(get_hermes_home() / "cmi_stats.jsonl")
            except Exception:
                pass
        return self.stats

    def _pending_stats(self) -> Dict[str, Any]:
        t = self.telemetry
        return {"s": self._session_id, "m": self._model, "p": self._provider,
                "raw": t["raw_context_tokens"] - self._flushed[0],
                "out": t["compiled_context_tokens"] - self._flushed[1],
                "n": t["select_calls"] - self._flushed[2],
                "rc": t.get("recalls", 0) - self._flushed[3],
                "lx": t.get("lexical_recalls", 0) - self._flushed[4],
                "cr": self.cost.raw.cost - self._flushed[5],
                "cc": self.cost.cmi.cost - self._flushed[6]}

    def _flush_stats(self) -> None:
        d = self._pending_stats()
        if d["n"] > 0:
            self._stats_ledger().add(d["s"], d["m"], d["raw"], d["out"], d["n"], d["rc"], d["lx"], d["p"],
                                     cost_raw=d["cr"], cost_cmi=d["cc"])
        t = self.telemetry
        self._flushed = (t["raw_context_tokens"], t["compiled_context_tokens"], t["select_calls"],
                         t.get("recalls", 0), t.get("lexical_recalls", 0), self.cost.raw.cost, self.cost.cmi.cost)

    # -- Recall tool ----------------------------------------------------------

    def get_tool_schemas(self) -> List[Dict[str, Any]]:
        # Hermes asks for schemas before on_session_start; host env is an explicit opt-in.
        if not self._project_binding_initialized and os.environ.get("CMI_PROJECT_ID"):
            self.bind_project(os.environ["CMI_PROJECT_ID"])
        schemas = [{
            "name": "cmi_recall",
            "description": (
                "Get back, verbatim, content that was shortened in this conversation to save "
                "context. Stubs marked [CMI ...] carry a key like cmi:1a2b3c4d5e; pass it here "
                "to see exactly what was there (file read, command output, written file "
                "content, screenshot text). Cheaper and more faithful than re-running the tool. "
                "Keys are valid in every session and subagent of this profile: when delegating, "
                "put the relevant cmi: keys in the subagent's context instead of pasting or "
                "re-fetching the content."
            ),
            "parameters": {
                "type": "object",
                "properties": {"key": {"type": "string", "description": "The cmi:<key> shown in the stub."}},
                "required": ["key"],
            },
        }, {
            "name": "cmi_search",
            "description": (
                "Search the exact tool outputs (command logs, file reads, API responses, web pages) "
                "of PAST sessions of this profile, which normal session search does not index. "
                "Use it when the user refers to something seen before (\"the error from yesterday\", "
                "\"the p99 we measured last week\") instead of re-running or guessing. Returns "
                "matches with a cmi: key; pass the key to cmi_recall for the full verbatim output."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Words, identifiers or fragments to find."},
                    "limit": {"type": "integer", "description": "Max results (default 5, max 10)."},
                },
                "required": ["query"],
            },
        }]
        if self._project is not None or self._auto_project_binding:
            from .project_bridge import ProjectBridge
            schemas.extend(ProjectBridge.schemas())
        return schemas

    def handle_tool_call(self, name: str, args: Dict[str, Any], **kwargs: Any) -> str:
        if name.startswith("cmi_project_"):
            self._try_auto_bind()
            if self._project is None:
                return json.dumps({"error": "Project memory is not enabled for this session"})
            self._project.model = self._model
            return json.dumps(self._project.handle(name, args or {}), ensure_ascii=False)
        if name == "cmi_search":
            return self._search_tool(args or {})
        if name != "cmi_recall":
            return json.dumps({"error": f"Unknown context engine tool: {name}"})
        key = str((args or {}).get("key") or "")
        content = self.store.get(key)
        if content is None:
            img = self.store.image_path(key)
            if img is not None:
                self.telemetry["recalls"] = self.telemetry.get("recalls", 0) + 1
                return f"{key} is an image, saved at {img}. Open that file with a tool that can view images."
            return json.dumps({"error": f"No stored content for {key!r}. Re-run the original tool instead."})
        self.telemetry["recalls"] = self.telemetry.get("recalls", 0) + 1
        return content

    def _search_tool(self, args: Dict[str, Any]) -> str:
        self._ensure_paths()
        try:
            limit = max(1, min(int(args.get("limit") or 5), 10))
        except (TypeError, ValueError):
            limit = 5
        exists = lambda k: self.store.get(k) is not None
        hits = self.index.search(str(args.get("query") or ""), limit=limit, exists=exists)
        self.telemetry["searches"] = self.telemetry.get("searches", 0) + 1
        if not hits:
            return json.dumps({"results": [], "note": "No past tool output matches. Try other words or fragments."})
        import datetime
        for h in hits:
            h["key"] = f"cmi:{h['key']}"
            h["when"] = datetime.datetime.fromtimestamp(h.pop("ts")).strftime("%Y-%m-%d %H:%M")
            h["this_session"] = h["session"] == self._session_id
        return json.dumps({"results": hits, "next": "cmi_recall(key) returns the full output."},
                          ensure_ascii=False)

    def _ensure_paths(self) -> None:
        from pathlib import Path
        home = Path(default_home())
        if self.store.root is None:
            self.store.set_root(home / "cmi_store")
        if self.index.path is None:
            self.index.set_path(home / "cmi_index.db")

    def _index_new_outputs(self, messages: List[Dict[str, Any]]) -> None:
        """Store + index every tool output of the turn that just completed, with the call that
        produced it. Outputs CMI already stashed share the same key (content-addressed)."""
        start = self._indexed_upto if len(messages) >= self._indexed_upto else 0
        self._indexed_upto = len(messages)
        new = [(i, m) for i, m in enumerate(messages) if i >= start and isinstance(m, dict)
               and m.get("role") == "tool"]
        if not new:
            return
        self._ensure_paths()
        for i, m in new:
            text = self._content_text(m)
            if len(text) < INDEX_MIN_CHARS or text.startswith("[CMI"):
                continue
            name, args = self._lookup_caller(messages, i, str(m.get("tool_call_id") or ""))
            key = self.store.put(text)
            if key:
                label = _call_label(name, args)
                self.index.add(key, text, session=self._session_id, tool=_bare_tool_name(name), label=label)

    def on_session_reset(self) -> None:
        super().on_session_reset()
        self._reset_session_state()
        self._reset_session_telemetry()

    def on_session_end(self, session_id: str, messages: List[Dict[str, Any]]) -> None:
        super().on_session_end(session_id, messages)
        self._flush_stats()
        self._reset_session_state()

    def _reset_session_state(self) -> None:
        """Per-session state; a long-lived process reuses the engine across sessions."""
        if self._project is not None:
            self._project.sources = {}
            self._project.query = ""
        self.working_set.reset()
        self.lifecycle.reset()
        self._image_counted = set()
        self._image_evicted_any = False
        self._learned_upto = 0
        self._sent = {}
        self._frozen_upto = 0
        self._last_req_t = None
        self._cold = False
        self._requests = 0
        self._last_fps = []
        self._adopted = False
        self._indexed_upto = 0
        self.cost.reset()

    def _reset_session_telemetry(self) -> None:
        self._flush_stats()
        for k in ("raw_context_tokens", "compiled_context_tokens", "tokens_saved",
                  "digest_count", "one_liner_count", "image_evictions",
                  "compression_count", "select_calls", "select_none_returns",
                  "recalls", "lexical_recalls"):
            self.telemetry[k] = 0
        self._flushed = (0, 0, 0, 0, 0, self.cost.raw.cost, self.cost.cmi.cost)
        self.telemetry["tool_result_reductions"] = {}
        self.telemetry["cache_hit_ratio"] = None

    # -- Model binding --------------------------------------------------------

    def update_model(
        self,
        model: str,
        context_length: int,
        base_url: str = "",
        api_key: str = "",
        provider: str = "",
        api_mode: str = "",
    ) -> None:
        self._model = model
        if self._project is not None:
            self._project.model = model
        self._provider = provider or ""
        self.cost.set_pricing(model, self._provider)
        self._base_url = base_url
        self._api_key = api_key
        self._provider = provider
        self._api_mode = api_mode

        if not hasattr(self, "_config_threshold_percent"):
            self._config_threshold_percent = self.threshold_percent
        self._base_threshold_percent = self._config_threshold_percent
        if HERMES:
            try:  # Hermes-internal helper: keep the configured threshold if it moves or breaks
                from agent.context_compressor import resolve_model_threshold
                self._base_threshold_percent = resolve_model_threshold(
                    model, getattr(self, "model_thresholds", {}), self._config_threshold_percent)
            except Exception:
                logger.warning("cmi: resolve_model_threshold unavailable; using configured threshold")
        self.threshold_percent = self._base_threshold_percent
        if HERMES:
            self.cache_ttl_s = _hermes_cache_ttl(self.cache_ttl_s)
        self.context_length = int(context_length or 0)
        self.threshold_tokens = int(self.context_length * self.threshold_percent)

        self.capabilities = ModelCapabilities.resolve(
            model, self.context_length,
            base_url=base_url, api_key=api_key, provider=provider,
        )

        if not HERMES:
            self._compactor = None
            return
        try:
            from agent.context_compressor import ContextCompressor
            self._compactor = ContextCompressor(
                model=model,
                base_url=base_url,
                api_key=api_key,
                provider=provider,
                api_mode=api_mode,
            )
            self._compactor.context_length = self.context_length
            self._compactor.threshold_percent = self.threshold_percent
            self._compactor.threshold_tokens = self.threshold_tokens
        except Exception:
            logger.warning("CMI: compactor init failed; compaction will fail-open", exc_info=True)
            self._compactor = None

    # -- Token tracking -------------------------------------------------------

    def update_from_response(self, usage: Dict[str, Any]) -> None:
        self.last_prompt_tokens = int(usage.get("prompt_tokens") or 0)
        self.last_completion_tokens = int(usage.get("completion_tokens") or 0)
        self.last_total_tokens = int(usage.get("total_tokens") or 0)
        cache_read = usage.get("cache_read_tokens") or 0
        total_input = usage.get("input_tokens") or self.last_prompt_tokens
        if total_input:
            self.telemetry["cache_hit_ratio"] = cache_read / total_input
        if self._compactor is not None:
            try:
                self._compactor.update_from_response(usage)
            except Exception:
                pass

    # -- Compaction (full delegation) -----------------------------------------

    def should_compress(self, prompt_tokens: Optional[int] = None) -> bool:
        if self._compactor is not None:
            try:
                return self._compactor.should_compress(prompt_tokens)
            except Exception:
                pass
        tokens = prompt_tokens if prompt_tokens is not None else self.last_prompt_tokens
        return bool(self.threshold_tokens and tokens >= self.threshold_tokens)

    def should_compress_info(
        self, prompt_tokens: Optional[int] = None,
    ) -> "tuple[bool, str | None]":
        if self._compactor is not None:
            try:
                return self._compactor.should_compress_info(prompt_tokens)
            except Exception:
                pass
        return self.should_compress(prompt_tokens), None

    def compress(
        self,
        messages: List[Dict[str, Any]],
        current_tokens: Optional[int] = None,
        focus_topic: Optional[str] = None,
        force: bool = False,
        memory_context: str = "",
    ) -> List[Dict[str, Any]]:
        if self._compactor is None:
            return messages
        effective_focus = focus_topic
        if not effective_focus:
            top = self.working_set.top(10)
            if top:
                effective_focus = "active files: " + ", ".join(p for p, _ in top)
        try:
            out = self._compactor.compress(
                messages,
                current_tokens=current_tokens,
                focus_topic=effective_focus,
                force=force,
                memory_context=memory_context,
            )
        except Exception:
            logger.warning("CMI: delegated compression failed; returning input", exc_info=True)
            return messages
        self.compression_count += 1
        self.telemetry["compression_count"] += 1
        self.lifecycle.on_compaction_boundary()
        return out

    def prune_tool_results_only(
        self, messages: List[Dict[str, Any]], current_tokens: int | None = None,
    ) -> tuple[List[Dict[str, Any]], int]:
        if self._compactor is not None:
            try:
                return self._compactor.prune_tool_results_only(messages, current_tokens)
            except Exception:
                pass
        return messages, 0

    # -- SELECTION core -------------------------------------------------------

    def select_context(  # type: ignore[override]
        self,
        request_messages: List[Dict[str, Any]],
        *,
        conversation_messages: Optional[List[Dict[str, Any]]] = None,
        incoming_message: Optional[Dict[str, Any]] = None,
        budget_tokens: int = 0,
    ) -> Optional[List[Dict[str, Any]]]:
        self.telemetry["select_calls"] += 1
        try:
            self._try_auto_bind()
            if self._project is not None:
                self._project.model = self._model
                self._project.capture(conversation_messages or request_messages)
            result = self._select_impl(
                request_messages,
                conversation_messages=conversation_messages,
                incoming_message=incoming_message,
                budget_tokens=budget_tokens,
            )
            if self._project is not None:
                base = result if result is not None else request_messages
                byte_budget = 4000
                if budget_tokens:
                    # UTF-8 bytes are a conservative bound for the injected text; reserve message framing.
                    available = budget_tokens - estimate_messages_tokens_rough(base) - 64
                    byte_budget = max(0, min(byte_budget, available))
                prepared = self._project.prepare(base, max_bytes=byte_budget)
                if prepared is not base:
                    added = estimate_messages_tokens_rough(prepared) - estimate_messages_tokens_rough(base)
                    self.telemetry["compiled_context_tokens"] += added
                    self.telemetry["project_memory_tokens"] = self.telemetry.get("project_memory_tokens", 0) + added
                    self.telemetry["project_retrievals"] = self.telemetry.get("project_retrievals", 0) + 1
                    result = prepared
            try:
                self._observe_cost(request_messages, result)
            except Exception:
                logger.debug("CMI cost shadow failed", exc_info=True)
        except Exception:
            logger.warning("CMI select_context failed; failing open", exc_info=True)
            self.telemetry["select_none_returns"] += 1
            return None
        if result is None:
            self.telemetry["select_none_returns"] += 1
        return result

    def _observe_cost(self, msgs, out) -> None:
        tok = lambda m: estimate_messages_tokens_rough([m])
        fps = self._req_fps if self._req_fps is not None and len(self._req_fps) == len(msgs) else [_fingerprint(m) for m in msgs]
        toks = [tok(m) for m in msgs]
        cold = self._cold
        if out is None:
            self.cost.observe(fps, toks, fps, toks, cold=cold)
            return
        ofps = [fps[i] if i < len(msgs) and o is msgs[i] else _fingerprint(o) for i, o in enumerate(out)]
        otoks = [toks[i] if i < len(msgs) and o is msgs[i] else tok(o) for i, o in enumerate(out)]
        self.cost.observe(fps, toks, ofps, otoks, cold=cold)

    def _select_impl(
        self,
        request_messages: List[Dict[str, Any]],
        *,
        conversation_messages: Optional[List[Dict[str, Any]]],
        incoming_message: Optional[Dict[str, Any]],
        budget_tokens: int,
    ) -> Optional[List[Dict[str, Any]]]:
        msgs = request_messages
        self._req_fps = None
        raw_tokens = estimate_messages_tokens_rough(msgs)

        last_user_idx = -1
        for i in range(len(msgs) - 1, -1, -1):
            if _is_user_turn(msgs[i]):
                last_user_idx = i
                break

        # Age units (user turns + step epochs) and the horizon: the start of the current unit.
        # Everything before the horizon is "completed" for every pass below; the user's own
        # message (hot files, lexical recall) is still read at last_user_idx.
        starts = turn_starts(msgs)
        self._starts = set(starts)
        user_idx = last_user_idx
        last_user_idx = max(starts[-1], user_idx) if starts else user_idx
        self._user_idx = user_idx

        self._known_index = KnownIndex(self.working_set.known_files())
        tool_results = self._index_tool_results(msgs, last_user_idx)
        hot_files = KnownIndex(self._hot_files(msgs, user_idx))
        self._superseded_now = self._superseded(msgs, last_user_idx)
        self._lexical_hits = self._lexical_recall(msgs, user_idx, tool_results, horizon=last_user_idx)
        self._hot_hits = self._hot_results(msgs, tool_results, hot_files, horizon=last_user_idx)

        # A write that put CMI's short form in a file is flagged first, whatever else happens.
        new_list: Optional[List[Dict[str, Any]]] = self._flag_stub_writes(msgs)
        flagged = new_list is not None
        # A read made obsolete by a later edit is re-rendered at the next tier or cold cache, not
        # forced mid-cache: the raw history keeps it too, and the later edit is visible after it.
        self._superseded_forced = ({i for i in self._superseded_now if i < last_user_idx}
                                   if SUPERSEDED_FORCES_REFRESH else set())
        self._must_refresh = set(self._superseded_forced)

        for idx, info in tool_results.items():
            state = self._decide_render(idx, info, msgs, last_user_idx, hot_files)
            if self._hot_hit:
                self._must_refresh.add(idx)
            prev = self.lifecycle.state_of(info.instance_id)
            if state != "raw" or prev != "raw":
                if new_list is None:
                    new_list = [m for m in msgs]
                if state == "raw":
                    rendered = new_list[idx]
                else:
                    rendered = self._render_tool_result(new_list[idx], info, state)
                    # Absolute backstop: never apply a rendering that grows
                    # the payload, regardless of upstream profitability math.
                    orig_len = len(self._content_text(new_list[idx]))
                    new_len = len(self._content_text(rendered))
                    # Quality floor: replacing content by a digest only pays when it removes most
                    # of it. A 420-char read squeezed to 377 loses the content to save 10%.
                    if new_len > orig_len * DIGEST_MAX_KEEP_RATIO:
                        state = "raw"
                        rendered = new_list[idx]
                if rendered is not new_list[idx]:
                    new_list[idx] = rendered
                self.lifecycle.record_state(info.instance_id, state)

        # Image eviction (independent)
        image_list = self._apply_images(new_list or msgs, last_user_idx)
        if image_list is not None:
            new_list = image_list

        # Tool-call ARGUMENT degradation (v1.1): shrinks write_file/patch
        # payloads once the write is old — independent of the tool RESULT
        # pass above (different axis: what the agent SENT vs what it GOT
        # BACK). Targets the write_file/patch argument gisement found on
        # code-heavy sessions.
        args_list = self._degrade_tool_call_args(new_list or msgs, last_user_idx)
        if args_list is not None:
            new_list = args_list

        # Exact-duplicate tool result collapsing (v1.1): a byte-identical
        # repeat (same command re-run, same search re-issued) carries zero
        # new information after the first occurrence.
        dedup_list = self._collapse_duplicate_results(new_list or msgs, tool_results, last_user_idx)
        if dedup_list is not None:
            new_list = dedup_list

        # Small-result batch aggregation (v1.1): individually non-profitable
        # tiny results (many short terminal/search calls) are folded into one
        # batch summary when several consecutive old same-tool ones appear.
        batch_list = self._aggregate_small_result_batches(new_list or msgs, tool_results, last_user_idx)
        if batch_list is not None:
            new_list = batch_list

        # Assistant narration compaction (v1.1): drops old lead-in prose that
        # accompanies a tool_call once the action itself is the record that
        # matters. Never touches a text-only (no tool_calls) final answer.
        narration_list = self._compact_old_narration(new_list or msgs, last_user_idx)
        if narration_list is not None:
            new_list = narration_list

        if self.cache_mode:
            new_list = self._apply_cache_tiers(msgs, new_list, last_user_idx)

        if new_list is None:
            self._record_telemetry(raw_tokens, raw_tokens, 0)
            return None

        # The estimator is a per-message sum, so only re-estimate the messages a pass replaced
        # (untouched entries are the caller's own objects, identity-shared with msgs).
        compiled_tokens = raw_tokens - sum(
            estimate_messages_tokens_rough([old]) - estimate_messages_tokens_rough([new])
            for old, new in zip(msgs, new_list) if old is not new
        )
        self._record_telemetry(raw_tokens, compiled_tokens, raw_tokens - compiled_tokens)
        if compiled_tokens >= raw_tokens and not flagged:
            # Every pass above only writes to new_list when its OWN edit is
            # locally profitable (each guards with len(replacement) <
            # len(original)). A net non-improvement here means estimator
            # rounding wiped out a tiny gain — safe to fail closed to None.
            # A stub-write warning is sent whatever it costs.
            return None
        return new_list

    # -- Cache tiers (v1.4) ----------------------------------------------------

    def _apply_cache_tiers(self, msgs, new_list, last_user_idx):
        """Keep the request prefix byte-stable between turns so the provider's prompt cache keeps
        hitting. Every change CMI makes to an old message rewrites the cache from that message on;
        doing it a little every turn means rewriting most of the context every turn.

        So compiled messages are only allowed to change in *tiers*:
          - Messages before ``_frozen_upto`` are sent exactly as the last time (the cached prefix).
          - Messages after it may be degraded as usual, except that each one, once sent, keeps
            that exact rendering until the next tier.
          - A new tier (re-compiling everything up to the current turn) happens at a turn
            boundary, only when the tokens it saves over the next turns outweigh rewriting the
            cache once: saved_per_request * EXPECTED_REQUESTS > (rewrite cost) / (read discount).

        Nothing changes for correctness: every message still maps 1:1, and anything degraded is
        still stored and recallable.
        """
        est = estimate_messages_tokens_rough
        now = self._clock()
        self._cold = (self._last_req_t is not None
                      and now - self._last_req_t > self.cache_ttl_s + CACHE_TTL_MARGIN_S)
        self._last_req_t = now
        self._requests += 1
        compiled = new_list if new_list is not None else list(msgs)
        sent = self._sent  # (position, fingerprint of source msg) -> rendering sent last time (None = raw)
        user_idx = getattr(self, "_user_idx", last_user_idx)
        at_turn_start = user_idx == len(msgs) - 1
        # A step epoch opened mid-turn (horizon past the user message) and has not been tiered
        # yet: the one other point where a re-tier may happen.
        at_epoch = last_user_idx > user_idx and last_user_idx > self._frozen_upto
        # Hermes hands over fresh structural clones on every request, so identity (id()) never
        # matches across requests; the content fingerprint does.
        fps = [_fingerprint(m) for m in msgs]
        self._req_fps = fps
        if not self._sent and self._requests == 1:
            self._adopt_parent_state(fps)
            sent = self._sent

        def previous(i):
            key = (i, fps[i])
            if key not in sent:
                return None
            r = sent[key]
            return msgs[i] if r is None else _clone(r)

        # Candidate: what the stable plan would send (no re-decision of already-sent messages).
        # Quality beats cache: a message the current turn needs in full (a file the user names)
        # or whose content is now wrong (a read superseded by a later edit) always takes its
        # fresh rendering, even if that rewrites the cache from there.
        warming = self._requests <= self.warmup_requests and not self._cold
        # While warming up nothing is refreshed: a session that may stop soon cannot repay a cache
        # rewrite, and CMI must never cost more than sending nothing. A shortened result stays
        # recoverable by its key (cmi_recall), also for a file the user names.
        must = set() if warming else self._must_refresh
        stable = list(compiled)
        for i in range(len(msgs)):
            p = previous(i)
            # A forced refresh restores content. A message already sent in full has nothing to
            # restore: a later pass shortening it (duplicate collapse) waits for a tier. An obsolete
            # read under the opt-in superseded refresh is the exception: its full text is now wrong.
            if i in must and (p is not msgs[i] or i in self._superseded_forced):
                continue
            if p is not None:
                stable[i] = p
            elif i < self._frozen_upto:
                stable[i] = msgs[i]  # never sent (e.g. a new session): leave it raw

        chosen = stable
        if self._adopted and not self._cold:
            pass  # a fork lives a few requests: keep the parent's cached prefix, never re-tier warm
        elif self._cold and any(a is not b for a, b in zip(compiled, stable)):
            # The provider cache expired: the whole prefix is written again whatever is sent,
            # so sending the fully compiled history costs nothing extra (a free tier).
            chosen = compiled
            self._frozen_upto = max(self._frozen_upto, last_user_idx)
            self.telemetry["tiers_cold"] = self.telemetry.get("tiers_cold", 0) + 1
        elif warming:
            pass  # start-up phase: keep what was already sent byte for byte
        elif (at_turn_start or at_epoch) and last_user_idx > 0:
            # Tokens a full re-tier would save per request compared to the stable plan.
            differs = [a is not b and _fingerprint(a) != _fingerprint(b) for a, b in zip(stable, compiled)]
            delta = sum(est([a]) - est([b]) for a, b, d in zip(stable, compiled, differs) if d)
            first_change = next((i for i, d in enumerate(differs) if d), None)
            if first_change is not None and delta > 0:
                # Everything from the first change to the end is re-written into the cache.
                rewrite = est(compiled[first_change:last_user_idx] if at_turn_start else compiled[first_change:])
                if self._tier_pays_back(delta, rewrite):
                    chosen = compiled
                    self._frozen_upto = last_user_idx
                    self.telemetry["tiers"] = self.telemetry.get("tiers", 0) + 1

        self._last_fps = fps
        # Stored as private copies: the host may decorate the returned dicts (cache markers).
        self._sent = {(i, fp): (None if r is m else _clone(r)) for i, (m, r, fp) in enumerate(zip(msgs, chosen, fps))}
        if all(a is b for a, b in zip(chosen, msgs)):
            return None
        return chosen

    def _adopt_parent_state(self, fps: List[int]) -> None:
        """First request of a fresh engine whose history starts with another live engine's last
        request (a fork of that conversation): send the parent's exact renderings so the provider
        cache keeps hitting. Content-matched (same message fingerprints), never by session id."""
        best = None
        for other in list(_LIVE_ENGINES):
            if other is self:
                continue
            prev = getattr(other, "_last_fps", None) or []
            if len(prev) >= 3 and len(prev) <= len(fps) and fps[:len(prev)] == prev:
                if best is None or len(prev) > len(best._last_fps):
                    best = other
        if best is None:
            return
        try:
            self._sent = dict(best._sent)
            self._frozen_upto = best._frozen_upto
            self._adopted = True
            self.telemetry["adopted_parent"] = 1
        except Exception:  # the parent is mid-request in another thread: start fresh
            self._sent, self._frozen_upto, self._adopted = {}, 0, False

    def _tier_pays_back(self, delta: int, rewrite: int) -> bool:
        """A tier rewrites the cache once from its first change (``rewrite`` tokens at the
        write-minus-read premium) and saves ``delta`` tokens on every later request. Pricing is
        the provider's own (no cache premium: always worth it). The fixed payback horizon was
        checked against alternatives on real sessions: a cumulative-cost brake and a
        session-length horizon both ended costlier, because a tier pays back over 100+ requests."""
        premium = self.cost.write - self.cost.read
        if premium <= 0:
            return True
        if self.tier_lindy <= 0:  # legacy rule (<= 1.11): fixed horizon, savings not priced as reads
            return delta * TIER_PAYBACK_REQUESTS >= rewrite * premium
        # Priced rule: removed tokens would have been cache reads on every later request; a
        # session that has run n requests is expected to run about n more (Lindy), so short
        # sessions do not pay for tiers they will never amortize.
        expected = self.tier_lindy * max(1, self._requests)
        return delta * self.cost.read * expected >= rewrite * premium

    # -- Helpers --------------------------------------------------------------

    def _index_tool_results(self, msgs, last_user_idx) -> Dict[int, Any]:
        starts = set(turn_starts(msgs))
        user_turn_seen = 0
        turn_at_idx: Dict[int, int] = {}
        for i, m in enumerate(msgs):
            if i in starts:
                user_turn_seen += 1
            turn_at_idx[i] = user_turn_seen
        total_turns = user_turn_seen
        callers = self._build_caller_index(msgs)

        results: Dict[int, Any] = {}
        for i, m in enumerate(msgs):
            if not isinstance(m, dict):
                continue
            role = m.get("role")
            tc_id = ""
            tool_name = ""
            tool_args = ""
            if role == "tool":
                tc_id = str(m.get("tool_call_id") or "")
                tool_name, tool_args = self._caller_of(callers, tc_id)
            elif role == "user" and isinstance(m.get("content"), list):
                for part in m.get("content", []):
                    if isinstance(part, dict) and part.get("type") == "tool_result":
                        tc_id = str(part.get("tool_use_id") or "")
                        tool_name, tool_args = self._caller_of(callers, tc_id)
                        break
            if not tc_id:
                continue
            age = max(0, total_turns - turn_at_idx[i])
            results[i] = _ToolResultInfo(
                instance_id=f"{i}:{tc_id}",
                tool_name=tool_name, tool_args=tool_args,
                age=age, msg_index=i,
            )
        return results

    @staticmethod
    def _build_caller_index(msgs) -> Dict[str, Tuple[str, Any, bool]]:
        """tool_call id -> (name, raw args, is_anthropic) in one O(n) pass (OpenAI tool_calls and
        Anthropic tool_use parts). Anthropic inputs are JSON-encoded lazily in _caller_of."""
        index: Dict[str, Tuple[str, Any, bool]] = {}
        for m in msgs:
            if not isinstance(m, dict):
                continue
            if m.get("role") == "assistant":
                for tc in m.get("tool_calls") or []:
                    if isinstance(tc, dict):
                        fn = tc.get("function") or {}
                        index[str(tc.get("id") or "")] = (str(fn.get("name") or ""), fn.get("arguments"), False)
            if isinstance(m.get("content"), list):
                for part in m["content"]:
                    if isinstance(part, dict) and part.get("type") == "tool_use":
                        index[str(part.get("id") or "")] = (str(part.get("name") or ""), part.get("input"), True)
        return index

    @staticmethod
    def _caller_of(index, tc_id) -> Tuple[str, str]:
        hit = index.get(tc_id)
        if hit is None:
            return "", ""
        name, args, is_anthropic = hit
        return name, (json.dumps(args or {}) if is_anthropic else str(args or ""))

    def _lookup_caller(self, msgs, result_idx, tc_id) -> Tuple[str, str]:
        for j in range(result_idx - 1, -1, -1):
            m = msgs[j]
            if not isinstance(m, dict):
                continue
            # OpenAI format
            if m.get("role") == "assistant":
                for tc in m.get("tool_calls") or []:
                    if not isinstance(tc, dict):
                        continue
                    if str(tc.get("id") or "") == tc_id:
                        fn = tc.get("function") or {}
                        return str(fn.get("name") or ""), str(fn.get("arguments") or "")
            # Anthropic format
            if isinstance(m.get("content"), list):
                for part in m.get("content", []):
                    if isinstance(part, dict) and part.get("type") == "tool_use":
                        if str(part.get("id") or "") == tc_id:
                            return str(part.get("name") or ""), json.dumps(part.get("input") or {})
        return "", ""

    def _superseded(self, msgs, last_user_idx) -> Dict[int, str]:
        """Result index -> reason, for file reads made obsolete by a later write/patch of the
        same file ("modified") or a later read of the same file with the same arguments
        ("reread"). Only events of COMPLETED turns count (before the current user message), so
        decisions change at turn boundaries and never mid-turn (prompt cache)."""
        result_idx: Dict[str, int] = {}
        for i, m in enumerate(msgs[:max(last_user_idx, 0)]):
            if not isinstance(m, dict):
                continue
            if m.get("role") == "tool" and m.get("tool_call_id"):
                result_idx[str(m["tool_call_id"])] = i
            elif isinstance(m.get("content"), list):
                for p in m["content"]:
                    if isinstance(p, dict) and p.get("type") == "tool_result" and p.get("tool_use_id"):
                        result_idx.setdefault(str(p["tool_use_id"]), i)
        reads: Dict[str, List[Tuple[int, str]]] = {}   # basename -> [(result idx, path, argkey)]
        runs: Dict[str, int] = {}                        # tool + normalized args -> latest result idx
        out: Dict[int, str] = {}
        for i, m in enumerate(msgs[:max(last_user_idx, 0)]):
            if not isinstance(m, dict) or m.get("role") != "assistant":
                continue
            for tc_id, name, args in self._calls_of(m):
                bare = _bare_tool_name(name)
                ri = result_idx.get(tc_id)
                if bare in _SNAPSHOT_TOOLS and ri is not None:
                    if bare in _SHELL_TOOLS:
                        cmd = _command_arg(args)
                        key = f"{bare}\0{command_key(cmd)}" if cmd.strip() else ""
                        if key and args.get("background"):
                            key = ""  # a background start returns a handle, not a snapshot
                    else:
                        key = f"{bare}\0{json.dumps(args, sort_keys=True, ensure_ascii=False)}"
                    if key:
                        if key in runs:
                            out.setdefault(runs[key], "rerun")
                        runs[key] = ri
                if bare in _SHELL_TOOLS:
                    cmd = _command_arg(args)
                    if cmd and self._write_succeeded(msgs, result_idx.get(tc_id)):
                        for wpath in shell_written_paths(cmd):
                            wbase = wpath.rstrip("/").rsplit("/", 1)[-1]
                            for prev_ri, rpath, _ in reads.get(wbase, ()):
                                if self._same_file(rpath, wpath):
                                    out[prev_ri] = "modified"
                    continue
                if bare in _WRITE_TOOLS:
                    if not args.get("dry_run") and self._write_succeeded(msgs, result_idx.get(tc_id)):
                        for path in _write_paths(args):
                            base = path.rstrip("/").rsplit("/", 1)[-1]
                            for prev_ri, rpath, _ in reads.get(base, ()):
                                if self._same_file(rpath, path):
                                    out[prev_ri] = "modified"
                    continue
                path = _path_arg(args)
                if not path:
                    continue
                base = path.rstrip("/").rsplit("/", 1)[-1]
                same = [r for r in reads.get(base, ()) if self._same_file(r[1], path)]
                if bare in _READ_TOOLS:
                    ri = result_idx.get(tc_id)
                    if ri is None:
                        continue
                    argkey = json.dumps(args, sort_keys=True, ensure_ascii=False)
                    for prev_ri, _, prev_key in same:
                        if prev_key == argkey:
                            out.setdefault(prev_ri, "reread")
                    reads.setdefault(base, []).append((ri, path, argkey))
        return out

    # -- Stub writes (1.12.2) ---------------------------------------------------
    # Every host gets this barrier, hook or not: a write that went through with CMI's short form as
    # its payload (copied from a digest or from a shortened past call) is flagged in its own result,
    # with the file's last known content stored under a recall key, so the model restores the file at
    # its very next step. The write_guard hook blocks such a write beforehand when the host runs it.
    _FULL_WRITE_TOOLS = frozenset({"write_file", "write", "create_file", "write_to_file"})

    def _flag_stub_writes(self, msgs) -> Optional[List[Dict[str, Any]]]:
        """Copy-on-write list with each stub write's result flagged, or None. Deterministic (same
        history, same bytes) and appended to a result that is new when it first appears, so the
        cached prefix is never rewritten for it."""
        from .write_guard import stub_in_payload
        scan = self.__dict__.setdefault("_stub_scan", {})  # tool call id -> hit (ids never change)
        if len(scan) > 50000:
            scan.clear()
        result_at: Dict[str, int] = {}
        hits = []
        for i, m in enumerate(msgs):
            if not isinstance(m, dict):
                continue
            if m.get("role") == "tool" and m.get("tool_call_id"):
                result_at[str(m["tool_call_id"])] = i
            elif m.get("role") == "user" and isinstance(m.get("content"), list):
                for p in m["content"]:
                    if isinstance(p, dict) and p.get("type") == "tool_result" and p.get("tool_use_id"):
                        result_at.setdefault(str(p["tool_use_id"]), i)
            elif m.get("role") == "assistant":
                ids = [str(tc.get("id") or "") for tc in m.get("tool_calls") or [] if isinstance(tc, dict)]
                if isinstance(m.get("content"), list):
                    ids += [str(p.get("id") or "") for p in m["content"]
                            if isinstance(p, dict) and p.get("type") == "tool_use"]
                if ids and all(t in scan and scan[t] is None for t in ids):
                    continue  # already scanned, nothing found: skip parsing the arguments again
                for tc_id, name, args in self._calls_of(m):
                    if tc_id not in scan:
                        scan[tc_id] = stub_in_payload(name, args)
                    if scan[tc_id] is not None:
                        hits.append((i, tc_id, name, args, scan[tc_id]))
        out = None
        for i, tc_id, name, args, (_key, stub) in hits:
            ri = result_at.get(tc_id)
            if ri is None:
                continue
            text, is_error = self._result_part(msgs[ri], tc_id)
            if is_error or _FAILED_RESULT_RE.match(text[:200]) or "Blocked by CMI" in text[:400]:
                continue  # refused or blocked: nothing was written
            path = next(iter(_write_paths(args)), "") or "the target file"
            if self._rewritten_later(msgs, i, path, result_at):
                continue  # already repaired: a later full write replaced the stub
            known, how = self._last_known_content(msgs, i, path, result_at)
            ref = self._stash(known) if known else None
            note = (f"\n\n[CMI warning] This {name} call sent CMI's short form "
                    f"({json.dumps(stub[:80], ensure_ascii=False)}) instead of real content, so {path} now "
                    f"holds that stub, not its content. Fix it before anything else: ")
            if ref:
                note += f"its last known content ({how}) is stored as{ref}; recall it, then write the full content back."
            else:
                note += (f"no earlier content of {path} is in this conversation; re-create it, or restore it "
                         f"from version control.")
            out = self._cow(out, msgs, ri)
            self._append_result_text(out[ri], tc_id, note)
            self.telemetry["stub_writes_flagged"] = self.telemetry.get("stub_writes_flagged", 0) + 1
        return out

    def _rewritten_later(self, msgs, after, path, result_at) -> bool:
        """A full, non-stub write of ``path`` succeeded after message ``after``."""
        from .write_guard import stub_in_payload
        for j in range(after + 1, len(msgs)):
            m = msgs[j]
            if not isinstance(m, dict) or m.get("role") != "assistant":
                continue
            for tc_id, name, args in self._calls_of(m):
                if _bare_tool_name(name) not in self._FULL_WRITE_TOOLS or stub_in_payload(name, args) is not None:
                    continue
                if not any(self._same_file(p, path) for p in _write_paths(args)):
                    continue
                ri = result_at.get(tc_id)
                if ri is None:
                    continue
                text, is_error = self._result_part(msgs[ri], tc_id)
                if not is_error and not _FAILED_RESULT_RE.match(text[:200]) and "Blocked by CMI" not in text[:400]:
                    return True
        return False

    def _last_known_content(self, msgs, before, path, result_at) -> Tuple[Optional[str], str]:
        """The newest full copy of ``path`` before message ``before``: a full write's payload or a
        read's result, as the raw history holds them. Partial edits after it are mentioned."""
        from .write_guard import stub_in_payload
        edited = False
        for j in range(before - 1, -1, -1):
            m = msgs[j]
            if not isinstance(m, dict) or m.get("role") != "assistant":
                continue
            for tc_id, name, args in reversed(list(self._calls_of(m))):
                bare = _bare_tool_name(name)
                ri = result_at.get(tc_id)
                if ri is None:
                    continue
                text, is_error = self._result_part(msgs[ri], tc_id)
                if is_error or _FAILED_RESULT_RE.match(text[:200]):
                    continue
                later = ", before later edits" if edited else ""
                if bare in _WRITE_TOOLS:
                    if not any(self._same_file(p, path) for p in _write_paths(args)):
                        continue
                    full = args.get("content") if bare in self._FULL_WRITE_TOOLS else None
                    if isinstance(full, str) and full and stub_in_payload(name, args) is None:
                        return full, f"as last written{later}"
                    edited = True
                elif bare in _READ_TOOLS:
                    rpath = _path_arg(args)
                    if rpath and self._same_file(rpath, path) and text.strip():
                        return text, f"as last read{later}"
        return None, ""

    @staticmethod
    def _result_part(msg, tc_id) -> Tuple[str, bool]:
        """(text, is_error) of the result for ``tc_id`` in an OpenAI tool message or an Anthropic
        user message holding tool_result parts."""
        if not isinstance(msg, dict):
            return "", False
        c = msg.get("content")
        if msg.get("role") == "tool":
            if isinstance(c, list):
                return "\n".join(p.get("text", "") for p in c if isinstance(p, dict) and p.get("type") == "text"), False
            return (c if isinstance(c, str) else ""), False
        for p in c if isinstance(c, list) else []:
            if isinstance(p, dict) and p.get("type") == "tool_result" and str(p.get("tool_use_id")) == tc_id:
                inner = p.get("content")
                if isinstance(inner, list):
                    inner = "\n".join(ip.get("text", "") for ip in inner if isinstance(ip, dict) and ip.get("type") == "text")
                return (inner if isinstance(inner, str) else ""), bool(p.get("is_error"))
        return "", False

    @staticmethod
    def _append_result_text(msg, tc_id, text) -> None:
        """Append ``text`` to the (deep-copied) result for ``tc_id``, in both message shapes."""
        if msg.get("role") == "tool":
            c = msg.get("content")
            msg["content"] = (c + text if isinstance(c, str) else
                              list(c) + [{"type": "text", "text": text}] if isinstance(c, list) else text.lstrip())
            return
        for p in msg.get("content") or []:
            if isinstance(p, dict) and p.get("type") == "tool_result" and str(p.get("tool_use_id")) == tc_id:
                inner = p.get("content")
                p["content"] = (inner + text if isinstance(inner, str) else
                                list(inner) + [{"type": "text", "text": text}] if isinstance(inner, list)
                                else text.lstrip())

    def _write_succeeded(self, msgs, ri) -> bool:
        """A failed patch leaves the file unchanged: marking the read outdated would hide the
        only correct view of it (the model then refuses to answer)."""
        if ri is None:
            return False
        m = msgs[ri]
        text = self._content_text(m) if isinstance(m, dict) else ""
        if isinstance(m, dict) and isinstance(m.get("content"), list):
            if any(isinstance(p, dict) and p.get("type") == "tool_result" and p.get("is_error") for p in m["content"]):
                return False
        if _FAILED_RESULT_RE.match(text[:200]):
            return False
        code = _EXIT_CODE_RE.search(text[:400]) or _EXIT_CODE_RE.search(text[-400:])
        return not (code and code.group(1) != "0")

    @staticmethod
    def _same_file(a: str, b: str) -> bool:
        """Same file for sure. Two anchored paths (/..., ~/...) must be equal; a relative path
        matches an anchored one only by a suffix of at least one directory plus the name (a
        bare `config.py` could be any project's)."""
        a, b = a.rstrip("/"), b.rstrip("/")
        if a == b:
            return True
        anchored = lambda p: p.startswith(("/", "~"))
        if anchored(a) and anchored(b):
            return False
        rel, full = (a, b) if not anchored(a) else (b, a)
        rel = rel[2:] if rel.startswith("./") else rel
        if "/" not in rel:
            return not anchored(full) and full.endswith("/" + rel)
        return full.endswith("/" + rel)

    def _calls_of(self, m: Dict[str, Any]):
        for tc in m.get("tool_calls") or []:
            if isinstance(tc, dict):
                fn = tc.get("function") or {}
                yield str(tc.get("id") or ""), str(fn.get("name") or ""), self._json_obj(fn.get("arguments"))
        if isinstance(m.get("content"), list):
            for p in m["content"]:
                if isinstance(p, dict) and p.get("type") == "tool_use":
                    inp = p.get("input")
                    yield str(p.get("id") or ""), str(p.get("name") or ""), inp if isinstance(inp, dict) else {}

    def _hot_files(self, msgs, last_user_idx) -> set:
        """Files the user is talking about: named in the CURRENT user message (effective on the
        very first request of the turn — the working set only learns after the turn) or named
        in the last FLOOR_USER_MENTION_TURNS turns."""
        hot = set(self.working_set.user_mentioned_paths())
        if 0 <= last_user_idx < len(msgs) and isinstance(msgs[last_user_idx], dict):
            known = self.working_set.known_files() or self._paths_in_tool_args(msgs)
            hot.update(extract_paths(self._user_text(msgs[last_user_idx]), known=known))
        return hot

    def _lexical_recall(self, msgs, last_user_idx, tool_results, horizon=None) -> set:
        """Old tool results holding a DISTINCTIVE term of the current user message (an
        identifier such as WEBHOOK_SECRET, dunningDays, a quoted string, a long number).
        Only rare terms count (found in at most LEXICAL_MAX_RESULTS results) and the restored
        total is capped, newest first. Keyed on the current user message only, so the decision
        is fixed for the whole turn (prompt cache)."""
        if not (0 <= last_user_idx < len(msgs)) or not isinstance(msgs[last_user_idx], dict):
            return set()
        terms = distinctive_terms(self._user_text(msgs[last_user_idx]))
        if not terms:
            return set()
        hits: Dict[str, List[int]] = {}
        texts: Dict[int, str] = {}
        horizon = last_user_idx if horizon is None else horizon
        for idx in tool_results:
            if idx >= horizon:
                continue
            text = texts[idx] = self._content_text(msgs[idx])
            for t in terms:
                if t in text:
                    hits.setdefault(t, []).append(idx)
        chosen, budget = set(), LEXICAL_RECALL_TOKENS
        for idx in sorted({i for v in hits.values() if len(v) <= LEXICAL_MAX_RESULTS for i in v}, reverse=True):
            cost = _tokens_rough(texts[idx])
            if cost <= budget:
                chosen.add(idx)
                budget -= cost
        if chosen:
            self.telemetry["lexical_recalls"] = self.telemetry.get("lexical_recalls", 0) + len(chosen)
        return chosen

    def _hot_results(self, msgs, tool_results, hot_files, horizon) -> set:
        """Results of files the user names, read BEFORE the current user message, newest first:
        the newest always, then older ones until one does not fit in HOT_FILE_TOKENS. A message
        naming dozens of files would otherwise pin every read raw; the rest stay recoverable
        with cmi_recall. Results of the current turn are raw anyway (user anchor) and do not
        count, so the set is fixed for the whole turn (prompt cache), like _lexical_recall."""
        if not hot_files:
            return set()
        chosen, budget = set(), HOT_FILE_TOKENS
        for idx in sorted((i for i in tool_results if i < horizon), reverse=True):
            if not match_known(extract_paths(tool_results[idx].tool_args or ""), hot_files):
                continue
            cost = _tokens_rough(self._content_text(msgs[idx]))
            if chosen and cost > budget:
                break
            chosen.add(idx)
            budget -= cost
        return chosen

    def _paths_in_tool_args(self, msgs) -> List[str]:
        """Cold start (engine just loaded on an existing session): the paths the agent touched."""
        out = []
        for name, args, _ in self._build_caller_index(msgs).values():
            a = args if isinstance(args, dict) else self._json_obj(args)
            out.extend(_write_paths(a))
        return out

    @staticmethod
    def _json_obj(s) -> Any:
        try:
            return json.loads(s) if isinstance(s, str) and s else {}
        except Exception:
            return {}

    def _decide_render(self, idx, info, msgs, last_user_idx, hot_files=frozenset()) -> str:
        content_text = self._content_text(msgs[idx])
        if idx in self._superseded_now and idx < last_user_idx:
            # An obsolete view of a file: keeping it raw costs tokens AND misleads the model.
            # Stays recallable by key; the model is told to re-read for the current content.
            profitable = ToolResultLifecycle.is_profitable(
                content_text, overhead_chars=self._DIGEST_WRAPPER_OVERHEAD + 80)
            return "digest" if profitable else "raw"
        size_tokens = _tokens_rough(content_text)
        relevance, relevance_priority = self._relevance_and_priority(content_text, tool_args=info.tool_args)
        self._hot_hit = False
        if idx in self._lexical_hits and not relevance_priority:
            # The user quotes a rare identifier that only this old result contains: send it
            # whole now instead of making the model spend a round trip on cmi_recall.
            relevance_priority = self._hot_hit = True
        if hot_files and not relevance_priority:
            # The result OF a file the user is talking about (its path is in the call's args)
            # comes back in full — the model must not work from a digest of it (within the
            # HOT_FILE_TOKENS budget, see _hot_results).
            relevance_priority = self._hot_hit = idx in self._hot_hits
        criticality = ToolResultLifecycle.criticality_of(info.tool_name, content_text)
        represented = info.instance_id in self.lifecycle._represented
        referenced = self.lifecycle.is_referenced(info.instance_id)
        user_anchor = idx >= last_user_idx
        score = self.lifecycle.score(
            age=info.age, relevance=relevance, criticality=criticality,
            size_tokens=size_tokens, represented=represented, user_anchor=user_anchor,
        )
        profitable = ToolResultLifecycle.is_profitable(
            content_text, overhead_chars=self._DIGEST_WRAPPER_OVERHEAD,
        )
        return self.lifecycle.decide(
            age=info.age, score=score, user_anchor=user_anchor,
            represented=represented, referenced=referenced,
            relevance_priority=relevance_priority,
            profitable=profitable,
        )

    def _relevance_and_priority(self, content_text: str, tool_args: str = "") -> Tuple[float, bool]:
        """One path extraction for both the relevance score and the priority flag."""
        if self.working_set.state_size() == 0:
            return 0.0, False
        known = self._known_index or KnownIndex(self.working_set.known_files())
        paths = extract_paths(content_text[:20000], known=known)
        paths += extract_paths(tool_args[:2000] if tool_args else "", known=known)
        if not paths:
            return 0.0, False
        prio = any(self.working_set.is_prioritary(p) for p in paths)
        return (1.0 if prio else 0.5), prio

    def _relevance(self, content_text: str, tool_args: str = "") -> float:
        if self.working_set.state_size() == 0:
            return 0.0
        known = self.working_set.known_files()
        paths = extract_paths(content_text[:20000], known=known)
        paths += extract_paths(tool_args[:2000] if tool_args else "", known=known)
        if not paths:
            return 0.0
        if any(self.working_set.is_prioritary(p) for p in paths):
            return 1.0
        return 0.5

    def _relevance_priority(self, content_text: str, tool_args: str = "") -> bool:
        if self.working_set.state_size() == 0:
            return False
        known = self.working_set.known_files()
        paths = extract_paths(content_text[:20000], known=known)
        paths += extract_paths(tool_args[:2000] if tool_args else "", known=known)
        return any(self.working_set.is_prioritary(p) for p in paths)

    def _render_tool_result(self, msg: Dict[str, Any], info, state: str) -> Dict[str, Any]:
        out = copy.deepcopy(msg)
        content_text = self._content_text(msg)
        if state == "raw":
            return msg
        one_liner = state == "one_liner"
        digest = self.lifecycle.render_digest(
            info.instance_id,
            tool_name=info.tool_name or "tool",
            tool_args=info.tool_args,
            content=content_text,
            summarize_fn=_summarize_with_key_arg,
            one_liner=one_liner,
            overhead_chars=self._DIGEST_WRAPPER_OVERHEAD,
        )
        ref = self._stash(content_text)
        if ref is None:
            return msg
        note = SUPERSEDED_NOTE.get(self._superseded_now.get(info.msg_index, ""), "")
        replacement = f"[CMI {state}]{ref} {note + ' ' if note else ''}{digest}"
        if note:
            self.telemetry["superseded"] = self.telemetry.get("superseded", 0) + 1
        self._telemetry_degrade(info.tool_name, content_text, replacement, one_liner)
        if self._image_parts(msg) and info.instance_id not in self._image_counted:
            self._image_counted.add(info.instance_id)
            self.telemetry["image_evictions"] += 1
            self._image_evicted_any = True
        if out.get("role") == "tool":
            out["content"] = replacement
            return out
        if isinstance(out.get("content"), list):
            for part in out["content"]:
                if isinstance(part, dict) and part.get("type") == "tool_result":
                    if isinstance(part.get("content"), list):
                        part["content"] = [{"type": "text", "text": replacement}]
                    else:
                        part["content"] = replacement
            return out
        out["content"] = replacement
        return out

    def _apply_images(
        self, msgs: List[Dict[str, Any]], last_user_idx: int,
    ) -> Optional[List[Dict[str, Any]]]:
        starts = set(turn_starts(msgs))
        total_turns = len(starts)
        user_seen = 0
        out: Optional[List[Dict[str, Any]]] = None
        for i, m in enumerate(msgs):
            if i in starts:
                user_seen += 1
            if not isinstance(m, dict):
                continue
            parts = self._image_parts(m)
            if _is_user_turn(m):
                # What the user pasted is user input (a mockup, a bug screenshot): never touched.
                # Only images inside tool results (screenshots a tool took) can go stale.
                parts = [p for p in parts if isinstance(p, tuple)]
            if not parts:
                continue
            age = total_turns - user_seen
            if ToolResultLifecycle.image_state(age) != "evicted":
                continue
            companion = self._content_text(m)[:300]
            iid = f"img:{i}"
            # Zero loss: every evicted image is kept as an image file first; if one cannot be
            # kept, the whole message keeps its images.
            refs = [self._stash_image(part[1] if isinstance(part, tuple) else part) for part in parts]
            if any(r is None for r in refs):
                continue
            out = self._cow(out, msgs, i)
            target = out[i]
            changed = False
            tparts = self._image_parts(target)
            if _is_user_turn(target):
                tparts = [p for p in tparts if isinstance(p, tuple)]
            for part, ref in zip(tparts, refs):
                placeholder = ToolResultLifecycle.image_placeholder(iid, companion, ref)
                if isinstance(part, tuple):
                    parent, image_ip = part
                    if isinstance(parent.get("content"), list):
                        parent["content"] = [
                            {"type": "text", "text": placeholder} if ip is image_ip else ip
                            for ip in parent["content"]
                        ]
                        changed = True
                elif isinstance(part, dict) and part.get("type") == "image":
                    content = target.get("content")
                    if isinstance(content, list):
                        target["content"] = [
                            {"type": "text", "text": placeholder} if p is part else p
                            for p in content
                        ]
                        changed = True
            if changed:
                self._image_evicted_any = True
                if iid not in self._image_counted:
                    self._image_counted.add(iid)
                    self.telemetry["image_evictions"] += 1
        return out

    def _degrade_tool_call_args(
        self, msgs: List[Dict[str, Any]], last_user_idx: int,
    ) -> Optional[List[Dict[str, Any]]]:
        """Shrink old write_file/patch tool-CALL argument payloads.

        Memoized per instance (byte-stable, same cache-break-once contract
        as tool-result digests). Never touches tool_call id/name/structure —
        only the argument text — so pairing stays intact for every provider
        adapter. A call referenced by a LATER turn (its id appears again
        after this message) is left untouched.
        """
        starts = set(turn_starts(msgs))
        total_turns = len(starts)
        user_seen = 0
        referenced_ids = self._collect_referenced_tool_call_ids(msgs)
        out: Optional[List[Dict[str, Any]]] = None

        for i, m in enumerate(msgs):
            if i in starts:
                user_seen += 1
            if not isinstance(m, dict) or m.get("role") != "assistant":
                continue
            age = max(0, total_turns - user_seen)
            user_anchor = i >= last_user_idx
            content = m.get("content")
            edits: List[Tuple[str, str, Dict[str, Any], str]] = []  # (kind, tc_id, args, name)

            if isinstance(content, list):
                for part in content:
                    if isinstance(part, dict) and part.get("type") == "tool_use":
                        tc_id = str(part.get("id") or "")
                        name = str(part.get("name") or "")
                        args = part.get("input") or {}
                        if (tc_id not in referenced_ids
                                and self.lifecycle.should_truncate_tool_call_args(
                                    name, args, age=age, user_anchor=user_anchor,
                                    referenced=False)):
                            edits.append(("anthropic", tc_id, args, name))
            for tc in m.get("tool_calls") or []:
                if not isinstance(tc, dict):
                    continue
                tc_id = str(tc.get("id") or "")
                fn = tc.get("function") or {}
                name = str(fn.get("name") or "")
                try:
                    args = json.loads(fn.get("arguments") or "{}")
                except Exception:
                    args = {}
                if (tc_id not in referenced_ids and isinstance(args, dict)
                        and self.lifecycle.should_truncate_tool_call_args(
                            name, args, age=age, user_anchor=user_anchor, referenced=False)):
                    edits.append(("openai", tc_id, args, name))

            if not edits:
                continue
            out = self._cow(out, msgs, i)
            target = out[i]
            for kind, tc_id, args, name in edits:
                instance_id = f"argcall:{i}:{tc_id}"
                new_args = self.lifecycle.render_tool_call_args_digest(
                    instance_id, tool_name=name, args=args, stash=self._stash,
                )
                # Profitability guarantee for args too.
                if len(json.dumps(new_args, ensure_ascii=False)) >= len(
                    json.dumps(args, ensure_ascii=False),
                ):
                    continue
                if kind == "anthropic" and isinstance(target.get("content"), list):
                    for part in target["content"]:
                        if isinstance(part, dict) and part.get("type") == "tool_use" \
                                and str(part.get("id") or "") == tc_id:
                            part["input"] = new_args
                elif kind == "openai":
                    for tc in target.get("tool_calls") or []:
                        if isinstance(tc, dict) and str(tc.get("id") or "") == tc_id:
                            fn = tc.get("function") or {}
                            fn["arguments"] = json.dumps(new_args, ensure_ascii=False)
                self._telemetry_degrade(f"{name}_args", json.dumps(args), json.dumps(new_args), False)
        return out

    def _collect_referenced_tool_call_ids(self, msgs: List[Dict[str, Any]]) -> set:
        """Tool-call ids whose id string is echoed again later (a rare but
        real dependency signal — e.g. the agent quotes an earlier call).
        Conservative: only exact id-string reuse counts."""
        ids_seen: Dict[str, int] = {}
        odd_ids: Dict[str, int] = {}   # ids with chars outside the token alphabet: substring check
        referenced: set = set()
        for i, m in enumerate(msgs):
            if not isinstance(m, dict):
                continue
            text = self._content_text(m)
            if text and ids_seen:
                for tok in set(_ID_TOKEN_RE.findall(text)):
                    pos = ids_seen.get(tok)
                    if pos is not None and pos < i:
                        referenced.add(tok)
            for tc_id, pos in odd_ids.items():
                if pos < i and tc_id in text:
                    referenced.add(tc_id)
            content = m.get("content")
            if isinstance(content, list):
                for part in content:
                    if isinstance(part, dict) and part.get("type") == "tool_use":
                        self._note_id(str(part.get("id") or ""), i, ids_seen, odd_ids)
            for tc in m.get("tool_calls") or []:
                if isinstance(tc, dict):
                    self._note_id(str(tc.get("id") or ""), i, ids_seen, odd_ids)
        return referenced

    @staticmethod
    def _note_id(tc_id: str, i: int, ids_seen: Dict[str, int], odd_ids: Dict[str, int]) -> None:
        if not tc_id:
            return
        (ids_seen if _ID_TOKEN_RE.fullmatch(tc_id) else odd_ids)[tc_id] = i

    # -- Duplicate-result collapsing (v1.1) ------------------------------------

    def _collapse_duplicate_results(
        self, msgs: List[Dict[str, Any]], tool_results: Dict[int, Any],
        last_user_idx: int,
    ) -> Optional[List[Dict[str, Any]]]:
        """Replace exact byte-identical repeat tool results (after the FIRST
        occurrence) with a tiny reference. Only touches instances still
        rendered "raw" this request — never overrides a digest/one_liner
        decision made by the primary scoring pass."""
        from .lifecycle import DUP_MIN_CHARS

        seen_hashes: Dict[str, str] = {}   # content_hash -> first instance_id
        out: Optional[List[Dict[str, Any]]] = None

        for idx, info in tool_results.items():
            if idx >= last_user_idx or info.age <= 2:
                continue
            if self.lifecycle.state_of(info.instance_id) != "raw":
                continue  # already degraded by the primary pass
            content = self._content_text(msgs[idx])
            if len(content) < DUP_MIN_CHARS:
                continue
            h = self.lifecycle.content_hash(content)
            if h not in seen_hashes:
                seen_hashes[h] = info.instance_id
                continue
            # Duplicate: replace with a reference to the first occurrence.
            ref_text = self.lifecycle.render_duplicate_reference(
                info.instance_id, first_instance_id=seen_hashes[h],
                tool_name=info.tool_name or "tool",
            )
            ref = self._stash(content)
            if ref is None:
                continue
            replacement = ref_text.replace("[CMI dup]", f"[CMI dup]{ref}", 1)
            if len(replacement) >= len(content):
                continue
            out = self._cow(out, msgs, idx)
            self._write_tool_result_content(out[idx], replacement)
            self.lifecycle.record_state(info.instance_id, "digest")
            self._telemetry_degrade(info.tool_name, content, replacement, False)
        return out

    # -- Small-result batch aggregation (v1.1) ---------------------------------

    def _aggregate_small_result_batches(
        self, msgs: List[Dict[str, Any]], tool_results: Dict[int, Any],
        last_user_idx: int,
    ) -> Optional[List[Dict[str, Any]]]:
        """Fold runs of consecutive, old, same-tool, individually-tiny
        results into one batch summary attached to the first item; the rest
        become minimal stubs. Every tool_call/tool_result PAIR is preserved —
        only content shrinks. Batches never cross a compaction boundary or
        include an anchored/recent/already-degraded instance."""
        from .lifecycle import BATCH_MIN_SIZE

        ordered = sorted(tool_results.items(), key=lambda kv: kv[0])
        eligible: List[Tuple[int, Any, str]] = []
        for idx, info in ordered:
            if idx >= last_user_idx or info.age <= 2:
                continue
            if self.lifecycle.state_of(info.instance_id) != "raw":
                continue  # already handled by an earlier pass
            content = self._content_text(msgs[idx])
            if len(content) >= 300:  # not "small" — leave to the digest pass
                continue
            eligible.append((idx, info, content))

        if len(eligible) < BATCH_MIN_SIZE:
            return None

        # Group consecutive-by-appearance runs sharing the same tool name.
        runs: List[List[Tuple[int, Any, str]]] = []
        current: List[Tuple[int, Any, str]] = []
        for item in eligible:
            if current and current[-1][1].tool_name == item[1].tool_name:
                current.append(item)
            else:
                if len(current) >= BATCH_MIN_SIZE:
                    runs.append(current)
                current = [item]
        if len(current) >= BATCH_MIN_SIZE:
            runs.append(current)
        if not runs:
            return None

        out: Optional[List[Dict[str, Any]]] = None
        for run in runs:
            first_idx = run[0][0]
            batch_id = f"{first_idx}:{run[0][1].instance_id}"
            items_payload = [
                {"tool_name": info.tool_name, "tool_args": info.tool_args, "content": content}
                for _, info, content in run
            ]
            summary = self.lifecycle.render_batch_summary(batch_id, items=items_payload)
            summary_text = summary  # already carries its own "[CMI batch]" prefix

            # Build stubs for every non-first item up front so profitability
            # is judged on the FULL set of messages this batch would touch —
            # not just the raw content strings. estimate_messages_tokens_rough
            # includes per-message JSON/role overhead that a bare string
            # comparison misses, and that overhead does not change (each
            # message keeps its role/id structure either way) but must still
            # be included on BOTH sides of the comparison for it to be valid.
            candidate_msgs_before = [msgs[first_idx]] + [msgs[idx] for idx, _, _ in run[1:]]
            stubs: List[Tuple[int, Any, str]] = []
            refs = [self._stash(content) for _, _, content in run]
            if any(r is None for r in refs):
                continue  # cannot keep every original: leave this run untouched
            summary_text = summary_text.replace("[CMI batch]", f"[CMI batch]{refs[0]}", 1)
            for (idx, info, content), ref in zip(run[1:], refs[1:]):
                stub = self.lifecycle.render_batch_stub(
                    info.instance_id, batch_id=batch_id, position=len(stubs) + 1, ref=ref,
                )
                stubs.append((idx, info, stub))
            candidate_first = copy.deepcopy(msgs[first_idx])
            self._write_tool_result_content(candidate_first, summary_text)
            candidate_msgs_after = [candidate_first]
            for idx, info, stub in stubs:
                cand = copy.deepcopy(msgs[idx])
                self._write_tool_result_content(cand, stub)
                candidate_msgs_after.append(cand)

            before_tokens = estimate_messages_tokens_rough(candidate_msgs_before)
            after_tokens = estimate_messages_tokens_rough(candidate_msgs_after)
            if after_tokens >= before_tokens:
                continue  # batching this run isn't profitable; skip it

            out = self._cow(out, msgs, first_idx)
            self._write_tool_result_content(out[first_idx], summary_text)
            self.lifecycle.record_state(run[0][1].instance_id, "digest")
            for idx, info, stub in stubs:
                out = self._cow(out, msgs, idx)
                self._write_tool_result_content(out[idx], stub)
                self.lifecycle.record_state(info.instance_id, "one_liner")
            self._telemetry_degrade(
                run[0][1].tool_name, "x" * (before_tokens * 4), summary_text, False,
            )
        return out

    # -- Assistant narration compaction (v1.1) ---------------------------------

    def _compact_old_narration(
        self, msgs: List[Dict[str, Any]], last_user_idx: int,
    ) -> Optional[List[Dict[str, Any]]]:
        """Drop old lead-in prose attached to a tool_call once it is stale.
        NEVER touches a message with no tool_calls (a final/standalone reply
        keeps its full text regardless of age)."""
        starts = set(turn_starts(msgs))
        total_turns = len(starts)
        user_seen = 0
        out: Optional[List[Dict[str, Any]]] = None

        for i, m in enumerate(msgs):
            if i in starts:
                user_seen += 1
            if not isinstance(m, dict) or m.get("role") != "assistant":
                continue
            has_tool_calls = bool(m.get("tool_calls")) or any(
                isinstance(p, dict) and p.get("type") == "tool_use"
                for p in (m.get("content") or []) if isinstance(m.get("content"), list)
            )
            if not has_tool_calls:
                continue  # standalone reply — never touched
            age = max(0, total_turns - user_seen)
            if i >= last_user_idx or age <= 2:
                continue
            text = self._content_text(m)
            if not ToolResultLifecycle.is_narration_profitable(text):
                continue
            instance_id = f"narr:{i}"
            ref = self._stash(text)
            if ref is None:
                continue
            stub = self.lifecycle.render_narration_stub(instance_id, original=text, ref=ref)
            if len(stub) >= len(text):
                continue
            out = self._cow(out, msgs, i)
            target = out[i]
            if isinstance(target.get("content"), str):
                target["content"] = stub
            elif isinstance(target.get("content"), list):
                new_parts = []
                replaced = False
                for part in target["content"]:
                    if isinstance(part, dict) and part.get("type") == "text" and not replaced:
                        new_parts.append({"type": "text", "text": stub})
                        replaced = True
                    else:
                        new_parts.append(part)
                target["content"] = new_parts
            self.lifecycle.record_state(instance_id, "narr_done")
            self._telemetry_degrade("assistant_narration", text, stub, False)
        return out

    def _ensure_store_root(self) -> None:
        if self.store.root is None:
            # Host never called on_session_start (tests, other harnesses): default location.
            from pathlib import Path
            self.store.set_root(Path(default_home()) / "cmi_store")

    def _stash_image(self, block: Any) -> Optional[str]:
        """Keep an image block's bytes as an image file; return the stub suffix or None."""
        self._ensure_store_root()
        src = block.get("source") if isinstance(block, dict) else None
        if not isinstance(src, dict):
            return None
        if src.get("type") == "base64":
            key = self.store.put_image(src.get("data"), src.get("media_type"))
            if not key:
                return None
            path = self.store.image_path(key)
            return f" cmi:{key} (image: {path})" if self.recall_via_file else f" cmi:{key}"
        if src.get("type") == "url" and isinstance(src.get("url"), str):
            return self._stash(src["url"])
        return None

    def _stash(self, content: str) -> Optional[str]:
        """Keep ``content`` verbatim; return the ' cmi:<key> (<path>)' suffix for the stub, or
        None when it cannot be stored (the caller then leaves the original in place)."""
        self._ensure_store_root()
        key = self.store.put(content)
        if not key:
            return None
        if self.recall_via_file:
            # Harness without the cmi_recall tool (proxy mode): point at the file itself.
            return f" cmi:{key} (full text: {self.store.path_of(key)})"
        return f" cmi:{key}"

    @staticmethod
    def _cow(out: Optional[List[Dict[str, Any]]], msgs: List[Dict[str, Any]], i: int) -> List[Dict[str, Any]]:
        """Copy-on-write: shallow list once, deep-copy only the message about to be edited.
        The caller's messages are never mutated; untouched entries keep their identity."""
        if out is None:
            out = list(msgs)
        if out[i] is msgs[i]:
            out[i] = copy.deepcopy(msgs[i])
        return out

    def _write_tool_result_content(self, msg: Dict[str, Any], replacement: str) -> None:
        """Mutate a (deep-copied) message's tool-result content in place,
        handling both OpenAI role:tool and Anthropic tool_result-part shapes."""
        if msg.get("role") == "tool":
            msg["content"] = replacement
            return
        content = msg.get("content")
        if isinstance(content, list):
            for part in content:
                if isinstance(part, dict) and part.get("type") == "tool_result":
                    if isinstance(part.get("content"), list):
                        part["content"] = [{"type": "text", "text": replacement}]
                    else:
                        part["content"] = replacement

    def _image_parts(self, msg) -> List[Any]:
        found = []
        content = msg.get("content")
        if not isinstance(content, list):
            return found
        for part in content:
            if not isinstance(part, dict):
                continue
            if part.get("type") == "image":
                found.append(part)
            elif part.get("type") == "tool_result" and isinstance(part.get("content"), list):
                for ip in part["content"]:
                    if isinstance(ip, dict) and ip.get("type") == "image":
                        found.append((part, ip))
        return found

    def _content_text(self, msg) -> str:
        c = msg.get("content")
        if isinstance(c, str):
            return c
        if isinstance(c, list):
            out = []
            for part in c:
                if isinstance(part, dict):
                    if part.get("type") == "text":
                        out.append(part.get("text") or "")
                    elif part.get("type") == "tool_result":
                        inner = part.get("content")
                        if isinstance(inner, str):
                            out.append(inner)
                        elif isinstance(inner, list):
                            out.extend(
                                ip.get("text", "") for ip in inner
                                if isinstance(ip, dict) and ip.get("type") == "text"
                            )
            return "\n".join(out)
        return ""

    def _telemetry_degrade(self, tool_name, original, replacement, one_liner) -> None:
        saved = _tokens_rough(original) - _tokens_rough(replacement)
        if saved <= 0:
            return
        key = "one_liner_count" if one_liner else "digest_count"
        self.telemetry[key] += 1
        per_tool = self.telemetry["tool_result_reductions"]
        per_tool[tool_name or "unknown"] = per_tool.get(tool_name or "unknown", 0) + saved

    def _record_telemetry(self, raw: int, compiled: int, saved: int) -> None:
        self.telemetry["raw_context_tokens"] += raw
        self.telemetry["compiled_context_tokens"] += compiled
        self.telemetry["tokens_saved"] += saved

    # -- Post-turn learning ---------------------------------------------------

    def on_turn_complete(
        self,
        messages: Optional[List[Dict[str, Any]]] = None,
        usage: Optional[Dict[str, Any]] = None,
        **kwargs: Any,
    ) -> None:
        try:
            if messages:
                # Learn only what is new since the last turn. Re-learning the whole history
                # every turn re-bumped every file ever read, pinning them all at the max score.
                start = self._learned_upto if len(messages) >= self._learned_upto else 0
                self._learn_from_messages(messages[start:])
                self._learned_upto = len(messages)
                self._index_new_outputs(messages)
            self.working_set.on_turn_complete()
        except Exception:
            logger.debug("CMI on_turn_complete failed", exc_info=True)
        self._flush_stats()

    def _learn_from_messages(self, msgs: List[Dict[str, Any]]) -> None:
        known = set(self.working_set.known_files())
        seen_user_turn = False
        for m in msgs:
            if not isinstance(m, dict):
                continue
            role = m.get("role")
            if _is_user_turn(m) and self._user_text(m):
                # Replay the timeline: a batch spanning several user turns (engine loaded on an
                # existing session) decays between them, like live turns would. Without it every
                # file ever read lands at the max score and nothing is ever digested.
                if seen_user_turn:
                    self.working_set.on_turn_complete()
                seen_user_turn = True
            if role == "assistant":
                # OpenAI format
                for tc in m.get("tool_calls") or []:
                    if not isinstance(tc, dict):
                        continue
                    fn = tc.get("function") or {}
                    self._learn_tool_call(
                        str(fn.get("name") or ""),
                        str(fn.get("arguments") or ""),
                        known,
                    )
                # Anthropic format
                if isinstance(m.get("content"), list):
                    for part in m["content"]:
                        if isinstance(part, dict) and part.get("type") == "tool_use":
                            self._learn_tool_call(
                                str(part.get("name") or ""),
                                json.dumps(part.get("input") or {}),
                                known,
                            )
            elif _is_user_turn(m):
                text = self._user_text(m)
                if text:
                    paths = extract_paths(text, known=known) if known else extract_paths(text)
                    if paths:
                        self.working_set.note_user_mention(paths)

    @staticmethod
    def _user_text(m: Dict[str, Any]) -> str:
        """What the USER wrote: str content, or the text parts of a list (never tool_result parts,
        which Anthropic's wire format also carries in user messages)."""
        c = m.get("content")
        if isinstance(c, str):
            return c
        if isinstance(c, list):
            return "\n".join(p.get("text", "") for p in c
                             if isinstance(p, dict) and p.get("type") == "text")
        return ""

    def _learn_tool_call(self, name: str, args: str, known: set) -> None:
        """Hermes, Claude Code (Read/Edit/Bash, file_path), Codex (shell argv, apply_patch),
        Cline (read_file/write_to_file/execute_command)... one vocabulary for all."""
        try:
            a = json.loads(args) if args else {}
        except Exception:
            a = {}
        if not isinstance(a, dict):
            return
        n = name.lower()
        bare = _bare_tool_name(name)
        if bare in _READ_TOOLS or "read_file" in n:
            path = _path_arg(a)
            if path:
                self.working_set.note_read(path)
        elif bare in _WRITE_TOOLS or "write_file" in n or "patch" in n:
            for path in _write_paths(a):
                self.working_set.note_write(path)
        if bare in _SHELL_TOOLS or "terminal" in n:
            cmd = _command_arg(a)
            for p in extract_paths(cmd, known=known) if known and cmd else []:
                self.working_set.note_terminal_mention(p)

    def get_status(self) -> Dict[str, Any]:
        base = super().get_status()
        base["cmi"] = {
            "version": running_version(),
            "installed_version": stale_install(),  # None unless a newer/other CMI waits for a restart
            "capabilities": self.capabilities.as_dict() if self.capabilities else None,
            "telemetry": dict(self.telemetry),
            "working_set_top": self.working_set.top(10),
        }
        return base


class _ToolResultInfo:
    __slots__ = ("instance_id", "tool_name", "tool_args", "age", "msg_index")

    def __init__(self, instance_id, tool_name, tool_args, age, msg_index):
        self.instance_id = instance_id
        self.tool_name = tool_name
        self.tool_args = tool_args
        self.age = age
        self.msg_index = msg_index
