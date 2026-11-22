"""This Host reading its peers' chat lists server to server (issue 651, feature peer_snapshot):
only configured peers, only two paths, the token only in the Authorization header to that peer,
never returned; refusals, redirects and size are contained."""
from __future__ import annotations

import http.client
import json
import os
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from lib import device_pairing, peer_reads, server_peers
from tests.integration.test_attention_questions_http import host, server_module  # noqa: F401

TOKEN = "cld_peer-token-kept-on-the-host-only-0123456789"


class Peer:
    """A fake peer Host: records every request, answers by its mode."""

    def __init__(self, mode="ok", server_id="peer-sid", name="host-b"):
        self.mode, self.server_id, self.name, self.requests = mode, server_id, name, []
        peer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _answer(self, status, body, headers=None):
                data = json.dumps(body).encode() if not isinstance(body, bytes) else body
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                for k, v in (headers or {}).items():
                    self.send_header(k, v)
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                peer.requests.append(("GET", self.path, dict(self.headers)))
                if peer.mode == "redirect":
                    return self._answer(302, {}, {"Location": "http://127.0.0.1:9/elsewhere"})
                if self.path == "/pairing/identity":
                    return self._answer(200, {"server_id": peer.server_id, "name": peer.name, "contract": {"host": 1}})
                if self.headers.get("Authorization") != "Bearer " + TOKEN or peer.mode == "refused":
                    return self._answer(401, {"error": "unauthorized"})
                if peer.mode == "huge":
                    return self._answer(200, b'{"x":"' + b"a" * (peer_reads.MAX_BYTES + 10) + b'"}')
                if self.path == "/server-info":
                    return self._answer(200, {"server_id": peer.server_id, "name": peer.name})
                if self.path == "/agents/snapshot":
                    return self._answer(200, {"agents": [{"session": "ada", "name": "Ada", "agent_id": "a1"}]})
                return self._answer(404, {"error": "not found"})

            def do_POST(self):
                length = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(length) or b"{}")
                peer.requests.append(("POST", self.path, body))
                if self.path == "/pairing/exchange" and body.get("code") == "clp_good-code-good-code-good-code":
                    return self._answer(201, {"device": {"token": TOKEN, "scope": "limited"}, "server": {}})
                return self._answer(409, {"error": "invalid pairing code"})

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_port}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def peers_file(tmp_path, monkeypatch):
    monkeypatch.setattr(server_peers, "PATH", tmp_path / "peers.json")
    server_peers._learned.clear()
    peer_reads.reset_cache()
    yield tmp_path / "peers.json"
    peer_reads.reset_cache()
    server_peers._learned.clear()


@pytest.fixture
def peer(peers_file):
    p = Peer()
    yield p
    p.close()


def _closed_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


# ---- the registry ---------------------------------------------------------------------------

@pytest.mark.parametrize("url,ok", [
    ("https://studio.example.ts.net", True), ("http://100.64.0.2:7682/", True),
    ("https://studio.example.ts.net/clarp", True), ("ftp://x", False), ("https://u:p@x", False),
    ("https://x/?a=1", False), ("https://x/#f", False), ("x", False), ("", False),
])
def test_only_a_plain_http_url_is_kept(url, ok):
    assert bool(server_peers.normalize_url(url)) is ok


def test_the_token_is_kept_0600_and_never_listed(peers_file):
    server_peers.add("host-b", "https://studio.example.ts.net/", TOKEN, server_id="sid")
    assert oct(os.stat(peers_file).st_mode & 0o777) == "0o600"
    listed = server_peers.list_public()
    assert listed == [{"name": "host-b", "url": "https://studio.example.ts.net", "enabled": True, "server_id": "sid"}]
    assert TOKEN not in json.dumps(listed)


def test_pairing_by_code_spends_it_on_the_peer(peer):
    paired = server_peers.pair(peer.url, "clp_good-code-good-code-good-code", "host-a (peer Host)")
    assert paired == {"token": TOKEN, "server_id": "peer-sid", "name": "host-b"}
    assert ("POST", "/pairing/exchange", {"code": "clp_good-code-good-code-good-code",
                                          "device_name": "host-a (peer Host)"}) in peer.requests
    with pytest.raises(peer_reads.PeerReadError):
        server_peers.pair(peer.url, "clp_bad-code-bad-code-bad-code-bad", "x")


# ---- the reads ------------------------------------------------------------------------------

def test_a_peer_is_read_with_its_token_in_the_header_only(peer):
    server_peers.add("host-b", peer.url, TOKEN)
    result = peer_reads.snapshot()
    assert list(result) == ["peer-sid"]
    entry = result["peer-sid"]
    assert entry["reachable"] is True and entry["error"] is None and entry["name"] == "host-b"
    assert entry["url"] == peer.url and entry["snapshot"]["agents"][0]["session"] == "ada"
    assert TOKEN not in json.dumps(result)
    paths = sorted(path for method, path, _ in peer.requests)
    assert paths == ["/agents/snapshot", "/server-info"]
    for method, path, headers in peer.requests:
        assert method == "GET" and TOKEN not in path
        assert headers["Authorization"] == "Bearer " + TOKEN
        assert "Cookie" not in headers and "X-Clarp-Peer" not in headers
    # The server_id it reported is kept in memory, so it stays keyed the same while it is
    # unreachable; the read path never writes peers.json.
    assert server_peers.enabled()[0]["server_id"] == "peer-sid"
    assert server_peers.list_public()[0]["server_id"] == ""


