"""Cross-origin reads for the web app's all-Hosts list (issue 651, lib.cross_origin): exactly
which origin may read what, through the real handler."""
from __future__ import annotations

import http.client
import json

import pytest

from lib import cross_origin, device_pairing
from tests.integration.test_attention_questions_http import host, server_module  # noqa: F401

ORIGIN = "https://studio.example.ts.net"


# ---- the policy ------------------------------------------------------------------------------

@pytest.mark.parametrize("value,expected", [
    ("https://studio.example.ts.net", "https://studio.example.ts.net"),
    ("HTTPS://Studio.Example.ts.net/", "https://studio.example.ts.net"),
    ("https://studio.example.ts.net:443", "https://studio.example.ts.net"),
    ("http://100.64.0.1:7682", "http://100.64.0.1:7682"),
    ("http://[::1]:7682", "http://[::1]:7682"),
    ("https://studio.example.ts.net/app", ""), ("https://*.ts.net", ""), ("null", ""),
    ("ftp://studio", ""), ("https://user:pw@studio", ""), ("https://studio/?q=1", ""),
    ("studio.example.ts.net", ""), ("", ""), (None, ""), (7, ""),
])
def test_an_origin_is_exact(value, expected):
    assert cross_origin.normalize_origin(value) == expected


def test_the_list_keeps_valid_origins_and_says_what_it_refused(monkeypatch):
    lines = []
    monkeypatch.setattr("lib.log.log", lambda event, detail="", *a, **k: lines.append((event, detail)))
    assert cross_origin.configured_origins([ORIGIN, "https://*.ts.net", "null"]) == {ORIGIN}
    assert len(lines) == 1 and "'https://*.ts.net'" in lines[0][1] and "'null'" in lines[0][1]
    assert cross_origin.configured_origins(None) == frozenset()
    assert cross_origin.configured_origins("https://one") == frozenset()


def test_only_the_two_read_routes_and_only_get():
    allowed = frozenset({ORIGIN})
    assert cross_origin.allowed_origin(ORIGIN, "/server-info", "GET", allowed) == ORIGIN
    assert cross_origin.allowed_origin(ORIGIN, "/agents/snapshot", "GET", allowed) == ORIGIN
    for path in ("/log", "/events", "/artifacts", "/agents", "/send"):
        assert cross_origin.allowed_origin(ORIGIN, path, "GET", allowed) == ""
    assert cross_origin.allowed_origin(ORIGIN, "/server-info", "POST", allowed) == ""
    assert cross_origin.allowed_origin("https://other.example", "/server-info", "GET", allowed) == ""
    assert cross_origin.allowed_origin(ORIGIN + "/", "/server-info", "GET", allowed) == ""  # not a browser's Origin
    assert cross_origin.allowed_origin(ORIGIN, "/server-info", "GET", frozenset()) == ""


# ---- through the handler ---------------------------------------------------------------------

@pytest.fixture
def listed(monkeypatch):
    monkeypatch.setattr(server_module.Handler, "_web_read_origins", lambda self: frozenset({ORIGIN}))


@pytest.fixture
def device():
    code = device_pairing.issue(device_name="Web app", scope="full")["code"]
    return device_pairing.exchange(code)["token"]


def _call(host, method, path, headers=None, conn=None):
    own = conn is None
    conn = conn or http.client.HTTPConnection(host.base.split("//", 1)[1], timeout=10)
    conn.request(method, path, headers=headers or {})
    response = conn.getresponse()
    body = response.read()
    headers = {k.lower(): v for k, v in response.getheaders()}
    if own:
        conn.close()
    return response.status, headers, body


CORS_KEYS = ("access-control-allow-origin", "access-control-allow-methods", "access-control-allow-headers",
             "access-control-allow-credentials", "access-control-expose-headers")


def _no_cors(headers):
    return not any(k in headers for k in CORS_KEYS)


