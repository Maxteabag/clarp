"""Route response goldens: status and response shape of every routed request.

Companion to test_route_goldens.py, which pins where each request is routed.
This one sends each routed `METHOD path` to the real server (build_server on
a fake context, as test_server_di.py does) with a minimal request: no query
string, and `{}` as the body of every POST, PUT, PATCH and DELETE. It records
the status, the content type and the shape of the body (JSON keys and value
types, never values) for the local administrator and for a limited device.

Provider CLIs and the network are refused by the test session guard
(tests/conftest.py), so provider-status routes answer as on a host without
them.

Streaming answers (SSE, chunked audio, WebSocket refusals without a length)
are recorded as `stream`; a request that gets no answer within the timeout as
`timeout`; a dropped connection as `dropped` (HTTP-04: an uncaught handler
exception sends nothing).

Regenerate after an intended change with
    CLARP_UPDATE_ROUTE_GOLDENS=1 pytest tests/integration/test_route_response_goldens.py
"""
from __future__ import annotations

import http.client
import json
import os
import pathlib
import re
import socket
import threading

import pytest

from tests.integration.test_server_di import (  # noqa: F401 - fixtures
    _free_port, build_server, fake_ctx)
from tests.integration import route_probe

GOLDEN_DIR = pathlib.Path(__file__).resolve().parents[1] / "fixtures" / "route_goldens"
DISPATCH_GOLDEN = GOLDEN_DIR / "dispatch.json"
RESPONSE_GOLDEN = GOLDEN_DIR / "responses.json"
UPDATE = os.environ.get("CLARP_UPDATE_ROUTE_GOLDENS") == "1"
TIMEOUT = 8.0  # above hls._PLAYLIST_WAIT_SEC (3 s), the longest built-in wait

# Bodies that depend on background workers racing the request; only their
# status and content type are pinned.
VOLATILE_BODY = {
    "GET /viz/events": "the fleet-map learner fills learning/world in the background",
}


_FIELD = re.compile(r"^[A-Za-z_][A-Za-z0-9_-]{0,40}$")


def shape(value, depth: int = 0):
    if isinstance(value, dict):
        if depth >= 4:
            return "{...}"
        # A dict keyed by data (ids, paths, "kind:name") is a map; its keys
        # depend on test state, so only its kind is recorded.
        if any(not _FIELD.match(key) for key in value):
            return "{map}"
        return {key: shape(value[key], depth + 1) for key in sorted(value)}
    if isinstance(value, list):
        return [shape(value[0], depth + 1)] if value else []
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, (int, float)):
        return "num"
    if value is None:
        return "null"
    return "str"


def _exercise(port: int, method: str, path: str, headers: dict) -> dict:
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=TIMEOUT)
    body = b"{}" if method != "GET" else None
    send_headers = dict(headers)
    if body is not None:
        send_headers["Content-Type"] = "application/json"
    try:
        connection.request(method, path, body=body, headers=send_headers)
        response = connection.getresponse()
    except (socket.timeout, TimeoutError):
        connection.close()
        return {"status": "timeout"}
    except (ConnectionError, http.client.RemoteDisconnected, http.client.BadStatusLine):
        connection.close()
        return {"status": "dropped"}
    result = {"status": response.status,
              "type": (response.getheader("Content-Type") or "").split(";")[0]}
    if response.getheader("Content-Length") is None:
        result["body"] = "stream"
        connection.close()
        return result
    try:
        data = response.read()
    except (socket.timeout, TimeoutError, ConnectionError):
        result["body"] = "truncated"
        connection.close()
        return result
    connection.close()
    if result["type"] == "application/json":
        try:
            result["body"] = shape(json.loads(data))
        except ValueError:
            result["body"] = "invalid json"
    else:
        result["body"] = "empty" if not data else "bytes"
    return result


def _routed_requests() -> list[tuple[str, str, bool]]:
    """(method, path, limited device reaches the same target) per routed request."""
    golden = json.loads(DISPATCH_GOLDEN.read_text())["routes"]
    requests = []
    for key, entry in sorted(golden.items()):
        if entry["target"] == "send 404":
            continue
        method, path = key.split(" ", 1)
        requests.append((method, path, "limited" not in entry["others"]))
    return requests


def _dump(golden: dict) -> str:
    lines = ["{", f' "_about": {json.dumps(golden["_about"])},', ' "routes": {']
    items = sorted(golden["routes"].items())
    for index, (key, entry) in enumerate(items):
        comma = "," if index < len(items) - 1 else ""
        lines.append(f"  {json.dumps(key)}: {json.dumps(entry, sort_keys=True)}{comma}")
    lines += [" }", "}"]
    return "\n".join(lines) + "\n"


def test_response_golden(fake_ctx, backend_cli_guard):
    from lib import device_pairing

    ctx = fake_ctx.with_(auth_token=route_probe.ADMIN_TOKEN)
    issued = device_pairing.issue(device_name="route golden limited", scope="limited")
    limited_token = device_pairing.exchange(issued["code"])["token"]
    principals = {
        "admin": {"Authorization": f"Bearer {route_probe.ADMIN_TOKEN}"},
        "limited": {"Authorization": f"Bearer {limited_token}"},
    }
    port = _free_port()
    srv = build_server(ctx, port, bind_addr="127.0.0.1")
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    routes = {}
    try:
        for method, path, limited_reaches in _routed_requests():
            entry = {"admin": _exercise(port, method, path, principals["admin"])}
            if limited_reaches:
                limited = _exercise(port, method, path, principals["limited"])
                if limited != entry["admin"]:
                    entry["limited"] = limited
            if f"{method} {path}" in VOLATILE_BODY:
                entry = {name: {**answer, "body": "volatile"} for name, answer in entry.items()}
                entry.pop("limited", None)
            routes[f"{method} {path}"] = entry
    finally:
        srv.shutdown()
        srv.server_close()
    # Provider status routes (/backend-auth, /backend-usage, /harness-providers)
    # and background workers try provider CLIs; the session guard refused them,
    # so the golden records the "no CLI" answers. Acknowledge the refusals.
    backend_cli_guard.take()
    actual = {
        "_about": (
            "Generated by tests/integration/test_route_response_goldens.py. Per routed "
            "'METHOD path' (minimal request: no query, body {} unless GET): admin = the "
            "local administrator's status, content type and body shape; limited = a "
            "limited device's answer where it differs (absent: same, or refused at the "
            "gate as dispatch.json records)."
        ),
        "routes": routes,
    }
    if UPDATE or not RESPONSE_GOLDEN.exists():
        RESPONSE_GOLDEN.write_text(_dump(actual))
        if not UPDATE:
            pytest.fail("response golden was missing and has been written; review and commit it")
    expected = json.loads(RESPONSE_GOLDEN.read_text())
    changed = sorted(
        key for key in set(expected["routes"]) | set(routes)
        if expected["routes"].get(key) != routes.get(key))
    assert not changed, (
        f"{len(changed)} route responses differ from the golden, first: "
        + json.dumps({k: {"golden": expected["routes"].get(k), "now": routes.get(k)}
                      for k in changed[:5]}, indent=1))
