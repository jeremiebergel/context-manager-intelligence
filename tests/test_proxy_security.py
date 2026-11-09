"""Proxy access gate: DNS rebinding, browser origins, recall token, non-loopback bind."""
import http.client
import threading
from http.server import ThreadingHTTPServer

import pytest

from cmi import proxy


def _serve(tmp_path, **kw):
    handler = proxy.make_handler(proxy.Sessions(str(tmp_path)), proxy.Upstream("http://127.0.0.1:9"),
                                 proxy.Upstream("http://127.0.0.1:9"), False, **kw)
    srv = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def _req(srv, path, headers=None, method="GET"):
    c = http.client.HTTPConnection("127.0.0.1", srv.server_port, timeout=5)
    c.request(method, path, headers=headers or {})
    r = c.getresponse()
    r.read()
    c.close()
    return r.status


@pytest.fixture
def srv(tmp_path):
    s = _serve(tmp_path, token="s3cret")
    yield s
    s.shutdown()


def test_loopback_host_and_token_accepted(srv):
    assert _req(srv, "/cmi-health", {"x-cmi-token": "s3cret"}) == 200


def test_rebound_host_refused(srv):
    # a web page on evil.example resolving to 127.0.0.1 sends its own name as Host
    assert _req(srv, "/cmi-health", {"Host": "evil.example", "x-cmi-token": "s3cret"}) == 403
    assert _req(srv, "/v1/models", {"Host": "evil.example"}) == 403


def test_browser_origin_refused(srv):
    assert _req(srv, "/cmi-health", {"Origin": "https://evil.example", "x-cmi-token": "s3cret"}) == 403
    assert _req(srv, "/v1/messages", {"Origin": "https://evil.example"}, method="POST") == 403


def test_recall_needs_token(srv):
    assert _req(srv, "/cmi/" + "a" * 16) == 401
    assert _req(srv, "/cmi/" + "a" * 16, {"x-cmi-token": "wrong"}) == 401


def test_no_token_configured_keeps_local_recall_open(tmp_path):
    s = _serve(tmp_path)
    try:
        assert _req(s, "/cmi-health") == 200
        assert _req(s, "/cmi-health", {"Host": "evil.example"}) == 403
    finally:
        s.shutdown()


def test_bad_content_length_is_a_400_not_a_crash(srv):
    c = http.client.HTTPConnection("127.0.0.1", srv.server_port, timeout=5)
    c.putrequest("POST", "/v1/messages")
    c.putheader("content-length", "abc")
    c.endheaders()
    assert c.getresponse().status == 400


def test_host_header_parsing():
    assert proxy._host_of("localhost:8787") == "localhost"
    assert proxy._host_of("[::1]:8787") == "::1"
    assert proxy._host_of("EVIL.example") == "evil.example"


def test_non_loopback_bind_requires_token(capsys, monkeypatch):
    monkeypatch.delenv("CMI_PROXY_TOKEN", raising=False)
    with pytest.raises(SystemExit):
        proxy.main(["--host", "0.0.0.0"])
    assert "--token" in capsys.readouterr().err
