"""Characterization of /select, the orchestrator branch of /send and /log as
server.py serves them today, before the S3 moves of the send/select and
/log handlers (the groups below 85% line coverage).

OrchestratorService, load_conversation and the pair-session timeline are
stubbed at their module boundary only to reach the handler's own branches.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request
from types import SimpleNamespace

import pytest

from tests.integration.test_server_di import (  # noqa: F401 - fixtures
    _model_catalog_never_probes_installed_clis, _running_context_server, fake_ctx,
    running_server, server_module)


def _call(base, method, path, body=None, raw=None):
    data = raw if raw is not None else (None if body is None else json.dumps(body).encode())
    request = urllib.request.Request(base + path, data=data, method=method,
                                     headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            status, ctype, payload = (response.status, response.headers.get("Content-Type"),
                                      response.read())
    except urllib.error.HTTPError as error:
        status, ctype, payload = error.code, error.headers.get("Content-Type"), error.read()
    return status, json.loads(payload) if ctype == "application/json" else payload


# ---- /select -------------------------------------------------------------

class _Herald:
    def __init__(self, fail=False):
        self.calls, self.fail = [], fail

    def set_focus(self, session):
        self.calls.append(session)
        if self.fail:
            raise RuntimeError("herald down")


@pytest.mark.parametrize("herald_fails", [False, True])
def test_select_with_a_herald(fake_ctx, herald_fails):
    herald = _Herald(fail=herald_fails)
    for base, _ctx, _srv in _running_context_server(fake_ctx.with_(herald=herald)):
        assert _call(base, "POST", "/select", {"session": "claude"}) == (
            200, {"ok": True, "session": "claude"})
    assert herald.calls == ["claude"]


def test_select_validation_and_failed_writes(running_server, monkeypatch, tmp_path):
    from lib import agents
    base, _ctx, _srv = running_server
    assert _call(base, "POST", "/select", raw=b"[]") == (400, {"error": "bad json"})
    assert _call(base, "POST", "/select", {"session": "bad name!"}) == (
        400, {"error": "bad session name"})
    assert _call(base, "POST", "/select", {"session": "nobody"}) == (
        404, {"error": "no such session"})

    def broken_focus(agent_id):
        raise RuntimeError("db locked")
    monkeypatch.setattr(agents, "set_focus", broken_focus)
    assert _call(base, "POST", "/select", {"session": "claude"}) == (
        200, {"ok": True, "session": "claude"})

    state_path = server_module._runtime_paths().app_session
    if state_path.exists():
        state_path.unlink()
    state_path.mkdir(parents=True)          # write_text on a directory raises OSError
    try:
        assert _call(base, "POST", "/select", {"session": "claude"}) == (
            500, {"error": "could not persist selection"})
    finally:
        state_path.rmdir()


# ---- /send through the orchestrator ---------------------------------------
# Only a hands-free send goes through the orchestrator (force_session otherwise).

def _orchestrator(monkeypatch, result):
    seen = []

    class FakeOrchestrator:
        def __init__(self, _ctx):
            pass

        def handle_send(self, **kwargs):
            seen.append(kwargs)
            return result
    monkeypatch.setattr(server_module, "OrchestratorService", FakeOrchestrator)
    return seen


@pytest.fixture
def deleted_transcriptions(monkeypatch):
    from lib import transcription_results
    deleted = []
    monkeypatch.setattr(transcription_results, "delete", deleted.append)
    return deleted


def test_send_orchestrator_disabled_with_fallback_request(
        running_server, monkeypatch, deleted_transcriptions):
    base, _ctx, _srv = running_server
    seen = _orchestrator(monkeypatch, None)
    status, body = _call(base, "POST", "/send", {
        "text": "hello", "session": "claude", "hands_free": True, "orchestrator_fallback": True,
        "transcription_id": "job-1", "trace_id": "t-1"})
    assert (status, body) == (200, {
        "ok": True, "session": "", "dispatch": "", "trace_id": "t-1",
        "orchestrator": {"action": "disabled", "decision_id": None, "decision": None}})
    assert seen[0]["fallback_request"] is True
    assert deleted_transcriptions == ["job-1"]


def test_send_orchestrator_error_transcription_and_voice(
        running_server, monkeypatch, deleted_transcriptions):
    base, _ctx, _srv = running_server
    _orchestrator(monkeypatch, SimpleNamespace(
        action="route", ok=True, session="rachel", dispatch="queued", trace_id="t-2",
        decision_id=7, decision={"kind": "named"}, error="partial", status=202))
    voices = []
    monkeypatch.setattr(server_module.Handler, "_record_voice",
                        lambda self, event, **fields: voices.append((event, fields)))
    status, body = _call(base, "POST", "/send", {
        "text": "tell rachel", "session": "claude", "hands_free": True, "transcription_id": "job-2",
        "utterance_id": "u-2", "trace_id": "t-2", "client_msg_id": "m-2"})
    assert (status, body) == (202, {
        "ok": True, "session": "rachel", "dispatch": "queued", "trace_id": "t-2",
        "orchestrator": {"action": "route", "decision_id": 7, "decision": {"kind": "named"}},
        "error": "partial"})
    assert deleted_transcriptions == ["job-2"]
    assert voices == [("send", {
        "session": "rachel", "trace_id": "t-2", "utterance_id": "u-2", "text": "tell rachel",
        "detail": {"orchestrator": "route", "dispatch": "queued", "client_msg_id": "m-2"}})]


# ---- /log -----------------------------------------------------------------

def test_log_bad_numbers_fall_back_to_defaults(running_server, monkeypatch):
    base, _ctx, _srv = running_server
    seen = []
    real = server_module.load_conversation

    def load(**kwargs):
        seen.append({k: kwargs[k] for k in ("session", "limit", "after_revision")})
        return real(**kwargs)
    monkeypatch.setattr(server_module, "load_conversation", load)
    status, body = _call(base, "GET", "/log?session=claude&limit=x&after_revision=y")
    assert status == 200 and isinstance(body, dict)
    assert seen == [{"session": "claude", "limit": 100, "after_revision": 0}]


@pytest.mark.parametrize("error, expected", [
    ("cursor", (409, {"error": "unknown history cursor"})),
    ("os", (500, {"error": "transcript unreadable"})),
])
def test_log_load_failures(running_server, monkeypatch, error, expected):
    from lib.message_previews import UnknownHistoryCursor
    base, _ctx, _srv = running_server

    def load(**_kwargs):
        if error == "cursor":
            raise UnknownHistoryCursor("m1")
        raise OSError("transcript unreadable")
    monkeypatch.setattr(server_module, "load_conversation", load)
    assert _call(base, "GET", "/log?session=claude&before=m1") == expected


def test_log_pair_session_unknown_cursor(running_server, monkeypatch):
    from lib import agent_conversations
    from lib.message_previews import UnknownHistoryCursor
    base, _ctx, _srv = running_server
    monkeypatch.setattr(agent_conversations, "is_pair_session", lambda session: True)

    def load_timeline(session, **_kwargs):
        raise UnknownHistoryCursor("m1")
    monkeypatch.setattr(agent_conversations, "load_timeline", load_timeline)
    assert _call(base, "GET", "/log?session=pair:a:b&before=m1") == (
        409, {"error": "unknown history cursor"})


def test_log_delivery_state_failure(running_server, monkeypatch):
    from lib import server_identity
    base, _ctx, _srv = running_server
    monkeypatch.setattr(server_identity, "feature_advertised", lambda name: True)

    def broken(self, body_obj, session, qs):
        raise RuntimeError("runtime unreachable")
    monkeypatch.setattr(server_module.Handler, "_add_delivery_state", broken)
    assert _call(base, "GET", "/log?session=claude") == (
        500, {"error": "delivery state unavailable"})
