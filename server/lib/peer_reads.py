"""This Host reading its peers' chat lists, server to server (issue 651, feature peer_snapshot).

The web app is served by one Host. For its all-Hosts list it asks that Host, GET /peers/snapshot,
and the Host reads each of its peers (lib.server_peers, `clarp-admin peers add`) with the
device token it keeps for that peer. The browser never holds another Host's token.

What is fetched, and how:
- Only the URLs kept in this Host's peer list, and only two paths on each: GET /server-info and
  GET /agents/snapshot. Nothing in the request to this Host chooses a URL, a path or a peer.
- The peer's device token goes in the Authorization header, to that peer only. It is never
  logged, never returned, never put in a URL.
- Redirects are refused (a peer cannot send this Host's request, or the token, elsewhere), no
  environment proxy is used, a
  read takes at most READ_TIMEOUT seconds and at most MAX_BYTES of body, and only JSON objects
  are taken.
- Results are cached for CACHE_SECONDS; one refresh runs at a time and every caller shares it.

What is returned, per peer, keyed by its server_id: its name, the URL kept for it (the web app
opens a peer's chat in that Host's own web app), whether it was reached, a short error code, and
the peer's /agents/snapshot as the peer gave it to this Host's device token.
"""
from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.request

READ_TIMEOUT = 4.0
CACHE_SECONDS = 10.0
MAX_BYTES = 4 * 1024 * 1024
PATHS = ("/server-info", "/agents/snapshot")


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D401 - urllib hook
        raise urllib.error.HTTPError(req.full_url, code, "redirect refused", headers, fp)


# Every request this Host makes to a peer with its token goes through this opener (the reads
# here and lib.server_peers.send): no environment proxy (an HTTP(S)_PROXY must not see the
# token) and no redirects (a 3xx must not carry the Authorization header elsewhere).
OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect)


class PeerReadError(Exception):
    """A read of a peer that failed; `code` is what the snapshot says (never the token or a body)."""
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def fetch_json(base: str, path: str, *, token: str = "", timeout: float = READ_TIMEOUT, body=None) -> dict:
    """GET (or, with `body`, POST JSON to) `base + path`: a JSON object, or PeerReadError."""
    headers = {"Accept": "application/json"}
    if token:
        headers["Authorization"] = "Bearer " + token
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(base + path, data=data, headers=headers, method="POST" if data else "GET")
    try:
        with OPENER.open(request, timeout=timeout) as response:
            raw = response.read(MAX_BYTES + 1)
    except urllib.error.HTTPError as exc:
        if 300 <= exc.code < 400:
            raise PeerReadError("redirect_refused") from None
        raise PeerReadError("refused" if exc.code in (401, 403) else f"http_{exc.code}") from None
    except (urllib.error.URLError, OSError, ValueError):
        raise PeerReadError("unreachable") from None
    if len(raw) > MAX_BYTES:
        raise PeerReadError("too_large")
    try:
        value = json.loads(raw)
    except ValueError:
        raise PeerReadError("not_json") from None
    if not isinstance(value, dict):
        raise PeerReadError("not_json")
    return value


def _read_peer(peer: dict) -> tuple[str, dict]:
    from . import server_peers
    key = peer["server_id"] or "peer:" + peer["name"]
    entry = {"name": peer["name"], "url": peer["url"], "server_id": peer["server_id"],
             "reachable": False, "error": None, "snapshot": None}
    try:
        info = fetch_json(peer["url"], "/server-info", token=peer["token"])
        snapshot = fetch_json(peer["url"], "/agents/snapshot", token=peer["token"])
    except PeerReadError as exc:
        entry["error"] = exc.code
        return key, entry
    server_id = str(info.get("server_id") or "")
    if server_id and server_id != peer["server_id"]:
        server_peers.remember_server_id(peer["name"], server_id)
    key = server_id or key
    entry.update(name=str(info.get("name") or peer["name"]), server_id=server_id or peer["server_id"],
                 reachable=True, snapshot=snapshot)
    return key, entry


_lock = threading.Lock()
_cache: dict = {"at": 0.0, "peers": None}


def snapshot(now=time.monotonic) -> dict:
    """{server_id: entry} for every enabled peer, at most CACHE_SECONDS old."""
    from . import server_peers
    with _lock:
        if _cache["peers"] is not None and now() - _cache["at"] < CACHE_SECONDS:
            return _cache["peers"]
        peers = server_peers.enabled()
        results: dict = {}
        threads = []

        def run(peer):
            key, entry = _read_peer(peer)
            results[key] = entry

        for peer in peers:
            thread = threading.Thread(target=run, args=(peer,), daemon=True, name="peer-read")
            thread.start()
            threads.append(thread)
        for thread in threads:
            thread.join(READ_TIMEOUT * len(PATHS) + 1)
        _cache.update(at=now(), peers=dict(results))
        return _cache["peers"]


def reset_cache() -> None:
    with _lock:
        _cache.update(at=0.0, peers=None)
