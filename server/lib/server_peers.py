"""Explicit trusted Clarp peers for cross-server agent delivery."""
from __future__ import annotations

import json
import pathlib
import urllib.request

from .deployment import LAYOUT

PATH = LAYOUT.config_dir / "peers.json"


def _read() -> dict:
    try:
        value = json.loads(PATH.read_text())
        return value if isinstance(value, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _write(value: dict) -> None:
    PATH.parent.mkdir(parents=True, exist_ok=True)
    temporary = PATH.with_suffix(".next")
    temporary.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")
    temporary.chmod(0o600)
    temporary.replace(PATH)


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
            out.append({"name": name, "url": url, "token": token, "server_id": str(value.get("server_id") or "")})
    return out


def remember_server_id(name: str, server_id: str) -> None:
    """Keeps the server_id a peer reported, so it is keyed the same while it is unreachable."""
    peers = _read()
    if isinstance(peers.get(name), dict) and server_id and peers[name].get("server_id") != server_id:
        peers[name]["server_id"] = server_id
        _write(peers)


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
    peers = _read()
    peers.pop(name, None)
    _write(peers)


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
