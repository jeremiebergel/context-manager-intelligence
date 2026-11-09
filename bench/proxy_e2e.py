"""End-to-end proxy check against a fake upstream (no network, no API key).

Starts a fake Anthropic + OpenAI upstream that records what it receives and streams back a
canned SSE response, starts cmi-proxy in front of it, sends a long real-shaped conversation
through the proxy in both formats, and checks:
  - the upstream received a compiled (smaller) request with every tool pair intact
  - headers (api key, anthropic-version) passed through unchanged
  - the streamed response reaches the client byte for byte
  - a stub's key is recallable through GET /cmi/<key> and via the file path in the stub
  - a malformed body is forwarded untouched
"""
from __future__ import annotations

import json
import re
import sys
import tempfile
import threading
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from cmi import proxy  # noqa: E402

RECEIVED = []
SSE = b"event: message_start\ndata: {\"type\":\"message_start\"}\n\nevent: content_block_delta\ndata: {\"delta\":{\"text\":\"hello\"}}\n\n"


class FakeUpstream(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def do_POST(self):
        body = self.rfile.read(int(self.headers["content-length"]))
        RECEIVED.append((self.path, dict(self.headers), body))
        self.send_response(200)
        self.send_header("content-type", "text/event-stream")
        self.send_header("content-length", str(len(SSE)))
        self.end_headers()
        self.wfile.write(SSE)


def serve(handler, port=0):
    srv = ThreadingHTTPServer(("127.0.0.1", port), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def conversation(fmt):
    big = "".join(f"line {i}: value={i * 7}\n" for i in range(900))
    msgs = [{"role": "user", "content": "read the config"}]
    if fmt == "anthropic":
        msgs += [{"role": "assistant", "content": [{"type": "tool_use", "id": "toolu_A", "name": "read_file", "input": {"path": "/srv/app/config.ini"}}]},
                 {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "toolu_A", "content": big}]},
                 {"role": "assistant", "content": [{"type": "text", "text": "read"}]}]
        for t in range(8):
            msgs += [{"role": "user", "content": f"question {t}"}, {"role": "assistant", "content": [{"type": "text", "text": f"answer {t}"}]}]
    elif fmt == "responses":
        msgs = [{"type": "message", "role": "user", "content": [{"type": "input_text", "text": "read the config"}]},
                {"type": "reasoning", "id": "rs_1", "summary": [], "encrypted_content": "gAAAAB" + "q" * 80},
                {"type": "function_call", "call_id": "call_A", "name": "read_file", "arguments": json.dumps({"path": "/srv/app/config.ini"})},
                {"type": "function_call_output", "call_id": "call_A", "output": big},
                {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "read"}]}]
        for t in range(8):
            msgs += [{"type": "message", "role": "user", "content": [{"type": "input_text", "text": f"question {t}"}]},
                     {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": f"answer {t}"}]}]
        msgs.append({"type": "message", "role": "user", "content": [{"type": "input_text", "text": "and now?"}]})
        return msgs, big
    else:
        msgs += [{"role": "assistant", "content": None, "tool_calls": [{"id": "call_A", "type": "function", "function": {"name": "read_file", "arguments": json.dumps({"path": "/srv/app/config.ini"})}}]},
                 {"role": "tool", "tool_call_id": "call_A", "content": big},
                 {"role": "assistant", "content": "read"}]
        for t in range(8):
            msgs += [{"role": "user", "content": f"question {t}"}, {"role": "assistant", "content": f"answer {t}"}]
    msgs.append({"role": "user", "content": "and now?"})
    return msgs, big


def post(url, body, headers):
    req = urllib.request.Request(url, data=body, headers={"content-type": "application/json", **headers}, method="POST")
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.status, r.read()


def main():
    up = serve(FakeUpstream)
    upurl = f"http://127.0.0.1:{up.server_address[1]}"
    home = tempfile.mkdtemp(prefix="cmi_proxy_")
    sessions = proxy.Sessions(home)
    px = serve(proxy.make_handler(sessions, proxy.Upstream(upurl), proxy.Upstream(upurl), False))
    base = f"http://127.0.0.1:{px.server_address[1]}"
    ok = True
    for fmt, path, hdr in (("anthropic", "/v1/messages", {"x-api-key": "sk-test-A", "anthropic-version": "2023-06-01"}),
                           ("openai", "/v1/chat/completions", {"authorization": "Bearer sk-test-B"}),
                           ("responses", "/v1/responses", {"authorization": "Bearer sk-test-B"})):
        msgs, big = conversation(fmt)
        field = "input" if fmt == "responses" else "messages"
        body = {"model": "claude-sonnet-5" if fmt == "anthropic" else "gpt-5", "max_tokens": 100, "stream": True, field: msgs}
        if fmt == "anthropic":
            body["system"] = "You are helpful."
        # two requests: the first teaches the engine, the second shows the steady state
        for _ in range(2):
            status, resp = post(base + path, json.dumps(body).encode(), hdr)
        upath, uhdr, ubody = RECEIVED[-1]
        sent = json.loads(ubody)[field]
        flat = json.dumps(sent)
        keys = re.findall(r"cmi:([0-9a-f]{12,64})", flat)
        paths = re.findall(r"full text: ([^)]+)\)", flat)
        if fmt == "responses":
            ids_call = {m["call_id"] for m in sent if m.get("type") == "function_call"}
            ids_res = {m["call_id"] for m in sent if m.get("type") == "function_call_output"}
        else:
            ids_call = set(re.findall(r'"(?:id)": "((?:toolu|call)_[A-Za-z0-9]+)"', flat))
            ids_res = set(re.findall(r'"(?:tool_use_id|tool_call_id)": "((?:toolu|call)_[A-Za-z0-9]+)"', flat))
        recall_http = urllib.request.urlopen(f"{base}/cmi/{keys[0]}").read().decode() if keys else None
        recall_file = Path(paths[0]).read_text() if paths else None
        checks = {
            "stream_intact": status == 200 and resp == SSE,
            "path_kept": upath == path,
            "auth_passthrough": uhdr.get("x-api-key", uhdr.get("authorization", uhdr.get("Authorization", uhdr.get("X-Api-Key")))) in ("sk-test-A", "Bearer sk-test-B"),
            "compiled_smaller": len(ubody) < len(json.dumps(body)),
            "same_message_count": len(sent) == len(msgs),
            "pairs_intact": ids_call == ids_res and len(ids_call) == 1,
            "recall_http_exact": recall_http == big,
            "recall_file_exact": recall_file == big,
            "untouched_items_identical": all(a == b for a, b in zip(sent, msgs) if "cmi:" not in json.dumps(a)),
        }
        ok &= all(checks.values())
        print(fmt, f"{len(json.dumps(body))} -> {len(ubody)} bytes", checks)
    # malformed body passes through untouched
    raw = b'{"messages": [ this is not json'
    try:
        post(base + "/v1/messages", raw, {"x-api-key": "k"})
    except Exception:
        pass
    passthrough = RECEIVED[-1][2] == raw
    print("malformed_passthrough", passthrough)
    ok &= passthrough
    print("ALL OK" if ok else "FAILURES")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
