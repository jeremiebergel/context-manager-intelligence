"""CMI proxy: put CMI in front of any OpenAI- or Anthropic-compatible API.

Any tool that lets you set an API base URL (Claude Code, Codex CLI, Cline, Roo, OpenCode, Aider,
Continue, your own agent) gets CMI with no code change: requests are compiled on the way out,
responses stream back untouched.

    python -m cmi.proxy --port 8787                     # upstreams: api.anthropic.com, api.openai.com
    ANTHROPIC_BASE_URL=http://127.0.0.1:8787 claude     # Claude Code
    OPENAI_BASE_URL=http://127.0.0.1:8787/v1 codex      # any OpenAI-compatible client

Routes
    POST /v1/messages            Anthropic Messages  -> ANTHROPIC upstream
    POST /v1/chat/completions    OpenAI Chat         -> OPENAI upstream
    POST /v1/responses           OpenAI Responses    -> OPENAI upstream (Codex CLI)
    anything else                forwarded unchanged to the upstream chosen by path

Recall: the proxy cannot add a tool to someone else's harness, so stubs point to the original's
file path (readable with any file-read tool) and the proxy also serves GET /cmi/<key>.

Sessions: one CMI engine per conversation, keyed by (route, model, system prompt, first user
message) hash, kept in an LRU. Credentials and headers are passed through as-is and never logged.
Stdlib only.
"""
from __future__ import annotations

import argparse
import hashlib
import hmac
import http.client
import json
import logging
import os
import ssl
import sys
import threading
import time
from collections import OrderedDict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlsplit

from .compat import default_home
from .engine import CMIEngine
from .store import RecallStore
from .wire import strip_cache_control, to_canonical

log = logging.getLogger("cmi.proxy")

HOP_BY_HOP = {"connection", "keep-alive", "proxy-authenticate", "proxy-authorization", "te",
              "trailers", "transfer-encoding", "upgrade", "host", "content-length",
              "accept-encoding"}
# Never forwarded upstream: the proxy's own secret.
PRIVATE_HEADERS = {"x-cmi-token"}
# The proxy's BaseHTTPRequestHandler writes its own.
RESPONSE_DROP = {"server", "date"}
# Headers that only make sense for one provider (credentials included).
ANTHROPIC_ONLY = {"x-api-key", "anthropic-version", "anthropic-beta"}
MAX_BODY = 64 * 1024 * 1024
DEFAULT_CONTEXT = 200_000


# ---- Wire formats ------------------------------------------------------------------------

ROUTES = {
    "/v1/messages": "anthropic",
    "/v1/chat/completions": "openai",
    "/chat/completions": "openai",
    "/v1/responses": "responses",
    "/responses": "responses",
}


# ---- Sessions ----------------------------------------------------------------------------