def test_the_read_path_never_writes_the_peer_file(peer, peers_file):
    server_peers.add("host-b", peer.url, TOKEN)
    before = (peers_file.read_bytes(), peers_file.stat().st_mtime_ns)
    peer_reads.snapshot()
    assert (peers_file.read_bytes(), peers_file.stat().st_mtime_ns) == before


def test_concurrent_adds_and_removes_lose_nothing(peers_file):
    server_peers.add("gone", "https://gone.example.ts.net", TOKEN)
    errors = []

    def add(i):
        try:
            server_peers.add(f"p{i}", f"https://p{i}.example.ts.net", TOKEN)
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=add, args=(i,)) for i in range(24)]
    threads.append(threading.Thread(target=server_peers.remove, args=("gone",)))
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
    names = {p["name"] for p in server_peers.list_public()}
    assert names == {f"p{i}" for i in range(24)}
    assert oct(os.stat(peers_file).st_mode & 0o777) == "0o600"
    assert [p.name for p in peers_file.parent.iterdir() if p.name.endswith(".next")] == []


def test_no_environment_proxy_sees_the_token(peer, monkeypatch):
    for name in ("http_proxy", "HTTP_PROXY", "https_proxy", "HTTPS_PROXY"):
        monkeypatch.setenv(name, "http://127.0.0.1:9")
    monkeypatch.delenv("no_proxy", raising=False)
    monkeypatch.delenv("NO_PROXY", raising=False)
    server_peers.add("host-b", peer.url, TOKEN)
    assert peer_reads.snapshot()["peer-sid"]["reachable"] is True


@pytest.mark.parametrize("url,warns", [
    ("http://192.168.1.20:7682", True), ("http://studio.example.com", True),
    ("https://studio.example.com", False), ("http://127.0.0.1:7682", False), ("http://localhost:7682", False),
    ("http://100.101.102.103:7682", False), ("http://studio.example.ts.net", False),
])
def test_a_plain_http_peer_outside_loopback_and_tailscale_is_warned(url, warns):
    assert bool(server_peers.plain_http_warning(url)) is warns


def test_results_are_cached_and_shared(peer):
    server_peers.add("host-b", peer.url, TOKEN)
    clock = [100.0]
    peer_reads.snapshot(now=lambda: clock[0])
    peer_reads.snapshot(now=lambda: clock[0] + 5)
    assert len(peer.requests) == 2
    peer_reads.snapshot(now=lambda: clock[0] + peer_reads.CACHE_SECONDS + 1)
    assert len(peer.requests) == 4


@pytest.mark.parametrize("mode,error", [("refused", "refused"), ("redirect", "redirect_refused"), ("huge", "too_large")])
def test_a_peer_that_cannot_be_read_says_why_and_nothing_else(peers_file, mode, error):
    p = Peer(mode=mode)
    try:
        server_peers.add("host-b", p.url, TOKEN)
        entry = peer_reads.snapshot()["peer:host-b"]
        assert entry == {"name": "host-b", "url": p.url, "server_id": "", "reachable": False,
                         "error": error, "snapshot": None}
        if mode == "redirect":
            assert all(path == "/server-info" for _, path, _ in p.requests)   # the redirect is not followed
    finally:
        p.close()


def test_an_unreachable_peer_is_listed_as_such(peers_file):
    server_peers.add("host-c", f"http://127.0.0.1:{_closed_port()}", TOKEN, server_id="c-sid")
    entry = peer_reads.snapshot()["c-sid"]
    assert entry["reachable"] is False and entry["error"] == "unreachable" and entry["snapshot"] is None


# ---- the route ------------------------------------------------------------------------------

def _get(host, path, token):
    conn = http.client.HTTPConnection(host.base.split("//", 1)[1], timeout=20)
    try:
        conn.request("GET", path, headers={"Authorization": "Bearer " + token} if token else {})
        response = conn.getresponse()
        return response.status, response.read()
    finally:
        conn.close()


def test_the_route_answers_like_the_snapshot_and_takes_no_url(host, peer):
    server_peers.add("host-b", peer.url, TOKEN)
    status, body = _get(host, "/peers/snapshot?url=http://127.0.0.1:9/&peer=x", "isolated-attention-test")
    assert status == 200
    data = json.loads(body)
    assert list(data["peers"]) == ["peer-sid"] and data["cache_seconds"] == 10
    assert TOKEN.encode() not in body
    assert all(path in ("/server-info", "/agents/snapshot") for _, path, _ in peer.requests)
    # Full scope only: a full device reads it, a limited device gets 403, no token 401.
    full = device_pairing.exchange(device_pairing.issue(device_name="Mac", scope="full")["code"])["token"]
    assert _get(host, "/peers/snapshot", full)[0] == 200
    limited = device_pairing.exchange(device_pairing.issue(device_name="Phone", scope="limited")["code"])["token"]
    assert _get(host, "/peers/snapshot", limited)[0] == 403
    assert _get(host, "/peers/snapshot", "")[0] == 401


def test_with_no_peers_the_answer_is_empty(host, peers_file):
    status, body = _get(host, "/peers/snapshot", "isolated-attention-test")
    assert status == 200 and json.loads(body)["peers"] == {}
