"""A message sent to a peer Host (lib.server_peers.send) goes through the peer opener: no
environment proxy sees the token, and a redirect is refused, never followed with it."""
from __future__ import annotations

import json
import threading
import urllib.error
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from lib import server_peers

TOKEN = "cld_peer-send-token-kept-on-the-host-0123456789"


class Server:
    """A fake Host that records every request; `redirect` answers /send with a 307 to `target`."""

    def __init__(self, redirect_to=""):
        self.requests = []
        me = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length)
                me.requests.append((self.path, dict(self.headers), body))
                if redirect_to:
                    self.send_response(307)
                    self.send_header("Location", redirect_to + "/send")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                data = json.dumps({"ok": True}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_port}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def peers_file(tmp_path, monkeypatch):
    monkeypatch.setattr(server_peers, "PATH", tmp_path / "peers.json")
    return tmp_path / "peers.json"


def test_a_send_reaches_the_peer_with_its_token_and_no_proxy(peers_file, monkeypatch):
    for name in ("http_proxy", "HTTP_PROXY", "https_proxy", "HTTPS_PROXY"):
        monkeypatch.setenv(name, "http://127.0.0.1:9")   # a dead proxy: urlopen would go there
    monkeypatch.delenv("no_proxy", raising=False)
    monkeypatch.delenv("NO_PROXY", raising=False)
    peer = Server()
    try:
        server_peers.add("work", peer.url, TOKEN)
        assert server_peers.send("work", {"session": "rachel", "text": "hi"}) == {"ok": True}
        [(path, headers, body)] = peer.requests
        assert path == "/send" and headers["Authorization"] == "Bearer " + TOKEN
        assert headers["X-Clarp-Peer"] == "work" and json.loads(body)["text"] == "hi"
    finally:
        peer.close()


def test_a_redirect_is_refused_and_never_carries_the_token(peers_file):
    elsewhere = Server()
    peer = Server(redirect_to=elsewhere.url)
    try:
        server_peers.add("work", peer.url, TOKEN)
        with pytest.raises(urllib.error.HTTPError) as refused:
            server_peers.send("work", {"session": "rachel", "text": "hi"})
        assert refused.value.code == 307
        assert len(peer.requests) == 1
        assert elsewhere.requests == []   # the redirect target never saw the request or the token
    finally:
        peer.close()
        elsewhere.close()
