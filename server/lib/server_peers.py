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


def add(name: str, url: str, token: str) -> dict:
    name = name.strip()
    url = url.strip().rstrip("/")
    if not name or not url.startswith(("http://", "https://")) or not token.strip():
        raise ValueError("peer requires name, HTTP(S) URL, and token")
    peers = _read()
    peers[name] = {"url": url, "token": token.strip(), "enabled": True}
    _write(peers)
    return {"name": name, "url": url, "enabled": True}


def remove(name: str) -> None:
    peers = _read()
    peers.pop(name, None)
    _write(peers)


def list_public() -> list[dict]:
    return [{"name": name, "url": value.get("url", ""),
             "enabled": bool(value.get("enabled", True))}
            for name, value in sorted(_read().items())]


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