def test_with_no_list_nothing_is_allowed_cross_origin(host, device):
    status, headers, _ = _call(host, "GET", "/server-info", {"Origin": ORIGIN, "Authorization": "Bearer " + device})
    assert status == 200 and _no_cors(headers)
    status, headers, _ = _call(host, "OPTIONS", "/server-info", {
        "Origin": ORIGIN, "Access-Control-Request-Method": "GET", "Access-Control-Request-Headers": "authorization"})
    assert status == 403 and _no_cors(headers)


def test_the_preflight_allows_exactly_get_with_authorization_and_the_client_header(host, listed):
    ask = {"Origin": ORIGIN, "Access-Control-Request-Method": "GET",
           "Access-Control-Request-Headers": "authorization, x-clarp-client"}
    status, headers, _ = _call(host, "OPTIONS", "/agents/snapshot", ask)
    assert status == 204
    assert headers["access-control-allow-origin"] == ORIGIN
    assert headers["access-control-allow-methods"] == "GET"
    assert headers["access-control-allow-headers"] == "Authorization, X-Clarp-Client"
    assert headers["access-control-max-age"] == "600"
    assert "origin" in headers["vary"].lower()
    assert "access-control-allow-credentials" not in headers
    refused = [
        {**ask, "Access-Control-Request-Headers": "authorization, content-type"},
        {**ask, "Access-Control-Request-Method": "POST"},
        {**ask, "Origin": "https://other.example"},
        {**ask, "Origin": "null"},
    ]
    for headers_ in refused:
        status, headers, _ = _call(host, "OPTIONS", "/agents/snapshot", headers_)
        assert status == 403 and _no_cors(headers), headers_
    status, headers, _ = _call(host, "OPTIONS", "/log", ask)
    assert status == 403 and _no_cors(headers)


def test_a_listed_origin_reads_the_two_routes_with_a_device_token_in_the_header(host, listed, device):
    for path in ("/server-info", "/agents/snapshot"):
        status, headers, body = _call(host, "GET", path, {"Origin": ORIGIN, "Authorization": "Bearer " + device,
                                                           "X-Clarp-Client": "web/1 contract=2"})
        assert status == 200, body
        assert headers["access-control-allow-origin"] == ORIGIN and "origin" in headers["vary"].lower()
        assert "access-control-allow-credentials" not in headers and "access-control-expose-headers" not in headers
        json.loads(body)
    # Every other route stays same-origin: answered, but never with a CORS header.
    status, headers, _ = _call(host, "GET", "/artifacts", {"Origin": ORIGIN, "Authorization": "Bearer " + device})
    assert _no_cors(headers)


def test_a_refusal_reaches_the_page_and_only_the_header_token_is_taken(host, listed, device):
    status, headers, _ = _call(host, "GET", "/server-info", {"Origin": ORIGIN})
    assert status == 401 and headers["access-control-allow-origin"] == ORIGIN
    # The administrator token is never taken cross-origin (the request is not local).
    status, headers, _ = _call(host, "GET", "/server-info", {"Origin": ORIGIN, "Authorization": "Bearer isolated-attention-test"})
    assert status == 401
    # A cookie is not a cross-origin credential.
    status, headers, _ = _call(host, "GET", "/server-info", {"Origin": ORIGIN, "Cookie": "claude_pwa_token=" + device})
    assert status == 401
    # An unlisted origin gets no CORS header, whatever the answer.
    status, headers, _ = _call(host, "GET", "/server-info", {"Origin": "https://other.example", "Authorization": "Bearer " + device})
    assert status == 200 and _no_cors(headers)


def test_keep_alive_never_carries_the_headers_to_the_next_request(host, listed, device):
    conn = http.client.HTTPConnection(host.base.split("//", 1)[1], timeout=10)
    try:
        _, first, _ = _call(host, "GET", "/server-info", {"Origin": ORIGIN, "Authorization": "Bearer " + device}, conn)
        _, second, _ = _call(host, "GET", "/server-info", {"Authorization": "Bearer " + device}, conn)
    finally:
        conn.close()
    assert first["access-control-allow-origin"] == ORIGIN
    assert _no_cors(second)
