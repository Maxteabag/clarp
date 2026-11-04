"""Characterization of the artifact, decision, upload and media handlers'
branches the full suite did not reach, as server.py serves them today: G0 for
the S3 artifacts/media recipe entry (route-registry.md section 8). Library
calls are stubbed at their module only to reach the handler's own branches;
malformed and short bodies go over a raw socket, which urllib cannot send.
"""
from __future__ import annotations

import json
import socket
import urllib.error
import urllib.parse
import urllib.request

import pytest

from tests.integration.test_server_di import (  # noqa: F401 - fixtures
    _model_catalog_never_probes_installed_clis, fake_ctx, running_server)


def _call(base: str, method: str, path: str, body=None, raw: bytes | None = None, headers=None):
    data = raw if raw is not None else (None if body is None else json.dumps(body).encode())
    request = urllib.request.Request(base + path, data=data, method=method,
                                     headers={"Content-Type": "application/json", **(headers or {})})
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            return response.status, json.loads(response.read() or b"null")
    except urllib.error.HTTPError as error:
        return error.code, json.loads(error.read() or b"null")


def _raw_post(base: str, path: str, length: str, body: bytes) -> tuple[int, dict]:
    """POST with a Content-Length that need not match the body; the write side
    is closed after the body, so a short body reads as end of stream."""
    port = urllib.parse.urlsplit(base).port
    with socket.create_connection(("127.0.0.1", port), timeout=5) as sock:
        sock.sendall(f"POST {path} HTTP/1.1\r\nHost: 127.0.0.1\r\nContent-Type: image/png\r\n"
                     f"Content-Length: {length}\r\n\r\n".encode() + body)
        sock.shutdown(socket.SHUT_WR)
        reply = b""
        while chunk := sock.recv(65536):
            reply += chunk
    head, _, payload = reply.partition(b"\r\n\r\n")
    return int(head.split()[1]), json.loads(payload)


def test_decision_withdraw_refusals(running_server, monkeypatch):
    from lib import artifacts
    base, _ctx, _srv = running_server
    assert _call(base, "POST", "/decisions/d1/withdraw", raw=b"[]") == (
        400, {"error": "json object required"})

    def settled(decision_id, session):
        raise ValueError("decision already resolved")
    monkeypatch.setattr(artifacts, "withdraw", settled)
    assert _call(base, "POST", "/decisions/d1/withdraw", {"session": "claude"}) == (
        409, {"error": "decision already resolved"})


@pytest.mark.parametrize("path", ["/upload", "/media"])
def test_unreadable_length_and_short_body(running_server, path):
    base, _ctx, _srv = running_server
    status, body = _raw_post(base, path, "x", b"")
    assert status == 400 and set(body) == {"error"}
    short = {"/upload": (408, {"error": "incomplete upload"}),
             "/media": (400, {"error": "incomplete body"})}[path]
    assert _raw_post(base, path, "10", b"abc") == short


def test_media_publish_refuses_an_empty_body(running_server):
    base, _ctx, _srv = running_server
    assert _call(base, "POST", "/media", raw=b"", headers={"Content-Type": "image/png"}) == (
        400, {"error": "bad size"})


@pytest.mark.parametrize("error, expected", [
    (ValueError("bad upload id"), (400, {"error": "bad upload id"})),
    (OSError("disk full"), (500, {"error": "disk full"})),
])
def test_upload_store_failures(running_server, monkeypatch, error, expected):
    from lib import upload_results

    def store(**_kwargs):
        raise error
    monkeypatch.setattr(upload_results, "store", store)
    base, _ctx, _srv = running_server
    assert _call(base, "POST", "/upload", raw=b"\x89PNG",
                 headers={"Content-Type": "image/png", "X-Upload-ID": "u1"}) == expected


def test_media_publish_write_failure(running_server, monkeypatch):
    from lib import media_store

    def publish(**_kwargs):
        raise OSError("disk full")
    monkeypatch.setattr(media_store, "publish", publish)
    base, _ctx, _srv = running_server
    assert _call(base, "POST", "/media", raw=b"\x89PNG",
                 headers={"Content-Type": "image/png"}) == (500, {"error": "disk full"})


def test_media_list_bad_limit_falls_back_to_100(running_server, monkeypatch):
    from lib import media_store
    seen = []
    monkeypatch.setattr(media_store, "list_for_session",
                        lambda session, limit: seen.append((session, limit)) or [])
    base, _ctx, _srv = running_server
    assert _call(base, "GET", "/media?session=claude&limit=x") == (
        200, {"session": "claude", "assets": []})
    assert seen == [("claude", 100)]
