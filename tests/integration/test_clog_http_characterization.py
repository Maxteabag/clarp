"""Characterization of POST /clog (client diagnostics upload) as server.py
serves it today, before the S3 move of the settings and ops handlers.

Pins the response, the legacy line log and the structured eventlog rows,
including the pre-existing HTTP-04 behaviour: a non-object event raises in
the handler and the client gets no response at all.
"""
from __future__ import annotations

import http.client
import json
import urllib.parse

import pytest

from tests.integration.test_server_di import (  # noqa: F401 - fixtures
    _model_catalog_never_probes_installed_clis, fake_ctx, running_server)


def _post(base: str, raw: bytes):
    url = urllib.parse.urlsplit(base)
    connection = http.client.HTTPConnection(url.hostname, url.port, timeout=5)
    try:
        connection.request("POST", "/clog", body=raw,
                           headers={"Content-Type": "application/json"})
        response = connection.getresponse()
        return response.status, json.loads(response.read() or b"null")
    except (ConnectionError, http.client.RemoteDisconnected):
        return "dropped", None
    finally:
        connection.close()


@pytest.fixture
def capture(running_server, monkeypatch, tmp_path):
    from lib import diagnostics_settings, eventlog
    diagnostics_settings.update({"enabled": True, "categories": ["client"]})
    log_path = tmp_path / "client-log" / "hook.log"
    monkeypatch.setenv("CLAUDE_PWA_CLIENT_LOG", str(log_path))
    rows = []
    real_emit = eventlog.emit

    def emit(source, event, **fields):
        if source == "client":
            rows.append((event, fields))
            return None
        return real_emit(source, event, **fields)
    monkeypatch.setattr(eventlog, "emit", emit)
    base, _ctx, _srv = running_server
    return base, log_path, rows


def _lines(log_path):
    # Drop the HH:MM:SS prefix; the rest of each line is pinned.
    return [line[9:] for line in log_path.read_text().splitlines()]


def test_bad_json_and_disabled_capture(running_server):
    from lib import diagnostics_settings
    base, _ctx, _srv = running_server
    assert _post(base, b"[1]") == (400, {"error": "bad json"})
    diagnostics_settings.update({"enabled": False, "categories": []})
    assert _post(base, b'{"event": "x"}') == (200, {"ok": True, "captured": False})


def test_single_event_is_logged_and_emitted(capture):
    base, log_path, rows = capture
    assert _post(base, json.dumps({
        "event": "ios.tap", "detail": "pressed", "session": "claude", "clip_id": "42",
        "duration_ms": 7, "trace_id": " t1 ", "client_id": "c1"}).encode()) == (
        200, {"ok": True})
    assert _lines(log_path) == ["client   ios.tap pressed"]
    assert rows == [("ios.tap", {
        "session": "claude", "agent_id": None, "backend_session_id": None, "persona": None,
        "clip_id": 42, "clip_url": None, "client_id": "c1", "duration_ms": 7,
        "trace_id": " t1 ", "detail": {"msg": "pressed"}})]


def test_batch_events_convert_details_and_clip_ids(capture):
    base, log_path, rows = capture
    assert _post(base, json.dumps({"events": [
        {"event": "a", "detail": {"k": 1}, "clip_id": "x"},
        {"detail": ""},
        {"event": "c" * 80, "detail": 5},
    ]}).encode()) == (200, {"ok": True})
    assert _lines(log_path) == ['client   a {"k":1}', "client   ? ", "client   " + "c" * 80 + " 5"]
    assert [(event, fields["detail"], fields["clip_id"]) for event, fields in rows] == [
        ("a", {"k": 1}, None), ("log", None, None), ("c" * 64, None, None)]


def test_non_object_event_drops_the_connection_http_04(capture):
    """Pre-existing HTTP-04: `it.get` on a string raises outside the handler's
    OSError guard, so no response is sent. The fix lane changes this."""
    base, _log_path, rows = capture
    assert _post(base, json.dumps({"events": ["x"]}).encode()) == ("dropped", None)
    assert rows == []
