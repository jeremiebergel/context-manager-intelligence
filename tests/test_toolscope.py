"""Per-job tool allowlist (x-cmi-tools-allow): trims schemas, fails open, keeps history valid."""
import json
import socket
import threading
from http.server import ThreadingHTTPServer
import http.client

from cmi import proxy
from cmi.toolscope import parse_allow, scope_tools


def _tools(*names):
    return [{"name": n, "description": "d" * 50, "input_schema": {"type": "object"}} for n in names]


def test_keeps_only_allowed_tools():
    b = {"tools": _tools("read_file", "terminal", "browser", "figma"), "messages": []}
    out = scope_tools(b, {"read_file", "terminal"})
    assert [t["name"] for t in out["tools"]] == ["read_file", "terminal"]
    assert len(b["tools"]) == 4  # input untouched


def test_no_header_or_typo_or_nothing_to_drop_is_noop():
    b = {"tools": _tools("a", "b")}
    assert scope_tools(b, set()) is None
    assert scope_tools(b, {"typo"}) is None  # never strip to nothing
    assert scope_tools(b, {"a", "b"}) is None
    assert scope_tools({"messages": []}, {"a"}) is None


def test_tools_used_in_history_and_tool_choice_are_kept():
    b = {"tools": _tools("a", "b", "c", "d"),
         "messages": [{"role": "assistant", "content": [{"type": "tool_use", "name": "c", "id": "1", "input": {}}]}],
         "tool_choice": {"type": "tool", "name": "d"}}
    assert [t["name"] for t in scope_tools(b, {"a"})["tools"]] == ["a", "c", "d"]


def test_openai_chat_and_responses_shapes():
    chat = {"tools": [{"type": "function", "function": {"name": n}} for n in ("a", "b")],
            "messages": [{"role": "assistant", "tool_calls": [{"id": "1", "function": {"name": "b"}}]}]}
    assert scope_tools(chat, {"a"}) is None  # b is used in history: nothing to drop
    chat["messages"] = []
    assert len(scope_tools(chat, {"a"})["tools"]) == 1
    resp = {"tools": [{"type": "function", "name": n} for n in ("a", "b", "c")],
            "input": [{"type": "function_call", "name": "c", "call_id": "1"}]}
    assert [t["name"] for t in scope_tools(resp, {"a"})["tools"]] == ["a", "c"]


def test_cache_breakpoint_moves_to_new_last_tool():
    tools = _tools("a", "b", "c")
    tools[-1]["cache_control"] = {"type": "ephemeral", "ttl": "1h"}
    out = scope_tools({"tools": tools}, {"a"})
    assert out["tools"][-1]["name"] == "a" and out["tools"][-1]["cache_control"]["ttl"] == "1h"


def test_parse_allow():
    assert parse_allow(" read_file, terminal ,,") == {"read_file", "terminal"}
    assert parse_allow(None) == set()


def test_proxy_trims_tools_and_strips_header(tmp_path):
    seen = {}
    up = socket.socket()
    up.bind(("127.0.0.1", 0))
    up.listen(2)

    def run():
        c, _ = up.accept()
        data = b""
        c.settimeout(5)
        while b"\r\n\r\n" not in data:
            data += c.recv(65536)
        head, _, rest = data.partition(b"\r\n\r\n")
        n = int([h.split(b":")[1] for h in head.split(b"\r\n") if h.lower().startswith(b"content-length")][0])
        while len(rest) < n:
            rest += c.recv(65536)
        seen["head"], seen["body"] = head.lower(), json.loads(rest)
        c.sendall(b"HTTP/1.1 200 OK\r\ncontent-length: 2\r\n\r\n{}")
        c.close()

    threading.Thread(target=run, daemon=True).start()
    sessions = proxy.Sessions(str(tmp_path))
    allowed = {"127.0.0.1", "localhost"}
    handler = proxy.make_handler(sessions, proxy.Upstream(f"http://127.0.0.1:{up.getsockname()[1]}"),
                                 proxy.Upstream("http://127.0.0.1:9"), False, allowed_hosts=allowed, token="")
    srv = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        body = {"model": "claude-sonnet-5", "max_tokens": 5, "tools": _tools("read_file", "terminal", "figma"),
                "messages": [{"role": "user", "content": "hi"}]}
        c = http.client.HTTPConnection("127.0.0.1", srv.server_address[1], timeout=10)
        c.request("POST", "/v1/messages", json.dumps(body),
                  {"content-type": "application/json", "x-cmi-tools-allow": "read_file,terminal"})
        assert c.getresponse().status == 200
    finally:
        srv.shutdown()
    assert [t["name"] for t in seen["body"]["tools"]] == ["read_file", "terminal"]
    assert b"x-cmi-tools-allow" not in seen["head"]
