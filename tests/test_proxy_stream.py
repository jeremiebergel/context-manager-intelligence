"""Upstream stream broken mid-response: the proxy must end the client connection at once
(truncated response, no final chunk) instead of raising or leaving a half-open socket."""
import http.client
import logging
import socket
import threading
import time
from http.server import ThreadingHTTPServer

from cmi import proxy


def _broken_upstream():
    """Answers 200 chunked, sends one chunk, then closes without the terminating chunk."""
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    s.listen(4)

    def run():
        while True:
            try:
                c, _ = s.accept()
            except OSError:
                return
            # Read the WHOLE request first: closing with unread bytes makes Windows send a RST,
            # which the proxy would see before the status line (a 502, not a mid-stream cut).
            data = b""
            c.settimeout(5)
            while b"}]}" not in data:
                part = c.recv(65536)
                if not part:
                    break
                data += part
            c.sendall(b"HTTP/1.1 200 OK\r\ncontent-type: text/event-stream\r\ntransfer-encoding: chunked\r\n\r\n"
                      b"5\r\nhello\r\n")
            c.close()

    threading.Thread(target=run, daemon=True).start()
    return s


def test_upstream_cut_mid_stream_closes_client_promptly(tmp_path, caplog):
    up = _broken_upstream()
    url = f"http://127.0.0.1:{up.getsockname()[1]}"
    handler = proxy.make_handler(proxy.Sessions(str(tmp_path)), proxy.Upstream(url), proxy.Upstream(url), False)
    srv = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        c = http.client.HTTPConnection("127.0.0.1", srv.server_port, timeout=10)
        body = b'{"model":"m","max_tokens":1,"messages":[{"role":"user","content":"hi"}]}'
        t0 = time.time()
        with caplog.at_level(logging.WARNING):
            c.request("POST", "/v1/messages", body=body,
                      headers={"content-type": "application/json", "x-api-key": "k", "host": "127.0.0.1"})
            r = c.getresponse()
            assert r.status == 200
            got = b""
            try:
                got = r.read()
            except http.client.IncompleteRead as e:  # the honest signal: truncated, not completed
                got = e.partial
            else:
                raise AssertionError("a broken upstream stream must not look like a complete response")
        assert got == b"hello"
        assert time.time() - t0 < 5
        assert any("upstream stream interrupted" in m for m in caplog.messages)
    finally:
        srv.shutdown()
        up.close()
