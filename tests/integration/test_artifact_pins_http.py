"""Pinned artifacts over the real HTTP handler against a temporary DB."""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace
import threading
import urllib.error
import urllib.request

import pytest

from lib import agents, artifacts, db


_ROOT = Path(__file__).resolve().parents[2]
_spec = importlib.util.spec_from_file_location("artifact_pins_http_server", _ROOT / "server/server.py")
server_module = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(server_module)

TOKEN = "isolated-artifact-pins-test"


@pytest.fixture
def host(tmp_path):
    agents.create_agent(persona="Theo", voice_id="V", cwd=str(tmp_path), session="theo")
    events: list = []
    ctx = SimpleNamespace(auth_token=TOKEN, stream=SimpleNamespace(broadcast=events.append))
    srv = server_module.ContextHTTPServer(("127.0.0.1", 0), server_module.Handler, ctx)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    try:
        yield SimpleNamespace(base=f"http://127.0.0.1:{srv.server_port}", events=events)
    finally:
        srv.shutdown()
        srv.server_close()
        thread.join(timeout=2)


def _request(host, path, body=None):
    request = urllib.request.Request(
        host.base + path,
        headers={"Content-Type": "application/json", "Authorization": "Bearer " + TOKEN},
        data=json.dumps(body).encode() if body is not None else None,
        method="POST" if body is not None else "GET")
    try:
        with urllib.request.urlopen(request, timeout=3) as response:
            return response.status, json.load(response)
    except urllib.error.HTTPError as exc:
        return exc.code, json.load(exc)


def _document(title: str) -> dict:
    return artifacts.create(session="theo", type="document", title=title,
                            payload={"content": "# " + title})


def _pinned_ids(host) -> list[str]:
    status, result = _request(host, "/pinned-artifacts")
    assert status == 200, result
    return [row["artifact_id"] for row in result["artifacts"]]


def test_pin_list_unpin_round_trip_newest_pin_first(host, monkeypatch):
    first, second = _document("Plan"), _document("Notes")
    clock = [5_000]
    monkeypatch.setattr(db, "now_ms", lambda: clock[0])
    assert _pinned_ids(host) == []

    status, result = _request(host, f"/artifacts/{first['artifact_id']}/pin", {"pinned": True})
    assert status == 200 and result["changed"] is True
    assert result["artifact"]["pinned"] is True and result["artifact"]["pinned_at"] == 5_000
    # A pin does not move the artifact in chat or the library.
    assert result["artifact"]["updated_at"] == first["updated_at"]
    assert host.events[0]["type"] == "artifact-updated"
    clock[0] = 6_000
    _request(host, f"/artifacts/{second['artifact_id']}/pin", {"pinned": True})
    assert _pinned_ids(host) == [second["artifact_id"], first["artifact_id"]]

    # Every device reads the same flag from the artifact payloads it already uses.
    assert _request(host, f"/artifacts/{first['artifact_id']}")[1]["artifact"]["pinned"] is True
    listed = _request(host, "/artifacts?session=theo&representation=flat-v1")[1]["artifacts"]
    assert {row["artifact_id"]: row["pinned"] for row in listed} == {
        first["artifact_id"]: True, second["artifact_id"]: True}

    events_before = len(host.events)
    status, result = _request(host, f"/artifacts/{first['artifact_id']}/pin", {"pinned": True})
    assert status == 200 and result["changed"] is False and len(host.events) == events_before

    status, result = _request(host, f"/artifacts/{first['artifact_id']}/pin", {"pinned": False})
    assert status == 200 and result["changed"] is True and result["artifact"]["pinned_at"] is None
    assert _pinned_ids(host) == [second["artifact_id"]]


def test_pin_rejects_bad_requests(host):
    artifact = _document("Plan")
    assert _request(host, f"/artifacts/{artifact['artifact_id']}/pin", {"pinned": "yes"})[0] == 400
    assert _request(host, "/artifacts/artifact-missing/pin", {"pinned": True})[0] == 404
    assert _request(host, "/pinned-artifacts?representation=v9")[0] == 400


def test_archiving_or_discarding_removes_the_pin(host):
    archived, discarded = _document("Archive me"), _document("Discard me")
    for row in (archived, discarded):
        _request(host, f"/artifacts/{row['artifact_id']}/pin", {"pinned": True})

    status, result = _request(host, f"/artifacts/{archived['artifact_id']}/archive",
                              {"archived": True, "expected_updated_at": archived["updated_at"]})
    assert status == 200 and result["artifact"]["pinned"] is False
    status, _ = _request(host, f"/artifacts/{discarded['artifact_id']}/discard",
                         {"expected_updated_at": discarded["updated_at"]})
    assert status == 200
    assert _pinned_ids(host) == []

    status, result = _request(host, f"/artifacts/{archived['artifact_id']}/pin", {"pinned": True})
    assert status == 409 and "archived" in result["error"]