class Sessions:
    """One engine per conversation. A conversation is identified by what never changes across
    its requests: route, model, system prompt, the first user message and, once it exists, the
    first assistant turn's tool-call ids (or text). The first prompt alone is not enough: two runs
    started with the same prompt would share one engine, and the second would inherit the first
    one's learned state (live test: it was never compressed)."""

    def __init__(self, home: str, capacity: int = 64) -> None:
        self.home = home
        self.capacity = capacity
        self._lru: "OrderedDict[str, Tuple[CMIEngine, threading.Lock, List[int]]]" = OrderedDict()
        self._lock = threading.Lock()

    @staticmethod
    def _base(route: str, body: Dict[str, Any], msgs: List[Dict[str, Any]]) -> "hashlib._Hash":
        first_user = next((m for m in msgs if isinstance(m, dict) and m.get("role") == "user"), None)
        system = body.get("system") if "system" in body else body.get("instructions")
        h = hashlib.sha256()
        for part in (route, str(body.get("model", "")), json.dumps(strip_cache_control(system), sort_keys=True, default=str),
                     json.dumps(strip_cache_control(first_user), sort_keys=True, default=str)):
            h.update(part.encode("utf-8", "replace"))
            h.update(b"\0")
        return h

    @staticmethod
    def _first_turn(msgs: List[Dict[str, Any]]) -> Optional[str]:
        """Fingerprint of the first assistant turn: tool-call ids (unique per run) and text.
        Thinking blocks are left out: a client may drop old-turn thinking later in the run."""
        m = next((m for m in msgs if isinstance(m, dict) and m.get("role") == "assistant"), None)
        if m is None:
            return None
        parts: List[str] = [str((tc or {}).get("id") or "") for tc in m.get("tool_calls") or []
                            if isinstance(tc, dict)]
        c = m.get("content")
        if isinstance(c, str):
            parts.append(c)
        elif isinstance(c, list):
            for p in c:
                if not isinstance(p, dict):
                    continue
                if p.get("type") == "text":
                    parts.append(str(p.get("text") or ""))
                elif p.get("type") in ("tool_use", "server_tool_use"):
                    parts.append(str(p.get("id") or ""))
        return json.dumps(parts)

    @staticmethod
    def key(route: str, body: Dict[str, Any], msgs: List[Dict[str, Any]]) -> str:
        h = Sessions._base(route, body, msgs)
        turn = Sessions._first_turn(msgs)
        if turn is not None:
            h.update(b"turn\0" + turn.encode("utf-8", "replace"))
        return h.hexdigest()[:24]

    @staticmethod
    def pending_key(route: str, body: Dict[str, Any], msgs: List[Dict[str, Any]]) -> str:
        """The key this conversation had before its first assistant turn existed."""
        return Sessions._base(route, body, msgs).hexdigest()[:24]

    def get(self, key: str, model: str, adopt: Optional[str] = None) -> Tuple[CMIEngine, threading.Lock, List[int]]:
        with self._lock:
            hit = self._lru.get(key)
            if hit is not None:
                self._lru.move_to_end(key)
                return hit
            # The engine that saw this run's first request (before any assistant turn) carries
            # on under the full key; a second run with the same prompt then starts fresh.
            entry = self._lru.pop(adopt, None) if adopt and adopt != key else None
            if entry is None:
                eng = CMIEngine()
                eng.recall_via_file = True
                eng.on_session_start(key, hermes_home=self.home)
                eng.update_model(model=model or "unknown", context_length=DEFAULT_CONTEXT)
                entry = (eng, threading.Lock(), [0])  # [0] = message count at the last user turn learned
            self._lru[key] = entry
            while len(self._lru) > self.capacity:
                self._lru.popitem(last=False)
            return entry


def _request_cache_ttl(body: Dict[str, Any], default: float) -> float:
    """Anthropic cache_control markers carry the TTL the client asked for ('1h' or default 5m)."""
    def marks(x):
        if isinstance(x, dict):
            cc = x.get("cache_control")
            if isinstance(cc, dict):
                yield cc
            c = x.get("content")
            if isinstance(c, list):
                for p in c:
                    yield from marks(p)
        elif isinstance(x, list):
            for p in x:
                yield from marks(p)
    ttls = {str(cc.get("ttl") or "5m") for cc in marks([body.get("system"), body.get("tools"),
                                                          *(body.get("messages") or [])[-4:]])}
    if "1h" in ttls:
        return 3600.0
    return 300.0 if ttls else default


