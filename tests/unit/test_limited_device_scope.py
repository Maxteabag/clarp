"""A limited-scope device cannot reach the full-access surfaces it bypassed.

HTTP-01: the interactive agent terminal. HTTP-02: PATCH /schedules/<id>, which
PUT and DELETE already refuse. HTTP-03: host directory listing (/dirs) and
every project's past sessions (/past-sessions?scope=all); protocol.md says host
filesystem browsing needs full scope.
"""
from __future__ import annotations

import pytest

import server as host
from lib import terminal_ws


def _handler(path, *, scope="limited"):
    handler = host.Handler.__new__(host.Handler)
    handler.path = path
    handler._request_device_scope = scope
    handler._request_auth_validated = True
    handler._request_principal = "synthetic-device"
    handler._authorized = lambda: True
    handler.denied = []
    handler.reached = []
    handler._json_error = lambda status, _message: handler.denied.append(status)
    return handler


def test_a_limited_device_cannot_open_an_agent_terminal(monkeypatch):
    handler = _handler("/terminal/synthetic")
    monkeypatch.setattr(terminal_ws, "serve_terminal",
                        lambda *_a: handler.reached.append("terminal"))
    handler.do_GET()
    assert handler.denied == [403] and not handler.reached


def test_a_limited_device_cannot_patch_a_schedule(monkeypatch):
    handler = _handler("/schedules/synthetic")
    handler._handle_schedule_patch = lambda *_a: handler.reached.append("patch")
    handler.do_PATCH()
    assert handler.denied == [403] and not handler.reached


@pytest.mark.parametrize("path", ["/dirs?path=/", "/past-sessions?cwd=/&scope=all"])
def test_a_limited_device_cannot_browse_host_directories(monkeypatch, path):
    from lib import backends
    handler = _handler(path)
    handler._send = lambda status, *_a, **_k: handler.reached.append(status)
    monkeypatch.setattr(backends, "list_sessions",
                        lambda *_a, **_k: handler.reached.append("listed") or [])
    handler.do_GET()
    assert handler.denied == [403] and not handler.reached


def test_a_limited_device_still_lists_its_workspace_past_sessions(monkeypatch):
    from lib import backends
    handler = _handler("/past-sessions?cwd=/synthetic&scope=workspace")
    handler._send = lambda status, *_a, **_k: handler.reached.append(status)
    monkeypatch.setattr(backends, "list_sessions", lambda *_a, **_k: [])
    handler.do_GET()
    assert handler.denied == [] and handler.reached == [200]


def test_a_full_device_keeps_every_one_of_these(monkeypatch):
    handler = _handler("/terminal/synthetic", scope="full")
    monkeypatch.setattr(terminal_ws, "serve_terminal",
                        lambda *_a: handler.reached.append("terminal"))
    handler.do_GET()
    patch = _handler("/schedules/synthetic", scope="full")
    patch._handle_schedule_patch = lambda *_a: patch.reached.append("patch")
    patch.do_PATCH()
    assert handler.reached == ["terminal"] and patch.reached == ["patch"]
