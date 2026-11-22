"""Explicit trusted Clarp peers for cross-server agent delivery."""
from __future__ import annotations

import contextlib
import fcntl
import json
import os
import pathlib
import tempfile
import threading
import urllib.request

from .deployment import LAYOUT

PATH = LAYOUT.config_dir / "peers.json"

# server_ids peers reported to this process's reads (lib.peer_reads), by peer name. Kept in
# memory only: the read path never writes peers.json.
_learned: dict[str, str] = {}
_learned_lock = threading.Lock()


@contextlib.contextmanager
def _locked():
    """An exclusive lock (peers.json.lock) around every read-modify-write of peers.json, across
    processes: the Host, `clarp-admin peers add` and `peers remove`."""
    PATH.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(PATH) + ".lock", os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def _read() -> dict:
    try:
        value = json.loads(PATH.read_text())
        return value if isinstance(value, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _write(value: dict) -> None:
    """Replaces peers.json atomically with a file created 0600 (mkstemp: a unique name, never
    readable by anyone else, whatever the umask). Call it under _locked()."""
    PATH.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".peers.", suffix=".next", dir=str(PATH.parent))
    try:
        with os.fdopen(fd, "w") as handle:
            os.fchmod(handle.fileno(), 0o600)
            handle.write(json.dumps(value, indent=2, sort_keys=True) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, PATH)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(temporary)
        raise


def normalize_url(url: str) -> str:
    """A peer's base URL as kept: http(s), a host, no credentials, query or fragment, no trailing
    slash; '' when it is not one. Only URLs kept here are ever fetched (lib.peer_reads)."""
    from urllib.parse import urlsplit
    text = str(url or "").strip().rstrip("/")
    try:
        parts = urlsplit(text)
        parts.port
    except ValueError:
        return ""
    if parts.scheme not in ("http", "https") or not parts.hostname:
        return ""
    if parts.username or parts.password or parts.query or parts.fragment:
        return ""
    return text


def add(name: str, url: str, token: str, *, server_id: str = "") -> dict:
    name = name.strip()
    url = normalize_url(url)
    if not name or not url or not token.strip():
        raise ValueError("peer requires name, HTTP(S) URL, and token")
    with _locked():
        peers = _read()
        peers[name] = {"url": url, "token": token.strip(), "enabled": True,
                       **({"server_id": server_id.strip()} if server_id.strip() else {})}
        _write(peers)
    return {"name": name, "url": url, "enabled": True, "server_id": server_id.strip()}


def enabled() -> list[dict]:
    """Every enabled peer with its token, for this Host's own server-to-server reads only."""
    out = []
    for name, value in sorted(_read().items()):
        if not isinstance(value, dict) or not value.get("enabled", True):
            continue
        url, token = normalize_url(value.get("url", "")), str(value.get("token") or "").strip()
        if url and token:
            with _learned_lock:
                learned = _learned.get(name, "")
            out.append({"name": name, "url": url, "token": token,
                        "server_id": str(value.get("server_id") or "") or learned})
    return out


def remember_server_id(name: str, server_id: str) -> None:
    """Keeps, in memory, the server_id a peer reported, so it is keyed the same while it is
    unreachable. Never writes peers.json (the read path runs in parallel threads)."""
    if server_id:
        with _learned_lock:
            _learned[name] = server_id


def pair(url: str, code: str, device_name: str) -> dict:
    """Pairs with a peer by a one-time code made on that peer (`clarp-admin pair create`): reads its
    identity, spends the code there, and returns {token, server_id, name}. Nothing is kept here;
    the caller adds the peer. Redirects are refused."""
    from .peer_reads import fetch_json
    base = normalize_url(url)
    if not base:
        raise ValueError("peer requires an HTTP(S) URL")
    identity = fetch_json(base, "/pairing/identity", timeout=10)
    reply = fetch_json(base, "/pairing/exchange", timeout=15,
                       body={"code": code.strip(), "device_name": device_name})
    token = str(((reply or {}).get("device") or {}).get("token") or "")
    if not token:
        raise ValueError("the peer did not return a device token")
    return {"token": token, "server_id": str((identity or {}).get("server_id") or ""),
            "name": str((identity or {}).get("name") or "")}


def remove(name: str) -> None:
    with _locked():
        peers = _read()
        peers.pop(name, None)
        _write(peers)
    with _learned_lock:
        _learned.pop(name, None)


def plain_http_warning(url: str) -> str:
    """A warning for an http:// peer outside loopback and Tailscale (a token sent in the clear),
    else ''."""
    import ipaddress
    from urllib.parse import urlsplit
    parts = urlsplit(normalize_url(url))
    if parts.scheme != "http":
        return ""
    host = (parts.hostname or "").lower()
    if host == "localhost" or host.endswith(".ts.net"):
        return ""
    try:
        address = ipaddress.ip_address(host)
        if address.is_loopback or address in ipaddress.ip_network("100.64.0.0/10") \
                or address in ipaddress.ip_network("fd7a:115c:a1e0::/48"):
            return ""
    except ValueError:
        pass
    return (f"warning: {url} is plain http outside loopback and Tailscale; this peer's token "
            "would cross the network unencrypted. Prefer its https:// address.")


def list_public() -> list[dict]:
    """The peers without their tokens (a token is never printed or sent to a client)."""
    return [{"name": name, "url": value.get("url", ""),
             "enabled": bool(value.get("enabled", True)), "server_id": str(value.get("server_id") or "")}
            for name, value in sorted(_read().items()) if isinstance(value, dict)]


def send(name: str, payload: dict) -> dict:
    peer = _read().get(name)
    if not isinstance(peer, dict) or not peer.get("enabled", True):
        raise ValueError(f"unknown or disabled peer: {name}")
    body = json.dumps(payload).encode()
    request = urllib.request.Request(
        str(peer["url"]) + "/send", data=body, method="POST",
        headers={
            "Content-Type": "application/json",
            "Authorization": "Bearer " + str(peer["token"]),
            "X-Clarp-Peer": name,
        },
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.loads(response.read() or b"{}")