def compile_request(sessions: Sessions, route: str, body: Dict[str, Any]) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """Return (body to send upstream, stats). Never raises: on any error the body is unchanged."""
    fmt = ROUTES[route]
    stats: Dict[str, Any] = {"route": fmt, "messages": 0, "changed": False}
    try:
        msgs, back = to_canonical(fmt, body)
    except Exception:
        log.warning("CMI could not read the %s body; forwarding unchanged", fmt, exc_info=True)
        return body, stats
    stats["messages"] = len(msgs)
    if not msgs:
        return body, stats
    try:
        key = Sessions.key(route, body, msgs)
        eng, lock, learned = sessions.get(key, str(body.get("model", "")),
                                          adopt=Sessions.pending_key(route, body, msgs))
    except Exception:
        log.warning("CMI could not open a session; forwarding unchanged", exc_info=True)
        return body, stats
    eng.cache_ttl_s = _request_cache_ttl(body, eng.cache_ttl_s)
    with lock:
        try:
            # Learn from every completed turn once (the host normally does this after each turn).
            last_user = max((i for i, m in enumerate(msgs) if _is_user_turn(m)), default=-1)
            if last_user > learned[0]:
                eng.on_turn_complete(msgs[:last_user])
                learned[0] = last_user
            out = eng.select_context(msgs)
            new_body = back(out) if out else None
        except Exception:
            log.warning("CMI compile failed; forwarding unchanged", exc_info=True)
            return body, stats
    if new_body is None:
        return body, stats
    before = len(json.dumps(body, ensure_ascii=False))
    after = len(json.dumps(new_body, ensure_ascii=False))
    stats.update(changed=True, chars_before=before, chars_after=after, session=key[:8])
    return new_body, stats


def _is_user_turn(m: Any) -> bool:
    if not isinstance(m, dict) or m.get("role") != "user":
        return False
    c = m.get("content")
    if isinstance(c, list):
        return not any(isinstance(p, dict) and p.get("type") == "tool_result" for p in c)
    return True


# ---- HTTP --------------------------------------------------------------------------------

class Upstream:
    def __init__(self, url: str) -> None:
        u = urlsplit(url)
        self.scheme, self.host, self.port = u.scheme, u.hostname, u.port
        self.base_path = u.path.rstrip("/")
        self.ctx = ssl.create_default_context() if u.scheme == "https" else None

    def connect(self) -> http.client.HTTPConnection:
        if self.scheme == "https":
            return http.client.HTTPSConnection(self.host, self.port or 443, context=self.ctx, timeout=600)
        return http.client.HTTPConnection(self.host, self.port or 80, timeout=600)


LOOPBACK_NAMES = {"localhost", "127.0.0.1", "::1"}


def is_loopback_bind(host: str) -> bool:
    return host in LOOPBACK_NAMES


def _host_of(header: str) -> str:
    """Hostname part of a Host header (IPv6 brackets and port removed, lowercased)."""
    h = (header or "").strip().lower()
    if h.startswith("["):
        return h[1:h.find("]")] if "]" in h else ""
    return h.rsplit(":", 1)[0] if h.count(":") == 1 else h


def check_request(headers: Any, allowed_hosts: set, token: str, needs_token: bool) -> Optional[Tuple[int, str]]:
    """Gate every request. Returns (status, reason) to refuse, or None to accept.

    - Host must be an allowed name: a web page reaching 127.0.0.1 through a DNS name it controls
      (DNS rebinding) sends its own name here and is refused.
    - A browser always sends Origin on cross-origin requests; CLI clients never do. Any Origin is refused.
    - Recall endpoints (stored originals) require the token when one is configured.
    """
    if _host_of(headers.get("host", "")) not in allowed_hosts:
        return 403, "host not allowed"
    if headers.get("origin") is not None:
        return 403, "browser requests are not allowed"
    if needs_token and token:
        given = headers.get("x-cmi-token") or ""
        if not hmac.compare_digest(given.encode(), token.encode()):
            return 401, "missing or invalid x-cmi-token"
    return None


def make_handler(sessions: Sessions, anthropic: Upstream, openai: Upstream, verbose: bool,
                 allowed_hosts: Optional[set] = None, token: str = ""):
    allowed_hosts = set(allowed_hosts) if allowed_hosts is not None else set(LOOPBACK_NAMES)
    _store: List[RecallStore] = []

    def _recall_store() -> RecallStore:
        # Read-only lookup: no engine, no session start, no prune per GET.
        if not _store:
            _store.append(RecallStore(Path(sessions.home) / "cmi_store"))
        return _store[0]

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        server_version = "cmi-proxy"

        def log_message(self, fmt: str, *args: Any) -> None:  # no request logging (credentials)
            if verbose:
                log.info("%s %s", self.command, self.path.split("?")[0])

        def _refuse(self, needs_token: bool) -> bool:
            verdict = check_request(self.headers, allowed_hosts, token, needs_token)
            if verdict is None:
                return False
            self.close_connection = True
            self._send_json(verdict[0], {"error": verdict[1]})
            return True

        def _refuse_target(self) -> bool:
            # Origin-form only: an absolute URL would set the upstream Host header.
            if self.path.startswith("/"):
                return False
            self.close_connection = True
            self._send_json(400, {"error": "cmi-proxy accepts origin-form paths only"})
            return True

        def do_GET(self) -> None:
            internal = self.path.startswith("/cmi/") or self.path == "/cmi-health"
            if self._refuse(needs_token=internal) or self._refuse_target():
                return
            if self.path.startswith("/cmi/"):
                return self._serve_recall(self.path[5:])
            if self.path == "/cmi-health":
                return self._send_json(200, {"ok": True, "sessions": len(sessions._lru)})
            self._forward(None)

        def do_POST(self) -> None:
            if self._refuse(needs_token=False) or self._refuse_target():
                return
            if self.headers.get("transfer-encoding"):
                self.close_connection = True
                return self._send_json(411, {"error": "cmi-proxy needs a content-length body"})
            try:
                length = int(self.headers.get("content-length") or 0)
            except ValueError:
                length = -1
            if length < 0:
                self.close_connection = True
                return self._send_json(400, {"error": "bad content-length"})
            if length > MAX_BODY:
                self.close_connection = True
                return self._send_json(413, {"error": "request too large for cmi-proxy"})
            raw = self.rfile.read(length) if length else b""
            path = self.path.split("?")[0]
            if path in ROUTES and raw:
                try:
                    body = json.loads(raw)
                except Exception:  # ValueError, RecursionError on absurd nesting...
                    body = None
                if isinstance(body, dict):
                    t = time.perf_counter()
                    new_body, stats = compile_request(sessions, path, body)
                    if stats.get("changed"):
                        try:
                            # ASCII escapes: a lone surrogate from a cut emoji survives as \udXXX.
                            raw = json.dumps(new_body).encode("ascii")
                        except Exception:
                            log.warning("CMI could not serialize the compiled body; forwarding original",
                                        exc_info=True)
                        if verbose:
                            log.info("cmi %s session=%s msgs=%d %d -> %d chars (%.0f ms)", stats["route"],
                                     stats["session"], stats["messages"], stats["chars_before"],
                                     stats["chars_after"], (time.perf_counter() - t) * 1000)
            self._forward(raw)

        def _serve_recall(self, key: str) -> None:
            content = _recall_store().get(key)
            if content is None:
                return self._send_json(404, {"error": "unknown key"})
            data = content.encode("utf-8", "surrogatepass")
            self.send_response(200)
            self.send_header("content-type", "text/plain; charset=utf-8")
            self.send_header("content-length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _send_json(self, code: int, obj: Dict[str, Any]) -> None:
            data = json.dumps(obj).encode()
            self.send_response(code)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _forward(self, raw: Optional[bytes]) -> None:
            path = self.path
            names = {k.lower() for k in self.headers.keys()}
            is_anthropic = path.startswith("/v1/messages") or bool(names & ANTHROPIC_ONLY)
            up = anthropic if is_anthropic else openai
            conn_listed = {h.strip().lower() for h in (self.headers.get("connection") or "").split(",")}
            drop = HOP_BY_HOP | PRIVATE_HEADERS | conn_listed
            if not is_anthropic:
                drop = drop | ANTHROPIC_ONLY
            headers = {k: v for k, v in self.headers.items() if k.lower() not in drop}
            if raw is not None:
                headers["content-length"] = str(len(raw))
            try:
                conn = up.connect()
                conn.request(self.command, up.base_path + path, body=raw, headers=headers)
                resp = conn.getresponse()
            except (OSError, http.client.HTTPException) as exc:
                return self._send_json(502, {"error": f"cmi-proxy: upstream unreachable: {exc.__class__.__name__}"})
            self.send_response(resp.status, resp.reason)
            for k, v in resp.getheaders():
                if k.lower() not in HOP_BY_HOP | RESPONSE_DROP:
                    self.send_header(k, v)
            if self.command == "HEAD" or resp.status in (204, 304) or 100 <= resp.status < 200:
                self.send_header("content-length", "0") if resp.status not in (204, 304) else None
                self.end_headers()
                conn.close()
                return
            self.send_header("transfer-encoding", "chunked")
            self.end_headers()
            try:
                while True:
                    chunk = resp.read1(65536) if hasattr(resp, "read1") else resp.read(65536)
                    if not chunk:
                        break
                    self.wfile.write(b"%x\r\n%s\r\n" % (len(chunk), chunk))
                    self.wfile.flush()
                self.wfile.write(b"0\r\n\r\n")
            except (BrokenPipeError, ConnectionResetError):
                pass
            finally:
                conn.close()

    return Handler


def main(argv: Optional[List[str]] = None) -> None:
    args = list(sys.argv[1:] if argv is None else argv)
    if args and args[0] in ("install-service", "uninstall-service", "status"):
        from . import service
        sys.exit(service.run(args[0], args[1:]))
    ap = argparse.ArgumentParser(prog="cmi-proxy", description=__doc__.split("\n\n")[0])
    ap.add_argument("--host", default="127.0.0.1", help="bind address (default 127.0.0.1: local only)")
    ap.add_argument("--port", type=int, default=8787)
    ap.add_argument("--token", default=os.environ.get("CMI_PROXY_TOKEN", ""),
                    help="secret required (header x-cmi-token) to read /cmi/<key>; mandatory for a non-loopback bind")
    ap.add_argument("--allow-host", action="append", default=[],
                    help="extra Host header name to accept (non-loopback deployments)")
    ap.add_argument("--anthropic-upstream", default=os.environ.get("CMI_ANTHROPIC_UPSTREAM", "https://api.anthropic.com"))
    ap.add_argument("--openai-upstream", default=os.environ.get("CMI_OPENAI_UPSTREAM", "https://api.openai.com"))
    ap.add_argument("--home", default=str(default_home()), help="where the recall store lives")
    ap.add_argument("-v", "--verbose", action="store_true", help="log one line per compiled request (no content)")
    a = ap.parse_args(args)
    logging.basicConfig(level=logging.INFO if a.verbose else logging.WARNING, format="%(asctime)s %(message)s")
    if not is_loopback_bind(a.host) and not a.token:
        ap.error("--host %s exposes the proxy beyond this machine: set --token (or CMI_PROXY_TOKEN). "
                 "The proxy is not a hardened network gateway; see SECURITY.md." % a.host)
    allowed = set(LOOPBACK_NAMES) | {h.lower() for h in a.allow_host}
    if not is_loopback_bind(a.host) and a.host not in ("0.0.0.0", "::"):
        allowed.add(a.host.lower())
    sessions = Sessions(a.home)
    srv = ThreadingHTTPServer((a.host, a.port), make_handler(sessions, Upstream(a.anthropic_upstream),
                                                              Upstream(a.openai_upstream), a.verbose,
                                                              allowed_hosts=allowed, token=a.token))
    print(f"cmi-proxy on http://{a.host}:{a.port}  (anthropic -> {a.anthropic_upstream}, openai -> {a.openai_upstream})",
          file=sys.stderr)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
